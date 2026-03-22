#!/bin/bash

BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"
LOG="/home/ubuntu/.picoclaw/logs/apex-cron.log"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

echo "$(date): Running earnings check" >> "$LOG"

RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-earnings-check.py 2>/dev/null | grep -v "Checking")

send_message "$RESULT"
echo "$(date): Earnings check complete" >> "$LOG"
