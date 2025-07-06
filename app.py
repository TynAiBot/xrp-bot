import pandas as pd
import numpy as np
from flask import Flask, request
from threading import Thread
from datetime import datetime
import time
import requests
import os
from dotenv import load_dotenv
import ccxt  # Live chart data

# 📊 Bot instellingen
TRADING_SYMBOL = "XRP/USDT"
START_CAPITAL = 500.0
SPAREN_START = 500.0
LIVE_MODE = True  # Zet op True om live te gaan met virtueel geld
TRAILING_STOP_PERCENT = 0.03  # 3% trailing stop

# .env laden voor Telegram (XRP specifieke)
load_dotenv()  # gebruikt standaard .env-bestand op Render
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print(f"[DEBUG] Token: {TELEGRAM_BOT_TOKEN[:10]}... | Chat ID: {TELEGRAM_CHAT_ID}")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("[FOUT] Telegram-configuratie ontbreekt! Check je .env bestand.")
    exit(1)

# 🧠 Globale variabelen
in_position = False
entry_price = 0.0
capital = START_CAPITAL
sparen = SPAREN_START
last_action = ""
trailing_stop_price = 0.0

# 📣 Telegram notificatie

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, data=payload)
        print(f"[TELEGRAM STATUS] {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# 🔁 Webhook endpoint

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, entry_price, capital, sparen, trailing_stop_price
    data = request.json
    source = data.get("source", "onbekend")  # Bron van het signaal
    actie = data.get("action")
  
    prijs = float(data.get("price", 0))
    if prijs <= 0:
        return "Fout: Ongeldige prijs ontvangen", 400

    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    if actie == "buy" and not in_position:
        entry_price = prijs
        trailing_stop_price = prijs * (1 - TRAILING_STOP_PERCENT)
        in_position = True
        send_telegram_message(
            f"🟢 <b>[XRP/USDT] AANKOOP</b>\n"
            f"📹 Koopprijs: ${prijs:.4f}\n"
            f"🧠 Signaalbron: {source}\n"
            f"💰 Handelssaldo: €{capital:.2f}\n"
            f"💼 Spaarrekening: €{sparen:.2f}\n"
            f"📈 Totale waarde: €{capital + sparen:.2f}\n"
            f"🔐 Tradebedrag: €{START_CAPITAL:.2f}\n"
            f"🔗 Tijd: {timestamp}"
        )

    elif actie == "sell" and in_position:
        if entry_price == 0:
            print("[FOUT] Verkoop ontvangen zonder geldige aankoop.")
            return "Fout: Geen geldige aankoopprijs", 400

        verkoop_bedrag = prijs * START_CAPITAL / entry_price
        winst_bedrag = round(verkoop_bedrag - START_CAPITAL, 2)

        if winst_bedrag > 0:
            sparen += (2 / 3) * winst_bedrag
            capital += (1 / 3) * winst_bedrag
        else:
            capital += winst_bedrag

        if capital < START_CAPITAL:
            tekort = START_CAPITAL - capital
            if sparen >= tekort:
                sparen -= tekort
                capital += tekort

        in_position = False
        resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
        send_telegram_message(
            f"📄 <b>[XRP/USDT] VERKOOP</b>\n"
            f"📹 Verkoopprijs: ${prijs:.4f}\n"
            f"💰 Handelssaldo: €{capital:.2f}\n"
            f"💼 Spaarrekening: €{sparen:.2f}\n"
            f"📈 {resultaat}: €{winst_bedrag:.2f}\n"
            f"📈 Totale waarde: €{capital + sparen:.2f}\n"
            f"🔐 Tradebedrag: €{START_CAPITAL:.2f}\n"
            f"🔗 Tijd: {timestamp}"
        )
    return "OK", 200

@app.route("/health", methods=["GET"])
def health_check():
    return "OK", 200

# 🔴 Live Trading Simulatie

def execute_trade(df, simulate=True):
    prijs = df['close'].iloc[-1]
    webhook_data = {'action': 'buy', 'price': prijs}
    response = requests.post("http://127.0.0.1:5001/webhook", json=webhook_data)
    return f"[TRADE LOGIC] Koopactie verzonden: {response.status_code}"

last_ma200 = 0
last_supertrend = True
last_rsi = 50

# ➕ Indicatorfuncties

def compute_rsi(series, period):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_supertrend(df, period=10, multiplier=3):
    hl2 = (df['high'] + df['low']) / 2
    atr = df['high'].combine(df['low'], max) - df['low'].combine(df['high'], min)
    atr = atr.rolling(window=period).mean()
    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr
    supertrend = [np.nan] * len(df)
    trend = True

    for i in range(1, len(df)):
        if df['close'][i] > upperband[i - 1]:
            trend = True
        elif df['close'][i] < lowerband[i - 1]:
            trend = False
        supertrend[i] = trend
    return supertrend

def compute_macd(df):
    df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
    df['EMA26'] = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = df['EMA12'] - df['EMA26']
    df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    return df

# 📈 Live chart ophalen via CoinGecko

def fetch_live_chart():
    print("[INFO] Live fetching uitgeschakeld voor BB/Ichimoku test. Gebruik TradingView-webhooks.")
    while True:
        time.sleep(60)

# ▶️ Start alles
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"✅ Webhook server draait op http://0.0.0.0:{port}/webhook")
    print("▶️ Simulatie gestart..." if not LIVE_MODE else "🚀 Live trading gestart...")

    t = Thread(target=fetch_live_chart)
    t.daemon = True
    t.start()

    send_telegram_message("✅ Testbericht vanuit de bot op Render")

    app.run(host="0.0.0.0", port=port)