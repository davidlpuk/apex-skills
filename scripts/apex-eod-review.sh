#!/bin/bash

source /home/ubuntu/.picoclaw/scripts/apex-telegram.sh
LOG="/home/ubuntu/.picoclaw/logs/apex-cron.log"

echo "$(date): Running EOD review" >> "$LOG"

source /home/ubuntu/.picoclaw/.env.trading212

CASH=$(curl -s -H "Authorization: Basic $T212_AUTH" \
  $T212_ENDPOINT/equity/account/cash)

PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH" \
  $T212_ENDPOINT/equity/portfolio)

REVIEW=$(python3 - << PYEOF
import json
from datetime import datetime, timezone

today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
now   = datetime.now(timezone.utc).strftime('%a %d %b %Y')
lines = [f"📊 APEX EOD REVIEW — {now}"]

# Portfolio
try:
    d        = json.loads('''$CASH''')
    free     = float(d.get('free', 0))
    invested = float(d.get('invested', 0))
    ppl      = float(d.get('ppl', 0))
    total    = round(free + invested, 2)
    lines.append(f"\n💼 Portfolio: £{total:.2f} | Cash: £{free:.2f} | PnL: £{ppl:.2f}")
except:
    lines.append("\n💼 Portfolio: unavailable")

# Positions
positions = []
try:
    positions = json.loads('''$PORTFOLIO''') if '''$PORTFOLIO'''.strip() else []
    if positions:
        lines.append(f"\n📌 Open positions ({len(positions)}):")
        for p in positions:
            ticker  = p.get('ticker','?')
            qty     = p.get('quantity', 0)
            current = p.get('currentPrice', 0)
            pos_ppl = float(p.get('ppl', 0))
            icon    = "✅" if pos_ppl >= 0 else "🔴"
            lines.append(f"  {icon} {ticker} | qty:{qty} | £{current} | PnL: £{round(pos_ppl,2)}")
    else:
        lines.append("\n📌 No open positions")
except:
    lines.append("\n📌 Positions: unavailable")

# Net PnL
net_pnl  = sum(float(p.get('ppl', 0)) for p in positions)
net_icon = "✅" if net_pnl >= 0 else "🔴"
lines.append(f"\n{net_icon} Net PnL: £{round(net_pnl, 2)}")

# Signal log
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-signal-log.json') as f:
        sig_db = json.load(f)
    today_sigs = [s for s in sig_db.get('signals', []) if s.get('date') == today]
    if today_sigs:
        confirmed = [s for s in today_sigs if s.get('action') == 'CONFIRMED']
        blocked   = [s for s in today_sigs if s.get('action') != 'CONFIRMED']
        lines.append(f"\n📡 Signals today: {len(today_sigs)}")
        if confirmed:
            lines.append(f"  ✅ Executed: {', '.join(s['name'] for s in confirmed)}")
        if blocked:
            lines.append(f"  ⛔ Blocked: {len(blocked)}")
    else:
        lines.append("\n📡 No signals today")
except:
    pass

# Regime
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-regime.json') as f:
        regime = json.load(f)
    vix     = regime.get('vix', '?')
    breadth = regime.get('breadth_pct', '?')
    overall = regime.get('overall', '?')
    icon    = "✅" if overall == "CLEAR" else "⚠️"
    lines.append(f"\n{icon} Regime: VIX {vix} | Breadth {breadth}% | {overall}")
except:
    pass

# Geo
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-geo-news.json') as f:
        geo = json.load(f)
    if geo.get('overall') == 'ALERT':
        lines.append(f"🌍 Geo: ALERT — energy trades blocked")
    elif geo.get('overall') == 'WARN':
        lines.append(f"🌍 Geo: WARN — monitoring")
    else:
        lines.append(f"🌍 Geo: Clear")
except:
    pass

# Autopilot
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-autopilot.json') as f:
        ap = json.load(f)
    enabled = ap.get('enabled', False)
    trades  = ap.get('trades_today', 0)
    total   = ap.get('total_autonomous_trades', 0)
    status  = f"ON | Trades today: {trades} | Total: {total}" if enabled else "MANUAL"
    lines.append(f"\n🤖 Autopilot: {status}")
except:
    pass

lines.append("\nNext scan: Monday 07:30.")
print("\n".join(lines))
PYEOF
)

send_message "$REVIEW"
echo "$(date): EOD review sent" >> "$LOG"
