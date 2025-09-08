# app.py — XRP single-bot (1m) met Advisor (embedded) + trendfilter + cooldown/de-dup
# + Persistente trade-logging (JSON-bestand) + /report/daily en /report/weekly + /advisor admin

import os, time, json

# --- Env parsing helpers (robust booleans/ints/floats) ---
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
LIVE_MODE = env_bool("LIVE_MODE", True)

LIVE_EXCHANGE = os.getenv("LIVE_EXCHANGE", "").lower()
# --- Local safety net (guardrails) ---
HARD_SL_PCT   = float(os.getenv("HARD_SL_PCT", "0.018"))  # 1.8% hard stop
MAX_HOLD_MIN  = int(os.getenv("MAX_HOLD_MIN", "45"))      # force exit na 45 min
PRICE_POLL_S  = int(os.getenv("PRICE_POLL_S", "5"))       # elke 5s prijs checken
# Data-bron voor koers/klines: 'auto' (binance→bybit→okx), of forceer 'binance' | 'bybit' | 'okx'
EXCHANGE_SOURCE = os.getenv("EXCHANGE_SOURCE", "auto").lower()  # auto
entry_ts = 0.0  # timestamp van laatste BUY (voor hold-timer)

# 100% van winst naar spaar (instelbaar)
SAVINGS_SPLIT = float(os.getenv("SAVINGS_SPLIT", "1.0"))

# 1m-tuning
MIN_TRADE_COOLDOWN_S = int(os.getenv("MIN_TRADE_COOLDOWN_S", "90"))  # 90s voor 1m
DEDUP_WINDOW_S       = int(os.getenv("DEDUP_WINDOW_S",       "30"))  # dezelfde tick/prijs wegfilteren

# Trendfilter
USE_TREND_FILTER = env_bool("USE_TREND_FILTER", True)         # MA200 only-long op BUY
TREND_MODE            = os.getenv("TREND_MODE", "soft").lower()   # off|soft|hard
TREND_MA_LEN          = int(os.getenv("TREND_MA_LEN", "50"))
TREND_MA_TYPE         = os.getenv("TREND_MA_TYPE", "SMA").upper() # SMA|EMA
TREND_TF              = os.getenv("TREND_TF", "1m")
TREND_SLOPE_LOOKBACK  = int(os.getenv("TREND_SLOPE_LOOKBACK", "0"))  # 0=uit
TREND_SOFT_TOL        = float(os.getenv("TREND_SOFT_TOL", "0.001"))  # 0.1%

# Advisor AAN
ADVISOR_ENABLED = env_bool("ADVISOR_ENABLED", True)
ADVISOR_URL     = os.getenv("ADVISOR_URL", "")
ADVISOR_SECRET  = os.getenv("ADVISOR_SECRET", "")

# --- Lokale TPSL monitor ---
LOCAL_TPSL_ENABLED = env_bool("LOCAL_TPSL_ENABLED", True)
SYNC_TPSL_FROM_ADVISOR = env_bool("SYNC_TPSL_FROM_ADVISOR", True)

LOCAL_TP_PCT     = float(os.getenv("LOCAL_TP_PCT",     "0.010"))
LOCAL_SL_PCT     = float(os.getenv("LOCAL_SL_PCT",     "0.009"))
LOCAL_USE_TRAIL = env_bool("LOCAL_USE_TRAIL", True)
LOCAL_TRAIL_PCT  = float(os.getenv("LOCAL_TRAIL_PCT",  "0.007"))
LOCAL_ARM_AT_PCT = float(os.getenv("LOCAL_ARM_AT_PCT", "0.002"))

# Webhook beveiliging (optioneel)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Telegram
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

if not TG_TOKEN or not TG_CHAT_ID:
    raise SystemExit("[FOUT] TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt.")

exchange = ccxt.binance({"enableRateLimit": True}) if USE_TREND_FILTER else None

# Debug helper (zet DEBUG_SIG=1 in Render > Environment; zet ook PYTHONUNBUFFERED=1)
DEBUG_SIG = env_bool("DEBUG_SIG", True)
def _dbg(msg: str):
    if DEBUG_SIG:
        print(f"[SIGDBG] {msg}", flush=True)

