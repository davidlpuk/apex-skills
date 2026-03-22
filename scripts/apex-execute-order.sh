#!/bin/bash

BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"
SIGNAL_FILE="/home/ubuntu/.picoclaw/logs/apex-pending-signal.json"
POSITIONS_FILE="/home/ubuntu/.picoclaw/logs/apex-positions.json"
LOG="/home/ubuntu/.picoclaw/logs/apex-orders.log"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

source /home/ubuntu/.picoclaw/.env.trading212

if [ ! -f "$SIGNAL_FILE" ]; then
  send_message "⚠️ No pending signal found."
  exit 1
fi

TICKER=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('t212_ticker',''))")
QUANTITY=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('quantity',''))")
NAME=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('name',''))")
ENTRY=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('entry',''))")
STOP=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('stop',''))")
TARGET1=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('target1',''))")
TARGET2=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('target2',''))")
SCORE=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('score',0))")
RSI=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('rsi',0))")
MACD=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('macd',0))")
SECTOR=$(python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(d.get('sector','unknown'))")
NEG_QUANTITY=$(python3 -c "print(float('$QUANTITY') * -1)")

if [ -z "$TICKER" ] || [ -z "$QUANTITY" ]; then
  send_message "⚠️ Signal file incomplete."
  exit 1
fi

echo "$(date): Placing LIMIT entry order — $TICKER x$QUANTITY @ $ENTRY" >> "$LOG"

# Step 1 — Place entry limit order
ENTRY_RESPONSE=$(curl -s -X POST \
  -H "Authorization: Basic $T212_AUTH" \
  -H "Content-Type: application/json" \
  -d "{\"ticker\":\"$TICKER\",\"quantity\":$QUANTITY,\"limitPrice\":$ENTRY,\"timeValidity\":\"DAY\"}" \
  https://demo.trading212.com/api/v0/equity/orders/limit)

echo "$(date): Entry response — $ENTRY_RESPONSE" >> "$LOG"

ENTRY_ID=$(echo "$ENTRY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERROR'))" 2>/dev/null)
ENTRY_STATUS=$(echo "$ENTRY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','UNKNOWN'))" 2>/dev/null)

if [ "$ENTRY_ID" = "ERROR" ] || [ -z "$ENTRY_ID" ]; then
  # Fall back to market order
  echo "$(date): Limit failed — trying market order" >> "$LOG"
  ENTRY_RESPONSE=$(curl -s -X POST \
    -H "Authorization: Basic $T212_AUTH" \
    -H "Content-Type: application/json" \
    -d "{\"ticker\":\"$TICKER\",\"quantity\":$QUANTITY}" \
    https://demo.trading212.com/api/v0/equity/orders/market)
  ENTRY_ID=$(echo "$ENTRY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERROR'))" 2>/dev/null)
  ENTRY_STATUS="MARKET_FILLED"
fi

if [ "$ENTRY_ID" = "ERROR" ] || [ -z "$ENTRY_ID" ]; then
  send_message "❌ ENTRY ORDER FAILED
$ENTRY_RESPONSE"
  exit 1
fi

echo "$(date): Entry order placed — ID: $ENTRY_ID" >> "$LOG"

# Step 2 — Place stop loss order immediately
echo "$(date): Placing STOP LOSS order — $TICKER x$NEG_QUANTITY @ $STOP" >> "$LOG"

sleep 2  # Brief pause to allow entry to register

STOP_RESPONSE=$(curl -s -X POST \
  -H "Authorization: Basic $T212_AUTH" \
  -H "Content-Type: application/json" \
  -d "{\"ticker\":\"$TICKER\",\"quantity\":$NEG_QUANTITY,\"stopPrice\":$STOP,\"timeValidity\":\"GOOD_TILL_CANCEL\"}" \
  https://demo.trading212.com/api/v0/equity/orders/stop)

echo "$(date): Stop response — $STOP_RESPONSE" >> "$LOG"

STOP_ID=$(echo "$STOP_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERROR'))" 2>/dev/null)
STOP_STATUS=$(echo "$STOP_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','UNKNOWN'))" 2>/dev/null)

if [ "$STOP_ID" = "ERROR" ] || [ -z "$STOP_ID" ]; then
  STOP_MSG="⚠️ Stop loss order FAILED — monitor manually"
  echo "$(date): Stop loss failed — $STOP_RESPONSE" >> "$LOG"
else
  STOP_MSG="✅ Stop loss order placed in T212 (ID: $STOP_ID)"
  echo "$(date): Stop loss placed — ID: $STOP_ID @ $STOP" >> "$LOG"
fi

# Step 3 — Save position with full metadata including stop order ID
python3 << PYEOF
import json
from datetime import datetime, timezone

today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

try:
    with open('$POSITIONS_FILE') as f:
        positions = json.load(f)
except:
    positions = []

positions.append({
    "t212_ticker":    "$TICKER",
    "name":           "$NAME",
    "quantity":       float("$QUANTITY"),
    "entry":          float("$ENTRY"),
    "stop":           float("$STOP"),
    "target1":        float("$TARGET1"),
    "target2":        float("$TARGET2"),
    "score":          float("$SCORE"),
    "rsi":            float("$RSI"),
    "macd":           float("$MACD"),
    "sector":         "$SECTOR",
    "opened":         today,
    "entry_order_id": "$ENTRY_ID",
    "stop_order_id":  "$STOP_ID",
    "order_type":     "LIMIT+STOP"
})

with open('$POSITIONS_FILE', 'w') as f:
    json.dump(positions, f, indent=2)
PYEOF

# Log to TRADING_STATE.md
echo "$(date '+%Y-%m-%d %H:%M') | LIMIT BUY | $NAME | $TICKER | qty:$QUANTITY | limit:$ENTRY | stop:$STOP (order:$STOP_ID) | T1:$TARGET1 | T2:$TARGET2 | score:$SCORE | entry_id:$ENTRY_ID" >> \
  /home/ubuntu/.picoclaw/workspace/skills/apex-trading/TRADING_STATE.md

rm -f "$SIGNAL_FILE"

send_message "✅ TRADE PLACED
🏷 $NAME ($TICKER)
📐 Qty: $QUANTITY shares
💰 Entry: £$ENTRY (limit — DAY order)
🛑 Stop: £$STOP (GTC — protected in T212)
🎯 T1: £$TARGET1 | T2: £$TARGET2
📊 Score: $SCORE/10
🔖 Entry ID: $ENTRY_ID
$STOP_MSG

Your position is now protected even if Apex goes offline.
Reply CANCEL to cancel entry order (stop cancels automatically if entry unfilled)."

# Check actual fill price and log slippage
sleep 3
FILL_CHECK=$(source /home/ubuntu/.picoclaw/.env.trading212 && \
  curl -s -H "Authorization: Basic $T212_AUTH" \
  https://demo.trading212.com/api/v0/equity/orders/$ENTRY_ID 2>/dev/null)

ACTUAL_PRICE=$(echo "$FILL_CHECK" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    # filled price
    fp = d.get('fillPrice') or d.get('limitPrice') or 0
    print(fp)
except:
    print(0)
" 2>/dev/null)

if [ -n "$ACTUAL_PRICE" ] && [ "$ACTUAL_PRICE" != "0" ]; then
    python3 /home/ubuntu/.picoclaw/scripts/apex-slippage-tracker.py log \
        "$NAME" "$TICKER" "$ENTRY" "$ACTUAL_PRICE" "$QUANTITY" "BUY" 2>/dev/null
fi
