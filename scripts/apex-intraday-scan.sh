#!/bin/bash
# apex-intraday-scan.sh — Lightweight intraday signal re-scan
#
# Refreshes the fast intraday intelligence sources (regime, direction,
# contrarian RSI, inverse VIX scanner) then runs the decision engine
# with a unique named session so the AM/PM idempotency guard does not
# block it.
#
# Usage: apex-intraday-scan.sh <session_name>
#   session_name: 10am | 11am | 13pm | 14pm  (any unique string works)
#
# Called from cron at 10:00, 11:00, 13:00, 14:00 UTC Mon-Fri.
set -euo pipefail

PYTHON=/home/ubuntu/bin/python3
LOG=/home/ubuntu/.picoclaw/logs/apex-cron.log
SESSION="${1:-intraday}"
SCRIPTS=/home/ubuntu/.picoclaw/scripts

echo "$(date): [intraday-scan ${SESSION}] starting" >> "$LOG"

# ── Market calendar guard ─────────────────────────────────────────────────
CALENDAR_CHECK=$($PYTHON -c "
import sys
sys.path.insert(0, '$SCRIPTS')
from apex_market_calendar import should_scan_today
ok, reason = should_scan_today()
print('OK' if ok else f'SKIP:{reason}')
" 2>/dev/null || echo "SKIP:calendar_error")

if echo "$CALENDAR_CHECK" | grep -q "SKIP"; then
    REASON=$(echo "$CALENDAR_CHECK" | sed 's/SKIP://')
    echo "$(date): [intraday-scan ${SESSION}] Market closed — $REASON — skipping" >> "$LOG"
    exit 0
fi

# ── Market hours guard (08:00–15:00 UTC) ─────────────────────────────────
HOUR=$(date -u +%H)
MIN=$(date -u +%M)
HOUR_MIN=$((10#$HOUR * 60 + 10#$MIN))
if [ "$HOUR_MIN" -lt 480 ] || [ "$HOUR_MIN" -gt 900 ]; then
    echo "$(date): [intraday-scan ${SESSION}] Outside 08:00–15:00 UTC — skipping" >> "$LOG"
    exit 0
fi

# ── Fast intraday data refresh ────────────────────────────────────────────
# These scripts are fast (<30s each) and produce fresh price-derived signals.
echo "$(date): [intraday-scan ${SESSION}] refreshing intraday intelligence" >> "$LOG"

$PYTHON "$SCRIPTS/apex-regime-check.py"       >> "$LOG" 2>&1 || true
$PYTHON "$SCRIPTS/apex-regime-scaling.py"     >> "$LOG" 2>&1 || true
$PYTHON "$SCRIPTS/apex-market-direction.py"   >> "$LOG" 2>&1 || true
$PYTHON "$SCRIPTS/apex-contrarian-scan.py"    >> "$LOG" 2>&1 || true
$PYTHON "$SCRIPTS/apex-inverse-scanner.py"    >> "$LOG" 2>&1 || true
$PYTHON "$SCRIPTS/apex-blackswan-test.py" quick >> "$LOG" 2>&1 || true

echo "$(date): [intraday-scan ${SESSION}] intelligence refresh done — running decision engine" >> "$LOG"

# ── Decision engine ───────────────────────────────────────────────────────
$PYTHON "$SCRIPTS/apex-decision-engine.py" "--session=${SESSION}" >> "$LOG" 2>&1

echo "$(date): [intraday-scan ${SESSION}] complete" >> "$LOG"
