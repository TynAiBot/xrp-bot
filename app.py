# app.py — XRP single-bot (1m) met:
# - TradingView webhooks (buy/sell)
# - Live SPOT trading via MEXC (ccxt)
# - Embedded Advisor (for TPSL params) + optioneel Advisor-allow check
# - Trendfilter (optioneel)
# - Cooldown + De-dup
# - Persistente trade-logging (/report/daily, /report/weekly)
# - Lokale TPSL monitor (TP/SL/Trailing) met ARMING-DELAYS (OPTIE 3)
# - Hard stop-loss + arm-delay
# - Rehydrate live positie op boot (+ dust ignore) en ghost-guards
# - /config met uitgebreide runtime status

import os, time, json
from datetime import datetime, timedelta
from threading import Thread
from typing import Tuple

import requests
import ccxt
from flask import Flask, request, jsonify
from dotenv import load_dotenv


# -----------------------
# Env parsing helpers
# -----------------------
def env_bool(name: str, default=False) -> bool:
    val = os.getenv(name, None)
    if val is None:
        return bool(default)
    s = str(val).strip().lower()
    return s in ("1", "true", "t", "yes", "y", "on")


def env_int(name: str, default=0) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        try:
            return int(float(os.getenv(name, default)))
        except Exception:
            return int(default)


def env_float(name: str, default=0.0) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


load_dotenv()

# -----------------------
# Basis & symbol
# -----------------------
SYMBOL_TV = "XRP/USDT"  # ccxt notatie
SYMBOL_STR = "XRPUSDT"  # string voor advisor/logs

START_CAPITAL = float(os.getenv("START_CAPITAL", "500"))
SPAREN_START = float(os.getenv("SPAREN_START", "500"))

LIVE_MODE = env_bool("LIVE_MODE", True)
LIVE_EXCHANGE = os.getenv("LIVE_EXCHANGE", "mexc").lower()
REHYDRATE_ON_BOOT = env_bool("REHYDRATE_ON_BOOT", True)
REHYDRATE_MIN_XRP = env_float(
    "REHYDRATE_MIN_XRP", 5.0
)  # balance < dit ⇒ negeren (dust)

# Safety / guardrails
HARD_SL_PCT = env_float("HARD_SL_PCT", 0.02)  # 2% hard stop
HARD_SL_ARM_SEC = env_int("HARD_SL_ARM_SEC", 15)  # pas na X sec na BUY actief
MAX_HOLD_MIN = env_int("MAX_HOLD_MIN", 45)  # force exit na X minuten
PRICE_POLL_S = env_int("PRICE_POLL_S", 2)  # prijs-check interval (s)

# Data-bron fallback volgorde
EXCHANGE_SOURCE = os.getenv(
    "EXCHANGE_SOURCE", "auto"
).lower()  # auto/binance/bybit/okx/mexc

# Winstverdeling
SAVINGS_SPLIT = env_float("SAVINGS_SPLIT", 1.0)  # 1.0 = 100% naar sparen

# Cooldown / De-dup
MIN_TRADE_COOLDOWN_S = env_int("MIN_TRADE_COOLDOWN_S", 60)
DEDUP_WINDOW_S = env_int("DEDUP_WINDOW_S", 15)

# Trendfilter (optioneel)
USE_TREND_FILTER = env_bool("USE_TREND_FILTER", False)
TREND_MODE = os.getenv("TREND_MODE", "off").lower()  # off|soft|hard
TREND_MA_LEN = env_int("TREND_MA_LEN", 50)
TREND_MA_TYPE = os.getenv("TREND_MA_TYPE", "SMA").upper()  # SMA|EMA
TREND_TF = os.getenv("TREND_TF", "1m")
TREND_SLOPE_LOOKBACK = env_int("TREND_SLOPE_LOOKBACK", 0)  # 0=uit
TREND_SOFT_TOL = env_float("TREND_SOFT_TOL", 0.001)  # 0.1%

# Advisor (embedded) + allow check
ADVISOR_ENABLED = env_bool("ADVISOR_ENABLED", False)  # allow/block check
ADVISOR_URL = os.getenv("ADVISOR_URL", "")
ADVISOR_SECRET = os.getenv("ADVISOR_SECRET", "")

# Lokale TPSL monitor
LOCAL_TPSL_ENABLED = env_bool("LOCAL_TPSL_ENABLED", True)
SYNC_TPSL_FROM_ADVISOR = env_bool("SYNC_TPSL_FROM_ADVISOR", True)
SELL_RETRY_RATIO   = env_float("SELL_RETRY_RATIO", 0.995)  # 2e poging bij oversold: % van free
SELL_EPSILON_TICKS = env_int("SELL_EPSILON_TICKS", 1)      # # ticks onder hoeveelheid-precision

# Defaults (gebruikt als SYNC_TPSL_FROM_ADVISOR=False of advisor niet bereikbaar)
LOCAL_TP_PCT = env_float("LOCAL_TP_PCT", 0.010)
LOCAL_SL_PCT = env_float("LOCAL_SL_PCT", 0.009)
LOCAL_USE_TRAIL = env_bool("LOCAL_USE_TRAIL", True)
LOCAL_TRAIL_PCT = env_float("LOCAL_TRAIL_PCT", 0.007)
LOCAL_ARM_AT_PCT = env_float("LOCAL_ARM_AT_PCT", 0.002)

# Optie 3: arming-delays voor lokale TPSL (seconden)
LOCAL_SL_ARM_SEC = env_int("LOCAL_SL_ARM_SEC", 20)
LOCAL_TP_ARM_SEC = env_int("LOCAL_TP_ARM_SEC", 0)

# Webhook beveiliging
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
if not TG_TOKEN or not TG_CHAT_ID:
    raise SystemExit("[FOUT] TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt.")

