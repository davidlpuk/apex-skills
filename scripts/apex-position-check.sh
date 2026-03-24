#!/bin/bash
source /home/ubuntu/.picoclaw/.env.trading212
BOT_TOKEN="${APEX_BOT_TOKEN}"
CHAT_ID="${APEX_CHAT_ID}"

curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d chat_id="${CHAT_ID}" \
  -d text="Apex, check all open positions against current prices. Flag anything near stop loss or target. No action needed unless I confirm."
