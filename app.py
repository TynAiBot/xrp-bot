# app.py — XRP single-bot (1m) met Advisor + trendfilter + cooldown/de-dup
# + Persistente trade-logging (JSON-bestand) + /report/daily en /report/weekly

import os, time, json
from datetime import datetime, timedelta
from threading import Thread

import requests
import ccxt
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from typing import Tuple

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

# Advisor standaard AAN
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
# Advisor runtime config (aanpasbaar via endpoints)
ADVISOR_FAIL_MODE = os.getenv("ADVISOR_FAIL_MODE", "open")  # 'open' = toestaan bij storing, 'closed' = blokkeren
ADVISOR_ALLOW_SOURCES = set()   # bv. {"bollinger_band","ichimoku"} -> alleen deze bronnen toegestaan
ADVISOR_BLOCK_SOURCES = set()   # bv. {"rsi"} -> deze bronnen blokkeren
ADVISOR_MANUAL_OVERRIDE = None  # dict {mode:"allow"|"block", until:epoch, reason:str}

# Persistente logging
TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
trade_log = []  # lijst van dicts met buy/sell

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

from typing import Tuple  # staat al bij jou

def advisor_allows(action: str, price: float, source: str, tf: str) -> Tuple[bool, str]:
    """
    Toestemming via Multi-Bot Advisor met lokale overrides/fail-modes.
    Volgorde:
      1) Handmatige override (tijdelijk allow/block)
      2) Source allow/block lijsten
      3) Externe Advisor-call (indien enabled)
      4) Fail-mode (open/closed) bij storing
    """
    # 1) Handmatige override
    if ADVISOR_MANUAL_OVERRIDE:
        try:
            if time.time() <= ADVISOR_MANUAL_OVERRIDE.get("until", 0):
                mode = ADVISOR_MANUAL_OVERRIDE.get("mode", "allow")
                reason = f"manual_{mode}:" + ADVISOR_MANUAL_OVERRIDE.get("reason", "")
                return (mode == "allow"), reason
        except Exception:
            pass

    # 2) Source allow/block
    src = (source or "").lower()
    if ADVISOR_ALLOW_SOURCES and src not in ADVISOR_ALLOW_SOURCES:
        return False, "src_not_allowed"
    if ADVISOR_BLOCK_SOURCES and src in ADVISOR_BLOCK_SOURCES:
        return False, "src_blocked"

    # 3) Externe Advisor
    if ADVISOR_ENABLED and ADVISOR_URL:
        payload = {"symbol": SYMBOL_STR, "action": action, "price": price, "source": source, "timeframe": tf}
        headers = {"Content-Type": "application/json"}
        if ADVISOR_SECRET:
            headers["Authorization"] = f"Bearer {ADVISOR_SECRET}"
        try:
            r = requests.post(ADVISOR_URL, json=payload, headers=headers, timeout=2.5)
            if r.status_code >= 400:
                print(f"[ADVISOR] HTTP {r.status_code} -> {r.text[:300]}")
            try:
                j = r.json()
            except Exception:
                j = {}
            allow  = bool(j.get("approve", j.get("allow", True)))
            reason = str(j.get("reason", f"status_{r.status_code}"))
            return allow, reason
        except Exception as e:
            print(f"[ADVISOR] unreachable: {e}")

    # 4) Fail-mode
    if ADVISOR_FAIL_MODE == "closed":
        return False, "fail_closed"
    return True, "fail_open"

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
    # optioneel: max 2000 trades bewaren in geheugen
    if len(trade_log) > 2000:
        trade_log[:] = trade_log[-2000:]
    save_trades()

def trend_ok(price: float) -> Tuple[bool, float]:
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
    
