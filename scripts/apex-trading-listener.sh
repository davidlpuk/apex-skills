#!/bin/bash

source /home/ubuntu/.picoclaw/.env.trading212

BOT_TOKEN="$APEX_BOT_TOKEN"
CHAT_ID="${APEX_CHAT_ID}"
OFFSET_FILE="/home/ubuntu/.picoclaw/logs/apex-trading-offset.txt"
LOG="/home/ubuntu/.picoclaw/logs/apex-trading-listener.log"
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

get_pnl() {
  PORTFOLIO=$(curl -s -H "Authorization: Basic $T212_AUTH" \
    $T212_ENDPOINT/equity/portfolio)
  CASH=$(curl -s -H "Authorization: Basic $T212_AUTH" \
    $T212_ENDPOINT/equity/account/cash)
  MSG=$(python3 << PYEOF
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
        lines.append("  No open positions")
except:
    pass
try:
    d = json.loads("""$CASH""")
    free     = float(d.get("free", 0))
    invested = float(d.get("invested", 0))
    total    = round(free + invested, 2)
    if total > 0:
        lines.append(f"\n💼 Portfolio: £{total} | Cash: £{round(free,2)} | Invested: £{round(invested,2)}")
    else:
        raise ValueError("zero total")
except:
    try:
        import json as _j
        c = _j.load(open("/home/ubuntu/.picoclaw/logs/apex-portfolio-cache.json"))
        v = c.get("value")
        if v: lines.append(f"\n💼 Portfolio: £{v} (cached) — live data unavailable")
    except: pass
print("\n".join(lines))
PYEOF
)
  send_message "$MSG"
}

close_position() {
  local ticker="$1"
  local qty="$2"
  local neg_qty=$(echo "$qty * -1" | bc)
  curl -s -X POST \
    -H "Authorization: Basic $T212_AUTH" \
    -H "Content-Type: application/json" \
    -d "{\"ticker\":\"$ticker\",\"quantity\":$neg_qty}" \
    $T212_ENDPOINT/equity/orders/market
}

process_message() {
  local text="$1"
  local text_lower=$(echo "$text" | tr '[:upper:]' '[:lower:]' | xargs)
  local upper=$(echo "$text" | tr '[:lower:]' '[:upper:]' | xargs)
  local cmd=$(echo "$upper" | awk '{print $1}')
  local arg1=$(echo "$upper" | awk '{print $2}')
  local arg2=$(echo "$upper" | awk '{print $3}')

  echo "$(date): $text" >> "$LOG"

  # Natural language P&L
  if echo "$text_lower" | grep -qE "profit|loss|pnl|how much|how am i|portfolio|what.*worth|performance|made today"; then
    get_pnl
    return
  fi

  # Manual buy flow
  if echo "$text_lower" | grep -qE "^buy |^purchase |^get |^i want to buy|^invest in"; then
    python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
      "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null
    return
  fi

  # Conversation flow replies
  if [ -f "/home/ubuntu/.picoclaw/logs/apex-manual-trade-state.json" ]; then
    if echo "$text_lower" | grep -qE "^yes$|^yeah$|^ok$|^sure$|^correct$|^yep$|^confirm$|^no$|^cancel$|^abort$" || \
       echo "$text_lower" | grep -qE "^adjust "; then
      python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
        "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null
      return
    fi
  fi

  case "$cmd" in
    BUY|PURCHASE)
      python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
        "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null
      ;;
    PNL|PROFIT|LOSS)
      get_pnl
      ;;
    CONFIRM)
      if [ "$arg1" = "TACO" ]; then
        # TACO confirmation gate — set confirmed=true in apex-taco-pending.json
        TACO_PENDING="/home/ubuntu/.picoclaw/logs/apex-taco-pending.json"
        if [ -f "$TACO_PENDING" ]; then
          RESULT=$(python3 << 'PYEOF'
import json, sys
path = "/home/ubuntu/.picoclaw/logs/apex-taco-pending.json"
try:
    with open(path) as f:
        data = json.load(f)
    if not data.get("event_id"):
        print("NO_EVENT")
        sys.exit(0)
    if data.get("confirmed"):
        print("ALREADY_CONFIRMED")
        sys.exit(0)
    data["confirmed"] = True
    import tempfile, os
    d = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
        json.dump(data, tf, indent=2)
        tmp = tf.name
    os.replace(tmp, path)
    print(f"CONFIRMED|{data.get('event_id','?')}|{data.get('taco_status','?')}|{data.get('confidence',0):.0%}|{data.get('taco_tranche',1)}")
except Exception as e:
    print(f"ERROR|{e}")
PYEOF
)
          case "$RESULT" in
            CONFIRMED*)
              EID=$(echo "$RESULT" | cut -d'|' -f2)
              TSTAT=$(echo "$RESULT" | cut -d'|' -f3)
              CONF=$(echo "$RESULT" | cut -d'|' -f4)
              TRANCHE=$(echo "$RESULT" | cut -d'|' -f5)
              send_message "🌮 TACO CONFIRMED

