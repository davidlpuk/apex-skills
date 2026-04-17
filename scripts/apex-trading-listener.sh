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

  # Natural language sell flow — handles:
  #   "Sell Apple / AAPL"          → prompts for confirmation
  #   "Confirm Apple sell"          → executes immediately
  #   "Confirm sell of AAPL"        → executes immediately
  #   "CONFIRM SELL AAPL_US_EQ"     → executes immediately
  if echo "$text_lower" | grep -qE \
    "^(confirm[[:space:]]+)?(sell|exit)[[:space:]]|^confirm[[:space:]]+.+[[:space:]]+(sell|close|exit)[[:space:]]*$"; then
    IS_CONFIRMED=0
    echo "$text_lower" | grep -qiE "^confirm" && IS_CONFIRMED=1
    SELL_RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-sell-command.py \
      --text "$text" --confirmed "$IS_CONFIRMED" 2>/dev/null)
    SELL_RC=$?
    SELL_MSG=$(echo "$SELL_RESULT" | python3 -c \
      "import sys,json; print(json.load(sys.stdin).get('message','Sell command error'))" 2>/dev/null)
    send_message "${SELL_MSG:-Sell command error}"
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
    SELL|EXIT)
      # SELL <ticker-or-name> or EXIT <ticker-or-name>
      # Passes the full text; the script handles both prompting and execution.
      SELL_RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-sell-command.py \
        --text "$text" --confirmed 0 2>/dev/null)
      SELL_MSG=$(echo "$SELL_RESULT" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('message','Sell command error'))" 2>/dev/null)
      send_message "${SELL_MSG:-Sell command error}"
      ;;
    CONFIRM)
      if [ "$arg1" = "SELL" ]; then
        # CONFIRM SELL <ticker> — rest of text after "CONFIRM SELL" is the stock identifier
        SELL_RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-sell-command.py \
          --text "$text" --confirmed 1 2>/dev/null)
        SELL_MSG=$(echo "$SELL_RESULT" | python3 -c \
          "import sys,json; print(json.load(sys.stdin).get('message','Sell command error'))" 2>/dev/null)
        send_message "${SELL_MSG:-Sell command error}"
      elif [ "$arg1" = "TACO" ]; then
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
      # Sync positions with T212 first so STATUS reflects manual trades/closes
      python3 /home/ubuntu/.picoclaw/scripts/apex-reconcile.py >/dev/null 2>&1 &
      RECON_PID=$!
      PENDING=$([ -f "$SIGNAL_FILE" ] && \
        python3 -c "import json; d=json.load(open('$SIGNAL_FILE')); print(f\"{d['name']} | entry:£{d['entry']} | stop:£{d['stop']}\")" \
        2>/dev/null || echo "none")
      AP=$(python3 /home/ubuntu/.picoclaw/scripts/apex-autopilot.py status 2>/dev/null | head -1)
      wait $RECON_PID 2>/dev/null || true
      CASH_VAL=$(curl -s --max-time 8 -H "Authorization: Basic $T212_AUTH" \
        "$T212_ENDPOINT/equity/account/cash" | \
        python3 -c "
import sys, json
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
        pos = json.load(open('/home/ubuntu/.picoclaw/logs/apex-positions.json'))
        invested = sum((p.get('current', p.get('entry',0)) or 0) * (p.get('quantity',0) or 0) for p in pos)
        if invested > 0:
            result = f'£{round(invested,2)} (est.)'
    except Exception:
        pass
print(result or '£? (unavailable)')
" 2>/dev/null)
      POS_SUMMARY=$(python3 -c "
import json
try:
    pos = json.load(open('/home/ubuntu/.picoclaw/logs/apex-positions.json'))
    lines = []
    total_pnl = 0
    for p in pos:
        ppl = p.get('ppl', 0) or 0
        total_pnl += ppl
        icon = '✅' if ppl >= 0 else '🔴'
        lines.append(f\"  {icon} {p.get('name','?')[:18]:18s} | PnL: £{round(ppl,2)}\")
    lines.append(f\"Net P&L: £{round(total_pnl,2)}\")
    print('\n'.join(lines))
except Exception as e:
    print('  Positions unavailable')
" 2>/dev/null)
      send_message "📊 APEX STATUS
Portfolio: $CASH_VAL
$AP
Pending: $PENDING

$POS_SUMMARY

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
    LLM)
      case "$arg1" in
        STATUS|"")
          LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_flags.py status 2>/dev/null || echo "❌ LLM flags unavailable")
          send_message "$LLM_MSG"
          ;;
        ON)
          FLAG=$(echo "${arg2:-all}" | tr '[:upper:]' '[:lower:]')
          LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_flags.py set "$FLAG" true 2>/dev/null || echo "❌ Failed")
          send_message "$LLM_MSG"
          ;;
        OFF)
          FLAG=$(echo "${arg2:-all}" | tr '[:upper:]' '[:lower:]')
          LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_flags.py set "$FLAG" false 2>/dev/null || echo "❌ Failed")
          send_message "$LLM_MSG"
          ;;
        RESET)
          LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_flags.py reset 2>/dev/null || echo "❌ Failed")
          send_message "$LLM_MSG"
          ;;
        PROVIDER)
          PROVIDER=$(echo "${arg2:-}" | tr '[:upper:]' '[:lower:]')
          if [ -z "$PROVIDER" ]; then
            LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_client.py status 2>/dev/null || echo "❌ Failed")
          else
            LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_client.py provider "$PROVIDER" 2>/dev/null || echo "❌ Failed")
          fi
          send_message "$LLM_MSG"
          ;;
        BUDGET)
          LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_cost_tracker.py status 2>/dev/null || echo "❌ Cost tracker unavailable")
          send_message "$LLM_MSG"
          ;;
        AB|ABTEST)
          LLM_MSG=$(python3 /home/ubuntu/.picoclaw/scripts/apex_llm_ab_tracker.py summary 2>/dev/null || echo "❌ A/B tracker unavailable")
          send_message "$LLM_MSG"
          ;;
        BRIEF)
          BRIEF_MSG=$(python3 -c "
import json
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-llm-morning-brief.json') as f:
        b = json.load(f)
    posture = b.get('risk_posture','?')
    icons = {'FULL':'✅','REDUCED':'⚠️','CAUTIOUS':'🟠','DEFENSIVE':'🔴'}
    icon = icons.get(posture,'❓')
    text = b.get('brief_text','')[:400]
    reason = b.get('risk_posture_reason','')[:100]
    ts = b.get('timestamp','')[:16].replace('T',' ')
    print(f'{icon} MORNING BRIEF ({ts})\nPosture: {posture} — {reason}\n\n{text}')
except Exception as e:
    print(f'❌ No morning brief available: {e}')
" 2>/dev/null || echo "❌ Brief unavailable")
          send_message "$BRIEF_MSG"
          ;;
        *)
          send_message "🤖 LLM Commands:

  LLM STATUS               — all flags + provider + budget
  LLM ON <flag>            — enable a module
  LLM OFF <flag>           — disable (fall back to rules)
  LLM RESET                — clear call counters
  LLM PROVIDER anthropic   — switch to Claude thinking
  LLM PROVIDER gemini      — switch to Gemini Pro
  LLM BUDGET               — today's spend vs limit
  LLM AB                   — A/B performance report (7d)
  LLM BRIEF                — today's morning brief

