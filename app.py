
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
from datetime import datetime, timezone
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
    try:
        return symbol.split("/")[0]
    except Exception:
        return symbol.replace("/", "")

def eur_rate() -> float:
    """USD -> EUR conversion via ENV USD_TO_EUR (default 0.92)."""
    try:
        v = float(os.getenv("USD_TO_EUR", 0.92))
        return max(v, 0.0001)
    except Exception:
        return 0.92

def fmt_eur(x: float) -> str:
    """Format number as € with EU comma decimals."""
    try:
        s = f"{x:,.2f}"
        s = s.replace(",", "_").replace(".", ",").replace("_", ".")
        return f"€{s}"
    except Exception:
        return f"€{x:.2f}"

def fmt_usd(x: float, decimals: int = 4) -> str:
    return f"${x:.{decimals}f}"

def local_now():
    tzname = os.getenv("TIMEZONE", "Europe/Amsterdam")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz)

def fmt_dt(dt: datetime) -> str:
    tzname = os.getenv("TIMEZONE", "Europe/Amsterdam")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc
    return dt.astimezone(tz).strftime("%d-%m-%Y %H:%M:%S")

def env_first(names, default=""):
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v)
    return str(default)

def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return str(v) if v is not None else str(default)

def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)

def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        try:
            return int(float(os.getenv(name, default)))
        except Exception:
            return int(default)

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    return s in ("1","true","t","yes","y","on")

def _dbg(msg: str):
    print(f"[SIGDBG] {msg}", flush=True)

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def tv_to_ccxt(symbol_tv: str) -> str:
    s = (symbol_tv or "").upper().replace("MEXC:", "").replace("SPOT:", "").replace("PERP:", "")
    if "/" in s:
        return s
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT"
    return s


# --- dynamic symbols/budgets/tf allowlist helpers ---
def parse_symbols_env():
    raw = os.getenv("SYMBOLS", "")
    symbols = []
    for tok in raw.replace(";", ",").split(","):
        s = tok.strip().upper()
        if not s:
            continue
        if "/" not in s and s.endswith("USDT"):
            s = f"{s[:-4]}/USDT"
        symbols.append(s)
    return symbols

def make_budgets_for(symbols):
    out = {}
    for sym in symbols:
        base, quote = sym.split("/")
        basequote = (base + quote).upper()
        val = os.getenv(f"BUDGET_{base.upper()}")
        if val is None:
            val = os.getenv(f"BUDGET_{basequote}")
        try:
            out[sym] = float(val) if val is not None else 500.0
        except Exception:
            out[sym] = 500.0
    return out

def allowed_tfs_for(symbol_ccxt: str):
    if "/" in symbol_ccxt:
        base, quote = symbol_ccxt.split("/")
    else:
        base, quote = symbol_ccxt, ""
    basequote = (base + quote).upper() if quote else base.upper()
    keys = [f"ALLOW_TF_{base.upper()}", f"ALLOW_TF_{basequote}"]
    vals = []
    for k in keys:
        v = os.getenv(k)
        if v and str(v).strip() != "":
            vals.append(v)
    if not vals:
        return set()
    tfs = set()
    for v in vals:
        for item in str(v).replace(";",",").split(","):
            s = item.strip()
            if s:
                tfs.add(normalize_tf(s))
    return tfs
# ------------- config -------------
PORT = env_int("PORT", 10000)

SYMBOLS = parse_symbols_env() or ["XRP/USDT", "WLFI/USDT"]

BUDGET_USDT: Dict[str, float] = make_budgets_for(SYMBOLS)

TG_TOKEN   = env_first(["TG_TOKEN","TELEGRAM_BOT_TOKEN","BOT_TOKEN","TELEGRAM_TOKEN"])
TG_CHAT_ID = env_first(["TG_CHAT_ID","TELEGRAM_CHAT_ID","CHAT_ID"])