Event: $EID
Status: $TSTAT | Confidence: $CONF | Tranche: $TRANCHE

Signal authorised. Autopilot will execute on next 5-min cycle.
Send CANCEL TACO to abort before then."
              ;;
            ALREADY_CONFIRMED*)
              send_message "🌮 TACO already confirmed — awaiting autopilot execution."
              ;;
            NO_EVENT*)
              send_message "⚠️ No active TACO event to confirm."
              ;;
            ERROR*)
              send_message "❌ TACO confirm error: $RESULT"
              ;;
          esac
        else
          send_message "⚠️ No TACO pending file found. Is the monitor running?"
        fi
      elif [ -f "$SIGNAL_FILE" ]; then
        send_message "⏳ Placing order..."
        /home/ubuntu/.picoclaw/scripts/apex-execute-order.sh
      else
        send_message "⚠️ No pending signal."
      fi
      ;;
    REJECT|CANCEL)
      if [ "$arg1" = "TACO" ]; then
        # Cancel a pending TACO signal before autopilot executes
        TACO_PENDING="/home/ubuntu/.picoclaw/logs/apex-taco-pending.json"
        rm -f "$TACO_PENDING"
        rm -f "$SIGNAL_FILE"
        send_message "🌮 TACO CANCELLED — signal cleared. Monitor returns to ARMED state."
      else
        rm -f "$SIGNAL_FILE"
        rm -f /home/ubuntu/.picoclaw/logs/apex-manual-trade-state.json
        send_message "❌ Cancelled."
      fi
      ;;
    ADJUST)
      if [ -f "/home/ubuntu/.picoclaw/logs/apex-manual-trade-state.json" ]; then
        python3 /home/ubuntu/.picoclaw/scripts/apex-manual-trade.py \
          "$text" "$BOT_TOKEN" "$CHAT_ID" 2>/dev/null
      else
        /home/ubuntu/.picoclaw/scripts/apex-adjust-signal.sh "$arg1" "$arg2"
      fi
      ;;
    CLOSE)
      if [ -n "$arg1" ]; then
        send_message "⏳ Closing $arg1..."
        QTY=$(python3 -c "
import json
with open('$POSITIONS_FILE') as f:
    p = json.load(f)
pos = next((x for x in p if x.get('t212_ticker','').upper() == '$arg1'.upper()), None)
print(pos['quantity'] if pos else 0)
" 2>/dev/null)
        if [ "$QTY" != "0" ] && [ -n "$QTY" ]; then
          RESULT=$(close_position "$arg1" "$QTY")
          ORDER_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))" 2>/dev/null)
          if [ "$ORDER_ID" != "ERROR" ]; then
            send_message "✅ Closed $arg1 | Order: $ORDER_ID"
          else
            send_message "❌ Close failed."
          fi
        else
          send_message "⚠️ Position $arg1 not found."
        fi
      fi
      ;;
    TRIM)
      if [ -n "$arg1" ]; then
        QTY=$(python3 -c "
import json, math
with open('$POSITIONS_FILE') as f:
    p = json.load(f)
pos = next((x for x in p if x.get('t212_ticker','').upper() == '$arg1'.upper()), None)
print(math.floor(pos['quantity'] / 2) if pos else 0)
" 2>/dev/null)
        if [ "$QTY" != "0" ] && [ -n "$QTY" ]; then
          RESULT=$(close_position "$arg1" "$QTY")
          ORDER_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','ERROR'))" 2>/dev/null)
          [ "$ORDER_ID" != "ERROR" ] && send_message "✅ Trimmed $arg1 — sold $QTY shares" || send_message "❌ Trim failed."
        fi
      fi
      ;;
    AUTOPILOT)
      case "$arg1" in
        ON)     python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py on ;;
        OFF)    python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py off ;;
        STATUS) RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py status)
                send_message "🤖 AUTOPILOT STATUS\n\n$RESULT" ;;
      esac
      ;;
    PANIC)
      echo "true" > /home/ubuntu/.picoclaw/logs/apex-paused.flag
      echo "PANIC_$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /home/ubuntu/.picoclaw/logs/apex-panic.flag
      PANIC_VAL=$(curl -s -H "Authorization: Basic $T212_AUTH" \
        $T212_ENDPOINT/equity/account/cash | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(f'£{round(float(d.get(\"free\",0))+float(d.get(\"invested\",0)),2)}')" 2>/dev/null || echo "unknown")
      PANIC_POS=$(python3 -c "
import json
try:
    pos = json.load(open('/home/ubuntu/.picoclaw/logs/apex-positions.json'))
    print(f'{len(pos)} open positions')
except:
    print('unknown positions')
" 2>/dev/null)
      send_message "🚨 PANIC MODE ACTIVATED

All trading HALTED immediately.
Portfolio: $PANIC_VAL
$PANIC_POS

System is paused. No new entries will be placed.
Existing positions remain open (manual action required to close).

To resume: send PANIC OFF
To close a position: send CLOSE [ticker]"
      ;;
    "PANIC OFF")
      rm -f /home/ubuntu/.picoclaw/logs/apex-paused.flag
      rm -f /home/ubuntu/.picoclaw/logs/apex-panic.flag
      send_message "✅ PANIC MODE CLEARED — trading restored. Monitor closely."
      ;;
    PAUSE)
      echo "true" > /home/ubuntu/.picoclaw/logs/apex-paused.flag
      send_message "⏸️ APEX PAUSED — all trading suspended. Type RESUME to restart."
      ;;
    RESUME)
      rm -f /home/ubuntu/.picoclaw/logs/apex-paused.flag
      send_message "▶️ APEX RESUMED — trading restored."
      ;;
    STATUS)
      PENDING=$([ -f "$SIGNAL_FILE" ] && \
        python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(f\"{d['name']} | entry:£{d['entry']} | stop:£{d['stop']}\")" \
        2>/dev/null || echo "none")
      AP=$(python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py status 2>/dev/null | head -1)
      CASH_VAL=$(curl -s -H "Authorization: Basic $T212_AUTH" \
        $T212_ENDPOINT/equity/account/cash | \
        python3 -c "
import sys, json
CACHE='/home/ubuntu/.picoclaw/logs/apex-portfolio-cache.json'
result = None
try:
    d = json.load(sys.stdin)
    total = round(float(d.get('free',0)) + float(d.get('invested',0)), 2)
    if total > 0:
        result = f'£{total}'
except Exception:
    pass
if not result:
    try:
        c = json.load(open(CACHE))
        v = c.get('value')
        result = f'£{v} (cached)' if v else '£? (unavailable)'
    except Exception:
        result = '£? (unavailable)'
print(result)
" 2>/dev/null)
      send_message "📊 APEX STATUS
Portfolio: $CASH_VAL
$AP
Pending: $PENDING
Uptime: $(uptime -p)"
      ;;
    SCAN)
      send_message "⏳ Running scan..."
      /home/ubuntu/.picoclaw/scripts/apex-morning-scan.sh
      ;;
    TACO)
      # TACO STATUS command
      TACO_STATE="/home/ubuntu/.picoclaw/logs/apex-taco-state.json"
      TACO_MON="/home/ubuntu/.picoclaw/logs/apex-taco-monitor-state.json"
      python3 << 'PYEOF'
