# -*- coding: utf-8 -*-
"""
BTC/USDT paper long/short webhook bot met Telegram, Supervisor v1 en TPSL.
- PAPER long + short simulatie via SIMULATE=true
- Spot-live long blijft mogelijk, live shorts worden bewust geblokkeerd
- Verwachte TradingView actions:
  long_entry, long_exit, short_entry, short_exit
- Backwards compatible:
  buy  -> long_entry
  sell -> long_exit
"""

import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from threading import Lock, Thread
from typing import Dict, Any, Tuple, List, Optional

import requests
from flask import Flask, request, jsonify
import ccxt


# ---------------- helpers ----------------

def env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except Exception:
        return float(default)


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def normalize_tf(tf: str) -> str:
    if tf is None:
        return ""
    s = str(tf).strip()
    if not s:
        return ""
    u = s.upper()
    if u.isdigit():
        n = int(u)
        return f"{n}m" if n < 60 else f"{n // 60}h"
    if u in ("D", "1D"):
        return "1d"
    if u in ("W", "1W"):
        return "1w"
    if u in ("M", "1M", "1MO"):
        return "1M"
    if s.lower().endswith(("m", "h", "d", "w")):
        return s.lower()
    if s.lower().endswith("mo"):
        return "1M"
    return s


def parse_symbol(tv_symbol: str) -> str:
    s = (tv_symbol or "").upper().strip().replace(" ", "").replace("/", "")
    if not s:
        return ""
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    return s


def sym_label(symbol: str) -> str:
    return symbol.split("/")[0].upper()


def fmt_usd(val: float, decimals: int = 2) -> str:
    return f"${val:,.{decimals}f}"


def fmt_eur(val: float, decimals: int = 2) -> str:
    return f"€{val:,.{decimals}f}"


def local_now() -> datetime:
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "UTC")))


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d-%m %H:%M")


def eur_rate() -> float:
    return env_float("USD_TO_EUR", "0.92")


def pct_change(from_price: float, to_price: float) -> float:
    if from_price <= 0:
        return 0.0
    return (to_price / from_price - 1.0) * 100.0


def pct_profit_by_side(side: str, entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    if side == "short":
        return (entry / price - 1.0) * 100.0
    return (price / entry - 1.0) * 100.0


def ema_series(values: List[float], length: int) -> List[float]:
    if not values or length <= 0:
        return []
    k = 2.0 / (length + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1.0 - k))
    return out


def rsi_last(closes: List[float], length: int = 14) -> Optional[float]:
    if len(closes) <= length + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_last(highs: List[float], lows: List[float], closes: List[float], length: int = 14) -> Optional[float]:
    if len(closes) <= length + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < length:
        return None
    atr = sum(trs[:length]) / length
    for tr in trs[length:]:
        atr = (atr * (length - 1) + tr) / length
    return atr


def adx_last(highs: List[float], lows: List[float], closes: List[float], length: int = 14) -> Optional[float]:
    if len(closes) <= length * 2 + 2:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:length])
    pdm = sum(plus_dm[:length])
    mdm = sum(minus_dm[:length])
    dxs = []
    for i in range(length, len(trs)):
        atr = atr - (atr / length) + trs[i]
        pdm = pdm - (pdm / length) + plus_dm[i]
        mdm = mdm - (mdm / length) + minus_dm[i]
        if atr <= 0:
            continue
        pdi = 100.0 * (pdm / atr)
        mdi = 100.0 * (mdm / atr)
        denom = pdi + mdi
        if denom > 0:
            dxs.append(100.0 * abs(pdi - mdi) / denom)
    if len(dxs) < length:
        return None
    adx = sum(dxs[:length]) / length
    for dx in dxs[length:]:
        adx = (adx * (length - 1) + dx) / length
    return adx


# ---------------- ENV / constants ----------------

app = Flask(__name__)

PORT = int(os.getenv("PORT", "10000"))
BOT_TITLE = os.getenv("BOT_TITLE", "BTC Paper L/S Bot")

SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
SYMBOLS = [SYMBOL]
_symbol_key = SYMBOL.replace("/", "_").upper()
_budget_env_key = f"BUDGET_{_symbol_key}"
BUDGET_USDT = {SYMBOL: float(os.getenv(_budget_env_key, os.getenv("BUDGET_BTC_USDT", "500")))}

MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
MEXC_RECVWINDOW_MS = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
CCXT_TIMEOUT_MS = int(os.getenv("CCXT_TIMEOUT_MS", "7000"))

SIMULATE = env_bool("SIMULATE", "true")
REHYDRATE_ENABLED = env_bool("REHYDRATE_ENABLED", "false")
STARTUP_TG_ENABLED = env_bool("STARTUP_TG_ENABLED", "true")
WEBHOOK_NOTIFY_SKIPS = env_bool("WEBHOOK_NOTIFY_SKIPS", "true")
WEBHOOK_NOTIFY_RECEIVED = env_bool("WEBHOOK_NOTIFY_RECEIVED", "false")

# Paper long/short controls
ENABLE_LONGS = env_bool("ENABLE_LONGS", "true")
ENABLE_SHORTS = env_bool("ENABLE_SHORTS", "false")
ALLOW_LIVE_SHORTS = env_bool("ALLOW_LIVE_SHORTS", "false")  # bewust false houden
PAPER_FEE_PCT = env_float("PAPER_FEE_PCT", "0.10")          # simulatie-fee per kant
PAPER_SLIPPAGE_PCT = env_float("PAPER_SLIPPAGE_PCT", "0.03")
LEVERAGE = env_float("PAPER_LEVERAGE", "1.0")

STRICT_DEDUP_S = env_float("STRICT_DEDUP_S", "3")
MIN_TRADE_COOLDOWN_S = env_float("MIN_TRADE_COOLDOWN_S", "0")
PER_BAR_LOCK = env_bool("PER_BAR_LOCK", "false")
PER_BAR_LOCK_BUY = env_bool("PER_BAR_LOCK_BUY", "false")
PER_BAR_LOCK_SELL = env_bool("PER_BAR_LOCK_SELL", "false")

SPAREN_ENABLED = env_bool("SPAREN_ENABLED", "true")
SPAREN_SPLIT_PCT = env_float("SPAREN_SPLIT_PCT", "100")

BOT_TPSL_ENABLED = env_bool("BOT_TPSL_ENABLED", "false")
TPSL_POLL_S = env_float("TPSL_POLL_S", "15")
HARD_SL_PCT = env_float("HARD_SL_PCT", "0.90")
BE_TRIGGER_PCT = env_float("BE_TRIGGER_PCT", "0.60")
BE_OFFSET_PCT = env_float("BE_OFFSET_PCT", "0.05")
TRAIL_TRIGGER_PCT = env_float("TRAIL_TRIGGER_PCT", "1.00")
TRAIL_DISTANCE_PCT = env_float("TRAIL_DISTANCE_PCT", "0.45")

# Supervisor
SUPERVISOR_ENABLED = env_bool("SUPERVISOR_ENABLED", "false")
SUPERVISOR_FAIL_OPEN = env_bool("SUPERVISOR_FAIL_OPEN", "true")
SUPERVISOR_NOTIFY = env_bool("SUPERVISOR_NOTIFY", "true")
SUPERVISOR_TF = os.getenv("SUPERVISOR_TF", os.getenv("ADVISOR_TF", "10m"))
SUPERVISOR_OHLCV_LIMIT = int(os.getenv("SUPERVISOR_OHLCV_LIMIT", os.getenv("ADVISOR_OHLCV_LIMIT", "260")))
SUPERVISOR_MIN_SCORE = env_float("SUPERVISOR_MIN_SCORE", "0.55")
SUPERVISOR_MIN_SHORT_SCORE = env_float("SUPERVISOR_MIN_SHORT_SCORE", str(SUPERVISOR_MIN_SCORE))
SUPERVISOR_DYNAMIC_SIZE = env_bool("SUPERVISOR_DYNAMIC_SIZE", "true")
SUPERVISOR_MIN_SIZE_FACTOR = env_float("SUPERVISOR_MIN_SIZE_FACTOR", "0.35")
SUPERVISOR_MIN_ADX = env_float("SUPERVISOR_MIN_ADX", os.getenv("MIN_ADX", "18"))
SUPERVISOR_RSI_MIN_LONG = env_float("SUPERVISOR_RSI_MIN_LONG", "50")
SUPERVISOR_RSI_MAX_LONG = env_float("SUPERVISOR_RSI_MAX_LONG", "74")
SUPERVISOR_RSI_MIN_SHORT = env_float("SUPERVISOR_RSI_MIN_SHORT", "26")
SUPERVISOR_RSI_MAX_SHORT = env_float("SUPERVISOR_RSI_MAX_SHORT", "50")
SUPERVISOR_MIN_ATR_PCT = env_float("SUPERVISOR_MIN_ATR_PCT", "0.05")
SUPERVISOR_MAX_ATR_PCT = env_float("SUPERVISOR_MAX_ATR_PCT", "1.20")
SUPERVISOR_MIN_VOLUME_FACTOR = env_float("SUPERVISOR_MIN_VOLUME_FACTOR", "0.75")
MAX_ALERT_PRICE_DEVIATION_PCT = env_float("MAX_ALERT_PRICE_DEVIATION_PCT", "0.15")