Thinking-tier flags (use Claude/Gemini Pro):
  taco_llm              — geopolitical classifier
  preflight_llm         — pre-entry falling knife filter
  signal_tiebreaker_llm — signal ranking
  morning_brief_llm     — daily strategy brief
  queue_revalidate_llm  — overnight signal check
  drawdown_review_llm   — drawdown cause assessment

Fast-tier flags (Gemini Flash):
  sentiment_llm         — news headline scoring
  exit_timing_llm       — partial exit fraction"
          ;;
      esac
      ;;

    AGENT)
      # Claude Agent control: AGENT ON | OFF | STATUS | CONFIRM | REJECT
      AGENT_FLAG="/home/ubuntu/.picoclaw/logs/apex-agent-enabled.json"
      AGENT_CONFIRM="/home/ubuntu/.picoclaw/logs/apex-agent-pending-confirm.json"
      case "$arg1" in
        ON)
          python3 << 'PYEOF'
import json, tempfile, os, datetime
path = "/home/ubuntu/.picoclaw/logs/apex-agent-enabled.json"
data = {"enabled": True, "changed_by": "telegram", "changed_at": datetime.datetime.utcnow().isoformat() + "Z", "reason": "enabled via Telegram"}
d = os.path.dirname(path)
with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
    json.dump(data, tf, indent=2); tmp = tf.name
