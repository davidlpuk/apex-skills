#!/usr/bin/env python3
import urllib.request
import json
from datetime import datetime, timezone, timedelta

US_WATCHLIST = {
    "AAPL":  "AAPL",
    "MSFT":  "MSFT",
    "NVDA":  "NVDA",
    "GOOGL": "GOOGL",
    "AMZN":  "AMZN",
    "META":  "META",
    "BRKB":  "BRK-B",
    "JPM":   "JPM",
    "JNJ":   "JNJ",
    "V":     "V",
    "ASML":  "ASML",
}

today = datetime.now(timezone.utc).date()
window_end = today + timedelta(days=5)

flagged = []
clear = []

headers = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json'
}

for name, ticker in US_WATCHLIST.items():
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # Try earnings dates
        dates = t.earnings_dates
        if dates is None or dates.empty:
            clear.append(name)
            continue

        for idx in dates.index:
            try:
                if hasattr(idx, 'date'):
                    ed = idx.date()
                else:
                    from pandas import Timestamp
                    ed = Timestamp(idx).date()

                days_away = (ed - today).days
                if 0 <= days_away <= 5:
                    flagged.append({
                        "name": name,
                        "ticker": ticker,
                        "earnings_date": str(ed),
                        "days_away": days_away
                    })
                    break
            except:
                continue
        else:
            clear.append(name)

    except Exception as e:
        clear.append(name)

# Save flags
with open('/home/ubuntu/.picoclaw/logs/apex-earnings-flags.json', 'w') as f:
    json.dump(flagged, f, indent=2)

# Output
lines = [f"📅 APEX EARNINGS CALENDAR — Week of {today}"]
lines.append(f"Window: {today} → {window_end}\n")

if flagged:
    lines.append("🚨 EARNINGS THIS WEEK — NO NEW ENTRIES:")
    for ev in flagged:
        lines.append(f"  ⛔ {ev['name']} ({ev['ticker']}) — {ev['earnings_date']} ({ev['days_away']} days)")
else:
    lines.append("✅ No earnings this week for watchlist instruments.")

lines.append(f"\n✅ Clear: {len(clear)} | 🚨 Flagged: {len(flagged)}")
print("\n".join(lines))
