from werkzeug.security import generate_password_hash, check_password_hash
import os
import requests
import copy
from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone
from collections import Counter   # ✅ for top stocks aggregation
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

# --- Helper Functions ---
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
    for doc in user_doc.collection("portfolio").stream():
        owned.append(doc.to_dict())
    meta_doc = user_doc.collection("meta").document("account").get()
    cash = meta_doc.to_dict().get("cash", 0) if meta_doc.exists else 0
    return owned, cash

def save_user_data(username, owned, cash):
    user_doc = db.collection("users").document(username)
    portfolio_ref = user_doc.collection("portfolio")
    for doc in portfolio_ref.stream():
        portfolio_ref.document(doc.id).delete()
    for stock in owned:
        portfolio_ref.add(stock)
    user_doc.collection("meta").document("account").set({"cash": round(cash, 2)})

def get_price(symbol):
    url = f'https://finnhub.io/api/v1/quote?symbol={symbol}&token={API_KEY}'
    try:
        data = requests.get(url, timeout=3).json()
        price = data.get('c', 0)
        if price > 0:
            db.collection("prices").document(symbol).set({"price": price, "updated": datetime.now(timezone.utc)})
            return price
        doc = db.collection("prices").document(symbol).get()
        return doc.to_dict().get("price") if doc.exists else None
    except:
        doc = db.collection("prices").document(symbol).get()
        return doc.to_dict().get("price") if doc.exists else None

def gen_rows(owned):
    rows = []
    for stock in owned:
        if stock["shares"] == 0:
            continue
        current_price = get_price(stock["symbol"])
        for row in rows:
            if row["symbol"] == stock["symbol"]:
                row["shares"] += stock["shares"]
                row["totalinvestment"] += stock["shares"] * stock["buyprice"]
                row["currentvalue"] += stock["shares"] * current_price
                break
        else:
            rows.append({
                "symbol": stock["symbol"],
                "shares": stock["shares"],
                "totalinvestment": stock["shares"] * stock["buyprice"],
                "currentvalue": stock["shares"] * current_price
            })
    return rows

def stockvalue(owned):
    return sum(stock["currentvalue"] for stock in gen_rows(owned))

def save_daily_history(username, cash, owned):
    now = datetime.now(timezone.utc)
    acc_value = round(stockvalue(owned) + cash, 2)
    history_ref = db.collection("users").document(username).collection("history")
    latest = list(history_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())
    if latest and (now - latest[0].to_dict().get("timestamp")).total_seconds() < 86400:
        return
    history_ref.document(now.strftime("%Y-%m-%d_%H:%M:%S")).set({
        "accValue": acc_value,
        "cash": round(cash, 2),
        "timestamp": now,
        # ✅ store stocks in history for stats
        "stocks": {s["symbol"]: round(s["shares"] * s["buyprice"], 2) for s in owned}
    })

# --- Routes ---
@app.route("/")
def index():
    return render_template("home.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        doc = db.collection("users").document(username).get()
        if not doc.exists or not check_password_hash(doc.to_dict().get("password_hash",""), password):
            return render_template("login.html", error="Invalid credentials.")
        session["username"] = username
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if not get_user(): return redirect(url_for("login"))
    return render_template("index.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/price", methods=["POST"])
def return_price():
    symbol = request.get_json().get("symbol","").upper()
    price = get_price(symbol)
    return jsonify({"price": price}) if price else (jsonify({"error":"Failed to fetch"}),500)

@app.route("/buy", methods=["POST"])
def buy():
    username = get_user()
    data = request.get_json()
    symbol, shares = data["symbol"].upper(), float(data["shares"])
    price = get_price(symbol)
    owned, cash = load_user_data(username)
    if price * shares > cash: return jsonify("Not enough cash")
    owned.append({"symbol":symbol,"buyprice":price,"shares":shares})
    save_user_data(username, owned, cash - price*shares)
    save_daily_history(username, cash - price*shares, owned)
    return jsonify("Buy success")

@app.route("/sell", methods=["POST"])
def sell():
    username = get_user()
    data = request.get_json()
    symbol, shares = data["symbol"].upper(), float(data["shares"])
    owned, cash = load_user_data(username)
    sold = 0
    for s in owned:
        if s["symbol"] == symbol:
            if s["shares"] <= shares:
                sold += s["shares"]; s["shares"] = 0
            else:
                s["shares"] -= shares; sold = shares; break
    if sold < shares: return jsonify("Not enough shares")
    owned = [s for s in owned if s["shares"]>0]
    cash += sold * get_price(symbol)
    save_user_data(username, owned, cash)
    save_daily_history(username, cash, owned)
    return jsonify("Sell success")

@app.route("/stats-data")
def stats_data():
    users = db.collection("users").stream()
    total_value, count, highest_value, highest_user = 0, 0, 0, None
    stock_totals = Counter()
    for user in users:
        username = user.id
        latest = list(db.collection("users").document(username).collection("history")
                      .order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream())
        if not latest: continue
        data = latest[0].to_dict()
        val = data.get("accValue",0)
        total_value += val; count += 1
        if val > highest_value: highest_value, highest_user = val, username
        stock_totals.update(data.get("stocks",{}))
    avg = round(total_value/count,2) if count else 0
    top_stocks = [{"symbol":s,"investment":round(v,2)} for s,v in stock_totals.most_common(3)]
    return jsonify({"averageValue":avg,"highestValue":round(highest_value,2),"highestUser":highest_user,"topStocks":top_stocks})

@app.route("/statistics")
def statistics_page():
    return render_template("statistics.html")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