os.replace(tmp, path)
print("ENABLED")
PYEOF
          send_message "🤖 AGENT ENABLED

Autonomous analysis will run on schedule.
Shadow mode — analysis is logged and Telegrammed, no trades without AGENT CONFIRM.
Send AGENT OFF to disable."
          ;;
        OFF)
          python3 << 'PYEOF'
import json, tempfile, os, datetime
path = "/home/ubuntu/.picoclaw/logs/apex-agent-enabled.json"
data = {"enabled": False, "changed_by": "telegram", "changed_at": datetime.datetime.utcnow().isoformat() + "Z", "reason": "disabled via Telegram"}
d = os.path.dirname(path)
with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
    json.dump(data, tf, indent=2); tmp = tf.name
os.replace(tmp, path)
print("DISABLED")
PYEOF
          send_message "🤖 AGENT DISABLED — scheduled runs will skip. Send AGENT ON to re-enable."
          ;;
        STATUS)
          AGENT_STATUS=$(python3 << 'PYEOF'
import json, os
from datetime import datetime, timezone

flag_path   = "/home/ubuntu/.picoclaw/logs/apex-agent-enabled.json"
log_path    = "/home/ubuntu/.picoclaw/logs/apex-agent-reasoning.jsonl"

def r(p, d=None):
    try:
        with open(p) as f: return json.load(f)
    except: return d

flag = r(flag_path, {})
enabled = flag.get("enabled", False) if isinstance(flag, dict) else False
changed = flag.get("changed_at", "")[:16].replace("T", " ") if isinstance(flag, dict) else ""
changed_by = flag.get("changed_by", "") if isinstance(flag, dict) else ""

# Last run from reasoning log
last_run = {}
try:
    with open(log_path) as f:
        data = json.load(f)
    if isinstance(data, list) and data:
        last_run = data[-1]
except: pass

# Current signal review
review = r("/home/ubuntu/.picoclaw/logs/apex-agent-review.json", {})

status_icon = "🟢" if enabled else "🔴"
lines = [
    f"🤖 AGENT STATUS",
    f"State:   {status_icon} {'ENABLED' if enabled else 'DISABLED'}",
]
if changed:
    lines.append(f"Changed: {changed} by {changed_by}")
if last_run:
    ts = last_run.get("started","")[:16].replace("T"," ")
    mode = last_run.get("mode","?")
    cost = last_run.get("cost_usd", 0)
    tools = last_run.get("tool_count", 0)
    lines += ["", f"Last run: {mode} @ {ts}", f"  Tools: {tools}  Cost: ${cost:.3f}"]
if review and isinstance(review, dict) and review.get("verdict"):
    v = review.get("verdict","?")
    ho = review.get("human_override") or "none"
    sig_ts = review.get("signal_timestamp","")[:16]
    summary = review.get("reasoning_summary","")[:60]
    lines += ["", f"Signal review: {v} (override: {ho})", f"  {sig_ts} — {summary}"]

print("\n".join(lines))
PYEOF
)
          send_message "$AGENT_STATUS"
          ;;
        CONFIRM)
          # Confirm: applies to BOTH the signal review file and any pending-confirm request.
          # Signal review: overrides agent's NEUTRAL or VETO → autopilot proceeds.
          # Pending confirm: unblocks a waiting request_confirmation() call.
          AGENT_REVIEW="/home/ubuntu/.picoclaw/logs/apex-agent-review.json"
          CONFIRMED_SOMETHING=0
          RESULT=$(python3 << 'PYEOF'
import json, tempfile, os, datetime
msgs = []

# 1. Apply to signal review file if it exists and has no override yet
review_path = "/home/ubuntu/.picoclaw/logs/apex-agent-review.json"
if os.path.exists(review_path):
    try:
        with open(review_path) as f: rv = json.load(f)
        if rv.get("human_override") is None:
            rv["human_override"] = "CONFIRM"
            rv["human_override_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            d = os.path.dirname(review_path)
            with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
                json.dump(rv, tf, indent=2); tmp = tf.name
            os.replace(tmp, review_path)
            sig = rv.get("signal_timestamp","")[:16]
            msgs.append(f"REVIEW_CONFIRMED|{rv.get('verdict','?')} overridden → PROCEED|{sig}")
        else:
            msgs.append(f"REVIEW_ALREADY|{rv.get('human_override','?')}")
    except Exception as e:
        msgs.append(f"REVIEW_ERROR|{e}")

# 2. Apply to pending-confirm file if it exists
confirm_path = "/home/ubuntu/.picoclaw/logs/apex-agent-pending-confirm.json"
if os.path.exists(confirm_path):
    try:
        with open(confirm_path) as f: cv = json.load(f)
        if cv.get("confirmed") is None:
            cv["confirmed"] = True
            d = os.path.dirname(confirm_path)
            with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
                json.dump(cv, tf, indent=2); tmp = tf.name
            os.replace(tmp, confirm_path)
            msgs.append(f"ACTION_CONFIRMED|{cv.get('action_description','?')[:60]}")
    except Exception as e:
        msgs.append(f"ACTION_ERROR|{e}")

print("\n".join(msgs) if msgs else "NOTHING_PENDING")
PYEOF
)
          if echo "$RESULT" | grep -q "CONFIRMED\|REVIEW_CONFIRMED"; then
            MSG=$(echo "$RESULT" | python3 -c "
import sys
lines = sys.stdin.read().strip().split('\n')
out = ['✅ AGENT CONFIRMED']
for l in lines:
    parts = l.split('|')
    if 'REVIEW_CONFIRMED' in l:
        out.append(f'Signal review: {parts[1]}')
    elif 'ACTION_CONFIRMED' in l:
        out.append(f'Action: {parts[1]}')
print('\n'.join(out))
" 2>/dev/null || echo "✅ AGENT CONFIRMED")
            send_message "$MSG"
          elif echo "$RESULT" | grep -q "ALREADY"; then
            send_message "⚠️ Already responded to this request."
          elif [ "$RESULT" = "NOTHING_PENDING" ]; then
            send_message "⚠️ No pending agent confirmation request."
          else
            send_message "❌ Confirm error: $RESULT"
          fi
          ;;
        REJECT)
          # Reject: clears the signal review (sets VETO) and/or the pending-confirm request.
          RESULT=$(python3 << 'PYEOF'
import json, tempfile, os, datetime
msgs = []

# 1. Apply to signal review file
review_path = "/home/ubuntu/.picoclaw/logs/apex-agent-review.json"
if os.path.exists(review_path):
    try:
        with open(review_path) as f: rv = json.load(f)
        if rv.get("human_override") is None:
            rv["human_override"] = "REJECT"
            rv["human_override_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            d = os.path.dirname(review_path)
            with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
                json.dump(rv, tf, indent=2); tmp = tf.name
            os.replace(tmp, review_path)
            msgs.append(f"REVIEW_REJECTED|{rv.get('signal_timestamp','')[:16]}")
        else:
            msgs.append(f"REVIEW_ALREADY|{rv.get('human_override','?')}")
    except Exception as e:
        msgs.append(f"REVIEW_ERROR|{e}")

# 2. Apply to pending-confirm file
confirm_path = "/home/ubuntu/.picoclaw/logs/apex-agent-pending-confirm.json"
if os.path.exists(confirm_path):
    try:
        with open(confirm_path) as f: cv = json.load(f)
        if cv.get("confirmed") is None:
            cv["confirmed"] = False
            d = os.path.dirname(confirm_path)
            with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False, suffix=".tmp") as tf:
                json.dump(cv, tf, indent=2); tmp = tf.name
            os.replace(tmp, confirm_path)
            msgs.append("ACTION_REJECTED")
    except Exception as e:
        msgs.append(f"ACTION_ERROR|{e}")

print("\n".join(msgs) if msgs else "NOTHING_PENDING")
PYEOF
)
          if echo "$RESULT" | grep -q "REJECTED"; then
            send_message "❌ AGENT REJECTED — signal/action blocked. Autopilot will not execute."
          elif [ "$RESULT" = "NOTHING_PENDING" ]; then
            send_message "⚠️ No pending agent request to reject."
          else
            send_message "⚠️ $RESULT"
          fi
          ;;
        *)
          send_message "🤖 AGENT Commands:

  AGENT ON       — enable autonomous analysis
  AGENT OFF      — disable (scheduled runs skip)
  AGENT STATUS   — show enabled state + last run
  AGENT CONFIRM  — approve a pending agent action
  AGENT REJECT   — reject a pending agent action