MEXC_RECVWINDOW_MS = env_int("MEXC_RECVWINDOW_MS", 10000)
CCXT_TIMEOUT_MS    = env_int("CCXT_TIMEOUT_MS", 10000)
COOLDOWN_S         = env_int("MIN_TRADE_COOLDOWN_S", 0)
DEDUP_WINDOW_S     = env_int("DEDUP_WINDOW_S", 20)
STRICT_DEDUP_S     = env_int("STRICT_DEDUP_S", 3)   # duplicates within N sec regardless of price/source
ENTRY_LOCK_S       = env_int("ENTRY_LOCK_S", 2)     # min seconds between 2 entries per symbol
PER_BAR_LOCK       = env_bool("PER_BAR_LOCK", True) or env_bool("BAR_LOCK", False)  # one action per bar
PER_BAR_LOCK_BUY   = env_bool("PER_BAR_LOCK_BUY", PER_BAR_LOCK) or env_bool("BAR_LOCK_BUY", False)
PER_BAR_LOCK_SELL  = env_bool("PER_BAR_LOCK_SELL", PER_BAR_LOCK) or env_bool("BAR_LOCK_SELL", False)

# ------------- telegram -------------

BOT_TITLE = env_str("BOT_TITLE", "Scalp_bot")
DAILY_REPORT_HHMM = env_str("DAILY_REPORT_HHMM", "23:59")
def send_tg(text_html: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        _dbg("[TG] missing TG_TOKEN or TG_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": text_html, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=10)
            if r.status_code == 200:
                _dbg("[TG] OK sent")
                return True
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "2"))
                _dbg(f"[TG] 429 rate-limited; retry_after={retry_after}s")
                time.sleep(min(retry_after, 5))
                continue
            _dbg(f"[TG] FAIL {r.status_code}: {r.text[:200]}")
        except requests.exceptions.Timeout:
            _dbg("[TG] timeout, retrying...")
            time.sleep(1 + attempt)
        except Exception as e:
            _dbg(f"[TG] error: {e}")
            time.sleep(1 + attempt)
    return False


# --- Telegram message builders ---
def tg_buy_msg(symbol: str, price_usd: float, qty: float, invested_usd: float, source: str, tf: str) -> str:
    budget_eur = eur_rate() * float(BUDGET_USDT.get(symbol, 0.0))
    spaar_eur = savings_for(symbol)
    total_eur = budget_eur + spaar_eur
    now_str = fmt_dt(local_now())
    sym_ccxt = sym_label(symbol)
    lines = [
        f"{BOT_TITLE}",
        f"🟢 [{sym_ccxt}] AANKOOP",
        f"📹 Koopprijs: {fmt_usd(price_usd, 4)}",
        f"🧠 Signaalbron: {source or 'TV'}",
        f"💰 Handelssaldo: {fmt_eur(budget_eur)}",
        f"💼 Spaarrekening: {fmt_eur(spaar_eur)}",
        f"📈 Totale waarde: {fmt_eur(total_eur)}",
        f"🔐 Tradebedrag: {fmt_eur(eur_rate() * invested_usd)}",
        f"🔗 Tijd: {now_str}",
    ]
    return "\n".join(lines)

def tg_sell_msg(symbol: str, price_usd: float, qty: float, net_out_usd: float, pnl_usd: float) -> str:
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

