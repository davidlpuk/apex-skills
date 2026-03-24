#!/bin/bash

source /home/ubuntu/.picoclaw/.env.trading212
BOT_TOKEN="${APEX_BOT_TOKEN}"
CHAT_ID="${APEX_CHAT_ID}"
POSITIONS_FILE="/home/ubuntu/.picoclaw/logs/apex-positions.json"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

TICKER="$1"
OUTCOME_TYPE="$2"  # CLOSE, TRIM, STOP_HIT, TARGET1_HIT, TARGET2_HIT

source /home/ubuntu/.picoclaw/.env.trading212

# Get current price from T212
PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH" \
  $T212_ENDPOINT/equity/portfolio)

EXIT_PRICE=$(echo "$PORTFOLIO" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
pos = next((p for p in data if p.get('ticker','').upper() == '$TICKER'.upper()), None)
print(pos['currentPrice'] if pos else 0)
" 2>/dev/null)

QTY=$(python3 -c "
import json
with open('$POSITIONS_FILE') as f:
    positions = json.load(f)
pos = next((p for p in positions if p.get('t212_ticker','').upper() == '$TICKER'.upper()), None)
print(pos['quantity'] if pos else 0)
" 2>/dev/null)

if [ "$OUTCOME_TYPE" = "TRIM" ]; then
  SELL_QTY=$(python3 -c "import math; print(math.floor(float('$QTY') / 2))")
else
  SELL_QTY=$QTY
fi

NEG_QTY=$(python3 -c "print(float('$SELL_QTY') * -1)")

# Place close/trim order
RESPONSE=$(curl -s -X POST \
  -H "Authorization: Basic $T212_AUTH" \
  -H "Content-Type: application/json" \
  -d "{\"ticker\":\"$TICKER\",\"quantity\":$NEG_QTY}" \
  $T212_ENDPOINT/equity/orders/market)

ORDER_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERROR'))" 2>/dev/null)

if [ "$ORDER_ID" != "ERROR" ] && [ -n "$ORDER_ID" ]; then
  # Log outcome
  RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-log-outcome.py "$TICKER" "$EXIT_PRICE" "$OUTCOME_TYPE")

  # Update or remove from positions (atomic write)
  python3 << PYEOF
import json, math, sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True

positions = safe_read('$POSITIONS_FILE', [])
if not isinstance(positions, list):
    positions = []

if '$OUTCOME_TYPE' == 'TRIM':
    for p in positions:
        if p.get('t212_ticker','').upper() == '$TICKER'.upper():
            p['quantity'] = p['quantity'] - math.floor(p['quantity'] / 2)
else:
    positions = [p for p in positions if p.get('t212_ticker','').upper() != '$TICKER'.upper()]

atomic_write('$POSITIONS_FILE', positions)
PYEOF

  send_message "✅ $OUTCOME_TYPE EXECUTED
Ticker: $TICKER
Exit price: $EXIT_PRICE
Order ID: $ORDER_ID

$RESULT"
else
  send_message "❌ $OUTCOME_TYPE failed: $RESPONSE"
fi
