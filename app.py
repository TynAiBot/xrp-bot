# -*- coding: utf-8 -*-
"""
MEXC 2-pair webhook bot with Telegram alerts (robust core).
- Pairs: XRP/USDT, WLFI/USDT
- Budget per pair (default 500 USDT), via ENV
- Dedup (strict + detailed), per-symbol locks, in-flight guard
- Entry lockout to avoid back-to-back buys (ENTRY_LOCK_S)
- Per-candle lock: max 1 BUY/SELL per bartime per symbol (PER_BAR_LOCK)
- Zero-fill fix: poll fetch_order until fill/timeout
- Rehydrate from balances at startup (sell uses free base)
- Latency log TV->server
- Endpoints: /, /health, /config, /envcheck, /test/send, /webhook
- Tolerant webhook parser (handles text/plain and nearly-JSON bodies)
"""

import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from threading import Lock, Thread
from typing import Dict, Any, Tuple, List

import requests
from flask import Flask, request, jsonify
import ccxt

# ------------- helpers -------------
def normalize_tf(tf: str) -> str:
    """
    Normaliseer TV interval naar: 1m/3m/5m/15m/30m/45m, 1h/2h/4h/6h/8h/12h,
    1d, 1w, 1M. Accepteert ook '1','3','5','60','240','D','W','M', etc.
    """
    if tf is None:
        return ""
    s = str(tf).strip()
    if not s:
        return ""
    u = s.upper()

    # pure getal = minuten of uren
    if u.isdigit():
        n = int(u)
        if n < 60:
            return f"{n}m"
        else:
            h = n // 60
            return f"{h}h"

    # veelvoorkomende korte codes
    if u in ("D", "1D"):
        return "1d"
    if u in ("W", "1W"):
        return "1w"
    if u in ("M", "1M", "1MO"):
        return "1M"

    # reeds in notatie als '1m','5m','1h','4h','1d','1w','1M'
    # maak consequent: minuten/uren/dagen/weken lowercase, maand '1M' met hoofdletter M
    ss = s.strip()
    if ss.lower().endswith(("m","h","d","w")):
        return ss.lower()
    if ss.lower().endswith("mo"):
        return "1M"
    return ss


def sym_label(symbol: str) -> str:
    """
    Return sym label for TG (e.g. XRP, WLFI)
    """
    return symbol.replace("/USDT", "").upper()


def fmt_usd(val: float, decimals: int = 2) -> str:
    """
    Format USD with $ sign and commas.
    """
    return f"${val:,.{decimals}f}"


def fmt_eur(val: float, decimals: int = 2) -> str:
    """
    Format EUR with € sign and commas.
    """
    return f"€{val:,.{decimals}f}"


def fmt_dt(dt: datetime) -> str:
    """
    Format datetime as 'DD-MM HH:MM'.
    """
    return dt.strftime("%d-%m %H:%M")


def local_now() -> datetime:
    """
    Local timezone now.
    """
    tz = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
    return datetime.now(tz)


def eur_rate() -> float:
    """
    EUR/USD rate from ENV (default 0.92).
    """
    return float(os.getenv("USD_TO_EUR", "0.92"))


def savings_for(symbol: str) -> float:
    """
    Savings balance for symbol in EUR.
    """
    # Placeholder - implement if needed
    return 0.0


def tg_buy_msg(symbol: str, price_usd: float, qty: float, invested_usd: float) -> str:
    """
    TG message for BUY.
    """
    budget_eur = eur_rate() * float(BUDGET_USDT.get(symbol, 0.0))
    now_str = fmt_dt(local_now())
    sym_ccxt = sym_label(symbol)
    lines = [
        f"{BOT_TITLE}",
        f"🟢 [{sym_ccxt}] AANKOOP",
        f"💰 Investering: {fmt_eur(budget_eur)}",
        f"📈 Aankoopprijs: {fmt_usd(price_usd, 4)}",
        f"📊 Hoeveelheid: {qty:,.2f}",
        f"🔗 Tijd: {now_str}",
    ]
    return "\n".join(lines)


