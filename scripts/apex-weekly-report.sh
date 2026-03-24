#!/bin/bash

source /home/ubuntu/.picoclaw/.env.trading212
BOT_TOKEN="${APEX_BOT_TOKEN}"
CHAT_ID="${APEX_CHAT_ID}"
LOG="/home/ubuntu/.picoclaw/logs/apex-cron.log"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

echo "$(date): Running weekly report" >> "$LOG"
RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-weekly-report.py 2>/dev/null)
send_message "$RESULT"
echo "$(date): Weekly report sent" >> "$LOG"
