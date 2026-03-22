#!/usr/bin/env python3
import yfinance as yf
import json
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


BREADTH_FILE = '/home/ubuntu/.picoclaw/logs/apex-breadth-drilldown.json'

# Representative stocks per sector — 10 per sector
SECTOR_STOCKS = {
    "Energy": {
        "tickers": ["XOM","CVX","SHEL.L","BP.L","TTE.PA","NG.L","SSE.L","IUES.L","INRG.L","ENB"],
        "etf": "IUES.L"
    },
    "Technology": {
        "tickers": ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","AMD","CRM","ORCL","QCOM"],
        "etf": "IITU.L"
    },
    "Financials": {
        "tickers": ["JPM","GS","MS","BAC","BLK","V","AXP","HSBA.L","BARC.L","NWG.L"],
        "etf": "IUFS.L"
    },
    "Healthcare": {
        "tickers": ["JNJ","PFE","MRK","UNH","ABBV","AZN.L","GSK.L","NVO","TMO","DHR"],
        "etf": "IUHC.L"
    },
    "Consumer": {
        "tickers": ["KO","PEP","MCD","WMT","PG","DGE.L","ULVR.L","CPG.L","IMB.L","BATS.L"],
        "etf": "IUCD.L"
    }
}

def get_breadth(tickers):
    above_200 = 0
    above_50  = 0
    total     = 0

    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period="1y")
            if hist.empty or len(hist) < 50:
                continue

            close = hist['Close']
            if close.iloc[-1] > 500 and ticker.endswith('.L'):
                close = close / 100

            price  = float(close.iloc[-1])
            ema200 = float(close.ewm(span=200).mean().iloc[-1])
            ema50  = float(close.ewm(span=50).mean().iloc[-1])

            total += 1
            if price > ema200:
                above_200 += 1
            if price > ema50:
                above_50 += 1
        except:
            continue

    if total == 0:
        return None

    return {
        "total":        total,
        "above_200":    above_200,
        "above_50":     above_50,
        "breadth_200":  round(above_200 / total * 100, 1),
        "breadth_50":   round(above_50 / total * 100, 1),
        "health":       "BULLISH" if above_200/total >= 0.6 else ("NEUTRAL" if above_200/total >= 0.4 else "BEARISH")
    }

def run():
    now     = datetime.now(timezone.utc)
    results = {}

    print("Running sector breadth drill-down...", flush=True)

    for sector, data in SECTOR_STOCKS.items():
        print(f"  {sector}...", flush=True)
        breadth = get_breadth(data['tickers'])
        if breadth:
            results[sector] = breadth

    # Sort by breadth
    sorted_sectors = sorted(results.items(), key=lambda x: x[1]['breadth_200'], reverse=True)

    print(f"\n=== SECTOR BREADTH DRILL-DOWN ===")
    print(f"{'Sector':12} | {'Above 200EMA':12} | {'Above 50EMA':11} | Health")
    print("-" * 60)

    for sector, data in sorted_sectors:
        icon = "🟢" if data['health'] == 'BULLISH' else ("🟡" if data['health'] == 'NEUTRAL' else "🔴")
        bar  = "█" * int(data['breadth_200'] / 10)
        print(f"{icon} {sector:10} | {data['breadth_200']:5}% ({data['above_200']}/{data['total']}) | {data['breadth_50']:5}% | {data['health']}")

    # Strongest and weakest
    if sorted_sectors:
        strongest = sorted_sectors[0]
        weakest   = sorted_sectors[-1]
        print(f"\n🚀 Strongest: {strongest[0]} ({strongest[1]['breadth_200']}% above 200 EMA)")
        print(f"📉 Weakest:   {weakest[0]} ({weakest[1]['breadth_200']}% above 200 EMA)")

    # Save
    output = {
        "timestamp": now.strftime('%Y-%m-%d %H:%M UTC'),
        "sectors":   {k: v for k, v in results.items()},
        "strongest": sorted_sectors[0][0] if sorted_sectors else None,
        "weakest":   sorted_sectors[-1][0] if sorted_sectors else None
    }

    atomic_write(BREADTH_FILE, output)

    return output

if __name__ == '__main__':
    run()
