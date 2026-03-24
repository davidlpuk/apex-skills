#!/usr/bin/env python3
"""
Portfolio Heat Monitor
Calculates total portfolio risk 'heat' across all open positions.

Heat = sum of ((entry_price - stop_price) * quantity) for each position,
expressed as a percentage of total portfolio value.

A position with a stop 2% below entry and 5% of portfolio allocated
contributes 0.10% of portfolio to heat (2% × 5% = 0.10%).

Two enhancements beyond simple summation:
1. Sector-adjusted heat: positions in the same sector share correlated risk.
   Their heat is summed without diversification credit (worst-case assumption).
2. Hard gate: if total heat > 8% of portfolio, block new entries.

Thresholds:
  ≤ 4%  → NORMAL    — 1.0x new position sizing
  4-6%  → ELEVATED  — 0.75x sizing
  6-8%  → HIGH      — 0.50x sizing
  > 8%  → CRITICAL  — 0.0x (block new entries)

Called by:
  - apex-position-sizer.py (get_heat_multiplier)
  - apex-autopilot.py (safety_check gate)
  - apex-morning-scan.sh (daily heat report)
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, get_portfolio_value
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None): return d or {}
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def get_portfolio_value(): return 5000.0

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
HEAT_FILE      = '/home/ubuntu/.picoclaw/logs/apex-portfolio-heat.json'

# Sectors where positions are treated as fully correlated (no diversification benefit)
CORRELATED_SECTORS = {'energy', 'oil', 'commodities'}

# Max heat threshold — blocks new entries above this level
MAX_HEAT_PCT = 8.0


def calculate_heat():
    """
    Calculate portfolio heat from open positions.
    Returns a dict with heat metrics and a size multiplier.
    """
    now = datetime.now(timezone.utc)

    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except Exception as e:
        log_error(f"portfolio-heat: cannot read positions: {e}")
        return None

    portfolio_value = get_portfolio_value()
    if not portfolio_value or portfolio_value <= 0:
        portfolio_value = 5000.0

    if not positions:
        result = {
            'total_heat_pct':     0.0,
            'total_heat_gbp':     0.0,
            'sector_heat':        {},
            'position_heat':      [],
            'portfolio_value':    portfolio_value,
            'status':             'NORMAL',
            'multiplier':         1.0,
            'note':               'No open positions',
            'block_new_entries':  False,
            'updated_at':         now.isoformat(),
        }
        atomic_write(HEAT_FILE, result)
        return result

    position_heat = []
    sector_heat   = {}
    total_heat_gbp = 0.0

    for pos in positions:
        ticker   = pos.get('t212_ticker', pos.get('name', 'UNKNOWN'))
        name     = pos.get('name', ticker)
        entry    = float(pos.get('entry', 0))
        stop     = float(pos.get('stop', 0))
        quantity = float(pos.get('quantity', 0))
        sector   = pos.get('sector', 'unknown').lower()

        if entry <= 0 or stop <= 0 or quantity <= 0:
            continue

        # Risk per share = distance from entry to stop
        risk_per_share = max(entry - stop, 0)
        risk_gbp       = round(risk_per_share * quantity, 2)
        risk_pct       = round(risk_gbp / portfolio_value * 100, 3) if portfolio_value > 0 else 0

        # Notional value of position
        notional     = round(entry * quantity, 2)
        notional_pct = round(notional / portfolio_value * 100, 1)

        position_heat.append({
            'ticker':       ticker,
            'name':         name,
            'sector':       sector,
            'entry':        entry,
            'stop':         stop,
            'quantity':     quantity,
            'risk_gbp':     risk_gbp,
            'risk_pct':     risk_pct,
            'notional_gbp': notional,
            'notional_pct': notional_pct,
        })

        total_heat_gbp += risk_gbp

        # Aggregate by sector
        if sector not in sector_heat:
            sector_heat[sector] = {'risk_gbp': 0.0, 'positions': 0}
        sector_heat[sector]['risk_gbp']   += risk_gbp
        sector_heat[sector]['positions']  += 1

    total_heat_pct = round(total_heat_gbp / portfolio_value * 100, 2) if portfolio_value > 0 else 0

    # Sector heat as %
    for s in sector_heat:
        sector_heat[s]['risk_pct'] = round(
            sector_heat[s]['risk_gbp'] / portfolio_value * 100, 2
        )

    # Status and multiplier
    if total_heat_pct <= 4.0:
        status     = 'NORMAL'
        multiplier = 1.0
        note       = f"Heat {total_heat_pct:.1f}% — full sizing available"
    elif total_heat_pct <= 6.0:
        status     = 'ELEVATED'
        multiplier = 0.75
        note       = f"Heat {total_heat_pct:.1f}% — reduce new positions to 75%"
    elif total_heat_pct <= 8.0:
        status     = 'HIGH'
        multiplier = 0.50
        note       = f"Heat {total_heat_pct:.1f}% — reduce new positions to 50%"
    else:
        status     = 'CRITICAL'
        multiplier = 0.0
        note       = f"Heat {total_heat_pct:.1f}% — BLOCK new entries until heat reduces"

    # Warn on any single sector with > 4% heat (concentrated sector risk)
    hot_sectors = {s: v for s, v in sector_heat.items() if v['risk_pct'] > 4.0}

    result = {
        'total_heat_pct':    total_heat_pct,
        'total_heat_gbp':    round(total_heat_gbp, 2),
        'sector_heat':       sector_heat,
        'hot_sectors':       hot_sectors,
        'position_count':    len(position_heat),
        'position_heat':     sorted(position_heat, key=lambda x: -x['risk_pct']),
        'portfolio_value':   round(portfolio_value, 2),
        'status':            status,
        'multiplier':        multiplier,
        'note':              note,
        'block_new_entries': total_heat_pct > MAX_HEAT_PCT,
        'updated_at':        now.isoformat(),
    }

    atomic_write(HEAT_FILE, result)
    return result


def get_heat_multiplier():
    """
    Returns (multiplier, status, heat_pct) for use by position sizer.
    Reads from file if fresh (< 2h), otherwise recalculates.
    """
    try:
        data = safe_read(HEAT_FILE, {})
        if data:
            updated = data.get('updated_at', '')
            if updated:
                age_h = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(updated)).total_seconds() / 3600
                if age_h < 2:
                    return (data.get('multiplier', 1.0),
                            data.get('status', 'NORMAL'),
                            data.get('total_heat_pct', 0.0))
    except Exception:
        pass

    result = calculate_heat()
    if result:
        return result['multiplier'], result['status'], result['total_heat_pct']
    return 1.0, 'UNKNOWN', 0.0


def is_blocked():
    """Returns True if portfolio heat is too high for new entries."""
    _, _, heat_pct = get_heat_multiplier()
    return heat_pct > MAX_HEAT_PCT


def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== PORTFOLIO HEAT CHECK ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    result = calculate_heat()
    if not result:
        print("  ERROR: Cannot calculate heat")
        return

    icons = {'NORMAL': '✅', 'ELEVATED': '⚠️', 'HIGH': '🟠', 'CRITICAL': '🚨'}
    icon  = icons.get(result['status'], '⚠️')

    print(f"\n  {icon} Status:     {result['status']}")
    print(f"  Total heat:  {result['total_heat_pct']:.2f}% (£{result['total_heat_gbp']:.2f})")
    print(f"  Portfolio:   £{result['portfolio_value']:,.2f}")
    print(f"  Multiplier:  {result['multiplier']}x")
    print(f"  Positions:   {result['position_count']}")

    if result['position_heat']:
        print(f"\n  Position breakdown:")
        for p in result['position_heat']:
            print(f"    {p['name']:15} risk £{p['risk_gbp']:5.2f} ({p['risk_pct']:.2f}%) "
                  f"| notional {p['notional_pct']:.1f}%")

    if result['hot_sectors']:
        print(f"\n  ⚠️  Hot sectors (>4% heat):")
        for s, v in result['hot_sectors'].items():
            print(f"    {s}: {v['risk_pct']:.2f}% heat across {v['positions']} positions")

    if result['block_new_entries']:
        print(f"\n  🚨 NEW ENTRIES BLOCKED — heat exceeds {MAX_HEAT_PCT}%")

    print(f"\n  {result['note']}")


if __name__ == '__main__':
    run()
