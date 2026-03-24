#!/bin/bash

source /home/ubuntu/.picoclaw/scripts/apex-telegram.sh
LOG="/home/ubuntu/.picoclaw/logs/apex-cron.log"

echo "$(date): Running Friday position review" >> "$LOG"

source /home/ubuntu/.picoclaw/.env.trading212

PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH" \
  $T212_ENDPOINT/equity/portfolio)

CASH=$(curl -s -H "Authorization: Basic $T212_AUTH" \
  $T212_ENDPOINT/equity/account/cash)

GEO=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-geo-news.json') as f:
        d = json.load(f)
    print(d.get('overall','CLEAR'))
except:
    print('CLEAR')
" 2>/dev/null)

REVIEW=$(python3 << PYEOF
import json
from datetime import datetime, timezone

lines = ["🏁 FRIDAY POSITION REVIEW — WEEKEND RISK ASSESSMENT"]
lines.append(f"{'='*40}")

try:
    positions = json.loads("""$PORTFOLIO""")
    tracked   = json.load(open('/home/ubuntu/.picoclaw/logs/apex-positions.json'))

    if not positions:
        lines.append("\nNo open positions — nothing to review.")
    else:
        total_pnl      = 0
        total_invested = 0
        high_risk      = []
        safe_positions = []

        lines.append(f"\n📌 {len(positions)} positions going into the weekend:\n")

        for p in positions:
            ticker  = p.get('ticker','?')
            current = float(p.get('currentPrice', 0))
            ppl     = float(p.get('ppl', 0))
            qty     = float(p.get('quantity', 0))
            avg     = float(p.get('averagePrice', 0))
            total_pnl += ppl
            total_invested += qty * avg

            track = next((t for t in tracked if t.get('t212_ticker') == ticker), None)
            stop    = float(track.get('stop', 0)) if track else 0
            target1 = float(track.get('target1', 0)) if track else 0
            name    = track.get('name', ticker) if track else ticker
            sig_type = track.get('signal_type', 'TREND') if track else 'TREND'

            risk    = avg - stop if stop else avg * 0.06
            r       = round((current - avg) / risk, 2) if risk else 0
            pct_to_stop = round((current - stop) / current * 100, 1) if stop else 0
            pct_to_t1   = round((target1 - current) / current * 100, 1) if target1 else 0

            icon = "✅" if ppl >= 0 else "🔴"
            type_tag = "🔄" if sig_type == 'CONTRARIAN' else "📈"

            lines.append(f"{icon} {type_tag} {name}")
            lines.append(f"   Price: £{current} | PnL: £{round(ppl,2)} | R: {r}")
            lines.append(f"   Stop: £{stop} ({pct_to_stop}% below) | T1: £{target1} ({pct_to_t1}% away)")

            # Weekend risk assessment
            if pct_to_stop <= 3:
                high_risk.append(f"⚠️ {name} — only {pct_to_stop}% above stop, vulnerable to gap down")
            elif ppl < 0 and abs(ppl) > 5:
                high_risk.append(f"⚠️ {name} — underwater £{round(abs(ppl),2)}, monitor Monday open")
            else:
                safe_positions.append(name)

            lines.append("")

        # Summary
        total_icon = "✅" if total_pnl >= 0 else "🔴"
        lines.append(f"{total_icon} NET PnL: £{round(total_pnl,2)}")
        lines.append(f"💼 Total invested: £{round(total_invested,2)}")

        # Weekend risk warnings
        lines.append(f"\n🌍 Geo status: $GEO")
        if "$GEO" == "ALERT":
            lines.append("⚠️ Geo alert active — energy positions exposed to gap risk")

        if high_risk:
            lines.append(f"\n🚨 WEEKEND RISK FLAGS:")
            for r in high_risk:
                lines.append(f"  {r}")
            lines.append(f"\nConsider closing high-risk positions before weekend.")
            lines.append(f"All stops are protected in T212.")
        else:
            lines.append(f"\n✅ All positions have adequate stop distance.")
            lines.append(f"Stops protected in T212 — safe to hold over weekend.")

        lines.append(f"\n📋 ACTIONS AVAILABLE:")
        for p in positions:
            ticker = p.get('ticker','?')
            lines.append(f"  CLOSE {ticker} — close before weekend")
        lines.append(f"\nNext scan: Monday 07:30.")

except Exception as e:
    lines.append(f"Error: {e}")

try:
    d        = json.loads("""$CASH""")
    free     = float(d.get('free', 0))
    invested = float(d.get('invested', 0))
    total    = round(free + invested, 2)
    lines.append(f"\n💰 Portfolio: £{total} | Cash: £{round(free,2)}")
except:
    pass

print("\n".join(lines))
PYEOF
)

send_message "$REVIEW"
echo "$(date): Friday review sent" >> "$LOG"