_dbg("booted")

# --- State ---
in_position = False
entry_price = 0.0
capital = START_CAPITAL
sparen  = SPAREN_START
pos_amount = 0.0  # hoeveelheid XRP in live modus
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
            print(f"[TG] {r.status_code} {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[TG ERROR] {e}", flush=True)

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
        print(f"[ADVISOR] unreachable: {e}", flush=True)
        return True, "advisor_unreachable"
    
# TPSL per-trade state
tpsl_state = {
    "active": False,     # aan/uit per positie
    "tp_pct": None,
    "sl_pct": None,
    "use_trail": None,
    "trail_pct": None,
    "arm_at_pct": None,
    "armed": False,      # trailing geactiveerd?
    "high": 0.0          # high-watermark sinds entry
}

def _tpsl_defaults_from_env():
    return {
        "tp_pct":     LOCAL_TP_PCT,
        "sl_pct":     LOCAL_SL_PCT,
        "use_trail":  LOCAL_USE_TRAIL,
        "trail_pct":  LOCAL_TRAIL_PCT,
        "arm_at_pct": LOCAL_ARM_AT_PCT,
    }

def _tpsl_from_advisor():
    ap = _advisor_applied(SYMBOL_STR)  # gebruikt je embedded advisor state
    return {
        "tp_pct":     float(ap.get("TAKE_PROFIT_PCT", LOCAL_TP_PCT)),
        "sl_pct":     float(ap.get("STOP_LOSS_PCT",   LOCAL_SL_PCT)),
        "use_trail":  bool(ap.get("USE_TRAILING",     LOCAL_USE_TRAIL)),
        "trail_pct":  float(ap.get("TRAIL_PCT",       LOCAL_TRAIL_PCT)),
        "arm_at_pct": float(ap.get("ARM_AT_PCT",      LOCAL_ARM_AT_PCT)),
    }

def tpsl_reset():
    tpsl_state.update({
        "active": False, "tp_pct": None, "sl_pct": None,
        "use_trail": None, "trail_pct": None, "arm_at_pct": None,
        "armed": False, "high": 0.0
    })

def tpsl_on_buy(entry: float):
    """Init TPSL op basis van Advisor of ENV, en activeer voor deze positie."""
    if not LOCAL_TPSL_ENABLED:
        tpsl_reset()
        return
    try:
        params = _tpsl_from_advisor() if SYNC_TPSL_FROM_ADVISOR else _tpsl_defaults_from_env()
    except Exception:
        params = _tpsl_defaults_from_env()

    tpsl_state.update(params)
    tpsl_state["active"] = True
    tpsl_state["armed"]  = False
    tpsl_state["high"]   = entry
    _dbg(f"TPSL armed: {params}")

def local_tpsl_check(last_price: float):
    """Controleer TP/SL/Trailing; bij trigger -> lokale SELL (forced)."""
    if not (LOCAL_TPSL_ENABLED and in_position and tpsl_state["active"] and entry_price > 0):
        return

    e  = entry_price
    tp = e * (1.0 + float(tpsl_state["tp_pct"] or 0.0))
    sl = e * (1.0 - float(tpsl_state["sl_pct"] or 0.0))

    # Take Profit
    if last_price >= tp > 0:
        _dbg(f"TPSL: TP hit @ {last_price:.6f} (tp={tp:.6f})")
        _do_forced_sell(last_price, "local_tp")
        tpsl_reset()
        return

    # Hard Stop
    if last_price <= sl < e:
        _dbg(f"TPSL: SL hit @ {last_price:.6f} (sl={sl:.6f})")
        _do_forced_sell(last_price, "local_sl")
        tpsl_reset()
        return

    # Trailing (optioneel)
    if tpsl_state.get("use_trail"):
        arm_level = e * (1.0 + float(tpsl_state["arm_at_pct"] or 0.0))
        if not tpsl_state["armed"]:
            if last_price >= arm_level:
                tpsl_state["armed"] = True
                tpsl_state["high"]  = last_price
                _dbg(f"TPSL: trail ARMED at {last_price:.6f} (arm_level={arm_level:.6f})")
        else:
            # update high-watermark
            if last_price > tpsl_state["high"]:
                tpsl_state["high"] = last_price
            trail_stop = tpsl_state["high"] * (1.0 - float(tpsl_state["trail_pct"] or 0.0))
            if last_price <= trail_stop:
                _dbg(f"TPSL: TRAIL stop @ {last_price:.6f} (trail_stop={trail_stop:.6f}, high={tpsl_state['high']:.6f})")
                _do_forced_sell(last_price, "local_trail")
                tpsl_reset()
                return
    
