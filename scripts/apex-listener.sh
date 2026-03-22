#!/bin/bash

BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\(.*\)".*/\1/')
CHAT_ID="6808823889"
OFFSET_FILE="/home/ubuntu/.picoclaw/logs/telegram-offset.txt"
LOG="/home/ubuntu/.picoclaw/logs/apex-hitl.log"
SIGNAL_FILE="/home/ubuntu/.picoclaw/logs/apex-pending-signal.json"
POSITIONS_FILE="/home/ubuntu/.picoclaw/logs/apex-positions.json"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    --data-urlencode "text=$1"
}

get_offset() {
  [ -f "$OFFSET_FILE" ] && cat "$OFFSET_FILE" || echo "0"
}

save_offset() {
  echo "$1" > "$OFFSET_FILE"
}

close_position() {
  local ticker="$1"
  local qty="$2"
  source /home/ubuntu/.picoclaw/.env.trading212
  local neg_qty=$(echo "$qty * -1" | bc)
  curl -s -X POST \
    -H "Authorization: Basic $T212_AUTH" \
    -H "Content-Type: application/json" \
    -d "{\"ticker\":\"$ticker\",\"quantity\":$neg_qty}" \
    https://demo.trading212.com/api/v0/equity/orders/market
}


get_pnl() {
  source /home/ubuntu/.picoclaw/.env.trading212
  PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH"     https://demo.trading212.com/api/v0/equity/portfolio)
  CASH=$(curl -s -H "Authorization: Basic $T212_AUTH"     https://demo.trading212.com/api/v0/equity/account/cash)
  MSG=$(python3 << PYEOF2
import json
lines = ["💰 PROFIT & LOSS SUMMARY"]
try:
    positions = json.loads("""$PORTFOLIO""")
    if positions:
        total_pnl = 0
        for p in positions:
            ticker  = p.get("ticker","?")
            ppl     = float(p.get("ppl", 0))
            current = p.get("currentPrice", 0)
            qty     = p.get("quantity", 0)
            icon    = "✅" if ppl >= 0 else "🔴"
            total_pnl += ppl
            lines.append(f"  {icon} {ticker} | qty:{qty} | £{current} | PnL: £{round(ppl,2)}")
        total_icon = "✅" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{total_icon} NET PnL: £{round(total_pnl,2)}")
    else:
        lines.append("No open positions")
except:
    pass
try:
    d = json.loads("""$CASH""")
    free     = float(d.get("free", 0))
    invested = float(d.get("invested", 0))
    total    = round(free + invested, 2)
    lines.append(f"\n💼 Portfolio: £{total} | Cash: £{round(free,2)} | Invested: £{round(invested,2)}")
except:
    pass
print("\n".join(lines))
PYEOF2
)
  send_message "$MSG"
}

process_message() {
  local text="$1"
  local upper=$(echo "$text" | tr '[:lower:]' '[:upper:]' | xargs)
  local cmd=$(echo "$upper" | awk '{print $1}')
  local arg1=$(echo "$upper" | awk '{print $2}')
  local arg2=$(echo "$upper" | awk '{print $3}')

  echo "$(date): Received: $text" >> "$LOG"

  # APEX: prefix — bypass PicoClaw, handle directly
  if [[ "${text,,}" == apex:* ]]; then
    python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
      "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null
    return
  fi

  # First — check if it's a manual trade instruction or ongoing conversation
  MANUAL_RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
    "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null)

  if [ "$MANUAL_RESULT" = "HANDLED" ]; then
    return
  fi

  # Natural language P&L detection
  if echo "${text,,}" | grep -qE "profit|loss|pnl|how much|portfolio value|what.*worth|made.*today|gained|lost"; then
    cmd="PNL"
  fi

  # Natural language P&L detection
  text_lower_nl=$(echo "$text" | tr '[:upper:]' '[:lower:]')
  if echo "$text_lower_nl" | grep -qE "profit|loss|pnl|how much|portfolio value|what.*worth|made today|gained|how am i doing|performance"; then
    get_pnl
    return
  fi

  # Standard commands
  case "$cmd" in
    CONFIRM)
      if [ -f "$SIGNAL_FILE" ]; then
        send_message "⏳ Confirmed — placing order now..."
        /home/ubuntu/.picoclaw/scripts/apex-execute-order.sh
      else
        send_message "⚠️ No pending signal. Request a scan or type 'buy INSTRUMENT'."
      fi
      ;;
    REJECT)
      rm -f "$SIGNAL_FILE"
      send_message "❌ REJECTED — Signal discarded."
      ;;
    CANCEL)
      rm -f "$SIGNAL_FILE"
      rm -f /home/ubuntu/.picoclaw/logs/apex-manual-trade-state.json
      send_message "🚫 CANCELLED."
      ;;
    ADJUST)
      if [ -n "$arg1" ] && [ -n "$arg2" ]; then
        # Check if in manual trade flow first
        if [ -f "/home/ubuntu/.picoclaw/logs/apex-manual-trade-state.json" ]; then
          python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
            "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null
        else
          /home/ubuntu/.picoclaw/scripts/apex-adjust-signal.sh "$arg1" "$arg2"
        fi
      else
        /home/ubuntu/.picoclaw/scripts/apex-adjust-signal.sh
      fi
      ;;
    CLOSE)
      if [ -n "$arg1" ]; then
        send_message "⏳ Closing $arg1..."
        QTY=$(python3 -c "
import json
with open('$POSITIONS_FILE') as f:
    positions = json.load(f)
pos = next((p for p in positions if p.get('t212_ticker','').upper() == '$arg1'.upper()), None)
print(pos['quantity'] if pos else 0)
" 2>/dev/null)
        if [ "$QTY" = "0" ] || [ -z "$QTY" ]; then
          send_message "⚠️ Position $arg1 not found."
        else
          RESULT=$(close_position "$arg1" "$QTY")
          ORDER_ID=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERROR'))" 2>/dev/null)
          if [ "$ORDER_ID" != "ERROR" ]; then
            send_message "✅ CLOSED $arg1 | Order: $ORDER_ID"
            python3 -c "
import json
with open('$POSITIONS_FILE') as f:
    p = json.load(f)
p = [x for x in p if x.get('t212_ticker','').upper() != '$arg1'.upper()]
with open('$POSITIONS_FILE','w') as f:
    json.dump(p, f, indent=2)
"
          else
            send_message "❌ Close failed."
          fi
        fi
      else
        send_message "⚠️ Usage: CLOSE VUAGl_EQ"
      fi
      ;;
    TRIM)
      if [ -n "$arg1" ]; then
        QTY=$(python3 -c "
import json, math
with open('$POSITIONS_FILE') as f:
    positions = json.load(f)
pos = next((p for p in positions if p.get('t212_ticker','').upper() == '$arg1'.upper()), None)
print(math.floor(pos['quantity'] / 2) if pos else 0)
" 2>/dev/null)
        if [ "$QTY" = "0" ] || [ -z "$QTY" ]; then
          send_message "⚠️ Position $arg1 not found."
        else
          RESULT=$(close_position "$arg1" "$QTY")
          ORDER_ID=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERROR'))" 2>/dev/null)
          if [ "$ORDER_ID" != "ERROR" ]; then
            send_message "✅ TRIMMED $arg1 — sold $QTY shares | Order: $ORDER_ID"
          else
            send_message "❌ Trim failed."
          fi
        fi
      fi
      ;;
    AUTOPILOT)
      case "$arg1" in
        ON)  python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py on ;;
        OFF) python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py off ;;
    
    PNL|PROFIT)
      source /home/ubuntu/.picoclaw/.env.trading212
      PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH"         https://demo.trading212.com/api/v0/equity/portfolio)
      CASH=$(curl -s -H "Authorization: Basic $T212_AUTH"         https://demo.trading212.com/api/v0/equity/account/cash)
      MSG=$(python3 << PYEOF2
import json
lines = ["💰 PROFIT & LOSS SUMMARY"]
try:
    positions = json.loads("""$PORTFOLIO""")
    if positions:
        total_pnl = 0
        for p in positions:
            ticker  = p.get("ticker","?")
            ppl     = float(p.get("ppl", 0))
            current = p.get("currentPrice", 0)
            qty     = p.get("quantity", 0)
            icon    = "✅" if ppl >= 0 else "🔴"
            total_pnl += ppl
            lines.append(f"  {icon} {ticker} | qty:{qty} | £{current} | PnL: £{round(ppl,2)}")
        total_icon = "✅" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{total_icon} NET PnL: £{round(total_pnl,2)}")
    else:
        lines.append("No open positions")
except:
    lines.append("Could not fetch positions")
try:
    d = json.loads("""$CASH""")
    free     = float(d.get("free", 0))
    invested = float(d.get("invested", 0))
    total    = round(free + invested, 2)
    lines.append(f"\n💼 Portfolio: £{total} | Cash: £{round(free,2)} | Invested: £{round(invested,2)}")
except:
    pass
print("\n".join(lines))
PYEOF2
)
      send_message "$MSG"
      ;;
    PNL)
      get_pnl
      ;;
    QUEUE)
      case "$arg1" in
        CANCEL)
          if [ -n "$arg2" ]; then
            python3 /home/ubuntu/.picoclaw/scripts/apex-trade-queue.py cancel "$arg2"
          else
            send_message "⚠️ Usage: QUEUE CANCEL [ID]"
          fi
          ;;
        EXECUTE)
          send_message "⏳ Executing queued trades..."
          python3 /home/ubuntu/.picoclaw/scripts/apex-trade-queue.py execute
          ;;
        *)
          python3 /home/ubuntu/.picoclaw/scripts/apex-trade-queue.py show
          ;;
      esac
      ;;
    STATUS) RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py status)
                send_message "🤖 AUTOPILOT STATUS\n\n$RESULT" ;;
        *) send_message "Usage: AUTOPILOT ON / OFF / STATUS" ;;
      esac
      ;;
    APEX)
      case "$arg1" in
        PAUSE)  echo "true" > /home/ubuntu/.picoclaw/logs/apex-paused.flag
                send_message "⏸️ APEX PAUSED — all trading suspended. Type APEX RESUME to restart." ;;
        RESUME) rm -f /home/ubuntu/.picoclaw/logs/apex-paused.flag
                send_message "▶️ APEX RESUMED — trading restored." ;;
        *) ;;
      esac
      ;;

    PNL|PROFIT)
      source /home/ubuntu/.picoclaw/.env.trading212
      PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH"         https://demo.trading212.com/api/v0/equity/portfolio)
      CASH=$(curl -s -H "Authorization: Basic $T212_AUTH"         https://demo.trading212.com/api/v0/equity/account/cash)
      MSG=$(python3 << PYEOF2
import json
lines = ["💰 PROFIT & LOSS SUMMARY"]
try:
    positions = json.loads("""$PORTFOLIO""")
    if positions:
        total_pnl = 0
        for p in positions:
            ticker  = p.get("ticker","?")
            ppl     = float(p.get("ppl", 0))
            current = p.get("currentPrice", 0)
            qty     = p.get("quantity", 0)
            icon    = "✅" if ppl >= 0 else "🔴"
            total_pnl += ppl
            lines.append(f"  {icon} {ticker} | qty:{qty} | £{current} | PnL: £{round(ppl,2)}")
        total_icon = "✅" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{total_icon} NET PnL: £{round(total_pnl,2)}")
    else:
        lines.append("No open positions")
except:
    lines.append("Could not fetch positions")
try:
    d = json.loads("""$CASH""")
    free     = float(d.get("free", 0))
    invested = float(d.get("invested", 0))
    total    = round(free + invested, 2)
    lines.append(f"\n💼 Portfolio: £{total} | Cash: £{round(free,2)} | Invested: £{round(invested,2)}")
except:
    pass
print("\n".join(lines))
PYEOF2
)
      send_message "$MSG"
      ;;
    PNL)
      get_pnl
      ;;
    QUEUE)
      case "$arg1" in
        CANCEL)
          if [ -n "$arg2" ]; then
            python3 /home/ubuntu/.picoclaw/scripts/apex-trade-queue.py cancel "$arg2"
          else
            send_message "⚠️ Usage: QUEUE CANCEL [ID]"
          fi
          ;;
        EXECUTE)
          send_message "⏳ Executing queued trades..."
          python3 /home/ubuntu/.picoclaw/scripts/apex-trade-queue.py execute
          ;;
        *)
          python3 /home/ubuntu/.picoclaw/scripts/apex-trade-queue.py show
          ;;
      esac
      ;;
    STATUS)
      PENDING=$([ -f "$SIGNAL_FILE" ] && \
        python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(f\"{d['name']} | entry:£{d['entry']} | stop:£{d['stop']}\")" \
        2>/dev/null || echo "none")
      AUTOPILOT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py status 2>/dev/null | head -1)
      source /home/ubuntu/.picoclaw/.env.trading212
      CASH=$(curl -s -H "Authorization: Basic $T212_AUTH" \
        https://demo.trading212.com/api/v0/equity/account/cash | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(f'£{round(float(d.get(\"free\",0))+float(d.get(\"invested\",0)),2)}')" 2>/dev/null)
      send_message "📊 Apex status
VM uptime: $(uptime -p)
Portfolio: $CASH
$AUTOPILOT
Pending signal: $PENDING
Last action: $(tail -1 $LOG | cut -c1-60)"
      ;;
    HELP)
      send_message "🤖 APEX COMMANDS

