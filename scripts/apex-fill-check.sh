#!/bin/bash

BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"
LOG="/home/ubuntu/.picoclaw/logs/apex-cron.log"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    --data-urlencode "text=$1"
}

echo "$(date): Running fill check" >> "$LOG"

source /home/ubuntu/.picoclaw/.env.trading212

# Get all open orders
ORDERS=$(curl -s -H "Authorization: Basic $T212_AUTH" \
  https://demo.trading212.com/api/v0/equity/orders)

RESULT=$(python3 << PYEOF
import json

try:
    orders = json.loads("""$ORDERS""")
except:
    orders = []

if not orders:
    print("NONE")
    exit(0)

# Filter for DAY limit orders that are still NEW/unconfirmed
unfilled = []
for o in orders:
    order_type   = o.get('type', '')
    status       = o.get('status', '')
    side         = o.get('side', '')
    ticker       = o.get('ticker', '')
    limit_price  = o.get('limitPrice', '')
    time_in_force = o.get('timeInForce', '')
    order_id     = o.get('id', '')

    # DAY limit BUY orders still open at 15:30 = unfilled
    if (order_type == 'LIMIT' and
        side == 'BUY' and
        status in ['NEW', 'UNCONFIRMED'] and
        time_in_force == 'DAY'):
        unfilled.append({
            'id':      order_id,
            'ticker':  ticker,
            'price':   limit_price,
            'status':  status
        })

if unfilled:
    lines = [f"⚠️ UNFILLED ORDERS — {len(unfilled)} limit orders expiring today"]
    for o in unfilled:
        lines.append(f"  {o['ticker']} | limit: £{o['price']} | ID: {o['id']}")
    lines.append("\nThese orders will expire at market close.")
    lines.append("Options:")
    lines.append("  • Let them expire — no position taken")
    lines.append("  • Place market order instead if still want to enter")
    lines.append("  • Tomorrow's scan will regenerate if signal still valid")
    print("UNFILLED\n" + "\n".join(lines))
else:
    print("NONE")
PYEOF
)

if echo "$RESULT" | grep -q "^UNFILLED"; then
    MSG=$(echo "$RESULT" | tail -n +2)
    send_message "$MSG"
    echo "$(date): Unfilled orders alert sent" >> "$LOG"
else
    echo "$(date): All orders filled or no open orders" >> "$LOG"
fi