def ema_series(vals, n: int):
    if not vals:
        return []
    k = 2.0 / (n + 1.0)
    e = []
    for i, v in enumerate(vals):
        if i == 0:
            e.append(float(v))
        else:
            e.append(float(v) * k + e[-1] * (1.0 - k))
    return e

def sma_at(vals, n: int, end_idx: int = None):
    if end_idx is None:
        end_idx = len(vals) - 1
    start = end_idx - n + 1
    if start < 0:
        return None
    window = vals[start:end_idx + 1]
    if len(window) < n:
        return None
    return sum(window) / n

from typing import Tuple

def _ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    ema = sum(values[:n]) / n
    for v in values[n:]:
        ema = v*k + ema*(1-k)
    return ema

from typing import Tuple
def trend_ok(price: float) -> Tuple[bool, float]:
    """
    Bereken MA op TREND_TF met type/len (+ optionele slope).
    Gate:
      - TREND_MODE=off  -> nooit blokkeren
      - TREND_MODE=soft -> blokkeren alleen als prijs < MA*(1 - tol)
      - TREND_MODE=hard -> blokkeren als prijs < MA
    """
    try:
        n = max(5, int(TREND_MA_LEN))
        ohlcv, src = fetch_ohlcv_any(SYMBOL_TV, timeframe=TREND_TF, limit=n + max(10, TREND_SLOPE_LOOKBACK + 5))
        closes = [float(c[4]) for c in ohlcv]
        if len(closes) < n:
            return True, float("nan")  # te weinig data -> niet blokkeren

        ma = _ema(closes, n) if TREND_MA_TYPE == "EMA" else sum(closes[-n:]) / float(n)

        ok_gate = True
        if USE_TREND_FILTER:
            mode = TREND_MODE
            if mode == "hard":
                ok_gate = price > ma
            elif mode == "soft":
                ok_gate = price >= ma * (1.0 - max(0.0, TREND_SOFT_TOL))
            else:  # off
                ok_gate = True

            # optionele slope
            if ok_gate and TREND_SLOPE_LOOKBACK > 0 and len(closes) > TREND_SLOPE_LOOKBACK:
                past = closes[-(TREND_SLOPE_LOOKBACK+1)]
                ok_gate = (closes[-1] - past) >= 0

        return ok_gate, (ma if ma is not None else float("nan"))
    except Exception as e:
        print(f"[TREND] fetch fail: {e}")
        return True, float("nan")

# --- Persistente log helpers ---
def load_trades():
    global trade_log
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r", encoding="utf-8") as f:
                trade_log = json.load(f)
                if not isinstance(trade_log, list):
                    trade_log = []
            print(f"[LOG] geladen: {len(trade_log)} trades uit {TRADES_FILE}", flush=True)
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

# --- Safety / Forced-Exit helpers ---
def _get_spot_price() -> float | None:
    """Haal actuele spotprijs op; val stil terug bij fout."""
    try:
        if exchange is not None:
            t = exchange.fetch_ticker(SYMBOL_TV)
            return float(t["last"])
    except Exception as e:
        print(f"[PRICE] fetch fail: {e}", flush=True)
    return None

