import os
import time
import datetime
import threading
import requests
import yfinance as yf
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# 100% SECURE: Uses ONLY your Telegram keys (NO broker login needed)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_RISK_RUPEES    = float(os.getenv("MAX_RISK_RUPEES", "600"))
TARGET_R_RATIO     = float(os.getenv("TARGET_R_RATIO", "2.0"))
LOT_SIZE           = int(os.getenv("LOT_SIZE", "75"))
SKIP_EXPIRY        = os.getenv("SKIP_EXPIRY", "true").lower() == "true"

bot_status = {
    "market_status": "Ready & Scanning",
    "orb_high": None,
    "orb_low": None,
    "current_nifty": None,
    "state": "WAITING_FOR_MARKET",
    "final_decision": "WAIT",
    "decision_reason": "Bot active. Waiting for 9:15–9:30 Opening Range.",
    "active_position": None,
    "trades_today": 0,
    "today_pnl": 0.0,
    "logs": ["[SYSTEM] Running in 100% Secure Mode: No Broker Accounts Linked."]
}

app = FastAPI(title="NIFTY ORB Alert Bot")

def get_ist_now():
    utc = datetime.datetime.now(datetime.timezone.utc)
    return utc + datetime.timedelta(hours=5, minutes=30)

def log(msg):
    ist = get_ist_now().strftime("%H:%M:%S")
    entry = f"[{ist}] {msg}"
    print(entry)
    bot_status["logs"].append(entry)
    if len(bot_status["logs"]) > 30:
        bot_status["logs"].pop(0)

