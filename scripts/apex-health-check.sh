#!/bin/bash

source /home/ubuntu/.picoclaw/scripts/apex-telegram.sh
LOG="/home/ubuntu/.picoclaw/logs/apex-health.log"
LOGS_DIR="/home/ubuntu/.picoclaw/logs"
PYTHON=/home/ubuntu/bin/python3

# Email fallback — activate by adding APEX_ALERT_EMAIL=you@example.com to .env.trading212
source /home/ubuntu/.picoclaw/.env.trading212 2>/dev/null
APEX_ALERT_EMAIL="${APEX_ALERT_EMAIL:-}"

send_email_fallback() {
  local subject="$1"
  local body="$2"
  if [ -z "$APEX_ALERT_EMAIL" ]; then
    echo "$(date): Email fallback skipped — APEX_ALERT_EMAIL not set in .env.trading212" >> "$LOG"
    return
  fi
  if ! command -v mail &>/dev/null; then
    echo "$(date): Email fallback skipped — mail command not found (install mailutils)" >> "$LOG"
    return
  fi
  echo "$body" | mail -s "$subject" "$APEX_ALERT_EMAIL" 2>/dev/null \
    && echo "$(date): Email alert sent to $APEX_ALERT_EMAIL" >> "$LOG" \
    || echo "$(date): Email alert FAILED to send to $APEX_ALERT_EMAIL" >> "$LOG"
}

echo "$(date): Running health check" >> "$LOG"

ISSUES=()
WARNINGS=()
RECOVERED=()

# 0 — Check for STOP_MISSING flags (critical: unprotected positions)
for flag in "$LOGS_DIR"/STOP_MISSING_*; do
  if [ -f "$flag" ]; then
    TICKER_NAME=$(basename "$flag" | sed 's/STOP_MISSING_//')
    ISSUES+=("🚨 STOP MISSING: $TICKER_NAME — stop loss placement failed, entry was closed for safety. Delete flag: $flag")
  fi
done

# 1 — Check systemd services (with self-healing)
for service in picoclaw apex-listener apex-trading-bot apex-dashboard; do
  STATUS=$(systemctl is-active "$service" 2>/dev/null)
  if [ "$STATUS" != "active" ]; then
    echo "$(date): Attempting restart of $service (was: $STATUS)" >> "$LOG"
    sudo systemctl start "$service" 2>/dev/null
    sleep 3
    NEW_STATUS=$(systemctl is-active "$service" 2>/dev/null)
    if [ "$NEW_STATUS" == "active" ]; then
      RECOVERED+=("♻️ $service: auto-restarted successfully")
    else
      ISSUES+=("❌ $service: $STATUS (restart failed — still $NEW_STATUS)")
    fi
  fi
done

# 2 — Check log files are updating (within last 26 hours)
NOW=$(date +%s)
for logfile in apex-cron.log apex-hitl.log apex-trading-listener.log; do
  FILEPATH="$LOGS_DIR/$logfile"
  if [ -f "$FILEPATH" ]; then
    MODIFIED=$(stat -c %Y "$FILEPATH" 2>/dev/null || echo 0)
    AGE=$(( (NOW - MODIFIED) / 3600 ))
    if [ "$AGE" -gt 26 ]; then
      WARNINGS+=("⚠️ $logfile not updated in ${AGE}h")
    fi
  else
    WARNINGS+=("⚠️ $logfile missing")
  fi
done

# 3 — Check T212 API connectivity
source /home/ubuntu/.picoclaw/.env.trading212
T212_RESPONSE=$(curl -s --max-time 10 \
  -H "Authorization: Basic $T212_AUTH" \
  "$T212_ENDPOINT/equity/account/cash" | \
  $PYTHON -c "import sys,json; d=json.load(sys.stdin); print('OK' if 'free' in d else 'FAIL')" 2>/dev/null)

if [ "$T212_RESPONSE" != "OK" ]; then
  ISSUES+=("❌ T212 API: unreachable")
fi

# 4 — Check Alpaca API
ALPACA_KEY=$(grep ALPACA_API_KEY /home/ubuntu/.picoclaw/.env.trading212 | cut -d= -f2)
ALPACA_SECRET=$(grep ALPACA_SECRET /home/ubuntu/.picoclaw/.env.trading212 | cut -d= -f2)
ALPACA_RESPONSE=$(curl -s --max-time 10 \
  -H "APCA-API-KEY-ID: $ALPACA_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET" \
  "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest" | \
  $PYTHON -c "import sys,json; d=json.load(sys.stdin); print('OK' if 'quote' in d else 'FAIL')" 2>/dev/null)

if [ "$ALPACA_RESPONSE" != "OK" ]; then
  WARNINGS+=("⚠️ Alpaca API: degraded — falling back to yfinance")
fi

