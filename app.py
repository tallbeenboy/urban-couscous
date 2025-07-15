from werkzeug.security import generate_password_hash, check_password_hash
import os
import requests
import copy
from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone
import json

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Firestore init
key_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
cred = credentials.Certificate(json.loads(key_json))
firebase_admin.initialize_app(cred)
db = firestore.client()

API_KEY = 'd0v5lc9r01qmg3ukcs60d0v5lc9r01qmg3ukcs6g'

# -- Helper Functions --

def get_user():
    return session.get("username")

def ensure_user_exists(username):
    user_doc = db.collection("users").document(username)
    if not user_doc.get().exists:
        user_doc.set({})
        user_doc.collection("meta").document("account").set({"cash": 10000})

def load_user_data(username):
    owned = []
    cash = 0

    user_doc = db.collection("users").document(username)
    owned_ref = user_doc.collection("portfolio")
    for doc in owned_ref.stream():
        owned.append(doc.to_dict())

    meta_ref = user_doc.collection("meta").document("account")
    doc = meta_ref.get()

    if doc.exists:
        cash = doc.to_dict().get("cash", 0)
    else:
        print(f"⚠️ Account metadata missing for {username}, returning 0 cash")

    return owned, cash

def save_user_data(username, owned, cash):
    print(f"💾 Saving data for {username} — Cash: {cash}")
    user_doc = db.collection("users").document(username)
    portfolio_ref = user_doc.collection("portfolio")

    for doc in portfolio_ref.stream():
        portfolio_ref.document(doc.id).delete()

    for stock in owned:
        portfolio_ref.add(stock)

    meta_ref = user_doc.collection("meta").document("account")
    meta_ref.set({"cash": round(cash, 2)})

def get_price(symbol):
    url = f'https://finnhub.io/api/v1/quote?symbol={symbol}&token={API_KEY}'
    try:
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()
        price = data.get('c', 0)

        if price and price > 0:
            db.collection("prices").document(symbol).set({
                "price": price,
                "updated": datetime.now(timezone.utc)
            })
            return price
        else:
            fallback_doc = db.collection("prices").document(symbol).get()
            if fallback_doc.exists:
                fallback_price = fallback_doc.to_dict().get("price")
                print(f"⚠️ Falling back to cached price for {symbol}: {fallback_price}")
                return fallback_price
            return None
    except Exception as e:
        print(f"🔥 Error fetching price for {symbol}: {e}")
        fallback_doc = db.collection("prices").document(symbol).get()
        if fallback_doc.exists:
            return fallback_doc.to_dict().get("price")
        return None

def gen_rows(owned):
    rows = []
    for stock in owned:
        current_price = get_price(stock["symbol"])
        if stock["shares"] == 0:
            continue

        for row in rows:
            if stock["symbol"] == row["symbol"]:
                row["shares"] += stock["shares"]
                row["totalinvestment"] += round(stock["buyprice"] * stock["shares"], 2)
                row["currentprice"] = current_price
                row["currentvalue"] += round(current_price * stock["shares"], 2)
                row["gain"] += round((current_price * stock["shares"]) - (stock["buyprice"] * stock["shares"]), 2)
                break
        else:
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
    return sum(round(stock["currentvalue"], 2) for stock in rows)

def save_daily_history(username, cash, owned):
    now = datetime.now(timezone.utc)
    acc_value = round(stockvalue(owned) + cash, 2)
    stock_val = round(stockvalue(owned), 2)

    history_ref = db.collection("users").document(username).collection("history")
    latest_doc = list(history_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())

    if latest_doc:
        last_time = latest_doc[0].to_dict().get("timestamp")
        if last_time and (now - last_time).total_seconds() < 86400:
            return

    date_str = now.strftime("%Y-%m-%d_%H:%M:%S")
    history_ref.document(date_str).set({
        "accValue": acc_value,
        "stockValue": stock_val,
        "cash": round(cash, 2),
        "timestamp": now
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
        username = request.form.get("username").strip()
        password = request.form.get("password")

        user_doc = db.collection("users").document(username).get()
        if not user_doc.exists:
            return render_template("login.html", error="User does not exist.")

        stored_hash = user_doc.to_dict().get("password_hash")
        if not stored_hash or not check_password_hash(stored_hash, password):
            return render_template("login.html", error="Invalid password.")

        session["username"] = username
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/buy", methods=["POST"])
def buy():
    username = get_user()
    data = request.get_json()
    symbol = data.get("symbol", "").upper().strip()
    shares = float(data.get("shares", 0))

    price = get_price(symbol)
    owned, cash = load_user_data(username)

    total_cost = price * shares
    if total_cost > cash:
        return jsonify("Transaction failed: not enough cash")

    owned.append({"symbol": symbol, "buyprice": round(price, 2), "shares": round(shares, 2)})
    cash -= total_cost

    try:
        save_user_data(username, owned, cash)
        owned, cash = load_user_data(username)
        save_daily_history(username, cash, owned)
    except Exception as e:
        print("🔥 Firestore failed:", e)
        return jsonify("failed: firestore error")

    return jsonify(f"success: bought {shares} {symbol} at {price}")

@app.route("/updatevalues", methods=["POST"])
def update_values():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    owned, cash = load_user_data(username)
    stock_val = round(stockvalue(owned), 2)
    acc_value = round(stock_val + cash, 2)
    return jsonify({"stockValue": stock_val, "accValue": acc_value, "cash": round(cash, 2)})

@app.route("/history", methods=["POST"])
def history():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    docs = db.collection("users").document(username).collection("history").order_by("timestamp").stream()
    return jsonify({doc.id: doc.to_dict() for doc in docs})

@app.route("/testfirestore")
def test_firestore():
    try:
        db.collection("test").document("ping").set({"status": "ok"})
        return "Write worked!"
    except Exception as e:
        return f"Write failed: {e}"

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
    cash += price * shares
    owned = [s for s in owned if s["shares"] > 0]

    save_user_data(username, owned, cash)
    save_daily_history(username, cash, owned)

    return jsonify(f"successful transaction ({round(price * shares, 2)})")

@app.route("/allinvestments", methods=["POST"])
def get_rows():
    username = get_user()
    if not username:
        return jsonify("Not logged in"), 403

    owned, cash = load_user_data(username)
    rows = gen_rows(owned)
    return jsonify(rows)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")

        if not username or not password:
            return render_template("register.html", error="Username and password required.")

        user_doc = db.collection("users").document(username)
        if user_doc.get().exists:
            return render_template("register.html", error="User already exists.")

        password_hash = generate_password_hash(password)
        user_doc.set({
            "password_hash": password_hash
        })
        user_doc.collection("meta").document("account").set({"cash": 10000})

        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    users = db.collection("users").stream()
    leaderboard_data = []

    for user in users:
        username = user.id
        history_ref = db.collection("users").document(username).collection("history")
        latest = list(history_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())

        if latest:
            latest_data = latest[0].to_dict()
            acc_value = latest_data.get("accValue", 0)
            leaderboard_data.append({
                "username": username,
                "accValue": round(acc_value, 2)
            })

    sorted_leaderboard = sorted(leaderboard_data, key=lambda x: x["accValue"], reverse=True)
    return jsonify(sorted_leaderboard)


@app.route("/about")
def about():
    return render_template("about.html")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
