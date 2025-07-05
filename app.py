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
    actie = data.get("action")
  
    prijs = float(data.get("price", 0))
    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    if actie == "buy" and not in_position:
        if prijs > 0:
            entry_price = prijs
            trailing_stop_price = prijs * (1 - TRAILING_STOP_PERCENT)
            in_position = True
            send_telegram_message(
                f"🟢 <b>[XRP/USDT] AANKOOP</b>\n"
                f"📹 Koopprijs: ${prijs:.2f}\n"
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

        winst = round((prijs - entry_price) / entry_price * START_CAPITAL, 2)
        capital += winst
        sparen += winst
        in_position = False
        resultaat = "Winst" if winst >= 0 else "Verlies"
        send_telegram_message(
            f"📤 <b>[XRP/USDT] VERKOOP</b>\n"
            f"📹 Verkoopprijs: ${prijs:.2f}\n"
            f"💰 Handelssaldo: €{capital:.2f}\n"
            f"💼 Spaarrekening: €{sparen:.2f}\n"
            f"📈 {resultaat}: €{winst:.2f}\n"
            f"📈 Totale waarde: €{capital + sparen:.2f}\n"
            f"🔐 Tradebedrag: €{START_CAPITAL:.2f}\n"
            f"🔗 Tijd: {timestamp}"
        )
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

# 📈 Live chart ophalen en analyseren

def fetch_live_chart():
    global in_position, trailing_stop_price, last_ma200, last_supertrend, last_rsi
    exchange = ccxt.binance()
    while True:
        for tf in ['1m', '5m']:
            try:
                print(f"\n--- Live chart ({tf}) [XRP/USDT] ---")
                ohlcv = exchange.fetch_ohlcv(TRADING_SYMBOL, timeframe=tf, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

                df['MA200'] = df['close'].rolling(window=200).mean()
                df['MA20'] = df['close'].rolling(window=20).mean()
                df['MA50'] = df['close'].rolling(window=50).mean()
                df['RSI'] = compute_rsi(df['close'], 14)
                df['Supertrend'] = compute_supertrend(df)
                df = compute_macd(df)

                current_price = df['close'].iloc[-1]

                # Signal logica (4 signalen)
                rsi_signal = df['RSI'].iloc[-1] < 30
                ma_cross_signal = df['MA20'].iloc[-2] < df['MA50'].iloc[-2] and df['MA20'].iloc[-1] > df['MA50'].iloc[-1]
                trend_signal = current_price > df['MA200'].iloc[-1]
                macd_signal = df['MACD'].iloc[-1] > df['MACD_signal'].iloc[-1]

                active_signals = sum([rsi_signal, ma_cross_signal, trend_signal, macd_signal])

                if in_position and current_price > entry_price:
                    new_trail = current_price * (1 - TRAILING_STOP_PERCENT)
                    if new_trail > trailing_stop_price:
                        trailing_stop_price = new_trail

                if in_position and current_price <= trailing_stop_price:
                    print(f"[TRAILING STOP] Koers onder trailing stop: {current_price} <= {trailing_stop_price}")
                    webhook_data = {'action': 'sell', 'price': current_price}
                    requests.post("http://127.0.0.1:5001/webhook", json=webhook_data)

                if not in_position and active_signals >= 2:
                    print(f"[SIGNALEN] Actieve signalen: {active_signals}/4 → KOOP")
                    result = execute_trade(df, simulate=not LIVE_MODE)
                    print(result)
                else:
                    print(f"[SIGNALEN] Actieve signalen: {active_signals}/4 → GEEN ACTIE")

            except Exception as e:
                print(f"[LIVE FETCH ERROR - {tf}] {e}")
        time.sleep(60)

# ▶️ Start alles
if __name__ == "__main__":
    print("✅ Webhook server draait op http://0.0.0.0:PORT/webhook (Render public URL)")
    print("▶️ Simulatie gestart..." if not LIVE_MODE else "🚀 Live trading gestart...")
    Thread(target=fetch_live_chart).start()
    send_telegram_message("✅ Testbericht vanuit de bot op Render")

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))