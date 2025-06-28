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
        meta_ref = user_doc.collection("meta").document("account")
        meta_ref.set({"cash": 10000})

def load_user_data(username):
    owned = []
    cash = None

    user_doc = db.collection("users").document(username)
    owned_ref = user_doc.collection("portfolio")
    for doc in owned_ref.stream():
        owned.append(doc.to_dict())

    meta_ref = user_doc.collection("meta").document("account")
    doc = meta_ref.get()

    if not doc.exists:
        print("⚠️ No account metadata found, creating new one with $10,000")
        cash = 10000
        meta_ref.set({"cash": cash})
    else:
        cash = doc.to_dict().get("cash")
        if cash is None:
            print("⚠️ Account exists but no cash field, defaulting to $10,000")
            cash = 10000
            meta_ref.set({"cash": cash})

    return owned, cash

def save_user_data(username, owned, cash):
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
        username = request.form.get("username")
        if username:
            session["username"] = username
            ensure_user_exists(username)
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid username")
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
