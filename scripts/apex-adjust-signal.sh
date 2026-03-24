#!/bin/bash

source /home/ubuntu/.picoclaw/.env.trading212
BOT_TOKEN="${APEX_BOT_TOKEN}"
CHAT_ID="${APEX_CHAT_ID}"
SIGNAL_FILE="/home/ubuntu/.picoclaw/logs/apex-pending-signal.json"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1"
}

if [ ! -f "$SIGNAL_FILE" ]; then
  send_message "⚠️ No pending signal to adjust. Request a new scan first."
  exit 1
fi

FIELD="$1"
VALUE="$2"

if [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
  # Show current signal with help
  python3 << PYEOF
import json
with open('$SIGNAL_FILE') as f:
    d = json.load(f)
entry = d.get('entry', 0)
stop  = d.get('stop', 0)
risk  = entry - stop
t1    = d.get('target1', 0)
t2    = d.get('target2', 0)
qty   = d.get('quantity', 0)
print(f"""⚙️ CURRENT SIGNAL — {d.get('name','')}

💰 Entry:  £{entry}
🛑 Stop:   £{stop}  (risk per share: £{round(risk,2)})
🎯 T1:     £{t1}  (R:R 1:{round((t1-entry)/risk,1) if risk>0 else '?'})
🎯 T2:     £{t2}  (R:R 1:{round((t2-entry)/risk,1) if risk>0 else '?'})
📐 Qty:    {qty} shares
⚠️ Risk:   £{round(risk*qty,2)}

To adjust, type:
ADJUST STOP 91.00
ADJUST ENTRY 94.50
ADJUST T1 101.00
ADJUST T2 106.00
ADJUST QTY 3
ADJUST RR 2.0  (recalculates targets at new R:R ratio)""")
PYEOF
  exit 0
fi

# Apply adjustment with validation and recalculation
python3 << PYEOF
import json, sys

with open('$SIGNAL_FILE') as f:
    d = json.load(f)

field  = '$FIELD'.upper()
value  = float('$VALUE')
errors = []

entry  = float(d.get('entry', 0))
stop   = float(d.get('stop', 0))
t1     = float(d.get('target1', 0))
t2     = float(d.get('target2', 0))
qty    = float(d.get('quantity', 1))
name   = d.get('name', '')

if field == 'STOP':
    if value >= entry:
        errors.append(f"Stop (£{value}) must be BELOW entry (£{entry})")
    elif (entry - value) / entry > 0.12:
        errors.append(f"Stop is more than 12% below entry — very wide. Are you sure?")
    else:
        old_stop = stop
        d['stop'] = value
        stop = value
        # Recalculate targets to maintain R:R
        risk = entry - stop
        d['target1'] = round(entry + risk * 1.5, 2)
        d['target2'] = round(entry + risk * 2.5, 2)
        # Recalculate qty for £50 max risk
        new_qty = min(max(1, round(50 / risk, 2)), round(250 / entry, 2))
        d['quantity'] = new_qty
        print(f"✅ Stop updated: £{old_stop} → £{value}")
        print(f"📐 Targets recalculated: T1=£{d['target1']} T2=£{d['target2']}")
        print(f"📐 Qty adjusted: {qty} → {new_qty} shares (£50 max risk)")

elif field == 'ENTRY':
    if value <= stop:
        errors.append(f"Entry (£{value}) must be ABOVE stop (£{stop})")
    else:
        old_entry = entry
        d['entry'] = value
        entry = value
        risk = entry - stop
        d['target1'] = round(entry + risk * 1.5, 2)
        d['target2'] = round(entry + risk * 2.5, 2)
        new_qty = min(max(1, round(50 / risk, 2)), round(250 / entry, 2))
        d['quantity'] = new_qty
        print(f"✅ Entry updated: £{old_entry} → £{value}")
        print(f"📐 Targets recalculated: T1=£{d['target1']} T2=£{d['target2']}")

elif field in ['T1', 'TARGET1']:
    if value <= entry:
        errors.append(f"Target 1 (£{value}) must be ABOVE entry (£{entry})")
    elif value >= t2:
        errors.append(f"Target 1 (£{value}) must be BELOW Target 2 (£{t2})")
    else:
        d['target1'] = value
        print(f"✅ Target 1 updated to £{value}")

elif field in ['T2', 'TARGET2']:
    if value <= t1:
        errors.append(f"Target 2 (£{value}) must be ABOVE Target 1 (£{t1})")
    elif value <= entry:
        errors.append(f"Target 2 (£{value}) must be ABOVE entry (£{entry})")
    else:
        d['target2'] = value
        print(f"✅ Target 2 updated to £{value}")

elif field in ['QTY', 'QUANTITY']:
    if value <= 0:
        errors.append("Quantity must be greater than 0")
    else:
        risk = entry - stop
        notional = round(value * entry, 2)
        total_risk = round(value * risk, 2)
        if total_risk > 50:
            errors.append(f"Qty {value} = £{total_risk} risk — exceeds £50 max. Max qty is {round(50/risk,2)} shares")
        elif notional > 250:
            errors.append(f"Qty {value} = £{notional} notional — exceeds £250 position limit")
        else:
            d['quantity'] = value
            print(f"✅ Quantity updated to {value} shares (notional: £{notional}, risk: £{total_risk})")

elif field == 'RR':
    if value < 1.0:
        errors.append("R:R ratio must be at least 1.0")
    else:
        risk = entry - stop
        d['target1'] = round(entry + risk * (value * 0.6), 2)
        d['target2'] = round(entry + risk * value, 2)
        print(f"✅ R:R updated to 1:{value}")
        print(f"📐 T1=£{d['target1']} T2=£{d['target2']}")
else:
    errors.append(f"Unknown field: {field}. Use STOP, ENTRY, T1, T2, QTY, or RR")

if errors:
    for e in errors:
        print(f"❌ {e}")
    sys.exit(1)

# Save updated signal
with open('$SIGNAL_FILE', 'w') as f:
    json.dump(d, f, indent=2)

# Show full updated card
entry = d['entry']
stop  = d['stop']
risk  = entry - stop
t1    = d['target1']
t2    = d['target2']
qty   = d['quantity']

print(f"""
📊 UPDATED SIGNAL — {name}
💰 Entry:  £{entry}
🛑 Stop:   £{stop}  (£{round(risk,2)} risk/share)
🎯 T1:     £{t1}  (1:{round((t1-entry)/risk,1) if risk>0 else '?'} R:R)
🎯 T2:     £{t2}  (1:{round((t2-entry)/risk,1) if risk>0 else '?'} R:R)
📐 Qty:    {qty} shares
⚠️ Risk:   £{round(risk*qty,2)} total

Reply CONFIRM to execute or ADJUST [field] [value] to change more.""")
PYEOF

# Capture output and send
RESULT=$(python3 << PYEOF
import json, sys

with open('$SIGNAL_FILE') as f:
    d = json.load(f)

field = '$FIELD'.upper()
value = '$VALUE'

# Re-run just for output capture
entry = d.get('entry', 0)
stop  = d.get('stop', 0)
risk  = entry - stop if entry > stop else 1
t1    = d.get('target1', 0)
t2    = d.get('target2', 0)
qty   = d.get('quantity', 0)
name  = d.get('name', '')

print(f"""📊 UPDATED SIGNAL — {name}
💰 Entry:  £{entry}
🛑 Stop:   £{stop}
🎯 T1:     £{t1}  (1:{round((t1-entry)/risk,1) if risk>0 else '?'} R:R)
🎯 T2:     £{t2}  (1:{round((t2-entry)/risk,1) if risk>0 else '?'} R:R)
📐 Qty:    {qty} shares | Risk: £{round(risk*qty,2)}

Reply CONFIRM to execute or ADJUST to change more.""")
PYEOF
)

send_message "$RESULT"
