#!/usr/bin/env python3
"""
Pillar 3: Liquidity & Slippage Audit (The Shaw Test)
Measures actual fill quality vs signal price on every trade.

NOW: Records every fill and measures slippage from signal price.
ACTIVATES: From trade 1 — every fill is measured immediately.

Answers:
- What is actual slippage vs expected price?
- Are limit orders filling at the signal price or worse?
- Which instruments have consistently poor fills?
- What is the true transaction cost including slippage?
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARRANTY: {m}')

SHAW_FILE      = '/home/ubuntu/.picoclaw/logs/apex-shaw-test.json'
SLIPPAGE_FILE  = '/home/ubuntu/.picoclaw/logs/apex-slippage.json'

# Slippage thresholds
SLIPPAGE_WARN_PCT  = 0.30  # Warn if slippage > 0.30%
SLIPPAGE_ALERT_PCT = 0.75  # Alert if slippage > 0.75%
FILL_TIMEOUT_MINS  = 30    # Flag unfilled orders after 30 mins

def record_fill(signal_name, ticker, signal_price, fill_price,
                quantity, signal_type, order_type='LIMIT'):
    """
    Record an actual fill and calculate slippage.
    Called immediately after every order confirmation.
    """
    now = datetime.now(timezone.utc)

    if signal_price <= 0 or fill_price <= 0:
        return None

    # Slippage calculation
    # For BUY: positive slippage = filled higher than signal (bad)
    # For SELL: positive slippage = filled lower than signal (bad)
    slip_per_share = fill_price - signal_price
    slip_pct       = round(abs(slip_per_share) / signal_price * 100, 4)
    slip_gbp       = round(abs(slip_per_share) * quantity, 2)
    slip_direction = 'ADVERSE' if fill_price > signal_price else (
                     'FAVOURABLE' if fill_price < signal_price else 'EXACT')

    # T212 FX cost (0.15% on USD instruments)
    is_usd     = '_US_EQ' in ticker or not ticker.endswith('l_EQ')
    fx_cost    = round(fill_price * quantity * 0.0015, 2) if is_usd else 0
    total_cost = round(slip_gbp + fx_cost, 2)

    # Impact on EV
    # Every £1 of slippage reduces EV by £1
    ev_impact = -total_cost

    record = {
        'timestamp':     now.isoformat(),
        'date':          now.strftime('%Y-%m-%d'),
        'name':          signal_name,
        'ticker':        ticker,
        'signal_type':   signal_type,
        'order_type':    order_type,
        'signal_price':  round(signal_price, 4),
        'fill_price':    round(fill_price, 4),
        'quantity':      quantity,
        'slip_per_share':round(slip_per_share, 4),
        'slip_pct':      slip_pct,
        'slip_gbp':      slip_gbp,
        'slip_direction':slip_direction,
        'fx_cost_gbp':   fx_cost,
        'total_cost_gbp':total_cost,
        'ev_impact':     ev_impact,
        'is_usd':        is_usd,
    }

    # Load and append to slippage log
    slip_log = safe_read(SHAW_FILE, {'fills': [], 'summary': {}})
    fills    = slip_log.get('fills', [])
    fills.append(record)
    slip_log['fills']   = fills[-500:]  # Keep last 500
    slip_log['summary'] = calculate_summary(fills)
    atomic_write(SHAW_FILE, slip_log)

    # Alert on large slippage
    if slip_pct > SLIPPAGE_ALERT_PCT:
        log_warning(f"HIGH SLIPPAGE: {signal_name} {slip_pct}% (£{slip_gbp}) on {fill_price} vs signal {signal_price}")

    return record

def calculate_summary(fills):
    """Calculate slippage statistics across all fills."""
    if not fills:
        return {}

    total_slip_gbp   = round(sum(f.get('slip_gbp', 0) for f in fills), 2)
    total_fx_gbp     = round(sum(f.get('fx_cost_gbp', 0) for f in fills), 2)
    total_cost       = round(total_slip_gbp + total_fx_gbp, 2)
    avg_slip_pct     = round(sum(f.get('slip_pct', 0) for f in fills) / len(fills), 4)

    adverse  = [f for f in fills if f.get('slip_direction') == 'ADVERSE']
    favourable=[f for f in fills if f.get('slip_direction') == 'FAVOURABLE']
    exact    = [f for f in fills if f.get('slip_direction') == 'EXACT']

    # By instrument
    by_instrument = {}
    for f in fills:
        name = f.get('name', '?')
        if name not in by_instrument:
            by_instrument[name] = {'fills': 0, 'total_slip': 0, 'total_cost': 0}
        by_instrument[name]['fills']      += 1
        by_instrument[name]['total_slip'] += f.get('slip_gbp', 0)
        by_instrument[name]['total_cost'] += f.get('total_cost_gbp', 0)

    worst_instrument = max(
        by_instrument.items(),
        key=lambda x: x[1]['total_cost'],
        default=(None, {})
    )

    return {
        'total_fills':       len(fills),
        'total_slip_gbp':    total_slip_gbp,
        'total_fx_gbp':      total_fx_gbp,
        'total_cost_gbp':    total_cost,
        'avg_slip_pct':      avg_slip_pct,
        'adverse_fills':     len(adverse),
        'favourable_fills':  len(favourable),
        'exact_fills':       len(exact),
        'worst_instrument':  worst_instrument[0],
        'by_instrument':     {k: {
            'fills': v['fills'],
            'avg_cost': round(v['total_cost'] / v['fills'], 2) if v['fills'] else 0
        } for k, v in by_instrument.items()},
    }

def audit_signal_liquidity(signal):
    """
    Pre-trade liquidity audit using historical fill data.
    Returns Shaw audit result with slippage estimate.
    """
    name     = signal.get('name', '?')
    ticker   = signal.get('t212_ticker', '')
    entry    = float(signal.get('entry', 0))
    quantity = float(signal.get('quantity', 0))
    is_usd   = '_US_EQ' in ticker

    # Historical slippage for this instrument
    shaw_data  = safe_read(SHAW_FILE, {'fills': []})
    fills      = shaw_data.get('fills', [])
    inst_fills = [f for f in fills if f.get('name') == name]

    # Estimate slippage
    if inst_fills:
        avg_slip_pct = sum(f.get('slip_pct', 0) for f in inst_fills) / len(inst_fills)
        estimated_slip_pct = round(avg_slip_pct, 4)
        data_source = f"Historical ({len(inst_fills)} fills)"
    else:
        # Use defaults by instrument type
        if is_usd and 'SQQQ' in ticker or '3USS' in ticker:
            estimated_slip_pct = 0.25  # Leveraged ETFs wider spread
        elif is_usd:
            estimated_slip_pct = 0.05  # US large caps tight
        else:
            estimated_slip_pct = 0.15  # UK stocks slightly wider
        data_source = "Default estimate (no historical fills)"

    # FX cost
    fx_cost = round(entry * quantity * 0.0015, 2) if is_usd else 0

    # Total estimated cost
    slip_cost  = round(entry * quantity * estimated_slip_pct / 100, 2)
    total_cost = round(slip_cost + fx_cost, 2)

    # Exit ghost — estimated cost to exit position
    # For liquid large caps this is symmetric
    # For leveraged ETFs slightly higher due to wider spreads
    exit_ghost = round(total_cost * 1.1, 2)  # 10% higher on exit

    round_trip = round(total_cost + exit_ghost, 2)

    verdict = "APPROVED"
    if estimated_slip_pct > SLIPPAGE_ALERT_PCT:
        verdict = "REDUCE — high historical slippage on this instrument"
    elif estimated_slip_pct > SLIPPAGE_WARN_PCT:
        verdict = "CAUTION — above average slippage expected"

    return {
        'instrument':          name,
        'is_usd':              is_usd,
        'quantity':            quantity,
        'notional':            round(entry * quantity, 2),
        'estimated_slip_pct':  estimated_slip_pct,
        'estimated_slip_gbp':  slip_cost,
        'fx_cost_gbp':         fx_cost,
        'entry_cost_gbp':      total_cost,
        'exit_ghost_gbp':      exit_ghost,
        'round_trip_cost_gbp': round_trip,
        'data_source':         data_source,
        'verdict':             verdict,
    }

def run():
    """Generate Shaw test report."""
    now      = datetime.now(timezone.utc)
    shaw_data = safe_read(SHAW_FILE, {'fills': [], 'summary': {}})
    summary   = shaw_data.get('summary', {})

    print(f"\n=== SHAW TEST — LIQUIDITY & SLIPPAGE AUDIT ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    total = summary.get('total_fills', 0)

    if total == 0:
        print(f"  Status: COLLECTING — no fills recorded yet")
        print(f"  Slippage measured from first trade Monday")
        print(f"\n  Default slippage estimates:")
        print(f"    US large caps (AAPL, XOM, V):  0.05% per side")
        print(f"    UK stocks (HSBA, AZN, ULVR):   0.15% per side")
        print(f"    Leveraged ETFs (QQQS, 3USS):    0.25% per side")
        print(f"    T212 FX conversion (USD):       0.15% flat")
        print(f"\n  Estimated round-trip costs on current positions:")

        positions = safe_read('/home/ubuntu/.picoclaw/logs/apex-positions.json', [])
        for pos in positions:
            ticker  = pos.get('t212_ticker','')
            is_usd  = '_US_EQ' in ticker
            entry   = float(pos.get('entry', 0))
            qty     = float(pos.get('quantity', 0))
            notional= round(entry * qty, 2)
            slip_est= 0.10 if is_usd else 0.30  # Round trip %
            fx_est  = round(notional * 0.003, 2) if is_usd else 0
            slip_gbp= round(notional * slip_est / 100, 2)
            total_rt= round(slip_gbp + fx_est, 2)
            print(f"    {pos.get('name','?'):25} £{notional:8.2f} notional | "
                  f"est. round-trip cost: £{total_rt:.2f}")
    else:
        print(f"  Total fills tracked: {total}")
        print(f"  Total slippage:      £{summary.get('total_slip_gbp',0)}")
        print(f"  Total FX costs:      £{summary.get('total_fx_gbp',0)}")
        print(f"  Total cost:          £{summary.get('total_cost_gbp',0)}")
        print(f"  Avg slippage:        {summary.get('avg_slip_pct',0)}%")
        print(f"  Adverse fills:       {summary.get('adverse_fills',0)}/{total}")
        if summary.get('worst_instrument'):
            print(f"  Worst instrument:    {summary.get('worst_instrument')}")

    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'status':    'COLLECTING' if total == 0 else 'ACTIVE',
        'fills':     shaw_data.get('fills', []),
        'summary':   summary,
    }

    atomic_write(SHAW_FILE, output)
    print(f"\n✅ Shaw test saved")
    return output

if __name__ == '__main__':
    run()
