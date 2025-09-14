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

# Webserver poort (Render levert PORT via env)
PORT = env_int("PORT", 10000)


# -----------------------
# Basis & symbol
# -----------------------
SYMBOL_TV = "XRP/USDT"  # ccxt notatie
SYMBOL_STR = "XRPUSDT"  # string voor advisor/logs

START_CAPITAL = float(os.getenv("START_CAPITAL", "500"))

# === Multi-pair setup ===
TRADEABLE = {           # budget per paar (USDT)
    "XRP/USDT": float(os.getenv("BUDGET_XRP", "500")),
    "WLFI/USDT": float(os.getenv("BUDGET_WLFI", "500")),
}

# Huidige 'actieve' symbol in context (wordt per webhook gezet)
CURRENT_SYMBOL = "XRP/USDT"

def _sym_str(sym: str) -> str:
    return (sym or "XRP/USDT").replace("/", "").upper()

CURRENT_SYMBOL_STR = _sym_str(CURRENT_SYMBOL)

# Per-paar runtime state
STATE = {
    sym: {
        "in_position": False,
        "entry_price": 0.0,
        "entry_ts": 0.0,
        "last_action_ts": 0.0,
        "pos_amount": 0.0,
        "pos_quote": 0.0,
    } for sym in TRADEABLE
}

def get_budget(sym: str) -> float:
    return float(TRADEABLE.get(sym, float(os.getenv("START_CAPITAL", "500"))))

def parse_symbol_from_tv(s):
    # TV kan 'XRPUSDT' of 'MEXC:XRPUSDT' sturen
    if not s:
        return CURRENT_SYMBOL
    s = str(s).upper().replace("MEXC:", "").replace("/", "").strip()
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT"
    return s

def bind(sym: str):
    # Laad STATE[sym] -> module globals
    global CURRENT_SYMBOL, CURRENT_SYMBOL_STR
    global in_position, entry_price, entry_ts, last_action_ts, pos_amount, pos_quote
    CURRENT_SYMBOL = sym
    CURRENT_SYMBOL_STR = _sym_str(sym)
    S = STATE[sym]
    in_position    = bool(S["in_position"])
    entry_price    = float(S["entry_price"])
    entry_ts       = float(S["entry_ts"])
    last_action_ts = float(S["last_action_ts"])
    pos_amount     = float(S["pos_amount"])
    pos_quote      = float(S["pos_quote"])

def commit(sym: str):
    # Schrijf module globals -> STATE[sym]
    STATE[sym].update({
        "in_position":    bool(in_position),
        "entry_price":    float(entry_price),
        "entry_ts":       float(entry_ts),
        "last_action_ts": float(last_action_ts),
        "pos_amount":     float(pos_amount),
        "pos_quote":      float(pos_quote),
    })
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
    """Stuur een Telegram-bericht met retries + nette afhandeling van rate limits/timeouts."""
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
                # Rate limit: wacht volgens header of json parameters
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
            time.sleep(1 + attempt)  # 1s, 2s backoff
        except Exception as e:
            print(f"[TG ERROR] {e}", flush=True)
            break

    return False


def advisor_allows(action: str, price: float, source: str, tf: str) -> Tuple[bool, str]:
    """Vraag Advisor (lokaal of extern). Fallback = toestaan."""
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


# --- LIVE order helpers ------------------------------------------------------

def _place_mexc_market(symbol: str, side: str, quote_amount_usd: float, ref_price: float):
    ex = _mexc_live()
    amount = _qty_for_quote(ex, symbol, quote_amount_usd, ref_price)
    if amount <= 0:
        raise RuntimeError("Calculated amount <= 0")
    order = ex.create_order(symbol, "market", side, amount)
    oid = order.get("id")
    if oid:
        for _ in range(2):
            try:
                time.sleep(0.25)
                o2 = ex.fetch_order(oid, symbol)
                if o2:
                    order = {**order, **o2}
                    break
            except Exception:
                pass
    avg = order.get("average") or order.get("price") or ref_price
    filled = order.get("filled") or amount
    return float(avg), float(filled), order