# Trend Runner Mode - bedoeld om op 10m entries grotere 1h trends langer vast te houden.
TREND_RUNNER_ENABLED = env_bool("TREND_RUNNER_ENABLED", "false")
TREND_TF = os.getenv("TREND_TF", "1h")
TREND_IGNORE_TV_EXIT = env_bool("TREND_IGNORE_TV_EXIT", "true")
TREND_MIN_ADX = env_float("TREND_MIN_ADX", "20")
TREND_ALLOW_HIGH_RSI = env_bool("TREND_ALLOW_HIGH_RSI", "true")
TREND_HARD_SL_PCT = env_float("TREND_HARD_SL_PCT", "1.20")
TREND_TRAIL_TRIGGER_PCT = env_float("TREND_TRAIL_TRIGGER_PCT", "1.00")
TREND_TRAIL_DISTANCE_PCT = env_float("TREND_TRAIL_DISTANCE_PCT", "0.85")
TREND_TP1_PCT = env_float("TREND_TP1_PCT", "1.00")
TREND_TP1_SELL_PCT = env_float("TREND_TP1_SELL_PCT", "50")
TREND_MAX_HOLD_HOURS = env_float("TREND_MAX_HOLD_HOURS", "24")
TREND_EXIT_EMA_LEN = int(os.getenv("TREND_EXIT_EMA_LEN", "50"))
TREND_NOTIFY = env_bool("TREND_NOTIFY", "true")

sym_key = SYMBOL.replace("/", "_").lower()
STATE_FILE = Path(os.getenv("STATE_FILE", f"bot_state_{sym_key}.json"))

TRADE_LOG: List[Dict[str, Any]] = []
STATE: Dict[str, Dict[str, Any]] = {}
ex = None
lock = Lock()


# ---------------- logging / state ----------------

def _dbg(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _save_state_file():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        _dbg(f"[STATE] Save error: {e}")


def _ensure_wallet(symbol: str):
    st = STATE.setdefault(symbol, {})
    if "target_trade_usd" in st and "trade_usd" in st and "savings_usd" in st:
        return
    total = float(BUDGET_USDT.get(symbol, 0.0))
    if SPAREN_ENABLED:
        target_trade = total * (1 - SPAREN_SPLIT_PCT / 100.0)
        savings = total - target_trade
    else:
        target_trade = total
        savings = 0.0
    st.setdefault("target_trade_usd", target_trade)
    st.setdefault("trade_usd", target_trade)
    st.setdefault("savings_usd", savings)
    st.setdefault("realized_pnl_usd", 0.0)


def _load_state_file():
    global STATE
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                STATE = json.load(f)
            _dbg(f"[STATE] Loaded {len(STATE)} symbols from file")
        except Exception as e:
            _dbg(f"[STATE] Load error: {e}")
            STATE = {}
    if SYMBOL not in STATE:
        STATE[SYMBOL] = {
            "in_position": False,
            "position_side": "none",
            "inflight": False,
            "last_action_ts": 0,
            "last_bar_time": 0,
            "entry_price": 0.0,
            "qty": 0.0,
            "invested_usd": 0.0,
        }
    _ensure_wallet(SYMBOL)
    _save_state_file()


def trade_and_savings_usd(symbol: str) -> tuple[float, float]:
    _ensure_wallet(symbol)
    st = STATE.get(symbol, {})
    return float(st.get("trade_usd", 0.0)), float(st.get("savings_usd", 0.0))


# ---------------- telegram ----------------

def send_tg(msg: str):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=5)
            _dbg("[TG] Message sent")
        else:
            _dbg("[TG] No token/chat_id - skipped")
    except Exception as e:
        _dbg(f"[TG] Send error: {e}")


def tg_skip_msg(reason: str, action: str = "", symbol: str = "", price: float = 0.0, tf: str = "", source: str = "", extra: str = "") -> str:
    st = STATE.get(symbol or SYMBOL, {})
    in_pos = bool(st.get("in_position", False))
    side = st.get("position_side", "none")
    lines = [
        f"{BOT_TITLE}",
        "⚠️ Webhook overgeslagen",
        f"Action: {action or '-'}",
        f"Symbol: {symbol or '-'}",
        f"TF: {tf or '-'}",
        f"Prijs: {fmt_usd(price, 2) if price else '-'}",
        f"Reden: {reason}",
        f"Positie: {'JA' if in_pos else 'NEE'} | Side: {side}",
    ]
    if source:
        lines.append(f"Bron: {source}")
    if extra:
        lines.append(f"Info: {extra}")
    lines.append(f"Tijd: {fmt_dt(local_now())}")
    return "\n".join(lines)


def notify_skip(reason: str, action: str = "", symbol: str = "", price: float = 0.0, tf: str = "", source: str = "", extra: str = ""):
    if WEBHOOK_NOTIFY_SKIPS:
        try:
            send_tg(tg_skip_msg(reason, action, symbol, price, tf, source, extra))
        except Exception as e:
            _dbg(f"[TG SKIP] notify error: {e}")


def tg_startup_msg() -> str:
    """
    Telegram startmelding bij Render deploy/restart.
    """
    st = STATE.get(SYMBOL, {})
    pos_side = st.get("position_side", "none")
    in_pos = bool(st.get("in_position", False))
    active_stop = float(st.get("active_stop_price", 0.0) or 0.0)

    lines = [
        f"{BOT_TITLE}",
        "🚀 Bot gestart",
        f"📌 Symbol: {SYMBOL}",
        f"🧪 Modus: {'PAPER' if SIMULATE else 'LIVE'}",
        f"📈 Longs: {'AAN' if ENABLE_LONGS else 'UIT'}",
        f"📉 Shorts: {'AAN' if ENABLE_SHORTS else 'UIT'}",
        f"🛡️ TPSL: {'AAN' if BOT_TPSL_ENABLED else 'UIT'} | SL {HARD_SL_PCT:.2f}% | BE {BE_TRIGGER_PCT:.2f}% | Trail {TRAIL_TRIGGER_PCT:.2f}%/{TRAIL_DISTANCE_PCT:.2f}%",
        f"🚀 Trend Runner: {'AAN' if TREND_RUNNER_ENABLED else 'UIT'} | TF {TREND_TF} | TV-exit negeren {'JA' if TREND_IGNORE_TV_EXIT else 'NEE'} | TP1 {TREND_TP1_PCT:.2f}%/{TREND_TP1_SELL_PCT:.0f}% | Max {TREND_MAX_HOLD_HOURS:g}h",
        f"🤖 Supervisor: {'AAN' if SUPERVISOR_ENABLED else 'UIT'} | Long min {SUPERVISOR_MIN_SCORE:.2f} | Short min {SUPERVISOR_MIN_SHORT_SCORE:.2f}",
        f"💰 Budget: {fmt_usd(float(BUDGET_USDT.get(SYMBOL, 0.0)), 2)}",
        f"🗂 State file: {STATE_FILE.name}",
        f"📍 Positie: {'JA' if in_pos else 'NEE'} | Side: {pos_side}",
    ]
    if active_stop > 0:
        lines.append(f"🛑 Actieve stop: {fmt_usd(active_stop, 2)}")
    lines.append(f"🔗 Tijd: {fmt_dt(local_now())}")
    return "\n".join(lines)