import json
from datetime import datetime, timezone
def r(f, d={}):
    try:
        with open(f) as fh: return json.load(fh)
    except: return d
state = r("/home/ubuntu/.picoclaw/logs/apex-taco-state.json")
mon   = r("/home/ubuntu/.picoclaw/logs/apex-taco-monitor-state.json")
out   = r("/home/ubuntu/.picoclaw/logs/apex-taco-outcomes.json")
exp   = state.get("expires_at","")
stale = False
if exp:
    try:
        e = datetime.fromisoformat(exp)
        if e.tzinfo is None: e = e.replace(tzinfo=timezone.utc)
        stale = datetime.now(timezone.utc) > e
    except: stale = True
status = state.get("status","NEUTRAL")
if stale: status = "NEUTRAL (stale)"
lines = [
    "🌮 TACO STATUS",
    f"Classifier: {status}",
    f"Confidence: {state.get('confidence',0):.0%}",
    f"VIX spike:  {state.get('vix_spike_pct',0):+.1f}%",
    f"Monitor:    {mon.get('state','NEUTRAL')}",
    f"Event ID:   {mon.get('event_id') or 'none'}",
    f"",
    f"30d trades: {out.get('count_30d',0)} | Win: {out.get('win_rate',0):.0%}",
    f"Exhausted:  {out.get('exhausted',False)}",
]
print("\n".join(lines))
PYEOF
      ;;
    HELP)
      send_message "🤖 APEX TRADING BOT

