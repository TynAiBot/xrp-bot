import os, time, json, logging
from datetime import datetime, timezone
from threading import Thread, Event
import pandas as pd
import pandas_ta as ta
import ccxt, requests
from flask import Flask, jsonify, Response, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ===== ENV =====
EXCHANGE = os.getenv("EXCHANGE", "mexc")
API_KEY  = os.getenv("MEXC_API_KEY", os.getenv("API_KEY", ""))
API_SECRET = os.getenv("MEXC_API_SECRET", os.getenv("API_SECRET", ""))

SYMBOL    = os.getenv("SYMBOL", "XRP/USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
HTF       = os.getenv("HTF", "1h")

# Spot/perps flags
USE_PERP    = int(os.getenv("USE_PERP", "0"))
LEVERAGE    = int(os.getenv("LEVERAGE", "3"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")  # isolated|cross
HEDGE_MODE  = int(os.getenv("HEDGE_MODE", "0"))
DERIV_SETUP = int(os.getenv("DERIV_SETUP", "0"))     # 0: sla API-setup over (UI gebruiken)

ENABLE_LONG  = int(os.getenv("ENABLE_LONG", "1"))
ENABLE_SHORT = int(os.getenv("ENABLE_SHORT", "0"))

# Grid & indicators
GRID_LAYERS   = int(os.getenv("GRID_LAYERS", "6"))
ATR_LEN       = int(os.getenv("ATR_LEN", "14"))
ATR_MULT      = float(os.getenv("ATR_MULT", "0.9"))
ADX_LEN       = int(os.getenv("ADX_LEN", "14"))
ADX_RANGE_TH  = float(os.getenv("ADX_RANGE_TH", "20"))
DONCHIAN_LEN  = int(os.getenv("DONCHIAN_LEN", "100"))

# Exposure
BASE_ORDER_USDT   = float(os.getenv("BASE_ORDER_USDT", "20"))
MAX_OPEN_NOTIONAL = float(os.getenv("MAX_OPEN_NOTIONAL", "1000"))
MAX_NOTIONAL_LONG = float(os.getenv("MAX_NOTIONAL_LONG", "600"))
MAX_NOTIONAL_SHORT= float(os.getenv("MAX_NOTIONAL_SHORT", "600"))

# Order gedrag
POST_ONLY      = int(os.getenv("POST_ONLY", "0"))
REDUCE_ONLY_TP = int(os.getenv("REDUCE_ONLY_TP", "0"))

# Runtime
POLL_SEC            = int(os.getenv("POLL_SEC", "15"))
REBUILD_ATR_DELTA   = float(os.getenv("REBUILD_ATR_DELTA", "0.2"))
BREAKOUT_ATR_MULT   = float(os.getenv("BREAKOUT_ATR_MULT", "1.0"))
DRY_RUN             = int(os.getenv("DRY_RUN", "1"))

STATE_FILE          = os.getenv("STATE_FILE", "grid_state.json")
MEXC_RECVWINDOW_MS  = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
CCXT_TIMEOUT_MS     = int(os.getenv("CCXT_TIMEOUT_MS", "7000"))

# TP/SL & fills
TP_MODE         = os.getenv("TP_MODE", "spacing")  # midline|spacing|pct
TP_SPACING_MULT = float(os.getenv("TP_SPACING_MULT", "1.0"))
TP_PCT          = float(os.getenv("TP_PCT", "0.004"))
ATR_SL_MULT     = float(os.getenv("ATR_SL_MULT", "2.0"))
NOTIFY_FILLS    = int(os.getenv("NOTIFY_FILLS", "1"))
NOTIFY_TP_SL    = int(os.getenv("NOTIFY_TP_SL", "1"))

# Trades fetch window (live fills)
TRADES_LOOKBACK_S = int(os.getenv("TRADES_LOOKBACK_S", "0"))

# Paper trading + Sparen
PAPER_MODE          = int(os.getenv("PAPER_MODE", "1"))
PAPER_USDT_START    = float(os.getenv("PAPER_USDT_START", "2000"))
PAPER_FEE_PCT       = float(os.getenv("PAPER_FEE_PCT", "0.0006"))
SPAREN_ENABLED      = int(os.getenv("SPAREN_ENABLED", "1"))
SPAREN_SPLIT_PCT    = float(os.getenv("SPAREN_SPLIT_PCT", "100"))
SPAREN_MIN_PNL_USDT = float(os.getenv("SPAREN_MIN_PNL_USDT", "0.01"))

# Paper export & snapshots
PAPER_EXPORT_FILENAME  = os.getenv("PAPER_EXPORT_FILENAME", "paper_closed_trades.csv")
PAPER_SNAPSHOT_ENABLED = int(os.getenv("PAPER_SNAPSHOT_ENABLED", "1"))
PAPER_SNAPSHOT_MIN     = int(os.getenv("PAPER_SNAPSHOT_MIN", "60"))

# Telegram & web
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Amsterdam")
DAILY_REPORT_HHMM = os.getenv("DAILY_REPORT_HHMM", "23:59")
USD_TO_EUR = float(os.getenv("USD_TO_EUR", "0.86"))
PORT = int(os.getenv("PORT", "10000"))

# ===== Globals =====
app = Flask(__name__)
_stop = Event()

runtime = {"mode":None,"center":None,"atr":None,"adx":None,"spacing":None,
           "don_low":None,"don_high":None,
           "long_notional":0.0,"short_notional":0.0,"global_notional":0.0,
           "last_report":None}
last_trade_ms = 0
last_paper_snapshot_min = None  # voor snapshot cadence

fills = {}  # real/perp of spot fills (dict)

# Paper state (met handels- en spaarrekening)
paper = {
    "enabled": bool(PAPER_MODE and DRY_RUN),
    "usdt_trading": PAPER_USDT_START,
    "usdt_sparen":  0.0,
    "xrp": 0.0,
    "realized_pnl_usdt": 0.0,
    "open_entries": [],  # FIFO entries: [{qty, entry, fee_usdt, ts}]
    "open_orders": [],   # TP’s:       [{id, type, side, price, qty, status}]
    "closed_trades": []  # gesloten:   [{entry, exit, qty, pnl_usdt, ts_entry, ts_exit}]
}

# ===== Utils =====
def now_utc(): return datetime.now(timezone.utc)
def fmt_ts(dt): return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")

def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        logging.warning(f"Telegram failed: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f: return json.load(f)
    return {}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, indent=2)

def ccxt_client():
    if EXCHANGE != "mexc":
        raise RuntimeError("Alleen 'mexc' wordt ondersteund in deze build.")
    opts = {
        "apiKey": API_KEY, "secret": API_SECRET,
        "enableRateLimit": True, "timeout": CCXT_TIMEOUT_MS,
        "options": {"defaultType": "swap" if USE_PERP else "spot", "recvWindow": MEXC_RECVWINDOW_MS}
    }
    ex = ccxt.mexc(opts); ex.load_markets(); return ex

def set_deriv_modes(ex):
    if (not USE_PERP) or (not DERIV_SETUP):
        logging.info("Derivatives setup via API overgeslagen (USE_PERP=0 of DERIV_SETUP=0).")
        return
    try:
        try:
            ex.set_margin_mode(MARGIN_MODE, SYMBOL); logging.info(f"Margin mode='{MARGIN_MODE}' ingesteld.")
        except Exception as e:
            logging.info(f"set_margin_mode: {e}")
        try:
            params = {"openType": 1 if MARGIN_MODE == "isolated" else 2}
            ex.set_leverage(LEVERAGE, SYMBOL, params=params); logging.info(f"Leverage={LEVERAGE} ingesteld.")
        except Exception as e:
            logging.info(f"set_leverage: {e}")
        try:
            ex.set_position_mode(bool(HEDGE_MODE), SYMBOL); logging.info(f"Hedge mode={'ON' if HEDGE_MODE else 'OFF'}.")
        except Exception as e:
            logging.info(f"set_position_mode: {e}")
    except Exception as e:
        logging.warning(f"Derivatives modes setup mislukte: {e}")

def fetch_ohlcv(ex, tf): return ex.fetch_ohlcv(SYMBOL, timeframe=tf, limit=500)

def to_df(ohlcv):
    d = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    d["ts"] = pd.to_datetime(d["ts"], unit="ms", utc=True)
    return d

def compute_indicators(df_ltf: pd.DataFrame, df_htf: pd.DataFrame):
    d = df_ltf.copy()
    d["atr"] = ta.atr(d["high"], d["low"], d["close"], length=ATR_LEN)
    adx     = ta.adx(d["high"], d["low"], d["close"], length=ADX_LEN)
    d["adx"]    = adx[f"ADX_{ADX_LEN}"]
    d["ema200"] = ta.ema(d["close"], length=200)

    h = df_htf.copy()
    don_high = float(h["high"].rolling(window=DONCHIAN_LEN, min_periods=1).max().iloc[-1])
    don_low  = float(h["low"].rolling(window=DONCHIAN_LEN,  min_periods=1).min().iloc[-1])
    return d, don_low, don_high

def decide_regime(adx_val): return "range" if adx_val < ADX_RANGE_TH else "trend"

def build_grids(center, atr_val, mode):
    spacing = max(1e-8, atr_val * ATR_MULT)
    if mode == "trend": spacing *= 1.8
    long_buys, short_sells = [], []
    for i in range(1, GRID_LAYERS + 1):
        long_buys.append(round(center - i * spacing, 8))
        short_sells.append(round(center + i * spacing, 8))
    return spacing, long_buys, short_sells

def compute_tp_price(mode, entry, spacing, center, side):
    if mode == "midline":
        target = center
        if side == "long" and target <= entry: target = entry + spacing
        if side == "short" and target >= entry: target = entry - spacing
        return target
    if mode == "spacing":
        return entry + (spacing * TP_SPACING_MULT if side == "long" else -spacing * TP_SPACING_MULT)
    if mode == "pct":
        delta = entry * TP_PCT
        return entry + (delta if side == "long" else -delta)
    return entry + (spacing if side == "long" else -spacing)

def compute_sl_price(entry, atr, side):
    return entry - ATR_SL_MULT * atr if side == "long" else entry + ATR_SL_MULT * atr

def can_add(side_total, add, side_cap, g_total, g_cap):
    return (side_total + add) <= side_cap and (g_total + add) <= g_cap

# ===== Paper helpers (FIFO + sparen) =====
def paper_reset():
    paper.update({
        "usdt_trading": PAPER_USDT_START,
        "usdt_sparen":  0.0,
        "xrp": 0.0,
        "realized_pnl_usdt": 0.0,
        "open_entries": [],
        "open_orders": [],
        "closed_trades": []
    })

def paper_place_buy(price, now_ts):
    usdt = BASE_ORDER_USDT
    if paper["usdt_trading"] < usdt: return False
    qty = round(usdt / max(price, 1e-12), 6)
    fee_buy = usdt * PAPER_FEE_PCT
    paper["usdt_trading"] -= (usdt + fee_buy)
    paper["xrp"] += qty
    paper["open_entries"].append({"qty": qty, "entry": price, "fee_usdt": fee_buy, "ts": now_ts})
    if NOTIFY_FILLS: send_telegram(f"🧪 PAPER BUY {qty:g} @ {price:.6f} (fee {fee_buy:.4f} USDT)")
    return qty

def paper_place_tp(qty, entry_price, spacing, center):
    tp = compute_tp_price(TP_MODE, entry_price, spacing, center, "long")
    oid = f"PTP-{int(time.time()*1000)}-{tp}"
    paper["open_orders"].append({"id": oid, "type": "tp", "side": "sell", "price": tp, "qty": qty, "status": "open"})
    if NOTIFY_TP_SL: send_telegram(f"🧪 PAPER TP geplaatst SELL @ {tp:.6f}")

def _paper_fifo_take(qty_need):
    taken = []
    qty_left = qty_need
    while qty_left > 1e-9 and paper["open_entries"]:
        e = paper["open_entries"][0]
        q = min(e["qty"], qty_left)
        part_fee = e["fee_usdt"] * (q / e["qty"])
        taken.append({"qty": q, "entry": e["entry"], "fee_usdt": part_fee, "ts": e["ts"]})
        e["qty"] -= q
        qty_left -= q
        if e["qty"] <= 1e-9: paper["open_entries"].pop(0)
    return taken, qty_need - qty_left

def paper_exec_tps(close, now_ts):
    executed = []
    for o in list(paper["open_orders"]):
        if o["status"] != "open" or o["type"] != "tp": continue
        if close >= o["price"]:
            qty = o["qty"]; price = o["price"]
            parts, filled = _paper_fifo_take(qty)
            if filled <= 0:
                o["status"] = "filled"; continue  # niets in voorraad, markeer klaar
            proceeds = filled * price
            fee_sell = proceeds * PAPER_FEE_PCT
            cost = sum(p["qty"] * p["entry"] for p in parts) if parts else 0.0
            total_buy_fee = sum(p["fee_usdt"] for p in parts) if parts else 0.0
            avg_entry = (cost / max(sum(p["qty"] for p in parts), 1e-12)) if parts else 0.0
            pnl = proceeds - fee_sell - cost - total_buy_fee

            paper["usdt_trading"] += (proceeds - fee_sell)
            paper["xrp"] -= filled
            paper["realized_pnl_usdt"] += pnl
            o["status"] = "filled"; executed.append(o["id"])
            paper["closed_trades"].append({
                "entry": round(avg_entry, 6),
                "exit":  round(price,    6),
                "qty":   round(filled,   6),
                "pnl_usdt": round(pnl,   6),
                "ts_entry": parts[0]["ts"] if parts else None,
                "ts_exit":  now_ts
            })
            if NOTIFY_TP_SL:
                send_telegram(
                    f"🧪 PAPER SELL {filled:g} @ {price:.6f} (avg entry {avg_entry:.6f}) | PnL {pnl:+.4f} USDT"
                )
            # Sparen: deel van winst naar spaarrekening
            if SPAREN_ENABLED and pnl > SPAREN_MIN_PNL_USDT and SPAREN_SPLIT_PCT > 0:
                to_save = pnl * (SPAREN_SPLIT_PCT / 100.0)
                to_save = min(to_save, paper["usdt_trading"])  # safety
                paper["usdt_trading"] -= to_save
                paper["usdt_sparen"]  += to_save
                if NOTIFY_TP_SL:
                    send_telegram(f"💰 PAPER sparen +{to_save:.4f} USDT ({SPAREN_SPLIT_PCT:.0f}% van winst)")
    # open_orders opschonen
    paper["open_orders"] = [o for o in paper["open_orders"] if o["status"] == "open"]

# ===== Rapportage helpers =====
def fetch_balances_report(ex):
    rep = {}
    try:
        b = ex.fetch_balance({"type":"spot", "recvWindow": MEXC_RECVWINDOW_MS})
        rep["spot"] = {
            "USDT_free":  float((b.get("free")  or {}).get("USDT", 0.0)),
            "USDT_used":  float((b.get("used")  or {}).get("USDT", 0.0)),
            "USDT_total": float((b.get("total") or {}).get("USDT", 0.0)),
        }
    except Exception:
        rep["spot"] = None
    try:
        b = ex.fetch_balance({"type":"swap", "recvWindow": MEXC_RECVWINDOW_MS})
        rep["swap"] = {
            "USDT_free":  float((b.get("free")  or {}).get("USDT", 0.0)),
            "USDT_used":  float((b.get("used")  or {}).get("USDT", 0.0)),
            "USDT_total": float((b.get("total") or {}).get("USDT", 0.0)),
        }
    except Exception:
        rep["swap"] = None
    rep["funding"] = None
    return rep

def daily_report_text(ex):
    rep = fetch_balances_report(ex)
    spot = rep.get("spot") or {}
    usdt_total = spot.get("USDT_total", 0.0)
    eur = usdt_total * USD_TO_EUR
    lines = []
    lines.append("<b>Dagrapport — Grid Bot</b>")
    lines.append(f"Symbool: <b>{SYMBOL}</b>")
    lines.append(f"Mode: <b>{runtime.get('mode')}</b> | ADX≈{(runtime.get('adx') or 0):.1f} | ATR≈{(runtime.get('atr') or 0):.6f}")
    lines.append(f"Center≈{(runtime.get('center') or 0):.6f} | Spacing≈{(runtime.get('spacing') or 0):.6f}")
    lines.append(f"HTF: [{(runtime.get('don_low') or 0):.6f} .. {(runtime.get('don_high') or 0):.6f}]")
    lines.append("")
    lines.append(f"(Spot) USDT: total≈<b>{usdt_total:.2f}</b> (free {spot.get('USDT_free',0.0):.2f} / used {spot.get('USDT_used',0.0):.2f}) ≈ €{eur:.2f}")
    if paper["enabled"]:
        lines.append(f"(Paper) Trading≈{paper['usdt_trading']:.2f} | Sparen≈{paper['usdt_sparen']:.2f} | XRP≈{paper['xrp']:.4f} | Realized PnL≈{paper['realized_pnl_usdt']:.2f}")
    lines.append(f"Tijd: {fmt_ts(now_utc())}")
    return "\n".join(lines)

# Paper summary/export for endpoints
def paper_summary_dict():
    return {
        "balances": {
            "USDT_trading": round(paper["usdt_trading"], 6),
            "USDT_sparen":  round(paper["usdt_sparen"],  6),
            "XRP":          round(paper["xrp"],          6),
        },
        "realized_pnl_usdt": round(paper["realized_pnl_usdt"], 6),
        "open_entries": paper["open_entries"],
        "open_tp_orders": paper["open_orders"],
        "closed_trades_count": len(paper["closed_trades"]),
    }

def build_closed_trades_csv():
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# PAPER SUMMARY"])
    s = paper_summary_dict()
    w.writerow(["USDT_trading", s["balances"]["USDT_trading"]])
    w.writerow(["USDT_sparen",  s["balances"]["USDT_sparen"]])
    w.writerow(["XRP",          s["balances"]["XRP"]])
    w.writerow(["Realized_PnL_USDT", s["realized_pnl_usdt"]])
    w.writerow([])
    w.writerow(["entry","exit","qty","pnl_usdt","ts_entry","ts_exit"])
    for t in paper["closed_trades"]:
        w.writerow([
            t.get("entry"), t.get("exit"), t.get("qty"), t.get("pnl_usdt"),
            t.get("ts_entry"), t.get("ts_exit")
        ])
    return buf.getvalue()

def send_paper_snapshot_to_telegram():
    if not (TG_TOKEN and TG_CHAT_ID): return
    s = paper_summary_dict()
    msg = (
        "<b>PAPER snapshot</b>\n"
        f"Trading: <b>{s['balances']['USDT_trading']:.2f}</b> USDT\n"
        f"Sparen:  <b>{s['balances']['USDT_sparen']:.2f}</b> USDT\n"
        f"XRP:     <b>{s['balances']['XRP']:.4f}</b>\n"
        f"Realized PnL: <b>{s['realized_pnl_usdt']:.2f}</b> USDT\n"
        f"Closed trades: {s['closed_trades_count']}\n"
        f"Tijd: {fmt_ts(now_utc())}"
    )
    send_telegram(msg)

# ===== Bot thread =====
def bot_thread():
    global last_trade_ms, last_paper_snapshot_min
    ex = ccxt_client(); set_deriv_modes(ex)

    # start vanaf nu (minus optional lookback) om oude fills te negeren
    last_trade_ms = int(time.time() * 1000) - TRADES_LOOKBACK_S * 1000

    state = load_state()
    last_atr   = state.get("last_atr")
    last_mode  = state.get("last_mode")
    last_center= state.get("last_center")
    grid_live  = state.get("grid_live", False)
    last_spacing = state.get("last_spacing")

    total_g = total_l = total_s = 0.0
    if paper["enabled"]:
        paper_reset()

    send_telegram(
        f"🔧 Bot gestart — {SYMBOL} | perp={USE_PERP} lev={LEVERAGE} hedge={HEDGE_MODE} iso={MARGIN_MODE=='isolated'} | "
        f"DRY_RUN={DRY_RUN} | PAPER={paper['enabled']}"
    )

    while not _stop.is_set():
        try:
            df_ltf = to_df(fetch_ohlcv(ex, TIMEFRAME))
            df_htf = to_df(fetch_ohlcv(ex, HTF))
            d, don_low, don_high = compute_indicators(df_ltf, df_htf)

            close  = float(d["close"].iloc[-1])
            atr    = float(d["atr"].iloc[-1])
            adx    = float(d["adx"].iloc[-1])
            ema200 = float(d["ema200"].iloc[-1])

            mode   = decide_regime(adx)
            center = min(max(ema200, don_low), don_high)
            lower_break = don_low  - atr * BREAKOUT_ATR_MULT
            upper_break = don_high + atr * BREAKOUT_ATR_MULT

            need_rebuild = (last_atr is None or last_mode is None or last_center is None or not grid_live)
            if not need_rebuild:
                if abs(atr - last_atr) / max(1e-8, last_atr) > REBUILD_ATR_DELTA: need_rebuild = True
                if mode != last_mode: need_rebuild = True
                if close < lower_break or close > upper_break:
                    send_telegram(f"⚠️ Breakout: close {close:.6f} buiten [{lower_break:.6f}, {upper_break:.6f}] — rebuild")
                    need_rebuild = True

            if need_rebuild:
                last_spacing, long_buys, short_sells = build_grids(center, atr, mode)
                grid_live = True; last_atr, last_mode, last_center = atr, mode, center
                save_state({
                    "last_atr": last_atr, "last_mode": last_mode, "last_center": last_center, "grid_live": grid_live,
                    "last_spacing": last_spacing, "updated_at": now_utc().isoformat(),
                    "last_levels": {"long_buys": long_buys, "short_sells": short_sells}
                })
                send_telegram(
                    f"🔁 Rebuild grid — Mode <b>{mode}</b> | ADX≈{adx:.1f} | ATR≈{atr:.6f}\n"
                    f"Center≈{center:.6f} | Spacing≈{last_spacing:.6f}\nHTF: [{don_low:.6f} .. {don_high:.6f}]"
                )

            # === PAPER SIM (spot long-only) ===
            if paper["enabled"] and ENABLE_LONG:
                spacing, long_buys, _ = build_grids(center, atr, mode)
                for px in long_buys:
                    if close <= px and paper["usdt_trading"] >= BASE_ORDER_USDT:
                        qty = paper_place_buy(px, int(time.time()*1000))
                        if qty:
                            paper_place_tp(qty, px, spacing, center)
                paper_exec_tps(close, int(time.time()*1000))

            # === LIVE fills (alleen als PAPER uit staat) ===
            if not paper["enabled"]:
                try:
                    trades = ex.fetch_my_trades(SYMBOL, since=last_trade_ms or None, limit=100)
                except Exception as e:
                    trades = []; logging.debug(f"fetch_my_trades: {e}")

                new_max_ms = last_trade_ms
                for tr in trades or []:
                    ts = int(tr.get("timestamp") or 0)
                    if ts and ts <= last_trade_ms: continue
                    new_max_ms = max(new_max_ms, ts or 0)

                    side = tr.get("side")  # 'buy'/'sell'
                    info = tr.get("info", {})
                    pos_side = (info.get("positionSide") or info.get("posSide") or "").upper()

                    if USE_PERP:
                        my_pos_side = "LONG" if (pos_side=="LONG" or (HEDGE_MODE and side=="buy")) else \
                                      ("SHORT" if (pos_side=="SHORT" or (HEDGE_MODE and side=="sell")) else None)
                    else:
                        if side == "sell":  # spot: sells niet als nieuwe fill tellen
                            continue
                        my_pos_side = "LONG"

                    price  = float(tr.get("price") or tr.get("info", {}).get("price") or 0)
                    amount = float(tr.get("amount") or tr.get("contracts") or 0)
                    if amount <= 0 or price <= 0: continue

                    fid = tr.get("id") or f"{ts}-{side}-{price}"
                    if fid in fills: continue

                    fills[fid] = {"side": "long" if my_pos_side=="LONG" else "short",
                                  "entry_price": price, "qty": amount, "tp": None, "sl": None, "active": True}
                    if NOTIFY_FILLS:
                        send_telegram(f"✅ Fill: {fills[fid]['side'].upper()} {amount:g} @ {price:.6f}")

                    spacing = last_spacing or (last_atr * ATR_MULT if last_atr else atr * ATR_MULT)
                    tp_price = compute_tp_price(TP_MODE, price, spacing, last_center if last_center is not None else center, fills[fid]["side"])
                    try:
                        side_out = "sell" if fills[fid]["side"] == "long" else "buy"
                        pos_tag  = "long" if fills[fid]["side"] == "long" else "short"
                        o = {"id":"TP"}
                        if not DRY_RUN:
                            o = ex.create_order(
                                SYMBOL, type="limit", side=side_out,
                                amount=amount, price=tp_price,
                                params={
                                    **({"postOnly": True} if POST_ONLY else {}),
                                    **({"reduceOnly": True} if (USE_PERP and REDUCE_ONLY_TP) else {}),
                                    **({"positionSide": "LONG" if pos_tag=="long" else "SHORT"} if (USE_PERP and HEDGE_MODE) else {})
                                }
                            )
                        fills[fid]["tp"] = {"id": o.get("id", "TP"), "price": tp_price}
                        if NOTIFY_TP_SL:
                            ro = "RO " if (USE_PERP and REDUCE_ONLY_TP) else ""
                            send_telegram(f"🎯 TP geplaatst: {side_out.upper()} {ro}@ {tp_price:.6f} (entry {price:.6f})")
                    except Exception as e:
                        logging.warning(f"TP place fail: {e}")

                    sl_price = compute_sl_price(price, last_atr if last_atr is not None else atr, fills[fid]["side"])
                    fills[fid]["sl"] = {"price": sl_price}

                if new_max_ms > last_trade_ms: last_trade_ms = new_max_ms

            # === Runtime + rapporten ===
            runtime.update({
                "mode": mode, "center": center, "atr": atr, "adx": adx,
                "spacing": last_spacing or (atr * ATR_MULT),
                "don_low": don_low, "don_high": don_high,
                "long_notional": total_l, "short_notional": total_s, "global_notional": total_g
            })

            # Dagrapport
            if datetime.now().strftime("%H:%M") == DAILY_REPORT_HHMM:
                stamp = datetime.now().strftime("%Y%m%d%H%M")
                if runtime.get("last_report") != stamp:
                    send_telegram(daily_report_text(ex))
                    runtime["last_report"] = stamp

            # Uurlijkse (of elke N minuten) paper-snapshot naar Telegram
            if PAPER_SNAPSHOT_ENABLED and paper["enabled"]:
                try:
                    minute = int(datetime.now().strftime("%M"))
                    nowm = datetime.now().strftime("%Y-%m-%d %H:%M")
                    if (minute % max(PAPER_SNAPSHOT_MIN, 1) == 0) and (last_paper_snapshot_min != nowm):
                        send_paper_snapshot_to_telegram()
                        last_paper_snapshot_min = nowm
                except Exception:
                    pass

            logging.info(f"Tick | close={close:.6f} ADX={adx:.1f} ATR={atr:.6f} mode={mode}")

        except Exception as e:
            logging.exception(f"Loop error: {e}")

        time.sleep(POLL_SEC)

# ===== Flask =====
@app.get("/health")
def health():
    return jsonify({"ok": True, "symbol": SYMBOL, "perp": bool(USE_PERP), "hedge": bool(HEDGE_MODE),
                    "paper": paper["enabled"], "ts": fmt_ts(now_utc())})

@app.get("/state")
def state_ep():
    ex = ccxt_client()
    xrep = fetch_balances_report(ex)
    return jsonify({"runtime": runtime, "fills_active": len(fills), "paper_enabled": paper["enabled"],
                    "exchange_balances": xrep})

@app.get("/paper")
def paper_ep():
    if not paper["enabled"]:
        return jsonify({"paper_enabled": False, "hint": "Zet PAPER_MODE=1 en DRY_RUN=1 in .env"}), 200
    return jsonify({
        "paper_enabled": True,
        "balances": {"USDT_trading": round(paper["usdt_trading"], 4),
                     "USDT_sparen":  round(paper["usdt_sparen"],  4),
                     "XRP":          round(paper["xrp"],          6)},
        "realized_pnl_usdt": round(paper["realized_pnl_usdt"], 4),
        "open_entries": paper["open_entries"],
        "open_tp_orders": paper["open_orders"],
        "closed_trades_count": len(paper["closed_trades"])
    })

@app.get("/paper/closed")
def paper_closed_ep():
    out = paper_summary_dict()
    out["closed_trades"] = paper["closed_trades"]
    out["paper_enabled"] = paper["enabled"]
    return jsonify(out), 200

@app.get("/paper/export")
def paper_export_ep():
    csv_text = build_closed_trades_csv()
    fname = PAPER_EXPORT_FILENAME or "paper_closed_trades.csv"
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.post("/paper/reset")
def paper_reset_ep():
    confirm = request.args.get("confirm", "")
    if confirm.lower() not in ("ja", "yes", "ok", "ikweethet", "confirm"):
        return jsonify({"ok": False, "msg": "Bevestig met ?confirm=ikweethet"}), 400
    paper_reset()
    return jsonify({"ok": True, "msg": "Paper reset", "state": paper_summary_dict()})

if __name__ == "__main__":
    t = Thread(target=bot_thread, daemon=True); t.start()
    try:
        app.run(host="0.0.0.0", port=PORT)
    finally:
        _stop.set(); t.join(timeout=5)
