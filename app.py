# app.py — XRP single-bot (1m) met Advisor (embedded) + trendfilter + cooldown/de-dup
# + Persistente trade-logging (JSON-bestand) + /report/daily en /report/weekly + /advisor admin

import os, time, json
from datetime import datetime, timedelta
from threading import Thread

import requests
import ccxt
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# --- Basis ---
SYMBOL_TV  = "XRP/USDT"        # ccxt notatie
SYMBOL_STR = "XRPUSDT"         # voor advisor/logs
START_CAPITAL = float(os.getenv("START_CAPITAL", "500"))
SPAREN_START  = float(os.getenv("SPAREN_START",  "500"))
LIVE_MODE     = os.getenv("LIVE_MODE", "1") == "1"

# 100% van winst naar spaar (instelbaar)
SAVINGS_SPLIT = float(os.getenv("SAVINGS_SPLIT", "1.0"))

# 1m-tuning
MIN_TRADE_COOLDOWN_S = int(os.getenv("MIN_TRADE_COOLDOWN_S", "90"))  # 90s voor 1m
DEDUP_WINDOW_S       = int(os.getenv("DEDUP_WINDOW_S",       "30"))  # dezelfde tick/prijs wegfilteren

# Trendfilter
USE_TREND_FILTER = os.getenv("USE_TREND_FILTER", "1") == "1"         # MA200 only-long op BUY

# Advisor AAN
ADVISOR_ENABLED = os.getenv("ADVISOR_ENABLED", "1") == "1"
ADVISOR_URL     = os.getenv("ADVISOR_URL", "")
ADVISOR_SECRET  = os.getenv("ADVISOR_SECRET", "")

# Webhook beveiliging (optioneel)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

if not TG_TOKEN or not TG_CHAT_ID:
    raise SystemExit("[FOUT] TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt.")

exchange = ccxt.binance({"enableRateLimit": True}) if USE_TREND_FILTER else None

# --- State ---
in_position = False
entry_price = 0.0
capital = START_CAPITAL
sparen  = SPAREN_START
last_action_ts = 0.0
last_signal_key_ts = {}     # (action, source, round(price,4), tf) -> ts

# Persistente logging
TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
trade_log = []  # lijst van dicts met buy/sell

# =========================
# Embedded Advisor (zelfde service, geen extra Render nodig)
# =========================
ADVISOR_STORE   = os.getenv("ADVISOR_STORE", "advisor_store.json")
ADVISOR_DEFAULTS = {
    "BUY_THRESHOLD":   0.41,
    "SELL_THRESHOLD":  0.56,
    "TAKE_PROFIT_PCT": 0.026,
    "STOP_LOSS_PCT":   0.020,
    "USE_TRAILING":    True,
    "TRAIL_PCT":       0.010,
    "DIRECT_TPSL":     True,
    "ARM_AT_PCT":      0.005,
    "MIN_MOVE_PCT":    0.001,
}
# persistent state: { "symbols": { "XRPUSDT": {"applied": {...}, "ts": epoch} }, "updated": ts }
ADVISOR_STATE = {"symbols": {}, "updated": 0}

def _advisor_load():
    global ADVISOR_STATE
    try:
        if os.path.exists(ADVISOR_STORE):
            with open(ADVISOR_STORE, "r", encoding="utf-8") as f:
                j = json.load(f)
            if isinstance(j, dict):
                ADVISOR_STATE = j
            else:
                ADVISOR_STATE = {"symbols": {}, "updated": 0}
        else:
            ADVISOR_STATE = {"symbols": {}, "updated": 0}
    except Exception:
        ADVISOR_STATE = {"symbols": {}, "updated": 0}

def _advisor_save():
    try:
        tmp = ADVISOR_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ADVISOR_STATE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ADVISOR_STORE)
    except Exception as e:
        print("[ADVISOR_STORE] save error:", e)

def _advisor_applied(symbol: str) -> dict:
    s = (symbol or "").upper().strip() or SYMBOL_STR
    sm = ADVISOR_STATE.setdefault("symbols", {})
    rec = sm.get(s)
    if not isinstance(rec, dict) or not isinstance(rec.get("applied"), dict):
        sm[s] = {"applied": dict(ADVISOR_DEFAULTS), "ts": int(time.time())}
        ADVISOR_STATE["updated"] = sm[s]["ts"]
        _advisor_save()
    return sm[s]["applied"]

