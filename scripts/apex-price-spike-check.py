#!/usr/bin/env python3
import yfinance as yf
import json
import sys

WATCHLIST_YAHOO = {
    "XOM": "XOM", "CVX": "CVX", "SHEL": "SHEL.L",
    "BP": "BP.L", "TTE": "TTE.PA", "AAPL": "AAPL",
    "MSFT": "MSFT", "NVDA": "NVDA", "GOOGL": "GOOGL",
    "AMZN": "AMZN", "META": "META", "TSLA": "TSLA",
    "JPM": "JPM", "GS": "GS", "BAC": "BAC",
}

def check_price_spike(name, threshold=2.5):
    yahoo = WATCHLIST_YAHOO.get(name, name)
    try:
        hist = yf.Ticker(yahoo).history(period="2d")
        if len(hist) < 2:
            return None, None

        prev_close   = float(hist['Close'].iloc[-2])
        current      = float(hist['Close'].iloc[-1])
        change_pct   = round((current - prev_close) / prev_close * 100, 2)
        direction    = "↑" if change_pct > 0 else "↓"

        if abs(change_pct) >= threshold:
            return "SPIKE", f"{name} moved {change_pct:+.1f}% {direction} — possible news event, skip"
        elif abs(change_pct) >= threshold * 0.6:
            return "WARN", f"{name} moved {change_pct:+.1f}% {direction} — elevated, proceed with caution"
        else:
            return "CLEAR", f"{name} {change_pct:+.1f}% — normal range"

    except Exception as e:
        return "UNKNOWN", str(e)

if __name__ == '__main__':
    name = sys.argv[1] if len(sys.argv) > 1 else "XOM"
    status, reason = check_price_spike(name)
    print(f"{status}|{reason}")