def send_telegram(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🤖 NIFTY ORB ALERT:\n\n{msg}", "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print("Telegram send failed:", e)

def fetch_public_nifty_candles():
    try:
        df = yf.Ticker('^NSEI').history(period='2d', interval='5m')
        if not df.empty:
            candles = []
            for dt, row in df.iterrows():
                candles.append({
                    "time": dt.strftime("%Y-%m-%d %H:%M"),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"])
                })
            return candles
    except Exception as e:
        log(f"Data fetch error: {e}")
    return []

def bot_loop():
    while True:
        try:
            ist = get_ist_now()
            time_str = ist.strftime("%H:%M")
            weekday = ist.weekday() # 0=Mon, 3=Thu, 4=Fri, 5=Sat, 6=Sun

            if weekday >= 5:
                bot_status["market_status"] = "Weekend (Market Closed)"
                bot_status["final_decision"] = "NO TRADE"
                bot_status["decision_reason"] = "Market is closed today (Weekend)."
                time.sleep(60)
                continue

            if SKIP_EXPIRY and weekday == 3:
                bot_status["market_status"] = "Thursday Expiry (Skipped)"
                bot_status["final_decision"] = "NO TRADE"
                bot_status["decision_reason"] = "Thursday is NIFTY Weekly Expiry. Skipping to avoid gamma spikes."
                time.sleep(60)
                continue

            if time_str < "09:15":
                bot_status["market_status"] = "Pre-Market (Waiting for 9:15 AM)"
                bot_status["final_decision"] = "WAIT"
                bot_status["decision_reason"] = "Waiting for market open at 9:15 AM."
                time.sleep(15)
                continue
            elif time_str >= "15:30":
                bot_status["market_status"] = "Post-Market (Closed)"
                bot_status["final_decision"] = "NO TRADE"
                bot_status["decision_reason"] = "Market closed for today."
                time.sleep(60)
                continue
            else:
                bot_status["market_status"] = "Market Open (Scanning 5m Candles)"

            if time_str >= "15:10" and bot_status["active_position"]:
                pos = bot_status["active_position"]
                msg = f"⏰ *15:10 SQUARE-OFF ALERT*\nExit your open *{pos['symbol']}* on Groww now!"
                log(msg)
                send_telegram(msg)
                bot_status["active_position"] = None
                bot_status["final_decision"] = "NO TRADE"
                bot_status["decision_reason"] = "Position closed at 15:10 cutoff."
                time.sleep(30)
                continue

            candles = fetch_public_nifty_candles()
            if not candles:
                time.sleep(20)
                continue

            today_str = ist.strftime("%Y-%m-%d")
            today_candles = [c for c in candles if c["time"].startswith(today_str)]

            if len(today_candles) < 3:
                bot_status["final_decision"] = "WAIT"
                bot_status["decision_reason"] = f"Forming 9:15–9:30 Opening Range ({len(today_candles)}/3 candles)."
                time.sleep(20)
                continue

            if bot_status["orb_high"] is None:
                orb_candles = today_candles[:3]
                bot_status["orb_high"] = max([c["high"] for c in orb_candles])
                bot_status["orb_low"]  = min([c["low"] for c in orb_candles])
                bot_status["state"]    = "WAITING_BREAKOUT"
                msg = f"📊 *9:15–9:30 OPENING RANGE FORMED*\n• High: `{bot_status['orb_high']:.2f}`\n• Low: `{bot_status['orb_low']:.2f}`\nWatching for 5m candle close outside range."
                log(f"Opening Range: High {bot_status['orb_high']:.2f} | Low {bot_status['orb_low']:.2f}")
                send_telegram(msg)

            latest = today_candles[-1]
            c_close = latest["close"]
            c_high  = latest["high"]
            c_low   = latest["low"]
            bot_status["current_nifty"] = c_close
            atm_strike = round(c_close / 50.0) * 50

            state = bot_status["state"]
            orb_h = bot_status["orb_high"]
            orb_l = bot_status["orb_low"]

            if bot_status["trades_today"] >= 1:
                bot_status["final_decision"] = "NO TRADE"
                bot_status["decision_reason"] = "Max 1 trade per day limit reached."
                time.sleep(30)
                continue

            if bot_status["active_position"]:
                pos = bot_status["active_position"]
                if pos["type"] == "CE":
                    if c_low <= pos["sl"]:
                        msg = f"❌ *STOP-LOSS HIT ALERT*\nExit *{pos['symbol']}* on Groww now.\n• Max planned loss maintained: ₹{pos['risk_rupees']:.0f}"
                        log(msg)
                        send_telegram(msg)
                        bot_status["active_position"] = None
                        bot_status["final_decision"] = "NO TRADE"
                    elif c_high >= pos["target"]:
                        profit = pos["risk_rupees"] * TARGET_R_RATIO
                        msg = f"🎯 *TARGET 2.0R HIT!*\nExit *{pos['symbol']}* on Groww now!\n• Profit: *+₹{profit:.0f}*"
                        log(msg)
                        send_telegram(msg)
                        bot_status["today_pnl"] += profit
                        bot_status["active_position"] = None
                        bot_status["final_decision"] = "NO TRADE"
                time.sleep(20)
                continue

            if state == "WAITING_BREAKOUT":
                if c_close > orb_h:
                    bot_status["state"] = "WAITING_BULL_RETEST"
                    bot_status["final_decision"] = "WAIT"
                    bot_status["decision_reason"] = f"5m Candle closed above High ({c_close:.2f} > {orb_h:.2f}). Waiting for retest."
                elif c_close < orb_l:
                    bot_status["state"] = "WAITING_BEAR_RETEST"
                    bot_status["final_decision"] = "WAIT"
                    bot_status["decision_reason"] = f"5m Candle closed below Low ({c_close:.2f} < {orb_l:.2f}). Waiting for retest."

            elif state == "WAITING_BULL_RETEST":
                if c_close < orb_h:
                    bot_status["state"] = "WAITING_BREAKOUT"
                    bot_status["final_decision"] = "NO TRADE"
                    bot_status["decision_reason"] = "Retest failed: candle closed back inside range."
                elif c_low <= (orb_h + 10.0) and c_close >= orb_h:
                    tech_sl = c_low
                    entry_trig = c_high
                    risk_pts = max(entry_trig - tech_sl, 1.0)
                    rupee_risk = risk_pts * 0.50 * LOT_SIZE
                    target_lvl = entry_trig + (risk_pts * TARGET_R_RATIO)
                    
                    if rupee_risk > MAX_RISK_RUPEES:
                        bot_status["state"] = "DONE_FOR_DAY"
                        bot_status["final_decision"] = "NO TRADE"
                        bot_status["decision_reason"] = f"SKIPPED: Technical SL risk (₹{rupee_risk:.0f}) exceeds ₹{MAX_RISK_RUPEES:.0f} limit."
                    else:
                        bot_status["retest_high"] = c_high
                        bot_status["retest_low"]  = c_low
                        bot_status["state"] = "WAITING_BULL_TRIGGER"
                        bot_status["final_decision"] = "WAIT"

            elif state == "WAITING_BULL_TRIGGER":
                if c_high > bot_status["retest_high"]:
                    opt_sym = f"NIFTY {atm_strike} CE"
                    tech_sl = bot_status["retest_low"]
                    entry_trig = bot_status["retest_high"]
                    risk_pts = max(entry_trig - tech_sl, 1.0)
                    rupee_risk = risk_pts * 0.50 * LOT_SIZE
                    target_lvl = entry_trig + (risk_pts * TARGET_R_RATIO)

                    bot_status["active_position"] = {
                        "type": "CE",
                        "symbol": opt_sym,
                        "entry": c_close,
                        "sl": tech_sl,
                        "target": target_lvl,
                        "risk_rupees": rupee_risk
                    }
                    bot_status["trades_today"] += 1
                    bot_status["final_decision"] = "BUY CE"
                    
                    msg = (
                        f"🚀 *NIFTY BUY CE TRADE CONFIRMED!*\n\n"
                        f"• *Buy on Groww:* `1 Lot {opt_sym}`\n"
                        f"• *Entry Level (NIFTY):* `{entry_trig:.2f}`\n"
                        f"• *Stop-Loss (NIFTY):* `{tech_sl:.2f}`\n"
                        f"• *Target (2.0R):* `{target_lvl:.2f}`\n"
                        f"• *Max Planned Risk:* `₹{rupee_risk:.0f}` (Within ₹{MAX_RISK_RUPEES:.0f} limit)\n\n"
                        f"_Open Groww app now and buy 1 lot of {opt_sym}._"
                    )
                    log(f"BUY CONFIRMED: {opt_sym}")
                    send_telegram(msg)

        except Exception as e:
            log(f"Loop Exception: {str(e)}")

        time.sleep(30)

threading.Thread(target=bot_loop, daemon=True).start()

@app.get("/", response_class=HTMLResponse)
def index():
    return "<h1>🤖 NIFTY ORB Telegram Bot is Active and Scanning!</h1><p>Running 24/7 on Free Cloud • 100% Secure</p>"