def _advisor_auth_ok(req) -> bool:
    if not ADVISOR_SECRET:
        return True
    h = req.headers.get("Authorization", "").strip()
    if h.lower().startswith("bearer ") and h.split(" ", 1)[1] == ADVISOR_SECRET:
        return True
    if req.headers.get("X-Advisor-Secret", "") == ADVISOR_SECRET:
        return True
    return False

def _advisor_coerce(vals: dict) -> dict:
    out = {}
    for k, v in (vals or {}).items():
        K = str(k).upper()
        if K in {"USE_TRAILING", "DIRECT_TPSL"}:
            out[K] = bool(v) if isinstance(v, bool) else str(v).strip().lower() in {"1","true","yes","y","on"}
        elif K in ADVISOR_DEFAULTS:
            try:
                out[K] = round(float(v), 3)
            except Exception:
                pass
    return out

# Als ADVISOR_URL leeg is, wijs naar onze eigen /advisor (localhost) zodat alles 1 service blijft.
if not ADVISOR_URL:
    ADVISOR_URL = f"http://127.0.0.1:{os.getenv('PORT','5000')}/advisor"

# --- Helpers ---
def now_str():
    return datetime.now().strftime("%d-%m-%Y %H:%M:%S")

def send_tg(text_html: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text_html, "parse_mode": "HTML"},
            timeout=6,
        )
        if r.status_code != 200:
            print(f"[TG] {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")

def advisor_allows(action: str, price: float, source: str, tf: str) -> (bool, str):
    """Vraag Advisor (lokale /advisor of externe). Fallback = toestaan."""
    if not ADVISOR_ENABLED or not ADVISOR_URL:
        return True, "advisor_off"
    payload = {"symbol": SYMBOL_STR, "action": action, "price": price, "source": source, "timeframe": tf}
    headers = {"Content-Type": "application/json"}
    if ADVISOR_SECRET:
        headers["Authorization"] = f"Bearer {ADVISOR_SECRET}"
    try:
        r = requests.post(ADVISOR_URL, json=payload, headers=headers, timeout=2.5)
        try:
            j = r.json()
        except Exception:
            j = {}
        allow  = bool(j.get("approve", j.get("allow", True)))
        reason = str(j.get("reason", f"status_{r.status_code}"))
        return allow, reason
    except Exception as e:
        print(f"[ADVISOR] unreachable: {e}")
        return True, "advisor_unreachable"

def trend_ok(price: float) -> (bool, float):
    """MA200: koop alleen boven MA200 (1m). Bij fout niet blokkeren."""
    if not USE_TREND_FILTER or exchange is None:
        return True, float("nan")
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL_TV, timeframe="1m", limit=210)
        closes = [c[4] for c in ohlcv]
        ma200 = sum(closes[-200:]) / 200.0
        return price > ma200, ma200
    except Exception as e:
        print(f"[TREND] fetch fail: {e}")
        return True, float("nan")

def blocked_by_cooldown() -> bool:
    return (time.time() - last_action_ts) < MIN_TRADE_COOLDOWN_S if MIN_TRADE_COOLDOWN_S > 0 else False

def is_duplicate_signal(action: str, source: str, price: float, tf: str) -> bool:
    key = (action, source, round(price, 4), tf)
    ts = last_signal_key_ts.get(key, 0.0)
    if (time.time() - ts) <= DEDUP_WINDOW_S:
        return True
    last_signal_key_ts[key] = time.time()
    return False

# --- Persistente log helpers ---
def load_trades():
    global trade_log
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r", encoding="utf-8") as f:
                trade_log = json.load(f)
                if not isinstance(trade_log, list):
                    trade_log = []
            print(f"[LOG] geladen: {len(trade_log)} trades uit {TRADES_FILE}")
        else:
            trade_log = []
    except Exception as e:
        print(f"[LOG] load error: {e}")
        trade_log = []


def save_trades():
    try:
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trade_log, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[LOG] save error: {e}")


def log_trade(action: str, price: float, winst: float, source: str, tf: str):
    trade_log.append({
        "timestamp": now_str(),
        "action": action,
        "price": round(price, 4),
        "winst": round(winst, 2),
        "source": source,
        "tf": tf,
        "capital": round(capital, 2),
        "sparen": round(sparen, 2)
    })
    if len(trade_log) > 2000:
        trade_log[:] = trade_log[-2000:]
    save_trades()

# --- Flask ---
app = Flask(__name__)

