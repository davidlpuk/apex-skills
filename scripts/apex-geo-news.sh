#!/bin/bash
source /home/ubuntu/.picoclaw/.env.trading212
BOT_TOKEN="${APEX_BOT_TOKEN}"
CHAT_ID="${APEX_CHAT_ID}"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-geo-news.py 2>/dev/null)
OVERALL=$(cat /home/ubuntu/.picoclaw/logs/apex-geo-news.json | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('overall','CLEAR'))")

if [ "$OVERALL" = "ALERT" ]; then
  ALERTS=$(cat /home/ubuntu/.picoclaw/logs/apex-geo-news.json | \
    python3 -c "
import sys, json
d = json.load(sys.stdin)
flags = d.get('energy_flags', [])
lines = [f\"⚠️ {f['title'][:80]}\" for f in flags[:5]]
print('\n'.join(lines))
")
  send_message "🌍 GEO-POLITICAL ALERT

🚨 Energy/geopolitical events detected:
$ALERTS

Apex will block energy sector trades today.
Type APEX PAUSE to suspend all trading.
Type STATUS for portfolio check."
elif [ "$OVERALL" = "WARN" ]; then
  send_message "🌍 Geo news: minor flags — monitoring. No blocks active."
fi