def advisor_fetch_effective(symbol: str):
    """Haal effectieve advisor-instellingen op (probeer POST _action=get, POST plain, dan GET)."""
    if not (ADVISOR_ENABLED and ADVISOR_URL):
        return None

    # Bouw headers – sommige Advisors accepteren 'Authorization', anderen 'X-Advisor-Secret'
    headers = {"Content-Type": "application/json"}
    if ADVISOR_SECRET:
        headers["Authorization"] = f"Bearer {ADVISOR_SECRET}"
        headers["X-Advisor-Secret"] = ADVISOR_SECRET  # extra compat

    def try_json(resp):
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "status": resp.status_code, "error": resp.text[:200]}

    try:
        # 1) POST met _action=get
        try:
            r = requests.post(
                ADVISOR_URL,
                json={"symbol": symbol, "_action": "get"},
                headers=headers,
                timeout=2.5,
            )
            if r.ok:
                return try_json(r)
            # Laat debug-info zien als unauthorized of andere fout
            if r.status_code == 401:
                print(f"[ADVISOR] POST _action=get -> 401 Unauthorized")
            else:
                print(f"[ADVISOR] POST _action=get -> {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[ADVISOR] fetch POST _action=get fail: {e}")

        # 2) POST zonder _action
        try:
            r2 = requests.post(
                ADVISOR_URL,
                json={"symbol": symbol},
                headers=headers,
                timeout=2.5,
            )
            if r2.ok:
                return try_json(r2)
            if r2.status_code == 401:
                print(f"[ADVISOR] POST symbol -> 401 Unauthorized")
            else:
                print(f"[ADVISOR] POST symbol -> {r2.status_code}: {r2.text[:200]}")
        except Exception as e:
            print(f"[ADVISOR] fetch POST symbol fail: {e}")

        # 3) GET ?symbol=...
        try:
            r3 = requests.get(
                ADVISOR_URL,
                params={"symbol": symbol},
                headers=headers,
                timeout=2.5,
            )
            if r3.ok:
                return try_json(r3)
            if r3.status_code == 401:
                print(f"[ADVISOR] GET symbol -> 401 Unauthorized")
            else:
                print(f"[ADVISOR] GET symbol -> {r3.status_code}: {r3.text[:200]}")
        except Exception as e:
            print(f"[ADVISOR] fetch GET fail: {e}")

        # Geen succes
        return {"ok": False, "error": "no_successful_variant"}
    except Exception as e:
        print(f"[ADVISOR] fetch_effective unexpected fail: {e}")
        return {"ok": False, "error": str(e)[:200]}

# --- Flask ---
app = Flask(__name__)

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
            "🟢 <b>[XRP/USDT] AANKOOP</b>\n"
            f"📹 Koopprijs: ${price:.4f}\n"
            f"🧠 Signaalbron: {source} | {advisor_reason}\n"
            f"🕒 TF: {tf}\n"
            f"💰 Handelssaldo: €{capital:.2f}\n"
            f"💼 Spaarrekening: €{sparen:.2f}\n"
            f"📈 Totale waarde: €{capital + sparen:.2f}\n"
            f"🔐 Tradebedrag: €{START_CAPITAL:.2f}\n"
            f"🔗 Tijd: {timestamp}"
        )

        # log (winst = 0 bij buy)
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
            "📄 <b>[XRP/USDT] VERKOOP</b>\n"
            f"📹 Verkoopprijs: ${price:.4f}\n"
            f"🧠 Signaalbron: {source} | {advisor_reason}\n"
            f"🕒 TF: {tf}\n"
            f"💰 Handelssaldo: €{capital:.2f}\n"
            f"💼 Spaarrekening: €{sparen:.2f}\n"
            f"📈 {resultaat}: €{winst_bedrag:.2f}\n"
            f"📈 Totale waarde: €{capital + sparen:.2f}\n"
            f"🔐 Tradebedrag: €{START_CAPITAL:.2f}\n"
            f"🔗 Tijd: {timestamp}"
        )

        # log (winst/verlies vastleggen)
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

