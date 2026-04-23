import os
import re
import time
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string, request

import alpaca_trade_api as tradeapi
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_KEY       = os.environ.get("API_KEY", "")
SECRET_KEY    = os.environ.get("SECRET_KEY", "")
_raw_url      = os.environ.get("BASE_URL", "https://paper-api.alpaca.markets")
BASE_URL      = re.sub(r'/v2/?$', '', _raw_url).rstrip('/')
WATCHLIST_ENV = os.environ.get("WATCHLIST", "NVDA,AMD,ASML,AMAT,LRCX,TSM,XOM,RTX,UNH,JPM")
TIMEFRAME     = os.environ.get("TIMEFRAME", "5Min")
STATE_FILE    = "/tmp/fvg_state.json"
EXCEL_FILE    = "/tmp/fvg_trades.xlsx"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
lock = threading.Lock()

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def default_state():
    return {
        "watchlist": [w.strip() for w in WATCHLIST_ENV.split(",") if w.strip()],
        "logs": [], "trades": [],
        "fvg_count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
        "settings": {
            "timeframe": TIMEFRAME, "fvg_min_size": 0.001,
            "risk_per_trade": 0.02, "reward_ratio": 2.0,
            "max_positions": 3, "check_interval": 60,
            "use_rsi": True, "use_volume": False, "use_ema_trend": True,
            "rsi_period": 14, "rsi_oversold": 20, "rsi_overbought": 80,
            "volume_mult": 1.2,
        }
    }

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
                base = default_state()
                base.update(saved)
                for k, v in default_state()["settings"].items():
                    if k not in base["settings"]:
                        base["settings"][k] = v
                return base
    except:
        pass
    return default_state()

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "watchlist": state["watchlist"],
                "logs": state["logs"][:100],
                "trades": state["trades"][:100],
                "fvg_count": state["fvg_count"],
                "wins": state["wins"], "losses": state["losses"],
                "total_pnl": state["total_pnl"],
                "settings": state["settings"],
            }, f)
    except Exception as e:
        log.error(f"Save state: {e}")

runtime = {
    "running": False, "market_open": False,
    "equity": 0.0, "cash": 0.0,
    "positions": [], "last_scan": "Never", "next_open": "",
    "pdt_disabled": False,
}
state = load_state()