def tg_open_msg(symbol: str, side: str, price_usd: float, qty: float, invested_usd: float, source: str) -> str:
    side_label = "LONG OPEN" if side == "long" else "SHORT OPEN"
    emoji = "🟢" if side == "long" else "🔴"
    return "\n".join([
        f"{BOT_TITLE}",
        f"{emoji} [{sym_label(symbol)}] {side_label}",
        f"💰 Inzet/margin: {fmt_eur(eur_rate() * invested_usd)}",
        f"📈 Entryprijs: {fmt_usd(price_usd, 4)}",
        f"📊 Hoeveelheid: {qty:,.4f}",
        f"⚙️ Leverage paper: {LEVERAGE:g}x | fee {PAPER_FEE_PCT:g}% | slip {PAPER_SLIPPAGE_PCT:g}%",
        f"🔗 Bron: {source}",
        f"⏱ Tijd: {fmt_dt(local_now())}",
        f"🧪 Modus: {'PAPER' if SIMULATE else 'LIVE'}",
    ])


def tg_close_msg(symbol: str, side: str, price_usd: float, qty: float, pnl_usd: float,
                 prev_trade_usd: float, prev_savings_usd: float, source: str) -> str:
    trade_usd, savings_usd = trade_and_savings_usd(symbol)
    total_eur = eur_rate() * (trade_usd + savings_usd)
    pnl_eur = eur_rate() * pnl_usd
    realized_eur = eur_rate() * float(STATE.get(symbol, {}).get("realized_pnl_usd", 0.0))
    side_label = "LONG CLOSE" if side == "long" else "SHORT CLOSE"
    winlose = "Winst" if pnl_eur >= 0 else "Verlies"
    return "\n".join([
        f"{BOT_TITLE}",
        f"📄 [{sym_label(symbol)}] {side_label}",
        f"📹 Exitprijs: {fmt_usd(price_usd, 4)}",
        f"📈 {winlose}: {fmt_eur(pnl_eur)}",
        f"💰 Handelssaldo: {fmt_eur(eur_rate() * trade_usd)}",
        f"💼 Spaarrekening: {fmt_eur(eur_rate() * savings_usd)}",
        f"📈 Totale waarde: {fmt_eur(total_eur)}",
        f"📊 Cumulatieve PnL: {fmt_eur(realized_eur)}",
        f"🧾 Wallet: {fmt_eur(eur_rate() * prev_trade_usd)} + {fmt_eur(eur_rate() * prev_savings_usd)} {fmt_eur(pnl_eur)} → {fmt_eur(total_eur)}",
        f"🔗 Bron: {source}",
        f"⏱ Tijd: {fmt_dt(local_now())}",
        f"🧪 Modus: {'PAPER' if SIMULATE else 'LIVE'}",
    ])


# ---------------- exchange / indicators ----------------

def allowed_tfs_for(symbol: str) -> set:
    key = f"ALLOW_TF_{symbol.replace('/', '_').upper()}"
    return set(x.strip() for x in os.getenv(key, "10m").split(",") if x.strip())


def init_exchange():
    global ex
    params = {
        "sandbox": False,
        "enableRateLimit": True,
        "options": {"recvWindow": MEXC_RECVWINDOW_MS},
        "timeout": CCXT_TIMEOUT_MS,
    }
    if not SIMULATE:
        params["apiKey"] = MEXC_API_KEY
        params["secret"] = MEXC_API_SECRET
    ex = ccxt.mexc(params)
    ex.load_markets()
    _dbg("[WARMUP] SIMULATE public client ready" if SIMULATE else "[WARMUP] MEXC client ready")


def rehydrate_positions():
    if not REHYDRATE_ENABLED:
        _dbg("[REHYDRATE] disabled via REHYDRATE_ENABLED=false")
        return
    if SIMULATE:
        _dbg("[REHYDRATE] skip in SIMULATE mode")
        return
    _dbg("[REHYDRATE] live rehydrate for futures/short is not implemented in this paper L/S version")



def mexc_interval_for_rest(tf: str) -> str:
    """Map normalised timeframe to MEXC spot REST kline interval."""
    tf = normalize_tf(tf)
    mapping = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "60m", "4h": "4h", "1d": "1d", "1w": "1W", "1M": "1M",
    }
    return mapping.get(tf, tf)


def fetch_ohlcv_supervisor(symbol: str, tf: str, limit: int) -> List[List[float]]:
    """
    Fetch OHLCV for supervisor. Direct MEXC REST first, because ccxt/MEXC
    can be slow/fail on Render and that can make TradingView mark the
    webhook as timed out. Returns ccxt-like rows:
    [timestamp, open, high, low, close, volume].
    """
    tf_norm = normalize_tf(tf) or "5m"
    sym = symbol.replace("/", "")
    interval = mexc_interval_for_rest(tf_norm)
    url = "https://api.mexc.com/api/v3/klines"
    params = {"symbol": sym, "interval": interval, "limit": int(limit)}
    direct_err = None

    # Preferred path: direct public MEXC klines. Fast and does not need account/API key.
    try:
        r = requests.get(url, params=params, timeout=4)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected_klines_response:{str(data)[:120]}")
        out = []
        for row in data:
            if len(row) < 6:
                continue
            out.append([
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ])
        if not out:
            raise RuntimeError("empty_klines_after_parse")
        _dbg(f"[SUPERVISOR] direct MEXC klines ok tf={tf_norm}/{interval} rows={len(out)}")
        return out
    except Exception as e:
        direct_err = e
        _dbg(f"[SUPERVISOR] direct MEXC klines failed tf={tf_norm}/{interval}: {e}")

    # Fallback only: ccxt fetch_ohlcv.
    if ex is not None:
        try:
            out = ex.fetch_ohlcv(symbol, timeframe=tf_norm, limit=limit)
            _dbg(f"[SUPERVISOR] ccxt OHLCV fallback ok tf={tf_norm} rows={len(out) if out else 0}")
            return out
        except Exception as e:
            raise RuntimeError(f"ohlcv_failed direct={direct_err} ccxt={e}")

    raise RuntimeError(f"ohlcv_failed direct={direct_err} ccxt=no_exchange_client")


# Directe publieke prijsfeed voor TPSL en prijs-fallback.
# Werkt zonder MEXC-account of API-sleutels.
_last_price_cache: Dict[str, Dict[str, float]] = {}
_last_price_error_log_ts = 0.0

def fetch_public_price(symbol: str) -> float:
    """
    Haal actuele publieke MEXC-prijs op.
    Volgorde:
    1) /api/v3/ticker/price
    2) /api/v3/avgPrice
    3) ccxt fetch_ticker als laatste fallback

    Een korte cache voorkomt dat een korte netwerkstoring de TPSL direct uitschakelt.
    """
    global _last_price_error_log_ts

    sym = symbol.replace("/", "")
    errors: List[str] = []

    endpoints = [
        ("ticker_price", "https://api.mexc.com/api/v3/ticker/price", {"symbol": sym}, "price"),
        ("avg_price", "https://api.mexc.com/api/v3/avgPrice", {"symbol": sym}, "price"),
    ]

    for name, url, params, key in endpoints:
        try:
            r = requests.get(url, params=params, timeout=4)
            r.raise_for_status()
            data = r.json()
            price = float(data.get(key) or 0.0) if isinstance(data, dict) else 0.0
            if price > 0:
                _last_price_cache[symbol] = {"price": price, "ts": time.time()}
                return price
            errors.append(f"{name}:invalid_price")
        except Exception as e:
            errors.append(f"{name}:{e}")

    if ex is not None:
        try:
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0.0)
            if price > 0:
                _last_price_cache[symbol] = {"price": price, "ts": time.time()}
                _dbg(f"[PRICE] ccxt ticker fallback ok {symbol} price={price}")
                return price
            errors.append("ccxt:invalid_price")
        except Exception as e:
            errors.append(f"ccxt:{e}")

    cached = _last_price_cache.get(symbol, {})
    cached_price = float(cached.get("price", 0.0) or 0.0)
    age = time.time() - float(cached.get("ts", 0.0) or 0.0)
    max_cache_age = env_float("PRICE_CACHE_MAX_AGE_S", "45")
    if cached_price > 0 and age <= max_cache_age:
        now = time.time()
        if now - _last_price_error_log_ts >= 60:
            _dbg(f"[PRICE] using cached price {symbol} price={cached_price} age={age:.1f}s errors={' | '.join(errors)}")
            _last_price_error_log_ts = now
        return cached_price

    raise RuntimeError("public_price_failed " + " | ".join(errors))


