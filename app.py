import pandas as pd
import numpy as np
from flask import Flask, request
from threading import Thread
from datetime import datetime
import time
import requests
import os
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import subprocess

# === CONFIG ===
TRADING_SYMBOL = "XRP/USDT"
START_CAPITAL = 500.0
SPAREN_START = 500.0
LIVE_MODE = True
TRAILING_STOP_PERCENT = 0.025
SIGNAL_THRESHOLD = 1  # Koop bij 1 signaal: halftrend_buy of signals >= 1

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("[FOUT] Telegram-configuratie ontbreekt! Check je .env bestand.")
    exit(1)

in_position = False
entry_price = 0.0
capital = START_CAPITAL
sparen = SPAREN_START
trailing_stop_price = 0.0
trade_history = []

# === TELEGRAM ===
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, data=payload)
        print(f"[TELEGRAM] {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# === HALF TREND PLACEHOLDER ===
def detect_halftrend_signals(df):
    df['buy_signal'] = df['close'] > df['open']  # Simpele placeholder
    df['sell_signal'] = df['close'] < df['open']  # Simpele placeholder
    return df

# === WEBHOOK SERVER ===
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, entry_price, capital, sparen, trailing_stop_price
    data = request.json
    actie = data.get("action")
    prijs = data.get("price")
    signalen = data.get("signals", 0)
    halftrend_buy = data.get("halftrend_buy", False)
    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    if not prijs:
        try:
            prijs = get_price_coingecko()
        except Exception as e:
            print(f"[FOUT] Kan prijs niet ophalen: {e}")
            return "Fout", 500
    else:
        prijs = float(prijs)

    print(f"[SIGNALEN] Halftrend BUY: {'✅' if halftrend_buy else '❌'} | Actie: {actie} | Prijs: ${prijs:.4f}")
    print(f"[STATUS] In positie: {in_position} | Entry: ${entry_price:.4f}")

    mag_kopen = halftrend_buy or signalen >= SIGNAL_THRESHOLD

    if actie == "buy" and not in_position:
        if not mag_kopen:
            print("[INFO] Koopsignaal genegeerd: voorwaarden niet voldaan")
        else:
            entry_price = prijs
            trailing_stop_price = prijs * (1 - TRAILING_STOP_PERCENT)
            in_position = True
            trade_history.append((timestamp, prijs, "BUY"))
            send_telegram_message(
                f"🟢 <b>[AANKOOP] XRP Halftrend</b>\n"
                f"📹 Koopprijs: ${prijs:.4f}\n"
                f"💰 Handelssaldo: €{capital:.2f}\n"
                f"💼 Spaarrekening: €{sparen:.2f}\n"
                f"📈 Totale waarde: €{capital + sparen:.2f}\n"
                f"🔗 Tijd: {timestamp}"
            )

    elif actie == "sell" and in_position:
        winst = round((prijs - entry_price) / entry_price * capital, 2)
        capital += winst
        sparen += winst
        in_position = False
        trade_history.append((timestamp, prijs, "SELL"))
        send_telegram_message(
            f"📤 <b>[VERKOOP] XRP Halftrend</b>\n"
            f"📹 Verkoopprijs: ${prijs:.4f}\n"
            f"💰 Handelssaldo: €{capital:.2f}\n"
            f"💼 Spaarrekening: €{sparen:.2f}\n"
            f"📈 Winst: €{winst:.2f}\n"
            f"📈 Totale waarde: €{capital + sparen:.2f}\n"
            f"🔗 Tijd: {timestamp}"
        )

    return "OK", 200

# === CHART + SIGNALS ===
def fetch_and_prepare_chart():
    try:
        url = "https://api.coingecko.com/api/v3/coins/ripple/market_chart?vs_currency=usd&days=1&interval=minutely"
        response = requests.get(url)
        data = response.json()
        prices = data["prices"]
        df = pd.DataFrame(prices, columns=["timestamp", "close"])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['open'] = df['close'].shift(1).fillna(df['close'])
        df['high'] = df[['open', 'close']].max(axis=1)
        df['low'] = df[['open', 'close']].min(axis=1)
        df['volume'] = 0
        df = detect_halftrend_signals(df)
        df.to_csv("xrp_chart.csv", index=False)
        return df
    except Exception as e:
        print(f"[FOUT] Ophalen mislukt: {e}")
        return None

# === COINGECKO PRICE ===
def get_price_coingecko():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd"
    response = requests.get(url)
    prijs = response.json()['ripple']['usd']
    return prijs

# === NGROK ===
def start_ngrok():
    try:
        ngrok_path = "C:/Users/lapto/Documents/ngrok/ngrok.exe"
        subprocess.Popen([ngrok_path, 'http', '5000'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("🌐 Ngrok gestart op poort 5000 (achtergrond)")
    except Exception as e:
        print(f"[FOUT] Ngrok start mislukt: {e}")

# === DELAYED TEST ===
def delayed_test():
    time.sleep(6)
    df = fetch_and_prepare_chart()
    if df is not None:
        prijs = df['close'].iloc[-1]
        halftrend_buy = df['buy_signal'].iloc[-1]
        data = {"action": "buy", "price": prijs, "signals": 1, "halftrend_buy": bool(halftrend_buy)}
        try:
            requests.post("http://localhost:5000/webhook", json=data)
        except Exception as e:
            print(f"[FOUT] Test-webhook mislukt: {e}")

# === START ===
if __name__ == "__main__":
    print("✅ XRP Halftrend webhook draait op /webhook")
    send_telegram_message(
        f"📡 <b>[XRP Halftrend BOT] LIVE</b>\n🔔 Mode: {'LIVE' if LIVE_MODE else 'SIMULATIE'}\n🕒 {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )

    if not os.environ.get("RENDER", False):
        start_ngrok()
        if not LIVE_MODE:
            Thread(target=delayed_test).start()

    try:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    finally:
        print("[EINDE] Bot gestopt.")