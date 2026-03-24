#!/bin/bash
set -euo pipefail

PYTHON=/home/ubuntu/bin/python3
LOG=/home/ubuntu/.picoclaw/logs/apex-cron.log

# Optional session flag — 'midday' skips pre-flight intelligence refresh
# Usage: apex-morning-scan.sh midday
SESSION=${1:-am}
SESSION_FLAG=""
if [ "$SESSION" = "midday" ]; then
    SESSION_FLAG="--session=midday"
fi

# Market calendar check — skip if markets closed
CALENDAR_CHECK=$($PYTHON -c "
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_market_calendar import should_scan_today
ok, reason = should_scan_today()
print('OK' if ok else f'SKIP:{reason}')
" 2>/dev/null || echo "SKIP:calendar_error")

if echo "$CALENDAR_CHECK" | grep -q "SKIP"; then
    REASON=$(echo "$CALENDAR_CHECK" | sed 's/SKIP://')
    echo "$(date): Market closed — $REASON — skipping scan" >> "$LOG"
    exit 0
fi

# Data integrity check — auto-refresh stale files then hard-block if still failing
INTEGRITY=$($PYTHON /home/ubuntu/.picoclaw/scripts/apex-data-integrity.py quick 2>/dev/null || echo "ERROR")
if echo "$INTEGRITY" | grep -q "BLOCKED"; then
    echo "$(date): Data integrity BLOCKED — attempting auto-refresh..." >> "$LOG"
    /home/ubuntu/.picoclaw/scripts/apex-data-refresher.sh || true

    # Re-check after refresh
    INTEGRITY=$($PYTHON /home/ubuntu/.picoclaw/scripts/apex-data-integrity.py quick 2>/dev/null || echo "ERROR")
    if echo "$INTEGRITY" | grep -q "BLOCKED"; then
        echo "$(date): Still BLOCKED after refresh — aborting scan" >> "$LOG"
        echo "$INTEGRITY" >> "$LOG"
        $PYTHON -c "
import sys; sys.path.insert(0,'/home/ubuntu/.picoclaw/scripts')
from apex_utils import send_telegram
send_telegram('🚨 MORNING SCAN BLOCKED\n\nData integrity check failed even after auto-refresh.\nScan skipped — check logs.\n\npython3 ~/.picoclaw/scripts/apex-data-integrity.py')
" 2>/dev/null || true
        exit 1
    fi
    echo "$(date): Data refreshed successfully — proceeding with scan" >> "$LOG"
fi

# Pre-market futures gate — suppress TREND entries if S&P futures gap down > 1.5%
FUTURES_CHECK=$($PYTHON -c "
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    import yfinance as yf
    hist = yf.Ticker('ES=F').history(period='3d')
    if len(hist) >= 2:
        prev_close = float(hist['Close'].iloc[-2])
        current    = float(hist['Close'].iloc[-1])
        gap_pct    = (current - prev_close) / prev_close * 100
        if gap_pct <= -1.5:
            print(f'GAP_DOWN:{gap_pct:.2f}')
        elif gap_pct <= -0.8:
            print(f'WEAK:{gap_pct:.2f}')
        else:
            print(f'OK:{gap_pct:.2f}')
    else:
        print('OK:0.00')
except Exception as e:
    print(f'OK:error')
" 2>/dev/null || echo "OK:0.00")

if echo "$FUTURES_CHECK" | grep -q "^GAP_DOWN:"; then
    GAP_VAL=$(echo "$FUTURES_CHECK" | cut -d: -f2)
    echo "$(date): S&P futures gap down ${GAP_VAL}% — writing futures_gap_down flag" >> "$LOG"
    echo "GAP_DOWN_${GAP_VAL}_$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /home/ubuntu/.picoclaw/logs/apex-futures-gap.flag
    $PYTHON -c "
import sys; sys.path.insert(0,'/home/ubuntu/.picoclaw/scripts')
from apex_utils import send_telegram
send_telegram('⚠️ PRE-MARKET GAP DOWN\n\nS&P futures: $GAP_VAL%\n\nTREND entries suppressed today.\nCONTRARIAN entries still allowed.\n\nSend RESUME to override.')
" 2>/dev/null || true
else
    # Clear stale gap flag if market recovers
    rm -f /home/ubuntu/.picoclaw/logs/apex-futures-gap.flag
fi

# Reconcile positions before scanning
$PYTHON /home/ubuntu/.picoclaw/scripts/apex-reconcile.py > /dev/null 2>&1 || true
echo "$(date): Starting decision engine (session=${SESSION})" >> "$LOG"
$PYTHON /home/ubuntu/.picoclaw/scripts/apex-decision-engine.py $SESSION_FLAG >> "$LOG" 2>&1
echo "$(date): Decision engine complete (session=${SESSION})" >> "$LOG"
