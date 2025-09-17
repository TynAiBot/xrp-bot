# app.py — Multi-pair bot (XRP/USDT + WLFI/USDT)
# - TradingView webhooks (buy/sell) met symbool-herkenning (MEXC:WLFIUSDT, WLFI/USDT, etc.)
# - Live SPOT trading via MEXC (ccxt), fees & partial fills → PnL pro rata
# - De-dup per symbool + cooldown per symbool
# - Persistente trade-logging (/report/daily, /report/weekly)
# - Lokale TPSL-monitor (TP/SL/Trailing) + arming delays + hard SL + max-hold
# - Rehydrate live positie(s) op boot + ghost-guard + dust-override
# - Embedded Advisor endpoints (allow passthrough) + admin tweak storage
# - /config met uitgebreide runtime status
#
# Taal: NL — 4-spaties inspringing — "paste‑ready"

import os, time, json
from datetime import datetime, timedelta
from threading import Thread, Lock
from typing import Tuple, Dict, Any
import html

# ---- MEXC recvWindow helper/constant (robust) ----
try:
    MEXC_RECV_WINDOW = env_int("MEXC_RECVWINDOW_MS", 10000)  # ms
except Exception:
    import os
    try:
        MEXC_RECV_WINDOW = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
    except Exception:
        MEXC_RECV_WINDOW = 10000

def _recvwin() -> int:
    try:
        return int(globals().get("MEXC_RECV_WINDOW", 10000))
    except Exception:
        return 10000

def ht(x) -> str:
    """Veilige HTML-escape voor Telegram parse_mode=HTML."""
    try:
        return html.escape(str(x), quote=False)
    except Exception:
        return ""

import requests
import ccxt
from flask import Flask, request, jsonify
from dotenv import load_dotenv


# -----------------------
# Env helpers
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

PORT = env_int("PORT", 10000)

# -----------------------
# Basis & multi-pair
# -----------------------
START_CAPITAL = float(os.getenv("START_CAPITAL", "500"))  # alleen display/simulatie

TRADEABLE: Dict[str, float] = {   # budget per paar (USDT)
    "XRP/USDT": float(os.getenv("BUDGET_XRP", "500")),
    "WLFI/USDT": float(os.getenv("BUDGET_WLFI", "500")),
}

def _sym_str(sym: str) -> str:
    return (sym or "XRP/USDT").replace("/", "").upper()

# Per-paar runtime state (inclusief TPSL dict)
def _fresh_tpsl():
    return {
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

STATE: Dict[str, Dict[str, Any]] = {
    sym: {
        "in_position": False,
        "entry_price": 0.0,
        "entry_ts": 0.0,
        "last_action_ts": 0.0,
        "pos_amount": 0.0,   # base
        "pos_quote": 0.0,    # netto inleg in USDT
        "capital": START_CAPITAL,
        "sparen": float(os.getenv("SPAREN_START", "500")),
        "tpsl": _fresh_tpsl(),
    } for sym in TRADEABLE
}

# Context (gebonden door bind())
CURRENT_SYMBOL = "XRP/USDT"
CURRENT_SYMBOL_STR = _sym_str(CURRENT_SYMBOL)

# Legacy globals (worden bij bind() uit STATE gevuld)
in_position = False
entry_price = 0.0
entry_ts = 0.0
last_action_ts = 0.0
pos_amount = 0.0
pos_quote = 0.0
capital = START_CAPITAL
sparen = float(os.getenv("SPAREN_START", "500"))
tpsl_state = _fresh_tpsl()
SYMBOL_LOCKS = {s: Lock() for s in TRADEABLE}
# Anti-spam voor forced-sell meldingen
FORCED_WARN_TS: Dict[str, float] = {}
FORCED_WARN_COOLDOWN_S = env_int("FORCED_WARN_COOLDOWN_S", 60)  # seconden


# De-dup window & per-key timestamp
MIN_TRADE_COOLDOWN_S = env_int("MIN_TRADE_COOLDOWN_S", 0)  # user: vaak 0s
DEDUP_WINDOW_S = env_int("DEDUP_WINDOW_S", 1)
last_signal_key_ts: Dict[tuple, float] = {}  # (symbol, action, source, round(price,4), tf) -> ts

# Winstverdeling & guardrails
SAVINGS_SPLIT = env_float("SAVINGS_SPLIT", 1.0)  # 1.0 = 100% naar sparen
HARD_SL_PCT = env_float("HARD_SL_PCT", 0.02)
HARD_SL_ARM_SEC = env_int("HARD_SL_ARM_SEC", 15)
MAX_HOLD_MIN = env_int("MAX_HOLD_MIN", 45)
PRICE_POLL_S = env_int("PRICE_POLL_S", 2)

# Data-bron fallback
EXCHANGE_SOURCE = os.getenv("EXCHANGE_SOURCE", "auto").lower()  # auto/mexc/bybit/okx

# Trendfilter (optioneel)
USE_TREND_FILTER = env_bool("USE_TREND_FILTER", False)
TREND_MODE = os.getenv("TREND_MODE", "off").lower()  # off|soft|hard
TREND_MA_LEN = env_int("TREND_MA_LEN", 50)
TREND_MA_TYPE = os.getenv("TREND_MA_TYPE", "SMA").upper()  # SMA|EMA
TREND_TF = os.getenv("TREND_TF", "1m")
TREND_SLOPE_LOOKBACK = env_int("TREND_SLOPE_LOOKBACK", 0)
TREND_SOFT_TOL = env_float("TREND_SOFT_TOL", 0.001)

# Rehydrate
REHYDRATE_ON_BOOT = env_bool("REHYDRATE_ON_BOOT", True)
REHYDRATE_MIN_XRP = env_float("REHYDRATE_MIN_XRP", 5.0)  # dust

# Advisor
ADVISOR_ENABLED = env_bool("ADVISOR_ENABLED", False)
ADVISOR_URL = os.getenv("ADVISOR_URL", "")
ADVISOR_SECRET = os.getenv("ADVISOR_SECRET", "")
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

# Lokale TPSL
LOCAL_TPSL_ENABLED = env_bool("LOCAL_TPSL_ENABLED", True)
SYNC_TPSL_FROM_ADVISOR = env_bool("SYNC_TPSL_FROM_ADVISOR", True)
LOCAL_TP_PCT = env_float("LOCAL_TP_PCT", 0.010)
LOCAL_SL_PCT = env_float("LOCAL_SL_PCT", 0.009)
LOCAL_USE_TRAIL = env_bool("LOCAL_USE_TRAIL", True)
LOCAL_TRAIL_PCT = env_float("LOCAL_TRAIL_PCT", 0.007)
LOCAL_ARM_AT_PCT = env_float("LOCAL_ARM_AT_PCT", 0.002)
LOCAL_SL_ARM_SEC = env_int("LOCAL_SL_ARM_SEC", 20)
LOCAL_TP_ARM_SEC = env_int("LOCAL_TP_ARM_SEC", 0)

# Webhook beveiliging
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# LIVE mode / exchange
LIVE_MODE = env_bool("LIVE_MODE", True)
LIVE_EXCHANGE = os.getenv("LIVE_EXCHANGE", "mexc").lower()

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
if not TG_TOKEN or not TG_CHAT_ID:
    raise SystemExit("[FOUT] TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt.")

# Trend client (alleen als filter aanstaat)
trend_exchange = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}}) if USE_TREND_FILTER else None

DEBUG_SIG = env_bool("DEBUG_SIG", True)

# --- Speed toggles / client cache ---
FAST_FILL = env_bool("FAST_FILL", True)   # True = skip extra fetch loops na order voor snelheid
EX_LIVE = None                            # cached ccxt client (MEXC)


def _dbg(msg: str):
    if DEBUG_SIG:
        print(f"[SIGDBG] {msg}", flush=True)


def now_str():
    return datetime.now().strftime("%d-%m-%Y %H:%M:%S")


def get_budget(sym: str) -> float:
    return float(TRADEABLE.get(sym, START_CAPITAL))