def tg_day_report_text(day_dt: datetime) -> str:
    tzname = os.getenv("TIMEZONE", "Europe/Amsterdam")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc
    # Filter trades for the given local date
    day = day_dt.astimezone(tz).date()
    trades = []
    for t in TRADE_LOG:
        try:
            ts = datetime.fromtimestamp(float(t.get("ts", 0.0)), tz=timezone.utc).astimezone(tz)
            if ts.date() == day:
                trades.append(t)
        except Exception:
            pass
    # Aggregate per symbol
    per = {}
    for t in trades:
        if str(t.get("action", "")).lower() != "sell":
            continue
        sym = t.get("symbol")
        pnl_eur = eur_rate() * float(t.get("pnl_usd", 0.0))
        ok = per.get(sym) or {"trades":0,"pnl_eur":0.0,"wins":0,"best":-1e18,"worst":1e18}
        ok["trades"] += 1
        ok["pnl_eur"] += pnl_eur
        if pnl_eur >= 0: ok["wins"] += 1
        ok["best"] = max(ok["best"], pnl_eur)
        ok["worst"] = min(ok["worst"], pnl_eur)
        per[sym] = ok

    # Build lines
    dstr = day_dt.astimezone(tz).strftime("%d-%m-%Y")
    lines = [f"📊 Dagrapport — {dstr}"]
    total_trades = 0
    total_pnl = 0.0
    best_sym = None; best_val = -1e18
    worst_sym = None; worst_val = 1e18

    for sym in SYMBOLS:
        stat = per.get(sym, {"trades":0,"pnl_eur":0.0,"wins":0})
        winrate = int(round(100.0 * (stat["wins"]/stat["trades"]) if stat["trades"]>0 else 0.0))
        sym_ccxt = sym_label(sym)
        pnl_str = f"＋{fmt_eur(stat['pnl_eur'])}" if stat["pnl_eur"] >= 0 else fmt_eur(stat["pnl_eur"])
        lines.append(f"{sym_ccxt}: {stat['trades']} trades • {pnl_str} • winrate {winrate}%")
        total_trades += stat["trades"]
        total_pnl += stat["pnl_eur"]
        if stat["trades"]>0:
            best_val = max(best_val, stat.get("best", -1e18)); 
            worst_val = min(worst_val, stat.get("worst", 1e18))
            if best_val == stat.get("best", -1e18): best_sym = sym_ccxt
            if worst_val == stat.get("worst", 1e18): worst_sym = sym_ccxt

    lines.append("——————————————")
    wr_total = 0
    wins_total = sum(per[s]["wins"] for s in per)
    if total_trades>0:
        wr_total = int(round(100.0 * wins_total/total_trades))
    total_str = f"{'＋' if total_pnl>=0 else ''}{fmt_eur(total_pnl)}"
    lines.append(f"Totaal: {total_trades} trades • {total_str} • winrate {wr_total}%")
    if best_sym is not None and worst_sym is not None:
        best_line = f"Beste: {best_sym} {'＋' if best_val>=0 else ''}{fmt_eur(best_val)}"
        worst_line = f"Slechtste: {worst_sym} {'＋' if worst_val>=0 else ''}{fmt_eur(worst_val)}"
        lines.append(f"{best_line}  •  {worst_line}")
    lines.append("——————————————")
    # Balances overview
    for sym in SYMBOLS:
        budget_eur = eur_rate() * float(BUDGET_USDT.get(sym, 0.0))
        spaar_eur = savings_for(sym)
        sym_short = sym_label(sym)
        lines.append(f"{sym_short} saldo: Handel {fmt_eur(budget_eur)} • Spaar {fmt_eur(spaar_eur)}")
    totvermogen = sum(eur_rate()*float(BUDGET_USDT.get(s,0.0)) + savings_for(s) for s in SYMBOLS)
    lines.append(f"Totaal vermogen: {fmt_eur(totvermogen)}")
    return "\n".join(lines)
# ------------- exchange -------------
_EX = None
def mexc() -> ccxt.mexc:
    global _EX
    if _EX:
        return _EX
    ak = env_first(["MEXC_API_KEY","MEXC_KEY"])
    sk = env_first(["MEXC_API_SECRET","MEXC_SECRET"])
    if not ak or not sk:
        raise RuntimeError("MEXC_API_KEY / MEXC_API_SECRET ontbreken")
    _EX = ccxt.mexc({
        "apiKey": ak,
        "secret": sk,
        "timeout": CCXT_TIMEOUT_MS,
        "enableRateLimit": False,
        "options": {
            "defaultType": "spot",
            "recvWindow": MEXC_RECVWINDOW_MS,
            "adjustForTimeDifference": True,
        }
    })
    try:
        _EX.load_markets()
        try:
            _EX.load_time_difference()
        except Exception as e:
            _dbg(f"[MEXC] load_time_difference warn: {e}")
    except Exception as e:
        _dbg(f"[MEXC] load_markets error: {e}")
    return _EX

# ------------- position state -------------
SYMBOL_LOCKS: Dict[str, Lock] = {s: Lock() for s in SYMBOLS}
last_signal_key_ts: Dict[Tuple[str, str, str, float, str], float] = {}
last_minimal_ts: Dict[Tuple[str, str], float] = {}
LAST_BAR: Dict[Tuple[str, str, str], str] = {}  # (symbol, action, tf) -> bartime

