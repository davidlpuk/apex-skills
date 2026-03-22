#!/usr/bin/env python3
import yfinance as yf
import json
import sys

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

WATCHLIST_YAHOO = {
    "VWRP": "VWRP.L", "VUAG": "VUAG.L", "AAPL": "AAPL",
    "MSFT": "MSFT",   "NVDA": "NVDA",   "GOOGL": "GOOGL",
    "AMZN": "AMZN",   "META": "META",   "XOM": "XOM",
    "CVX":  "CVX",    "JPM":  "JPM",    "GS":  "GS",
    "BAC":  "BAC",    "JNJ":  "JNJ",    "BA":  "BA.L",
    "SHEL": "SHEL.L", "BP":   "BP.L",   "ASML": "ASML.AS",
}

def check_correlation(new_instrument):
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        positions = []

    if not positions:
        print(f"CLEAR|No open positions to correlate against")
        return

    # Get 3 months of data for new instrument
    new_ticker = WATCHLIST_YAHOO.get(new_instrument, new_instrument)
    try:
        new_hist = yf.Ticker(new_ticker).history(period="3mo")['Close'].pct_change().dropna()
    except:
        print(f"CLEAR|Could not fetch data for {new_instrument}")
        return

    correlations = []
    for pos in positions:
        pos_name = pos.get('name', '')
        pos_t212 = pos.get('t212_ticker', '')

        # Find yahoo ticker for existing position
        pos_yahoo = None
        for name, yahoo in WATCHLIST_YAHOO.items():
            if name in pos_t212 or pos_t212 in name:
                pos_yahoo = yahoo
                break

        if not pos_yahoo:
            continue

        try:
            pos_hist = yf.Ticker(pos_yahoo).history(period="3mo")['Close'].pct_change().dropna()
            # Align dates
            combined = new_hist.align(pos_hist, join='inner')
            if len(combined[0]) < 20:
                continue
            corr = round(float(combined[0].corr(combined[1])), 2)
            correlations.append({
                "position": pos_name,
                "correlation": corr,
                "flagged": corr > 0.7
            })
        except:
            continue

    if not correlations:
        print(f"CLEAR|No correlation data available")
        return

    flagged = [c for c in correlations if c['flagged']]

    if flagged:
        for f in flagged:
            print(f"BLOCK|{new_instrument} has {f['correlation']} correlation with open position {f['position']} — too similar, diversify")
    else:
        for c in correlations:
            print(f"CLEAR|{new_instrument} vs {c['position']}: correlation {c['correlation']} — acceptable")

if __name__ == '__main__':
    instrument = sys.argv[1] if len(sys.argv) > 1 else "XOM"
    check_correlation(instrument)