# ---------------- trend runner helpers ----------------

def trend_context(symbol: str, side: str, price: float = 0.0) -> Dict[str, Any]:
    """
    Bepaalt of de hogere timeframe trend sterk genoeg is voor Trend Runner Mode.
    Gebruikt publieke MEXC klines via fetch_ohlcv_supervisor; geen API-sleutels nodig.
    """
    if not TREND_RUNNER_ENABLED:
        return {"ok": False, "reason": "trend_runner_disabled"}
    try:
        tf = normalize_tf(TREND_TF) or "1h"
        limit = max(220, SUPERVISOR_OHLCV_LIMIT)
        ohlcv = fetch_ohlcv_supervisor(symbol, tf, limit)
        if not ohlcv or len(ohlcv) < 210:
            return {"ok": False, "reason": f"trend_not_enough_ohlcv:{len(ohlcv) if ohlcv else 0}"}

        highs = [float(x[2]) for x in ohlcv]
        lows = [float(x[3]) for x in ohlcv]
        closes = [float(x[4]) for x in ohlcv]
        live_close = closes[-1]
        check_price = price if price > 0 else live_close

        ema50_s = ema_series(closes, 50)
        ema89_s = ema_series(closes, 89)
        ema200_s = ema_series(closes, 200)
        ema50 = ema50_s[-1]
        ema89 = ema89_s[-1]
        ema200 = ema200_s[-1]
        ema200_old = ema200_s[-6] if len(ema200_s) > 6 else ema200
        ema200_up = ema200 > ema200_old
        ema200_down = ema200 < ema200_old
        adx = adx_last(highs, lows, closes, 14) or 0.0
        rsi = rsi_last(closes, 14) or 50.0
        atr = atr_last(highs, lows, closes, 14) or 0.0
        atr_pct = (atr / live_close * 100.0) if live_close > 0 else 0.0

        if side == "long":
            ok = check_price > ema50 and ema50 > ema89 and ema89 > ema200 and ema200_up and adx >= TREND_MIN_ADX
            exit_broken = check_price < ema50 or ema50 < ema89 or not ema200_up
            checks = []
            if check_price > ema50: checks.append("price>ema50")
            if ema50 > ema89: checks.append("ema50>ema89")
            if ema89 > ema200: checks.append("ema89>ema200")
            if ema200_up: checks.append("ema200_up")
            if adx >= TREND_MIN_ADX: checks.append("adx_ok")
        else:
            ok = check_price < ema50 and ema50 < ema89 and ema89 < ema200 and ema200_down and adx >= TREND_MIN_ADX
            exit_broken = check_price > ema50 or ema50 > ema89 or not ema200_down
            checks = []
            if check_price < ema50: checks.append("price<ema50")
            if ema50 < ema89: checks.append("ema50<ema89")
            if ema89 < ema200: checks.append("ema89<ema200")
            if ema200_down: checks.append("ema200_down")
            if adx >= TREND_MIN_ADX: checks.append("adx_ok")

        reason = f"tf={tf} checks={','.join(checks) or '-'} adx={adx:.1f} rsi={rsi:.1f} atr={atr_pct:.2f}%"
        return {
            "ok": bool(ok),
            "exit_broken": bool(exit_broken),
            "reason": reason,
            "tf": tf,
            "adx": float(adx),
            "rsi": float(rsi),
            "atr_pct": float(atr_pct),
            "ema50": float(ema50),
            "ema89": float(ema89),
            "ema200": float(ema200),
            "price": float(check_price),
        }
    except Exception as e:
        return {"ok": False, "exit_broken": True, "reason": f"trend_error:{e}"}


def position_age_hours(st: Dict[str, Any]) -> float:
    opened = float(st.get("opened_ts", 0.0) or 0.0)
    if opened <= 0:
        return 0.0
    return (time.time() - opened) / 3600.0


def is_tradingview_source(source: str) -> bool:
    s = (source or "").lower()
    return "tradingview" in s or "tyn_strat" in s or s == "unknown"


# ---------------- supervisor ----------------

def supervisor_size_factor(score: float) -> float:
    if not SUPERVISOR_DYNAMIC_SIZE:
        return 1.0
    if score >= 0.75:
        return 1.0
    if score >= 0.65:
        return 0.70
    return SUPERVISOR_MIN_SIZE_FACTOR


def supervisor_decision(action: str, symbol: str, price: float, payload: Dict[str, Any]) -> tuple[bool, str, float, float]:
    if not SUPERVISOR_ENABLED or action not in ("long_entry", "short_entry"):
        return True, "supervisor_disabled_or_not_entry", 1.0, 1.0
    side = "long" if action == "long_entry" else "short"
    try:
        if ex is None:
            raise RuntimeError("exchange_client_not_ready")
        tf = normalize_tf(SUPERVISOR_TF) or "10m"
        ohlcv = fetch_ohlcv_supervisor(symbol, tf, max(80, SUPERVISOR_OHLCV_LIMIT))
        if not ohlcv or len(ohlcv) < 80:
            raise RuntimeError(f"not_enough_ohlcv:{len(ohlcv) if ohlcv else 0}")
        highs = [float(x[2]) for x in ohlcv]
        lows = [float(x[3]) for x in ohlcv]
        closes = [float(x[4]) for x in ohlcv]
        volumes = [float(x[5]) for x in ohlcv]
        live_close = closes[-1]
        check_price = price if price > 0 else live_close

        ema50_s = ema_series(closes, 50)
        ema200_s = ema_series(closes, 200)
        ema50 = ema50_s[-1]
        ema200 = ema200_s[-1]
        ema200_old = ema200_s[-6] if len(ema200_s) > 6 else ema200
        ema200_up = ema200 > ema200_old
        ema200_down = ema200 < ema200_old
        rsi = rsi_last(closes, 14) or 50.0
        atr = atr_last(highs, lows, closes, 14) or 0.0
        atr_pct = (atr / live_close * 100.0) if live_close > 0 else 0.0
        adx = adx_last(highs, lows, closes, 14) or 0.0
        vol_ma = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else max(volumes[-1], 1.0)
        vol_factor = volumes[-1] / vol_ma if vol_ma > 0 else 1.0

        score = 0.0
        checks, blocks = [], []
        if side == "long":
            min_score = SUPERVISOR_MIN_SCORE
            if check_price > ema200:
                score += 0.18; checks.append("price>ema200")
            if ema50 > ema200:
                score += 0.16; checks.append("ema50>ema200")
            if ema200_up:
                score += 0.12; checks.append("ema200_up")
            if SUPERVISOR_RSI_MIN_LONG <= rsi <= SUPERVISOR_RSI_MAX_LONG:
                score += 0.16; checks.append("rsi_long_ok")
            elif rsi > SUPERVISOR_RSI_MAX_LONG:
                blocks.append("rsi_long_too_high")
            else:
                blocks.append("rsi_long_too_low")
        else:
            min_score = SUPERVISOR_MIN_SHORT_SCORE
            if check_price < ema200:
                score += 0.18; checks.append("price<ema200")
            if ema50 < ema200:
                score += 0.16; checks.append("ema50<ema200")
            if ema200_down:
                score += 0.12; checks.append("ema200_down")
            if SUPERVISOR_RSI_MIN_SHORT <= rsi <= SUPERVISOR_RSI_MAX_SHORT:
                score += 0.16; checks.append("rsi_short_ok")
            elif rsi < SUPERVISOR_RSI_MIN_SHORT:
                blocks.append("rsi_short_too_low")
            else:
                blocks.append("rsi_short_too_high")

        if adx >= SUPERVISOR_MIN_ADX:
            score += 0.12; checks.append("adx_ok")
        if SUPERVISOR_MIN_ATR_PCT <= atr_pct <= SUPERVISOR_MAX_ATR_PCT:
            score += 0.10; checks.append("atr_ok")
        elif atr_pct > SUPERVISOR_MAX_ATR_PCT:
            blocks.append("atr_too_high")
        if vol_factor >= SUPERVISOR_MIN_VOLUME_FACTOR:
            score += 0.08; checks.append("volume_ok")

        dev_pct = abs(check_price / live_close - 1.0) * 100.0 if live_close > 0 else 0.0
        if dev_pct <= MAX_ALERT_PRICE_DEVIATION_PCT:
            score += 0.08; checks.append("price_fresh")
        else:
            blocks.append("price_stale")

        trend_ctx = None
        if TREND_RUNNER_ENABLED and TREND_ALLOW_HIGH_RSI:
            trend_ctx = trend_context(symbol, side, check_price)
            if trend_ctx.get("ok"):
                if side == "long" and "rsi_long_too_high" in blocks:
                    blocks = [b for b in blocks if b != "rsi_long_too_high"]
                    checks.append("trend_override_high_rsi")
                    score += 0.06
                if side == "short" and "rsi_short_too_low" in blocks:
                    blocks = [b for b in blocks if b != "rsi_short_too_low"]
                    checks.append("trend_override_low_rsi")
                    score += 0.06

        score = clamp(score, 0.0, 1.0)
        size = supervisor_size_factor(score)
        allow = score >= min_score and not blocks
        reason = f"side={side} score={score:.2f} size={size:.2f} checks={','.join(checks) or '-'} blocks={','.join(blocks) or '-'}"
        if trend_ctx is not None:
            reason += f" trend={trend_ctx.get('ok')} {trend_ctx.get('reason')}"

        if SUPERVISOR_NOTIFY:
            send_tg(
                f"🤖 Supervisor {'ALLOW' if allow else 'BLOCK'}\n"
                f"Signaal: {side.upper()} ENTRY {sym_label(symbol)}\n"
                f"Score: {score:.2f} | Size: {size:.0%}\n"
                f"Prijs: {fmt_usd(check_price, 2)}\n"
                f"RSI: {rsi:.1f} | ADX: {adx:.1f} | ATR%: {atr_pct:.2f} | Vol: {vol_factor:.2f}x\n"
                f"Reden: {reason}"
            )
        return allow, reason, score, size
    except Exception as e:
        reason = f"supervisor_error:{e}"
        _dbg(f"[SUPERVISOR] {reason}")
        if SUPERVISOR_NOTIFY:
            send_tg(f"🤖 Supervisor {'ALLOW' if SUPERVISOR_FAIL_OPEN else 'BLOCK'}\nReden: {reason}")
        return SUPERVISOR_FAIL_OPEN, reason, 0.0, 1.0