# --------------- Symbol helpers ----------------
def _normalize_tv_symbol(s: str | None) -> str | None:
    """'MEXC:WLFIUSDT' / 'WLFIUSDT' / 'WLFI/USDT' -> 'WLFI/USDT'."""
    if not s:
        return None
    s = str(s).upper().strip().replace("MEXC:", "")
    if "/" in s:
        parts = s.split("/", 1)
        if len(parts) == 2 and parts[1] == "USDT" and parts[0]:
            return f"{parts[0]}/USDT"
        return None
    s = s.replace("/", "")
    if s.endswith("USDT") and len(s) > 4:
        base = s[:-4]
        return f"{base}/USDT"
    return None


def extract_symbol_from_payload(data: dict) -> str | None:
    for key in ("symbol", "tv_symbol", "ticker", "TICKER", "pair"):
        val = data.get(key)
        sym = _normalize_tv_symbol(val) if val else None
        if sym:
            return sym
    return None


# --------------- Binding ----------------
def bind(sym: str):
    """Koppel globals aan STATE[sym] voor compacte code in handlers."""
    global CURRENT_SYMBOL, CURRENT_SYMBOL_STR
    global in_position, entry_price, entry_ts, last_action_ts, pos_amount, pos_quote, capital, sparen, tpsl_state
    CURRENT_SYMBOL = sym
    CURRENT_SYMBOL_STR = _sym_str(sym)
    S = STATE[sym]
    in_position = bool(S["in_position"])
    entry_price = float(S["entry_price"])
    entry_ts = float(S["entry_ts"])
    last_action_ts = float(S["last_action_ts"])
    pos_amount = float(S["pos_amount"])
    pos_quote = float(S["pos_quote"])
    capital = float(S["capital"])
    sparen = float(S["sparen"])
    tpsl_state = S["tpsl"]  # reference


def commit(sym: str):
    """Schrijf globals terug naar STATE[sym]."""
    STATE[sym].update({
        "in_position": bool(in_position),
        "entry_price": float(entry_price),
        "entry_ts": float(entry_ts),
        "last_action_ts": float(last_action_ts),
        "pos_amount": float(pos_amount),
        "pos_quote": float(pos_quote),
        "capital": float(capital),
        "sparen": float(sparen),
        "tpsl": tpsl_state,  # dict ref ok
    })


