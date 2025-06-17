import os
import requests
import copy
from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Firestore init
cred = credentials.Certificate("mockdesk-5c364-firebase-adminsdk-fbsvc-4dd4f8d312.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

API_KEY = 'd0v5lc9r01qmg3ukcs60d0v5lc9r01qmg3ukcs6g'  # your finnhub API key

# -- Helper Functions --

def get_user():
    return session.get("username")

def ensure_user_exists(username):
    user_doc = db.collection("users").document(username)
    if not user_doc.get().exists:
        user_doc.set({})
        meta_ref = user_doc.collection("meta").document("account")
        meta_ref.set({"cash": 10000})

def load_user_data(username):
    owned = []
    cash = 10000

    owned_ref = db.collection("users").document(username).collection("portfolio")
    for doc in owned_ref.stream():
        owned.append(doc.to_dict())

    meta_ref = db.collection("users").document(username).collection("meta").document("account")
    doc = meta_ref.get()
    if doc.exists:
        cash = doc.to_dict().get("cash", 10000)

    return owned, cash

def save_user_data(username, owned, cash):
    user_doc = db.collection("users").document(username)
    portfolio_ref = user_doc.collection("portfolio")

    # Clear portfolio collection before saving new data
    for doc in portfolio_ref.stream():
        portfolio_ref.document(doc.id).delete()

    for stock in owned:
        portfolio_ref.add(stock)

    meta_ref = user_doc.collection("meta").document("account")
    meta_ref.set({"cash": round(cash, 2)})

def get_price(symbol):
    url = f'https://finnhub.io/api/v1/quote?symbol={symbol}&token={API_KEY}'
    response = requests.get(url)
    data = response.json()
    return data.get('c', 0)  # current price or 0 if not found

def gen_rows(owned):
    rows = []
    for stock in owned:
        current_price = get_price(stock["symbol"])
        existing = False

        if stock["shares"] == 0:
            continue

        for row in rows:
            if stock["symbol"] == row["symbol"]:
                existing = True
                row["shares"] += stock["shares"]
                row["totalinvestment"] += round(stock["buyprice"] * stock["shares"], 2)
                # For currentprice, keep latest price only
                row["currentprice"] = current_price
                row["currentvalue"] += round(current_price * stock["shares"], 2)
                row["gain"] += round((current_price * stock["shares"]) - (stock["buyprice"] * stock["shares"]), 2)
                break

        if not existing:
            rows.append({
                "symbol": stock["symbol"],
                "shares": stock["shares"],
                "totalinvestment": round(stock["shares"] * stock["buyprice"], 2),
                "currentprice": current_price,
                "currentvalue": round(current_price * stock["shares"], 2),
                "gain": round((current_price * stock["shares"]) - (stock["buyprice"] * stock["shares"]), 2)
            })
    return rows

def stockvalue(owned):
    rows = gen_rows(owned)
    value = 0
    for stock in rows:
        value += round(stock["currentvalue"], 2)
    return value

def save_daily_history(username, cash, owned):
    acc_value = round(stockvalue(owned) + cash, 2)
    stock_val = round(stockvalue(owned), 2)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    history_ref = db.collection("users").document(username).collection("history").document(date_str)
    history_ref.set({
        "accValue": acc_value,
        "stockValue": stock_val,
        "cash": round(cash, 2),
        "timestamp": datetime.utcnow()
    })

# -- Routes --

@app.route("/")
def index():
    if not get_user():
        return redirect(url_for("login"))
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            return render_template("login.html", error="Please enter a username")
        ensure_user_exists(username)
        session["username"] = username
        return redirect(url_for("index"))
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/price", methods=["POST"])
def price():
    if not get_user():
        return jsonify("Not logged in"), 403

    data = request.get_json()
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify("No symbol provided"), 400

    price = get_price(symbol)
    if price == 0:
        return jsonify("Symbol not found or no price"), 404

    return jsonify(str(price))

@app.route("/buy", methods=["POST"])
def buy():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    data = request.get_json()
    symbol = data.get("symbol", "").upper().strip()
    shares = float(data.get("shares", 0))

    if shares < 1:
        return jsonify("failed transaction: invalid shares")

    price = get_price(symbol)
    if price == 0:
        return jsonify("failed transaction: symbol doesn't exist")

    owned, cash = load_user_data(username)

    if price * shares > cash:
        return jsonify("failed transaction: not enough cash")

    owned.append({"symbol": symbol, "buyprice": round(price, 2), "shares": round(shares, 2)})
    cash -= price * shares

    save_user_data(username, owned, cash)
    save_daily_history(username, cash, owned)

    return jsonify(f"successful transaction (${round(price * shares, 2)})")

@app.route("/allinvestments", methods=["POST"])
def get_rows():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    owned, cash = load_user_data(username)
    rows = gen_rows(owned)
    return jsonify(rows)

@app.route("/sell", methods=["POST"])
def sell():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    data = request.get_json()
    symbol = data.get("symbol", "").upper().strip()
    shares = float(data.get("shares", 0))

    if shares < 1:
        return jsonify("failed transaction: invalid shares")

    owned, cash = load_user_data(username)

    old_owned = copy.deepcopy(owned)
    subtracted = 0

    for stock in owned:
        if stock["symbol"] == symbol:
            if stock["shares"] < shares:
                subtracted += stock["shares"]
                stock["shares"] = 0
            else:
                subtracted = shares
                stock["shares"] -= shares
            if subtracted == shares:
                break

    if subtracted < shares:
        return jsonify("transaction failed: you don't own enough shares")

    price = get_price(symbol)
    diff = shares * price

    cash += diff

    # Remove stocks with 0 shares
    owned = [s for s in owned if s["shares"] > 0]

    save_user_data(username, owned, cash)
    save_daily_history(username, cash, owned)

    return jsonify(f"successful transaction ({round(diff, 2)})")

@app.route("/updatevalues", methods=["POST"])
def update_values():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    owned, cash = load_user_data(username)

    stock_val = round(stockvalue(owned), 2)
    acc_value = round(stock_val + cash, 2)
    cash = round(cash, 2)

    data = {"stockValue": stock_val, "accValue": acc_value, "cash": cash}
    return jsonify(data)

@app.route("/history", methods=["POST"])
def history():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    history_ref = db.collection("users").document(username).collection("history")
    docs = history_ref.order_by("timestamp").stream()

    hist_data = {}
    for doc in docs:
        data = doc.to_dict()
        date = doc.id
        hist_data[date] = {
            "accValue": data.get("accValue", 0),
            "stockValue": data.get("stockValue", 0),
            "cash": data.get("cash", 0)
        }

    return jsonify(hist_data)

if __name__ == "__main__":
    app.run(debug=True)