@app.route("/advisor/status", methods=["GET"])
def advisor_status():
    now = time.time()
    override = None
    if ADVISOR_MANUAL_OVERRIDE and now <= ADVISOR_MANUAL_OVERRIDE.get("until", 0):
        override = {
            "mode": ADVISOR_MANUAL_OVERRIDE.get("mode"),
            "until": ADVISOR_MANUAL_OVERRIDE.get("until"),
            "seconds_left": round(ADVISOR_MANUAL_OVERRIDE.get("until") - now, 1),
            "reason": ADVISOR_MANUAL_OVERRIDE.get("reason", "")
        }
    return jsonify({
        "enabled": ADVISOR_ENABLED,
        "url_set": bool(ADVISOR_URL),
        "secret_set": bool(ADVISOR_SECRET),
        "fail_mode": ADVISOR_FAIL_MODE,  # 'open' of 'closed'
        "allow_sources": sorted(list(ADVISOR_ALLOW_SOURCES)) if ADVISOR_ALLOW_SOURCES else [],
        "block_sources": sorted(list(ADVISOR_BLOCK_SOURCES)) if ADVISOR_BLOCK_SOURCES else [],
        "manual_override": override
    })

# --- Advisor proxy helpers (tweak/get) ---
def _advisor_headers():
    h = {"Content-Type": "application/json"}
    if ADVISOR_SECRET:
        h["Authorization"] = f"Bearer {ADVISOR_SECRET}"
        h["X-Advisor-Secret"] = ADVISOR_SECRET  # extra compat
    return h

def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "text": resp.text[:400]}

@app.route("/advisor/get", methods=["GET", "POST"])
def advisor_get():
    if not (ADVISOR_ENABLED and ADVISOR_URL):
        return jsonify({"ok": False, "error": "advisor_not_configured"}), 400
    symbol = (request.get_json(silent=True) or {}).get("symbol", SYMBOL_STR)

    attempts = []
    # 1) POST _action=get
    try:
        r = requests.post(ADVISOR_URL, json={"_action": "get", "symbol": symbol},
                          headers=_advisor_headers(), timeout=3)
        j = _safe_json(r)
        attempts.append({"variant": "post_action_get", "status": r.status_code, "resp": j})
        if r.ok and isinstance(j, dict) and ("applied" in j or j.get("ok") is True):
            return jsonify({"ok": True, "via": "post_action_get", "resp": j})
    except Exception as e:
        attempts.append({"variant": "post_action_get", "error": str(e)})

    # 2) GET ?symbol=
    try:
        r = requests.get(ADVISOR_URL, params={"symbol": symbol},
                         headers=_advisor_headers(), timeout=3)
        j = _safe_json(r)
        attempts.append({"variant": "get_query", "status": r.status_code, "resp": j})
        if r.ok and isinstance(j, dict) and ("applied" in j or j.get("ok") is True):
            return jsonify({"ok": True, "via": "get_query", "resp": j})
    except Exception as e:
        attempts.append({"variant": "get_query", "error": str(e)})

    return jsonify({"ok": False, "attempts": attempts}), 502

@app.route("/advisor/tweak", methods=["POST"])
def advisor_tweak():
    if not (ADVISOR_ENABLED and ADVISOR_URL):
        return jsonify({"ok": False, "error": "advisor_not_configured"}), 400

    body = request.get_json(force=True) or {}
    symbol = body.get("symbol", SYMBOL_STR)
    # zowel top-level als {"values": {...}} accepteren
    values = body.get("values") if isinstance(body.get("values"), dict) else body

    variants = [
        {"_variant": "post_action_set_values", "_action": "set", "symbol": symbol, "values": values},
        {"_variant": "post_action_set_top",    "_action": "set", "symbol": symbol, **values},
        {"_variant": "post_plain_top",         "symbol": symbol, **values},
    ]

    attempts = []
    for v in variants:
        data = dict(v)  # copy
        tag = data.pop("_variant", "unknown")
        try:
            r = requests.post(ADVISOR_URL, json=data, headers=_advisor_headers(), timeout=3)
            j = _safe_json(r)
            attempts.append({"variant": tag, "status": r.status_code, "resp": j})
            # Succescriterium: server geeft 'applied' terug of ok==true (meer dan echo)
            if r.ok and isinstance(j, dict) and ("applied" in j or j.get("ok") is True):
                return jsonify({"ok": True, "used": tag, "resp": j})
        except Exception as e:
            attempts.append({"variant": tag, "error": str(e)})

    return jsonify({"ok": False, "attempts": attempts}), 502