# Trend client (alleen als filter aan staat)
trend_exchange = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}}) if USE_TREND_FILTER else None

# Debug helper
DEBUG_SIG = env_bool("DEBUG_SIG", True)


def _dbg(msg: str):
    if DEBUG_SIG:
        print(f"[SIGDBG] {msg}", flush=True)


_dbg("booted")

# -----------------------
# Runtime state
# -----------------------
in_position = False
entry_price = 0.0
entry_ts = 0.0
last_action_ts = 0.0
capital = START_CAPITAL
sparen = SPAREN_START
pos_amount = 0.0
pos_quote = 0.0  # werkelijk besteed USDT (quote) bij live BUY

last_signal_key_ts = {}  # (action, source, round(price,4), tf) -> ts

TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
trade_log = []

# TPSL per-trade state (Optie 3: start_ts toegevoegd + key 'high')
tpsl_state = {
    "active": False,
    "tp_pct": None,
    "sl_pct": None,
    "use_trail": None,
    "trail_pct": None,
    "arm_at_pct": None,
    "armed": False,
    "high": 0.0,
    "start_ts": 0.0,
}

# -----------------------
# Embedded Advisor
# -----------------------
ADVISOR_STORE = os.getenv("ADVISOR_STORE", "advisor_store.json")
ADVISOR_DEFAULTS = {
    "BUY_THRESHOLD": 0.41,
    "SELL_THRESHOLD": 0.56,
    "TAKE_PROFIT_PCT": 0.026,
    "STOP_LOSS_PCT": 0.020,
    "USE_TRAILING": True,
    "TRAIL_PCT": 0.010,
    "DIRECT_TPSL": True,
    "ARM_AT_PCT": 0.005,
    "MIN_MOVE_PCT": 0.001,
}
ADVISOR_STATE = {"symbols": {}, "updated": 0}


def _advisor_load():
    global ADVISOR_STATE
    try:
        if os.path.exists(ADVISOR_STORE):
            with open(ADVISOR_STORE, "r", encoding="utf-8") as f:
                j = json.load(f)
            ADVISOR_STATE = j if isinstance(j, dict) else {"symbols": {}, "updated": 0}
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
        print("[ADVISOR_STORE] save error:", e, flush=True)


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
            out[K] = (
                bool(v)
                if isinstance(v, bool)
                else str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
            )
        elif K in ADVISOR_DEFAULTS:
            try:
                out[K] = round(float(v), 3)
            except Exception:
                pass
    return out


if not ADVISOR_URL:
    ADVISOR_URL = f"http://127.0.0.1:{os.getenv('PORT','5000')}/advisor"


# -----------------------
# Helpers
# -----------------------
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
            print(f"[TG] {r.status_code} {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[TG ERROR] {e}", flush=True)


def advisor_allows(action: str, price: float, source: str, tf: str) -> Tuple[bool, str]:
    if not ADVISOR_ENABLED or not ADVISOR_URL:
        return True, "advisor_off"
    payload = {
        "symbol": SYMBOL_STR,
        "action": action,
        "price": price,
        "source": source,
        "timeframe": tf,
    }
    headers = {"Content-Type": "application/json"}
    if ADVISOR_SECRET:
        headers["Authorization"] = f"Bearer {ADVISOR_SECRET}"
    try:
        r = requests.post(ADVISOR_URL, json=payload, headers=headers, timeout=2.5)
        try:
            j = r.json()
        except Exception:
            j = {}
        allow = bool(j.get("approve", j.get("allow", True)))
        reason = str(j.get("reason", f"status_{r.status_code}"))
        return allow, reason
    except Exception as e:
        print(f"[ADVISOR] unreachable: {e}", flush=True)
        return True, "advisor_unreachable"


# -----------------------
# TPSL helpers (Optie 3)
# -----------------------
def _tpsl_defaults_from_env():
    return {
        "tp_pct": LOCAL_TP_PCT,
        "sl_pct": LOCAL_SL_PCT,
        "use_trail": LOCAL_USE_TRAIL,
        "trail_pct": LOCAL_TRAIL_PCT,
        "arm_at_pct": LOCAL_ARM_AT_PCT,
    }


def _tpsl_from_advisor():
    ap = _advisor_applied(SYMBOL_STR)
    return {
        "tp_pct": float(ap.get("TAKE_PROFIT_PCT", LOCAL_TP_PCT)),
        "sl_pct": float(ap.get("STOP_LOSS_PCT", LOCAL_SL_PCT)),
        "use_trail": bool(ap.get("USE_TRAILING", LOCAL_USE_TRAIL)),
        "trail_pct": float(ap.get("TRAIL_PCT", LOCAL_TRAIL_PCT)),
        "arm_at_pct": float(ap.get("ARM_AT_PCT", LOCAL_ARM_AT_PCT)),
    }


def tpsl_reset():
    tpsl_state.update(
        {
            "active": False,
            "tp_pct": None,
            "sl_pct": None,
            "use_trail": None,
            "trail_pct": None,
            "arm_at_pct": None,
            "armed": False,
            "high": 0.0,
            "start_ts": 0.0,
        }
    )


def tpsl_on_buy(entry: float):
    if not LOCAL_TPSL_ENABLED:
        tpsl_reset()
        return
    try:
        params = (
            _tpsl_from_advisor()
            if SYNC_TPSL_FROM_ADVISOR
            else _tpsl_defaults_from_env()
        )
    except Exception:
        params = _tpsl_defaults_from_env()

    tpsl_state.update(params)
    tpsl_state["active"] = True
    tpsl_state["armed"] = False
    tpsl_state["high"] = entry
    tpsl_state["start_ts"] = time.time()
    _dbg(f"[TPSL] armed: {params}")