# ===== Embedded Advisor endpoints =====
@app.route("/advisor", methods=["GET","POST"])
def advisor_endpoint():
    """
    GET  /advisor?symbol=XRPUSDT   -> applied (geen auth)
    POST {"_action":"get","symbol":"XRPUSDT"} -> applied (geen auth)
    POST {"action":"buy|sell",...} -> approve open + applied echo (zoals bot gebruikt)
    """
    try:
        if request.method == "GET":
            sym = request.args.get("symbol", SYMBOL_STR)
            return jsonify({"ok": True, "applied": _advisor_applied(sym)})

        data = request.get_json(force=True, silent=True) or {}
        if str(data.get("_action","")).lower() == "get":
            sym = data.get("symbol", SYMBOL_STR)
            return jsonify({"ok": True, "applied": _advisor_applied(sym)})

        # approval pad (we blokkeren hier niets)
        sym = data.get("symbol", SYMBOL_STR)
        act = str(data.get("action","")).lower()
        if act not in {"buy","sell"}:
            return jsonify({"ok": False, "error": "bad_payload"}), 400
        return jsonify({"ok": True, "approve": True, "reason": "open", "applied": _advisor_applied(sym)})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/advisor/admin/get", methods=["POST"])
def advisor_admin_get():
    if not _advisor_auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    sym = data.get("symbol", SYMBOL_STR)
    return jsonify({"ok": True, "applied": _advisor_applied(sym)})


@app.route("/advisor/admin/set", methods=["POST"])
def advisor_admin_set():
    if not _advisor_auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    sym = (data.get("symbol") or SYMBOL_STR).upper().strip()
    raw = data.get("values") or data.get("changes") or {}
    vals = _advisor_coerce(raw)
    ap = _advisor_applied(sym)
    ap.update(vals)
    ADVISOR_STATE["symbols"][sym] = {"applied": ap, "ts": int(time.time())}
    ADVISOR_STATE["updated"] = ADVISOR_STATE["symbols"][sym]["ts"]
    _advisor_save()
    return jsonify({"ok": True, "applied": ap})


@app.route("/advisor/tweak", methods=["POST"])  # alias
def advisor_admin_tweak():
    return advisor_admin_set()


@app.route("/health")
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, entry_price, capital, sparen, last_action_ts

    # Secret check
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return "Unauthorized", 401

    data = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).lower().strip()
    source = str(data.get("source", "onbekend")).lower().strip()
    tf     = str(data.get("tf", "1m")).lower().strip()  # optioneel, default 1m

    try:
        price = float(data.get("price", 0))
    except Exception:
        return "Bad price", 400

    if price <= 0 or action not in ("buy", "sell"):
        return "Bad payload", 400

    # De-dup & cooldown
    if is_duplicate_signal(action, source, price, tf):
        print("[DEDUP] ignored", action, source, price, tf)
        return "OK", 200
    if blocked_by_cooldown():
        print("[COOLDOWN] ignored")
        return "OK", 200

    # Advisor check
    allowed, advisor_reason = advisor_allows(action, price, source, tf)
    if not allowed:
        print(f"[ADVISOR] blocked: {advisor_reason}")
        return "OK", 200

    timestamp = now_str()

    # === BUY ===
    if action == "buy" and not in_position:
        ok, ma200 = trend_ok(price)
        if not ok:
            print(f"[TREND] Blocked BUY: price {price:.4f} < MA200 {ma200:.4f}")
            return "OK", 200

        entry_price = price
        in_position = True
        last_action_ts = time.time()

        send_tg(
    f"""🟢 <b>[XRP/USDT] AANKOOP</b>
📹 Koopprijs: ${price:.4f}
🧠 Signaalbron: {source} | {advisor_reason}
🕒 TF: {tf}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{START_CAPITAL:.2f}
🔗 Tijd: {timestamp}"""
)

        log_trade("buy", price, 0.0, source, tf)
        return "OK", 200

    # === SELL ===
    if action == "sell" and in_position:
        if entry_price <= 0:
            return "No valid entry", 400

        verkoop_bedrag = price * START_CAPITAL / entry_price
        winst_bedrag = round(verkoop_bedrag - START_CAPITAL, 2)

        if winst_bedrag > 0:
            sparen  += SAVINGS_SPLIT * winst_bedrag
            capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
        else:
            capital += winst_bedrag  # verlies ten laste van trading-kapitaal

        # top-up terug naar START_CAPITAL
        if capital < START_CAPITAL:
            tekort = START_CAPITAL - capital
            if sparen >= tekort:
                sparen -= tekort
                capital += tekort

        in_position = False
        last_action_ts = time.time()

        resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
        send_tg(
    f"""📄 <b>[XRP/USDT] VERKOOP</b>
📹 Verkoopprijs: ${price:.4f}
🧠 Signaalbron: {source} | {advisor_reason}
🕒 TF: {tf}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 {"Winst" if winst_bedrag >= 0 else "Verlies"}: €{winst_bedrag:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{START_CAPITAL:.2f}
🔗 Tijd: {timestamp}"""
)

        log_trade("sell", price, winst_bedrag, source, tf)
        entry_price = 0.0
        return "OK", 200

    return "OK", 200

