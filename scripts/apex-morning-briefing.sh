#!/bin/bash
source /home/ubuntu/.picoclaw/scripts/apex-telegram.sh
PYTHON=/home/ubuntu/bin/python3

# Run all checks silently
$PYTHON /home/ubuntu/.picoclaw/scripts/apex-regime-check.py > /dev/null 2>&1
$PYTHON /home/ubuntu/.picoclaw/scripts/apex-drawdown-check.py > /dev/null 2>&1
$PYTHON /home/ubuntu/.picoclaw/scripts/apex-geo-news.py > /dev/null 2>&1
$PYTHON /home/ubuntu/.picoclaw/scripts/apex-news-check.py > /dev/null 2>&1

# Read results
REGIME=$($PYTHON -c "
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

GEO=$($PYTHON -c "
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

NEWS=$($PYTHON -c "
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

EARNINGS=$($PYTHON -c "
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

PORTFOLIO=$($PYTHON -c "
import subprocess, json

CACHE = '/home/ubuntu/.picoclaw/logs/apex-portfolio-cache.json'

def from_cache():
    try:
        with open(CACHE) as f:
            d = json.load(f)
        free     = float(d.get('free', 0))
        invested = float(d.get('invested', 0))
        ppl      = float(d.get('ppl', 0))
        total    = round(free + invested, 2)
        ts       = d.get('timestamp', '')[:10]
        return f'💼 Portfolio: £{total:.2f} | Cash: £{free:.2f} | P&L: £{ppl:.2f} (cached {ts})'
    except:
        return '💼 Portfolio: unavailable'

result = subprocess.run(
    ['bash', '-c', 'source /home/ubuntu/.picoclaw/.env.trading212 && curl -s --max-time 10 -H \"Authorization: Basic \$T212_AUTH\" \$T212_ENDPOINT/equity/account/cash'],
    capture_output=True, text=True
)
try:
    d = json.loads(result.stdout)
    free     = d.get('free')
    invested = d.get('invested')
    ppl      = d.get('ppl', 0)
    if free is None or invested is None:
        raise ValueError('missing fields')
    free = float(free); invested = float(invested); ppl = float(ppl)
    total = round(free + invested, 2)
    import datetime; d['timestamp'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(CACHE, 'w') as f:
        json.dump(d, f)
    print(f'💼 Portfolio: £{total:.2f} | Cash: £{free:.2f} | P&L: £{ppl:.2f}')
except:
    print(from_cache())
" 2>/dev/null)

DRAWDOWN=$($PYTHON -c "
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

AUTOPILOT=$($PYTHON -c "
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

MOMENTUM=$($PYTHON -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-outcomes.json') as f:
        outcomes = json.load(f)
    trades = outcomes.get('trades', [])
    if len(trades) < 3:
        print('📊 Momentum: collecting data ({}/3 trades)'.format(len(trades)))
    else:
        recent = trades[-5:]
        results = [1 if t.get('pnl', 0) > 0 else -1 for t in recent]
        if len(results) >= 5 and all(r > 0 for r in results[-5:]):
            print('🔥 Momentum: 5-win streak — sizing at 125%')
        elif len(results) >= 3 and all(r > 0 for r in results[-3:]):
            print('📈 Momentum: 3-win streak — sizing at 115%')
        elif len(results) >= 3 and all(r < 0 for r in results[-3:]):
            print('🛡️ Momentum: 3-loss streak — DEFENSIVE (max 1 trade/day, sizing at 75%)')
        elif len(results) >= 2 and all(r < 0 for r in results[-2:]):
            print('⚠️ Momentum: 2-loss streak — sizing at 90%')
        else:
            print('📊 Momentum: neutral — standard sizing')
except:
    print('📊 Momentum: unavailable')
" 2>/dev/null)

send_message "🌅 APEX MORNING BRIEFING — $(date '+%a %d %b %Y')

$PORTFOLIO
$AUTOPILOT
$MOMENTUM
$DRAWDOWN

$REGIME
$GEO
$NEWS
$EARNINGS

Morning scan fires at 08:30.
Reply DIGEST for full system summary."