# ---------------- position management ----------------

def normalize_action(raw_action: str) -> str:
    a = (raw_action or "").lower().strip()
    # backwards compatibility met oude spot-alerts
    if a == "buy":
        return "long_entry"
    if a == "sell":
        return "long_exit"
    return a


def action_side(action: str) -> Optional[str]:
    if action in ("long_entry", "long_exit"):
        return "long"
    if action in ("short_entry", "short_exit"):
        return "short"
    return None


def is_entry(action: str) -> bool:
    return action in ("long_entry", "short_entry")


def is_exit(action: str) -> bool:
    return action in ("long_exit", "short_exit")


def effective_entry_price(side: str, price: float) -> float:
    # long koopt iets duurder; short verkoopt iets lager
    if side == "short":
        return price * (1.0 - PAPER_SLIPPAGE_PCT / 100.0)
    return price * (1.0 + PAPER_SLIPPAGE_PCT / 100.0)


def effective_exit_price(side: str, price: float) -> float:
    # long verkoopt iets lager; short koopt terug iets hoger
    if side == "short":
        return price * (1.0 + PAPER_SLIPPAGE_PCT / 100.0)
    return price * (1.0 - PAPER_SLIPPAGE_PCT / 100.0)


def update_trade_protection_state(symbol: str, price: float):
    st = STATE.get(symbol, {})
    if not st.get("in_position", False):
        return
    side = st.get("position_side", "long")
    entry = float(st.get("entry_price", 0.0))
    if entry <= 0 or price <= 0:
        return

    trend_active = bool(st.get("trend_runner_active", False))
    hard_sl_pct = TREND_HARD_SL_PCT if trend_active else HARD_SL_PCT
    trail_trigger_pct = TREND_TRAIL_TRIGGER_PCT if trend_active else TRAIL_TRIGGER_PCT
    trail_distance_pct = TREND_TRAIL_DISTANCE_PCT if trend_active else TRAIL_DISTANCE_PCT
    be_trigger_pct = max(BE_TRIGGER_PCT, TREND_TP1_PCT) if trend_active else BE_TRIGGER_PCT

    if side == "short":
        lowest = min(float(st.get("lowest_price", entry)), price)
        st["lowest_price"] = lowest
        profit_pct = pct_profit_by_side("short", entry, price)
        stop_candidates = []
        hard_sl = entry * (1.0 + hard_sl_pct / 100.0)
        stop_candidates.append((hard_sl, "trend_hard_sl" if trend_active else "hard_sl"))
        if profit_pct >= be_trigger_pct or st.get("be_armed", False):
            st["be_armed"] = True
            stop_candidates.append((entry * (1.0 - BE_OFFSET_PCT / 100.0), "break_even"))
        if pct_profit_by_side("short", entry, lowest) >= trail_trigger_pct or st.get("trail_armed", False):
            st["trail_armed"] = True
            stop_candidates.append((lowest * (1.0 + trail_distance_pct / 100.0), "trend_trailing" if trend_active else "trailing"))
        active_stop, reason = min(stop_candidates, key=lambda x: x[0])
    else:
        highest = max(float(st.get("highest_price", entry)), price)
        st["highest_price"] = highest
        profit_pct = pct_profit_by_side("long", entry, price)
        stop_candidates = []
        hard_sl = entry * (1.0 - hard_sl_pct / 100.0)
        stop_candidates.append((hard_sl, "trend_hard_sl" if trend_active else "hard_sl"))
        if profit_pct >= be_trigger_pct or st.get("be_armed", False):
            st["be_armed"] = True
            stop_candidates.append((entry * (1.0 + BE_OFFSET_PCT / 100.0), "break_even"))
        if pct_profit_by_side("long", entry, highest) >= trail_trigger_pct or st.get("trail_armed", False):
            st["trail_armed"] = True
            stop_candidates.append((highest * (1.0 - trail_distance_pct / 100.0), "trend_trailing" if trend_active else "trailing"))
        active_stop, reason = max(stop_candidates, key=lambda x: x[0])

    st["active_stop_price"] = active_stop
    st["active_stop_reason"] = reason


