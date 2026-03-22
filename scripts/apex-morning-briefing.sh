#!/bin/bash
BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

# Run all checks silently
python3 /home/ubuntu/.picoclaw/scripts/apex-regime-check.py > /dev/null 2>&1
python3 /home/ubuntu/.picoclaw/scripts/apex-drawdown-check.py > /dev/null 2>&1
python3 /home/ubuntu/.picoclaw/scripts/apex-geo-news.py > /dev/null 2>&1
python3 /home/ubuntu/.picoclaw/scripts/apex-news-check.py > /dev/null 2>&1

# Read results
REGIME=$(python3 -c "
import json
with open('/home/ubuntu/.picoclaw/logs/apex-regime.json') as f:
    d = json.load(f)
vix      = d.get('vix', '?')
breadth  = d.get('breadth_pct', '?')
overall  = d.get('overall', '?')
reasons  = d.get('block_reason', [])
icon     = '✅' if overall == 'CLEAR' else '⚠️'
reason_str = ' | '.join(reasons) if reasons else 'Clear'
print(f'{icon} Regime: VIX {vix} | Breadth {breadth}% | {overall}')
if reasons:
    print(f'   {reason_str[:80]}')
" 2>/dev/null)

GEO=$(python3 -c "
import json
with open('/home/ubuntu/.picoclaw/logs/apex-geo-news.json') as f:
    d = json.load(f)
overall = d.get('overall', 'CLEAR')
flags   = d.get('energy_flags', []) + d.get('geo_flags', [])
if overall == 'ALERT':
    print(f'🚨 Geo: ALERT — {len(flags)} flags')
    if flags:
        print(f'   {flags[0][\"title\"][:80]}')
elif overall == 'WARN':
    print(f'⚠️ Geo: WARN — {len(flags)} flags')
else:
    print(f'✅ Geo: Clear')
" 2>/dev/null)

NEWS=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-news-flags.json') as f:
        flags = json.load(f)
    if flags:
        print(f'⚠️ News flags: {len(flags)} instruments blocked ({\" | \".join(flags[:3])})')
    else:
        print(f'✅ News: Clear')
except:
    print('✅ News: Clear')
" 2>/dev/null)

EARNINGS=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-earnings-flags.json') as f:
        flags = json.load(f)
    if flags:
        names = [f['name'] for f in flags]
        print(f'⚠️ Earnings: {len(flags)} blocked ({\" | \".join(names[:3])})')
    else:
        print(f'✅ Earnings: Clear')
except:
    print('✅ Earnings: Clear')
" 2>/dev/null)

PORTFOLIO=$(python3 -c "
import subprocess, json
result = subprocess.run(
    ['bash', '-c', 'source /home/ubuntu/.picoclaw/.env.trading212 && curl -s -H \"Authorization: Basic \$T212_AUTH\" https://demo.trading212.com/api/v0/equity/account/cash'],
    capture_output=True, text=True
)
try:
    d = json.loads(result.stdout)
    free     = d.get('free', 0)
    invested = d.get('invested', 0)
    ppl      = d.get('ppl', 0)
    total    = round(float(free) + float(invested), 2)
    print(f'💼 Portfolio: £{total:.2f} | Cash: £{float(free):.2f} | P&L: £{float(ppl):.2f}')
except:
    print('💼 Portfolio: unavailable')
" 2>/dev/null)

DRAWDOWN=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-drawdown.json') as f:
        d = json.load(f)
    status = d.get('status','NORMAL')
    pct    = d.get('drawdown_pct', 0)
    mult   = d.get('multiplier', 1.0)
    if status == 'NORMAL':
        print(f'✅ Drawdown: {pct}% — full sizing')
    elif status == 'HALT':
        print(f'🚨 Drawdown: {pct}% — TRADING HALTED')
    else:
        print(f'⚠️ Drawdown: {pct}% — sizing at {int(mult*100)}%')
except:
    print('✅ Drawdown: normal')
" 2>/dev/null)

AUTOPILOT=$(python3 -c "
import json, os
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-autopilot.json') as f:
        d = json.load(f)
    enabled = d.get('enabled', False)
    paused  = os.path.exists('/home/ubuntu/.picoclaw/logs/apex-paused.flag')
    trades  = d.get('trades_today', 0)
    maxtr   = d.get('max_trades_per_day', 2)
    if paused:
        print(f'⏸️ Autopilot: PAUSED')
    elif enabled:
        print(f'🤖 Autopilot: ON | Trades today: {trades}/{maxtr}')
    else:
        print(f'👤 Autopilot: MANUAL')
except:
    print('👤 Autopilot: MANUAL')
" 2>/dev/null)

send_message "🌅 APEX MORNING BRIEFING — $(date '+%a %d %b %Y')

$PORTFOLIO
$AUTOPILOT
$DRAWDOWN

$REGIME
$GEO
$NEWS
$EARNINGS

Morning scan fires at 08:30.
Reply STATUS for full details."

