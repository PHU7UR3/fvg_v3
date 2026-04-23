import os
import time
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string, request

import alpaca_trade_api as tradeapi
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_KEY       = os.environ.get("API_KEY", "")
SECRET_KEY    = os.environ.get("SECRET_KEY", "")
BASE_URL      = os.environ.get("BASE_URL", "https://paper-api.alpaca.markets")
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
        "logs":      [],
        "trades":    [],
        "fvg_count": 0,
        "wins":      0,
        "losses":    0,
        "total_pnl": 0.0,
        "settings": {
            "timeframe":      TIMEFRAME,
            "fvg_min_size":   0.001,
            "risk_per_trade": 0.02,
            "reward_ratio":   2.0,
            "max_positions":  3,
            "check_interval": 60,
            "use_rsi":        True,
            "use_volume":     False,
            "use_ema_trend":  True,
            "rsi_period":     14,
            "rsi_oversold":   20,
            "rsi_overbought": 80,
            "volume_mult":    1.2,
        }
    }

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
                base  = default_state()
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
                "logs":      state["logs"][:100],
                "trades":    state["trades"][:100],
                "fvg_count": state["fvg_count"],
                "wins":      state["wins"],
                "losses":    state["losses"],
                "total_pnl": state["total_pnl"],
                "settings":  state["settings"],
            }, f)
    except Exception as e:
        log.error(f"Save state: {e}")

runtime = {
    "running":     False,
    "market_open": False,
    "equity":      0.0,
    "cash":        0.0,
    "positions":   [],
    "last_scan":   "Never",
    "next_open":   "",
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
# EXCEL
# ─────────────────────────────────────────────
def init_excel():
    if os.path.exists(EXCEL_FILE):
        return
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Trade Log"
    headers = ["Date","Time","Symbol","Side","Qty","Entry ($)","SL ($)","TP ($)",
               "Exit ($)","P&L ($)","P&L (%)","Status","FVG Type","Trend","RSI","Vol OK","Notes"]
    hfill = PatternFill("solid", fgColor="0D0D14")
    hfont = Font(bold=True, color="00FF88", name="Arial", size=10)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    widths = [12,10,8,6,5,12,12,12,10,10,8,8,10,8,6,8,25]
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

def log_trade_excel(trade):
    try:
        wb  = openpyxl.load_workbook(EXCEL_FILE)
        ws  = wb["Trade Log"]
        nr  = ws.max_row + 1
        pnl = trade.get("pnl", 0.0)
        ent = trade.get("entry", 1)
        qty = trade.get("qty", 1)
        pct = round((pnl / (ent * qty)) * 100, 2) if ent and qty else 0
        st  = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "OPEN")
        row = [trade.get("date",""), trade.get("time",""), trade.get("symbol",""),
               trade.get("side","").upper(), qty, ent,
               trade.get("sl",0), trade.get("tp",0), trade.get("exit_price",""),
               round(pnl,2), pct, st, trade.get("fvg_type",""),
               trade.get("trend",""), trade.get("rsi",""),
               "YES" if trade.get("volume_ok") else "NO", trade.get("notes","")]
        for col, val in enumerate(row, 1):
            cell = ws.cell(row=nr, column=col, value=val)
            cell.font = Font(name="Arial", size=9)
            cell.alignment = Alignment(horizontal="center")
            if col == 10 and isinstance(val, (int, float)):
                cell.font = Font(name="Arial", size=9,
                    color="00AA44" if val>0 else "CC0000" if val<0 else "888888")
            if col == 12:
                if val == "WIN":
                    cell.fill = PatternFill("solid", fgColor="002200")
                    cell.font = Font(name="Arial", size=9, color="00FF88", bold=True)
                elif val == "LOSS":
                    cell.fill = PatternFill("solid", fgColor="220000")
                    cell.font = Font(name="Arial", size=9, color="FF3D6E", bold=True)
            if col == 4:
                cell.font = Font(name="Arial", size=9,
                    color="00FF88" if val=="BUY" else "FF3D6E", bold=True)
        wb.save(EXCEL_FILE)
    except Exception as e:
        log.error(f"Excel error: {e}")

