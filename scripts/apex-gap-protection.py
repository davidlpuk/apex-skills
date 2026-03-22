#!/usr/bin/env python3
import yfinance as yf
import json
import subprocess
from datetime import datetime, timezone

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

YAHOO_MAP = {
    "VUAGl_EQ":   "VUAG.L",
    "XOM_US_EQ":  "XOM",
    "V_US_EQ":    "V",
    "AAPL_US_EQ": "AAPL",
    "MSFT_US_EQ": "MSFT",
    "NVDA_US_EQ": "NVDA",
    "GOOGL_US_EQ":"GOOGL",
    "JPM_US_EQ":  "JPM",
    "GS_US_EQ":   "GS",
    "SHEL_EQ":    "SHEL.L",
    "HSBA_EQ":    "HSBA.L",
    "AZN_EQ":     "AZN.L",
}

def send_telegram(message):
    subprocess.run([
        'bash', '-c',
        f'''BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot${{BOT_TOKEN}}/sendMessage" \
  -d chat_id="6808823889" \
  --data-urlencode "text={message}"'''
    ], capture_output=True, text=True)

def get_premarket_price(yahoo_ticker):
    # Try Alpaca for US stocks first
    ticker = yahoo_ticker.replace('.L','').replace('.PA','').replace('.AS','').replace('.DE','')
    us_tickers = {
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
        "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
        "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
        "WMT","PG","XOM","CVX","NVO"
    }
    if ticker in us_tickers:
        try:
            import sys
            sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
            import apex_alpaca as alpaca
            snap = alpaca.get_snapshot(ticker)
            if snap and snap['prev_close'] > 0:
                return snap['current'], snap['prev_close']
        except Exception as _e:
            log_error(f"Silent failure in apex-gap-protection.py: {_e}")

    # Fall back to yfinance
    try:
        t    = yf.Ticker(yahoo_ticker)
        info = t.info
        pre  = info.get('preMarketPrice') or info.get('regularMarketPrice') or 0
        prev = info.get('previousClose') or info.get('regularMarketPreviousClose') or 0
        return float(pre), float(prev)
    except:
        return None, None

def run():
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        print("No positions")
        return

    if not positions:
        print("No open positions")
        return

    now      = datetime.now(timezone.utc)
    alerts   = []
    warnings = []

    print(f"Gap protection check — {now.strftime('%H:%M UTC')}", flush=True)

    for pos in positions:
        ticker  = pos.get('t212_ticker', '')
        name    = pos.get('name', ticker)
        entry   = float(pos.get('entry', 0))
        stop    = float(pos.get('stop', 0))
        yahoo   = YAHOO_MAP.get(ticker, '')

        if not yahoo:
            continue

        pre_price, prev_close = get_premarket_price(yahoo)

        if not pre_price or not prev_close:
            continue

        # Gap calculation
        gap_pct = round((pre_price - prev_close) / prev_close * 100, 2)
        print(f"  {name}: prev close £{prev_close} → pre-market £{pre_price} ({gap_pct:+.1f}%)")

        # Gap through stop
        if pre_price < stop:
            gap_through = round(stop - pre_price, 2)
            alerts.append(
                f"🚨 GAP THROUGH STOP — {name}\n"
                f"Pre-market: £{pre_price} | Stop: £{stop}\n"
                f"Will open £{gap_through} BELOW your stop\n"
                f"Actual loss will exceed planned risk\n"
                f"Consider: CLOSE {ticker} at open"
            )

        # Gap down warning — approaching stop
        elif pre_price < entry and gap_pct < -2:
            pct_above_stop = round((pre_price - stop) / stop * 100, 1)
            warnings.append(
                f"⚠️ GAP DOWN WARNING — {name}\n"
                f"Pre-market: £{pre_price} ({gap_pct:+.1f}%)\n"
                f"Still {pct_above_stop}% above stop £{stop}\n"
                f"Monitor closely at open"
            )

        # Gap up — good news
        elif gap_pct > 2:
            pct_to_t1 = round((pos.get('target1', 0) - pre_price) / pre_price * 100, 1)
            warnings.append(
                f"📈 GAP UP — {name}\n"
                f"Pre-market: £{pre_price} ({gap_pct:+.1f}%)\n"
                f"Still {pct_to_t1}% from Target 1"
            )

    if alerts:
        for alert in alerts:
            send_telegram(alert)
            print(f"ALERT: {alert[:50]}")
    elif warnings:
        combined = "🌅 PRE-MARKET GAP CHECK\n\n" + "\n\n".join(warnings)
        send_telegram(combined)
        print(f"WARNINGS sent: {len(warnings)}")
    else:
        print("No significant gaps detected — all positions safe at open")

if __name__ == '__main__':
    run()