# --- Rapportage ---
@app.route("/report/daily", methods=["GET"])
def report_daily():
    today = datetime.now().date()
    trades_today = [t for t in trade_log if datetime.strptime(t["timestamp"], "%d-%m-%Y %H:%M:%S").date() == today]
    total_pnl = sum(t.get("winst", 0.0) for t in trades_today)
    return jsonify({
        "symbol": SYMBOL_STR,
        "capital": round(capital, 2),
        "sparen": round(sparen, 2),
        "totaalwaarde": round(capital + sparen, 2),
        "in_position": in_position,
        "entry_price": round(entry_price, 4),
        "laatste_actie": datetime.fromtimestamp(last_action_ts).strftime("%d-%m-%Y %H:%M:%S") if last_action_ts > 0 else None,
        "trades_vandaag": trades_today,
        "pnl_vandaag": round(total_pnl, 2)
    })

@app.route("/report/weekly", methods=["GET"])
def report_weekly():
    now = datetime.now()
    week_start = now - timedelta(days=7)
    trades_week = [
        t for t in trade_log
        if datetime.strptime(t["timestamp"], "%d-%m-%Y %H:%M:%S") >= week_start
    ]
    total_pnl = sum(t.get("winst", 0.0) for t in trades_week)
    return jsonify({
        "symbol": SYMBOL_STR,
        "capital": round(capital, 2),
        "sparen": round(sparen, 2),
        "totaalwaarde": round(capital + sparen, 2),
        "trades_week": trades_week,
        "pnl_week": round(total_pnl, 2)
    })

# Optioneel: handmatig opslaan/forceren
@app.route("/report/save", methods=["POST"]) 
def report_save():
    save_trades()
    return jsonify({"saved": True, "file": TRADES_FILE, "count": len(trade_log)})

# Config + Advisor view
@app.route("/config", methods=["GET"])
def config_view():
    def masked(s): return bool(s)
    return jsonify({
        "symbol": SYMBOL_STR,
        "timeframe_default": "1m",
        "live_mode": LIVE_MODE,
        "start_capital": START_CAPITAL,
        "sparen_start": SPAREN_START,
        "savings_split": SAVINGS_SPLIT,
        "cooldown_s": MIN_TRADE_COOLDOWN_S,
        "dedup_window_s": DEDUP_WINDOW_S,
        "use_trend_filter": USE_TREND_FILTER,
        "advisor_enabled": ADVISOR_ENABLED,
        "advisor_url": ADVISOR_URL,
        "advisor_secret_set": masked(ADVISOR_SECRET),
        "telegram_config_ok": bool(TG_TOKEN and TG_CHAT_ID),
        "trades_file": TRADES_FILE,
        # runtime
        "in_position": in_position,
        "entry_price": round(entry_price, 4),
        "capital": round(capital, 2),
        "sparen": round(sparen, 2),
        "totaalwaarde": round(capital + sparen, 2),
        "last_action": datetime.fromtimestamp(last_action_ts).strftime("%d-%m-%Y %H:%M:%S") if last_action_ts > 0 else None,
        # advisor (lokaal applied)
        "advisor_applied": _advisor_applied(SYMBOL_STR),
    })


def idle_worker():
    # plek voor periodieke taken (bijv. future trailing/pings)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    _advisor_load()  # laad persistente advisor-config
    load_trades()    # probeer bestaande log in te lezen bij start
    port = int(os.environ.get("PORT", "5000"))
    print(f"✅ Webhook server op http://0.0.0.0:{port}/webhook")
    Thread(target=idle_worker, daemon=True).start()
    send_tg("✅ XRP-bot gestart op Render")
    app.run(host="0.0.0.0", port=port)