def _order_quote_breakdown(ex, symbol, order: dict, side: str):
    """
    Haal ccxt-order uiteen in:
      avg         : gemiddelde fillprijs
      filled      : gevulde hoeveelheid (base, XRP)
      gross_quote : bruto USDT (avg * filled of order.cost)
      fee_quote   : fees omgerekend naar USDT (trade-fee heeft voorrang)
      net_quote   : BUY = gross + fee ; SELL = gross - fee
    """
    side = (side or "").lower().strip()
    avg = float(order.get("average") or order.get("price") or 0.0)
    filled = float(order.get("filled") or 0.0)
    gross = float(order.get("cost") or (avg * filled))

    fee_q = 0.0
    trades = order.get("trades") or []
    base_ccy = (ex.market(symbol).get("base") or "").upper()

    # Voorkeur: trade-level fees (voorkomt dubbel tellen)
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
                fee_q += val * avg   # base-fee → quote (USDT)
            # MX/overige fee-tokens negeren we voor PnL (tenzij je later een rate toevoegt)
        except Exception:
            pass

    # Fallback: order-level fees als er geen trades zijn
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

    # Safety voor avg
    if avg <= 0.0 and filled > 0.0:
        avg = gross / filled if gross > 0 else 0.0

    return float(avg), float(fee_q), float(gross), float(fee_q), float(net)


def _order_quote_breakdown(ex, symbol, order: dict, side: str):
    """
    Parse ccxt order into:
      avg: average fill price
      filled: base filled
      gross_quote: quote gross (avg*filled or order.cost)
      fee_quote: fees converted to quote (USDT) if possible
      net_quote: BUY -> gross+fee ; SELL -> gross-fee
    """
    side = (side or "").lower().strip()
    avg = float(order.get("average") or order.get("price") or 0.0)
    filled = float(order.get("filled") or 0.0)
    gross = float(order.get("cost") or (avg * filled))

    fee_q = 0.0
    # prefer trade fees
    trades = order.get("trades") or []
    base_ccy = (ex.market(symbol).get("base") or "").upper()
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


    ex = _mexc_live()
    symbol = SYMBOL_TV
    amount = _qty_for_quote(ex, symbol, quote_amount_usd, ref_price)
    if amount <= 0:
        raise RuntimeError("Calculated amount <= 0")
    order = ex.create_order(symbol, "market", side, amount)
    oid = order.get("id")
    if oid:
        for _ in range(2):
            try:
                time.sleep(0.25)
                o2 = ex.fetch_order(oid, symbol)
                if o2:
                    order = {**order, **o2}
                    break
            except Exception:
                pass
    avg = order.get("average") or order.get("price") or ref_price
    filled = order.get("filled") or amount
    return float(avg), float(filled), order

# Rehydrate live positie (spot)