# ─────────────────────────────────────────────
# ALPACA
# ─────────────────────────────────────────────
def get_api():
    return tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")

# ─────────────────────────────────────────────
# MARKET OPEN CHECK
# Uses both Alpaca clock AND manual UTC time check as fallback
# ─────────────────────────────────────────────
def is_market_open(api):
    # Manual UTC check: market open 13:30-20:00 UTC Mon-Fri
    utc_now    = datetime.now(timezone.utc)
    utc_mins   = utc_now.hour * 60 + utc_now.minute
    is_weekday = utc_now.weekday() < 5
    manual_open = is_weekday and 810 <= utc_mins <= 1200
    next_open  = ""
    try:
        clock      = api.get_clock()
        alpaca_open = clock.is_open
        next_open  = str(clock.next_open)[:16]
        # Use OR — if either says open, we trade
        return alpaca_open or manual_open, next_open
    except:
        return manual_open, next_open

# ─────────────────────────────────────────────
# MARKET DATA
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
        if hasattr(bars.index, 'tz') and bars.index.tz is not None:
            bars.index = bars.index.tz_localize(None)
        return bars if len(bars) >= 5 else None
    except Exception as e:
        err = str(e).lower()
        if "subscription" not in err and "forbidden" not in err and "market" not in err:
            add_log(f"⚠️ Candle {symbol}: {e}", "error")
        return None

def calculate_rsi(closes, period=14):
    try:
        if len(closes) < period + 1:
            return 50.0
        delta = closes.diff()
        gain  = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss  = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))
        val   = float(rsi.iloc[-1])
        return round(val, 1) if not pd.isna(val) else 50.0
    except:
        return 50.0

def calculate_ema(closes, period):
    return closes.ewm(span=period, adjust=False).mean()

def get_trend(api, symbol):
    try:
        bars = get_candles(api, symbol, tf="1Hour", hours=72, limit=50)
        if bars is None or len(bars) < 21:
            return "neutral"
        closes = bars["close"]
        ema20  = calculate_ema(closes, 20).iloc[-1]
        ema50  = calculate_ema(closes, min(50, len(closes))).iloc[-1]
        price  = closes.iloc[-1]
        if price > ema20 and ema20 > ema50 * 0.999:
            return "bullish"
        if price < ema20 and ema20 < ema50 * 1.001:
            return "bearish"
        return "neutral"
    except:
        return "neutral"

def detect_fvg(bars):
    fvgs     = []
    min_size = state["settings"]["fvg_min_size"]
    for i in range(2, len(bars)):
        c1 = bars.iloc[i-2]
        c2 = bars.iloc[i-1]
        c3 = bars.iloc[i]
        c2_body  = abs(float(c2["close"]) - float(c2["open"]))
        c2_range = float(c2["high"]) - float(c2["low"])
        if c2_range == 0 or c2_body / c2_range < 0.3:
            continue
        c1_high = float(c1["high"])
        c3_low  = float(c3["low"])
        c1_low  = float(c1["low"])
        c3_high = float(c3["high"])
        if c3_low > c1_high:
            gap = (c3_low - c1_high) / c1_high
            if gap >= min_size and float(c2["close"]) > float(c2["open"]):
                score = gap * 100 * (i / len(bars))
                fvgs.append({
                    "type":     "bullish",
                    "top":      c3_low,
                    "bottom":   c1_high,
                    "gap_size": round(gap * 100, 3),
                    "score":    score,
                    "c2_vol":   float(c2.get("volume", 0)),
                })
        elif c3_high < c1_low:
            gap = (c1_low - c3_high) / c1_low
            if gap >= min_size and float(c2["close"]) < float(c2["open"]):
                score = gap * 100 * (i / len(bars))
                fvgs.append({
                    "type":     "bearish",
                    "top":      c1_low,
                    "bottom":   c3_high,
                    "gap_size": round(gap * 100, 3),
                    "score":    score,
                    "c2_vol":   float(c2.get("volume", 0)),
                })
    return sorted(fvgs, key=lambda x: x["score"], reverse=True)