STATE: Dict[str, Dict[str, Any]] = {
    s: {
        "in_position": False,
        "entry_price": 0.0,
        "pos_amount": 0.0,
        "pos_quote": 0.0,
        "last_action_ts": 0.0,
        "last_entry_ts": 0.0,
        "inflight": False,
    } for s in SYMBOLS
}


# --- Savings + Trade log (persisted to JSON) ---
SAVINGS_EUR: Dict[str, float] = {}
TRADE_LOG: List[Dict[str, Any]] = []
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

def _load_state_file():
    global SAVINGS_EUR, TRADE_LOG
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            SAVINGS_EUR = {k: float(v) for k, v in (data.get("savings_eur") or {}).items()}
            TRADE_LOG[:] = data.get("trade_log") or []
            _dbg(f"[STATE] loaded {STATE_FILE} (savings={len(SAVINGS_EUR)}, trades={len(TRADE_LOG)})")
    except Exception as e:
        _dbg(f"[STATE] load warn: {e}")

def _save_state_file():
    try:
        data = {"savings_eur": SAVINGS_EUR, "trade_log": TRADE_LOG[-2000:]}
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        _dbg(f"[STATE] save warn: {e}")

def savings_for(symbol: str) -> float:
    return float(SAVINGS_EUR.get(symbol, 0.0))

def add_savings(symbol: str, delta_eur: float):
    if abs(delta_eur) <= 1e-12:
        return
    SAVINGS_EUR[symbol] = float(SAVINGS_EUR.get(symbol, 0.0) + delta_eur)
    if SAVINGS_EUR[symbol] < 0:
        SAVINGS_EUR[symbol] = 0.0
    _save_state_file()
def _round_amount(ex: ccxt.Exchange, symbol: str, qty: float) -> float:
    try:
        return float(ex.amount_to_precision(symbol, qty))
    except Exception:
        return float(round(qty, 6))

def _order_breakdown(ex: ccxt.Exchange, symbol: str, order, side: str):
    filled = float(order.get("filled") or 0.0)
    avg = float(order.get("average") or order.get("price") or 0.0)
    cost = float(order.get("cost") or 0.0)
    fee_q = 0.0
    for f in (order.get("fees") or []):
        try:
            if (f.get("currency") or "").upper() == "USDT":
                fee_q += float(f.get("cost") or 0.0)
        except Exception:
            pass
    gross_q = cost if cost > 0 else (avg * filled if (avg and filled) else 0.0)
    net_q = max(gross_q - fee_q, 0.0)
    return avg, filled, gross_q, fee_q, net_q

def _poll_fill(ex: ccxt.Exchange, symbol: str, order_id: str, timeout_s: float = 6.0):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        try:
            o = ex.fetch_order(order_id, symbol)
            last = o
            if o.get("status") in ("closed", "canceled") or float(o.get("filled") or 0) > 0:
                return o
        except Exception as e:
            _dbg(f"[MEXC] fetch_order warn: {e}")
        time.sleep(0.5)
    return last or {"id": order_id}

def _ensure_spend_buy(ex: ccxt.Exchange, symbol: str, spend_usdt: float, price_hint: float = 0.0):
    params = {"recvWindow": MEXC_RECVWINDOW_MS}
    try:
        spend_prec = float(ex.price_to_precision(symbol, spend_usdt))
    except Exception:
        spend_prec = round(spend_usdt, 6)
    params["quoteOrderQty"] = spend_prec
    params["cost"] = spend_prec
    try:
        o = ex.create_order(symbol, "market", "buy", None, None, params)
    except Exception as e:
        _dbg(f"[LIVE] cost-buy not supported, fallback qty: {e}")
        p = float(price_hint or 0.0)
        if p <= 0:
            try:
                t = ex.fetch_ticker(symbol)
                p = float(t.get("last") or t.get("close") or 0.0)
            except Exception:
                p = 0.0
        if p <= 0:
            raise
        qty = spend_usdt / max(p, 1e-12)
        qty = _round_amount(ex, symbol, qty)
        o = ex.create_order(symbol, "market", "buy", qty, None, {"recvWindow": MEXC_RECVWINDOW_MS})
    o2 = _poll_fill(ex, symbol, o.get("id"))
    return o2 or o