def _rehydrate_from_mexc_symbol(symbol: str) -> bool:
    """Vul STATE[symbol] vanuit live MEXC balans & trades."""
    try:
        ex = _mexc_live()
        bal = ex.fetch_balance()
        base = (ex.market(symbol).get("base") or symbol.split("/")[0]).upper()
        tot  = float((bal.get(base) or {}).get("total") or 0.0)

        if tot < REHYDRATE_MIN_XRP:
            _dbg(f"[REHYDRATE] {symbol} dust {tot} -> ignore")
            STATE[symbol].update({
                "in_position": False, "entry_price": 0.0, "pos_amount": 0.0, "pos_quote": 0.0
            })
            return False

        pos_amount = float(ex.amount_to_precision(symbol, tot))
        entry_price = 0.0

        # 7d trades om entry te schatten (incl. BUY fees in quote)
        try:
            since  = int((time.time() - 7 * 24 * 3600) * 1000)
            trades = ex.fetch_my_trades(symbol, since=since)
            net_base = 0.0
            cost_q   = 0.0
            fee_buy_q= 0.0
            base_ccy = (ex.market(symbol).get("base") or base).upper()
            for t in trades:
                amt  = float(t.get("amount") or 0.0)
                pr   = float(t.get("price") or 0.0)
                side = (t.get("side") or "").lower()
                if side == "buy":
                    net_base += amt
                    cost_q   += amt * pr
                    ff = t.get("fee") or {}
                    try:
                        cur = str(ff.get("currency", "")).upper()
                        val = float(ff.get("cost") or 0.0)
                        if val > 0:
                            if cur in {"USDT","USD"}: fee_buy_q += val
                            elif cur == base_ccy and pr > 0: fee_buy_q += val * pr
                    except Exception: pass
                elif side == "sell":
                    net_base -= amt
                    cost_q   -= amt * pr
                if net_base <= 1e-12:
                    net_base, cost_q, fee_buy_q = 0.0, 0.0, 0.0
            if net_base > 0 and (cost_q + fee_buy_q) > 0:
                entry_price = (cost_q + fee_buy_q) / net_base
        except Exception as e2:
            _dbg(f"[REHYDRATE] trade-based entry calc failed {symbol}: {e2}")

        if entry_price <= 0.0:
            try:
                tk = ex.fetch_ticker(symbol)
                entry_price = float(tk.get("last") or 0.0)
            except Exception:
                entry_price = 0.0

        pos_quote = round(entry_price * pos_amount, 6)
        STATE[symbol].update({
            "in_position": True,
            "entry_price": entry_price,
            "entry_ts":    time.time(),
            "last_action_ts": time.time(),
            "pos_amount":  pos_amount,
            "pos_quote":   pos_quote,
        })
        _dbg(f"[REHYDRATE] {symbol} amt={pos_amount}, entry≈{entry_price}, pos_quote≈{pos_quote}")
        return True

    except Exception as e:
        _dbg(f"[REHYDRATE] failed {symbol}: {e}")
        STATE[symbol].update({
            "in_position": False, "entry_price": 0.0, "pos_amount": 0.0, "pos_quote": 0.0
        })
        return False

def _rehydrate_all_symbols():
    for sym in TRADEABLE:
        _rehydrate_from_mexc_symbol(sym)

def _place_mexc_market(side: str, quote_amount_usd: float, ref_price: float):
    ex = _mexc_live()
    symbol = SYMBOL_TV

    # Bepaal amount in base (XRP) op basis van quote budget
    amount = _qty_for_quote(ex, symbol, quote_amount_usd, ref_price)
    if amount <= 0:
        raise RuntimeError("Calculated amount <= 0")

    # Plaats marktorder
    order = ex.create_order(symbol, "market", side, amount)

    # Definitieve fills/fees ophalen (sommige velden komen pas na fetch_order)
    oid = order.get("id")
    if oid:
        for _ in range(2):
            try:
                time.sleep(0.25)
                o2 = ex.fetch_order(oid, symbol)
                if o2:
                    order = {**order, **o2}
                    break
            except Exception:
                pass

    avg = order.get("average") or order.get("price") or ref_price
    filled = order.get("filled") or amount
    return float(avg), float(filled), order


