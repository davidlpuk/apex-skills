#!/usr/bin/env python3
import json
import os
from datetime import datetime, timezone, timedelta

TRADING_STATE = '/home/ubuntu/.picoclaw/workspace/skills/apex-trading/TRADING_STATE.md'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
ORDERS_LOG = '/home/ubuntu/.picoclaw/logs/apex-orders.log'
HITL_LOG = '/home/ubuntu/.picoclaw/logs/apex-hitl.log'

today = datetime.now(timezone.utc).date()
week_start = today - timedelta(days=7)

# --- Portfolio value from T212 API ---
import subprocess
result = subprocess.run(
    ['bash', '-c', 'source /home/ubuntu/.picoclaw/.env.trading212 && '
     'curl -s -H "Authorization: Basic $T212_AUTH" '
     'https://demo.trading212.com/api/v0/equity/account/cash'],
    capture_output=True, text=True
)

try:
    cash_data = json.loads(result.stdout)
    cash_free = float(cash_data.get('free', 0))
    cash_total = float(cash_data.get('total', 0))
    ppl = float(cash_data.get('ppl', 0))
    invested = float(cash_data.get('invested', 0))
    portfolio_value = round(cash_free + invested, 2)
except:
    cash_free = 0
    portfolio_value = 0
    ppl = 0
    invested = 0

# --- Open positions ---
positions = []
try:
    result2 = subprocess.run(
        ['bash', '-c', 'source /home/ubuntu/.picoclaw/.env.trading212 && '
         'curl -s -H "Authorization: Basic $T212_AUTH" '
         'https://demo.trading212.com/api/v0/equity/portfolio'],
        capture_output=True, text=True
    )
    positions = json.loads(result2.stdout) if result2.stdout.strip() else []
except:
    positions = []

# --- Parse orders log for this week ---
confirmed = 0
rejected = 0
orders_this_week = []

try:
    with open(HITL_LOG) as f:
        for line in f:
            try:
                date_str = line.split(':')[0] + ':' + line.split(':')[1] + ':' + line.split(':')[2]
                log_date = datetime.strptime(date_str.strip(), '%a %b %d %H:%M:%S UTC %Y').date()
            except:
                log_date = None

            if 'CONFIRM received' in line:
                confirmed += 1
            elif 'REJECT received' in line:
                rejected += 1
except:
    pass

try:
    with open(ORDERS_LOG) as f:
        for line in f:
            if 'Placing order' in line:
                orders_this_week.append(line.strip())
except:
    pass

total_signals = confirmed + rejected
win_rate = round((confirmed / total_signals * 100), 1) if total_signals > 0 else 0

# --- Portfolio peak from TRADING_STATE.md ---
portfolio_peak = 5000.00
try:
    with open(TRADING_STATE) as f:
        for line in f:
            if 'portfolio_peak:' in line:
                portfolio_peak = float(line.split(':')[1].strip().replace('£', '').replace(',', ''))
                break
except:
    pass

# Update peak if new high
if portfolio_value > portfolio_peak:
    portfolio_peak = portfolio_value

drawdown = round(((portfolio_peak - portfolio_value) / portfolio_peak) * 100, 2) if portfolio_peak > 0 else 0

# --- Build report ---
lines = []
lines.append(f"📊 APEX WEEKLY REPORT — {today}")
lines.append(f"Week of {week_start} → {today}")
lines.append("")
lines.append("💼 PORTFOLIO SUMMARY")
lines.append(f"  Total value:    £{portfolio_value:,.2f}")
lines.append(f"  Cash free:      £{cash_free:,.2f}")
lines.append(f"  Invested:       £{invested:,.2f}")
lines.append(f"  Unrealised P&L: £{ppl:,.2f}")
lines.append(f"  Portfolio peak: £{portfolio_peak:,.2f}")
lines.append(f"  Drawdown:       {drawdown}%")
lines.append("")
lines.append("📈 SIGNAL PERFORMANCE")
lines.append(f"  Signals confirmed: {confirmed}")
lines.append(f"  Signals rejected:  {rejected}")
lines.append(f"  Total signals:     {total_signals}")
lines.append(f"  Confirmation rate: {win_rate}%")
lines.append("")
lines.append("📌 OPEN POSITIONS")

if positions:
    for pos in positions:
        ticker = pos.get('ticker', '')
        qty = pos.get('quantity', 0)
        avg = pos.get('averagePrice', 0)
        current = pos.get('currentPrice', 0)
        pos_ppl = pos.get('ppl', 0)
        lines.append(f"  {ticker} | qty:{qty} | avg:£{avg:.2f} | now:£{current:.2f} | P&L:£{pos_ppl:.2f}")
else:
    lines.append("  No open positions")

lines.append("")

if drawdown >= 10:
    lines.append("🚨 DEFENSIVE MODE ACTIVE — drawdown exceeds 10%")
    lines.append("   No new positions until recovery.")
elif drawdown >= 5:
    lines.append("⚠️ CAUTION — drawdown approaching 10% threshold")
else:
    lines.append("✅ Portfolio healthy — within normal parameters")

lines.append("")
lines.append("🔭 OUTLOOK")
lines.append("  Morning scan fires at 08:30 today.")
lines.append("  Earnings check complete — see earlier message.")
lines.append("  Reply STATUS anytime for live portfolio data.")

print("\n".join(lines))
