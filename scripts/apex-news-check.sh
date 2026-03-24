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

echo "$(date): Running pre-market news check" >> "$LOG"
RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-news-check.py 2>/dev/null)
send_message "$RESULT"
echo "$(date): News check complete" >> "$LOG"
