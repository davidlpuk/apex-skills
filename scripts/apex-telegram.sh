#!/bin/bash
# Shared Telegram messaging for all Apex shell scripts.
# Source this file: source /home/ubuntu/.picoclaw/scripts/apex-telegram.sh

source /home/ubuntu/.picoclaw/.env.trading212

send_telegram() {
  curl -s -X POST "https://api.telegram.org/bot${APEX_BOT_TOKEN}/sendMessage" \
    -d chat_id="${APEX_CHAT_ID}" \
    --data-urlencode "text=$1"
}

# Alias for scripts that use send_message instead of send_telegram
send_message() {
  send_telegram "$1"
}