def _order_quote_breakdown(ex, symbol, order: dict, side: str):
    """
    Haal ccxt-order uiteen in:
      avg         : gemiddelde fillprijs
      filled      : gevulde hoeveelheid (base, XRP)
      gross_quote : bruto USDT (avg * filled of order.cost)
      fee_quote   : fees omgerekend naar USDT (trade-fee heeft voorrang)
      net_quote   : BUY = gross + fee ; SELL = gross - fee
    """
    side = (side or "").lower().strip()
    avg = float(order.get("average") or order.get("price") or 0.0)
    filled = float(order.get("filled") or 0.0)
    gross = float(order.get("cost") or (avg * filled))

    fee_q = 0.0
    trades = order.get("trades") or []
    base_ccy = (ex.market(symbol).get("base") or "").upper()

    # Voorkeur: trade-level fees (voorkomt dubbel tellen)
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
                fee_q += val * avg   # base-fee → quote (USDT)
            # MX/overige fee-tokens negeren we voor PnL (tenzij je later een rate toevoegt)
        except Exception:
            pass

    # Fallback: order-level fees als er geen trades zijn
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

    # Safety voor avg
    if avg <= 0.0 and filled > 0.0:
        avg = gross / filled if gross > 0 else 0.0

    return float(avg), float(filled), float(gross), float(fee_q), float(net)


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

        # LIVE BUY op MEXC → gebruik echte fills + fees
        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            try:
                avg, filled, order = _place_mexc_market("buy", START_CAPITAL, price)

                # Breakdown → netto inleg (incl. USDT-fee). Vereist _order_quote_breakdown helper.
                ex = _mexc_live()
                avg2, filled2, gross_q, fee_q, net_in = _order_quote_breakdown(ex, SYMBOL_TV, order, "buy")

                price       = float(avg2 or avg)                           # echte fillprijs
                pos_amount  = float(filled2 or filled)                     # gekochte XRP (base)
                pos_quote   = float(net_in if net_in > 0 else round(price * pos_amount, 6))  # netto inleg (USDT)

                _dbg(f"[LIVE] MEXC BUY id={order.get('id')} filled={pos_amount} avg={price} gross={gross_q} fee_q={fee_q} pos_quote={pos_quote}")
            except Exception as e:
                _dbg(f"[LIVE] MEXC BUY failed: {e}")
                return "LIVE BUY failed", 500
        else:
            # Simulatiepad
            pos_amount = START_CAPITAL / price
            pos_quote  = START_CAPITAL

        entry_price    = price if pos_amount > 0 else 0.0
        in_position    = True
        last_action_ts = time.time()
        entry_ts       = time.time()
        tpsl_on_buy(entry_price)  # lokaal TPSL/arming volgens ENV
        _dbg(f"BUY executed: entry={entry_price}")

        # Telegram
        delta_txt = f"  (Δ {(price / tv_price - 1) * 100:+.2f}%)" if tv_price else ""
        send_tg(
            f"""🟢 <b>[XRP/USDT] AANKOOP</b>
📹 TV prijs: ${tv_price:.4f}
🎯 Fill (MEXC): ${price:.4f}{delta_txt}
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
        # Alleen verkopen als we daadwerkelijk in een geopende trade zitten
        if not in_position:
            _dbg("sell ignored: not in_position")
            return "OK", 200

        tv_sell = float(tv_price or 0.0)
        amt_for_pnl = float(pos_amount)   # default: hele positie
        inleg_quote = float(pos_quote)    # netto inleg bij entry

        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            try:
                ex = _mexc_live()
                m  = ex.market(SYMBOL_TV)

                # Marktspecificaties
                bal      = ex.fetch_balance()
                base     = m.get("base") or "XRP"
                free_amt = float((bal.get(base) or {}).get("free") or 0.0)

                prec = int((m.get("precision") or {}).get("amount") or 6)
                lims = (m.get("limits") or {}).get("amount") or {}
                min_from_limits    = float(lims.get("min") or 0.0)
                min_from_precision = (10 ** (-prec)) if prec > 0 else 0.0
                min_amt = max(min_from_limits, min_from_precision, float(os.getenv("FALLBACK_MIN_XRP", "0.01")))

                ticks   = max(1, int(os.getenv("SELL_EPSILON_TICKS", "1")))
                epsilon = (10 ** (-prec)) * ticks

                # Wat we willen vs. wat vrij is
                wanted = float(ex.amount_to_precision(SYMBOL_TV, pos_amount))
                amt    = min(wanted, free_amt)

                # Onder minimum? → dust afhandelen, niet verkopen
                if amt < min_amt or amt <= 0.0:
                    _dbg(f"[LIVE] SELL skipped: amt<{min_amt} (pos_amount={pos_amount}, free={free_amt}) → treat as dust")
                    # Sluit intern als het echt resten zijn (of laat staan, jouw keuze)
                    if pos_amount <= max(min_amt, float(os.getenv("REHYDRATE_MIN_XRP", "5.0"))):
                        in_position = False
                        entry_price = 0.0
                        pos_amount  = 0.0
                        pos_quote   = 0.0
                        send_tg(
                            f"📄 <b>[XRP/USDT] VERKOOP</b>\n"
                            f"🧠 Reden: dust_below_exchange_min\n"
                            f"🪙 Restpositie intern gesloten (dust)\n"
                            f"🕒 Tijd: {now_str()}"
                        )
                    else:
                        _dbg("[LIVE] leave as dust; no order placed")
                    return "OK", 200

                # Pas epsilon alleen toe als we boven minimum blijven
                if amt - epsilon >= min_amt:
                    amt = amt - epsilon

                # Her-quantize
                amt = float(ex.amount_to_precision(SYMBOL_TV, max(0.0, amt)))

                # Order plaatsen
                order = ex.create_order(SYMBOL_TV, "market", "sell", amt)

                # >>> Definitieve fills/fees/trades ophalen (anders kun je filled=0 zien)
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

                # Netto opbrengst uit breakdown
                avg2, filled2, gross_q, fee_q, net_out = _order_quote_breakdown(ex, SYMBOL_TV, order, "sell")
                price   = float(avg2)
                filled  = float(filled2)
                amt_for_pnl = filled

                # Proportionele inleg bij partial
                if pos_amount > 0 and filled < pos_amount:
                    inleg_quote = inleg_quote * (filled / pos_amount)

                revenue_net = float(net_out)  # na USDT-fee
                winst_bedrag = round(revenue_net - inleg_quote, 2)

                _dbg(f"[LIVE] MEXC SELL id={order.get('id')} filled={filled} avg={price} gross={gross_q} fee_q={fee_q} net_out={revenue_net} pnl={winst_bedrag}")

            except Exception as e:
                _dbg(f"[LIVE] MEXC SELL error: {e}")
                return "OK", 200  # rustig doorschuiven

        else:
            # Simulatiepad
            if entry_price > 0:
                verkoop_bedrag = price * START_CAPITAL / entry_price
                winst_bedrag   = round(verkoop_bedrag - START_CAPITAL, 2)
            else:
                winst_bedrag   = 0.0
            price = price or tv_sell  # display

        # Boekhouding
        if winst_bedrag > 0:
            sparen  += SAVINGS_SPLIT * winst_bedrag
            capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
        else:
            capital += winst_bedrag

        # Top-up naar START_CAPITAL
        if capital < START_CAPITAL:
            tekort = START_CAPITAL - capital
            if sparen >= tekort:
                sparen -= tekort
                capital += tekort

        # Positie bijwerken (partial vs volledig)
        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            if pos_amount > 0:
                if amt_for_pnl < pos_amount:
                    # partial: hou restpositie over met juiste entry
                    factor     = (pos_amount - amt_for_pnl) / pos_amount
                    pos_quote  = round(pos_quote * factor, 6)
                    pos_amount = round(pos_amount - amt_for_pnl, 6)
                    min_xrp    = float(os.getenv("REHYDRATE_MIN_XRP", "5"))
                    in_position = pos_amount > min_xrp
                    entry_price = (pos_quote / pos_amount) if in_position and pos_amount > 0 else 0.0
                else:
                    # volledig dicht
                    pos_amount  = 0.0
                    pos_quote   = 0.0
                    in_position = False
                    entry_price = 0.0
            else:
                in_position = False
                entry_price = 0.0
                pos_amount  = 0.0
                pos_quote   = 0.0
        else:
            in_position = False
            entry_price = 0.0
            pos_amount  = 0.0
            pos_quote   = 0.0

        last_action_ts = time.time()
        if LOCAL_TPSL_ENABLED:
            tpsl_reset()

        # Telegram
        display_fill = price if (LIVE_MODE and LIVE_EXCHANGE == "mexc") else tv_sell
        base_for_delta = tv_sell if tv_sell else display_fill
        delta_txt   = f"  (Δ {(display_fill / base_for_delta - 1) * 100:+.2f}%)" if base_for_delta else ""
        resultaat   = "Winst" if winst_bedrag >= 0 else "Verlies"
        rest_txt    = f"\n🪙 Resterend: {pos_amount:.4f} XRP @ ~${entry_price:.4f}" if in_position else ""

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
🔗 Tijd: {timestamp}{rest_txt}"""
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

    # Alleen forceren als we echt in positie zijn óf (live) size zien
    if not in_position and not (LIVE_MODE and LIVE_EXCHANGE == "mexc" and pos_amount > 1e-12):
        return False

    # Snapshot vóór wijziging (belangrijk voor PnL)
    amt_before   = float(pos_amount)
    quote_before = float(pos_quote)
    winst_bedrag = 0.0
    display_fill = float(price or 0.0)  # fallback voor TG

    # LIVE exit (MEXC) – robuust tegen “filled=0”, min_amt, oversold
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            ex   = _mexc_live()
            m    = ex.market(SYMBOL_TV)
            base = m.get("base") or "XRP"

            bal      = ex.fetch_balance()
            free_amt = float((bal.get(base) or {}).get("free") or 0.0)

            # Precisie en minimum-groottes
            prec = int((m.get("precision") or {}).get("amount") or 6)
            lims = (m.get("limits") or {}).get("amount") or {}
            min_from_limits    = float(lims.get("min") or 0.0)
            min_from_precision = (10 ** (-prec)) if prec > 0 else 0.0
            min_amt = max(min_from_limits, min_from_precision, float(os.getenv("FALLBACK_MIN_XRP", "0.01")))

            ticks   = max(1, int(os.getenv("SELL_EPSILON_TICKS", "1")))
            epsilon = (10 ** (-prec)) * ticks

            wanted = float(ex.amount_to_precision(SYMBOL_TV, amt_before))
            amt    = min(wanted, free_amt)

            # Onder minimum? → behandel als dust en sluit intern
            if amt < min_amt or amt <= 0.0:
                _dbg(f"[LIVE] FORCED SELL skipped: amt<{min_amt} (pos={pos_amount}, free={free_amt}) → dust")
                if amt_before <= max(min_amt, float(os.getenv("REHYDRATE_MIN_XRP", "5.0"))):
                    in_position = False
                    entry_price = 0.0
                    pos_amount  = 0.0
                    pos_quote   = 0.0
                    send_tg(
                        f"🚨 <b>[XRP/USDT] FORCED SELL</b>\n"
                        f"🧠 Reden: {reason} / dust_below_exchange_min\n"
                        f"🪙 Restpositie intern gesloten (dust)\n"
                        f"🔗 Tijd: {now_str()}"
                    )
                    log_trade("sell_forced", display_fill, 0.0, source, tf)
                return False

            # Pas epsilon alleen toe als we boven minimum blijven
            if amt - epsilon >= min_amt:
                amt = amt - epsilon

            # Her-quantize
            amt = float(ex.amount_to_precision(SYMBOL_TV, max(0.0, amt)))

            # Plaats order
            order = ex.create_order(SYMBOL_TV, "market", "sell", amt)

            # >>> Definitieve fills/fees/trades ophalen (anders kun je filled=0 zien)
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

            # Netto opbrengst uit breakdown
            avg2, filled2, gross_q, fee_q, net_out = _order_quote_breakdown(ex, SYMBOL_TV, order, "sell")
            display_fill = float(avg2)
            filled       = float(filled2)

            # Proportionele inleg als partial
            if amt_before > 0 and filled < amt_before:
                quote_before = quote_before * (filled / amt_before)
            amt_for_pnl = filled

            revenue_net  = float(net_out)  # na USDT-fee
            winst_bedrag = round(revenue_net - quote_before, 2)

            _dbg(f"[LIVE] MEXC FORCED SELL id={order.get('id')} filled={filled} avg={display_fill} gross={gross_q} fee_q={fee_q} net_out={revenue_net} pnl={winst_bedrag}")

        except Exception as e:
            msg = str(e)
            _dbg(f"[LIVE] MEXC FORCED SELL error: {msg}")
            # Optionele retry bij “Oversold/Insufficient”
            if "Oversold" in msg or "30005" in msg or "Insufficient" in msg:
                try:
                    bal       = ex.fetch_balance()
                    free_amt  = float((bal.get(base) or {}).get("free") or 0.0)
                    retry_amt = float(ex.amount_to_precision(SYMBOL_TV, max(0.0, free_amt * 0.995)))
                    if retry_amt > 0:
                        order = ex.create_order(SYMBOL_TV, "market", "sell", retry_amt)
                        oid   = order.get("id")
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
                        amt_for_pnl   = filled
                        revenue_net   = float(net_out)
                        winst_bedrag  = round(revenue_net - quote_before, 2)
                        _dbg(f"[LIVE] MEXC FORCED SELL RETRY ok id={order.get('id')} filled={filled} avg={display_fill} net_out={revenue_net} pnl={winst_bedrag}")
                    else:
                        _dbg("[LIVE] FORCED RETRY skipped: retry_amt<=0")
                        return False
                except Exception as e2:
                    _dbg(f"[LIVE] FORCED RETRY failed: {e2}")
                    return False
            else:
                return False

    else:
        # Simulatiepad
        if entry_price > 0:
            verkoop_bedrag = price * START_CAPITAL / entry_price
            winst_bedrag   = round(verkoop_bedrag - START_CAPITAL, 2)
        else:
            winst_bedrag   = 0.0

    # Boekhouding
    if winst_bedrag > 0:
        sparen  += SAVINGS_SPLIT * winst_bedrag
        capital += (1.0 - SAVINGS_SPLIT) * winst_bedrag
    else:
        capital += winst_bedrag

    # Top-up naar START_CAPITAL
    if capital < START_CAPITAL:
        tekort = START_CAPITAL - capital
        if sparen >= tekort:
            sparen -= tekort
            capital += tekort

    # Positie bijwerken (partial/volledig)
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            # als 'filled' niet bestaat (simulatie), ga uit van volledig dicht
            filled
        except NameError:
            filled = amt_before

        if amt_before > 0 and filled < amt_before:
            # partial: verlaag pos_quote met verkochte inleg (quote_before), hou rest over
            sold_quote = round(quote_before, 6)
            pos_quote  = round(max(0.0, pos_quote - sold_quote), 6)
            pos_amount = round(max(0.0, amt_before - filled), 6)
            min_xrp    = float(os.getenv("REHYDRATE_MIN_XRP", "5"))
            in_position = pos_amount > min_xrp
            entry_price = (pos_quote / pos_amount) if in_position and pos_amount > 0 else 0.0
        else:
            # volledig dicht
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

    # Telegram
    resultaat = "Winst" if winst_bedrag >= 0 else "Verlies"
    send_tg(
        f"""🚨 <b>[XRP/USDT] FORCED SELL</b>
📹 Verkoopprijs: ${display_fill:.4f}
🧠 Reden: {reason}
🕒 TF: {tf}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 {resultaat}: €{winst_bedrag:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{START_CAPITAL:.2f}
🔗 Tijd: {now_str()}"""
    )

    log_trade("sell_forced", display_fill, winst_bedrag, source, tf)
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
        "live_pos_amount": round(pos_amount, 6),
        "live_pos_quote": round(pos_quote, 2),
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
    # (optioneel) advisor inladen
    if "_advisor_load" in globals():
        try:
            _advisor_load()
        except Exception as e:
            _dbg(f"[ADVISOR] _advisor_load() failed: {e}")
    else:
        _dbg("[ADVISOR] no _advisor_load() defined — skipping")

    _dbg("booted")

    # (optioneel) historische trades laden – alleen als de functie bestaat
    if "load_trades" in globals():
        try:
            load_trades()
        except Exception as e:
            _dbg(f"[TRADES] load_trades() failed: {e}")
    else:
        _dbg("[TRADES] no load_trades() defined — skipping history load")

    # Rehydrate alle geconfigureerde paren (XRP/USDT, WLFI/USDT)
    try:
        _rehydrate_all_symbols()
    except Exception as e:
        _dbg(f"[REHYDRATE] all symbols failed: {e}")

    # Start Flask
    _dbg(f"✅ Webhook server op http://0.0.0.0:{PORT}/webhook")
    app.run(host="0.0.0.0", port=PORT, debug=False)