def _market_sell_all(ex: ccxt.Exchange, symbol: str, amount: float):
    amount = max(0.0, float(amount))
    if amount <= 0:
        return {}
    qty = _round_amount(ex, symbol, amount)
    o = ex.create_order(symbol, "market", "sell", qty, None, {"recvWindow": MEXC_RECVWINDOW_MS})
    o2 = _poll_fill(ex, symbol, o.get("id"))
    return o2 or o

# ------------- dedup / cooldown -------------
def is_duplicate_signal(action: str, source: str, price: float, tf: str, symbol: str) -> bool:
    ts = time.time()
    mini_key = (symbol, action)
    last_mini = last_minimal_ts.get(mini_key, 0.0)
    if ts - last_mini < STRICT_DEDUP_S:
        return True
    key = (symbol, action, source, round(float(price or 0.0), 4), tf or "")
    last_ts = last_signal_key_ts.get(key, 0.0)
    if ts - last_ts < DEDUP_WINDOW_S:
        return True
    last_signal_key_ts[key] = ts
    last_minimal_ts[mini_key] = ts
    return False

def blocked_by_cooldown(symbol: str, action: str) -> bool:
    if COOLDOWN_S <= 0:
        return False
    st = STATE[symbol]
    if action == "sell" and st.get("in_position"):
        return False
    last_ts = float(st.get("last_action_ts") or 0.0)
    return (time.time() - last_ts) < COOLDOWN_S

# ------------- startup rehydrate -------------
def rehydrate_positions() -> None:
    ex = mexc()
    try:
        bal = ex.fetch_balance(params={"recvWindow": MEXC_RECVWINDOW_MS})
    except Exception as e:
        _dbg(f"[WARMUP] balance error: {e}")
        return
    for sym in SYMBOLS:
        base = sym.split("/")[0]
        free = float(((bal.get("free") or {}).get(base)) or 0.0)
        if free > 0:
            STATE[sym]["in_position"] = True
            STATE[sym]["pos_amount"] = free
            try:
                t = ex.fetch_ticker(sym)
                last = float(t.get("last") or t.get("close") or 0.0)
            except Exception:
                last = 0.0
            STATE[sym]["entry_price"] = last
            STATE[sym]["pos_quote"] = round(free * last, 6)
            _dbg(f"[REHYDRATE] {sym} amt={free}, entry≈{STATE[sym]['entry_price']}, pos_quote≈{STATE[sym]['pos_quote']}")
        else:
            _dbg(f"[REHYDRATE] {sym} flat")

# ------------- flask -------------
app = Flask(__name__)

@app.route("/", methods=["GET", "HEAD"])
def root():
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    try:
        mexc()
        ok = True
    except Exception:
        ok = False
    return jsonify({"ok": ok, "time": now_iso(), "symbols": SYMBOLS}), 200

@app.route("/config", methods=["GET"])
def config():
    st = {k: dict(v) for k, v in STATE.items()}
    return jsonify({
        "symbols": SYMBOLS,
        "budget": BUDGET_USDT,
        "cooldown_s": COOLDOWN_S,
        "dedup_window_s": DEDUP_WINDOW_S,
        "strict_dedup_s": STRICT_DEDUP_S,
        "entry_lock_s": ENTRY_LOCK_S,
        "per_bar_lock": PER_BAR_LOCK,
        "per_bar_lock_buy": PER_BAR_LOCK_BUY,
        "per_bar_lock_sell": PER_BAR_LOCK_SELL,
        "bar_lock_key": "symbol+action+tf",
        "allowed_tfs": {s: sorted(list(allowed_tfs_for(s))) for s in SYMBOLS},
        "state": st,
    }), 200