def local_tpsl_check(last_price: float):
    if not (
        LOCAL_TPSL_ENABLED and in_position and tpsl_state["active"] and entry_price > 0
    ):
        return

    now = time.time()
    elapsed = now - float(tpsl_state.get("start_ts") or now)

    e = entry_price
    tp = e * (1.0 + float(tpsl_state["tp_pct"] or 0.0))
    sl = e * (1.0 - float(tpsl_state["sl_pct"] or 0.0))

    allow_sl = (LOCAL_SL_ARM_SEC <= 0) or (elapsed >= LOCAL_SL_ARM_SEC)
    allow_tp = (LOCAL_TP_ARM_SEC <= 0) or (elapsed >= LOCAL_TP_ARM_SEC)

    # Take Profit
    if allow_tp and last_price >= tp > 0:
        _dbg(f"TPSL: TP hit @ {last_price:.6f} (tp={tp:.6f})")
        _do_forced_sell(last_price, "local_tp")
        tpsl_reset()
        return

    # Lokale Stop-Loss
    if allow_sl and last_price <= sl < e:
        _dbg(f"TPSL: SL hit @ {last_price:.6f} (sl={sl:.6f})")
        _do_forced_sell(last_price, "local_sl")
        tpsl_reset()
        return

    # Trailing
    if tpsl_state.get("use_trail"):
        arm_level = e * (1.0 + float(tpsl_state["arm_at_pct"] or 0.0))
        if not tpsl_state["armed"]:
            if last_price >= arm_level:
                tpsl_state["armed"] = True
                tpsl_state["high"] = last_price
                _dbg(
                    f"TPSL: trail ARMED at {last_price:.6f} (arm_level={arm_level:.6f})"
                )
        else:
            if last_price > tpsl_state["high"]:
                tpsl_state["high"] = last_price
            trail_stop = tpsl_state["high"] * (
                1.0 - float(tpsl_state["trail_pct"] or 0.0)
            )
            if last_price <= trail_stop:
                _dbg(
                    f"TPSL: TRAIL stop @ {last_price:.6f} (trail_stop={trail_stop:.6f}, high={tpsl_state['high']:.6f})"
                )
                _do_forced_sell(last_price, "local_trail")
                tpsl_reset()
                return


