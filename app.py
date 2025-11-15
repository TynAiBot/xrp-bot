import os, time, json, logging, io, csv
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

# Spot/perps
USE_PERP    = int(os.getenv("USE_PERP", "0"))
LEVERAGE    = int(os.getenv("LEVERAGE", "3"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")   # isolated|cross
HEDGE_MODE  = int(os.getenv("HEDGE_MODE", "0"))
DERIV_SETUP = int(os.getenv("DERIV_SETUP", "0"))      # 0: UI gebruiken, geen API-setup

# Kanten aan/uit
ENABLE_LONG  = int(os.getenv("ENABLE_LONG", "1"))
ENABLE_SHORT = int(os.getenv("ENABLE_SHORT", "1"))

# Grid & indicators
GRID_LAYERS   = int(os.getenv("GRID_LAYERS", "6"))
ATR_LEN       = int(os.getenv("ATR_LEN", "14"))
ATR_MULT      = float(os.getenv("ATR_MULT", "0.9"))
ADX_LEN       = int(os.getenv("ADX_LEN", "14"))
ADX_RANGE_TH  = float(os.getenv("ADX_RANGE_TH", "20"))
DONCHIAN_LEN  = int(os.getenv("DONCHIAN_LEN", "100"))

# Low-vol dynamische lagen
VOL_MIN_MULT        = float(os.getenv("VOL_MIN_MULT", "0.70"))    # ATR < 0.70*medianATR -> lowvol
LOWVOL_LAYER_FACTOR = float(os.getenv("LOWVOL_LAYER_FACTOR", "0.50"))  # % van GRID_LAYERS in lowvol

# Trend-bias settings
BEARISH_BIAS     = int(os.getenv("BEARISH_BIAS", "1"))
BULLISH_BIAS     = int(os.getenv("BULLISH_BIAS", "1"))
BIAS_RATIO_SHORT = float(os.getenv("BIAS_RATIO_SHORT", "0.7"))  # % naar short in bearish
BIAS_RATIO_LONG  = float(os.getenv("BIAS_RATIO_LONG",  "0.7"))  # % naar long in bullish
BIAS_SHIFT_ATR   = float(os.getenv("BIAS_SHIFT_ATR",   "0.4"))  # center shift in ATRs

# Exposure
BASE_ORDER_USDT   = float(os.getenv("BASE_ORDER_USDT", "20"))
MAX_OPEN_NOTIONAL = float(os.getenv("MAX_OPEN_NOTIONAL", "1000"))
MAX_NOTIONAL_LONG = float(os.getenv("MAX_NOTIONAL_LONG", "600"))
MAX_NOTIONAL_SHORT= float(os.getenv("MAX_NOTIONAL_SHORT", "600"))

# Orders
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

NOTIFY_FILLS    = int(os.getenv("NOTIFY_FILLS", "1"))   # BUY/SHORT-open
NOTIFY_TP_SL    = int(os.getenv("NOTIFY_TP_SL", "1"))   # SELL/cover PnL

# Stilte-profiel (alles uit behalve fills en afsluitingen)
NOTIFY_REBUILD       = int(os.getenv("NOTIFY_REBUILD", "0"))
NOTIFY_TP_PLACED     = int(os.getenv("NOTIFY_TP_PLACED", "0"))
NOTIFY_SPAREN        = int(os.getenv("NOTIFY_SPAREN", "0"))
NOTIFY_SELL_OVERVIEW = int(os.getenv("NOTIFY_SELL_OVERVIEW", "0"))

# TP trigger
TP_TRIGGER_MODE = os.getenv("TP_TRIGGER_MODE", "wick").lower()  # close|wick
PAPER_EPS_PCT   = float(os.getenv("PAPER_EPS_PCT", "0.0005"))

# Paper trading + Sparen
PAPER_MODE          = int(os.getenv("PAPER_MODE", "1"))
PAPER_USDT_START    = float(os.getenv("PAPER_USDT_START", "2000"))
PAPER_FEE_PCT       = float(os.getenv("PAPER_FEE_PCT", "0.0006"))
SPAREN_ENABLED      = int(os.getenv("SPAREN_ENABLED", "1"))
SPAREN_SPLIT_PCT    = float(os.getenv("SPAREN_SPLIT_PCT", "100"))
SPAREN_MIN_PNL_USDT = float(os.getenv("SPAREN_MIN_PNL_USDT", "0.01"))

# Paper export & snapshots
PAPER_EXPORT_FILENAME  = os.getenv("PAPER_EXPORT_FILENAME", "paper_closed_trades.csv")
PAPER_SNAPSHOT_ENABLED = int(os.getenv("PAPER_SNAPSHOT_ENABLED", "0"))
PAPER_SNAPSHOT_MIN     = int(os.getenv("PAPER_SNAPSHOT_MIN", "60"))

# Rapportage
REPORT_TG_ON_REBUILD  = int(os.getenv("REPORT_TG_ON_REBUILD", "0"))
REPORT_INCLUDE_HOURLY = int(os.getenv("REPORT_INCLUDE_HOURLY", "1"))

# Telegram & web
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Amsterdam")
DAILY_REPORT_HHMM = os.getenv("DAILY_REPORT_HHMM", "23:59")
USD_TO_EUR = float(os.getenv("USD_TO_EUR", "0.86"))
PORT = int(os.getenv("PORT", "10000"))

# ==== Order pacing / exchange safety ====
API_THROTTLE_MS = int(os.getenv("API_THROTTLE_MS", "300"))
MAX_NEW_ORDERS_PER_TICK = int(os.getenv("MAX_NEW_ORDERS_PER_TICK", "6"))
MAX_TP_PLACEMENTS_PER_TICK = int(os.getenv("MAX_TP_PLACEMENTS_PER_TICK", "8"))
CANCEL_BATCH_SIZE = int(os.getenv("CANCEL_BATCH_SIZE", "10"))
MIN_NOTIONAL_USDT = float(os.getenv("MIN_NOTIONAL_USDT", "6"))

def _sleep_throttle():
    time.sleep(max(API_THROTTLE_MS, 0) / 1000.0)

def _notional_ok(price, qty):
    # vermijd orders onder min notional (~MEXC-eis)
    return (price * qty) >= (MIN_NOTIONAL_USDT - 1e-9)

def place_ladder_spread(ex, side_levels, base_usdt, max_per_tick, spacing, center_adj, now_ms):
    """
    Doseer entries en TP's.
    side_levels: lijst met tuples (price, 'long'|'short') die al 'raak' zijn op basis van je close/hi/lo checks.
    In PAPER: gebruikt paper_place_buy/short + paper_place_tp_*
    In LIVE (DRY_RUN=0): plaatst echte limit orders; TP live is optioneel (nu uit).
    """
    placed = 0
    for px, side in side_levels:
        if placed >= max_per_tick:
            break
        qty = round(base_usdt / max(px, 1e-12), 6)
        if not _notional_ok(px, qty):
            continue
        try:
            if DRY_RUN:
                # PAPER — doe exact wat je al had: entry + TP
                if side == "long":
                    q = paper_place_buy(px, now_ms)
                    if q:
                        paper_place_tp_long(q, px, spacing, center_adj)
                else:
                    q = paper_place_short(px, now_ms)
                    if q:
                        paper_place_tp_short(q, px, spacing, center_adj)
            else:
                # LIVE — echte entry; TP live plaatsen kun je later aanzetten
                order_side = "buy" if side == "long" else "sell"
                params = {}
                ex.create_order(SYMBOL, "limit", order_side, qty, px, params=params)
                # TIP: live TP plaatsen? gebruik place_tp_batch(...) of interne TP-monitor
            placed += 1
            _sleep_throttle()
        except ccxt.NetworkError as e:
            logging.warning(f"Netwerkprobleem (entry): {e} – retry volgende tick")
            break
        except ccxt.ExchangeError as e:
            logging.warning(f"Exchange error (entry): {e} – skip")
            _sleep_throttle()
            continue
    return placed

def place_tp_batch(ex, tp_orders, max_per_tick):
    """
    (Voor LIVE later) Doseer TP-limitorders via de exchange.
    tp_orders: [{price, qty, side: 'sell'|'buy'}]
    """
    placed = 0
    for o in tp_orders:
        if placed >= max_per_tick:
            break
        px, qty, side = o["price"], o["qty"], o["side"]
        if not _notional_ok(px, qty):
            continue
        try:
            if DRY_RUN:
                # In PAPER worden TP's al intern gemanaged; niets te doen
                pass
            else:
                params = {}
                ex.create_order(SYMBOL, "limit", side, qty, px, params=params)
            placed += 1
            _sleep_throttle()
        except Exception as e:
            logging.warning(f"TP plaatsing faalde: {e}")
            _sleep_throttle()
            continue
    return placed

# ===== Globals =====
app = Flask(__name__)
_stop = Event()

runtime = {"mode":None,"center":None,"atr":None,"adx":None,"spacing":None,
           "don_low":None,"don_high":None,
           "long_notional":0.0,"short_notional":0.0,"global_notional":0.0,
           "last_report":None}
last_trade_ms = 0
last_paper_snapshot_min = None

fills = {}

# Paper state
paper = {
    "enabled": bool(PAPER_MODE and DRY_RUN),
    "usdt_trading": PAPER_USDT_START,
    "usdt_sparen":  0.0,
    "xrp": 0.0,                         # long inventory
    "realized_pnl_usdt": 0.0,
    "open_entries": [],                 # LONG lots [{qty, entry, fee_usdt, ts}]
    "short_entries": [],                # SHORT lots [{qty, entry, fee_usdt, ts}]
    "open_orders": [],                  # [{'id','type':'tp','side','price','qty','status'}]
    "closed_trades": []
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

# ===== Bias-aware grids (compact + lowvol) =====
def build_grids(center, atr_val, mode, total_layers, bias=None):
    """
    total_layers: effectief aantal lagen (kan dynamisch verlaagd worden bij low-vol)
    bias:
      None        = symmetrisch
      'bearish'   = meer shorts + center-shift omlaag
      'bullish'   = meer longs  + center-shift omhoog
    """
    spacing = max(1e-8, atr_val * ATR_MULT)
    if mode == "trend":
        spacing *= 1.8  # ruimer in trend

    long_layers  = total_layers
    short_layers = total_layers
    center_adj = center

    if bias == "bearish":
        short_layers = max(1, int(round(total_layers * BIAS_RATIO_SHORT)))
        long_layers  = max(1, total_layers - short_layers)
        center_adj   = center - (atr_val * BIAS_SHIFT_ATR)
    elif bias == "bullish":
        long_layers  = max(1, int(round(total_layers * BIAS_RATIO_LONG)))
        short_layers = max(1, total_layers - long_layers)
        center_adj   = center + (atr_val * BIAS_SHIFT_ATR)

    long_buys, short_sells = [], []
    for i in range(1, long_layers + 1):
        long_buys.append(round(center_adj - i * spacing, 8))
    for i in range(1, short_layers + 1):
        short_sells.append(round(center_adj + i * spacing, 8))

    return spacing, long_buys, short_sells, center_adj

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

# ===== Paper helpers (FIFO + sparen) =====
def paper_reset():
    paper.update({
        "usdt_trading": PAPER_USDT_START,
        "usdt_sparen":  0.0,
        "xrp": 0.0,
        "realized_pnl_usdt": 0.0,
        "open_entries": [],
        "short_entries": [],
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

def paper_place_tp_long(qty, entry_price, spacing, center):
    tp = compute_tp_price(TP_MODE, entry_price, spacing, center, "long")
    oid = f"PTP-L-{int(time.time()*1000)}-{tp}"
    paper["open_orders"].append({"id": oid, "type": "tp", "side": "sell", "price": tp, "qty": qty, "status": "open"})
    if NOTIFY_TP_PLACED:
        send_telegram(f"🧪 PAPER TP geplaatst SELL @ {tp:.6f}")

def paper_place_short(price, now_ts):
    usdt_notional = BASE_ORDER_USDT
    qty = round(usdt_notional / max(price, 1e-12), 6)
    fee_open = usdt_notional * PAPER_FEE_PCT
    paper["usdt_trading"] -= fee_open
    paper["short_entries"].append({"qty": qty, "entry": price, "fee_usdt": fee_open, "ts": now_ts})
    if NOTIFY_FILLS:
        send_telegram(f"🧪 PAPER SHORT {qty:g} @ {price:.6f} (fee {fee_open:.4f} USDT)")
    return qty

def paper_place_tp_short(qty, entry_price, spacing, center):
    tp = compute_tp_price(TP_MODE, entry_price, spacing, center, "short")
    oid = f"PTP-S-{int(time.time()*1000)}-{tp}"
    paper["open_orders"].append({"id": oid, "type": "tp", "side": "buy", "price": tp, "qty": qty, "status": "open"})
    if NOTIFY_TP_PLACED:
        send_telegram(f"🧪 PAPER TP geplaatst BUY @ {tp:.6f} (short)")

def _fifo_take(lots_key, qty_need):
    taken = []; qty_left = qty_need
    while qty_left > 1e-9 and paper[lots_key]:
        e = paper[lots_key][0]
        q = min(e["qty"], qty_left)
        part_fee = e["fee_usdt"] * (q / e["qty"])
        taken.append({"qty": q, "entry": e["entry"], "fee_usdt": part_fee, "ts": e["ts"]})
        e["qty"] -= q; qty_left -= q
        if e["qty"] <= 1e-9: paper[lots_key].pop(0)
    return taken, qty_need - qty_left

def paper_exec_tps(trigger_long_price, trigger_short_price, now_ts):
    eps_up  = 1.0 + PAPER_EPS_PCT
    eps_dn  = 1.0 - PAPER_EPS_PCT

    for o in list(paper["open_orders"]):
        if o["status"] != "open" or o["type"] != "tp": continue

        # ---- LONG sluiting ----
        if o["side"] == "sell":  # close LONG
            if trigger_long_price >= o["price"] * eps_up:
                qty = o["qty"]; price = o["price"]
                parts, filled = _fifo_take("open_entries", qty)
                if filled <= 0:
                    o["status"] = "filled"
                    continue

                proceeds = filled * price
                fee_sell = proceeds * PAPER_FEE_PCT
                cost = sum(p["qty"] * p["entry"] for p in parts)
                total_buy_fee = sum(p["fee_usdt"] for p in parts)
                avg_entry = cost / max(sum(p["qty"] for p in parts), 1e-12)
                pnl = proceeds - fee_sell - cost - total_buy_fee

                paper["usdt_trading"] += (proceeds - fee_sell)
                paper["xrp"] -= filled
                paper["realized_pnl_usdt"] += pnl
                paper["closed_trades"].append({
                    "entry": round(avg_entry,6), "exit": round(price,6), "qty": round(filled,6),
                    "pnl_usdt": round(pnl,6), "ts_entry": parts[0]["ts"] if parts else None, "ts_exit": now_ts
                })
                o["status"] = "filled"

                if NOTIFY_TP_SL:
                    send_telegram(f"🧪 PAPER SELL {filled:g} @ {price:.6f} (avg entry {avg_entry:.6f}) | PnL {pnl:+.4f} USDT")

                if SPAREN_ENABLED and pnl > SPAREN_MIN_PNL_USDT and SPAREN_SPLIT_PCT > 0:
                    to_save = min(pnl * (SPAREN_SPLIT_PCT/100.0), paper["usdt_trading"])
                    paper["usdt_trading"] -= to_save
                    paper["usdt_sparen"]  += to_save
                    if NOTIFY_SPAREN:
                        send_telegram(f"💰 PAPER sparen +{to_save:.4f} USDT ({SPAREN_SPLIT_PCT:.0f}% van winst)")

                if NOTIFY_SELL_OVERVIEW:
                    send_telegram(
                        f"📊 PAPER: Realized {paper['realized_pnl_usdt']:.4f} USDT | "
                        f"Trading {paper['usdt_trading']:.2f} | Sparen {paper['usdt_sparen']:.2f} | "
                        f"Open LONG {paper['xrp']:.4f} | "
                        f"Open SHORT {sum(e['qty'] for e in paper['short_entries']):.4f}"
                    )

        # ---- SHORT sluiting (buy-to-cover) ----
        elif o["side"] == "buy":
            if trigger_short_price <= o["price"] * eps_dn:
                qty = o["qty"]; price = o["price"]
                parts, filled = _fifo_take("short_entries", qty)
                if filled <= 0:
                    o["status"] = "filled"
                    continue

                cost_cover = filled * price
                fee_buy    = cost_cover * PAPER_FEE_PCT
                proceeds_open   = sum(p["qty"] * p["entry"] for p in parts)
                total_open_fee  = sum(p["fee_usdt"] for p in parts)
                avg_entry = proceeds_open / max(sum(p["qty"] for p in parts), 1e-12)
                pnl = proceeds_open - cost_cover - total_open_fee - fee_buy

                paper["usdt_trading"]      += pnl
                paper["realized_pnl_usdt"] += pnl
                paper["closed_trades"].append({
                    "entry": round(avg_entry,6), "exit": round(price,6), "qty": round(filled,6),
                    "pnl_usdt": round(pnl,6), "ts_entry": parts[0]["ts"] if parts else None, "ts_exit": now_ts
                })
                o["status"] = "filled"

                if NOTIFY_TP_SL:
                    send_telegram(f"🧪 PAPER BUY {filled:g} @ {price:.6f} (short cover; avg entry {avg_entry:.6f}) | PnL {pnl:+.4f} USDT")

                if SPAREN_ENABLED and pnl > SPAREN_MIN_PNL_USDT and SPAREN_SPLIT_PCT > 0:
                    to_save = min(pnl * (SPAREN_SPLIT_PCT/100.0), paper["usdt_trading"])
                    paper["usdt_trading"] -= to_save
                    paper["usdt_sparen"]  += to_save
                    if NOTIFY_SPAREN:
                        send_telegram(f"💰 PAPER sparen +{to_save:.4f} USDT ({SPAREN_SPLIT_PCT:.0f}% van winst)")

                if NOTIFY_SELL_OVERVIEW:
                    send_telegram(
                        f"📊 PAPER: Realized {paper['realized_pnl_usdt']:.4f} USDT | "
                        f"Trading {paper['usdt_trading']:.2f} | Sparen {paper['usdt_sparen']:.2f} | "
                        f"Open LONG {paper['xrp']:.4f} | "
                        f"Open SHORT {sum(e['qty'] for e in paper['short_entries']):.4f}"
                    )

    paper["open_orders"] = [o for o in paper["open_orders"] if o["status"] == "open"]

# ===== Rapportage =====
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
        short_qty = sum(e["qty"] for e in paper["short_entries"])
        lines.append(f"(Paper) Trading≈{paper['usdt_trading']:.2f} | Sparen≈{paper['usdt_sparen']:.2f} | XRP(long)≈{paper['xrp']:.4f} | SHORT_qty≈{short_qty:.4f} | Realized≈{paper['realized_pnl_usdt']:.2f}")
    lines.append(f"Tijd: {fmt_ts(now_utc())}")
    return "\n".join(lines)

def paper_summary_dict():
    return {
        "balances": {
            "USDT_trading": round(paper["usdt_trading"], 6),
            "USDT_sparen":  round(paper["usdt_sparen"],  6),
            "XRP_long":     round(paper["xrp"],          6),
            "XRP_short_qty": round(sum(e["qty"] for e in paper["short_entries"]), 6),
        },
        "realized_pnl_usdt": round(paper["realized_pnl_usdt"], 6),
        "open_entries": paper["open_entries"],
        "short_entries": paper["short_entries"],
        "open_tp_orders": paper["open_orders"],
        "closed_trades_count": len(paper["closed_trades"]),
    }

def build_closed_trades_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# PAPER SUMMARY"])
    s = paper_summary_dict()
    w.writerow(["USDT_trading", s["balances"]["USDT_trading"]])
    w.writerow(["USDT_sparen",  s["balances"]["USDT_sparen"]])
    w.writerow(["XRP_long",     s["balances"]["XRP_long"]])
    w.writerow(["XRP_short_qty",s["balances"]["XRP_short_qty"]])
    w.writerow(["Realized_PnL_USDT", s["realized_pnl_usdt"]])
    w.writerow([])
    w.writerow(["entry","exit","qty","pnl_usdt","ts_entry","ts_exit"])
    for t in paper["closed_trades"]:
        w.writerow([
            t.get("entry"), t.get("exit"), t.get("qty"), t.get("pnl_usdt"),
            t.get("ts_entry"), t.get("ts_exit")
        ])
    return buf.getvalue()

def compute_report_kpis():
    trades = paper.get("closed_trades", []) or []
    if not trades:
        return {
            "closed_trades": 0, "realized_total_usdt": 0.0, "win_rate_percent": None,
            "avg_pnl": None, "median_pnl": None
        }
    pnl_list = [float(t.get("pnl_usdt", 0.0)) for t in trades]
    wins = sum(1 for p in pnl_list if p > 0)
    win_rate = 100.0 * wins / max(1, len(pnl_list))
    avg_pnl = sum(pnl_list) / len(pnl_list)
    med_pnl = sorted(pnl_list)[len(pnl_list)//2]
    return {
        "closed_trades": len(pnl_list),
        "realized_total_usdt": round(sum(pnl_list), 6),
        "win_rate_percent": round(win_rate, 2),
        "avg_pnl": round(avg_pnl, 6),
        "median_pnl": round(med_pnl, 6)
    }

def build_report_csv(include_hourly=True):
    k = compute_report_kpis()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["# REPORT KPI"])
    for key, val in k.items():
        w.writerow([key, val])
    if include_hourly:
        w.writerow([])
        w.writerow(["# hourly_pnl"])
        w.writerow(["hour", "pnl_usdt"])
        hourly = {}
        for t in paper.get("closed_trades", []) or []:
            ts_exit = t.get("ts_exit")
            if not ts_exit: continue
            try:
                ts = pd.to_datetime(ts_exit, unit="ms", utc=True)
            except Exception:
                try: ts = pd.to_datetime(ts_exit)
                except Exception: continue
            hour_key = ts.tz_convert(None).strftime("%Y-%m-%d %H:00")
            hourly[hour_key] = hourly.get(hour_key, 0.0) + float(t.get("pnl_usdt", 0.0))
        for h, v in sorted(hourly.items()):
            w.writerow([h, round(v, 6)])
    return out.getvalue()

def send_report_to_telegram():
    k = compute_report_kpis()
    msg = (
        "<b>Grid Report</b>\n"
        f"Closed trades: <b>{k['closed_trades']}</b>\n"
        f"Realized PnL: <b>{k['realized_total_usdt']:.4f}</b> USDT\n"
        f"Win-rate: <b>{k['win_rate_percent'] if k['win_rate_percent'] is not None else 'n/a'}%</b>\n"
        f"Avg/Med PnL: {k['avg_pnl'] if k['avg_pnl'] is not None else 'n/a'} / {k['median_pnl'] if k['median_pnl'] is not None else 'n/a'} USDT"
    )
    send_telegram(msg)

def send_paper_snapshot_to_telegram():
    if not (TG_TOKEN and TG_CHAT_ID): return
    s = paper_summary_dict()
    msg = (
        "<b>PAPER snapshot</b>\n"
        f"Trading: <b>{s['balances']['USDT_trading']:.2f}</b> USDT\n"
        f"Sparen:  <b>{s['balances']['USDT_sparen']:.2f}</b> USDT\n"
        f"XRP long: <b>{s['balances']['XRP_long']:.4f}</b>\n"
        f"SHORT qty: <b>{s['balances']['XRP_short_qty']:.4f}</b>\n"
        f"Realized PnL: <b>{paper['realized_pnl_usdt']:.2f}</b> USDT\n"
        f"Closed trades: {len(paper['closed_trades'])}\n"
        f"Tijd: {fmt_ts(now_utc())}"
    )
    send_telegram(msg)

# ===== Bot thread =====
def bot_thread():
    global last_trade_ms, last_paper_snapshot_min
    ex = ccxt_client(); set_deriv_modes(ex)

    last_trade_ms = int(time.time() * 1000)

    state = load_state()
    last_atr   = state.get("last_atr")
    last_mode  = state.get("last_mode")
    last_center= state.get("last_center")
    grid_live  = state.get("grid_live", False)
    last_spacing = state.get("last_spacing")

    if paper["enabled"]:
        paper_reset()

    send_telegram(
        f"🔧 Bot gestart — {SYMBOL} | perp={USE_PERP} lev={LEVERAGE} hedge={HEDGE_MODE} iso={MARGIN_MODE=='isolated'} | "
        f"DRY_RUN={DRY_RUN} | PAPER={paper['enabled']} | L:{ENABLE_LONG} S:{ENABLE_SHORT}"
    )

    while not _stop.is_set():
        try:
            df_ltf = to_df(fetch_ohlcv(ex, TIMEFRAME))
            df_htf = to_df(fetch_ohlcv(ex, HTF))
            d, don_low, don_high = compute_indicators(df_ltf, df_htf)

            close  = float(d["close"].iloc[-1])
            hi     = float(d["high"].iloc[-1])
            lo     = float(d["low"].iloc[-1])
            atr    = float(d["atr"].iloc[-1])
            adx    = float(d["adx"].iloc[-1])
            ema200 = float(d["ema200"].iloc[-1])

            mode   = decide_regime(adx)
            center = min(max(ema200, don_low), don_high)
            lower_break = don_low  - atr * BREAKOUT_ATR_MULT
            upper_break = don_high + atr * BREAKOUT_ATR_MULT

            # ---- bias (alleen in trend mode)
            bias = None
            if mode == "trend":
                if BEARISH_BIAS and close < ema200:
                    bias = "bearish"
                elif BULLISH_BIAS and close > ema200:
                    bias = "bullish"

            # ==== Low-vol check: bepaal effectieve lagen ====
            median_atr = float(d["atr"].rolling(200, min_periods=50).median().iloc[-1])
            eff_layers = GRID_LAYERS
            if atr < median_atr * VOL_MIN_MULT:
                eff_layers = max(2, int(round(GRID_LAYERS * LOWVOL_LAYER_FACTOR)))

            need_rebuild = (last_atr is None or last_mode is None or last_center is None or not grid_live)
            if not need_rebuild:
                if abs(atr - last_atr) / max(1e-8, last_atr) > REBUILD_ATR_DELTA: need_rebuild = True
                if mode != last_mode: need_rebuild = True
                if close < lower_break or close > upper_break:
                    if NOTIFY_REBUILD:
                        send_telegram(f"⚠️ Breakout: close {close:.6f} buiten [{lower_break:.6f}, {upper_break:.6f}] — rebuild")
                    need_rebuild = True

            if need_rebuild:
                last_spacing, long_buys, short_sells, center_adj = build_grids(center, atr, mode, eff_layers, bias=bias)
                center = center_adj
                grid_live = True; last_atr, last_mode, last_center = atr, mode, center
                save_state({
                    "last_atr": last_atr, "last_mode": last_mode, "last_center": last_center, "grid_live": grid_live,
                    "last_spacing": last_spacing, "updated_at": now_utc().isoformat(),
                    "last_levels": {"long_buys": long_buys, "short_sells": short_sells}
                })
                if NOTIFY_REBUILD:
                    msg = (
                        f"🔁 Rebuild grid — Mode <b>{mode}</b> | ADX≈{adx:.1f} | ATR≈{atr:.6f}\n"
                        f"Center≈{center:.6f} | Spacing≈{last_spacing:.6f} | Layers={eff_layers}\n"
                        f"HTF: [{don_low:.6f} .. {don_high:.6f}]"
                    )
                    if bias == "bearish":
                        msg += "\n🧭 Bias: <b>bearish</b> (meer shorts + center↓)"
                    elif bias == "bullish":
                        msg += "\n🧭 Bias: <b>bullish</b> (meer longs + center↑)"
                    send_telegram(msg)
                if REPORT_TG_ON_REBUILD:
                    send_report_to_telegram()

                        # === ENTRIES & TP (PAPER) — paced/dosed ===
            if paper["enabled"]:
                spacing, long_buys, short_sells, center_adj = build_grids(center, atr, mode, eff_layers, bias=bias)

                long_levels = []
                short_levels = []
                now_ms = int(time.time() * 1000)

                if ENABLE_LONG:
                    # neem alleen levels die "raak" zijn
                    for px in long_buys:
                        if close <= px:
                            long_levels.append((px, "long"))

                if ENABLE_SHORT:
                    for px in short_sells:
                        if close >= px:
                            short_levels.append((px, "short"))

                # Gedoseerd entries plaatsen; in PAPER zet dit óók meteen de bijbehorende TP per entry
                _ = place_ladder_spread(
                    ex, long_levels, BASE_ORDER_USDT, MAX_NEW_ORDERS_PER_TICK, spacing, center_adj, now_ms
                )
                _ = place_ladder_spread(
                    ex, short_levels, BASE_ORDER_USDT, MAX_NEW_ORDERS_PER_TICK, spacing, center_adj, now_ms
                )

                # TP-executie (PAPER behandelt TP's intern)
                trigger_long = hi if TP_TRIGGER_MODE == "wick" else close
                trigger_short = lo if TP_TRIGGER_MODE == "wick" else close
                paper_exec_tps(trigger_long, trigger_short, now_ms)


                trigger_long  = hi if TP_TRIGGER_MODE == "wick" else close
                trigger_short = lo if TP_TRIGGER_MODE == "wick" else close
                paper_exec_tps(trigger_long, trigger_short, int(time.time()*1000))

            # === Runtime + rapporten ===
            runtime.update({
                "mode": mode, "center": center, "atr": atr, "adx": adx,
                "spacing": last_spacing or (atr * ATR_MULT),
                "don_low": don_low, "don_high": don_high,
                "long_notional": 0.0, "short_notional": 0.0, "global_notional": 0.0
            })

            # Dagrapport
            if datetime.now().strftime("%H:%M") == DAILY_REPORT_HHMM:
                stamp = datetime.now().strftime("%Y%m%d%H%M")
                if runtime.get("last_report") != stamp:
                    send_telegram(daily_report_text(ex))
                    runtime["last_report"] = stamp

            # Uurlijkse (of N-min) paper-snapshot (default uit)
            if PAPER_SNAPSHOT_ENABLED and paper["enabled"]:
                try:
                    minute = int(datetime.now().strftime("%M"))
                    nowm = datetime.now().strftime("%Y-%m-%d %H:%M")
                    if (minute % max(PAPER_SNAPSHOT_MIN, 1) == 0) and (last_paper_snapshot_min != nowm):
                        send_paper_snapshot_to_telegram()
                        last_paper_snapshot_min = nowm
                except Exception:
                    pass

            logging.info(f"Tick | close={close:.6f} ADX={adx:.1f} ATR={atr:.6f} mode={mode} bias={bias} layers={eff_layers}")

        except Exception as e:
            logging.exception(f"Loop error: {e}")

        time.sleep(POLL_SEC)

# ===== Flask =====
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"ok": True, "symbol": SYMBOL, "perp": bool(USE_PERP), "hedge": bool(HEDGE_MODE),
                    "paper": paper["enabled"], "ts": fmt_ts(now_utc()),
                    "enable_long": bool(ENABLE_LONG), "enable_short": bool(ENABLE_SHORT)})

@app.get("/state")
def state_ep():
    try:
        ex = ccxt_client()
        xrep = fetch_balances_report(ex)
    except Exception:
        xrep = None
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
                     "XRP_long":     round(paper["xrp"],          6),
                     "XRP_short_qty": round(sum(e["qty"] for e in paper["short_entries"]), 6)},
        "realized_pnl_usdt": round(paper["realized_pnl_usdt"], 4),
        "open_entries": paper["open_entries"],
        "short_entries": paper["short_entries"],
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
def paper_reset_route():
    confirm = request.args.get("confirm", "")
    if confirm.lower() not in ("ja", "yes", "ok", "ikweethet", "confirm"):
        return jsonify({"ok": False, "msg": "Bevestig met ?confirm=ikweethet"}), 400
    paper_reset()
    return jsonify({"ok": True, "msg": "Paper reset", "state": paper_summary_dict()})

# ---- Report endpoints ----
@app.get("/report/export")
def report_export_ep():
    inc_hourly = bool(REPORT_INCLUDE_HOURLY)
    csv_text = build_report_csv(include_hourly=inc_hourly)
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=report_summary.csv"})

@app.post("/report/telegram")
def report_telegram_ep():
    send_report_to_telegram()
    return jsonify({"ok": True, "msg": "Report to Telegram verzonden."})

@app.route("/report/table")
def report_table():
    # haal gesloten trades uit dezelfde bron die je voor /paper/export gebruikt
    # verwacht structuur met keys: entry, exit, qty, pnl_usdt, ts_entry, ts_exit
    rows = []
    try:
        # Als je al een paper-state hebt zoals paper["closed"], gebruik die:
        global paper
        rows = paper.get("closed", []) if isinstance(paper, dict) else []

        # fallback: als je elders een lijst houdt (pas zo nodig aan jouw variabelenaam)
        if not rows and "paper_closed" in globals():
            rows = paper_closed  # type: ignore

    except Exception:
        rows = []

    # niets te tonen?
    if not rows:
        return (
            "<h3>Geen gesloten trades gevonden</h3>"
            "<p>Wacht op een paar 🧪 PAPER SELL regels of run /paper/export, "
            "en refresh dan deze pagina.</p>",
            200,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    # maak nette koppen & bereken KPI's
    import math
    def to_float(v):
        try:
            return float(v)
        except Exception:
            return math.nan

    # sorteren op ts_exit indien aanwezig
    rows_sorted = sorted(
        rows,
        key=lambda r: (str(r.get("ts_exit", "")), str(r.get("ts_entry", "")))
    )

    # cumulatieve PnL, winrate, enz.
    realized = 0.0
    wins = 0
    total = 0
    table_rows_html = []
    for r in rows_sorted:
        entry = to_float(r.get("entry"))
        exitp = to_float(r.get("exit"))
        qty = to_float(r.get("qty"))
        pnl = to_float(r.get("pnl_usdt"))
        ts_e = r.get("ts_entry", "")
        ts_x = r.get("ts_exit", "")

        realized += (0.0 if math.isnan(pnl) else pnl)
        total += 1
        if not math.isnan(pnl) and pnl > 0:
            wins += 1

        # cumulatieve kolom tonen
        cum = realized

        table_rows_html.append(
            f"<tr>"
            f"<td>{ts_e}</td>"
            f"<td>{ts_x}</td>"
            f"<td>{qty:.6f}</td>"
            f"<td>{entry:.6f}</td>"
            f"<td>{exitp:.6f}</td>"
            f"<td>{pnl:.6f}</td>"
            f"<td>{cum:.6f}</td>"
            f"</tr>"
        )

    winrate = (wins / total * 100.0) if total else 0.0

    kpi_html = (
        "<h3>KPI’s — huidige run</h3>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>Closed trades</th><th>Realized PnL (USDT)</th><th>Win rate</th></tr>"
        f"<tr><td>{total}</td><td>{realized:.6f}</td><td>{winrate:.2f}%</td></tr>"
        "</table><br/>"
    )

    html = (
        "<!DOCTYPE html><meta charset='utf-8'/>"
        "<style>body{font-family:system-ui,Segoe UI,Arial,sans-serif} "
        "table{border-collapse:collapse;font-size:14px} "
        "th,td{padding:6px 10px} th{background:#f3f4f6}</style>"
        + kpi_html +
        "<h3>Gesloten trades</h3>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr>"
        "<th>ts_entry</th><th>ts_exit</th>"
        "<th>qty</th><th>entry</th><th>exit</th>"
        "<th>pnl_usdt</th><th>cum_pnl_usdt</th>"
        "</tr>"
        + "".join(table_rows_html) +
        "</table>"
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    t = Thread(target=bot_thread, daemon=True); t.start()
    try:
        app.run(host="0.0.0.0", port=PORT)
    finally:
        _stop.set(); t.join(timeout=5)