def _do_forced_sell(price: float, reason: str, source: str = "forced_exit", tf: str = "1m") -> bool:
    """Voer een SELL uit buiten de normale flow (TPSL/max-hold/manual), met jouw bestaande boekhouding."""
    global in_position, entry_price, capital, sparen, last_action_ts, pos_amount

    # Alleen als er echt een positie open staat
    if not in_position or entry_price <= 0:
        return False

    # --- LIVE exit indien actief (MEXC) ---
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            ex = _mexc_live()
            amt = float(ex.amount_to_precision(SYMBOL_TV, pos_amount))
            if amt > 0:
                order = ex.create_order(SYMBOL_TV, "market", "sell", amt)
                avg = order.get("average") or order.get("price") or price
                price = float(avg)  # gebruik daadwerkelijke exit-prijs voor PnL
                _dbg(f"[LIVE] MEXC FORCED SELL id={order.get('id')} filled={order.get('filled')} avg={price}")
            else:
                _dbg("[LIVE] forced sell skipped: pos_amount=0")
        except Exception as e:
            _dbg(f"[LIVE] MEXC FORCED SELL failed: {e}")
            # Lokale afwikkeling gaat door; we loggen dit
        pos_amount = 0.0
    # --------------------------------------

    # PnL en boeking (zoals in jouw SELL)
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

    # State resetten
    in_position = False
    entry_price = 0.0
    last_action_ts = time.time()

    # (optioneel) TPSL resetten als die helper bestaat
    try:
        local_tpsl_reset()
        _dbg("[TPSL] reset on FORCED SELL")
    except Exception:
        pass

    return True


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
    """Check hard SL en max-hold. Roept _do_forced_sell aan indien nodig."""
    if not in_position or entry_price <= 0:
        return

    # Haal prijs (of gebruik aangeleverde)
    if last_price is None:
        try:
            last_price, src = fetch_last_price_any(SYMBOL_TV)
            print(f"[PRICE] via {src}: {last_price}")
        except Exception as e:
            print(f"[PRICE] fetch fail all: {e}")
            return

    # Hard stop-loss
    if HARD_SL_PCT > 0 and last_price <= entry_price * (1.0 - HARD_SL_PCT):
        _do_forced_sell(last_price, f"hard_sl_{HARD_SL_PCT:.3%}")
        return

    # Max hold tijd
    if MAX_HOLD_MIN > 0 and entry_ts > 0 and (time.time() - entry_ts) >= MAX_HOLD_MIN * 60:
        _do_forced_sell(last_price, f"max_hold_{MAX_HOLD_MIN}m")
        return

# ------- Multi-exchange fallbacks (klines/price) -------
def _make_client(name: str):
    if name == "binance":
        return ccxt.binance({"enableRateLimit": True})
    if name == "bybit":
        # forceer SPOT
        return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    if name == "okx":
        # forceer SPOT (anders kan ccxt per ongeluk futures/SWAP kiezen)
        return ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    if name == "mexc":
        # MEXC SPOT
        return ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    raise ValueError(f"unknown exchange: {name}")

def _sources_order():
    src = EXCHANGE_SOURCE.lower()
    if src == "auto":
        return ["mexc", "binance", "bybit", "okx"]
    return [src]

def fetch_ohlcv_any(symbol_tv="XRP/USDT", timeframe="1m", limit=210):
    errs = []
    for name in _sources_order():
        try:
            ex = _make_client(name)
            data = ex.fetch_ohlcv(symbol_tv, timeframe=timeframe, limit=limit)
            return data, name
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise Exception(" | ".join(errs))