Agent runs in shadow mode by default — it analyses and messages you,
but never executes trades without AGENT CONFIRM."
          ;;
      esac
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

🤖 LLM / AI
  LLM STATUS           — flags + provider + spend
  LLM ON/OFF <flag>    — toggle module
  LLM PROVIDER <x>     — switch anthropic|gemini
  LLM BUDGET           — today's cost vs limit
  LLM AB               — A/B performance (7d)
  LLM BRIEF            — today's morning brief

🧠 CLAUDE AGENT
  AGENT ON             — enable autonomous analysis
  AGENT OFF            — disable scheduled runs
  AGENT STATUS         — state + last run summary
  AGENT CONFIRM        — approve a pending trade action
  AGENT REJECT         — reject a pending trade action

🤖 AGENT / QUERY
  QUERY regime      — regime + VIX snapshot
  QUERY positions   — open positions + P&L
  QUERY signals     — queue + last EV
  QUERY performance — Sharpe, win rate
  QUERY all         — full system snapshot
  CHAIN risk-snapshot — run risk chain
  TOOLS             — list all agent commands

Just type naturally — 'what is my profit' works too."
      ;;
    QUERY)
      # Agent query interface — QUERY <source>
      # Sources: regime, positions, signals, health, performance, autopilot, learning, schedule, all
      SRC="${arg1:-all}"
      VALID_SOURCES="regime positions signals health performance autopilot learning schedule queue all"
      if ! echo "$VALID_SOURCES" | grep -qw "$SRC"; then
        send_message "❓ Unknown query source: $SRC\n\nValid: regime positions signals health performance autopilot learning schedule queue all\n\nExample: QUERY regime"
      else
        RAW=$(python3 /home/ubuntu/.picoclaw/scripts/apex-query.py "$SRC" 2>&1)
        if echo "$RAW" | python3 -c "import sys,json; json.load(sys.stdin)" >/dev/null 2>&1; then
          SUMMARY=$(echo "$RAW" | python3 -c "
import sys, json
d = json.load(sys.stdin)
src = d.get('source', '$SRC')
lines = ['📊 APEX QUERY: ${SRC^^}', '']

if '$SRC' == 'regime' or 'overall' in d:
    lines += [
        f'Regime: {d.get(\"overall\",\"?\")}',
        f'VIX: {d.get(\"vix\",\"?\")}',
        f'Multiplier: {d.get(\"size_multiplier\",\"?\")}x',
        f'Circuit breaker: {d.get(\"circuit_breaker\",{}).get(\"status\",\"?\") if isinstance(d.get(\"circuit_breaker\"),dict) else d.get(\"circuit_breaker\",\"?\")}',
        f'Block: {d.get(\"block_reason\",\"none\")}',
    ]
elif '$SRC' == 'positions' or 'count' in d:
    lines += [
        f'Open positions: {d.get(\"count\",0)}',
        f'Total P&L: £{d.get(\"ppl\",0)}',
        f'Cash: £{d.get(\"cash\",\"?\")}',
        f'Data age: {d.get(\"age_mins\",\"?\")}m',
    ]
    for p in d.get('positions', [])[:5]:
        lines.append(f'  {p.get(\"name\",\"?\")} | £{p.get(\"ppl\",0)} | {p.get(\"signal_type\",\"?\")}')
elif '$SRC' == 'performance' or 'sharpe_ratio' in d:
    lines += [
        f'Sharpe: {d.get(\"sharpe_ratio\",\"?\")}',
        f'Win rate: {round(float(d.get(\"win_rate\",0))*100,1)}%',
        f'Closed trades: {d.get(\"closed_trades\",0)}',
        f'Total P&L: £{round(d.get(\"total_pnl\",0),2)}',
        f'Drawdown: {d.get(\"drawdown_pct\",\"?\")}%',
    ]
elif '$SRC' == 'signals' or 'queue_count' in d:
    ev = d.get('ev_summary') or {}
    lines += [
        f'Queue: {d.get(\"queue_count\",0)} items',
        f'Last EV: {ev.get(\"last_ev\",\"?\")} ({ev.get(\"verdict\",\"?\")})',
        f'Signal type: {ev.get(\"signal_type\",\"?\")}',
    ]
elif '$SRC' == 'health' or 'circuit_breaker' in d:
    cb = d.get('circuit_breaker',{})
    dd = d.get('drawdown',{})
    lines += [
        f'Circuit breaker: {cb.get(\"status\",\"?\") if isinstance(cb,dict) else cb}',
        f'Drawdown: {dd.get(\"status\",\"?\") if isinstance(dd,dict) else dd} {dd.get(\"drawdown_pct\",\"\") if isinstance(dd,dict) else \"\"}%',
    ]
elif '$SRC' == 'autopilot' or 'enabled' in d:
    lines += [
        f'Enabled: {d.get(\"enabled\",False)}',
        f'Paused: {d.get(\"paused\",False)}',
        f'Total trades: {d.get(\"total_autonomous_trades\",0)}',
        f'Last action: {d.get(\"last_action\",\"none\")}',
    ]
elif '$SRC' == 'all':
    for key in ['regime','positions','signals','health','performance','autopilot']:
        sub = d.get(key, {})
        if isinstance(sub, dict):
            lines.append(f'{key.upper()}: ' + ' | '.join(f'{k}={v}' for k,v in list(sub.items())[:3]))
else:
    lines.append(json.dumps(d, indent=2)[:800])

print('\n'.join(str(l) for l in lines))
" 2>/dev/null || echo "$RAW" | head -c 1000)
          send_message "$SUMMARY"
        else
          send_message "❌ Query failed:\n$RAW"
        fi
      fi
      ;;

    CHAIN)
      # Run a named chain — CHAIN <name>
      CHAIN_NAME="$arg1"
      if [ -z "$CHAIN_NAME" ]; then
        CHAINS=$(python3 -c "
import json
c = json.load(open('/home/ubuntu/.picoclaw/scripts/apex-tool-chains.json'))
for name, chain in c['chains'].items():
    print(f'  {name} — {chain[\"description\"]}')
" 2>/dev/null)
        send_message "🔗 Available chains:\n\n$CHAINS\n\nUsage: CHAIN <name>"
      else
        send_message "🔗 Running chain: $CHAIN_NAME..."
        RESULT=$(python3 /home/ubuntu/.picoclaw/scripts/apex-cron-runner.py "$CHAIN_NAME" 2>&1)
        send_message "🔗 CHAIN $CHAIN_NAME\n\n$RESULT"
      fi
      ;;

    TOOLS)
      # List available query sources and chains
      send_message "🛠 APEX AGENT TOOLS

📊 QUERY <source>
  regime       — VIX, breadth, circuit breaker
  positions    — open positions, P&L
  signals      — queue, last EV signal
  health       — data integrity, drawdown
  performance  — Sharpe, win rate, trades
  autopilot    — enabled status, trade count
  learning     — weights, edge proof
  schedule     — upcoming cron jobs
  all          — full system snapshot

🔗 CHAIN <name>
  morning-health    — data integrity checks
  morning-regime    — regime + risk gates
  signal-pipeline   — macro + signals
  risk-snapshot     — full risk assessment
  learning-cycle    — update weights + edge
  performance-review — EOD analysis
  full-morning      — complete morning prep

Example: QUERY regime
Example: CHAIN risk-snapshot"
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