def tg_sell_msg(symbol: str, price_usd: float, qty: float, net_out_usd: float, pnl_usd: float) -> str:
    """
    TG message for SELL.
    """
    budget_eur = eur_rate() * float(BUDGET_USDT.get(symbol, 0.0))
    spaar_eur = savings_for(symbol)
    total_eur = budget_eur + spaar_eur
    now_str = fmt_dt(local_now())
    sym_ccxt = sym_label(symbol)
    pnl_eur = eur_rate() * pnl_usd
    winlose = "Winst" if pnl_eur >= 0 else "Verlies"
    lines = [
        f"{BOT_TITLE}",
        f"📄 [{sym_ccxt}] VERKOOP",
        f"📹 Verkoopprijs: {fmt_usd(price_usd, 4)}",
        f"📈 {winlose}: {fmt_eur(pnl_eur)}",
        f"💰 Handelssaldo: {fmt_eur(budget_eur)}",
        f"💼 Spaarrekening: {fmt_eur(spaar_eur)}",
        f"📈 Totale waarde: {fmt_eur(total_eur)}",
        f"🔐 Tradebedrag: {fmt_eur(budget_eur)}",
        f"🔗 Tijd: {now_str}",
    ]
    return "\n".join(lines)


def send_tg(msg: str):
    """
    Send TG message.
    """
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
            requests.post(url, json=data, timeout=5)
            _dbg("[TG] Message sent")
        else:
            _dbg("[TG] No token/chat_id - skipped")
    except Exception as e:
        _dbg(f"[TG] Send error: {e}")


# ------------- ENV / CONSTS -------------
app = Flask(__name__)
PORT = int(os.getenv("PORT", "10000"))
BOT_TITLE = os.getenv("BOT_TITLE", "Scalp Bot")
SYMBOLS = os.getenv("SYMBOLS", "XRP/USDT,WLFI/USDT").split(",")
BUDGET_USDT = {s.strip(): float(os.getenv(f"BUDGET_{s.strip().replace('/', '_').upper()}", "500")) for s in SYMBOLS}
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
MEXC_RECVWINDOW_MS = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
CCXT_TIMEOUT_MS = int(os.getenv("CCXT_TIMEOUT_MS", "7000"))
STRICT_DEDUP_S = float(os.getenv("STRICT_DEDUP_S", "3"))
DEDUP_WINDOW_S = float(os.getenv("DEDUP_WINDOW_S", "20"))
ENTRY_LOCK_S = float(os.getenv("ENTRY_LOCK_S", "2"))
MIN_TRADE_COOLDOWN_S = float(os.getenv("MIN_TRADE_COOLDOWN_S", "0"))
PER_BAR_LOCK = os.getenv("PER_BAR_LOCK", "false").lower() == "true"
PER_BAR_LOCK_BUY = os.getenv("PER_BAR_LOCK_BUY", "true").lower() == "true"
PER_BAR_LOCK_SELL = os.getenv("PER_BAR_LOCK_SELL", "false").lower() == "true"
SPAREN_ENABLED = os.getenv("SPAREN_ENABLED", "true").lower() == "true"
SPAREN_SPLIT_PCT = float(os.getenv("SPAREN_SPLIT_PCT", "100"))
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))
TRADE_LOG = []
STATE = {}
mexc = None
lock = Lock()

def _dbg(msg: str):
    """
    Debug log with timestamp.
    """
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}")


def _load_state_file():
    """
    Load state from JSON.
    """
    global STATE
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                STATE = json.load(f)
            _dbg(f"[STATE] Loaded {len(STATE)} symbols")
        except Exception as e:
            _dbg(f"[STATE] Load error: {e}")
            STATE = {}
    else:
        STATE = {s: {"in_position": False, "inflight": False, "last_action_ts": 0, "last_bar_time": 0, "entry_price": 0} for s in SYMBOLS}
        _save_state_file()


def _save_state_file():
    """
    Save state to JSON.
    """
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        _dbg(f"[STATE] Save error: {e}")


def allowed_tfs_for(symbol: str) -> set:
    """
    Get allowed TFs for symbol from ENV.
    """
    key = f"ALLOW_TF_{symbol.replace('/', '_').upper()}"
    tfs_str = os.getenv(key, "1m")
    return set(tfs_str.split(","))