📈 TRADING
  buy [instrument]  — start a manual trade
  CONFIRM           — execute pending signal
  REJECT            — discard signal
  CANCEL            — abort anything
  ADJUST STOP 91    — change stop loss
  ADJUST QTY 2      — change quantity
  CLOSE VUAGl_EQ    — close position
  TRIM VUAGl_EQ     — sell 50%

🤖 AUTOPILOT
  AUTOPILOT ON      — autonomous mode
  AUTOPILOT OFF     — manual mode
  AUTOPILOT STATUS  — check state
  APEX PAUSE        — emergency stop
  APEX RESUME       — restart

📊 INFO
  STATUS            — live portfolio
  HELP              — this menu"
      ;;
  esac
}

while true; do
  OFFSET=$(get_offset)
  RESPONSE=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?offset=${OFFSET}&timeout=30")

  COUNT=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
updates = data.get('result', [])
for u in updates:
    msg = u.get('message', {})
    text = msg.get('text', '')
    uid = u.get('update_id', 0)
    chat_id = msg.get('chat', {}).get('id', '')
    if str(chat_id) == '6808823889' and text:
        print(f'{uid}|||{text}')
" 2>/dev/null)

  if [ -n "$COUNT" ]; then
    LAST_ID=0
    while IFS= read -r line; do
      UPDATE_ID=$(echo "$line" | cut -d'|' -f1)
      TEXT=$(echo "$line" | cut -d'|' -f4-)
      process_message "$TEXT"
      LAST_ID=$UPDATE_ID
    done <<< "$COUNT"
    [ "$LAST_ID" -gt 0 ] && save_offset $((LAST_ID + 1))
  fi
  sleep 2
done