# -----------------------
# Trend utilities
# -----------------------
def _ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    ema = sum(values[:n]) / n
    for v in values[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def trend_ok(price: float) -> Tuple[bool, float]:
    try:
        if not USE_TREND_FILTER:
            return True, float("nan")

        n = max(5, int(TREND_MA_LEN))
        ohlcv, src = fetch_ohlcv_any(SYMBOL_TV, timeframe=TREND_TF, limit=n + 20)
        closes = [float(c[4]) for c in ohlcv]
        if len(closes) < n:
            return True, float("nan")

        ma = _ema(closes, n) if TREND_MA_TYPE == "EMA" else sum(closes[-n:]) / float(n)

        ok_gate = True
        mode = TREND_MODE
        if mode == "hard":
            ok_gate = price > ma
        elif mode == "soft":
            ok_gate = price >= ma * (1.0 - max(0.0, TREND_SOFT_TOL))
        else:
            ok_gate = True

        if ok_gate and TREND_SLOPE_LOOKBACK > 0 and len(closes) > TREND_SLOPE_LOOKBACK:
            past = closes[-(TREND_SLOPE_LOOKBACK + 1)]
            ok_gate = (closes[-1] - past) >= 0

        return ok_gate, (ma if ma is not None else float("nan"))
    except Exception as e:
        print(f"[TREND] fetch fail: {e}")
        return True, float("nan")


# -----------------------
# Persistente log helpers
# -----------------------
def load_trades():
    global trade_log
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r", encoding="utf-8") as f:
                trade_log = json.load(f)
                if not isinstance(trade_log, list):
                    trade_log = []
            print(
                f"[LOG] geladen: {len(trade_log)} trades uit {TRADES_FILE}", flush=True
            )
        else:
            trade_log = []
    except Exception as e:
        print(f"[LOG] load error: {e}", flush=True)
        trade_log = []


def save_trades():
    try:
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trade_log, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[LOG] save error: {e}", flush=True)


def log_trade(action: str, price: float, winst: float, source: str, tf: str):
    trade_log.append(
        {
            "timestamp": now_str(),
            "action": action,
            "price": round(price, 4),
            "winst": round(winst, 2),
            "source": source,
            "tf": tf,
            "capital": round(capital, 2),
            "sparen": round(sparen, 2),
        }
    )
    if len(trade_log) > 2000:
        trade_log[:] = trade_log[-2000:]
    save_trades()


# -----------------------
# Multi-exchange public clients (cache)
# -----------------------
_client_cache = {}


def _client(name: str):
    ex = _client_cache.get(name)
    if ex:
        return ex
    if name == "binance":
        ex = ccxt.binance({"enableRateLimit": True})
    elif name == "bybit":
        ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    elif name == "okx":
        ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    elif name == "mexc":
        ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    else:
        raise ValueError(f"unknown exchange: {name}")
    _client_cache[name] = ex
    return ex


def _sources_order():
    src = EXCHANGE_SOURCE.lower()
    if src == "mexc":
        return ["mexc"]
    if src == "auto":
        # desnoods: eerst MEXC, dan optioneel bybit/okx; laat binance eruit
        return ["mexc", "bybit", "okx"]
    return [src]


def fetch_ohlcv_any(symbol_tv="XRP/USDT", timeframe="1m", limit=210):
    errs = []
    for name in _sources_order():
        try:
            ex = _client(name)
            data = ex.fetch_ohlcv(symbol_tv, timeframe=timeframe, limit=limit)
            return data, name
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise Exception(" | ".join(errs))


def fetch_last_price_any(symbol_tv="XRP/USDT"):
    errs = []
    for name in _sources_order():
        try:
            ex = _client(name)
            t = ex.fetch_ticker(symbol_tv)
            last = float(
                t.get("last")
                or t.get("close")
                or t.get("info", {}).get("lastPrice")
                or 0.0
            )
            if last > 0:
                return last, name
            raise Exception("no last price")
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise Exception(" | ".join(errs))


# -----------------------
# LIVE trading (MEXC)
# -----------------------
def _mexc_live():
    ak = os.getenv("MEXC_API_KEY", "")
    sk = os.getenv("MEXC_API_SECRET", "")
    if not ak or not sk:
        raise RuntimeError("MEXC_API_KEY/SECRET ontbreken")
    ex = ccxt.mexc(
        {
            "apiKey": ak,
            "secret": sk,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    ex.load_markets()
    return ex


def _qty_for_quote(ex, symbol, quote_amount, price):
    raw = max(float(quote_amount) / float(price), 0.0)
    amt = float(ex.amount_to_precision(symbol, raw))
    m = ex.market(symbol)
    min_amt = (m.get("limits") or {}).get("amount", {}).get("min") or 0.0
    if min_amt and amt < float(min_amt):
        amt = float(ex.amount_to_precision(symbol, float(min_amt)))
    return amt


def _place_mexc_market(side: str, quote_amount_usd: float, ref_price: float):
    ex = _mexc_live()
    symbol = SYMBOL_TV
    amount = _qty_for_quote(ex, symbol, quote_amount_usd, ref_price)
    if amount <= 0:
        raise RuntimeError("Calculated amount <= 0")
    order = ex.create_order(symbol, "market", side, amount)
    avg = order.get("average") or order.get("price") or ref_price
    filled = order.get("filled") or amount
    return float(avg), float(filled), order


# Rehydrate live positie (spot)
def _rehydrate_from_mexc():
    global in_position, pos_amount, entry_price
    try:
        ex = _mexc_live()
        bal = ex.fetch_balance()
        xrp = bal.get("XRP") or {}
        total_amt = float(xrp.get("total") or 0.0)
        if total_amt >= REHYDRATE_MIN_XRP:
            pos_amount = float(ex.amount_to_precision(SYMBOL_TV, total_amt))
            in_position = True
            # schat entry uit recente trades (7d)
            try:
                since = int((time.time() - 7 * 24 * 3600) * 1000)
                trades = ex.fetch_my_trades(SYMBOL_TV, since=since)
                net = 0.0
                cost = 0.0
                for t in trades:
                    amt = float(t.get("amount") or 0.0)
                    pr = float(t.get("price") or 0.0)
                    side = t.get("side")
                    if side == "buy":
                        net += amt
                        cost += amt * pr
                    elif side == "sell":
                        net -= amt
                        cost -= amt * pr
                    if net <= 1e-12:
                        net, cost = 0.0, 0.0
                if net > 0 and cost > 0:
                    entry_price = cost / net
            except Exception as e2:
                _dbg(f"[REHYDRATE] trade-based entry calc failed: {e2}")
            _dbg(
                f"[REHYDRATE] detected XRP position amt={pos_amount}, entry≈{entry_price}"
            )
            return True
        else:
            _dbg(f"[REHYDRATE] small dust {total_amt} XRP -> ignore")
    except Exception as e:
        _dbg(f"[REHYDRATE] failed: {e}")
    return False


# -----------------------
# Flask
# -----------------------
app = Flask(__name__)


# Advisor endpoints (embedded)
@app.route("/advisor", methods=["GET", "POST"])
def advisor_endpoint():
    try:
        if request.method == "GET":
            sym = request.args.get("symbol", SYMBOL_STR)
            return jsonify({"ok": True, "applied": _advisor_applied(sym)})

        data = request.get_json(force=True, silent=True) or {}
        if str(data.get("_action", "")).lower() == "get":
            sym = data.get("symbol", SYMBOL_STR)
            return jsonify({"ok": True, "applied": _advisor_applied(sym)})

        sym = data.get("symbol", SYMBOL_STR)
        act = str(data.get("action", "")).lower()
        if act not in {"buy", "sell"}:
            return jsonify({"ok": False, "error": "bad_payload"}), 400
        return jsonify(
            {
                "ok": True,
                "approve": True,
                "reason": "open",
                "applied": _advisor_applied(sym),
            }
        )
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
    raw = (
        data.get("values")
        or data.get("changes")
        or {k: v for k, v in data.items() if k.lower() not in {"symbol"}}
    )
    vals = _advisor_coerce(raw)
    ap = _advisor_applied(sym)
    ap.update(vals)
    ADVISOR_STATE["symbols"][sym] = {"applied": ap, "ts": int(time.time())}
    ADVISOR_STATE["updated"] = ADVISOR_STATE["symbols"][sym]["ts"]
    _advisor_save()
    return jsonify({"ok": True, "applied": ap})


@app.route("/advisor/set", methods=["POST"])
def advisor_set_compat():
    return advisor_admin_set()


@app.route("/advisor/tweak", methods=["POST"])
def advisor_admin_tweak():
    return advisor_admin_set()


@app.route("/debug/ping", methods=["GET"])
def debug_ping():
    _dbg("ping from /debug/ping")
    print("[DIRECT] debug ping hit", flush=True)
    return jsonify({"ok": True, "debug_sig": DEBUG_SIG}), 200


@app.route("/health")
def health():
    return "OK", 200


# Cooldown & De-dup
def blocked_by_cooldown() -> bool:
    if MIN_TRADE_COOLDOWN_S <= 0:
        return False
    return (time.time() - last_action_ts) < MIN_TRADE_COOLDOWN_S


def is_duplicate_signal(action: str, source: str, price: float, tf: str) -> bool:
    key = (
        (action or "").lower(),
        (source or "").lower(),
        round(float(price), 4),
        (tf or "").lower(),
    )
    ts_prev = last_signal_key_ts.get(key, 0.0)
    if (time.time() - ts_prev) <= DEDUP_WINDOW_S:
        return True
    last_signal_key_ts[key] = time.time()
    return False


# -------------- Webhook --------------
@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, entry_price, capital, sparen, last_action_ts, entry_ts, tpsl_state, pos_amount, pos_quote

    # Secret
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return "Unauthorized", 401

    _dbg(f"/webhook hit ct={request.headers.get('Content-Type')} raw={request.data[:200]!r}")

    data   = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).lower().strip()
    source = str(data.get("source", "unknown")).lower().strip()
    tf     = str(data.get("tf", "1m")).lower().strip()

    # Parse prijs en bewaar TV-prijs (price kan later vervangen worden door MEXC fill)
    try:
        price = float(data.get("price", 0) or 0.0)
    except Exception:
        _dbg("bad price in payload")
        return "Bad price", 400
    if price <= 0 or action not in ("buy", "sell"):
        _dbg("bad payload guard (price<=0 of action niet buy/sell)")
        return "Bad payload", 400
    tv_price = price

    # TV symbol guard (alleen LIVE MEXC)
    tv_symbol = str(data.get("tv_symbol", "")).upper()
    if LIVE_EXCHANGE == "mexc" and tv_symbol:
        if not (tv_symbol.startswith("MEXC:") and tv_symbol.endswith("XRPUSDT")):
            _dbg(f"[TVGUARD] ignored non-MEXC alert tv_symbol={tv_symbol}")
            return "OK", 200

    # De-dup & cooldown (SELL mag cooldown bypassen als we in positie zijn)
    if is_duplicate_signal(action, source, price, tf):
        _dbg(f"dedup ignored key={(action, source, round(price,4), tf)} win={DEDUP_WINDOW_S}s")
        return "OK", 200
    if blocked_by_cooldown():
        if action == "sell" and in_position:
            _dbg("cooldown bypassed for SELL while in_position")
        else:
            _dbg(f"cooldown ignored min={MIN_TRADE_COOLDOWN_S}s since last_action")
            return "OK", 200

    # Advisor allow?
    allowed, advisor_reason = advisor_allows(action, price, source, tf)
    _dbg(f"advisor {('ALLOW' if allowed else 'BLOCK')} reason={advisor_reason}")
    if not allowed:
        return "OK", 200

    # --- Ghost-position guard (alleen LIVE MEXC) ---
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        if in_position and (pos_amount <= 1e-12 or entry_price <= 0):
            _dbg(f"[GUARD] ghost detected: in_position={in_position}, pos_amount={pos_amount}, entry_price={entry_price}")
            in_position = False
            entry_price = 0.0
            pos_amount  = 0.0
            pos_quote   = 0.0
            tpsl_reset()

    timestamp = now_str()


    # === BUY ===
    if action == "buy":
        if in_position:
            _dbg(f"buy ignored: already in_position at entry={entry_price}")
            return "OK", 200

        ok, ma = trend_ok(price)
        _dbg(f"trend_ok={ok} ma={ma:.6f} price={price:.6f}")
        if not ok:
            _dbg("blocked by trend filter")
            return "OK", 200

        # Bewaar TV-entry prijs voor TG (we gaan 'price' zo mogelijk overschrijven met fill)
        tv_entry = tv_price

        # --- LIVE BUY op MEXC ---
        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            try:
                avg, filled, order = _place_mexc_market("buy", START_CAPITAL, price)
                price = float(avg)           # echte fill als entry
                pos_amount = float(filled)   # aantal XRP gekocht
                cost = order.get("cost")
                pos_quote = float(cost) if cost is not None else float(avg) * float(filled)  # werkelijk besteed USDT
                _dbg(f"[LIVE] MEXC BUY id={order.get('id')} filled={filled} avg={avg} cost={pos_quote}")
            except Exception as e:
                _dbg(f"[LIVE] MEXC BUY failed: {e}")
                return "LIVE BUY failed", 500
        # ------------------------------------

        # State
        entry_price    = price
        in_position    = True
        last_action_ts = time.time()
        entry_ts       = time.time()
        _dbg(f"BUY executed: entry={entry_price}")

        # Arm lokale TPSL (Optie 3) — gebruikt de entry (bij LIVE = MEXC fill)
        tpsl_on_buy(price)

        # TG met TV-prijs + MEXC fill (incl. delta als tv_entry > 0)
        display_fill = price if (LIVE_MODE and LIVE_EXCHANGE == "mexc") else tv_entry
        delta_txt = f"  (Δ {(display_fill / tv_entry - 1) * 100:+.2f}%)" if tv_entry else ""
        send_tg(
            f"""🟢 <b>[XRP/USDT] AANKOOP</b>
📹 TV prijs: ${tv_entry:.4f}
🎯 Fill (MEXC): ${display_fill:.4f}{delta_txt}
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
    if action == "sell":
        # niet in positie? probeer lazy rehydrate voor LIVE MEXC
        if not in_position:
            did = False
            if LIVE_MODE and LIVE_EXCHANGE == "mexc":
                try:
                    did = _rehydrate_from_mexc()
                    _dbg(f"[REHYDRATE] lazy attempt result={did} in_position={in_position} pos_amount={pos_amount}")
                except NameError:
                    _dbg("[REHYDRATE] function not present; skipping")
                except Exception as e:
                    _dbg(f"[REHYDRATE] lazy failed: {e}")

        # als we nog steeds niets hebben, en er is ook geen echte live size -> terug
        if not in_position and not (LIVE_MODE and LIVE_EXCHANGE == "mexc" and pos_amount > 1e-12):
            _dbg("sell ignored: not in_position")
            return "OK", 200

        # Bewaar TV-verkoopprijs vóór we 'price' vervangen door MEXC fill
        tv_sell = tv_price

        # --- LIVE SELL op MEXC (robust + .env tuning) ---
        amt_for_pnl = float(pos_amount)
        inleg_quote  = float(pos_quote or 0.0)

        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            ex   = None
            base = "XRP"
            try:
                ex   = _mexc_live()
                m    = ex.market(SYMBOL_TV)
                base = m.get("base") or "XRP"

                # Vrije hoeveelheid XRP
                bal       = ex.fetch_balance()
                free_amt  = float((bal.get(base) or {}).get("free") or 0.0)

                # Gewenst vs vrij
                wanted = float(ex.amount_to_precision(SYMBOL_TV, pos_amount))
                amt    = min(wanted, free_amt)

                # Epsilon onder hoeveelheid-precision (via .env ticks)
                prec    = int((m.get("precision") or {}).get("amount") or 6)
                ticks   = max(1, int(SELL_EPSILON_TICKS))
                epsilon = (10 ** (-prec)) * ticks
                amt     = max(0.0, amt - epsilon)
                amt     = float(ex.amount_to_precision(SYMBOL_TV, amt))

                if amt <= 0:
                    _dbg(f"[LIVE] MEXC SELL skipped: amt<=0 (pos_amount={pos_amount}, free={free_amt})")
                    return "OK", 200

                order  = ex.create_order(SYMBOL_TV, "market", "sell", amt)
                avg    = order.get("average") or order.get("price") or price
                filled = float(order.get("filled") or amt)
                price  = float(avg)

                # Partial fill → schaal quote-inleg voor PnL
                if pos_amount > 0 and filled < pos_amount:
                    inleg_quote = inleg_quote * (filled / pos_amount)

                amt_for_pnl = filled
                _dbg(f"[LIVE] MEXC SELL id={order.get('id')} filled={filled} avg={price} amt={amt}")

            except Exception as e:
                msg = str(e)
                _dbg(f"[LIVE] MEXC SELL error: {msg}")

                # Specifiek 'Oversold/Insufficient' → 1 retry met .env ratio
                if "Oversold" in msg or "30005" in msg or "Insufficient" in msg:
                    try:
                        ex  = ex or _mexc_live()
                        bal = ex.fetch_balance()
                        free_amt  = float((bal.get(base) or {}).get("free") or 0.0)
                        ratio     = float(SELL_RETRY_RATIO or 0.995)
                        retry_amt = float(ex.amount_to_precision(SYMBOL_TV, max(0.0, free_amt * ratio)))

                        if retry_amt > 0:
                            order  = ex.create_order(SYMBOL_TV, "market", "sell", retry_amt)
                            avg    = order.get("average") or order.get("price") or price
                            filled = float(order.get("filled") or retry_amt)
                            price  = float(avg)

                            if pos_amount > 0 and filled < pos_amount:
                                inleg_quote = inleg_quote * (filled / pos_amount)

                            amt_for_pnl = filled
                            _dbg(f"[LIVE] MEXC SELL RETRY ok id={order.get('id')} filled={filled} avg={price} amt={retry_amt}")
                        else:
                            _dbg("[LIVE] RETRY skipped: retry_amt<=0")
                            return "OK", 200
                    except Exception as e2:
                        _dbg(f"[LIVE] RETRY failed: {e2}")
                        return "OK", 200
                else:
                    # Andere fout → geen 500 naar TV; laat webhook door
                    return "OK", 200
        # ----------------------------------------------

        _dbg(f"[SELLDBG] amt_for_pnl={amt_for_pnl}, inleg_quote={inleg_quote}, fill={price}")

        # PnL & boeking
        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            verkoop_bedrag = price * amt_for_pnl
            inleg = inleg_quote
            winst_bedrag = round(verkoop_bedrag - inleg, 2)
        else:
            if entry_price > 0:
                verkoop_bedrag = price * START_CAPITAL / entry_price
                winst_bedrag = round(verkoop_bedrag - START_CAPITAL, 2)
            else:
                _dbg("[SELL] missing entry_price; booking pnl=0 fallback")
                winst_bedrag = 0.0

        _dbg(f"SELL calc -> price={price} entry={entry_price} pnl={winst_bedrag}")

        if winst_bedrag > 0:
            sparen  += SAVINGS_SPLIT * winst_bedrag
            capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
        else:
            capital += winst_bedrag  # verlies ten laste van trading-kapitaal

        # top-up naar START_CAPITAL
        if capital < START_CAPITAL:
            tekort = START_CAPITAL - capital
            if sparen >= tekort:
                sparen -= tekort
                capital += tekort

        # reset state (na berekening!)
        in_position    = False
        entry_price    = 0.0
        pos_amount     = 0.0
        pos_quote      = 0.0
        last_action_ts = time.time()

        if LOCAL_TPSL_ENABLED:
            tpsl_reset()
            _dbg("[TPSL] reset on SELL")

        # TG: toon TV-prijs én MEXC-fill (met delta)
        display_fill = price if (LIVE_MODE and LIVE_EXCHANGE == "mexc") else tv_sell
        delta_txt = f"  (Δ {(display_fill / tv_sell - 1) * 100:+.2f}%)" if tv_sell else ""
        resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
        send_tg(
            f"""📄 <b>[XRP/USDT] VERKOOP</b>
📹 TV prijs: ${tv_sell:.4f}
🎯 Fill (MEXC): ${display_fill:.4f}{delta_txt}
🧠 Signaalbron: {source} | {advisor_reason}
🕒 TF: {tf}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 {resultaat}: €{winst_bedrag:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{START_CAPITAL:.2f}
🔗 Tijd: {timestamp}"""
        )
        log_trade("sell", price, winst_bedrag, source, tf)
        return "OK", 200

# -----------------------
# Safety / forced exits
# -----------------------
def _do_forced_sell(
    price: float, reason: str, source: str = "forced_exit", tf: str = "1m"
) -> bool:
    global in_position, entry_price, capital, sparen, last_action_ts, pos_amount, pos_quote

    # Laat forced sell toe als we in positie zijn OF (live op MEXC en er is echt size)
    if not in_position and not (LIVE_MODE and LIVE_EXCHANGE == "mexc" and pos_amount > 1e-12):
        return False

    # Snapshot vóór we iets wijzigen (belangrijk voor PnL)
    amt_before = float(pos_amount)
    quote_before = float(pos_quote)

    # LIVE exit (MEXC) - robust tegen 'Oversold'
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            ex   = _mexc_live()
            m    = ex.market(SYMBOL_TV)
            base = m.get("base") or "XRP"

            bal      = ex.fetch_balance()
            free_amt = float((bal.get(base) or {}).get("free") or 0.0)

            wanted = float(ex.amount_to_precision(SYMBOL_TV, amt_before))
            amt    = min(wanted, free_amt)
            prec    = int((m.get("precision") or {}).get("amount") or 6)
            epsilon = 10 ** (-prec)
            amt     = max(0.0, amt - epsilon)
            amt     = float(ex.amount_to_precision(SYMBOL_TV, amt))

            if amt > 0:
                order  = ex.create_order(SYMBOL_TV, "market", "sell", amt)
                avg    = order.get("average") or order.get("price") or price
                filled = float(order.get("filled") or amt)
                price  = float(avg)

                # Partial fill → schaal quote-inleg + amt voor PnL
                if amt_before > 0 and filled < amt_before:
                    quote_before = quote_before * (filled / amt_before)
                amt_before = filled

                _dbg(f"[LIVE] MEXC FORCED SELL id={order.get('id')} filled={filled} avg={price}")
            else:
                _dbg("[LIVE] forced sell skipped: amt<=0")
                return False

        except Exception as e:
            msg = str(e)
            _dbg(f"[LIVE] MEXC FORCED SELL error: {msg}")
            if "Oversold" in msg or "30005" in msg or "Insufficient" in msg:
                try:
                    bal       = ex.fetch_balance()
                    free_amt  = float((bal.get(base) or {}).get("free") or 0.0)
                    retry_amt = float(ex.amount_to_precision(SYMBOL_TV, max(0.0, free_amt * 0.995)))
                    if retry_amt > 0:
                        order  = ex.create_order(SYMBOL_TV, "market", "sell", retry_amt)
                        avg    = order.get("average") or order.get("price") or price
                        filled = float(order.get("filled") or retry_amt)
                        price  = float(avg)
                        if amt_before > 0 and filled < amt_before:
                            quote_before = quote_before * (filled / amt_before)
                        amt_before = filled
                        _dbg(f"[LIVE] MEXC FORCED SELL RETRY ok id={order.get('id')} filled={filled} avg={price}")
                    else:
                        _dbg("[LIVE] FORCED RETRY skipped: retry_amt<=0")
                        return False
                except Exception as e2:
                    _dbg(f"[LIVE] FORCED RETRY failed: {e2}")
                    return False


    # PnL berekenen:
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        verkoop_bedrag = float(price) * float(amt_before)
        inleg = float(quote_before)
    else:
        # Simulatiepad (of geen live): val terug op START_CAPITAL/entry
        if entry_price > 0:
            verkoop_bedrag = price * START_CAPITAL / entry_price
            inleg = START_CAPITAL
        else:
            # Als entry onbekend is, boek geen PnL (defensief)
            verkoop_bedrag = START_CAPITAL
            inleg = START_CAPITAL

    winst_bedrag = round(verkoop_bedrag - inleg, 2)

    # Boekhouding
    if winst_bedrag > 0:
        sparen  += SAVINGS_SPLIT * winst_bedrag
        capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
    else:
        capital += winst_bedrag  # verlies ten laste van trading-kapitaal

    # Top-up terug naar START_CAPITAL
    if capital < START_CAPITAL:
        tekort = START_CAPITAL - capital
        if sparen >= tekort:
            sparen -= tekort
            capital += tekort

    # State reset (pas NA berekening)
    in_position = False
    entry_price = 0.0
    pos_amount  = 0.0
    pos_quote   = 0.0
    last_action_ts = time.time()
    tpsl_reset()

    # TG + log
    resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
    timestamp = now_str()
    send_tg(
        "🚨 <b>[XRP/USDT] FORCED SELL</b>\n"
        f"📹 Verkoopprijs: ${price:.4f}\n"
        f"🧠 Reden: {reason}\n"
        f"🕒 TF: {tf}\n"
        f"💰 Handelssaldo: €{capital:.2f}\n"
        f"💼 Spaarrekening: €{sparen:.2f}\n"
        f"📈 {resultaat}: €{winst_bedrag:.2f}\n"
        f"📈 Totale waarde: €{capital + sparen:.2f}\n"
        f"🔐 Tradebedrag: €{START_CAPITAL:.2f}\n"
        f"🔗 Tijd: {timestamp}"
    )
    log_trade("sell", price, winst_bedrag, source, tf)
    return True


def forced_exit_check(last_price: float | None = None):
    if not in_position or entry_price <= 0:
        return

    # Prijs ophalen indien nodig
    if last_price is None:
        try:
            last_price, src = fetch_last_price_any(SYMBOL_TV)
        except Exception as e:
            print(f"[PRICE] fetch fail all: {e}")
            return

    # Hard SL pas na arm-delay
    if HARD_SL_PCT > 0 and entry_ts > 0 and (time.time() - entry_ts) >= HARD_SL_ARM_SEC:
        if last_price <= entry_price * (1.0 - HARD_SL_PCT):
            _do_forced_sell(last_price, f"hard_sl_{HARD_SL_PCT:.3%}")
            return

    # Max hold
    if (
        MAX_HOLD_MIN > 0
        and entry_ts > 0
        and (time.time() - entry_ts) >= MAX_HOLD_MIN * 60
    ):
        _do_forced_sell(last_price, f"max_hold_{MAX_HOLD_MIN}m")
        return


# -----------------------
# Rapportage & config
# -----------------------
@app.route("/report/daily", methods=["GET"])
def report_daily():
    today = datetime.now().date()
    trades_today = [
        t
        for t in trade_log
        if datetime.strptime(t["timestamp"], "%d-%m-%Y %H:%M:%S").date() == today
    ]
    total_pnl = sum(t.get("winst", 0.0) for t in trades_today)
    return jsonify(
        {
            "symbol": SYMBOL_STR,
            "capital": round(capital, 2),
            "sparen": round(sparen, 2),
            "totaalwaarde": round(capital + sparen, 2),
            "in_position": in_position,
            "entry_price": round(entry_price, 4),
            "pos_amount": pos_amount,
            "pos_quote": pos_quote,
            "laatste_actie": (
                datetime.fromtimestamp(last_action_ts).strftime("%d-%m-%Y %H:%M:%S")
                if last_action_ts > 0
                else None
            ),
            "trades_vandaag": trades_today,
            "pnl_vandaag": round(total_pnl, 2),
        }
    )


@app.route("/report/weekly", methods=["GET"])
def report_weekly():
    now = datetime.now()
    week_start = now - timedelta(days=7)
    trades_week = [
        t
        for t in trade_log
        if datetime.strptime(t["timestamp"], "%d-%m-%Y %H:%M:%S") >= week_start
    ]
    total_pnl = sum(t.get("winst", 0.0) for t in trades_week)
    return jsonify(
        {
            "symbol": SYMBOL_STR,
            "capital": round(capital, 2),
            "sparen": round(sparen, 2),
            "totaalwaarde": round(capital + sparen, 2),
            "trades_week": trades_week,
            "pnl_week": round(total_pnl, 2),
        }
    )


@app.route("/report/save", methods=["POST"])
def report_save():
    save_trades()
    return jsonify({"saved": True, "file": TRADES_FILE, "count": len(trade_log)})


_last_price_cache = {"price": None, "src": None, "ts": 0.0}


@app.route("/config", methods=["GET"])
def config_view():
    last_action_str = (
        datetime.fromtimestamp(last_action_ts).strftime("%d-%m-%Y %H:%M:%S")
        if last_action_ts > 0
        else None
    )
    payload = {
        "live_exchange": LIVE_EXCHANGE,
        "mexc_keys_set": bool(
            os.getenv("MEXC_API_KEY") and os.getenv("MEXC_API_SECRET")
        ),
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
        "advisor_secret_set": bool(ADVISOR_SECRET),
        "telegram_config_ok": bool(TG_TOKEN and TG_CHAT_ID),
        "trades_file": TRADES_FILE,
        # safety
        "sell_retry_ratio": SELL_RETRY_RATIO,
        "sell_epsilon_ticks": SELL_EPSILON_TICKS,
        "hard_sl_pct": round(HARD_SL_PCT, 6),
        "hard_sl_arm_sec": int(HARD_SL_ARM_SEC),
        "max_hold_min": int(MAX_HOLD_MIN),
        "price_poll_s": int(PRICE_POLL_S),
        # runtime
        "in_position": in_position,
        "entry_price": round(entry_price, 4),
            "pos_amount": pos_amount,
            "pos_quote": pos_quote,
        "capital": round(capital, 2),
        "sparen": round(sparen, 2),
        "totaalwaarde": round(capital + sparen, 2),
        "last_action": last_action_str,
        # laatste prijs (na eerste poll)
        "last_price": _last_price_cache["price"],
        "last_price_source": _last_price_cache["src"],
        # advisor applied
        "advisor_applied": _advisor_applied(SYMBOL_STR),
        # lokale TPSL monitor
        "local_tpsl_enabled": LOCAL_TPSL_ENABLED,
        "local_sl_arm_sec": int(LOCAL_SL_ARM_SEC),
        "local_tp_arm_sec": int(LOCAL_TP_ARM_SEC),
        "tpsl_state": {
            "active": tpsl_state.get("active", False),
            "armed": tpsl_state.get("armed", False),
            "tp_pct": tpsl_state.get("tp_pct"),
            "sl_pct": tpsl_state.get("sl_pct"),
            "trail_pct": tpsl_state.get("trail_pct"),
            "arm_at_pct": tpsl_state.get("arm_at_pct"),
            "armed_since": tpsl_state.get("start_ts"),
            "high": tpsl_state.get("high"),
        },
    }
    return jsonify(payload)


# -----------------------
# Idle worker (poll price -> TPSL & safety)
# -----------------------
def idle_worker():
    while True:
        time.sleep(PRICE_POLL_S)
        try:
            last, src = fetch_last_price_any(SYMBOL_TV)
            _last_price_cache["price"] = last
            _last_price_cache["src"] = src
            _last_price_cache["ts"] = time.time()

            local_tpsl_check(last)
            forced_exit_check(last)
        except Exception as e:
            print(f"[IDLE] error: {e}")
            continue


# -----------------------
# Main
# -----------------------
if __name__ == "__main__":
    _advisor_load()
    load_trades()

    # On boot: rehydrate live positie (na defs)
    if LIVE_MODE and LIVE_EXCHANGE == "mexc" and REHYDRATE_ON_BOOT:
        try:
            _rehydrate_from_mexc()
        except Exception as _e:
            _dbg(f"[REHYDRATE] on boot error: {_e}")

    port = int(os.environ.get("PORT", "5000"))
    print(f"✅ Webhook server op http://0.0.0.0:{port}/webhook")
    Thread(target=idle_worker, daemon=True).start()
    send_tg("✅ XRP-bot gestart op Render")
    app.run(host="0.0.0.0", port=port)