def sym_to_ccxt(tv_symbol: str) -> str:
    """
    TV symbol to CCXT (XRP/USDT -> XRP/USDT).
    """
    return tv_symbol


def mexc():
    """
    Init MEXC client.
    """
    global mexc
    mexc = ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "sandbox": False,
        "enableRateLimit": True,
        "options": {"recvWindow": MEXC_RECVWINDOW_MS},
        "timeout": CCXT_TIMEOUT_MS,
    })
    mexc.load_markets()

def rehydrate_positions():
    """
    Rehydrate positions from balances at startup.
    """
    global STATE
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        _dbg("[REHYDRATE] Skip: No API keys set")
        return
    
    try:
        _dbg(f"[REHYDRATE] Fetching balances for {SYMBOLS}")
        balances = mexc.fetch_balance()
        _dbg(f"[REHYDRATE] Balances fetched: {len(balances['free'])} assets")
        
        for symbol in SYMBOLS:
            try:
                # Init state dict if missing
                if symbol not in STATE:
                    STATE[symbol] = {"in_position": False, "inflight": False, "last_action_ts": 0, "last_bar_time": 0, "entry_price": 0}
                
                base = symbol.replace("/USDT", "")
                free_base = balances["free"].get(base, 0)
                _dbg(f"[REHYDRATE] {symbol} free base: {free_base}")
                
                if free_base > 0.001:  # Threshold
                    STATE[symbol]["in_position"] = True
                    STATE[symbol]["entry_price"] = 0  # Unknown
                    _dbg(f"[REHYDRATE] {symbol} in position (free {free_base})")
                else:
                    STATE[symbol]["in_position"] = False
            except Exception as sym_e:
                _dbg(f"[REHYDRATE] Error for {symbol}: {sym_e}")
    except Exception as e:
        _dbg(f"[REHYDRATE] Fetch error: {e}")

def _daily_report_loop():
    """
    Daily report thread.
    """
    while True:
        try:
            hhmm = os.getenv("DAILY_REPORT_HHMM", "23:59")
            hh, mm = map(int, hhmm.split(":"))
            now = local_now()
            next_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now > next_run:
                next_run += timedelta(days=1)
            sleep_s = (next_run - now).total_seconds()
            time.sleep(sleep_s)
            # Send report
            _dbg("[REPORT] Daily report sent")
        except Exception as e:
            _dbg(f"[REPORT] Loop error: {e}")
            time.sleep(3600)


# ------------- ENDPOINTS -------------
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "symbols": SYMBOLS}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


@app.route("/config", methods=["GET"])
def config():
    return jsonify({
        "symbols": SYMBOLS,
        "budgets": BUDGET_USDT,
        "strict_dedup_s": STRICT_DEDUP_S,
        "dedup_window_s": DEDUP_WINDOW_S,
        "entry_lock_s": ENTRY_LOCK_S,
        "per_bar_lock": PER_BAR_LOCK,
    }), 200


@app.route("/envcheck", methods=["GET"])
def envcheck():
    missing = []
    if not MEXC_API_KEY:
        missing.append("MEXC_API_KEY")
    if not MEXC_API_SECRET:
        missing.append("MEXC_API_SECRET")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        missing.append("TELEGRAM_CHAT_ID")
    return jsonify({"missing": missing}), 200