@app.route("/envcheck", methods=["GET"])
def envcheck():
    has_tg_token = bool(TG_TOKEN)
    has_tg_chat  = bool(TG_CHAT_ID)
    ak = env_first(["MEXC_API_KEY","MEXC_KEY"])
    sk = env_first(["MEXC_API_SECRET","MEXC_SECRET"])
    return jsonify({
        "ok": True,
        "tg_token_present": has_tg_token,
        "tg_chat_present": has_tg_chat,
        "tg_chat_id_sample_len": len(TG_CHAT_ID) if has_tg_chat else 0,
        "mexc_key_present": bool(ak),
        "mexc_secret_present": bool(sk),
        "time": now_iso(),
    }), 200

@app.route("/test/send", methods=["GET"])
def test_send():
    ok = send_tg(f"✅ TG test — {now_iso()}")
    return jsonify({"ok": bool(ok)}), 200


@app.route("/report", methods=["GET"])
def report():
    # GET /report?date=YYYY-MM-DD (optional), local TZ
    d = request.args.get("date")
    tzname = os.getenv("TIMEZONE", "Europe/Amsterdam")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc
    if d:
        try:
            day_dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=tz)
        except Exception:
            return jsonify({"ok": False, "err": "bad date"}), 400
    else:
        day_dt = local_now()
    text = tg_day_report_text(day_dt)
    ok = send_tg(text)
    return jsonify({"ok": bool(ok), "text": text}), 200

def _daily_report_loop():
    tzname = os.getenv("TIMEZONE", "Europe/Amsterdam")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc
    last_sent_date = None
    hhmm = os.getenv("DAILY_REPORT_HHMM", DAILY_REPORT_HHMM)
    while True:
        try:
            now = datetime.now(tz)
            if now.strftime("%H:%M") == hhmm:
                if last_sent_date != now.date():
                    txt = tg_day_report_text(now)
                    send_tg(txt)
                    last_sent_date = now.date()
            time.sleep(20)
        except Exception as e:
            _dbg(f"[REPORT] loop warn: {e}")
            time.sleep(60)

@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True)
    _dbg(f"[SIGDBG] /webhook hit ct={request.content_type} raw='{raw[:100]}...'")
    
    fixed = ""
    for attempt in range(0, fixed):  # Je originele loop voor tolerant parse
        try:
            payload = json.loads(raw)
            _dbg(f"[PARSE] JSON loaded from tolerant parser")
            break
        except Exception:
            pass  # Je originele except-logic hier, als je wilt
    else:
        payload = {}
    
    if payload is None:
        _dbg("[SIGDBG] bad_json")
        return jsonify({"ok": True, "skip": "bad_json"}), 200
    
    action = payload.get("action", "").lower().strip()
    tv_symbol = str(payload.get("symbol") or "").upper().replace("/", "")
    tf_raw = payload.get("tf") or ""
    tf = normalize_tf(tf_raw)
    
    try:
        price = float(payload.get("price") or 0.0)
    except ValueError:
        _dbg(f"[WARN] Invalid price '{payload.get('price')}'; fallback to current")
        try:
            price = float(mexc.fetch_ticker('XRP/USDT')['last'])  # Live prijs fetch als fallback
        except Exception as e:
            _dbg(f"[WARN] Fetch ticker error: {e}; use 0")
            price = 0.0
    
    if action not in ["buy", "sell"]:
        _dbg(f"[SKIP] Invalid action '{action}'")
        return jsonify({"ok": True, "skipped": "invalid_action"}), 200
    
    # ... (je bestaande code voor sym_to_ccxt, TF allowlist, etc. – plak die hieronder)
    
    # Timeframe allowlist per symbol from ENV
    try:
        allowlist_tfs = allowed_tfs_for(tv_symbol)
        atfs = allowed_tfs_for(tv_symbol)
        if tf not in atfs:
            _dbg(f"[TF FILTER] skip {tv_symbol} tf={tf} not allowed ({', '.join(sorted(atfs))})")
            return jsonify({"ok": True, "skip": f"tf not allowed"}), 200
    except Exception as e:
        _dbg(f"[TF FILTER] warn: {e}")
    
    # ... (rest van je functie: sym_to_ccxt, buy/sell logic, etc.)
    
    return jsonify({"ok": True}), 200  # Je originele return

    action = str(payload.get("action") or "").lower().strip()
    tv_symbol = str(payload.get("symbol") or payload.get("SYMBOL") or "").upper().replace(" ", "")
    source = str(payload.get("source") or "")
    raw_tf = str(payload.get("tf") or payload.get("timeframe") or "")
    tf = normalize_tf(raw_tf)
