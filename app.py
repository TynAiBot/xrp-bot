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

USE_PERP = int(os.getenv("USE_PERP", "1"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
HEDGE_MODE = int(os.getenv("HEDGE_MODE", "1"))

ENABLE_LONG  = int(os.getenv("ENABLE_LONG", "1"))
ENABLE_SHORT = int(os.getenv("ENABLE_SHORT", "1"))

GRID_LAYERS = int(os.getenv("GRID_LAYERS", "6"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
ATR_MULT = float(os.getenv("ATR_MULT", "0.9"))
ADX_LEN = int(os.getenv("ADX_LEN", "14"))
ADX_RANGE_TH = float(os.getenv("ADX_RANGE_TH", "20"))
RSI_LEN, RSI_OB, RSI_OS = int(os.getenv("RSI_LEN", "14")), int(os.getenv("RSI_OB","70")), int(os.getenv("RSI_OS","30"))
STOCH_LEN, STOCH_K, STOCH_D = int(os.getenv("STOCH_LEN","14")), int(os.getenv("STOCH_K","3")), int(os.getenv("STOCH_D","3"))
DONCHIAN_LEN = int(os.getenv("DONCHIAN_LEN","100"))

BASE_ORDER_USDT   = float(os.getenv("BASE_ORDER_USDT", "20"))
MAX_OPEN_NOTIONAL = float(os.getenv("MAX_OPEN_NOTIONAL", "1000"))
MAX_NOTIONAL_LONG = float(os.getenv("MAX_NOTIONAL_LONG","600"))
MAX_NOTIONAL_SHORT= float(os.getenv("MAX_NOTIONAL_SHORT","600"))

POST_ONLY = int(os.getenv("POST_ONLY","1"))
REDUCE_ONLY_TP = int(os.getenv("REDUCE_ONLY_TP","1"))

POLL_SEC = int(os.getenv("POLL_SEC","15"))
REBUILD_ATR_DELTA = float(os.getenv("REBUILD_ATR_DELTA","0.2"))
BREAKOUT_ATR_MULT = float(os.getenv("BREAKOUT_ATR_MULT","1.0"))
DRY_RUN = int(os.getenv("DRY_RUN","1"))

STATE_FILE = os.getenv("STATE_FILE","grid_state.json")
MEXC_RECVWINDOW_MS = int(os.getenv("MEXC_RECVWINDOW_MS", "10000"))
CCXT_TIMEOUT_MS = int(os.getenv("CCXT_TIMEOUT_MS","7000"))

# --- Nieuw voor TP/SL/fills ---
TP_MODE = os.getenv("TP_MODE","midline")   # midline | spacing | pct
TP_SPACING_MULT = float(os.getenv("TP_SPACING_MULT","1.0"))
TP_PCT = float(os.getenv("TP_PCT","0.004"))   # 0.004=0.4% (alleen voor TP_MODE=pct)
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT","1.5"))
NOTIFY_FILLS = int(os.getenv("NOTIFY_FILLS","1"))
NOTIFY_TP_SL = int(os.getenv("NOTIFY_TP_SL","1"))

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
fills = {}  # fill_id -> {"side":"long/short","entry_price":..., "qty":..., "tp":..., "sl":..., "active":True}

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
    if EXCHANGE!="mexc": raise RuntimeError("Alleen 'mexc' hier.")
    opts = {"apiKey":API_KEY,"secret":API_SECRET,"enableRateLimit":True,"timeout":CCXT_TIMEOUT_MS}
    opts["options"]={"defaultType":"swap" if USE_PERP else "spot","recvWindow":MEXC_RECVWINDOW_MS}
    ex = ccxt.mexc(opts); ex.load_markets(); return ex

def set_deriv_modes(ex):
    if not USE_PERP: return
    try:
        try: ex.set_margin_mode(MARGIN_MODE, SYMBOL)
        except Exception as e: logging.info(f"margin_mode: {e}")
        try: ex.set_leverage(LEVERAGE, SYMBOL, params={"marginMode":MARGIN_MODE})
        except Exception as e: logging.info(f"leverage: {e}")
        try: ex.set_position_mode(bool(HEDGE_MODE), SYMBOL)
        except Exception as e: logging.info(f"position_mode: {e}")
    except Exception as e:
        logging.warning(f"derivatives modes setup mislukte: {e}")

def fetch_ohlcv(ex, tf): return ex.fetch_ohlcv(SYMBOL, timeframe=tf, limit=500)
def to_df(ohlcv):
    d = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    d["ts"]=pd.to_datetime(d["ts"],unit="ms",utc=True); return d

def compute_indicators(df_ltf, df_htf):
    d=df_ltf.copy()
    d["atr"]=ta.atr(d["high"],d["low"],d["close"],length=ATR_LEN)
    adx=ta.adx(d["high"],d["low"],d["close"],length=ADX_LEN)
    d["adx"]=adx[f"ADX_{ADX_LEN}"]
    d["ema200"]=ta.ema(d["close"],length=200)

    h=df_htf.copy()
    h["don_high"]=ta.highest(h["high"],length=DONCHIAN_LEN)
    h["don_low"]=ta.lowest(h["low"],length=DONCHIAN_LEN)
    return d, float(h["don_low"].iloc[-1]), float(h["don_high"].iloc[-1])

def decide_regime(adx_val): return "range" if adx_val < ADX_RANGE_TH else "trend"

def build_grids(center, atr_val, mode):
    spacing=max(1e-8, atr_val*ATR_MULT)
    if mode=="trend": spacing*=1.8
    long_buys, short_sells=[], []
    for i in range(1, GRID_LAYERS+1):
        long_buys.append(round(center - i*spacing,8))
        short_sells.append(round(center + i*spacing,8))
    return spacing, long_buys, short_sells

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
        "<b>Dagrapport — Hedge Grid Bot</b>\n"
        f"Symbool: <b>{SYMBOL}</b>\n"
        f"Mode: <b>{runtime.get('mode')}</b> | ADX≈{(runtime.get('adx') or 0):.1f} | ATR≈{(runtime.get('atr') or 0):.6f}\n"
        f"Center≈{(runtime.get('center') or 0):.6f} | Spacing≈{(runtime.get('spacing') or 0):.6f}\n"
        f"HTF: [{(runtime.get('don_low') or 0):.6f} .. {(runtime.get('don_high') or 0):.6f}]\n\n"
        f"USDT: total≈<b>{usdt_total:.2f}</b> (free {usdt_free:.2f} / used {usdt_used:.2f}) ≈ €{eur:.2f}\n"
        f"Exposure: LONG≈{runtime['long_notional']:.2f} | SHORT≈{runtime['short_notional']:.2f} | TOTAAL≈{runtime['global_notional']:.2f} USDT\n"
        f"Tijd: {fmt_ts(now_utc())}"
    )

def time_matches(hhmm):
    try: hh,mm=map(int, hhmm.split(":"))
    except: return False
    loc=datetime.now().astimezone()
    return loc.hour==hh and loc.minute==mm

# === TP/SL helpers ===
def compute_tp_price(mode, entry, spacing, center, side):
    if mode=="midline":
        # tp richting center
        return center if (side=="long" and center>entry) or (side=="short" and center<entry) else entry + (spacing if side=="long" else -spacing)
    elif mode=="spacing":
        return entry + (spacing*TP_SPACING_MULT if side=="long" else -spacing*TP_SPACING_MULT)
    elif mode=="pct":
        delta = entry*TP_PCT
        return entry + (delta if side=="long" else -delta)
    else:
        return entry + (spacing if side=="long" else -spacing)

def compute_sl_price(entry, atr, side):
    return entry - ATR_SL_MULT*atr if side=="long" else entry + ATR_SL_MULT*atr

def approx_base_from_usdt(entry_price):
    return round(BASE_ORDER_USDT/max(entry_price,1e-12),6)

# ===== Bot thread =====
def bot_thread():
    global last_trade_ms
    ex=ccxt_client(); set_deriv_modes(ex)
    state=load_state()
    last_atr, last_mode, last_center = state.get("last_atr"), state.get("last_mode"), state.get("last_center")
    grid_live = state.get("grid_live", False)
    total_g=total_l=total_s=0.0

    send_telegram(f"🔧 Bot gestart — {SYMBOL} | perp={USE_PERP} lev={LEVERAGE} hedge={HEDGE_MODE} iso={MARGIN_MODE=='isolated'} | DRY_RUN={DRY_RUN}")

    while not _stop.is_set():
        try:
            df_ltf, df_htf = to_df(fetch_ohlcv(ex,TIMEFRAME)), to_df(fetch_ohlcv(ex,HTF))
            d, don_low, don_high = compute_indicators(df_ltf, df_htf)
            close=float(d["close"].iloc[-1]); atr=float(d["atr"].iloc[-1]); adx=float(d["adx"].iloc[-1]); ema200=float(d["ema200"].iloc[-1])
            mode=decide_regime(adx); center=min(max(ema200,don_low),don_high)
            lower_break=don_low-atr*BREAKOUT_ATR_MULT; upper_break=don_high+atr*BREAKOUT_ATR_MULT

            # rebuild?
            need_rebuild = (last_atr is None or last_mode is None or last_center is None or not grid_live)
            if not need_rebuild:
                if abs(atr-last_atr)/max(1e-8,last_atr)>REBUILD_ATR_DELTA: need_rebuild=True
                if mode!=last_mode: need_rebuild=True
                if close<lower_break or close>upper_break:
                    send_telegram(f"⚠️ Breakout: close {close:.6f} buiten [{lower_break:.6f}, {upper_break:.6f}] — flatten & rebuild")
                    try: cancel_all(ex)
                    except: pass
                    need_rebuild=True

            if need_rebuild:
                cancel_all(ex)
                spacing,long_buys,short_sells = build_grids(center,atr,mode)
                placed=0
                if ENABLE_LONG:
                    for px in long_buys:
                        if not can_add(total_l, BASE_ORDER_USDT, MAX_NOTIONAL_LONG, total_g, MAX_OPEN_NOTIONAL): break
                        try:
                            place_limit(ex,"buy",px,BASE_ORDER_USDT,reduce_only=False,pos_side="long" if HEDGE_MODE else None)
                            total_l+=BASE_ORDER_USDT; total_g+=BASE_ORDER_USDT; placed+=1
                        except Exception as e: logging.warning(f"long grid fail @{px}: {e}")
                if ENABLE_SHORT:
                    for px in short_sells:
                        if not can_add(total_s, BASE_ORDER_USDT, MAX_NOTIONAL_SHORT, total_g, MAX_OPEN_NOTIONAL): break
                        try:
                            place_limit(ex,"sell",px,BASE_ORDER_USDT,reduce_only=False,pos_side="short" if HEDGE_MODE else None)
                            total_s+=BASE_ORDER_USDT; total_g+=BASE_ORDER_USDT; placed+=1
                        except Exception as e: logging.warning(f"short grid fail @{px}: {e}")

                grid_live=True; last_atr, last_mode, last_center = atr, mode, center
                save_state({"last_atr":last_atr,"last_mode":last_mode,"last_center":last_center,"grid_live":grid_live,
                            "last_spacing":spacing,"updated_at":now_utc().isoformat(),
                            "last_levels":{"long_buys":long_buys,"short_sells":short_sells}})
                runtime.update({"spacing":spacing})
                send_telegram(f"🔁 Rebuild grid — Mode <b>{mode}</b> | ADX≈{adx:.1f} | ATR≈{atr:.6f}\nCenter≈{center:.6f} | Spacing≈{spacing:.6f}\nHTF: [{don_low:.6f} .. {don_high:.6f}]\nOrders: <b>{placed}</b>")

            # === Fills detectie ===
            # We poll trades sinds last_trade_ms; note: MEXC geeft ms timestamps
            try:
                trades = ex.fetch_my_trades(SYMBOL, since=last_trade_ms or None, limit=100)
            except Exception as e:
                trades=[]
                logging.debug(f"fetch_my_trades: {e}")

            new_max_ms = last_trade_ms
            for tr in trades or []:
                ts = int(tr.get("timestamp") or tr.get("datetime") or 0)
                if ts and ts <= last_trade_ms: continue
                new_max_ms = max(new_max_ms, ts or 0)

                side = tr.get("side")  # 'buy'/'sell'
                info = tr.get("info", {})
                pos_side = (info.get("positionSide") or info.get("posSide") or "").upper()  # 'LONG'/'SHORT' soms
                # heuristiek: als geen positionSide, leid af van grid: buy≈LONG, sell≈SHORT in hedge
                my_pos_side = "LONG" if (pos_side=="LONG" or (HEDGE_MODE and side=="buy")) else ("SHORT" if (pos_side=="SHORT" or (HEDGE_MODE and side=="sell")) else None)

                price = float(tr.get("price") or tr.get("info",{}).get("price") or 0)
                amount = float(tr.get("amount") or tr.get("contracts") or 0)
                if amount<=0 or price<=0: continue

                fill_id = tr.get("id") or f"{ts}-{side}-{price}"
                if fill_id in fills: continue

                # Registreer fill
                fills[fill_id] = {"side":"long" if my_pos_side=="LONG" else ("short" if my_pos_side=="SHORT" else ("long" if side=="buy" else "short")),
                                  "entry_price": price, "qty": amount, "tp": None, "sl": None, "active": True}
                if NOTIFY_FILLS:
                    send_telegram(f"✅ Fill: {fills[fill_id]['side'].upper()} {amount:g} @ {price:.6f}")

                # Plaats TP (reduce-only limit)
                spacing = runtime.get("spacing") or (last_atr*ATR_MULT)
                tp_price = compute_tp_price(TP_MODE, price, spacing, last_center, fills[fill_id]["side"])
                if REDUCE_ONLY_TP:
                    try:
                        side_out = "sell" if fills[fill_id]["side"]=="long" else "buy"
                        pos_tag = "long" if fills[fill_id]["side"]=="long" else "short"
                        o = place_limit(ex, side_out, tp_price, usdt=price*amount, reduce_only=True, pos_side=pos_tag if HEDGE_MODE else None)
                        fills[fill_id]["tp"] = {"id": o.get("id","TP"), "price": tp_price}
                        if NOTIFY_TP_SL: send_telegram(f"🎯 TP geplaatst: {side_out.upper()} RO @ {tp_price:.6f} (entry {price:.6f})")
                    except Exception as e:
                        logging.warning(f"TP place fail: {e}")

                # Bereken SL (soft watchdog; market reduce-only bij trigger)
                sl_price = compute_sl_price(price, last_atr, fills[fill_id]["side"])
                fills[fill_id]["sl"] = {"price": sl_price}

            if new_max_ms>last_trade_ms: last_trade_ms = new_max_ms

            # === SL Watchdog & TP-hit detectie (soft) ===
            # Als close voorbij SL, exit market reduce-only.
            to_deactivate=[]
            for fid, f in fills.items():
                if not f.get("active"): continue
                side=f["side"]; entry=f["entry_price"]; qty=f["qty"]
                # TP soft check: als koers voorbij TP-niveau in juiste richting en order zou gemist zijn
                tp=f.get("tp")
                if tp:
                    if (side=="long" and close>=tp["price"]) or (side=="short" and close<=tp["price"]):
                        try:
                            side_out = "sell" if side=="long" else "buy"
                            place_market_reduce_only(ex, side_out, amount_base=qty, pos_side=side)
                            f["active"]=False; to_deactivate.append(fid)
                            if NOTIFY_TP_SL: send_telegram(f"🏁 TP uitgevoerd (soft): {side_out.upper()} {qty:g} @~{close:.6f} (tp {tp['price']:.6f})")
                            continue
                        except Exception as e:
                            logging.warning(f"TP soft exec fail: {e}")

                # SL soft check
                sl=f.get("sl")
                if sl:
                    if (side=="long" and close<=sl["price"]) or (side=="short" and close>=sl["price"]):
                        try:
                            side_out = "sell" if side=="long" else "buy"
                            place_market_reduce_only(ex, side_out, amount_base=qty, pos_side=side)
                            f["active"]=False; to_deactivate.append(fid)
                            if NOTIFY_TP_SL: send_telegram(f"🛑 SL uitgevoerd (soft): {side_out.upper()} {qty:g} @~{close:.6f} (sl {sl['price']:.6f})")
                        except Exception as e:
                            logging.warning(f"SL soft exec fail: {e}")

            for fid in to_deactivate:
                fills.pop(fid, None)

            # Runtime + daily report
            runtime.update({"mode":mode,"center":center,"atr":atr,"adx":adx,"spacing":runtime.get("spacing") or (last_atr*ATR_MULT),
                            "don_low":don_low,"don_high":don_high,"long_notional":total_l,"short_notional":total_s,"global_notional":total_g})

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
def health(): return jsonify({"ok":True,"symbol":SYMBOL,"perp":bool(USE_PERP),"hedge":bool(HEDGE_MODE),"ts":fmt_ts(now_utc())})
@app.get("/state")
def state_ep(): return jsonify({"runtime":runtime,"fills_active":len(fills)})

if __name__=="__main__":
    t=Thread(target=bot_thread,daemon=True); t.start()
    try: app.run(host="0.0.0.0", port=PORT)
    finally: _stop.set(); t.join(timeout=5)
