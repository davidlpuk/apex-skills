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


SECTOR_FILE = '/home/ubuntu/.picoclaw/logs/apex-sector-rotation.json'

SECTORS = {
    "Technology":   "IITU.L",
    "Financials":   "IUFS.L",
    "Healthcare":   "IUHC.L",
    "Energy":       "IUES.L",
    "Consumer":     "IUCD.L",
    "World":        "VWRP.L",
    "UK FTSE":      "ISF.L",
    "Gold":         "SGLN.L",
}

def get_sector_data(name, ticker):
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="3mo")
        if hist.empty or len(hist) < 20:
            return None

        close = hist['Close']
        # Fix pence to pounds for UK-listed ETFs
        if close.iloc[-1] > 500:
            close = close / 100
        price = round(float(close.iloc[-1]), 2)

        # Returns over different periods
        ret_1w  = round((close.iloc[-1] / close.iloc[-5] - 1) * 100, 2)  if len(close) >= 5  else 0
        ret_1m  = round((close.iloc[-1] / close.iloc[-21] - 1) * 100, 2) if len(close) >= 21 else 0
        ret_3m  = round((close.iloc[-1] / close.iloc[0] - 1) * 100, 2)

        # Trend
        ema20 = float(close.ewm(span=20).mean().iloc[-1])
        ema50 = float(close.ewm(span=50).mean().iloc[-1])
        trend = "BULLISH" if price > ema20 > ema50 else ("BEARISH" if price < ema20 < ema50 else "NEUTRAL")

        # Momentum score
        score = 0
        if ret_1w > 0:  score += 1
        if ret_1m > 0:  score += 2
        if ret_3m > 0:  score += 2
        if trend == "BULLISH": score += 3
        if ret_1w > 1:  score += 1
        if ret_1m > 3:  score += 1

        return {
            "name":    name,
            "ticker":  ticker,
            "price":   price,
            "ret_1w":  ret_1w,
            "ret_1m":  ret_1m,
            "ret_3m":  ret_3m,
            "trend":   trend,
            "score":   score,
            "max":     10
        }
    except:
        return None

def run():
    now     = datetime.now(timezone.utc)
    results = []

    print("Scanning sector rotation...", flush=True)

    for name, ticker in SECTORS.items():
        print(f"  {name}...", flush=True)
        data = get_sector_data(name, ticker)
        if data:
            results.append(data)

    # Sort by momentum score
    results.sort(key=lambda x: x['score'], reverse=True)

    output = {
        "timestamp": now.strftime('%Y-%m-%d %H:%M UTC'),
        "sectors":   results,
        "leaders":   [r['name'] for r in results[:3]],
        "laggards":  [r['name'] for r in results[-3:]]
    }

    atomic_write(SECTOR_FILE, output)

    print(f"\n=== SECTOR ROTATION HEAT MAP ===")
    print(f"{'Sector':15} | {'Score':5} | {'1W':7} | {'1M':7} | {'3M':7} | Trend")
    print("-" * 65)
    for r in results:
        arrow = "↑" if r['ret_1w'] > 0 else "↓"
        icon  = "🟢" if r['score'] >= 7 else ("🟡" if r['score'] >= 4 else "🔴")
        print(f"{icon} {r['name']:13} | {r['score']:5}/10 | {r['ret_1w']:+6.1f}% | {r['ret_1m']:+6.1f}% | {r['ret_3m']:+6.1f}% | {r['trend']}")

    print(f"\n🚀 Leading:  {', '.join(output['leaders'])}")
    print(f"📉 Lagging:  {', '.join(output['laggards'])}")

    return output

if __name__ == '__main__':
    run()