try:
    price = float(payload.get("price") or 0.0)
except ValueError:
    _dbg(f"[WARN] Invalid price '{payload.get('price')}'; fallback to 0")
    price = 0.0  # Of: price = float(mexc.fetch_ticker('XRP/USDT')['last']) voor current price

# ... (je bestaande code voor action/symbol check)

action = payload.get("action", "").lower()
if action not in ["buy", "sell"]:
    _dbg(f"[SKIP] Invalid action '{action}'")
    return jsonify({"ok": True, "skipped": "invalid_action"}), 200

# ... (rest van je webhook-functie, zoals sym_to_ccxt)

    symbol = tv_to_ccxt(tv_symbol)

    # Timeframe allowlist per symbol from ENV
    try:
        atfs = allowed_tfs_for(symbol)
        if atfs and tf not in atfs:
            _dbg(f"[TF-FILTER] skip {symbol} tf={tf} not allowed (allowed={sorted(list(atfs))})")
            return jsonify({"ok": True, "skip": "tf_not_allowed"}), 200
    except Exception as _e:
        _dbg(f"[TF-FILTER] warn: {_e}")

    # Per-candle lock: max 1 action per symbol per bartime
    try:
        apply_lock = ((action == "buy" and PER_BAR_LOCK_BUY) or (action == "sell" and PER_BAR_LOCK_SELL))
        if apply_lock and bartime:
            bk = (symbol, action, tf or "")
            if LAST_BAR.get(bk) == bartime:
                _dbg(f"[BARLOCK] ignore {action} for {symbol} at tf={tf} bartime={bartime}")
                return jsonify({"ok": True, "skip": "barlock"}), 200
            LAST_BAR[bk] = bartime
    except Exception as _e:
        _dbg(f"[BARLOCK] warn: {_e}")

    # Latency measure (if timenow present)
    try:
        tv_now = str(payload.get("timenow") or "")
        if tv_now.endswith("Z"):
            tv_dt = datetime.strptime(tv_now, "%Y-%m-%dT%H:%M:%SZ")
            srv_dt = datetime.now(timezone.utc)
            lag_ms = (srv_dt - tv_dt).total_seconds() * 1000.0
            _dbg(f"[LATENCY] TV->server ≈ {lag_ms:.0f} ms for {tv_symbol}")
    except Exception:
        pass

    if symbol not in SYMBOLS:
        return jsonify({"ok": True, "skip": f"symbol {symbol} not enabled"}), 200

    if is_duplicate_signal(action, source, price, tf, symbol):
        _dbg(f"dedup ignored key={(action, source, round(price,4), tf, symbol)} window={DEDUP_WINDOW_S}/{STRICT_DEDUP_S}s")
        return jsonify({"ok": True}), 200

    if blocked_by_cooldown(symbol, action):
        _dbg(f"cooldown ignored min={COOLDOWN_S}s since last_action for {symbol}")
        return jsonify({"ok": True}), 200

    lock = SYMBOL_LOCKS[symbol]
    with lock:
        ex = mexc()
        st = STATE[symbol]
        base = symbol.split("/")[0]
        quote = symbol.split("/")[1]

        # fetch balances (sell uses free base even if this fails)
        try:
            bal = ex.fetch_balance(params={"recvWindow": MEXC_RECVWINDOW_MS})
        except Exception as e:
            _dbg(f"[PRECHK] balance error: {e}")
            bal = {}

        if action == "buy":
            if st.get("inflight"):
                _dbg("buy ignored: order already inflight")
                return jsonify({"ok": True}), 200
            if st["in_position"]:
                _dbg(f"buy ignored: already in_position at entry={st['entry_price']}")
                return jsonify({"ok": True}), 200
            if (time.time() - float(st.get("last_entry_ts") or 0.0)) < ENTRY_LOCK_S:
                _dbg(f"buy ignored: entry lock {ENTRY_LOCK_S}s")
                return jsonify({"ok": True}), 200

            free_q = 0.0
            try:
                free_q = float(((bal.get("free") or {}).get(quote)) or 0.0)
            except Exception:
                pass
            spend = min(BUDGET_USDT[symbol], free_q if free_q > 0 else BUDGET_USDT[symbol])

            try:
                st["inflight"] = True
                order = _ensure_spend_buy(ex, symbol, spend_usdt=spend, price_hint=price)
                avg, filled, gross_q, fee_q, net_in = _order_breakdown(ex, symbol, order, "buy")
                if filled > 0:
                    st["in_position"] = True
                    st["entry_price"] = avg if avg > 0 else price
                    st["pos_amount"] = filled
                    st["pos_quote"] = net_in if net_in > 0 else (filled * (avg if avg > 0 else price))
                    st["last_action_ts"] = time.time()
                    st["last_entry_ts"] = st["last_action_ts"]
                    _dbg(f"[LIVE] BUY {symbol} id={order.get('id')} filled={filled} avg={avg} gross={gross_q} fee_q={fee_q} pos_quote={st['pos_quote']}")
                    msg = tg_buy_msg(symbol, st['entry_price'], filled, st['pos_quote'], source, tf)
                    send_tg(msg)
                    TRADE_LOG.append({
                        "ts": time.time(),
                        "action": "buy",
                        "symbol": symbol,
                        "price_usd": float(st['entry_price']),
                        "qty": float(filled),
                        "invested_usd": float(st['pos_quote']),
                        "source": source,
                        "tf": tf,
                    })
                    _save_state_file()
                else:
                    _dbg("[LIVE] BUY zero-fill detected → skip marking position")
            except Exception as e:
                _dbg(f"[LIVE] buy error: {e}")
            finally:
                st["inflight"] = False

        elif action == "sell":
            if st.get("inflight"):
                _dbg("sell ignored: order already inflight")
                return jsonify({"ok": True}), 200

            sell_amt = 0.0
            try:
                sell_amt = float(((bal.get("free") or {}).get(base)) or 0.0)
            except Exception:
                pass
            sell_amt = max(sell_amt, float(st.get("pos_amount") or 0.0))
            if sell_amt <= 0:
                _dbg("sell ignored: nothing to sell")
                return jsonify({"ok": True}), 200

            try:
                st["inflight"] = True
                order = _market_sell_all(ex, symbol, sell_amt)
                avg, filled, gross_q, fee_q, net_out = _order_breakdown(ex, symbol, order, "sell")
                if filled > 0:
                    st["pos_amount"] = max(0.0, float(st.get("pos_amount") or 0.0) - filled)
                    if st["pos_amount"] <= 1e-9:
                        entry = float(st.get("entry_price") or price or avg)
                        pnl = net_out - float(st.get("pos_quote") or (filled * entry))
                        st["in_position"] = False
                        st["entry_price"] = 0.0
                        st["pos_quote"] = 0.0
                    else:
                        entry = float(st.get("entry_price") or price or avg)
                        st["pos_quote"] = max(0.0, float(st["pos_quote"]) - (filled * entry))
                        pnl = net_out - (filled * entry)
                    st["last_action_ts"] = time.time()
                    _dbg(f"[LIVE] SELL {symbol} id={order.get('id')} filled={filled} avg={avg} gross={gross_q} fee_q={fee_q} net_out={net_out}")
                    msg = tg_sell_msg(symbol, avg if avg>0 else price, filled, net_out, pnl)
                    send_tg(msg)
                    TRADE_LOG.append({
                        "ts": time.time(),
                        "action": "sell",
                        "symbol": symbol,
                        "price_usd": float(avg if avg>0 else price),
                        "qty": float(filled),
                        "net_out_usd": float(net_out),
                        "pnl_usd": float(pnl),
                        "source": source,
                        "tf": tf,
                    })
                    _save_state_file()
                else:
                    _dbg("[LIVE] SELL zero-fill detected → no state change, no TG")
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