def check_volume(bars, fvg):
    try:
        if "volume" not in bars.columns:
            return True
        mult    = state["settings"]["volume_mult"]
        avg_vol = bars["volume"].rolling(20).mean().iloc[-1]
        if pd.isna(avg_vol) or avg_vol == 0:
            return True
        return float(fvg["c2_vol"]) > float(avg_vol) * mult
    except:
        return True

def price_in_fvg(price, fvg):
    tolerance = fvg["bottom"] * 0.001
    return (fvg["bottom"] - tolerance) <= price <= fvg["top"]

def calculate_qty(equity, entry, stop_loss):
    risk       = state["settings"]["risk_per_trade"]
    risk_amt   = equity * risk
    risk_share = abs(entry - stop_loss)
    if risk_share == 0:
        return 0
    shares     = risk_amt / risk_share
    max_shares = (equity * 0.25) / entry
    return max(1, int(min(shares, max_shares)))

# ─────────────────────────────────────────────
# TRADE EXECUTION
# ─────────────────────────────────────────────
def place_trade(api, symbol, side, qty, entry, sl, tp, fvg, trend, rsi, volume_ok):
    try:
        entry = round(float(entry), 2)
        sl    = round(float(sl), 2)
        tp    = round(float(tp), 2)
        if side == "buy" and (sl >= entry or tp <= entry):
            add_log(f"⚠️ Invalid SL/TP BUY {symbol} entry={entry} sl={sl} tp={tp}", "error")
            return
        if side == "sell" and (sl <= entry or tp >= entry):
            add_log(f"⚠️ Invalid SL/TP SELL {symbol} entry={entry} sl={sl} tp={tp}", "error")
            return
        api.submit_order(
            symbol=symbol, qty=qty, side=side,
            type="limit", time_in_force="day",
            limit_price=entry,
            order_class="bracket",
            stop_loss={"stop_price": sl},
            take_profit={"limit_price": tp}
        )
        add_log(f"✅ {side.upper()} {qty} {symbol} @ ${entry} SL:${sl} TP:${tp} RSI:{rsi} {trend}", "trade")
        trade = {
            "time":       datetime.now().strftime("%H:%M:%S"),
            "date":       datetime.now().strftime("%d %b %Y"),
            "symbol":     symbol,
            "side":       side,
            "qty":        qty,
            "entry":      entry,
            "sl":         sl,
            "tp":         tp,
            "exit_price": None,
            "pnl":        0.0,
            "status":     "OPEN",
            "fvg_type":   fvg["type"],
            "gap_size":   fvg["gap_size"],
            "trend":      trend,
            "rsi":        rsi,
            "volume_ok":  volume_ok,
            "notes":      f"FVG gap={fvg['gap_size']}% trend={trend} rsi={rsi}",
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
        orders    = api.list_orders(status="closed", limit=50)
        total_pnl = 0.0
        wins = losses = 0
        with lock:
            for t in state["trades"]:
                if t["status"] != "OPEN":
                    if t["pnl"] > 0: wins += 1
                    elif t["pnl"] < 0: losses += 1
                    total_pnl += t["pnl"]
                    continue
                for o in orders:
                    if o.symbol != t["symbol"]: continue
                    if o.filled_avg_price is None: continue
                    fp     = float(o.filled_avg_price)
                    fq     = float(o.filled_qty or 0)
                    o_side = str(o.side).lower()
                    if t["side"] == "buy" and o_side == "sell":
                        pnl = (fp - t["entry"]) * fq
                        t["pnl"] = round(pnl, 2)
                        t["exit_price"] = fp
                        t["status"] = "WIN" if pnl > 0 else "LOSS"
                        total_pnl += pnl
                        if pnl > 0: wins += 1
                        else: losses += 1
                        log_trade_excel(t)
                        break
                    elif t["side"] == "sell" and o_side == "buy":
                        pnl = (t["entry"] - fp) * fq
                        t["pnl"] = round(pnl, 2)
                        t["exit_price"] = fp
                        t["status"] = "WIN" if pnl > 0 else "LOSS"
                        total_pnl += pnl
                        if pnl > 0: wins += 1
                        else: losses += 1
                        log_trade_excel(t)
                        break
            state["total_pnl"] = round(total_pnl, 2)
            state["wins"]      = wins
            state["losses"]    = losses
        save_state()
    except Exception as e:
        log.error(f"Update trades: {e}")

# ─────────────────────────────────────────────
# BOT LOOP
# ─────────────────────────────────────────────
def bot_loop():
    add_log("🤖 FVG Bot started — FVG + EMA + RSI")
    api = get_api()

    # Fetch account immediately
    try:
        acc = api.get_account()
        with lock:
            runtime["equity"] = float(acc.equity)
            runtime["cash"]   = float(acc.cash)
        add_log(f"💰 Connected: ${float(acc.equity):,.2f}")
    except Exception as e:
        add_log(f"❌ Account error: {e}", "error")

    while True:
        with lock:
            if not runtime["running"]:
                break

        try:
            # Refresh account
            try:
                acc = api.get_account()
                with lock:
                    runtime["equity"] = float(acc.equity)
                    runtime["cash"]   = float(acc.cash)
            except:
                pass

            # Market open check — uses Alpaca + manual UTC fallback
            market_open, next_open = is_market_open(api)
            with lock:
                runtime["market_open"] = market_open
                runtime["next_open"]   = next_open

            if not market_open:
                add_log(f"💤 Market closed — next open: {next_open}")
                time.sleep(60)
                continue

            positions = api.list_positions()
            pos_dict  = {p.symbol: p for p in positions}

            with lock:
                runtime["last_scan"] = datetime.now().strftime("%H:%M:%S")
                runtime["positions"] = [
                    {
                        "symbol":  p.symbol,
                        "qty":     p.qty,
                        "entry":   float(p.avg_entry_price),
                        "current": float(p.current_price),
                        "pnl":     float(p.unrealized_pl),
                        "pnl_pct": round(float(p.unrealized_plpc) * 100, 2),
                        "side":    "long" if float(p.qty) > 0 else "short"
                    }
                    for p in positions
                ]

            update_closed_trades(api)

            equity  = runtime["equity"]
            s       = state["settings"]
            max_pos = s["max_positions"]

            add_log(f"📡 Scanning {len(state['watchlist'])} stocks | ${equity:,.0f} | {len(pos_dict)}/{max_pos} pos")

            if len(pos_dict) >= max_pos:
                add_log(f"⚠️ Max {max_pos} positions — waiting")
                time.sleep(s["check_interval"])
                continue

            watchlist = []
            with lock:
                watchlist = state["watchlist"][:]

            for symbol in watchlist:
                with lock:
                    if not runtime["running"]:
                        break
                if symbol in pos_dict:
                    continue

                bars = get_candles(api, symbol)
                if bars is None or len(bars) < 10:
                    continue

                price  = float(bars["close"].iloc[-1])
                closes = bars["close"]
                rsi    = calculate_rsi(closes, int(s["rsi_period"]))
                trend  = get_trend(api, symbol) if s["use_ema_trend"] else "neutral"
                fvgs   = detect_fvg(bars)

                if not fvgs:
                    continue

                with lock:
                    state["fvg_count"] += len(fvgs)

                for fvg in fvgs[:5]:
                    if not price_in_fvg(price, fvg):
                        continue

                    volume_ok = check_volume(bars, fvg) if s["use_volume"] else True
                    reward    = s["reward_ratio"]

                    add_log(f"🎯 {symbol} FVG({fvg['type']}) gap={fvg['gap_size']}% RSI={rsi} trend={trend} vol={'✅' if volume_ok else '❌'}")

                    # BULLISH ENTRY
                    if fvg["type"] == "bullish" and trend in ["bullish", "neutral"]:
                        rsi_ok = rsi < s["rsi_overbought"] if s["use_rsi"] else True
                        if rsi_ok and volume_ok:
                            sl  = round(fvg["bottom"] * 0.997, 2)
                            tp  = round(price + (price - sl) * reward, 2)
                            qty = calculate_qty(equity, price, sl)
                            if qty > 0:
                                place_trade(api, symbol, "buy", qty, price, sl, tp,
                                            fvg, trend, rsi, volume_ok)
                                break

                    # BEARISH ENTRY
                    elif fvg["type"] == "bearish" and trend in ["bearish", "neutral"]:
                        rsi_ok = rsi > s["rsi_oversold"] if s["use_rsi"] else True
                        if rsi_ok and volume_ok:
                            sl  = round(fvg["top"] * 1.003, 2)
                            tp  = round(price - (sl - price) * reward, 2)
                            qty = calculate_qty(equity, price, sl)
                            if qty > 0:
                                place_trade(api, symbol, "sell", qty, price, sl, tp,
                                            fvg, trend, rsi, volume_ok)
                                break

            save_state()
            time.sleep(s["check_interval"])

        except Exception as e:
            add_log(f"❌ Loop error: {e}", "error")
            time.sleep(30)

    add_log("⏹ Bot stopped")

def start_bot():
    with lock:
        if runtime["running"]:
            return
        runtime["running"] = True
    threading.Thread(target=bot_loop, daemon=True).start()

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
        log.error(f"Status account: {e}")
    with lock:
        return jsonify({
            "running":     runtime["running"],
            "market_open": runtime["market_open"],
            "next_open":   runtime["next_open"],
            "equity":      runtime["equity"],
            "cash":        runtime["cash"],
            "positions":   runtime["positions"],
            "last_scan":   runtime["last_scan"],
            "fvg_count":   state["fvg_count"],
            "total_pnl":   state["total_pnl"],
            "wins":        state["wins"],
            "losses":      state["losses"],
            "watchlist":   state["watchlist"],
            "logs":        state["logs"][:30],
            "trades":      state["trades"][:20],
            "settings":    state["settings"],
        })

@app.route("/api/start", methods=["POST"])
def api_start():
    start_bot()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    with lock:
        runtime["running"] = False
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
    add_log(f"⚙️ Settings saved — RSI:{s['rsi_oversold']}-{s['rsi_overbought']} Vol:{'ON' if s['use_volume'] else 'OFF'} EMA:{'ON' if s['use_ema_trend'] else 'OFF'}")
    return jsonify({"ok": True, "settings": state["settings"]})

@app.route("/api/watchlist/add/<symbol>", methods=["POST"])
def api_add(symbol):
    symbol = symbol.upper().strip()
    with lock:
        if symbol in state["watchlist"]:
            return jsonify({"ok": False, "msg": f"{symbol} already in watchlist"})
        state["watchlist"].append(symbol)
    save_state()
    add_log(f"➕ Added {symbol}")
    return jsonify({"ok": True, "watchlist": state["watchlist"]})

@app.route("/api/watchlist/remove/<symbol>", methods=["POST"])
def api_remove(symbol):
    symbol = symbol.upper().strip()
    with lock:
        if symbol not in state["watchlist"]:
            return jsonify({"ok": False, "msg": "Not found"})
        state["watchlist"].remove(symbol)
    save_state()
    add_log(f"➖ Removed {symbol}")
    return jsonify({"ok": True, "watchlist": state["watchlist"]})

@app.route("/api/clear_logs", methods=["POST"])
def api_clear_logs():
    with lock:
        state["logs"] = []
    save_state()
    return jsonify({"ok": True})

@app.route("/api/download_excel")
def download_excel():
    from flask import send_file
    if not os.path.exists(EXCEL_FILE):
        init_excel()
    return send_file(EXCEL_FILE, as_attachment=True, download_name="fvg_trades.xlsx")

@app.route("/ping")
def ping():
    return "pong", 200

@app.route("/api/test")
def api_test():
    try:
        acc = get_api().get_account()
        return jsonify({"connected": True, "equity": float(acc.equity), "status": acc.status})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})

# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
init_excel()
if API_KEY and SECRET_KEY:
    start_bot()
else:
    log.warning("⚠️ No API keys — bot not started")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
