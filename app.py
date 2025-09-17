
# -*- coding: utf-8 -*-
"""
MEXC 2-pair webhook bot with Telegram alerts (robust core).
- Pairs: XRP/USDT, WLFI/USDT
- Budget per pair (default 500 USDT), via ENV
- Dedup, per-symbol locks, in-flight guard
- Zero-fill fix: poll fetch_order until fill/timeout
- Rehydrate from balances at startup (sell uses free base)
- Latency log TV->server
- Endpoints: /, /health, /config, /envcheck, /test/send, /webhook
"""

import os
import time
import json
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Any, Optional, Tuple

import requests
from flask import Flask, request, jsonify
import ccxt

# ------------- helpers -------------
def env_first(names, default=""):
    """
    Return the first non-empty env value among 'names'.
    """
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

def _dbg(msg: str):
    print(f"[SIGDBG] {msg}", flush=True)

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def tv_to_ccxt(symbol_tv: str) -> str:
    # "XRPUSDT" -> "XRP/USDT"
    s = (symbol_tv or "").upper().replace("MEXC:", "").replace("SPOT:", "").replace("PERP:", "")
    if "/" in s:
        return s
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT"
    return s

# ------------- config -------------
PORT = env_int("PORT", 10000)

SYMBOLS = [
    "XRP/USDT",
    "WLFI/USDT",
]

BUDGET_USDT: Dict[str, float] = {
    "XRP/USDT": env_float("BUDGET_XRP", 500.0),
    "WLFI/USDT": env_float("BUDGET_WLFI", 500.0),
}

# Accept multiple env names for Telegram for compatibility
TG_TOKEN  = env_first(["TG_TOKEN","TELEGRAM_BOT_TOKEN","BOT_TOKEN","TELEGRAM_TOKEN"])
TG_CHAT_ID = env_first(["TG_CHAT_ID","TELEGRAM_CHAT_ID","CHAT_ID"])

MEXC_RECVWINDOW_MS = env_int("MEXC_RECVWINDOW_MS", 10000)
CCXT_TIMEOUT_MS = env_int("CCXT_TIMEOUT_MS", 10000)
COOLDOWN_S = env_int("MIN_TRADE_COOLDOWN_S", 0)  # user prefers 0
DEDUP_WINDOW_S = env_int("DEDUP_WINDOW_S", 20)

# ------------- telegram -------------
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

STATE: Dict[str, Dict[str, Any]] = {
    s: {
        "in_position": False,
        "entry_price": 0.0,
        "pos_amount": 0.0,   # base
        "pos_quote": 0.0,    # USDT inleg netto
        "last_action_ts": 0.0,
        "inflight": False,
    } for s in SYMBOLS
}

def _round_amount(ex: ccxt.Exchange, symbol: str, qty: float) -> float:
    try:
        return float(ex.amount_to_precision(symbol, qty))
    except Exception:
        return float(round(qty, 6))

def _order_breakdown(ex: ccxt.Exchange, symbol: str, order: Dict[str, Any], side: str) -> Tuple[float, float, float, float, float]:
    """
    returns: avg_price, filled, gross_quote, fee_quote, net_quote
    """
    filled = float(order.get("filled") or 0.0)
    avg = float(order.get("average") or order.get("price") or 0.0)
    cost = float(order.get("cost") or 0.0)
    fee_q = 0.0
    for f in (order.get("fees") or []):
        try:
            if (f.get("currency") or "").upper() in ("USDT",):
                fee_q += float(f.get("cost") or 0.0)
        except Exception:
            pass
    gross_q = cost if cost > 0 else (avg * filled if (avg and filled) else 0.0)
    net_q = max(gross_q - fee_q, 0.0)
    return avg, filled, gross_q, fee_q, net_q

def _poll_fill(ex: ccxt.Exchange, symbol: str, order_id: str, timeout_s: float = 6.0) -> Dict[str, Any]:
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

def _ensure_spend_buy(ex: ccxt.Exchange, symbol: str, spend_usdt: float, price_hint: float = 0.0) -> Dict[str, Any]:
    """Try market buy by cost; fallback to qty."""
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

def _market_sell_all(ex: ccxt.Exchange, symbol: str, amount: float) -> Dict[str, Any]:
    amount = max(0.0, float(amount))
    if amount <= 0:
        return {}
    qty = _round_amount(ex, symbol, amount)
    o = ex.create_order(symbol, "market", "sell", qty, None, {"recvWindow": MEXC_RECVWINDOW_MS})
    o2 = _poll_fill(ex, symbol, o.get("id"))
    return o2 or o

# ------------- dedup / cooldown -------------
def is_duplicate_signal(action: str, source: str, price: float, tf: str, symbol: str) -> bool:
    key = (symbol, action, source, round(float(price or 0.0), 4), tf or "")
    ts = time.time()
    last_ts = last_signal_key_ts.get(key, 0.0)
    if ts - last_ts < DEDUP_WINDOW_S:
        return True
    last_signal_key_ts[key] = ts
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
    except Exception as e:
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
        "state": st,
    }), 200

@app.route("/envcheck", methods=["GET"])
def envcheck():
    # no secrets returned; only presence flags and basic info
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

@app.route("/webhook", methods=["POST"])
def webhook():
    ct = request.headers.get("Content-Type", "")
    raw = request.get_data(as_text=True)
    _dbg(f"/webhook hit ct={ct} raw={raw!r}")
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        _dbg("bad_json")
        return jsonify({"ok": True, "skip": "bad_json"}), 200

    action = str(payload.get("action") or "").lower().strip()
    tv_symbol = str(payload.get("symbol") or payload.get("SYMBOL") or "").upper().replace(" ", "")
    source = str(payload.get("source") or "")
    tf = str(payload.get("tf") or payload.get("timeframe") or "")
    price = float(payload.get("price") or 0.0)

    if not action or not tv_symbol:
        return jsonify({"ok": True, "skip": "missing action/symbol"}), 200

    symbol = tv_to_ccxt(tv_symbol)

    # Simple latency measure from TV timenow if provided
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
        _dbg(f"dedup ignored key={(action, source, round(price,4), tf, symbol)} win={DEDUP_WINDOW_S}s")
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

        # fetch balances to be safe
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

            # available USDT
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
                    _dbg(f"[LIVE] BUY {symbol} id={order.get('id')} filled={filled} avg={avg} gross={gross_q} fee_q={fee_q} pos_quote={st['pos_quote']}")
                    send_tg(
                        f"🤖 <b>AANKOOP</b>\n"
                        f"• {symbol}\n"
                        f"• Prijs: ${st['entry_price']:.4f}\n"
                        f"• Aantal: {filled:.6f}\n"
                        f"• Inleg: ${st['pos_quote']:.2f}\n"
                        f"• Bron: {source} ({tf})"
                    )
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

            # amount to sell = free base (so we can exit even after reboot)
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
                    send_tg(
                        f"🤖 <b>VERKOOP</b>\n"
                        f"• {symbol}\n"
                        f"• Prijs: ${avg if avg>0 else price:.4f}\n"
                        f"• Aantal: {filled:.6f}\n"
                        f"• Ontvangen: ${net_out:.2f}\n"
                        f"• P&L (approx): ${pnl:.2f}\n"
                        f"• Bron: {source} ({tf})"
                    )
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
    _dbg(f"✅ Webhook server op http://0.0.0.0:{PORT}/webhook — symbols: {SYMBOLS}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