# ---------------- Telegram -----------------
def send_tg(text_html: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": text_html, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                retry_after = 2
                try:
                    retry_after = int(r.headers.get("Retry-After") or 2)
                except Exception:
                    pass
                try:
                    retry_after = int((r.json().get("parameters") or {}).get("retry_after", retry_after))
                except Exception:
                    pass
                time.sleep(min(retry_after, 5))
                continue
            print(f"[TG] {r.status_code} {r.text[:200]}", flush=True)
            break
        except requests.exceptions.Timeout:
            print("[TG] timeout, retrying...", flush=True)
            time.sleep(1 + attempt)
        except Exception as e:
            print(f"[TG ERROR] {e}", flush=True)
            break
    return False


# ---------------- Advisor -----------------
def _advisor_load():
    global ADVISOR_STATE
    try:
        if os.path.exists(ADVISOR_STORE):
            with open(ADVISOR_STORE, "r", encoding="utf-8") as f:
                ADVISOR_STATE = json.load(f)
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


def _advisor_applied(symbol_str: str) -> dict:
    s = (symbol_str or CURRENT_SYMBOL_STR).upper().strip()
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
            out[K] = (bool(v) if isinstance(v, bool) else str(v).strip().lower() in {"1", "true", "yes", "y", "on"})
        elif K in ADVISOR_DEFAULTS:
            try:
                out[K] = round(float(v), 3)
            except Exception:
                pass
    return out


if not ADVISOR_URL:
    ADVISOR_URL = f"http://127.0.0.1:{os.getenv('PORT','5000')}/advisor"


def advisor_allows(action: str, price: float, source: str, tf: str) -> Tuple[bool, str]:
    if not ADVISOR_ENABLED or not ADVISOR_URL:
        return True, "advisor_off"
    payload = {
        "symbol": CURRENT_SYMBOL_STR,
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
        j = {}
        try:
            j = r.json()
        except Exception:
            pass
        allow = bool(j.get("approve", j.get("allow", True)))
        reason = str(j.get("reason", f"status_{r.status_code}"))
        return allow, reason
    except Exception as e:
        print(f"[ADVISOR] unreachable: {e}", flush=True)
        return True, "advisor_unreachable"


# --------------- Files (trades) ---------------
TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
trade_log = []

def load_trades():
    global trade_log
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r", encoding="utf-8") as f:
                trade_log = json.load(f)
        else:
            trade_log = []
    except Exception:
        trade_log = []


def save_trades():
    try:
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trade_log, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[LOG] save error: {e}", flush=True)


def log_trade(action: str, price: float, winst: float, source: str, tf: str, symbol: str):
    trade_log.append(
        {
            "timestamp": now_str(),
            "symbol": symbol,
            "action": action,
            "price": round(price, 6),
            "winst": round(winst, 2),
            "source": source,
            "tf": tf,
            "capital": round(STATE[symbol]["capital"], 2),
            "sparen": round(STATE[symbol]["sparen"], 2),
        }
    )
    if len(trade_log) > 5000:
        trade_log[:] = trade_log[-5000:]
    save_trades()


# --------------- Public clients ---------------
_client_cache: Dict[str, Any] = {}

def _client(name: str):
    ex = _client_cache.get(name)
    if ex:
        return ex
    if name == "bybit":
        ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    elif name == "okx":
        ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    elif name == "mexc":
        ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    else:
        # niet binance i.v.m. NL restricties
        ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    _client_cache[name] = ex
    return ex


def _sources_order():
    src = EXCHANGE_SOURCE.lower()
    if src == "mexc":
        return ["mexc"]
    if src == "auto":
        return ["mexc", "bybit", "okx"]
    return [src]


def fetch_ohlcv_any(symbol_tv="XRP/USDT", timeframe="1m", limit=200):
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
            last = float(t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice") or 0.0)
            if last > 0:
                return last, name
            raise Exception("no last price")
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise Exception(" | ".join(errs))

# --------------- LIVE MEXC ----------------
def _mexc_live():
    global EX_LIVE
    ak = os.getenv("MEXC_API_KEY", "") or os.getenv("MEXC_KEY", "")
    sk = os.getenv("MEXC_API_SECRET", "") or os.getenv("MEXC_SECRET", "")
    if not ak or not sk:
        raise RuntimeError("MEXC_API_KEY/SECRET ontbreken")

    if EX_LIVE is not None:
        return EX_LIVE

    ex = ccxt.mexc({
        "apiKey": ak,
        "secret": sk,
        "enableRateLimit": False,  # snel; laat zo staan voor latency
        "timeout": int(os.getenv("MEXC_TIMEOUT_MS", "10000")),  # was 3000
        "options": {
            "defaultType": "spot",
            "recvWindow": int(os.getenv("MEXC_RECVWINDOW_MS", "10000")),  # was 5000
            "adjustForTimeDifference": True,
        },
    })
    try:
        ex.load_markets()
        # sync lokale klok met exchange-klok (ccxt berekent offset)
        try:
            ex.load_time_difference()
        except Exception as e2:
            _dbg(f"[MEXC] load_time_difference warn: {e2}")
    except Exception as e:
        _dbg(f"[MEXC] init error: {e}")

    EX_LIVE = ex
    return EX_LIVE

def _qty_for_quote(ex, symbol, quote_amount, price):
    raw = max(float(quote_amount) / float(price), 0.0)
    amt = float(ex.amount_to_precision(symbol, raw))
    m = ex.market(symbol)
    min_amt = float(((m.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    if min_amt and amt < min_amt:
        amt = float(ex.amount_to_precision(symbol, min_amt))
    return amt


def _order_quote_breakdown(ex, symbol, order: dict, side: str):
    """
    Parse ccxt order into:
      avg: average fill price
      filled: base filled
      gross_quote: quote gross (avg*filled or order.cost)
      fee_quote: fees converted to USDT if possible
      net_quote: BUY -> gross+fee ; SELL -> gross-fee
    """
    side = (side or "").lower().strip()
    avg = float(order.get("average") or order.get("price") or 0.0)
    filled = float(order.get("filled") or 0.0)
    gross = float(order.get("cost") or (avg * filled))

    fee_q = 0.0
    trades = order.get("trades") or []
    base_ccy = (ex.market(symbol).get("base") or "").upper()

    # prefer trade-level
    for tr in trades:
        try:
            gross = max(gross, float(tr.get("cost") or gross))
        except Exception:
            pass
        ff = tr.get("fee") or {}
        try:
            cur = str(ff.get("currency", "")).upper()
            val = float(ff.get("cost") or 0.0)
            if val <= 0:
                continue
            if cur in {"USDT", "USD"}:
                fee_q += val
            elif cur == base_ccy and avg > 0:
                fee_q += val * avg
        except Exception:
            pass

    if not trades:
        for f in (order.get("fees") or []):
            try:
                cur = str(f.get("currency", "")).upper()
                val = float(f.get("cost") or 0.0)
                if val <= 0:
                    continue
                if cur in {"USDT", "USD"}:
                    fee_q += val
                elif cur == base_ccy and avg > 0:
                    fee_q += val * avg
            except Exception:
                pass

    net = gross + fee_q if side == "buy" else gross - fee_q
    if avg <= 0.0 and filled > 0.0:
        avg = gross / filled if gross > 0 else 0.0
    return float(avg), float(filled), float(gross), float(fee_q), float(net)

def _prepare_sell_amount(ex, symbol: str, desired_amt: float):
    """
    Bepaalt een veilige SELL-amount met exchange-limits en precisie.
    Retourneert (amt, info) waarbij amt=0.0 betekent: onder minimum (dust).
    """
    m = ex.market(symbol)
    prec = int((m.get("precision") or {}).get("amount") or 6)
    lims = (m.get("limits") or {}).get("amount") or {}
    min_from_limits = float(lims.get("min") or 0.0)
    min_from_precision = (10 ** (-prec)) if prec > 0 else 0.0
    fallback_min = float(os.getenv("FALLBACK_MIN_XRP", "0.01"))
    min_amt = max(min_from_limits, min_from_precision, fallback_min)

    bal = ex.fetch_balance()
    base = (m.get("base") or symbol.split("/")[0]).upper()
    free_amt = float((bal.get(base) or {}).get("free") or 0.0)

    wanted = min(float(desired_amt), free_amt)

    try:
        rounded = float(ex.amount_to_precision(symbol, wanted))
    except Exception:
        rounded = wanted

    ticks = max(0, int(os.getenv("SELL_EPSILON_TICKS", "1")))
    epsilon = ((10 ** (-prec)) if prec > 0 else 0.0) * ticks

    amt = rounded
    if epsilon > 0 and (amt - epsilon) >= min_amt:
        amt = amt - epsilon
        try:
            amt = float(ex.amount_to_precision(symbol, amt))
        except Exception:
            pass

    # Als afronding onder min valt maar free >= min → verkoop min_amt
    if amt < min_amt:
        if free_amt >= min_amt:
            amt = min_amt
            try:
                amt = float(ex.amount_to_precision(symbol, amt))
            except Exception:
                pass

    info = {
        "prec": prec,
        "min_from_limits": min_from_limits,
        "min_from_precision": min_from_precision,
        "fallback_min": fallback_min,
        "min_amt": min_amt,
        "free_amt": free_amt,
        "desired": float(desired_amt),
        "wanted": float(wanted),
        "rounded": float(rounded),
        "epsilon": float(epsilon),
        "final_amt": float(amt),
    }
    if amt <= 0.0 or amt < min_amt:
        return 0.0, info
    return amt, info

def _place_mexc_market(symbol: str, side: str, budget_quote: float, price_hint: float = 0.0):
    ex = _mexc_live()
    side_l = (side or "").lower()
    if side_l not in ("buy", "sell"):
        raise ValueError("side must be 'buy' of 'sell'")

    m = ex.market(symbol)
    min_amt  = float((((m.get("limits") or {}).get("amount") or {}).get("min") or 0.0) or 0.0)
    min_cost = float((((m.get("limits") or {}).get("cost")   or {}).get("min") or 0.0) or 0.0)
    quote_ccy = (m.get("quote") or symbol.split("/")[1]).upper()

    params = {"recvWindow": _recvwin()}

    if side_l == "buy":
        spend = float(budget_quote or 0.0)
        if spend <= 0:
            raise ValueError("budget_quote moet > 0 zijn voor market BUY")

        try:
            bal = ex.fetch_balance(params=params)
            free_q = float(((bal.get("free") or {}).get(quote_ccy)) or 0.0)
        except Exception:
            free_q = spend

        spend = min(spend, free_q)
        if min_cost and spend < min_cost:
            raise ValueError(f"insufficient quote balance: have {free_q}, need >= min_cost {min_cost}")

        try:
            spend_prec = float(ex.price_to_precision(symbol, spend))
        except Exception:
            spend_prec = float(round(spend, 6))
        params["quoteOrderQty"] = spend_prec
        order = ex.create_order(symbol, "market", "buy", None, None, params)

    else:
        if price_hint and budget_quote and budget_quote > 0:
            qty = float(budget_quote / max(price_hint, 1e-12))
        else:
            qty = 0.0
        if qty and qty < min_amt:
            qty = min_amt
        try:
            amount = float(ex.amount_to_precision(symbol, qty)) if qty else None
        except Exception:
            amount = float(round(qty, 6)) if qty else None
        order = ex.create_order(symbol, "market", "sell", amount, None, params)

    return order
def _tpsl_defaults_from_env():
    return {
        "tp_pct": LOCAL_TP_PCT,
        "sl_pct": LOCAL_SL_PCT,
        "use_trail": LOCAL_USE_TRAIL,
        "trail_pct": LOCAL_TRAIL_PCT,
        "arm_at_pct": LOCAL_ARM_AT_PCT,
    }


def _tpsl_from_advisor():
    ap = _advisor_applied(CURRENT_SYMBOL_STR)
    return {
        "tp_pct": float(ap.get("TAKE_PROFIT_PCT", LOCAL_TP_PCT)),
        "sl_pct": float(ap.get("STOP_LOSS_PCT", LOCAL_SL_PCT)),
        "use_trail": bool(ap.get("USE_TRAILING", LOCAL_USE_TRAIL)),
        "trail_pct": float(ap.get("TRAIL_PCT", LOCAL_TRAIL_PCT)),
        "arm_at_pct": float(ap.get("ARM_AT_PCT", LOCAL_ARM_AT_PCT)),
    }


def tpsl_reset():
    tpsl_state.update(_fresh_tpsl())


def tpsl_on_buy(entry: float):
    if not LOCAL_TPSL_ENABLED:
        tpsl_reset()
        return
    try:
        params = (_tpsl_from_advisor() if SYNC_TPSL_FROM_ADVISOR else _tpsl_defaults_from_env())
    except Exception:
        params = _tpsl_defaults_from_env()
    tpsl_state.update(params)
    tpsl_state["active"] = True
    tpsl_state["armed"] = False
    tpsl_state["high"] = entry
    tpsl_state["start_ts"] = time.time()
    _dbg(f"[TPSL] armed for {CURRENT_SYMBOL}: {params}")


def local_tpsl_check(last_price: float):
    if not (LOCAL_TPSL_ENABLED and in_position and tpsl_state["active"] and entry_price > 0):
        return
    now = time.time()
    elapsed = now - float(tpsl_state.get("start_ts") or now)

    e = entry_price
    tp = e * (1.0 + float(tpsl_state["tp_pct"] or 0.0))
    sl = e * (1.0 - float(tpsl_state["sl_pct"] or 0.0))

    allow_sl = (LOCAL_SL_ARM_SEC <= 0) or (elapsed >= LOCAL_SL_ARM_SEC)
    allow_tp = (LOCAL_TP_ARM_SEC <= 0) or (elapsed >= LOCAL_TP_ARM_SEC)

    # TP
    if allow_tp and last_price >= tp > 0:
        _dbg(f"TPSL: TP hit @ {last_price:.6f} (tp={tp:.6f}) {CURRENT_SYMBOL}")
        _do_forced_sell(last_price, "local_tp")
        tpsl_reset()
        return

    # SL
    if allow_sl and last_price <= sl < e:
        _dbg(f"TPSL: SL hit @ {last_price:.6f} (sl={sl:.6f}) {CURRENT_SYMBOL}")
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
                _dbg(f"TPSL: trail ARMED at {last_price:.6f} (arm={arm_level:.6f}) {CURRENT_SYMBOL}")
        else:
            if last_price > tpsl_state["high"]:
                tpsl_state["high"] = last_price
            trail_stop = tpsl_state["high"] * (1.0 - float(tpsl_state["trail_pct"] or 0.0))
            if last_price <= trail_stop:
                _dbg(f"TPSL: TRAIL stop @ {last_price:.6f} (stop={trail_stop:.6f}, high={tpsl_state['high']:.6f}) {CURRENT_SYMBOL}")
                _do_forced_sell(last_price, "local_trail")
                tpsl_reset()
                return


# --------------- Trend ---------------
def _ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    ema = sum(values[:n]) / n
    for v in values[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def trend_ok(price: float, symbol_tv: str = None):
    symbol_tv = symbol_tv or CURRENT_SYMBOL
    try:
        if not USE_TREND_FILTER:
            return True, float("nan")
        n = max(5, int(TREND_MA_LEN))
        ohlcv, src = fetch_ohlcv_any(symbol_tv, timeframe=TREND_TF, limit=n + 20)
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


# --------------- Cooldown & De-dup ---------------
def blocked_by_cooldown() -> bool:
    if MIN_TRADE_COOLDOWN_S <= 0:
        return False
    return (time.time() - last_action_ts) < MIN_TRADE_COOLDOWN_S


def is_duplicate_signal(action: str, source: str, price: float, tf: str, symbol: str) -> bool:
    key = (
        symbol,
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


# --------------- Rehydrate ---------------
def _rehydrate_from_mexc_symbol(symbol: str) -> bool:
    try:
        ex = _mexc_live()
        bal = ex.fetch_balance()
        base = (ex.market(symbol).get("base") or symbol.split("/")[0]).upper()
        tot = float((bal.get(base) or {}).get("total") or 0.0)

        if tot < REHYDRATE_MIN_XRP:
            _dbg(f"[REHYDRATE] {symbol} dust {tot} -> ignore")
            STATE[symbol].update({"in_position": False, "entry_price": 0.0, "pos_amount": 0.0, "pos_quote": 0.0})
            return False

        pos_amt = float(ex.amount_to_precision(symbol, tot))
        entry_est = 0.0

        try:
            since = int((time.time() - 7 * 24 * 3600) * 1000)
            trades = ex.fetch_my_trades(symbol, since=since)
            net_base = 0.0
            cost_q = 0.0
            fee_buy_q = 0.0
            base_ccy = (ex.market(symbol).get("base") or base).upper()
            for t in trades:
                amt = float(t.get("amount") or 0.0)
                pr = float(t.get("price") or 0.0)
                side = (t.get("side") or "").lower()
                if side == "buy":
                    net_base += amt
                    cost_q += amt * pr
                    ff = t.get("fee") or {}
                    try:
                        cur = str(ff.get("currency", "")).upper()
                        val = float(ff.get("cost") or 0.0)
                        if val > 0:
                            if cur in {"USDT", "USD"}: fee_buy_q += val
                            elif cur == base_ccy and pr > 0: fee_buy_q += val * pr
                    except Exception:
                        pass
                elif side == "sell":
                    net_base -= amt
                    cost_q -= amt * pr
                if net_base <= 1e-12:
                    net_base, cost_q, fee_buy_q = 0.0, 0.0, 0.0
            if net_base > 0 and (cost_q + fee_buy_q) > 0:
                entry_est = (cost_q + fee_buy_q) / net_base
        except Exception as e2:
            _dbg(f"[REHYDRATE] trade-based entry calc failed {symbol}: {e2}")

        if entry_est <= 0.0:
            try:
                tk = ex.fetch_ticker(symbol)
                entry_est = float(tk.get("last") or 0.0)
            except Exception:
                entry_est = 0.0

        pos_quote_est = round(entry_est * pos_amt, 6)
        STATE[symbol].update({
            "in_position": True,
            "entry_price": entry_est,
            "entry_ts": time.time(),
            "last_action_ts": time.time(),
            "pos_amount": pos_amt,
            "pos_quote": pos_quote_est,
        })
        _dbg(f"[REHYDRATE] {symbol} amt={pos_amt}, entry≈{entry_est}, pos_quote≈{pos_quote_est}")
        return True
    except Exception as e:
        _dbg(f"[REHYDRATE] failed {symbol}: {e}")
        STATE[symbol].update({"in_position": False, "entry_price": 0.0, "pos_amount": 0.0, "pos_quote": 0.0})
        return False


def _rehydrate_all_symbols():
    if not REHYDRATE_ON_BOOT:
        return
    for sym in TRADEABLE:
        _rehydrate_from_mexc_symbol(sym)


# --------------- Flask ----------------
app = Flask(__name__)

# Landing/health on "/"
@app.route("/", methods=["GET", "HEAD"])
def root():
    _dbg("[ROOT] 200")
    return "OK", 200

@app.route("/health")
def health():
    return "OK", 200

@app.route("/config", methods=["GET"])
def config_view():
    payload = {
        "symbols": {s: {
            "in_position": STATE[s]["in_position"],
            "entry_price": round(STATE[s]["entry_price"], 6),
            "entry_ts": STATE[s]["entry_ts"],
            "last_action_ts": STATE[s]["last_action_ts"],
            "pos_amount": STATE[s]["pos_amount"],
            "pos_quote": STATE[s]["pos_quote"],
            "capital": STATE[s]["capital"],
            "sparen": STATE[s]["sparen"],
            "tpsl": {
                "active": STATE[s]["tpsl"]["active"],
                "tp_pct": STATE[s]["tpsl"]["tp_pct"],
                "sl_pct": STATE[s]["tpsl"]["sl_pct"],
                "use_trail": STATE[s]["tpsl"]["use_trail"],
                "trail_pct": STATE[s]["tpsl"]["trail_pct"],
                "arm_at_pct": STATE[s]["tpsl"]["arm_at_pct"],
                "armed": STATE[s]["tpsl"]["armed"],
                "armed_since": STATE[s]["tpsl"]["start_ts"],
                "high": STATE[s]["tpsl"]["high"],
            }
        } for s in TRADEABLE},
        "cooldown_s": MIN_TRADE_COOLDOWN_S,
        "dedup_s": DEDUP_WINDOW_S,
        "trend_filter": {
            "enabled": USE_TREND_FILTER,
            "mode": TREND_MODE,
            "len": TREND_MA_LEN,
            "type": TREND_MA_TYPE,
            "tf": TREND_TF,
            "slope_lookback": TREND_SLOPE_LOOKBACK,
        },
        "hard_sl_pct": HARD_SL_PCT,
        "hard_sl_arm_sec": HARD_SL_ARM_SEC,
        "max_hold_min": MAX_HOLD_MIN,
        "live_mode": LIVE_MODE,
        "live_exchange": LIVE_EXCHANGE,
        "exchange_source": EXCHANGE_SOURCE,
        "budgets": TRADEABLE,
    }
    return jsonify(payload)

# --- Advisor endpoints ---
@app.route("/advisor", methods=["GET", "POST"])
def advisor_endpoint():
    try:
        if request.method == "GET":
            sym = request.args.get("symbol", CURRENT_SYMBOL_STR)
            return jsonify({"ok": True, "applied": _advisor_applied(sym)})
        data = request.get_json(force=True, silent=True) or {}
        if str(data.get("_action", "")).lower() == "get":
            sym = data.get("symbol", CURRENT_SYMBOL_STR)
            return jsonify({"ok": True, "applied": _advisor_applied(sym)})
        act = str(data.get("action", "")).lower()
        if act not in {"buy", "sell"}:
            return jsonify({"ok": False, "error": "bad_payload"}), 400
        sym = data.get("symbol", CURRENT_SYMBOL_STR)
        return jsonify({"ok": True, "approve": True, "reason": "open", "applied": _advisor_applied(sym)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/advisor/admin/get", methods=["POST"])
def advisor_admin_get():
    if not _advisor_auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    sym = data.get("symbol", CURRENT_SYMBOL_STR)
    return jsonify({"ok": True, "applied": _advisor_applied(sym)})


@app.route("/advisor/admin/set", methods=["POST"])
def advisor_admin_set():
    if not _advisor_auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    sym = (data.get("symbol") or CURRENT_SYMBOL_STR).upper().strip()
    raw = (data.get("values") or data.get("changes") or {k: v for k, v in data.items() if k.lower() not in {"symbol"}})
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


# --------------- Webhook ---------------
@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, entry_price, capital, sparen, last_action_ts, entry_ts, pos_amount, pos_quote, tpsl_state

    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return "Unauthorized", 401

    _dbg(f"/webhook hit ct={request.headers.get('Content-Type')} raw={request.data[:200]!r}")
    data = request.get_json(force=True, silent=True) or {}

    action = str(data.get("action", "")).lower().strip()
    source = str(data.get("source", "unknown")).lower().strip()
    tf = str(data.get("tf", "1m")).lower().strip()

    try:
        price = float(data.get("price", 0) or 0.0)
    except Exception:
        _dbg("bad price in payload")
        return "Bad price", 400
    if price <= 0 or action not in ("buy", "sell"):
        _dbg("bad payload guard (price<=0 of action niet buy/sell)")
        return "Bad payload", 400
    tv_price = price

    # --- Symbool uit payload + guard/normalisatie ---
    ccxt_symbol = extract_symbol_from_payload(data)
    if not ccxt_symbol:
        _dbg("[SYMBOL] missing/unknown in alert -> ignored")
        return "OK", 200
    if ccxt_symbol not in TRADEABLE:
        _dbg(f"[SYMBOL] ignored {ccxt_symbol} (not in TRADEABLE)")
        return "OK", 200

    # TV guard: als TV een andere pair meestuurt → negeren
    tv_symbol_hdr = str(data.get("tv_symbol") or data.get("ticker") or data.get("symbol") or "").upper().strip()
    if LIVE_EXCHANGE == "mexc" and tv_symbol_hdr:
        norm_hdr = _normalize_tv_symbol(tv_symbol_hdr)
        if norm_hdr and norm_hdr != ccxt_symbol:
            _dbg(f"[TVGUARD] ignored alert tv_symbol={tv_symbol_hdr} (norm={norm_hdr}) for {ccxt_symbol}")
            return "OK", 200

    # Bind naar dit symbool
    bind(ccxt_symbol)
    SYMBOL_TV = ccxt_symbol  # e.g., WLFI/USDT
    SYMBOL_STR = CURRENT_SYMBOL_STR
    TRADE_BUDGET = get_budget(SYMBOL_TV)

    # De-dup & cooldown
    if is_duplicate_signal(action, source, price, tf, SYMBOL_TV):
        _dbg(f"dedup ignored key={(SYMBOL_TV, action, source, round(price,4), tf)} win={DEDUP_WINDOW_S}s")
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

    # Ghost-guard
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        if in_position and (pos_amount <= 1e-12 or entry_price <= 0):
            _dbg(f"[GUARD] ghost detected: in_position={in_position}, pos_amount={pos_amount}, entry_price={entry_price}")
            in_position = False
            entry_price = 0.0
            pos_amount = 0.0
            pos_quote = 0.0
            tpsl_reset()

    timestamp = now_str()

    # === BUY ===
    if action == "buy":
        # Lock per symbool: beschermt tegen bind()/commit() van idle_worker
        lock = SYMBOL_LOCKS.setdefault(SYMBOL_TV, Lock())
        with lock:
            # 0) PREBUY reality check (balans + open BUY orders)
            if LIVE_MODE and LIVE_EXCHANGE == "mexc":
                try:
                    ex = _mexc_live()
                    bal = ex.fetch_balance(params={"recvWindow": _recvwin()})
                    base = sym_pair.split("/")[0]
                    free = float(((bal.get("free") or {}).get(base)) or 0.0)
                    used = float(((bal.get("used") or {}).get(base)) or 0.0)
                    total = float(((bal.get("total") or {}).get(base)) or (free + used))

                    try:
                        m = ex.market(sym_pair)
                        min_amt = float((((m.get("limits") or {}).get("amount") or {}).get("min") or 0.0) or 0.0)
                    except Exception:
                        min_amt = 0.0

                    if total > max(min_amt, REHYDRATE_MIN_XRP):
                        if not in_position or pos_amount < total * 0.999:
                            _dbg(f"[PREBUY] exchange base_total={total} > min → mark in_position & skip")
                        in_position = True
                        pos_amount  = round(total, 6)
                        if entry_price > 0 and pos_quote <= 0:
                            pos_quote = round(entry_price * pos_amount, 6)
                        # schrijf terug en afsluiten
                        pos.update({"in_position": in_position, "pos_amount": pos_amount, "pos_quote": pos_quote})
                        commit(SYMBOL_TV)
                        return "OK", 200

                    # 0b) Open BUY-order aanwezig? → skip
                    try:
                        oo = ex.fetch_open_orders(sym_pair, params={"recvWindow": _recvwin()})
                        has_open_buy = False
                        for o in (oo or []):
                            side = (o.get("side") or o.get("type") or "").lower()
                            status = (o.get("status") or "open").lower()
                            if side == "buy" and status in ("open", "new", "partially_filled"):
                                has_open_buy = True
                                break
                        if has_open_buy:
                            _dbg("[PREBUY] open BUY order bestaat al → skip")
                            commit(SYMBOL_TV)
                            return "OK", 200
                    except Exception:
                        pass

                except Exception as e:
                    _dbg(f"[PREBUY] balance/open_orders check failed: {e}")

            # 1) Ghost-snapshot guard
            if in_position and (pos_amount <= REHYDRATE_MIN_XRP or entry_price <= 0):
                _dbg(f"[GUARD] ghost detected: in_position={in_position}, pos_amount={pos_amount}, entry_price={entry_price}")
                commit(SYMBOL_TV)
                return "OK", 200

            # Al in positie? → geen dubbele buy
            if in_position:
                _dbg(f"buy ignored: already in_position at entry={entry_price}")
                commit(SYMBOL_TV)
                return "OK", 200

            # 2) Cooldown
            now_ts = time.time()
            if now_ts - last_action_ts < MIN_TRADE_COOLDOWN_S:
                _dbg(f"buy ignored: cooldown {MIN_TRADE_COOLDOWN_S}s not elapsed")
                commit(SYMBOL_TV)
                return "OK", 200

            # 3) Trend filter
            ok, ma = trend_ok(price, sym_pair)
            _dbg(f"trend_ok={ok} ma={ma:.6f} price={price:.6f}")
            if not ok:
                _dbg("blocked by trend filter")
                commit(SYMBOL_TV)
                return "OK", 200

            # 4) Order plaatsen (MEXC: BUY via quoteOrderQty)
            if LIVE_MODE and LIVE_EXCHANGE == "mexc":
                try:
                    order = _place_mexc_market(sym_pair, "buy", get_budget(SYMBOL_TV), price)
                    ex = _mexc_live()
                    avg2, filled2, gross_q, fee_q, net_in = _order_quote_breakdown(ex, sym_pair, order, "buy")

                    price      = float(avg2 or price or 0.0)
                    pos_amount = float(filled2 or 0.0)
                    pos_quote  = float(net_in or 0.0)
                    if pos_quote <= 0 and pos_amount > 0:
                        pos_quote = round(price * pos_amount, 6)

                    _dbg(f"[LIVE] MEXC BUY {sym_pair} id={order.get('id')} filled={pos_amount} avg={price} gross={gross_q} fee_q={fee_q} pos_quote={pos_quote}")

                    # 0-fill/ghost guard
                    if pos_amount <= 0 or pos_quote <= 0:
                        _dbg("[LIVE] BUY zero-fill detected → skip marking position")
                        commit(SYMBOL_TV)
                        return "OK", 200

                except Exception as e:
                    msg = str(e).lower()
                    # Netjes skippen i.p.v. 500 bij te weinig USDT / min notional
                    if "insufficient" in msg or "min cost" in msg or "min_cost" in msg:
                        _dbg(f"[LIVE] BUY skipped: {e}")
                        commit(SYMBOL_TV)
                        return "OK", 200
                    _dbg(f"[LIVE] MEXC BUY failed: {e}")
                    commit(SYMBOL_TV)
                    return "LIVE BUY failed", 500
            else:
                pos_amount = float(get_budget(SYMBOL_TV)) / float(price or 1e-12)
                pos_quote  = float(get_budget(SYMBOL_TV))

            # 5) State bijwerken
            entry_price    = float(price if pos_amount > 0 else 0.0)
            in_position    = bool(pos_amount and pos_amount > REHYDRATE_MIN_XRP)
            last_action_ts = now_ts
            entry_ts       = now_ts

            pos.update({
                "in_position": in_position,
                "pos_amount": round(pos_amount, 6),
                "pos_quote": round(pos_quote, 6),
                "entry_price": entry_price,
                "entry_ts": entry_ts,
                "last_action_ts": last_action_ts,
            })
            tpsl_on_buy(entry_price)

            # 6) Telegram + logging (safe)
            tv_show  = float(tv_price or price or 0.0)
            base_for_delta = tv_show if tv_show else price
            delta_txt = f"  (Δ {(price / base_for_delta - 1) * 100:+.2f}%)" if (base_for_delta and price) else ""
            source_h  = ht(source)
            advisor_h = ht(advisor_reason or "n/a")
            tf_h      = ht(tf)
            ts        = timestamp or now_str()

            send_tg(
                f"""🟢 <b>[{sym_pair}] AANKOOP</b>
📹 TV prijs: ${tv_show:.6f}
🎯 Fill (MEXC): ${float(price or 0.0):.6f}{delta_txt}
🧠 Signaalbron: {source_h} | {advisor_h}
🕒 TF: {tf_h}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{get_budget(SYMBOL_TV):.2f}
🔗 Tijd: {ts}"""
            )
            log_trade("buy", float(price or 0.0), 0.0, source, tf, sym_pair)
            commit(SYMBOL_TV)
            return "OK", 200


    # === SELL ===
    if action == "sell":
        lock = SYMBOL_LOCKS.setdefault(SYMBOL_TV, Lock())
        with lock:
            if not in_position:
                _dbg("sell ignored: not in_position")
                commit(SYMBOL_TV)
                return "OK", 200

            tv_sell = float(tv_price or 0.0)
            winst_bedrag = 0.0
            display_fill = tv_sell

            # snapshots zodat idle_worker ons niet beïnvloedt
            pos_amount0 = float(pos_amount)
            pos_quote0  = float(pos_quote)

            if LIVE_MODE and LIVE_EXCHANGE == "mexc":
                try:
                    ex = _mexc_live()

                    # --- EXCHANGE RECONCILE: klopt onze positie met de beurs? ---
                    try:
                        bal  = ex.fetch_balance()
                        base = SYMBOL_TV.split('/')[0]
                        free_base  = float(((bal.get('free')  or {}).get(base)) or 0.0)
                        total_base = float(((bal.get('total') or {}).get(base)) or 0.0)
                        if total_base <= 1e-12:
                            _dbg(f"[RECON] {SYMBOL_TV} exchange total=0 → flatten local state")
                            in_position = False
                            pos_amount  = 0.0
                            pos_quote   = 0.0
                            entry_price = 0.0
                            commit(SYMBOL_TV)
                            return "OK", 200
                    except Exception as e_bal:
                        _dbg(f"[RECON] fetch_balance failed: {e_bal}")

                    # veilige sell-amount bepalen (met fallback als /account faalt)
                    try:
                        amt, info = _prepare_sell_amount(ex, SYMBOL_TV, pos_amount0)
                        _dbg(f"[SELL PREP] {SYMBOL_TV} info={info}")
                    except Exception as e_prep:
                        _dbg(f"[SELL PREP] balance fetch failed: {e_prep} → fallback to snapshot")
                        # bepaal min_amt uit markets; val terug op pos_amount0
                        min_amt = 0.0
                        try:
                            m = ex.market(SYMBOL_TV)
                            min_amt = float((((m.get('limits') or {}).get('amount') or {}).get('min') or 0.0) or 0.0)
                        except Exception:
                            pass
                        raw_amt = max(float(pos_amount0), float(min_amt))
                        try:
                            amt = float(ex.amount_to_precision(SYMBOL_TV, raw_amt))
                        except Exception:
                            amt = float(raw_amt)
                        info = {"min_amt": min_amt, "free_amt": None, "desired": pos_amount0, "final_amt": amt}

                    if amt <= 0.0:
                        _dbg(f"[LIVE] SELL skipped: <min_amt (pos={pos_amount0}, free={info.get('free_amt')}) → dust")

                        # Als de beurs free=0 meldt, check 1x total; zo ja=0 → flatten lokaal
                        try:
                            if float(info.get("free_amt") or 0.0) <= 1e-12:
                                bal  = ex.fetch_balance()
                                base = SYMBOL_TV.split('/')[0]
                                total_base = float(((bal.get('total') or {}).get(base)) or 0.0)
                                if total_base <= 1e-12:
                                    _dbg(f"[RECON] {SYMBOL_TV} total=0 bij amt<=0 → flatten local state")
                                    in_position = False
                                    entry_price = 0.0
                                    pos_amount  = 0.0
                                    pos_quote   = 0.0
                                    send_tg(
                                        f"📄 <b>[{SYMBOL_TV}] VERKOOP</b>\n"
                                        f"🧠 Reden: reconcile_flatten (exchange total=0)\n"
                                        f"🕒 Tijd: {now_str()}"
                                    )
                                    log_trade("sell", tv_sell, 0.0, source, tf, SYMBOL_TV)
                                    commit(SYMBOL_TV)
                                    return "OK", 200
                        except Exception:
                            pass

                        if pos_amount0 <= max(info.get("min_amt", 0.0), REHYDRATE_MIN_XRP):
                            in_position = False
                            entry_price = 0.0
                            pos_amount  = 0.0
                            pos_quote   = 0.0
                            send_tg(
                                f"📄 <b>[{SYMBOL_TV}] VERKOOP</b>\n"
                                f"🧠 Reden: dust_below_exchange_min\n"
                                f"🪙 Restpositie intern gesloten (dust)\n"
                                f"🕒 Tijd: {now_str()}"
                            )
                            log_trade("sell", tv_sell, 0.0, source, tf, SYMBOL_TV)
                            commit(SYMBOL_TV)
                        else:
                            _dbg("[LIVE] leave as dust; no order placed")
                        return "OK", 200

                    # plaats order
                    order = ex.create_order(SYMBOL_TV, "market", "sell", amt)

                    # FAST_FILL: sla fetch loops over en vul minimale velden
                    if FAST_FILL:
                        order.setdefault("amount", amt)
                        order.setdefault("filled", amt)
                        order.setdefault("price", tv_sell)
                        order.setdefault("average", tv_sell)
                        order.setdefault("cost", amt * max(tv_sell, 1e-12))
                    else:
                        # ---- order opvullen ----
                        oid = order.get("id")
                        if oid:
                            for _ in range(3):
                                try:
                                    time.sleep(0.25)
                                    o2 = ex.fetch_order(oid, SYMBOL_TV)
                                    if o2:
                                        order = {**order, **o2}
                                    if not order.get("trades"):
                                        try:
                                            trs = ex.fetch_order_trades(oid, SYMBOL_TV)
                                            if trs:
                                                order["trades"] = trs
                                        except Exception:
                                            pass
                                    if float(order.get("filled") or 0) > 0 or float(order.get("cost") or 0) > 0:
                                        break
                                except Exception:
                                    pass

                except Exception as e_sell:
                    msg = str(e_sell).lower()

                    # Specifiek: oversold/insufficient → flatten i.p.v. blijven hangen
                    if "oversold" in msg or "insufficient" in msg:
                        _dbg("[LIVE] MEXC SELL oversold/insufficient → flatten local state")
                        in_position = False
                        pos_amount  = 0.0
                        pos_quote   = 0.0
                        entry_price = 0.0
                        commit(SYMBOL_TV)
                        return "OK", 200

                    # één retry met minimaal toelaatbare amount
                    if "minimum amount" in msg or "precision" in msg or "min" in msg:
                        min_retry = info.get("min_amt", 0.0) if 'info' in locals() else 0.0
                        try:
                            free_amt = float(info.get("free_amt", 0.0)) if 'info' in locals() else 0.0
                        except Exception:
                            free_amt = 0.0
                        if free_amt >= min_retry and min_retry > 0:
                            try:
                                retry_amt = float(ex.amount_to_precision(SYMBOL_TV, min_retry))
                                _dbg(f"[SELL RETRY] using min_amt={retry_amt}")
                                order = ex.create_order(SYMBOL_TV, "market", "sell", retry_amt)
                            except Exception as e2:
                                _dbg(f"[LIVE] SELL retry failed: {e2}")
                                commit(SYMBOL_TV)
                                return "OK", 200
                    else:
                        _dbg(f"[LIVE] MEXC SELL error: {e_sell}")
                        commit(SYMBOL_TV)
                        return "OK", 200

                # ---- order opvullen en PnL berekenen ----
                avg2, filled2, gross_q, fee_q, net_out = _order_quote_breakdown(ex, SYMBOL_TV, order, "sell")
                if float(filled2 or 0.0) <= 0.0:
                    _dbg("[LIVE] SELL zero-fill detected → no state change, no TG")
                    commit(SYMBOL_TV)
                    return "OK", 200

                display_fill = float(avg2 or tv_sell or 0.0)
                filled       = float(filled2 or 0.0)

                inleg_quote = float(pos_quote0)
                if pos_amount0 > 0 and filled < pos_amount0:
                    inleg_quote = inleg_quote * (filled / pos_amount0)

                revenue_net  = float(net_out or 0.0)
                winst_bedrag = round(revenue_net - inleg_quote, 2)

                _dbg(f"[LIVE] MEXC SELL {SYMBOL_TV} id={order.get('id')} filled={filled} avg={display_fill} net_out={revenue_net} pnl={winst_bedrag}")

            else:
                # Simulatie-pad
                sim_price = float(display_fill or tv_sell or 0.0)
                if entry_price > 0 and sim_price > 0:
                    verkoop_bedrag = sim_price * get_budget(SYMBOL_TV) / entry_price
                    winst_bedrag   = round(verkoop_bedrag - get_budget(SYMBOL_TV), 2)

            # Boekhouding
            if winst_bedrag > 0:
                sparen  += SAVINGS_SPLIT * winst_bedrag
                capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
            else:
                capital += winst_bedrag

            if capital < START_CAPITAL:
                tekort = START_CAPITAL - capital
                if sparen >= tekort:
                    sparen  -= tekort
                    capital += tekort

            # Positie bijwerken met snapshot (partial/full)
            if LIVE_MODE and LIVE_EXCHANGE == "mexc":
                try:
                    filled  # type: ignore
                except NameError:
                    filled = pos_amount0
                if pos_amount0 > 0 and filled < pos_amount0:
                    factor      = (pos_amount0 - filled) / pos_amount0
                    pos_quote   = round(pos_quote0 * factor, 6)
                    pos_amount  = round(pos_amount0 - filled, 6)
                    in_position = pos_amount > REHYDRATE_MIN_XRP
                    entry_price = (pos_quote / pos_amount) if in_position and pos_amount > 0 else 0.0
                else:
                    pos_amount  = 0.0
                    pos_quote   = 0.0
                    in_position = False
                    entry_price = 0.0
            else:
                in_position = False
                pos_amount  = 0.0
                pos_quote   = 0.0
                entry_price = 0.0

            last_action_ts = time.time()
            if LOCAL_TPSL_ENABLED:
                tpsl_reset()

            base_for_delta = tv_sell if tv_sell else display_fill
            delta_txt = f"  (Δ {(display_fill / base_for_delta - 1) * 100:+.2f}%)" if base_for_delta else ""
            resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
            rest_txt  = f"\n🪙 Resterend: {pos_amount:.4f} @ ~${entry_price:.6f}" if in_position else ""

            # Safe vars voor bericht
            tv_show  = float(tv_price or display_fill or 0.0)
            source_h = ht(source)
            advisor_h= ht(advisor_reason or "n/a")
            tf_h     = ht(tf)
            ts       = timestamp or now_str()

            send_tg(
                f"""📄 <b>[{SYMBOL_TV}] VERKOOP</b>
📹 TV prijs: ${tv_show:.6f}
🎯 Fill (MEXC): ${display_fill:.6f}{delta_txt}
🧠 Signaalbron: {source_h} | {advisor_h}
🕒 TF: {tf_h}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 {resultaat}: €{winst_bedrag:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{get_budget(SYMBOL_TV):.2f}
🔗 Tijd: {ts}{rest_txt}"""
            )
            log_trade("sell", display_fill, winst_bedrag, source, tf, SYMBOL_TV)
            commit(SYMBOL_TV)
            return "OK", 200

# --------------- Forced exits ---------------
def _do_forced_sell(price: float, reason: str, source: str = "forced_exit", tf: str = "1m") -> bool:
    global in_position, entry_price, capital, sparen, last_action_ts, pos_amount, pos_quote
    if not in_position and not (LIVE_MODE and LIVE_EXCHANGE == "mexc" and pos_amount > 1e-12):
        return False

    SYMBOL_TV = CURRENT_SYMBOL
    winst_bedrag = 0.0
    display_fill = float(price or 0.0)
    amt_before   = float(pos_amount)
    quote_before = float(pos_quote)

    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            ex = _mexc_live()
            amt, info = _prepare_sell_amount(ex, SYMBOL_TV, amt_before)
            _dbg(f"[FORCED SELL PREP] {SYMBOL_TV} info={info}")

            free_amt = float(info.get("free_amt") or 0.0)
            min_amt  = float(info.get("min_amt")  or 0.0)

            # Geen vrij saldo op de beurs → interne reconcile en stoppen
            if free_amt <= 0.0:
                _dbg(f"[LIVE] FORCED SELL reconcile: exchange free=0 → sluit intern positie")
                in_position = False
                entry_price = 0.0
                pos_amount  = 0.0
                pos_quote   = 0.0
                commit(SYMBOL_TV)
                return False

            # Vrij saldo onder minimum → hoogstens 1x/min loggen, niet blijven spammen
            if free_amt < max(min_amt, REHYDRATE_MIN_XRP):
                t_last = FORCED_WARN_TS.get(SYMBOL_TV, 0.0)
                if time.time() - t_last >= FORCED_WARN_COOLDOWN_S:
                    _dbg(f"[LIVE] FORCED SELL skipped: free<{min_amt} (free={free_amt}) → dust")
                    FORCED_WARN_TS[SYMBOL_TV] = time.time()
                return False

            # Plaats order met veilige amount (amt komt al uit _prepare_sell_amount)
            order = ex.create_order(SYMBOL_TV, "market", "sell", amt)

        except Exception as e_sell:
            msg_low = str(e_sell).lower()
            # MEXC 'Oversold' -> positie bestaat niet meer op de beurs: sluit intern en stop
            if "oversold" in msg_low or "code\":30005" in msg_low:
                _dbg(f"[LIVE] FORCED SELL reconcile on Oversold → sluit intern positie")
                in_position = False
                entry_price = 0.0
                pos_amount  = 0.0
                pos_quote   = 0.0
                commit(SYMBOL_TV)
                return False

            # Eén retry met min_amt indien applicable
            if "minimum amount" in msg_low or "precision" in msg_low or "min" in msg_low:
                try:
                    ex = _mexc_live()
                    min_retry = float(info.get("min_amt", 0.0))
                    free_amt  = float(info.get("free_amt", 0.0))
                    if free_amt >= min_retry and min_retry > 0:
                        retry_amt = float(ex.amount_to_precision(SYMBOL_TV, min_retry))
                        _dbg(f"[FORCED SELL RETRY] using min_amt={retry_amt}")
                        order = ex.create_order(SYMBOL_TV, "market", "sell", retry_amt)
                    else:
                        _dbg(f"[LIVE] FORCED SELL retry skipped: free<{min_retry}")
                        return False
                except Exception as e2:
                    _dbg(f"[LIVE] FORCED SELL retry failed: {e2}")
                    return False
            else:
                _dbg(f"[LIVE] MEXC FORCED SELL error: {e_sell}")
                return False

        # ---- order opvullen en PnL berekenen ----
        oid = order.get("id")
        if oid:
            for _ in range(3):
                try:
                    time.sleep(0.25)
                    o2 = ex.fetch_order(oid, SYMBOL_TV)
                    if o2:
                        order = {**order, **o2}
                    if not order.get("trades"):
                        try:
                            trs = ex.fetch_order_trades(oid, SYMBOL_TV)
                            if trs:
                                order["trades"] = trs
                        except Exception:
                            pass
                    if float(order.get("filled") or 0) > 0 or float(order.get("cost") or 0) > 0:
                        break
                except Exception:
                    pass

        avg2, filled2, gross_q, fee_q, net_out = _order_quote_breakdown(ex, SYMBOL_TV, order, "sell")
        display_fill = float(avg2)
        filled       = float(filled2)

        if amt_before > 0 and filled < amt_before:
            quote_before = quote_before * (filled / amt_before)

        revenue_net  = float(net_out)
        winst_bedrag = round(revenue_net - quote_before, 2)
        _dbg(f"[LIVE] MEXC FORCED SELL {SYMBOL_TV} id={order.get('id')} filled={filled} avg={display_fill} net_out={revenue_net} pnl={winst_bedrag}")

    else:
        if entry_price > 0:
            verkoop_bedrag = price * get_budget(SYMBOL_TV) / entry_price
            winst_bedrag   = round(verkoop_bedrag - get_budget(SYMBOL_TV), 2)

    # Boekhouding
    if winst_bedrag > 0:
        sparen  += SAVINGS_SPLIT * winst_bedrag
        capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
    else:
        capital += winst_bedrag

    if capital < START_CAPITAL:
        tekort = START_CAPITAL - capital
        if sparen >= tekort:
            sparen  -= tekort
            capital += tekort

    # Positie bijwerken
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            filled  # type: ignore
        except NameError:
            filled = amt_before
        if amt_before > 0 and filled < amt_before:
            sold_quote = round(quote_before, 6)
            pos_quote  = round(max(0.0, pos_quote - sold_quote), 6)
            pos_amount = round(max(0.0, amt_before - filled), 6)
            in_position = pos_amount > REHYDRATE_MIN_XRP
            entry_price = (pos_quote / pos_amount) if in_position and pos_amount > 0 else 0.0
        else:
            pos_amount  = 0.0
            pos_quote   = 0.0
            in_position = False
            entry_price = 0.0
    else:
        in_position = False
        pos_amount  = 0.0
        pos_quote   = 0.0
        entry_price = 0.0

    last_action_ts = time.time()
    if LOCAL_TPSL_ENABLED:
        tpsl_reset()

    resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
    send_tg(
        f"""🚨 <b>[{SYMBOL_TV}] FORCED SELL</b>
📹 Verkoopprijs: ${display_fill:.6f}
🧠 Reden: {reason}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 {resultaat}: €{winst_bedrag:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{get_budget(SYMBOL_TV):.2f}
🔗 Tijd: {now_str()}"""
    )
    log_trade("sell_forced", display_fill, winst_bedrag, source, tf, SYMBOL_TV)
    commit(SYMBOL_TV)
    return True

def forced_exit_check(symbol_tv: str, last_price: float | None = None):
    lock = SYMBOL_LOCKS.setdefault(symbol_tv, Lock())
    with lock:
        bind(symbol_tv)
        if not in_position or entry_price <= 0:
            commit(symbol_tv)
            return
        if last_price is None:
            try:
                last_price, src = fetch_last_price_any(symbol_tv)
            except Exception as e:
                print(f"[PRICE] fetch fail all: {e}")
                commit(symbol_tv)
                return
        # Hard SL (na arm)
        if HARD_SL_PCT > 0 and entry_ts > 0 and (time.time() - entry_ts) >= HARD_SL_ARM_SEC:
            if last_price <= entry_price * (1.0 - HARD_SL_PCT):
                _do_forced_sell(last_price, f"hard_sl_{HARD_SL_PCT:.3%}")
                commit(symbol_tv)
                return
        # Max hold
        if MAX_HOLD_MIN > 0 and entry_ts > 0 and (time.time() - entry_ts) >= MAX_HOLD_MIN * 60:
            _do_forced_sell(last_price, f"max_hold_{MAX_HOLD_MIN}m")
            commit(symbol_tv)
            return
        # Lokale TPSL
        local_tpsl_check(last_price)
        commit(symbol_tv)

# --------------- Reports ---------------
@app.route("/report/daily", methods=["GET"])
def report_daily():
    today = datetime.now().date()
    trades_today = [t for t in trade_log if datetime.strptime(t["timestamp"], "%d-%m-%Y %H:%M:%S").date() == today]
    total_pnl = sum(t.get("winst", 0.0) for t in trades_today)
    return jsonify({"trades_vandaag": trades_today, "pnl_vandaag": round(total_pnl, 2)})


@app.route("/report/weekly", methods=["GET"])
def report_weekly():
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    trades = [
        t for t in trade_log
        if week_ago <= datetime.strptime(t["timestamp"], "%d-%m-%Y %H:%M:%S") <= now
    ]
    total_pnl = sum(t.get("winst", 0.0) for t in trades)
    return jsonify({"trades_week": trades, "pnl_week": round(total_pnl, 2)})


# --------------- Idle worker ---------------
_last_price_cache: Dict[str, Dict[str, Any]] = {}


def idle_worker():
    while True:
        time.sleep(PRICE_POLL_S)
        try:
            for sym in TRADEABLE:
                try:
                    last, src = fetch_last_price_any(sym)
                    _last_price_cache.setdefault(sym, {})["price"] = last
                    _last_price_cache[sym]["src"] = src
                    _last_price_cache[sym]["ts"] = time.time()
                    forced_exit_check(sym, last)
                except Exception as e2:
                    print(f"[IDLE] {sym} error: {e2}")
                    continue
        except Exception as e:
            print(f"[IDLE] loop error: {e}")
            continue

# --------------- Main ---------------
if __name__ == "__main__":
    # advisor store
    try:
        _advisor_load()
    except Exception as e:
        _dbg(f"[ADVISOR] _advisor_load() failed: {e}")

    # load trades
    try:
        load_trades()
    except Exception as e:
        _dbg(f"[TRADES] load_trades() failed: {e}")

    # rehydrate
    try:
        _rehydrate_all_symbols()
    except Exception as e:
        _dbg(f"[REHYDRATE] all symbols failed: {e}")

    # log effective conf from env (zichtbaar in logs na deploy)
    try:
        rw = os.getenv("MEXC_RECV_WINDOW", "5000")
        to = os.getenv("CCXT_TIMEOUT_MS", "7000")
        _dbg(f"[CONF] MEXC_RECV_WINDOW={rw} CCXT_TIMEOUT_MS={to}")
    except Exception:
        pass

    # warm-up: cache de MEXC client zodat de eerste order geen extra latency heeft
    def _warmup():
        try:
            _mexc_live()
            _dbg("[WARMUP] MEXC client ready")
        except Exception as e:
            _dbg(f"[WARMUP] MEXC failed: {e}")

    warmup_thread = Thread(target=_warmup, daemon=True)
    warmup_thread.start()

    # start idle thread
    idle_thread = Thread(target=idle_worker, daemon=True)
    idle_thread.start()

    _dbg(f"✅ Webhook server op http://0.0.0.0:{PORT}/webhook — symbols: {list(TRADEABLE.keys())}")
    app.run(host="0.0.0.0", port=PORT, debug=False)