@app.route("/advisor/set", methods=["POST"])
def advisor_set():
    global ADVISOR_FAIL_MODE, ADVISOR_MANUAL_OVERRIDE
    global ADVISOR_ALLOW_SOURCES, ADVISOR_BLOCK_SOURCES
    global ADVISOR_ENABLED, ADVISOR_URL, ADVISOR_SECRET

    data = request.get_json(force=True, silent=True) or {}

    # Basis toggle/config
    if "enabled" in data:
        ADVISOR_ENABLED = bool(data["enabled"])
    if "url" in data:
        ADVISOR_URL = str(data["url"]).strip()
    if "secret" in data:
        ADVISOR_SECRET = str(data["secret"]).strip()
    if data.get("fail_mode") in ("open", "closed"):
        ADVISOR_FAIL_MODE = data["fail_mode"]

    # Allow/Block lijsten
    if "allow_sources" in data:
        ADVISOR_ALLOW_SOURCES = set(str(s).lower() for s in (data.get("allow_sources") or []))
    if "block_sources" in data:
        ADVISOR_BLOCK_SOURCES = set(str(s).lower() for s in (data.get("block_sources") or []))

    # Tijdelijke manual override: {"override":{"mode":"allow"|"block","seconds":600,"reason":"..."}}
    if isinstance(data.get("override"), dict):
        ov = data["override"]
        mode = ov.get("mode", "allow")
        if mode in ("allow", "block"):
            secs = int(ov.get("seconds", 0))
            if secs > 0:
                ADVISOR_MANUAL_OVERRIDE = {
                    "mode": mode,
                    "until": time.time() + secs,
                    "reason": str(ov.get("reason", ""))
                }
            else:
                ADVISOR_MANUAL_OVERRIDE = None

    return advisor_status()

def idle_worker():
    # plek voor periodieke taken (bijv. future trailing/pings)
    while True:
        time.sleep(60)

@app.route("/config", methods=["GET"])
def config_view():
    def masked(s): 
        return bool(s)  # geen secrets tonen; alleen aangeven of iets gezet is

    advisor_effective = advisor_fetch_effective(SYMBOL_STR) if (ADVISOR_ENABLED and ADVISOR_URL) else None

    return jsonify({
        "symbol": SYMBOL_STR,
        "timeframe_default": "1m",
        "live_mode": LIVE_MODE,
        "start_capital": START_CAPITAL,
        "sparen_start": SPAREN_START,
        "savings_split": SAVINGS_SPLIT,          # 1.0 = 100% winst naar spaar
        "cooldown_s": MIN_TRADE_COOLDOWN_S,
        "dedup_window_s": DEDUP_WINDOW_S,
        "use_trend_filter": USE_TREND_FILTER,
        "advisor_enabled": ADVISOR_ENABLED,
        "advisor_url_set": bool(ADVISOR_URL),
        "advisor_secret_set": masked(ADVISOR_SECRET),
        "webhook_secret_set": masked(WEBHOOK_SECRET),
        "telegram_config_ok": bool(TG_TOKEN and TG_CHAT_ID),
        "trades_file": TRADES_FILE,
        # runtime state
        "in_position": in_position,
        "entry_price": round(entry_price, 4),
        "capital": round(capital, 2),
        "sparen": round(sparen, 2),
        "totaalwaarde": round(capital + sparen, 2),
        "last_action": datetime.fromtimestamp(last_action_ts).strftime("%d-%m-%Y %H:%M:%S") if last_action_ts > 0 else None,
        # nieuw: toon wat de Advisor nu hanteert (zoals je in Postman zag: {"applied": {...}})
        "advisor_effective": advisor_effective
    })


if __name__ == "__main__":
    load_trades()  # probeer bestaande log in te lezen bij start
    port = int(os.environ.get("PORT", "5000"))
    print(f"✅ Webhook server op http://0.0.0.0:{port}/webhook")
    Thread(target=idle_worker, daemon=True).start()
    send_tg("✅ XRP-bot gestart op Render")
    app.run(host="0.0.0.0", port=port)