def fetch_last_price_any(symbol_tv="XRP/USDT"):
    errs = []
    for name in _sources_order():
        try:
            ex = _make_client(name)
            t = ex.fetch_ticker(symbol_tv)
            last = float(t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice") or 0.0)
            if last > 0:
                return last, name
            raise Exception("no last price")
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise Exception(" | ".join(errs))
# --- LIVE trading helpers (MEXC) ---
def _mexc_live():
    ak = os.getenv("MEXC_API_KEY", "")
    sk = os.getenv("MEXC_API_SECRET", "")
    if not ak or not sk:
        raise RuntimeError("MEXC_API_KEY/SECRET ontbreken")
    ex = ccxt.mexc({
        "apiKey": ak,
        "secret": sk,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
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
    raw = data.get("values") or data.get("changes") or {k:v for k,v in data.items() if k.lower() not in {"symbol"}}
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

@app.route("/advisor/tweak", methods=["POST"])  # alias
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
# --- Cooldown & de-dup helpers (must be defined before /webhook) ---
def blocked_by_cooldown() -> bool:
    """Blokkeer nieuwe signalen binnen de cooldownperiode na de laatste actie."""
    if MIN_TRADE_COOLDOWN_S <= 0:
        return False
    return (time.time() - last_action_ts) < MIN_TRADE_COOLDOWN_S

def is_duplicate_signal(action: str, source: str, price: float, tf: str) -> bool:
    """Filter identieke signalen binnen DEDUP_WINDOW_S op (action, source, price4, tf)."""
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

@app.route("/webhook", methods=["POST"])
def webhook():
    global in_position, entry_price, capital, sparen, last_action_ts, entry_ts, tpsl_state, pos_amount

    # Secret check
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return "Unauthorized", 401

    # DEBUG: ruwe binnenkomst
    _dbg(f"/webhook hit ct={request.headers.get('Content-Type')} raw={request.data[:200]!r}")

    data = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).lower().strip()
    source = str(data.get("source", "onbekend")).lower().strip()
    tf     = str(data.get("tf", "1m")).lower().strip()  # optioneel, default 1m

    try:
        price = float(data.get("price", 0))
    except Exception:
        _dbg("bad price in payload")
        return "Bad price", 400

    if price <= 0 or action not in ("buy", "sell"):
        _dbg("bad payload guard (price<=0 of action niet buy/sell)")
        return "Bad payload", 400

    # ---- DEBUG: binnenkomend signaal
    _dbg(f"incoming action={action} price={price} src={source} tf={tf}")

    # De-dup & cooldown
    if is_duplicate_signal(action, source, price, tf):
        _dbg(f"dedup ignored key={(action, source, round(price,4), tf)} win={DEDUP_WINDOW_S}s")
        return "OK", 200
    if blocked_by_cooldown():
        _dbg(f"cooldown ignored min={MIN_TRADE_COOLDOWN_S}s since last_action")
        return "OK", 200

    # Advisor check
    allowed, advisor_reason = advisor_allows(action, price, source, tf)
    _dbg(f"advisor {('ALLOW' if allowed else 'BLOCK')} reason={advisor_reason}")
    if not allowed:
        return "OK", 200

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

        # --- LIVE BUY on MEXC (optional) ---
        if LIVE_MODE and LIVE_EXCHANGE == "mexc":
            try:
                avg, filled, order = _place_mexc_market("buy", START_CAPITAL, price)
                price = float(avg)          # gebruik echte fillprijs
                pos_amount = float(filled)  # bewaar gekochte hoeveelheid XRP
                _dbg(f"[LIVE] MEXC BUY id={order.get('id')} filled={filled} avg={avg}")
            except Exception as e:
                _dbg(f"[LIVE] MEXC BUY failed: {e}")
                return "LIVE BUY failed", 500
        # ------------------------------------

        entry_price = price
        in_position = True
        last_action_ts = time.time()
        entry_ts = time.time()
        _dbg(f"BUY executed: entry={entry_price}")

        # --- (5) ARM LOKALE TPSL OP BUY ---
        if LOCAL_TPSL_ENABLED:
            ap = _advisor_applied(SYMBOL_STR)  # neem actuele params over
            tpsl_state["active"]      = True
            tpsl_state["armed"]       = False
            tpsl_state["entry_price"] = price
            tpsl_state["high_water"]  = price
            tpsl_state["tp_pct"]      = float(ap.get("TAKE_PROFIT_PCT", 0.012))
            tpsl_state["sl_pct"]      = float(ap.get("STOP_LOSS_PCT",   0.018))
            tpsl_state["trail_pct"]   = float(ap.get("TRAIL_PCT",       0.006)) if ap.get("USE_TRAILING", True) else 0.0
            tpsl_state["arm_at_pct"]  = float(ap.get("ARM_AT_PCT",      0.004))
            _dbg(f"[TPSL] armed on BUY: {tpsl_state}")

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

        # log (winst = 0 bij buy)
        log_trade("buy", price, 0.0, source, tf)
        return "OK", 200

    # === SELL ===
    if action == "sell":
        if not in_position:
            _dbg("sell ignored: not in_position")
            return "OK", 200
        if entry_price <= 0:
            _dbg("sell guard: invalid entry_price")
            return "No valid entry", 400

            # --- LIVE SELL on MEXC (optional) ---
    if LIVE_MODE and LIVE_EXCHANGE == "mexc":
        try:
            ex = _mexc_live()
            amt = float(ex.amount_to_precision(SYMBOL_TV, pos_amount))
            if amt > 0:
                order = ex.create_order(SYMBOL_TV, "market", "sell", amt)
                avg = order.get("average") or order.get("price") or price
                price = float(avg)   # gebruik daadwerkelijke exit-prijs
                _dbg(f"[LIVE] MEXC SELL id={order.get('id')} filled={order.get('filled')} avg={price}")
            else:
                _dbg("[LIVE] MEXC SELL skipped: pos_amount=0")
        except Exception as e:
            _dbg(f"[LIVE] MEXC SELL failed: {e}")
            return "LIVE SELL failed", 500
    pos_amount = 0.0

verkoop_bedrag = price * START_CAPITAL / entry_price
        winst_bedrag = round(verkoop_bedrag - START_CAPITAL, 2)
        _dbg(f"SELL calc -> price={price} entry={entry_price} pnl={winst_bedrag}")

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
        _dbg(f"SELL executed -> {resultaat}={winst_bedrag}, capital={capital}, sparen={sparen}")

        # --- (5) RESET LOKALE TPSL NA SELL ---
        if LOCAL_TPSL_ENABLED:
            tpsl_state["active"]      = False
            tpsl_state["armed"]       = False
            tpsl_state["entry_price"] = 0.0
            tpsl_state["high_water"]  = 0.0
            _dbg("[TPSL] reset on SELL")

        send_tg(
            f"""📄 <b>[XRP/USDT] VERKOOP</b>
📹 Verkoopprijs: ${price:.4f}
🧠 Signaalbron: {source} | {advisor_reason}
🕒 TF: {tf}
💰 Handelssaldo: €{capital:.2f}
💼 Spaarrekening: €{sparen:.2f}
📈 {resultaat}: €{winst_bedrag:.2f}
📈 Totale waarde: €{capital + sparen:.2f}
🔐 Tradebedrag: €{START_CAPITAL:.2f}
🔗 Tijd: {timestamp}"""
        )

        # log (winst/verlies vastleggen)
        log_trade("sell", price, winst_bedrag, source, tf)
        entry_price = 0.0
        return "OK", 200

    # Fallback
    _dbg("unknown path fallthrough (should not happen)")
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
    def masked(s): 
        return bool(s)

    last_action_str = (
        datetime.fromtimestamp(last_action_ts).strftime("%d-%m-%Y %H:%M:%S")
        if last_action_ts > 0 else None
    )

    payload = {
        "live_exchange": LIVE_EXCHANGE,
        "mexc_keys_set": bool(os.getenv("MEXC_API_KEY") and os.getenv("MEXC_API_SECRET")),
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
        "last_action": last_action_str,

        # advisor (lokaal applied)
        "advisor_applied": _advisor_applied(SYMBOL_STR),

        # lokale TPSL monitor
        "local_tpsl_enabled": LOCAL_TPSL_ENABLED,
        "tpsl_state": {
            "active": tpsl_state.get("active", False),
            "armed": tpsl_state.get("armed", False),
            "tp_pct": tpsl_state.get("tp_pct"),
            "sl_pct": tpsl_state.get("sl_pct"),
            "trail_pct": tpsl_state.get("trail_pct"),
            "arm_at_pct": tpsl_state.get("arm_at_pct"),
        },
    }
    return jsonify(payload)

def idle_worker():
    while True:
        time.sleep(PRICE_POLL_S)
        try:
            last, src = fetch_last_price_any(SYMBOL_TV)
            # optioneel loggen:
            # print(f"[PRICE] via {src}: {last}")
            # Eerst lokale TPSL, dan safety-net:
            local_tpsl_check(last)
            forced_exit_check(last)
        except Exception as e:
            print(f"[IDLE] error: {e}")
            continue

if __name__ == "__main__":
    _advisor_load()  # laad persistente advisor-config
    load_trades()    # probeer bestaande log in te lezen bij start
    port = int(os.environ.get("PORT", "5000"))
    print(f"✅ Webhook server op http://0.0.0.0:{port}/webhook")
    Thread(target=idle_worker, daemon=True).start()
    send_tg("✅ XRP-bot gestart op Render")
    app.run(host="0.0.0.0", port=port)