def open_position(symbol: str, side: str, price: float, source: str, tf: str) -> Tuple[Dict, int]:
    st = STATE[symbol]
    st["inflight"] = True
    try:
        if side == "long" and not ENABLE_LONGS:
            notify_skip("longs_disabled", "long_entry", symbol, price, tf, source)
            return jsonify({"ok": True, "skip": "longs_disabled"}), 200
        if side == "short" and not ENABLE_SHORTS:
            notify_skip("shorts_disabled", "short_entry", symbol, price, tf, source)
            return jsonify({"ok": True, "skip": "shorts_disabled"}), 200
        if side == "short" and not SIMULATE and not ALLOW_LIVE_SHORTS:
            notify_skip("live_shorts_blocked", "short_entry", symbol, price, tf, source)
            return jsonify({"ok": True, "skip": "live_shorts_blocked"}), 200

        trade_usd, savings_usd = trade_and_savings_usd(symbol)
        size_factor = clamp(float(st.pop("next_size_factor", 1.0)), 0.05, 1.0)
        margin_usd = trade_usd * size_factor
        notional_usd = margin_usd * max(1.0, LEVERAGE)
        if margin_usd <= 0 or price <= 0:
            notify_skip("invalid_budget_or_price", f"{side}_entry", symbol, price, tf, source, f"margin={margin_usd}")
            return jsonify({"ok": True, "skip": "invalid_budget_or_price"}), 200

        if SIMULATE:
            entry = effective_entry_price(side, price)
            entry_fee = notional_usd * PAPER_FEE_PCT / 100.0
            qty = notional_usd / entry
            st["in_position"] = True
            st["position_side"] = side
            st["entry_price"] = entry
            st["qty"] = qty
            st["invested_usd"] = margin_usd
            st["notional_usd"] = notional_usd
            st["entry_fee_usd"] = entry_fee
            st["highest_price"] = entry
            st["lowest_price"] = entry
            st["be_armed"] = False
            st["trail_armed"] = False
            st["opened_ts"] = time.time()
            trend_ctx = trend_context(symbol, side, entry) if TREND_RUNNER_ENABLED else {"ok": False, "reason": "disabled"}
            st["trend_runner_active"] = bool(trend_ctx.get("ok", False))
            st["trend_runner_reason"] = trend_ctx.get("reason", "")
            hard_init = TREND_HARD_SL_PCT if st["trend_runner_active"] else HARD_SL_PCT
            st["active_stop_price"] = entry * (1 - hard_init / 100.0) if side == "long" else entry * (1 + hard_init / 100.0)
            st["active_stop_reason"] = "trend_hard_sl" if st["trend_runner_active"] else "hard_sl"
            st["trend_tp1_done"] = False
            st["last_size_factor"] = size_factor
            _dbg(f"[PAPER {side.upper()} OPEN] {symbol} qty={qty} entry={entry} margin={margin_usd} notional={notional_usd} trend_runner={st['trend_runner_active']}")
            send_tg(tg_open_msg(symbol, side, entry, qty, margin_usd, source))
            if st["trend_runner_active"] and TREND_NOTIFY:
                send_tg(f"🚀 Trend Runner actief\nSide: {side.upper()} {sym_label(symbol)}\nTF: {TREND_TF}\nReden: {st['trend_runner_reason']}\nTP1: {TREND_TP1_PCT:.2f}% / {TREND_TP1_SELL_PCT:.0f}%\nTrail: {TREND_TRAIL_TRIGGER_PCT:.2f}%/{TREND_TRAIL_DISTANCE_PCT:.2f}%\nMax hold: {TREND_MAX_HOLD_HOURS:g} uur")
        else:
            # Live long spot kan, maar live short/futures bewust niet in deze versie.
            if side != "long":
                return jsonify({"ok": True, "skip": "live_short_not_implemented"}), 200
            qty = notional_usd / price
            order = ex.create_market_buy_order(symbol, qty)
            _dbg(f"[LIVE LONG OPEN] {symbol} id={order.get('id')} qty={qty} price={price}")
            st["in_position"] = True
            st["position_side"] = "long"
            st["entry_price"] = price
            st["qty"] = qty
            st["invested_usd"] = margin_usd
            st["notional_usd"] = notional_usd
            st["highest_price"] = price
            st["lowest_price"] = price
            send_tg(tg_open_msg(symbol, side, price, qty, margin_usd, source))

        TRADE_LOG.append({
            "ts": time.time(), "mode": "paper" if SIMULATE else "live", "action": f"{side}_entry",
            "symbol": symbol, "price_usd": float(st["entry_price"]), "qty": float(st["qty"]),
            "invested_usd": float(st["invested_usd"]), "notional_usd": float(st.get("notional_usd", st["invested_usd"])),
            "source": source, "tf": tf,
        })
        _save_state_file()
        return jsonify({"ok": True, "state": STATE[symbol]}), 200
    except Exception as e:
        _dbg(f"[OPEN ERROR] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        st["inflight"] = False



def partial_close_position(symbol: str, side: str, price: float, pct: float, source: str, tf: str) -> bool:
    """Paper-only gedeeltelijke winstname voor Trend Runner TP1."""
    st = STATE.get(symbol, {})
    if not SIMULATE or not st.get("in_position", False):
        return False
    if st.get("position_side", "long") != side:
        return False

    pct = clamp(float(pct), 1.0, 99.0)
    qty = float(st.get("qty", 0.0))
    entry = float(st.get("entry_price", 0.0))
    notional_entry = float(st.get("notional_usd", qty * entry))
    entry_fee = float(st.get("entry_fee_usd", 0.0))
    invested_usd = float(st.get("invested_usd", 0.0))
    if qty <= 0 or entry <= 0 or price <= 0 or notional_entry <= 0:
        return False

    frac = pct / 100.0
    qty_close = qty * frac
    entry_notional_close = notional_entry * frac
    entry_fee_close = entry_fee * frac
    exit_price = effective_exit_price(side, price)
    exit_notional = qty_close * exit_price
    exit_fee = exit_notional * PAPER_FEE_PCT / 100.0
    gross_pnl = (entry_notional_close - exit_notional) if side == "short" else (exit_notional - entry_notional_close)
    pnl = gross_pnl - entry_fee_close - exit_fee

    trade_usd, savings_usd = trade_and_savings_usd(symbol)
    target_trade = float(st.get("target_trade_usd", trade_usd))
    total_capital = trade_usd + savings_usd + pnl
    st["trade_usd"] = min(target_trade, total_capital) if total_capital > 0 else 0.0
    st["savings_usd"] = max(0.0, total_capital - st["trade_usd"]) if total_capital > 0 else 0.0
    st["realized_pnl_usd"] = float(st.get("realized_pnl_usd", 0.0)) + float(pnl)

    st["qty"] = max(0.0, qty - qty_close)
    st["notional_usd"] = max(0.0, notional_entry - entry_notional_close)
    st["entry_fee_usd"] = max(0.0, entry_fee - entry_fee_close)
    st["invested_usd"] = max(0.0, invested_usd * (1.0 - frac))

    TRADE_LOG.append({
        "ts": time.time(), "mode": "paper", "action": f"{side}_partial_exit",
        "symbol": symbol, "price_usd": float(exit_price), "qty": float(qty_close),
        "pnl_usd": float(pnl), "source": source, "tf": tf, "pct": pct,
    })
    _dbg(f"[PAPER {side.upper()} PARTIAL] {symbol} pct={pct} qty={qty_close} exit={exit_price} gross_pnl={gross_pnl} fees={entry_fee_close+exit_fee} pnl={pnl}")
    send_tg("\n".join([
        f"{BOT_TITLE}",
        f"✅ [{sym_label(symbol)}] TREND TP1 {side.upper()}",
        f"Deel gesloten: {pct:.0f}%",
        f"Exitprijs: {fmt_usd(exit_price, 4)}",
        f"Resultaat deel: {fmt_eur(eur_rate() * pnl)}",
        f"Resterende qty: {st['qty']:,.6f}",
        f"Bron: {source}",
        f"Tijd: {fmt_dt(local_now())}",
    ]))
    _save_state_file()
    return True


def should_ignore_tv_exit_for_trend(symbol: str, side: str, price: float, source: str) -> Tuple[bool, str]:
    st = STATE.get(symbol, {})
    if not (TREND_RUNNER_ENABLED and TREND_IGNORE_TV_EXIT):
        return False, "trend_ignore_disabled"
    if not st.get("trend_runner_active", False):
        return False, "trend_runner_not_active"
    if not is_tradingview_source(source):
        return False, "not_tradingview_source"
    if position_age_hours(st) >= TREND_MAX_HOLD_HOURS:
        return False, "trend_max_hold_reached"
    entry = float(st.get("entry_price", 0.0))
    profit_pct = pct_profit_by_side(side, entry, price)
    if profit_pct <= 0:
        return False, f"not_in_profit:{profit_pct:.2f}%"
    ctx = trend_context(symbol, side, price)
    if ctx.get("ok") and not ctx.get("exit_broken"):
        return True, f"trend_intact profit={profit_pct:.2f}% {ctx.get('reason')}"
    return False, f"trend_exit_allowed profit={profit_pct:.2f}% {ctx.get('reason')}"

def close_position(symbol: str, requested_side: str, price: float, source: str, tf: str) -> Tuple[Dict, int]:
    st = STATE[symbol]
    if not st.get("in_position", False):
        _dbg(f"[CLOSE] skip {symbol} no position")
        notify_skip("no_position", f"{requested_side}_exit", symbol, price, tf, source)
        return jsonify({"ok": True, "skip": "no_position"}), 200
    side = st.get("position_side", "long")
    if requested_side != side:
        _dbg(f"[CLOSE] skip {symbol} side mismatch requested={requested_side} actual={side}")
        notify_skip("side_mismatch", f"{requested_side}_exit", symbol, price, tf, source, f"actual_side={side}")
        return jsonify({"ok": True, "skip": "side_mismatch", "actual_side": side}), 200

    ignore_exit, ignore_reason = should_ignore_tv_exit_for_trend(symbol, side, price, source)
    if ignore_exit:
        _dbg(f"[TREND] ignore TV exit {symbol} side={side} reason={ignore_reason}")
        if TREND_NOTIFY:
            send_tg(f"🚀 Trend Runner houdt positie vast\nSignaal: {side.upper()} EXIT genegeerd\nSymbol: {sym_label(symbol)}\nPrijs: {fmt_usd(price, 2)}\nReden: {ignore_reason}")
        _save_state_file()
        return jsonify({"ok": True, "skip": "trend_runner_ignore_exit", "reason": ignore_reason}), 200

    st["inflight"] = True
    try:
        qty = float(st.get("qty", 0.0))
        entry = float(st.get("entry_price", 0.0))
        notional_entry = float(st.get("notional_usd", qty * entry))
        entry_fee = float(st.get("entry_fee_usd", 0.0))
        if qty <= 0 or entry <= 0 or price <= 0:
            notify_skip("invalid_position", f"{requested_side}_exit", symbol, price, tf, source, f"qty={qty} entry={entry}")
            return jsonify({"ok": True, "skip": "invalid_position"}), 200

        if SIMULATE:
            exit_price = effective_exit_price(side, price)
            exit_notional = qty * exit_price
            exit_fee = exit_notional * PAPER_FEE_PCT / 100.0
            if side == "short":
                gross_pnl = notional_entry - exit_notional
            else:
                gross_pnl = exit_notional - notional_entry
            pnl = gross_pnl - entry_fee - exit_fee
            _dbg(f"[PAPER {side.upper()} CLOSE] {symbol} qty={qty} exit={exit_price} gross_pnl={gross_pnl} fees={entry_fee+exit_fee} pnl={pnl}")
        else:
            if side != "long":
                return jsonify({"ok": True, "skip": "live_short_not_implemented"}), 200
            order = ex.create_market_sell_order(symbol, qty)
            _dbg(f"[LIVE LONG CLOSE] {symbol} id={order.get('id')} qty={qty} price={price}")
            exit_price = price
            exit_notional = qty * exit_price
            fee = exit_notional * 0.001
            pnl = exit_notional - notional_entry - fee
            exit_fee = fee

        trade_usd, savings_usd = trade_and_savings_usd(symbol)
        prev_trade_usd, prev_savings_usd = trade_usd, savings_usd
        target_trade = float(st.get("target_trade_usd", trade_usd))
        total_capital = trade_usd + savings_usd + pnl
        if total_capital <= 0:
            new_trade, new_savings = 0.0, 0.0
        else:
            new_trade = min(target_trade, total_capital)
            new_savings = total_capital - new_trade

        st["trade_usd"] = new_trade
        st["savings_usd"] = new_savings
        st["realized_pnl_usd"] = float(st.get("realized_pnl_usd", 0.0)) + float(pnl)

        send_tg(tg_close_msg(symbol, side, exit_price, qty, pnl, prev_trade_usd, prev_savings_usd, source))

        TRADE_LOG.append({
            "ts": time.time(), "mode": "paper" if SIMULATE else "live", "action": f"{side}_exit",
            "symbol": symbol, "price_usd": float(exit_price), "qty": float(qty), "pnl_usd": float(pnl),
            "source": source, "tf": tf,
        })

        # reset position fields
        for k, v in {
            "in_position": False, "position_side": "none", "entry_price": 0.0, "qty": 0.0,
            "invested_usd": 0.0, "notional_usd": 0.0, "entry_fee_usd": 0.0,
            "highest_price": 0.0, "lowest_price": 0.0, "be_armed": False, "trail_armed": False,
            "active_stop_price": 0.0, "active_stop_reason": "", "trend_runner_active": False,
            "trend_runner_reason": "", "trend_tp1_done": False, "opened_ts": 0.0, "last_action_ts": time.time()
        }.items():
            st[k] = v
        _save_state_file()
        return jsonify({"ok": True, "state": STATE[symbol]}), 200
    except Exception as e:
        _dbg(f"[CLOSE ERROR] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        st["inflight"] = False


# ---------------- loops ----------------

def _tpsl_monitor_loop():
    while True:
        try:
            time.sleep(max(5.0, TPSL_POLL_S))
            if not BOT_TPSL_ENABLED or ex is None:
                continue
            symbol = SYMBOL
            st = STATE.get(symbol, {})
            if not st.get("in_position", False) or st.get("inflight", False):
                continue
            price = fetch_public_price(symbol)
            if price <= 0:
                continue
            side = st.get("position_side", "long")
            entry = float(st.get("entry_price", 0.0) or 0.0)
            profit_pct = pct_profit_by_side(side, entry, price) if entry > 0 else 0.0

            if st.get("trend_runner_active", False):
                if position_age_hours(st) >= TREND_MAX_HOLD_HOURS:
                    _dbg(f"[TREND] max hold trigger {symbol} side={side} age_h={position_age_hours(st):.2f}")
                    with app.app_context():
                        close_position(symbol, side, price, source="bot_trend_max_hold", tf="bot")
                    continue
                if not st.get("trend_tp1_done", False) and profit_pct >= TREND_TP1_PCT:
                    ok = partial_close_position(symbol, side, price, TREND_TP1_SELL_PCT, source="bot_trend_tp1", tf="bot")
                    if ok:
                        st["trend_tp1_done"] = True
                        st["be_armed"] = True
                        _save_state_file()

            update_trade_protection_state(symbol, price)
            stop = float(st.get("active_stop_price", 0.0))
            reason = st.get("active_stop_reason", "")
            if stop <= 0:
                continue
            triggered = (price <= stop) if side == "long" else (price >= stop)
            if triggered:
                _dbg(f"[TPSL] trigger {symbol} side={side} reason={reason} price={price} stop={stop}")
                # close_position returns jsonify responses; background threads need an app context.
                with app.app_context():
                    close_position(symbol, side, price, source=f"bot_tpsl_{reason}", tf="bot")
        except Exception as e:
            _dbg(f"[TPSL] loop error: {e}")
            time.sleep(10)


def _daily_report_loop():
    while True:
        try:
            hhmm = os.getenv("DAILY_REPORT_HHMM", "23:59")
            hh, mm = map(int, hhmm.split(":"))
            now = local_now()
            next_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now > next_run:
                next_run += timedelta(days=1)
            time.sleep((next_run - now).total_seconds())
            _dbg("[REPORT] Daily report tick")
        except Exception as e:
            _dbg(f"[REPORT] Loop error: {e}")
            time.sleep(3600)


# ---------------- routes ----------------

@app.route("/", methods=["GET"])
def home():
    st = STATE.get(SYMBOL, {})
    return jsonify({
        "status": "ok",
        "symbols": SYMBOLS,
        "simulate": SIMULATE,
        "enable_longs": ENABLE_LONGS,
        "enable_shorts": ENABLE_SHORTS,
        "allow_live_shorts": ALLOW_LIVE_SHORTS,
        "paper_fee_pct": PAPER_FEE_PCT,
        "paper_slippage_pct": PAPER_SLIPPAGE_PCT,
        "paper_leverage": LEVERAGE,
        "position": {
            "in_position": st.get("in_position", False),
            "side": st.get("position_side", "none"),
            "entry_price": st.get("entry_price", 0),
            "qty": st.get("qty", 0),
            "active_stop_price": st.get("active_stop_price", 0),
            "active_stop_reason": st.get("active_stop_reason", ""),
        },
        "bot_tpsl_enabled": BOT_TPSL_ENABLED,
        "supervisor_enabled": SUPERVISOR_ENABLED,
        "supervisor_min_score": SUPERVISOR_MIN_SCORE,
        "supervisor_min_short_score": SUPERVISOR_MIN_SHORT_SCORE,
        "supervisor_dynamic_size": SUPERVISOR_DYNAMIC_SIZE,
        "webhook_notify_skips": WEBHOOK_NOTIFY_SKIPS,
        "webhook_notify_received": WEBHOOK_NOTIFY_RECEIVED,
        "price_feed": "direct_mexc_ticker_price_then_avgPrice_then_ccxt",
        "price_cache_max_age_s": env_float("PRICE_CACHE_MAX_AGE_S", "45"),
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


@app.route("/config", methods=["GET"])
def config():
    return jsonify({
        "symbols": SYMBOLS,
        "budgets": BUDGET_USDT,
        "simulate": SIMULATE,
        "enable_longs": ENABLE_LONGS,
        "enable_shorts": ENABLE_SHORTS,
        "allow_tfs": {SYMBOL: sorted(list(allowed_tfs_for(SYMBOL)))},
        "bot_tpsl_enabled": BOT_TPSL_ENABLED,
        "supervisor_enabled": SUPERVISOR_ENABLED,
        "webhook_notify_skips": WEBHOOK_NOTIFY_SKIPS,
    }), 200


@app.route("/envcheck", methods=["GET"])
def envcheck():
    missing = []
    if not SIMULATE:
        if not MEXC_API_KEY:
            missing.append("MEXC_API_KEY")
        if not MEXC_API_SECRET:
            missing.append("MEXC_API_SECRET")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        missing.append("TELEGRAM_CHAT_ID")
    return jsonify({"missing": missing}), 200


@app.route("/test/send", methods=["GET", "POST"])
def test_send():
    send_tg("🧪 Test message from BTC long/short paper bot")
    return jsonify({"ok": True}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True)
    _dbg(f"[SIGDBG] /webhook hit ct={request.content_type} raw='{raw[:300]}...'")
    try:
        payload = json.loads(raw)
        _dbg("[PARSE] JSON loaded")
    except Exception:
        _dbg("[SIGDBG] bad_json")
        notify_skip("bad_json", extra=raw[:200])
        return jsonify({"ok": True, "skip": "bad_json"}), 200

    action = normalize_action(payload.get("action") or "")
    raw_action = payload.get("action") or ""
    if action not in ("long_entry", "long_exit", "short_entry", "short_exit"):
        _dbg(f"[SKIP] Invalid action '{action}'")
        notify_skip("invalid_action", raw_action, extra=f"normalized={action}")
        return jsonify({"ok": True, "skipped": "invalid_action", "action": action}), 200

    symbol = parse_symbol(payload.get("symbol") or "")
    if symbol != SYMBOL:
        _dbg(f"[SKIP] Unknown symbol '{symbol}' raw='{payload.get('symbol')}'")
        notify_skip("unknown_symbol", action, symbol, extra=f"raw_symbol={payload.get('symbol')}")
        return jsonify({"ok": True, "skip": "unknown_symbol"}), 200

    tf = normalize_tf(payload.get("tf") or "")
    try:
        if tf not in allowed_tfs_for(symbol):
            _dbg(f"[TF FILTER] skip {symbol} tf={tf} not allowed")
            notify_skip("tf_not_allowed", action, symbol, 0.0, tf, payload.get("source", "unknown"), f"allowed={sorted(list(allowed_tfs_for(symbol)))}")
            return jsonify({"ok": True, "skip": "tf_not_allowed", "tf": tf}), 200
    except Exception as e:
        _dbg(f"[TF FILTER] warn: {e}")

    try:
        price = float(payload.get("price") or 0.0)
    except Exception:
        price = 0.0
    if price <= 0:
        try:
            price = fetch_public_price(symbol)
        except Exception as e:
            _dbg(f"[PRICE] webhook fallback failed: {e}")
            notify_skip("invalid_price", action, symbol, price, tf, payload.get("source", "unknown"), str(e)[:180])
            return jsonify({"ok": True, "skip": "invalid_price"}), 200

    source = payload.get("source", "unknown")
    if WEBHOOK_NOTIFY_RECEIVED:
        send_tg(f"📩 Webhook ontvangen\nAction: {action}\nSymbol: {symbol}\nTF: {tf}\nPrijs: {fmt_usd(price,2)}\nBron: {source}\nTijd: {fmt_dt(local_now())}")
    side = action_side(action)
    now = time.time()

    st = STATE.setdefault(symbol, {})
    _ensure_wallet(symbol)

    if now - float(st.get("last_action_ts", 0)) < STRICT_DEDUP_S:
        notify_skip("dedup", action, symbol, price, tf, source)
        return jsonify({"ok": True, "skip": "dedup"}), 200

    if PER_BAR_LOCK or (is_entry(action) and PER_BAR_LOCK_BUY) or (is_exit(action) and PER_BAR_LOCK_SELL):
        bar_time = int(now / 300) * 300
        if bar_time == st.get("last_bar_time", 0):
            notify_skip("bar_lock", action, symbol, price, tf, source)
            return jsonify({"ok": True, "skip": "bar_lock"}), 200
        st["last_bar_time"] = bar_time

    if st.get("inflight", False):
        notify_skip("inflight", action, symbol, price, tf, source)
        return jsonify({"ok": True, "skip": "inflight"}), 200

    if is_entry(action) and st.get("in_position", False):
        notify_skip("already_in_position", action, symbol, price, tf, source, f"side={st.get('position_side')}")
        return jsonify({"ok": True, "skip": "already_in_position", "side": st.get("position_side")}), 200

    if now - float(st.get("last_action_ts", 0)) < MIN_TRADE_COOLDOWN_S:
        notify_skip("cooldown", action, symbol, price, tf, source)
        return jsonify({"ok": True, "skip": "cooldown"}), 200

    if is_entry(action):
        sup_allow, sup_reason, sup_score, sup_size = supervisor_decision(action, symbol, price, payload)
        st["last_supervisor"] = {
            "ts": time.time(), "allow": bool(sup_allow), "reason": sup_reason,
            "score": float(sup_score), "size_factor": float(sup_size),
        }
        st["next_size_factor"] = float(sup_size)
        if not sup_allow:
            _dbg(f"[SUPERVISOR] skip {symbol} action={action} reason={sup_reason}")
            notify_skip("supervisor", action, symbol, price, tf, source, f"score={sup_score:.2f} {sup_reason}")
            _save_state_file()
            return jsonify({"ok": True, "skip": "supervisor", "reason": sup_reason, "score": sup_score}), 200

    st["last_action_ts"] = now

    if is_entry(action):
        return open_position(symbol, side, price, source=source, tf=tf)
    return close_position(symbol, side, price, source=source, tf=tf)


# ---------------- main ----------------

if __name__ == "__main__":
    _dbg(f"[CONF] SIMULATE={SIMULATE} ENABLE_LONGS={ENABLE_LONGS} ENABLE_SHORTS={ENABLE_SHORTS} REHYDRATE={REHYDRATE_ENABLED}")
    _load_state_file()
    try:
        init_exchange()
    except Exception as e:
        _dbg(f"[WARMUP] MEXC init error: {e}")
    rehydrate_positions()
    _dbg(f"✅ Webhook server op http://0.0.0.0:{PORT}/webhook — symbol: {SYMBOL}")
    _dbg(f"[CONF] budgets={BUDGET_USDT}")
    try:
        Thread(target=_daily_report_loop, daemon=True).start()
        _dbg("[REPORT] daily scheduler started")
    except Exception as e:
        _dbg(f"[REPORT] scheduler warn: {e}")
    try:
        Thread(target=_tpsl_monitor_loop, daemon=True).start()
        _dbg(f"[TPSL] monitor started enabled={BOT_TPSL_ENABLED}")
    except Exception as e:
        _dbg(f"[TPSL] scheduler warn: {e}")

    if STARTUP_TG_ENABLED:
        try:
            send_tg(tg_startup_msg())
            _dbg("[STARTUP] Telegram startup message sent")
        except Exception as e:
            _dbg(f"[STARTUP] Telegram startup message error: {e}")

    app.run(host="0.0.0.0", port=PORT, debug=False)
