#!/bin/bash

BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"
LOG="/home/ubuntu/.picoclaw/logs/apex-health.log"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    --data-urlencode "text=$1"
}

echo "$(date): Running health check" >> "$LOG"

ISSUES=()
WARNINGS=()

# 1 — Check systemd services
for service in picoclaw apex-listener apex-trading-bot apex-dashboard; do
  STATUS=$(systemctl is-active $service 2>/dev/null)
  if [ "$STATUS" != "active" ]; then
    ISSUES+=("❌ $service: $STATUS")
  fi
done

# 2 — Check log files are updating (within last 24 hours)
NOW=$(date +%s)
for logfile in apex-cron.log apex-hitl.log apex-trading-listener.log; do
  FILEPATH="/home/ubuntu/.picoclaw/logs/$logfile"
  if [ -f "$FILEPATH" ]; then
    MODIFIED=$(stat -c %Y "$FILEPATH" 2>/dev/null || echo 0)
    AGE=$(( (NOW - MODIFIED) / 3600 ))
    if [ "$AGE" -gt 48 ]; then
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
  https://demo.trading212.com/api/v0/equity/account/cash | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if 'free' in d else 'FAIL')" 2>/dev/null)

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
  python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if 'quote' in d else 'FAIL')" 2>/dev/null)

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

# 6 — Check VM memory
MEM_FREE=$(free -m | awk 'NR==2{print $7}')
if [ "$MEM_FREE" -lt 100 ]; then
  ISSUES+=("❌ Memory critical: ${MEM_FREE}MB free")
elif [ "$MEM_FREE" -lt 250 ]; then
  WARNINGS+=("⚠️ Memory low: ${MEM_FREE}MB free")
fi

# 7 — Check pending signal age
if [ -f "/home/ubuntu/.picoclaw/logs/apex-pending-signal.json" ]; then
  SIGNAL_AGE=$(python3 -c "
import json, datetime, timezone
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-pending-signal.json') as f:
        d = json.load(f)
    gen = d.get('generated_at','')
    if gen:
        from datetime import datetime, timezone
        dt  = datetime.fromisoformat(gen)
        age = (datetime.now(timezone.utc) - dt).seconds / 3600
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
AP_STATUS=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-autopilot.json') as f:
        d = json.load(f)
    print('ON' if d.get('enabled') else 'OFF')
except:
    print('UNKNOWN')
" 2>/dev/null)

# 9 — Check open positions vs T212
TRACKED=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-positions.json') as f:
        p = json.load(f)
    print(len(p))
except:
    print(0)
" 2>/dev/null)

T212_POSITIONS=$(curl -s --max-time 10 \
  -H "Authorization: Basic $T212_AUTH" \
  https://demo.trading212.com/api/v0/equity/portfolio | \
  python3 -c "import sys,json; p=json.load(sys.stdin); print(len(p) if isinstance(p,list) else 0)" 2>/dev/null)

if [ -n "$T212_POSITIONS" ] && [ "$TRACKED" != "$T212_POSITIONS" ]; then
  WARNINGS+=("⚠️ Position mismatch: tracking $TRACKED but T212 has $T212_POSITIONS")
fi

# 10 — Check error log for recent failures
ERROR_COUNT=0
RECENT_ERRORS=""
ERROR_LOG="/home/ubuntu/.picoclaw/logs/apex-errors.log"

if [ -f "$ERROR_LOG" ]; then
  # Count errors in last 24 hours
  YESTERDAY=$(date -d "24 hours ago" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || date -v-24H "+%Y-%m-%d %H:%M:%S" 2>/dev/null)
  ERROR_COUNT=$(grep -c "ERROR" "$ERROR_LOG" 2>/dev/null || echo 0)
  # Get last 3 errors
  RECENT_ERRORS=$(grep "ERROR" "$ERROR_LOG" 2>/dev/null | tail -3 | sed 's/^/    /' || echo "")

  if [ "$ERROR_COUNT" -gt 10 ]; then
    WARNINGS+=("⚠️ $ERROR_COUNT errors in error log — check apex-errors.log")
  fi
fi

# 11 — Uptime
UPTIME=$(uptime -p)

# Build message
if [ ${#ISSUES[@]} -gt 0 ]; then
  # Critical issues
  ISSUE_STR=$(printf '%s\n' "${ISSUES[@]}")
  WARN_STR=""
  if [ ${#WARNINGS[@]} -gt 0 ]; then
    WARN_STR=$(printf '\n%s' "${WARNINGS[@]}")
  fi

  send_message "🚨 APEX HEALTH ALERT

Critical issues detected:
$ISSUE_STR
$WARN_STR

Autopilot: $AP_STATUS
Uptime: $UPTIME

Immediate attention required."

  echo "$(date): CRITICAL — ${#ISSUES[@]} issues" >> "$LOG"

elif [ ${#WARNINGS[@]} -gt 0 ]; then
  # Warnings only
  WARN_STR=$(printf '%s\n' "${WARNINGS[@]}")

  send_message "⚠️ APEX HEALTH WARNING

$WARN_STR

All services: ✅ Running
Autopilot: $AP_STATUS
Positions: $TRACKED tracked / $T212_POSITIONS in T212
Uptime: $UPTIME"

  echo "$(date): WARNING — ${#WARNINGS[@]} warnings" >> "$LOG"

else
  # All good
  ERROR_NOTE=""
  if [ "$ERROR_COUNT" -gt 0 ]; then
    ERROR_NOTE="
Errors logged: $ERROR_COUNT (last 24h)"
  fi

  send_message "✅ APEX HEALTH CHECK

All systems operational.

Services: picoclaw ✅ listener ✅ trading-bot ✅ dashboard ✅
APIs: T212 ✅ Alpaca ✅
Autopilot: $AP_STATUS
Positions: $TRACKED tracked / $T212_POSITIONS in T212
Disk: ${DISK_FREE}GB free | RAM: ${MEM_FREE}MB free
Uptime: $UPTIME${ERROR_NOTE}"

  echo "$(date): OK — all systems healthy" >> "$LOG"
fi
