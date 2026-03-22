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


def check_market_direction():
    checks = {
        "SP500":  "VUAG.L",
        "FTSE100": "ISF.L",
        "WORLD":  "VWRP.L"
    }

    results  = {}
    blocks   = []
    warnings = []

    for name, ticker in checks.items():
        try:
            hist = yf.Ticker(ticker).history(period="2d", interval="1h")
            if hist.empty or len(hist) < 2:
                continue

            # Today's open vs current price
            today_bars = hist[hist.index.date == hist.index[-1].date()]
            if today_bars.empty:
                continue

            day_open    = float(today_bars['Open'].iloc[0])
            day_current = float(today_bars['Close'].iloc[-1])
            day_change  = round((day_current - day_open) / day_open * 100, 2)

            results[name] = {
                "open":    round(day_open, 2),
                "current": round(day_current, 2),
                "change":  day_change
            }

            if day_change <= -1.5:
                blocks.append(f"{name} down {day_change}% today — market falling, skip new longs")
            elif day_change <= -0.8:
                warnings.append(f"{name} down {day_change}% today — weak market, be cautious")

        except Exception as e:
            results[name] = {"error": str(e)}

    # Save result
    output = {
        "markets":   results,
        "blocks":    blocks,
        "warnings":  warnings,
        "overall":   "BLOCKED" if blocks else ("WARN" if warnings else "CLEAR"),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    }

    atomic_write('/home/ubuntu/.picoclaw/logs/apex-market-direction.json', output)

    # Print result
    print(f"\n=== MARKET DIRECTION ===")
    for name, data in results.items():
        if "error" not in data:
            arrow = "↑" if data['change'] > 0 else "↓"
            icon  = "✅" if data['change'] > -0.8 else ("⚠️" if data['change'] > -1.5 else "🚨")
            print(f"  {icon} {name:8} | {data['change']:+.2f}% today | {data['current']}")

    if blocks:
        for b in blocks:
            print(f"  🚨 BLOCK: {b}")
    elif warnings:
        for w in warnings:
            print(f"  ⚠️ WARN: {w}")
    else:
        print(f"  ✅ Market direction clear for new longs")

    print(f"  Overall: {output['overall']}")
    return output

if __name__ == '__main__':
    check_market_direction()