📈 BUYING
  buy visa          — start manual trade
  buy apple         — buy any instrument
  yes               — confirm instrument
  confirm           — place order
  cancel            — abort

📊 PORTFOLIO
  PNL               — profit & loss
  STATUS            — full status
  CLOSE VUAGl_EQ    — close position
  TRIM VUAGl_EQ     — sell 50%

🤖 AUTOPILOT
  AUTOPILOT ON      — autonomous mode
  AUTOPILOT OFF     — manual mode
  PAUSE             — suspend trading
  RESUME            — restart
  PANIC             — emergency halt + portfolio status
  PANIC OFF         — clear panic mode
  SCAN              — run manual scan

🌮 TACO MODULE
  TACO              — TACO regime status
  CONFIRM TACO      — authorise TACO signal
  CANCEL TACO       — abort TACO signal

Just type naturally — 'what is my profit' works too."
      ;;
    *)
      # Unknown command — show help hint
      send_message "🤖 Type HELP for commands or just ask naturally:
  'buy visa'
  'what is my profit'
  'close my XOM position'"
      ;;
  esac
}

# Start listener
echo "$(date): Apex Trading Bot started" >> "$LOG"
# Only send welcome if first start today
TODAY=$(date +%Y-%m-%d)
LAST_START=$(cat /home/ubuntu/.picoclaw/logs/apex-bot-last-start 2>/dev/null || echo "")
if [ "$LAST_START" != "$TODAY" ]; then
  echo "$TODAY" > /home/ubuntu/.picoclaw/logs/apex-bot-last-start
  send_message "🤖 APEX TRADING BOT ONLINE

I'm your dedicated trading interface.
Type HELP for commands or just say what you want.

Examples:
  buy visa
  what is my profit
  STATUS"
fi

while true; do
  OFFSET=$(get_offset)
  RESPONSE=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?offset=${OFFSET}&timeout=30")

  UPDATES=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
updates = data.get('result', [])
for u in updates:
    msg = u.get('message', {})
    text = msg.get('text', '')
    uid  = u.get('update_id', 0)
    cid  = str(msg.get('chat', {}).get('id', ''))
    if text and cid:
        safe_text = text.replace('|', '_')
        print(f'{uid}|||{cid}|||{safe_text}')
" 2>/dev/null)

  if [ -n "$UPDATES" ]; then
    LAST_ID=0
    while IFS= read -r line; do
      UPDATE_ID=$(echo "$line" | cut -d'|' -f1)
      CHAT_ID=$(echo "$line" | cut -d'|' -f4)
      TEXT=$(echo "$line" | sed 's/^[^|]*|||[^|]*|||//')
      echo "$(date): Processing — chat:$CHAT_ID text:$TEXT" >> "$LOG"
      process_message "$TEXT"
      LAST_ID=$UPDATE_ID
    done <<< "$UPDATES"
    [ "$LAST_ID" -gt 0 ] && save_offset $((LAST_ID + 1))
  fi
  sleep 2
done