def add_log(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    with lock:
        state["logs"].insert(0, entry)
        if len(state["logs"]) > 100:
            state["logs"] = state["logs"][:100]
    save_state()
    log.info(msg)

# ─────────────────────────────────────────────
# EXCEL — smart update (no duplicate rows)
# ─────────────────────────────────────────────
def init_excel():
    if os.path.exists(EXCEL_FILE):
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trade Log"
    headers = ["Date","Time","Symbol","Side","Qty","Entry ($)","SL ($)","TP ($)",
               "Exit ($)","P&L ($)","P&L (%)","Status","FVG Type","Trend","RSI","Notes"]
    hfill = PatternFill("solid", fgColor="0D0D14")
    hfont = Font(bold=True, color="00FF88", name="Arial", size=10)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont; cell.fill = hfill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    widths = [12,10,8,6,6,12,12,12,10,10,8,8,10,8,6,35]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 20
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "FVG Bot — Trade Summary"
    ws2["A1"].font = Font(bold=True, size=14, color="00FF88", name="Arial")
    for i, l in enumerate(["Total Trades","Wins","Losses","Win Rate (%)","Total P&L ($)",
                            "Avg P&L ($)","Best Trade ($)","Worst Trade ($)"], 3):
        ws2.cell(row=i, column=1, value=l).font = Font(bold=True, name="Arial")
    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 16
    ws2["B3"]  = "=COUNTA('Trade Log'!A2:A10000)-1"
    ws2["B4"]  = "=COUNTIF('Trade Log'!L2:L10000,\"WIN\")"
    ws2["B5"]  = "=COUNTIF('Trade Log'!L2:L10000,\"LOSS\")"
    ws2["B6"]  = "=IFERROR(B4/B3*100,0)"
    ws2["B7"]  = "=IFERROR(SUM('Trade Log'!J2:J10000),0)"
    ws2["B8"]  = "=IFERROR(AVERAGE('Trade Log'!J2:J10000),0)"
    ws2["B9"]  = "=IFERROR(MAX('Trade Log'!J2:J10000),0)"
    ws2["B10"] = "=IFERROR(MIN('Trade Log'!J2:J10000),0)"
    wb.save(EXCEL_FILE)

def _write_row(ws, nr, trade):
    pnl = trade.get("pnl", 0.0)
    ent = trade.get("entry", 1)
    qty = trade.get("qty", 1)
    pct = round((pnl / (ent * qty)) * 100, 2) if ent and qty else 0
    st  = trade.get("status", "OPEN")
    row = [trade.get("date",""), trade.get("time",""), trade.get("symbol",""),
           trade.get("side","").upper(), qty, ent,
           trade.get("sl",0), trade.get("tp",0),
           trade.get("exit_price","") or "",
           round(pnl,2), pct, st, trade.get("fvg_type",""),
           trade.get("trend",""), trade.get("rsi",""), trade.get("notes","")]
    for col, val in enumerate(row, 1):
        cell = ws.cell(row=nr, column=col, value=val)
        cell.font = Font(name="Arial", size=9)
        cell.alignment = Alignment(horizontal="center")
        if col == 10 and isinstance(val, (int, float)):
            cell.font = Font(name="Arial", size=9,
                color="00AA44" if val>0 else "CC0000" if val<0 else "888888")
        if col == 12:
            colors = {"WIN":("002200","00FF88"), "LOSS":("220000","FF3D6E")}
            if val in colors:
                cell.fill = PatternFill("solid", fgColor=colors[val][0])
                cell.font = Font(name="Arial", size=9, color=colors[val][1], bold=True)
            else:
                cell.fill = PatternFill("solid", fgColor="1a1a2e")
                cell.font = Font(name="Arial", size=9, color="FFD166", bold=True)
        if col == 4:
            cell.font = Font(name="Arial", size=9,
                color="00FF88" if val=="BUY" else "FF3D6E", bold=True)

def log_trade_excel(trade):
    """Update existing row or append new one — never duplicates."""
    try:
        wb = openpyxl.load_workbook(EXCEL_FILE)
        ws = wb["Trade Log"]
        if trade.get("excel_row"):
            _write_row(ws, trade["excel_row"], trade)
        else:
            existing = None
            for r in range(2, ws.max_row + 1):
                if (ws.cell(row=r, column=3).value == trade.get("symbol") and
                    ws.cell(row=r, column=2).value == trade.get("time")):
                    existing = r
                    break
            nr = existing or (ws.max_row + 1)
            trade["excel_row"] = nr
            _write_row(ws, nr, trade)
        wb.save(EXCEL_FILE)
    except Exception as e:
        log.error(f"Excel error: {e}")

# ─────────────────────────────────────────────
# ALPACA
# ─────────────────────────────────────────────
def get_api():
    return tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")

def disable_pdt(api):
    try:
        import requests as req
        r = req.patch(
            f"{BASE_URL}/v2/account/configurations",
            headers={"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY},
            json={"pdt_check": "entry"}
        )
        if r.status_code == 200:
            add_log("✅ PDT set to entry-only (trades allowed)")
            with lock: runtime["pdt_disabled"] = True
        else:
            add_log(f"⚠️ PDT config: {r.status_code}", "error")
    except Exception as e:
        add_log(f"⚠️ PDT error: {e}", "error")

def cancel_all_orders(api):
    """Cancel stale open orders to free buying power."""
    try:
        orders = api.list_orders(status="open")
        if orders:
            api.cancel_all_orders()
            add_log(f"🗑️ Cancelled {len(orders)} stale open orders")
    except Exception as e:
        log.error(f"Cancel orders: {e}")

# ─────────────────────────────────────────────
# MARKET HOURS — Alpaca clock + UTC fallback
# ─────────────────────────────────────────────
def is_market_open(api):
    utc     = datetime.now(timezone.utc)
    mins    = utc.hour * 60 + utc.minute
    weekday = utc.weekday() < 5
    manual  = weekday and 810 <= mins <= 1200
    next_open = ""
    try:
        clock = api.get_clock()
        next_open = str(clock.next_open)[:16]
        return clock.is_open or manual, next_open
    except:
        return manual, next_open

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def get_candles(api, symbol, tf=None, hours=10, limit=100):
    try:
        tf    = tf or state["settings"]["timeframe"]
        end   = datetime.utcnow()
        start = end - timedelta(hours=hours)
        bars  = api.get_bars(symbol, tf,
                             start=start.isoformat()+"Z",
                             end=end.isoformat()+"Z",
                             limit=limit, feed="iex").df
        if hasattr(bars.index, "tz") and bars.index.tz:
            bars.index = bars.index.tz_localize(None)
        return bars if len(bars) >= 5 else None
    except Exception as e:
        err = str(e).lower()
        if not any(x in err for x in ["subscription","forbidden","market","iex"]):
            add_log(f"⚠️ Candle {symbol}: {e}", "error")
        return None

def calculate_rsi(closes, period=14):
    try:
        if len(closes) < period + 1: return 50.0
        d = closes.diff()
        g = d.where(d>0, 0.0).rolling(period).mean()
        l = (-d.where(d<0, 0.0)).rolling(period).mean()
        v = float((100 - (100/(1+(g/l)))).iloc[-1])
        return round(v, 1) if not pd.isna(v) else 50.0
    except: return 50.0

def calc_ema(closes, period):
    return closes.ewm(span=period, adjust=False).mean()

def get_trend(api, symbol):
    try:
        bars = get_candles(api, symbol, tf="1Hour", hours=72, limit=50)
        if bars is None or len(bars) < 21: return "neutral"
        c  = bars["close"]
        e20 = calc_ema(c, 20).iloc[-1]
        e50 = calc_ema(c, min(50, len(c))).iloc[-1]
        p   = c.iloc[-1]
        if p > e20 and e20 > e50 * 0.999: return "bullish"
        if p < e20 and e20 < e50 * 1.001: return "bearish"
        return "neutral"
    except: return "neutral"

def detect_fvg(bars):
    fvgs = []
    min_size = state["settings"]["fvg_min_size"]
    for i in range(2, len(bars)):
        c1, c2, c3 = bars.iloc[i-2], bars.iloc[i-1], bars.iloc[i]
        body  = abs(float(c2["close"]) - float(c2["open"]))
        rng   = float(c2["high"]) - float(c2["low"])
        if rng == 0 or body/rng < 0.3: continue
        c1h, c3l = float(c1["high"]), float(c3["low"])
        c1l, c3h = float(c1["low"]),  float(c3["high"])
        if c3l > c1h:
            gap = (c3l - c1h) / c1h
            if gap >= min_size and float(c2["close"]) > float(c2["open"]):
                fvgs.append({"type":"bullish","top":c3l,"bottom":c1h,
                             "gap_size":round(gap*100,3),
                             "score":gap*100*(i/len(bars)),
                             "c2_vol":float(c2.get("volume",0))})
        elif c3h < c1l:
            gap = (c1l - c3h) / c1l
            if gap >= min_size and float(c2["close"]) < float(c2["open"]):
                fvgs.append({"type":"bearish","top":c1l,"bottom":c3h,
                             "gap_size":round(gap*100,3),
                             "score":gap*100*(i/len(bars)),
                             "c2_vol":float(c2.get("volume",0))})
    return sorted(fvgs, key=lambda x: x["score"], reverse=True)

def check_volume(bars, fvg):
    try:
        if "volume" not in bars.columns: return True
        avg = bars["volume"].rolling(20).mean().iloc[-1]
        if pd.isna(avg) or avg == 0: return True
        return float(fvg["c2_vol"]) > float(avg) * state["settings"]["volume_mult"]
    except: return True

def price_in_fvg(price, fvg):
    tol = fvg["bottom"] * 0.0005  # 0.05% tolerance only
    return (fvg["bottom"] - tol) <= price <= fvg["top"]

def calculate_qty(equity, cash, entry, stop_loss):
    risk     = state["settings"]["risk_per_trade"]
    risk_amt = equity * risk
    risk_per = abs(entry - stop_loss)
    if risk_per == 0: return 0
    by_risk  = risk_amt / risk_per
    by_cash  = (cash * 0.95) / entry
    by_equity = (equity * 0.25) / entry
    qty = int(min(by_risk, by_cash, by_equity))
    return max(1, qty) if cash >= entry else 0

# ─────────────────────────────────────────────
# TRADE EXECUTION
# ─────────────────────────────────────────────
def place_trade(api, symbol, side, qty, entry, sl, tp, fvg, trend, rsi):
    try:
        entry = round(float(entry), 2)
        sl    = round(float(sl), 2)
        tp    = round(float(tp), 2)

        # Auto-fix invalid SL/TP
        if side == "buy":
            if sl >= entry: sl = round(entry * 0.985, 2)
            if tp <= entry: tp = round(entry * 1.03, 2)
        else:
            if sl <= entry: sl = round(entry * 1.015, 2)
            if tp >= entry: tp = round(entry * 0.97, 2)

        order_type = "BRACKET"
        try:
            api.submit_order(
                symbol=symbol, qty=qty, side=side,
                type="limit", time_in_force="gtc",
                limit_price=entry,
                order_class="bracket",
                stop_loss={"stop_price": sl},
                take_profit={"limit_price": tp}
            )
        except Exception as be:
            if "pattern day" in str(be).lower() or "pdt" in str(be).lower():
                # Fallback: simple limit order
                api.submit_order(
                    symbol=symbol, qty=qty, side=side,
                    type="limit", time_in_force="gtc",
                    limit_price=entry,
                )
                order_type = "LIMIT"
            else:
                raise be

        msg = f"✅ {side.upper()} {qty} {symbol} @ ${entry} SL:${sl} TP:${tp} RSI:{rsi} [{order_type}]"
        add_log(msg, "trade")

        trade = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%d %b %Y"),
            "symbol": symbol, "side": side, "qty": qty,
            "entry": entry, "sl": sl, "tp": tp,
            "exit_price": None, "pnl": 0.0, "status": "OPEN",
            "fvg_type": fvg["type"], "gap_size": fvg["gap_size"],
            "trend": trend, "rsi": rsi,
            "notes": f"FVG {fvg['gap_size']}% trend={trend} RSI={rsi} [{order_type}]",
            "excel_row": None,
        }
        with lock:
            state["trades"].insert(0, trade)
            if len(state["trades"]) > 100:
                state["trades"] = state["trades"][:100]
        log_trade_excel(trade)
        save_state()
    except Exception as e:
        add_log(f"❌ Order failed {symbol}: {e}", "error")

def update_closed_trades(api):
    try:
        orders = api.list_orders(status="closed", limit=50)
        total_pnl = wins = losses = 0
        with lock:
            for t in state["trades"]:
                if t["status"] != "OPEN":
                    if t["pnl"] > 0: wins += 1
                    elif t["pnl"] < 0: losses += 1
                    total_pnl += t["pnl"]
                    continue
                for o in orders:
                    if o.symbol != t["symbol"] or not o.filled_avg_price: continue
                    fp = float(o.filled_avg_price)
                    fq = float(o.filled_qty or 0)
                    os = str(o.side).lower()
                    if (t["side"]=="buy" and os=="sell") or (t["side"]=="sell" and os=="buy"):
                        pnl = (fp-t["entry"])*fq if t["side"]=="buy" else (t["entry"]-fp)*fq
                        t.update({"pnl":round(pnl,2),"exit_price":fp,
                                  "status":"WIN" if pnl>0 else "LOSS"})
                        total_pnl += pnl
                        if pnl>0: wins+=1
                        else: losses+=1
                        log_trade_excel(t)
                        break
            state.update({"total_pnl":round(total_pnl,2),"wins":wins,"losses":losses})
        save_state()
    except Exception as e:
        log.error(f"Update trades: {e}")

# ─────────────────────────────────────────────
# BOT LOOP
# ─────────────────────────────────────────────
def bot_loop():
    add_log("🤖 FVG Bot started — Auto mode")
    api = get_api()
    try:
        acc = api.get_account()
        with lock:
            runtime["equity"] = float(acc.equity)
            runtime["cash"]   = float(acc.cash)
        add_log(f"💰 Connected: ${float(acc.equity):,.2f}")
    except Exception as e:
        add_log(f"❌ Account error: {e}", "error")
        with lock: runtime["running"] = False
        return

    disable_pdt(api)
    cancel_all_orders(api)

    while True:
        with lock:
            if not runtime["running"]: break
        try:
            try:
                acc = api.get_account()
                with lock:
                    runtime["equity"] = float(acc.equity)
                    runtime["cash"]   = float(acc.cash)
            except: pass

            open_, next_open = is_market_open(api)
            with lock:
                runtime["market_open"] = open_
                runtime["next_open"]   = next_open

            if not open_:
                add_log(f"💤 Market closed — next open: {next_open} UTC")
                time.sleep(60)
                continue

            positions = api.list_positions()
            pos_dict  = {p.symbol: p for p in positions}
            with lock:
                runtime["last_scan"] = datetime.now().strftime("%H:%M:%S")
                runtime["positions"] = [
                    {"symbol":p.symbol,"qty":p.qty,
                     "entry":float(p.avg_entry_price),"current":float(p.current_price),
                     "pnl":float(p.unrealized_pl),
                     "pnl_pct":round(float(p.unrealized_plpc)*100,2),
                     "side":"long" if float(p.qty)>0 else "short"}
                    for p in positions
                ]

            update_closed_trades(api)
            equity = runtime["equity"]
            cash   = runtime["cash"]
            s      = state["settings"]
            max_pos = s["max_positions"]

            add_log(f"📡 Scanning {len(state['watchlist'])} stocks | ${equity:,.0f} | {len(pos_dict)}/{max_pos}")

            if len(pos_dict) >= max_pos:
                add_log(f"⚠️ Max {max_pos} positions — waiting")
                time.sleep(s["check_interval"])
                continue

            with lock: watchlist = state["watchlist"][:]

            for symbol in watchlist:
                with lock:
                    if not runtime["running"]: break
                if symbol in pos_dict: continue

                bars = get_candles(api, symbol)
                if bars is None or len(bars) < 10: continue

                price  = float(bars["close"].iloc[-1])
                rsi    = calculate_rsi(bars["close"], int(s["rsi_period"]))
                trend  = get_trend(api, symbol) if s["use_ema_trend"] else "neutral"
                fvgs   = detect_fvg(bars)
                if not fvgs: continue

                with lock: state["fvg_count"] += len(fvgs)

                traded = False
                for fvg in fvgs[:5]:
                    if traded: break
                    if not price_in_fvg(price, fvg): continue
                    vol_ok = check_volume(bars, fvg) if s["use_volume"] else True
                    reward = s["reward_ratio"]
                    add_log(f"🎯 {symbol} FVG({fvg['type']}) gap={fvg['gap_size']}% RSI={rsi} trend={trend}")

                    if fvg["type"] == "bullish" and trend in ["bullish","neutral"]:
                        if (not s["use_rsi"] or rsi < s["rsi_overbought"]) and vol_ok:
                            sl  = round(fvg["bottom"] * 0.997, 2)
                            tp  = round(price + (price-sl) * reward, 2)
                            qty = calculate_qty(equity, cash, price, sl)
                            if qty > 0:
                                place_trade(api, symbol, "buy", qty, price, sl, tp, fvg, trend, rsi)
                                traded = True

                    elif fvg["type"] == "bearish" and trend in ["bearish","neutral"]:
                        if (not s["use_rsi"] or rsi > s["rsi_oversold"]) and vol_ok:
                            sl  = round(fvg["top"] * 1.003, 2)
                            tp  = round(price - (sl-price) * reward, 2)
                            qty = calculate_qty(equity, cash, price, sl)
                            if qty > 0:
                                place_trade(api, symbol, "sell", qty, price, sl, tp, fvg, trend, rsi)
                                traded = True

            save_state()
            time.sleep(s["check_interval"])
        except Exception as e:
            add_log(f"❌ Loop error: {e}", "error")
            time.sleep(30)
    add_log("⏹ Bot stopped")

def watchdog():
    time.sleep(15)
    while True:
        time.sleep(30)
        with lock: should_run = runtime["running"]
        if should_run:
            alive = any(t.name=="bot_thread" and t.is_alive() for t in threading.enumerate())
            if not alive:
                add_log("🔄 Watchdog: restarting bot", "error")
                threading.Thread(target=bot_loop, daemon=True, name="bot_thread").start()

def start_bot():
    with lock:
        if runtime["running"]: return
        runtime["running"] = True
    threading.Thread(target=bot_loop, daemon=True, name="bot_thread").start()

# ─────────────────────────────────────────────
# FLASK
# ─────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "dashboard.html")) as f:
        return render_template_string(f.read())

@app.route("/api/status")
def api_status():
    try:
        acc = get_api().get_account()
        with lock:
            runtime["equity"] = float(acc.equity)
            runtime["cash"]   = float(acc.cash)
    except Exception as e:
        log.error(f"Status: {e}")
    with lock:
        return jsonify({
            "running": runtime["running"], "market_open": runtime["market_open"],
            "next_open": runtime["next_open"], "equity": runtime["equity"],
            "cash": runtime["cash"], "positions": runtime["positions"],
            "last_scan": runtime["last_scan"], "fvg_count": state["fvg_count"],
            "total_pnl": state["total_pnl"], "wins": state["wins"],
            "losses": state["losses"], "watchlist": state["watchlist"],
            "logs": state["logs"][:30], "trades": state["trades"][:20],
            "settings": state["settings"], "pdt_disabled": runtime["pdt_disabled"],
        })

@app.route("/api/start", methods=["POST"])
def api_start():
    start_bot(); return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    with lock: runtime["running"] = False
    return jsonify({"ok": True})

@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json()
    with lock:
        s = state["settings"]
        s["timeframe"]      = data.get("timeframe", s["timeframe"])
        s["fvg_min_size"]   = float(data.get("fvg_min_size", s["fvg_min_size"]))
        s["risk_per_trade"] = float(data.get("risk_per_trade", s["risk_per_trade"]))
        s["reward_ratio"]   = float(data.get("reward_ratio", s["reward_ratio"]))
        s["max_positions"]  = int(data.get("max_positions", s["max_positions"]))
        s["check_interval"] = int(data.get("check_interval", s["check_interval"]))
        s["use_rsi"]        = bool(data.get("use_rsi", s["use_rsi"]))
        s["use_volume"]     = bool(data.get("use_volume", s["use_volume"]))
        s["use_ema_trend"]  = bool(data.get("use_ema_trend", s["use_ema_trend"]))
        s["rsi_oversold"]   = float(data.get("rsi_oversold", s["rsi_oversold"]))
        s["rsi_overbought"] = float(data.get("rsi_overbought", s["rsi_overbought"]))
        s["volume_mult"]    = float(data.get("volume_mult", s["volume_mult"]))
    save_state()
    add_log(f"⚙️ Settings saved")
    return jsonify({"ok": True, "settings": state["settings"]})

@app.route("/api/watchlist/add/<symbol>", methods=["POST"])
def api_add(symbol):
    symbol = symbol.upper().strip()
    with lock:
        if symbol in state["watchlist"]:
            return jsonify({"ok": False, "msg": f"{symbol} already in watchlist"})
        state["watchlist"].append(symbol)
    save_state(); add_log(f"➕ Added {symbol}")
    return jsonify({"ok": True, "watchlist": state["watchlist"]})

@app.route("/api/watchlist/remove/<symbol>", methods=["POST"])
def api_remove(symbol):
    symbol = symbol.upper().strip()
    with lock:
        if symbol not in state["watchlist"]:
            return jsonify({"ok": False, "msg": "Not found"})
        state["watchlist"].remove(symbol)
    save_state(); add_log(f"➖ Removed {symbol}")
    return jsonify({"ok": True, "watchlist": state["watchlist"]})

@app.route("/api/clear_logs", methods=["POST"])
def api_clear_logs():
    with lock: state["logs"] = []
    save_state(); return jsonify({"ok": True})

@app.route("/api/download_excel")
def download_excel():
    from flask import send_file
    if not os.path.exists(EXCEL_FILE): init_excel()
    return send_file(EXCEL_FILE, as_attachment=True, download_name="fvg_trades.xlsx")

@app.route("/ping")
def ping(): return "pong", 200

@app.route("/api/test")
def api_test():
    try:
        acc = get_api().get_account()
        return jsonify({"connected":True,"equity":float(acc.equity),"base_url":BASE_URL})
    except Exception as e:
        return jsonify({"connected":False,"error":str(e),"base_url":BASE_URL})

# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
init_excel()
if API_KEY and SECRET_KEY:
    start_bot()
    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()
else:
    log.warning("⚠️ No API keys")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