# 5 — Check disk space
DISK_FREE=$(df /home/ubuntu --output=avail -BG | tail -1 | tr -d 'G ')
if [ "$DISK_FREE" -lt 2 ]; then
  ISSUES+=("❌ Disk space critical: ${DISK_FREE}GB free")
elif [ "$DISK_FREE" -lt 5 ]; then
  WARNINGS+=("⚠️ Disk space low: ${DISK_FREE}GB free")
fi

# 6 — Check VM memory (available RAM)
MEM_FREE=$(free -m | awk 'NR==2{print $7}')
SWAP_USED=$(free -m | awk 'NR==3{print $3}')
SWAP_TOTAL=$(free -m | awk 'NR==3{print $2}')
if [ "$MEM_FREE" -lt 100 ]; then
  ISSUES+=("❌ Memory critical: ${MEM_FREE}MB available (swap: ${SWAP_USED}/${SWAP_TOTAL}MB)")
elif [ "$MEM_FREE" -lt 250 ]; then
  WARNINGS+=("⚠️ Memory low: ${MEM_FREE}MB available (swap: ${SWAP_USED}/${SWAP_TOTAL}MB)")
fi

# 7 — Check pending signal age
if [ -f "$LOGS_DIR/apex-pending-signal.json" ]; then
  SIGNAL_AGE=$($PYTHON -c "
import json
try:
    with open('$LOGS_DIR/apex-pending-signal.json') as f:
        d = json.load(f)
    gen = d.get('generated_at','')
    if gen:
        from datetime import datetime, timezone
        dt  = datetime.fromisoformat(gen)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        print(round(age,1))
    else:
        print(0)
except:
    print(0)
" 2>/dev/null)
  if [ -n "$SIGNAL_AGE" ] && (( $(echo "$SIGNAL_AGE > 12" | bc -l) )); then
    WARNINGS+=("⚠️ Stale pending signal: ${SIGNAL_AGE}h old")
  fi
fi

# 8 — Check autopilot state
AP_STATUS=$($PYTHON -c "
import json
try:
    with open('$LOGS_DIR/apex-autopilot.json') as f:
        d = json.load(f)
    print('ON' if d.get('enabled') else 'OFF')
except:
    print('UNKNOWN')
" 2>/dev/null)

# 9 — Check open positions vs T212
# Only flag a mismatch if T212 API is reachable (T212_RESPONSE=OK above).
# An API error returns a non-list dict which previously printed 0 and caused
# false "tracking 4 but T212 has 0" alerts.
TRACKED=$($PYTHON -c "
import json
try:
    with open('$LOGS_DIR/apex-positions.json') as f:
        p = json.load(f)
    # Exclude awaiting_fill — not yet confirmed in T212
    confirmed = [x for x in p if x.get('status') != 'awaiting_fill']
    print(len(confirmed))
except:
    print(0)
" 2>/dev/null)

T212_POSITIONS=""
if [ "$T212_RESPONSE" == "OK" ]; then
  T212_POSITIONS=$(curl -s --max-time 10 \
    -H "Authorization: Basic $T212_AUTH" \
    "$T212_ENDPOINT/equity/portfolio" | \
    $PYTHON -c "
import sys, json
try:
    p = json.load(sys.stdin)
    # Only print count if response is actually a list (valid portfolio)
    if isinstance(p, list):
        print(len(p))
    # else: print nothing — non-list means API error/empty, skip check
except:
    pass
" 2>/dev/null)
fi

if [ -n "$T212_POSITIONS" ] && [ "$TRACKED" != "$T212_POSITIONS" ]; then
  WARNINGS+=("⚠️ Position mismatch: tracking $TRACKED but T212 has $T212_POSITIONS")
fi

# 10 — Check error log — last 24h only
ERROR_COUNT=0
RECENT_ERRORS=""
ERROR_LOG="$LOGS_DIR/apex-errors.log"

if [ -f "$ERROR_LOG" ]; then
  YESTERDAY_DATE=$(date -d "24 hours ago" "+%Y-%m-%d")
  ERROR_COUNT=$(awk -v cutoff="$YESTERDAY_DATE" '$1 >= cutoff && /\| ERROR \|/' "$ERROR_LOG" | wc -l)
  RECENT_ERRORS=$(awk -v cutoff="$YESTERDAY_DATE" '$1 >= cutoff && /\| ERROR \|/' "$ERROR_LOG" | tail -3 | sed 's/^/    /' || echo "")

  if [ "$ERROR_COUNT" -gt 50 ]; then
    ISSUES+=("❌ $ERROR_COUNT errors in last 24h — check apex-errors.log")
  elif [ "$ERROR_COUNT" -gt 10 ]; then
    WARNINGS+=("⚠️ $ERROR_COUNT errors in last 24h — check apex-errors.log")
  fi
fi

# 11 — Check for UNPROTECTED positions in broker watchdog (critical — open risk)
UNPROTECTED=$($PYTHON -c "
import json
try:
    with open('$LOGS_DIR/apex-broker-watchdog.json') as f:
        d = json.load(f)
    alerts = [a for a in d.get('alerts', []) if 'UNPROTECTED' in str(a)]
    if alerts:
        print('\n'.join(['🚨 ' + str(a) for a in alerts]))
except:
    pass
" 2>/dev/null)

if [ -n "$UNPROTECTED" ]; then
  while IFS= read -r line; do
    ISSUES+=("$line")
  done <<< "$UNPROTECTED"
fi

# 12 — Check critical intelligence files for staleness
declare -A STALE_MAX=( ["apex-regime.json"]=26 ["apex-multiframe.json"]=26 ["apex-breadth-thrust.json"]=26 ["apex-relative-strength.json"]=26 )
for datafile in "${!STALE_MAX[@]}"; do
  FILEPATH="$LOGS_DIR/$datafile"
  MAX_AGE="${STALE_MAX[$datafile]}"
  if [ -f "$FILEPATH" ]; then
    MODIFIED=$(stat -c %Y "$FILEPATH" 2>/dev/null || echo 0)
    AGE_H=$(( (NOW - MODIFIED) / 3600 ))
    if [ "$AGE_H" -gt "$MAX_AGE" ]; then
      WARNINGS+=("⚠️ Stale intelligence: $datafile is ${AGE_H}h old (max ${MAX_AGE}h)")
    fi
  fi
done

# 13 — Check Python dependency health using the correct Python environment
MISSING_MODULES=$($PYTHON -c "
modules = ['yfinance', 'pandas', 'numpy', 'requests']
missing = []
for m in modules:
    try: __import__(m)
    except ImportError: missing.append(m)
print(','.join(missing))
" 2>/dev/null)

if [ -n "$MISSING_MODULES" ]; then
  ISSUES+=("❌ Missing Python modules: $MISSING_MODULES")
fi

# 14 — Uptime
UPTIME=$(uptime -p)

# ─── Build Telegram message ──────────────────────────────────────────────────

RECOVERY_STR=""
if [ ${#RECOVERED[@]} -gt 0 ]; then
  RECOVERY_STR=$(printf '\n%s' "${RECOVERED[@]}")
fi

if [ ${#ISSUES[@]} -gt 0 ]; then
  ISSUE_STR=$(printf '%s\n' "${ISSUES[@]}")
  WARN_STR=""
  if [ ${#WARNINGS[@]} -gt 0 ]; then
    WARN_STR=$(printf '\n%s' "${WARNINGS[@]}")
  fi

  send_message "🚨 APEX HEALTH ALERT

Critical issues detected:
$ISSUE_STR
$WARN_STR$RECOVERY_STR

Autopilot: $AP_STATUS
Uptime: $UPTIME

Immediate attention required."

  send_email_fallback \
    "APEX CRITICAL ALERT — ${#ISSUES[@]} issue(s)" \
    "APEX HEALTH ALERT — $(date)

Critical issues detected:
$ISSUE_STR
$WARN_STR

Autopilot: $AP_STATUS | Uptime: $UPTIME

This is an automated fallback alert from apex-health-check.sh.
Telegram may be unreachable — log in to check the system directly."

  echo "$(date): CRITICAL — ${#ISSUES[@]} issues, ${#WARNINGS[@]} warnings" >> "$LOG"

elif [ ${#WARNINGS[@]} -gt 0 ]; then
  WARN_STR=$(printf '%s\n' "${WARNINGS[@]}")

  send_message "⚠️ APEX HEALTH WARNING

$WARN_STR$RECOVERY_STR

All services: ✅ Running
Autopilot: $AP_STATUS
Positions: $TRACKED tracked / ${T212_POSITIONS:-unknown} in T212
Uptime: $UPTIME"

  echo "$(date): WARNING — ${#WARNINGS[@]} warnings" >> "$LOG"

else
  ERROR_NOTE=""
  if [ "$ERROR_COUNT" -gt 0 ]; then
    ERROR_NOTE="
Errors (24h): $ERROR_COUNT"
  fi
  RECOVERY_NOTE=""
  if [ ${#RECOVERED[@]} -gt 0 ]; then
    RECOVERY_NOTE="$RECOVERY_STR"
  fi

  send_message "✅ APEX HEALTH CHECK

All systems operational.$RECOVERY_NOTE

Services: picoclaw ✅ listener ✅ trading-bot ✅ dashboard ✅
APIs: T212 ✅ Alpaca ✅
Autopilot: $AP_STATUS
Positions: $TRACKED tracked / ${T212_POSITIONS:-unknown} in T212
Disk: ${DISK_FREE}GB free | RAM: ${MEM_FREE}MB available
Uptime: $UPTIME${ERROR_NOTE}"

  echo "$(date): OK — all systems healthy" >> "$LOG"
fi