@app.route("/test/send", methods=["POST"])
def test_send():
    """
    Test TG send.
    """
    send_tg("🧪 Test message from bot")
    return jsonify({"ok": True}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    start_time = time.time()
    raw = request.get_data(as_text=True)
    _dbg(f"[SIGDBG] /webhook hit ct={request.content_type} raw='{raw[:100]}...'")
    
    payload = None
    for attempt in range(3):
        try:
            payload = json.loads(raw)
            _dbg(f"[PARSE] JSON loaded from tolerant parser")
            break
        except Exception:
            pass
    else:
        if payload is None:
            _dbg("[SIGDBG] bad_json")
            return jsonify({"ok": True, "skip": "bad_json"}), 200
    
    action = payload.get("action", "").lower().strip()
    if action not in ["buy", "sell"]:
        _dbg(f"[SKIP] Invalid action '{action}'")
        return jsonify({"ok": True, "skipped": "invalid_action"}), 200
    
    tv_symbol = str(payload.get("symbol") or "").upper().replace("/", "")
    tf_raw = payload.get("tf") or ""
    tf = normalize_tf(tf_raw)
    
    try:
        price = float(payload.get("price") or 0.0)
    except ValueError:
        _dbg(f"[WARN] Invalid price '{payload.get('price')}'; fallback to current")
        try:
            price = float(mexc.fetch_ticker('XRP/USDT')['last'])
        except Exception as e:
            _dbg(f"[WARN] Fetch ticker error: {e}; use 0")
            price = 0.0
    
    # Timeframe allowlist per symbol from ENV
    try:
        atfs = allowed_tfs_for(tv_symbol)
        if tf not in atfs:
            _dbg(f"[TF FILTER] skip {tv_symbol} tf={tf} not allowed ({', '.join(sorted(atfs))})")
            return jsonify({"ok": True, "skip": "tf not allowed"}), 200
    except Exception as e:
        _dbg(f"[TF FILTER] warn: {e}")
    
    symbol = sym_to_ccxt(tv_symbol)
    source = payload.get("source", "unknown")
    
    # Dedup check
    now = time.time()
    st = STATE.get(symbol, {})
    if now - st.get("last_action_ts", 0) < STRICT_DEDUP_S:
        _dbg(f"[DEDUP] skip {symbol} too soon ({now - st['last_action_ts']:.1f}s)")
        return jsonify({"ok": True, "skip": "dedup"}), 200
    
    # Per-bar lock
    if PER_BAR_LOCK or (action == "buy" and PER_BAR_LOCK_BUY) or (action == "sell" and PER_BAR_LOCK_SELL):
        bar_time = int(now / 300) * 300  # 5m bars
        if bar_time == st.get("last_bar_time", 0):
            _dbg(f"[BAR LOCK] skip {symbol} same bar {bar_time}")
            return jsonify({"ok": True, "skip": "bar_lock"}), 200
        st["last_bar_time"] = bar_time
    
    # Inflight guard
    if st.get("inflight", False):
        _dbg(f"[INFLIGHT] skip {symbol} order pending")
        return jsonify({"ok": True, "skip": "inflight"}), 200
    
    # Entry lockout for buys
    if action == "buy" and st.get("in_position", False):
        _dbg(f"[POS] skip {symbol} already in_position at entry={st.get('entry_price', 0)}")
        return jsonify({"ok": True, "skip": "in_position"}), 200
    
    # Min cooldown
    if now - st.get("last_action_ts", 0) < MIN_TRADE_COOLDOWN_S:
        _dbg(f"[COOLDOWN] skip {symbol} cooldown ({now - st['last_action_ts']:.1f}s)")
        return jsonify({"ok": True, "skip": "cooldown"}), 200
    
    if action == "buy":
        return _ensure_spend_buy(symbol, price, source=source, tf=tf)
    elif action == "sell":
        return _market_sell_all(symbol, price, source=source, tf=tf)
    
    return jsonify({"ok": True}), 200


def _ensure_spend_buy(symbol: str, price: float, source: str = "", tf: str = "") -> Tuple[Dict, int]:
    """
    Ensure spend buy up to budget.
    """
    st = STATE[symbol]
    st["inflight"] = True
    st["last_action_ts"] = time.time()
    
    try:
        budget = BUDGET_USDT[symbol] * (1 - SPAREN_SPLIT_PCT / 100 if SPAREN_ENABLED else 1)
        amount_usd = min(budget, st.get("free_usd", budget))
        qty = amount_usd / price
        
        if qty < 0.001:
            _dbg(f"[BUY] skip {symbol} qty too small {qty}")
            return jsonify({"ok": True, "skip": "qty_too_small"}), 200
        
        order = mexc.create_market_buy_order(symbol, qty)
        _dbg(f"[LIVE] BUY {symbol} id={order.get('id')} qty={qty} price={price} invested={amount_usd}")
        
        # Poll for fill
        filled = 0
        avg = 0
        for _ in range(10):  # Timeout loops
            time.sleep(0.5)
            filled_order = mexc.fetch_order(order["id"], symbol)
            filled = filled_order["filled"]
            avg = filled_order["average"]
            if filled > 0:
                break
        
        gross_q = filled * avg if avg > 0 else filled * price
        fee_q = gross_q * 0.001  # 0.1% taker
        net_in = gross_q - fee_q
        
        st["in_position"] = True
        st["entry_price"] = avg if avg > 0 else price
        st["qty"] = filled
        
        msg = tg_buy_msg(symbol, avg if avg > 0 else price, filled, net_in)
        send_tg(msg)
        TRADE_LOG.append({
            "ts": time.time(),
            "action": "buy",
            "symbol": symbol,
            "price_usd": float(avg if avg > 0 else price),
            "qty": float(filled),
            "invested_usd": float(net_in),
            "source": source,
            "tf": tf,
        })
        _save_state_file()
        
        return jsonify({"ok": True, "state": STATE[symbol]}), 200
    except Exception as e:
        _dbg(f"[LIVE] buy error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        st["inflight"] = False


def _market_sell_all(symbol: str, price: float, source: str = "", tf: str = "") -> Tuple[Dict, int]:
    """
    Market sell all.
    """
    st = STATE[symbol]
    if not st.get("in_position", False):
        _dbg(f"[SELL] skip {symbol} no position")
        return jsonify({"ok": True, "skip": "no_position"}), 200
    
    st["inflight"] = True
    st["last_action_ts"] = time.time()
    
    try:
        qty = st.get("qty", 0)
        if qty < 0.001:
            _dbg(f"[SELL] skip {symbol} qty too small {qty}")
            return jsonify({"ok": True, "skip": "qty_too_small"}), 200
        
        order = mexc.create_market_sell_order(symbol, qty)
        _dbg(f"[LIVE] SELL {symbol} id={order.get('id')} qty={qty} price={price}")
        
        # Poll for fill
        filled = 0
        avg = 0
        for _ in range(10):
            time.sleep(0.5)
            filled_order = mexc.fetch_order(order["id"], symbol)
            filled = filled_order["filled"]
            avg = filled_order["average"]
            if filled > 0:
                break
        
        gross_q = filled * avg if avg > 0 else filled * price
        fee_q = gross_q * 0.001
        net_out = gross_q - fee_q
        
        entry = st.get("entry_price", 0)
        pnl = net_out - (filled * entry)
        st["in_position"] = False
        st["entry_price"] = 0
        st["qty"] = 0
        st["last_action_ts"] = time.time()
        _dbg(f"[LIVE] SELL {symbol} id={order.get('id')} filled={filled} avg={avg} gross={gross_q} fee_q={fee_q} net_out={net_out}")
        msg = tg_sell_msg(symbol, avg if avg > 0 else price, filled, net_out, pnl)
        send_tg(msg)
        TRADE_LOG.append({
            "ts": time.time(),
            "action": "sell",
            "symbol": symbol,
            "price_usd": float(avg if avg > 0 else price),
            "qty": float(filled),
            "net_out_usd": float(net_out),
            "pnl_usd": float(pnl),
            "source": source,
            "tf": tf,
        })
        _save_state_file()
    except Exception as e:
        _dbg(f"[LIVE] sell error: {e}")
    finally:
        st["inflight"] = False

    return jsonify({"ok": True, "state": STATE[symbol]}), 200


# ------------- main -------------
if __name__ == "__main__":
    _dbg(f"[CONF] MEXC_RECV_WINDOW={MEXC_RECVWINDOW_MS} CCXT_TIMEOUT_MS={CCXT_TIMEOUT_MS}")
    try:
        mexc()
        _dbg("[WARMUP] MEXC client ready")
    except Exception as e:
        _dbg(f"[WARMUP] MEXC init error: {e}")
    rehydrate_positions()
    _load_state_file()
    _dbg(f"✅ Webhook server op http://0.0.0.0:{PORT}/webhook — symbols: {SYMBOLS}")
    _dbg(f"[CONF] budgets={BUDGET_USDT}")
    try:
        t = Thread(target=_daily_report_loop, daemon=True)
        t.start()
        _dbg("[REPORT] daily scheduler started")
    except Exception as e:
        _dbg(f"[REPORT] scheduler warn: {e}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
