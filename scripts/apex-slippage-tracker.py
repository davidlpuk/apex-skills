#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


SLIPPAGE_FILE  = '/home/ubuntu/.picoclaw/logs/apex-slippage.json'
OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'

def load_slippage():
    try:
        with open(SLIPPAGE_FILE) as f:
            return json.load(f)
    except:
        return {"records": [], "summary": {}}

def save_slippage(data):
    atomic_write(SLIPPAGE_FILE, data)

def log_slippage(name, ticker, intended_price, actual_price, quantity, side="BUY", stop_price=0):
    db      = load_slippage()
    now     = datetime.now(timezone.utc)

    slip_per_share = round(actual_price - intended_price, 4) if side == "BUY" else round(intended_price - actual_price, 4)
    slip_pct       = round(abs(slip_per_share) / intended_price * 100, 4)
    slip_cost      = round(abs(slip_per_share) * quantity, 2)
    direction      = "AGAINST" if slip_per_share > 0 else ("IN_FAVOUR" if slip_per_share < 0 else "NONE")

    # Cost as % of risk budget
    risk_per_share   = abs(intended_price - stop_price) if stop_price > 0 else intended_price * 0.06
    risk_gbp         = round(risk_per_share * quantity, 2)
    cost_as_pct_risk = round(slip_cost / risk_gbp * 100, 1) if risk_gbp > 0 else 0.0

    record = {
        "date":             now.strftime('%Y-%m-%d'),
        "name":             name,
        "ticker":           ticker,
        "side":             side,
        "intended_price":   intended_price,
        "actual_price":     actual_price,
        "quantity":         quantity,
        "slip_per_share":   slip_per_share,
        "slip_pct":         slip_pct,
        "slip_cost":        slip_cost,
        "direction":        direction,
        "stop_price":       stop_price,
        "risk_gbp":         risk_gbp,
        "cost_as_pct_risk": cost_as_pct_risk,
    }

    db['records'].append(record)

    # Update summary
    records = db['records']
    total   = len(records)
    against = [r for r in records if r['direction'] == 'AGAINST']
    favour  = [r for r in records if r['direction'] == 'IN_FAVOUR']

    avg_slip_cost  = round(sum(r['slip_cost'] for r in records) / total, 2) if total else 0
    avg_slip_pct   = round(sum(r['slip_pct'] for r in records) / total, 4) if total else 0
    total_slip_cost = round(sum(r['slip_cost'] for r in records), 2)

    db['summary'] = {
        "total_trades":      total,
        "trades_against":    len(against),
        "trades_in_favour":  len(favour),
        "avg_slip_per_trade": avg_slip_cost,
        "avg_slip_pct":      avg_slip_pct,
        "total_slip_cost":   total_slip_cost,
        "last_updated":      now.strftime('%Y-%m-%d')
    }

    save_slippage(db)
    print(f"Slippage logged: {name} | intended £{intended_price} → actual £{actual_price} | slip: £{slip_cost} ({direction})")

def show_report():
    db      = load_slippage()
    records = db.get('records', [])
    summary = db.get('summary', {})

    if not records:
        print("No slippage data yet — logs after first filled order")
        return

    total = summary.get('total_trades', 0)
    print(f"\n📊 SLIPPAGE REPORT — {total} trades\n")
    print(f"  Avg slippage per trade: £{summary.get('avg_slip_per_trade', 0)}")
    print(f"  Avg slippage %:         {summary.get('avg_slip_pct', 0)}%")
    print(f"  Total slippage cost:    £{summary.get('total_slip_cost', 0)}")
    print(f"  Trades against you:     {summary.get('trades_against', 0)}/{total}")
    print(f"  Trades in your favour:  {summary.get('trades_in_favour', 0)}/{total}")

    print(f"\n  Recent records:")
    for r in records[-5:]:
        icon = "🔴" if r['direction'] == 'AGAINST' else "✅"
        print(f"  {icon} {r['name']:15} | intended £{r['intended_price']} → actual £{r['actual_price']} | cost: £{r['slip_cost']}")

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'report'

    if mode == 'report':
        show_report()
    elif mode == 'log' and len(sys.argv) >= 7:
        log_slippage(
            name=sys.argv[2],
            ticker=sys.argv[3],
            intended_price=float(sys.argv[4]),
            actual_price=float(sys.argv[5]),
            quantity=float(sys.argv[6]),
            side=sys.argv[7] if len(sys.argv) > 7 else "BUY",
            stop_price=float(sys.argv[8]) if len(sys.argv) > 8 else 0,
        )
