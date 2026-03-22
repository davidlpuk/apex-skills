#!/bin/bash

BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"
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
