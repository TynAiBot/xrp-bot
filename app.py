import os, time, json, math, logging
from datetime import datetime, timezone
from threading import Thread, Event
import pandas as pd
import pandas_ta as ta
import ccxt, requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ===== ENV =====
EXCHANGE = os.getenv("EXCHANGE", "mexc")
API_KEY  = os.getenv("MEXC_API_KEY", os.getenv("API_KEY", ""))
API_SECRET = os.getenv("MEXC_API_SECRET", os.getenv("API_SECRET", ""))
SYMBOL  = os.getenv("SYMBOL", "XRP/USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
HTF = os.getenv("HTF", "1h")

USE_PERP = int(os.getenv("USE_PERP", "0"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
HEDGE_MODE = int(os.getenv("HEDGE_MODE", "0"))
DERIV_SETUP = int(os.getenv("DERIV_SETUP", "0"))

ENABLE_LONG  = int(os.getenv("ENABLE_LONG", "1"))
ENABLE_SHORT = int(os.getenv("ENABLE_SHORT", "0"))

GRID_LAYERS = int(os.getenv("GRID_LAYERS", "6"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
ATR_MULT = float(os.getenv("ATR_MULT", "0.9"))
ADX_LEN = int(os.getenv("ADX_LEN", "14"))
ADX_RANGE_TH = float(os.getenv("ADX_RANGE_TH", "20"))
DONCHIAN_LEN = int(os.getenv("DONCHIAN_LEN","100"))

BASE_ORDER_USDT   = float(os.getenv("BASE_ORDER_USDT", "20"))
MAX_OPEN_NOTIONAL = float(os.getenv("MAX_OPEN_NOTIONAL", "1000"))
MAX_NOTIONAL_LONG = float(os.getenv("MAX_NOTIONAL_LONG","600"))
MAX_NOTIONAL_SHORT= float(os.getenv("MAX_NOTIONAL_SHORT","600"))

POST_ONLY = int(os.getenv("POST_ONLY","0"))
REDUCE_ONLY_TP = int(os.getenv("REDUCE_ONLY_TP","0"))

POLL_SEC = int(os.getenv("POLL_SEC","15"))
REBUILD_ATR_DELTA = float(os.getenv("REBUILD_ATR_DELTA","0.2"))
BREAKOUT_ATR_MULT = float(os.getenv("BREAKOUT_ATR_MULT","1.0"))
DRY_RUN = int(os.getenv("DRY_RUN","1"))

STATE_FILE = os.getenv("STATE_FILE","grid_state.json")
MEXC_RECVWINDOW_MS = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
CCXT_TIMEOUT_MS = int(os.getenv("CCXT_TIMEOUT_MS","7000"))

# TP/SL & fills
TP_MODE = os.getenv("TP_MODE","spacing")       # midline | spacing | pct
TP_SPACING_MULT = float(os.getenv("TP_SPACING_MULT","1.0"))
TP_PCT = float(os.getenv("TP_PCT","0.004"))
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT","2.0"))
NOTIFY_FILLS = int(os.getenv("NOTIFY_FILLS","1"))
NOTIFY_TP_SL = int(os.getenv("NOTIFY_TP_SL","1"))

# Trades fetch window (live fills)
TRADES_LOOKBACK_S = int(os.getenv("TRADES_LOOKBACK_S", "0"))

# Paper trading
PAPER_MODE = int(os.getenv("PAPER_MODE","1"))
PAPER_USDT_START = float(os.getenv("PAPER_USDT_START","2000"))
PAPER_FEE_PCT = float(os.getenv("PAPER_FEE_PCT","0.0006"))

# Telegram & web
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")
TIMEZONE_STR = os.getenv("TIMEZONE","Europe/Amsterdam")
DAILY_REPORT_HHMM = os.getenv("DAILY_REPORT_HHMM","23:59")
USD_TO_EUR = float(os.getenv("USD_TO_EUR","0.86"))
PORT = int(os.getenv("PORT","10000"))

# ===== Globals =====
app = Flask(__name__)
_stop = Event()

runtime = {"mode":None,"center":None,"atr":None,"adx":None,"spacing":None,
           "don_low":None,"don_high":None,"long_notional":0.0,"short_notional":0.0,
           "global_notional":0.0,"last_report":None}
last_trade_ms = 0
fills = {}  # real/perp or spot fills (dict)

# Paper state
paper = {
    "enabled": bool(PAPER_MODE and DRY_RUN),
    "usdt": PAPER_USDT_START,
    "xrp": 0.0,
    "realized_pnl_usdt": 0.0,
    "open_orders": [],   # list of dicts: {id,type,side,price,qty,linked_tp,created_at,status}
    "closed_trades": []  # {side, entry, exit, qty, pnl_usdt, ts_entry, ts_exit}
}

# ===== Utils =====
def now_utc(): return datetime.now(timezone.utc)
def fmt_ts(dt): return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")

def send_telegram(msg:str):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id":TG_CHAT_ID,"text":msg,"parse_mode":"HTML","disable_web_page_preview":True}, timeout=10)
    except Exception as e:
        logging.warning(f"Telegram failed: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE,"r") as f: return json.load(f)
    return {}
def save_state(s):
    with open(STATE_FILE,"w") as f: json.dump(s,f,indent=2)

def ccxt_client():
    if EXCHANGE!="mexc": raise RuntimeError("Alleen 'mexc' in dit script.")
    opts = {"apiKey":API_KEY,"secret":API_SECRET,"enableRateLimit":True,"timeout":CCXT_TIMEOUT_MS}
    opts["options"]={"defaultType":"swap" if USE_PERP else "spot","recvWindow":MEXC_RECVWINDOW_MS}
    ex = ccxt.mexc(opts); ex.load_markets(); return ex

def set_deriv_modes(ex):
    if (not USE_PERP) or (not DERIV_SETUP):
        logging.info("Derivatives setup via API overgeslagen (USE_PERP=0 of DERIV_SETUP=0).")
        return
    try:
        try:
            ex.set_margin_mode(MARGIN_MODE, SYMBOL)
            logging.info(f"Margin mode='{MARGIN_MODE}' ingesteld.")
        except Exception as e:
            logging.info(f"set_margin_mode: {e}")
        try:
            params = {"openType": 1 if MARGIN_MODE=="isolated" else 2}
            ex.set_leverage(LEVERAGE, SYMBOL, params=params)
            logging.info(f"Leverage={LEVERAGE} ingesteld.")
        except Exception as e:
            logging.info(f"set_leverage: {e}")
        try:
            ex.set_position_mode(bool(HEDGE_MODE), SYMBOL)
            logging.info(f"Hedge mode={'ON' if HEDGE_MODE else 'OFF'}.")
        except Exception as e:
            logging.info(f"set_position_mode: {e}")
    except Exception as e:
        logging.warning(f"Derivatives modes setup mislukte: {e}")

def fetch_ohlcv(ex, tf): return ex.fetch_ohlcv(SYMBOL, timeframe=tf, limit=500)
def to_df(ohlcv):
    d = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    d["ts"]=pd.to_datetime(d["ts"],unit="ms",utc=True); return d

def compute_indicators(df_ltf: pd.DataFrame, df_htf: pd.DataFrame):
    d = df_ltf.copy()
    d["atr"]=ta.atr(d["high"],d["low"],d["close"],length=ATR_LEN)
    adx=ta.adx(d["high"],d["low"],d["close"],length=ADX_LEN)
    d["adx"]=adx[f"ADX_{ADX_LEN}"]
    d["ema200"]=ta.ema(d["close"],length=200)

    h = df_htf.copy()
    don_high = float(h["high"].rolling(window=DONCHIAN_LEN, min_periods=1).max().iloc[-1])
    don_low  = float(h["low"] .rolling(window=DONCHIAN_LEN, min_periods=1).min().iloc[-1])
    return d, don_low, don_high

def decide_regime(adx_val): return "range" if adx_val < ADX_RANGE_TH else "trend"

def build_grids(center, atr_val, mode):
    spacing=max(1e-8, atr_val*ATR_MULT)
    if mode=="trend": spacing*=1.8
    long_buys, short_sells=[], []
    for i in range(1, GRID_LAYERS+1):
        long_buys.append(round(center - i*spacing,8))
        short_sells.append(round(center + i*spacing,8))
    return spacing, long_buys, short_sells

def compute_tp_price(mode, entry, spacing, center, side):
    if mode=="midline":
        target = center
        if side=="long" and target<=entry: target = entry + spacing
        if side=="short" and target>=entry: target = entry - spacing
        return target
    elif mode=="spacing":
        return entry + (spacing*TP_SPACING_MULT if side=="long" else -spacing*TP_SPACING_MULT)
    elif mode=="pct":
        delta = entry*TP_PCT
        return entry + (delta if side=="long" else -delta)
    return entry + (spacing if side=="long" else -spacing)

def compute_sl_price(entry, atr, side):
    return entry - ATR_SL_MULT*atr if side=="long" else entry + ATR_SL_MULT*atr

def can_add(side_total, add, side_cap, g_total, g_cap):
    return (side_total+add)<=side_cap and (g_total+add)<=g_cap

def place_limit(ex, side, price, usdt, reduce_only=False, pos_side=None):
    if DRY_RUN: return {"id":"DRY","price":price,"side":side,"reduceOnly":reduce_only,"posSide":pos_side}
    amount=round(usdt/max(price,1e-12),6)
    params={}
    if POST_ONLY: params["postOnly"]=True
    if reduce_only: params["reduceOnly"]=True
    if pos_side: params["positionSide"]="LONG" if pos_side=="long" else "SHORT"
    return ex.create_order(SYMBOL, type="limit", side=side, amount=amount, price=price, params=params)

def place_market_reduce_only(ex, side, amount_base, pos_side=None):
    if DRY_RUN: return {"id":"DRY_MKT","side":side,"amount":amount_base,"reduceOnly":True}
    params={"reduceOnly":True}
    if pos_side: params["positionSide"]="LONG" if pos_side=="long" else "SHORT"
    return ex.create_order(SYMBOL, type="market", side=side, amount=amount_base, params=params)

def cancel_all(ex):
    if DRY_RUN: return
    try: ex.cancel_all_orders(SYMBOL)
    except Exception as e: logging.warning(f"cancel_all: {e}")

def fetch_futures_wallet(ex):
    try: bal=ex.fetch_balance({"type":"swap","recvWindow":MEXC_RECVWINDOW_MS})
    except Exception:
        try: bal=ex.fetch_balance({"recvWindow":MEXC_RECVWINDOW_MS})
        except Exception as e: logging.warning(f"fetch_balance: {e}"); return None
    return bal

def daily_report(ex):
    bal=fetch_futures_wallet(ex)
    usdt_free=usdt_used=usdt_total=0.0
    if bal and "USDT" in (bal.get("total") or {}):
        usdt_free=float((bal.get("free") or {}).get("USDT",0.0))
        usdt_used=float((bal.get("used") or {}).get("USDT",0.0))
        usdt_total=float((bal.get("total") or {}).get("USDT",usdt_free+usdt_used))
    eur=usdt_total*USD_TO_EUR
    return (
        "<b>Dagrapport — Grid Bot</b>\n"
        f"Symbool: <b>{SYMBOL}</b>\n"
        f"Mode: <b>{runtime.get('mode')}</b> | ADX≈{(runtime.get('adx') or 0):.1f} | ATR≈{(runtime.get('atr') or 0):.6f}\n"
        f"Center≈{(runtime.get('center') or 0):.6f} | Spacing≈{(runtime.get('spacing') or 0):.6f}\n"
        f"HTF: [{(runtime.get('don_low') or 0):.6f} .. {(runtime.get('don_high') or 0):.6f}]\n\n"
        f"(Exchange) USDT≈<b>{usdt_total:.2f}</b> (free {usdt_free:.2f} / used {usdt_used:.2f}) ≈ €{eur:.2f}\n"
        + (f"(Paper) USDT≈{paper['usdt']:.2f} | XRP≈{paper['xrp']:.4f} | Realized PnL≈{paper['realized_pnl_usdt']:.2f}\n" if paper["enabled"] else "")
        + f"Tijd: {fmt_ts(now_utc())}"
    )

def time_matches(hhmm):
    try: hh,mm=map(int, hhmm.split(":"))
    except: return False
    loc=datetime.now().astimezone()
    return loc.hour==hh and loc.minute==mm

# ===== Paper helpers =====
def paper_reset():
    paper.update({"usdt": PAPER_USDT_START, "xrp": 0.0, "realized_pnl_usdt": 0.0, "open_orders": [], "closed_trades": []})

def paper_place_buy(price, now_ts):
    usdt = BASE_ORDER_USDT
    if paper["usdt"] < usdt: return False
    qty = round(usdt / max(price,1e-12), 6)
    fee = usdt * PAPER_FEE_PCT
    paper["usdt"] -= (usdt + fee)
    paper["xrp"]  += qty
    oid = f"PBUY-{now_ts}-{price}"
    paper["open_orders"].append({"id": oid, "type":"tp", "side":"sell", "price": None, "qty": qty, "status":"open"})
    if NOTIFY_FILLS: send_telegram(f"🧪 PAPER BUY {qty:g} @ {price:.6f} (fee {fee:.4f} USDT)")
    return oid, qty

def paper_set_tp(entry_price, spacing, center, side, last_atr):
    tp = compute_tp_price(TP_MODE, entry_price, spacing, center, side)
    # laatste open tp order krijgt prijs
    for o in reversed(paper["open_orders"]):
        if o["type"]=="tp" and o["price"] is None:
            o["price"]=tp
            if NOTIFY_TP_SL: send_telegram(f"🧪 PAPER TP set SELL @ {tp:.6f}")
            break

def paper_check_exec(close, now_ts):
    """execute TP sells when close >= tp price"""
    executed=[]
    for o in list(paper["open_orders"]):
        if o["status"]!="open" or o["type"]!="tp" or o["price"] is None: continue
        if close >= o["price"]:
            qty=o["qty"]; price=o["price"]
            proceeds = qty * price
            fee = proceeds * PAPER_FEE_PCT
            paper["usdt"] += (proceeds - fee)
            paper["xrp"]  -= qty
            o["status"]="filled"; executed.append(o["id"])
            paper["realized_pnl_usdt"] += (proceeds - fee) - (qty * price)  # approx 0; PnL zou op entry vs exit moeten, maar we boeken netto in usdt
            if NOTIFY_TP_SL: send_telegram(f"🧪 PAPER SELL {qty:g} @ {price:.6f} (fee {fee:.4f} USDT)")
    # opruimen
    paper["open_orders"] = [o for o in paper["open_orders"] if o["status"]=="open"]

# ===== Bot thread =====
def bot_thread():
    global last_trade_ms
    ex=ccxt_client(); set_deriv_modes(ex)

    last_trade_ms = int(time.time() * 1000) - TRADES_LOOKBACK_S * 1000
    state=load_state()
    last_atr, last_mode, last_center = state.get("last_atr"), state.get("last_mode"), state.get("last_center")
    grid_live = state.get("grid_live", False)
    last_spacing = state.get("last_spacing")

    total_g=total_l=total_s=0.0
    if paper["enabled"]: paper_reset()

    send_telegram(f"🔧 Bot gestart — {SYMBOL} | perp={USE_PERP} lev={LEVERAGE} hedge={HEDGE_MODE} iso={MARGIN_MODE=='isolated'} | DRY_RUN={DRY_RUN} | PAPER={paper['enabled']}")

    while not _stop.is_set():
        try:
            df_ltf, df_htf = to_df(fetch_ohlcv(ex,TIMEFRAME)), to_df(fetch_ohlcv(ex,HTF))
            d, don_low, don_high = compute_indicators(df_ltf, df_htf)
            close=float(d["close"].iloc[-1]); atr=float(d["atr"].iloc[-1]); adx=float(d["adx"].iloc[-1]); ema200=float(d["ema200"].iloc[-1])

            mode=decide_regime(adx)
            center=min(max(ema200,don_low),don_high)
            lower_break=don_low-atr*BREAKOUT_ATR_MULT
            upper_break=don_high+atr*BREAKOUT_ATR_MULT

            need_rebuild = (last_atr is None or last_mode is None or last_center is None or not grid_live)
            if not need_rebuild:
                if abs(atr-last_atr)/max(1e-8,last_atr)>REBUILD_ATR_DELTA: need_rebuild=True
                if mode!=last_mode: need_rebuild=True
                if close<lower_break or close>upper_break:
                    send_telegram(f"⚠️ Breakout: close {close:.6f} buiten [{lower_break:.6f}, {upper_break:.6f}] — rebuild")
                    need_rebuild=True

            if need_rebuild:
                last_spacing,long_buys,short_sells = build_grids(center,atr,mode)
                grid_live=True; last_atr, last_mode, last_center = atr, mode, center
                save_state({"last_atr":last_atr,"last_mode":last_mode,"last_center":last_center,"grid_live":grid_live,
                            "last_spacing":last_spacing,"updated_at":now_utc().isoformat(),
                            "last_levels":{"long_buys":long_buys,"short_sells":short_sells}})
                send_telegram(f"🔁 Rebuild grid — Mode <b>{mode}</b> | ADX≈{adx:.1f} | ATR≈{atr:.6f}\nCenter≈{center:.6f} | Spacing≈{last_spacing:.6f}\nHTF: [{don_low:.6f} .. {don_high:.6f}]")

            # === PAPER SIM: trigger buys op gridbreuk (spot long-only) ===
            if paper["enabled"] and ENABLE_LONG:
                # We bouwen actuele long-buys opnieuw om richting te weten
                spacing,long_buys,_ = build_grids(center,atr,mode)
                # Voor elk buy-level dat de close kruist -> simulate BUY + set TP
                for px in long_buys:
                    # simpel: als close <= level en we hebben genoeg USDT, koop
                    if close <= px and paper["usdt"] >= BASE_ORDER_USDT:
                        oid_qty = paper_place_buy(px, int(time.time()*1000))
                        if oid_qty:
                            _, qty = oid_qty
                            # bepaal TP en zet in open_orders
                            tp_price = compute_tp_price(TP_MODE, px, spacing, center, "long")
                            paper["open_orders"].append({"id": f"PTP-{int(time.time()*1000)}-{tp_price}",
                                                         "type":"tp","side":"sell","price":tp_price,"qty":qty,
                                                         "status":"open"})
                            if NOTIFY_TP_SL: send_telegram(f"🧪 PAPER TP geplaatst SELL @ {tp_price:.6f}")

                # Check of TP's raken
                paper_check_exec(close, int(time.time()*1000))

            # === REAL TRADES FETCH (alleen als we niet in pure paper willen leunen) ===
            if not paper["enabled"]:
                try:
                    trades = ex.fetch_my_trades(SYMBOL, since=last_trade_ms or None, limit=100)
                except Exception as e:
                    trades=[]; logging.debug(f"fetch_my_trades: {e}")

                new_max_ms = last_trade_ms
                for tr in trades or []:
                    ts = int(tr.get("timestamp") or 0)
                    if ts and ts <= last_trade_ms: continue
                    new_max_ms = max(new_max_ms, ts or 0)

                    side = tr.get("side")
                    info = tr.get("info", {})
                    pos_side = (info.get("positionSide") or info.get("posSide") or "").upper()

                    if USE_PERP:
                        my_pos_side = "LONG" if (pos_side=="LONG" or (HEDGE_MODE and side=="buy")) else ("SHORT" if (pos_side=="SHORT" or (HEDGE_MODE and side=="sell")) else None)
                    else:
                        if side == "sell":  # spot: sells niet als nieuwe fill tellen
                            continue
                        my_pos_side = "LONG"

                    price = float(tr.get("price") or tr.get("info",{}).get("price") or 0)
                    amount = float(tr.get("amount") or tr.get("contracts") or 0)
                    if amount<=0 or price<=0: continue

                    fid = tr.get("id") or f"{ts}-{side}-{price}"
                    if fid in fills: continue

                    fills[fid] = {"side":"long" if my_pos_side=="LONG" else "short",
                                  "entry_price": price, "qty": amount, "tp": None, "sl": None, "active": True}
                    if NOTIFY_FILLS:
                        send_telegram(f"✅ Fill: {fills[fid]['side'].upper()} {amount:g} @ {price:.6f}")

                    spacing = last_spacing or (last_atr*ATR_MULT if last_atr else atr*ATR_MULT)
                    tp_price = compute_tp_price(TP_MODE, price, spacing, last_center if last_center is not None else center, fills[fid]["side"])
                    try:
                        side_out = "sell" if fills[fid]["side"]=="long" else "buy"
                        pos_tag = "long" if fills[fid]["side"]=="long" else "short"
                        o = place_limit(
                            ex, side_out, tp_price, usdt=price*amount,
                            reduce_only=bool(REDUCE_ONLY_TP) if USE_PERP else False,
                            pos_side=pos_tag if (USE_PERP and HEDGE_MODE) else None
                        )
                        fills[fid]["tp"] = {"id": o.get("id","TP"), "price": tp_price}
                        if NOTIFY_TP_SL: send_telegram(f"🎯 TP geplaatst: {side_out.upper()} {'RO ' if (USE_PERP and REDUCE_ONLY_TP) else ''}@ {tp_price:.6f} (entry {price:.6f})")
                    except Exception as e:
                        logging.warning(f"TP place fail: {e}")

                    sl_price = compute_sl_price(price, last_atr if last_atr is not None else atr, fills[fid]["side"])
                    fills[fid]["sl"] = {"price": sl_price}

                if new_max_ms>last_trade_ms: last_trade_ms = new_max_ms

            # Runtime + dagrapport
            runtime.update({
                "mode":mode,"center":center,"atr":atr,"adx":adx,"spacing":last_spacing or (atr*ATR_MULT),
                "don_low":don_low,"don_high":don_high,
                "long_notional":total_l,"short_notional":total_s,"global_notional":total_g
            })

            if time_matches(DAILY_REPORT_HHMM):
                stamp=datetime.now().strftime("%Y%m%d%H%M")
                if runtime.get("last_report")!=stamp:
                    send_telegram(daily_report(ex))
                    runtime["last_report"]=stamp

            logging.info(f"Tick | close={close:.6f} ADX={adx:.1f} ATR={atr:.6f} mode={mode}")

        except Exception as e:
            logging.exception(f"Loop error: {e}")

        time.sleep(POLL_SEC)

# ===== Flask =====
@app.get("/health")
def health(): return jsonify({"ok":True,"symbol":SYMBOL,"perp":bool(USE_PERP),"hedge":bool(HEDGE_MODE),"paper":paper["enabled"],"ts":fmt_ts(now_utc())})

@app.get("/state")
def state_ep(): return jsonify({"runtime":runtime,"fills_active":len(fills),"paper_enabled":paper["enabled"]})

@app.get("/paper")
def paper_ep():
    if not paper["enabled"]:
        return jsonify({"paper_enabled": False, "hint": "Zet PAPER_MODE=1 en DRY_RUN=1 in .env"}), 200
    return jsonify({
        "paper_enabled": True,
        "balances": {"USDT": round(paper["usdt"], 4), "XRP": round(paper["xrp"], 6)},
        "realized_pnl_usdt": round(paper["realized_pnl_usdt"], 4),
        "open_orders": paper["open_orders"],
        "closed_trades_count": len(paper["closed_trades"])
    })

if __name__=="__main__":
    t=Thread(target=bot_thread,daemon=True); t.start()
    try: app.run(host="0.0.0.0", port=PORT)
    finally: _stop.set(); t.join(timeout=5)
