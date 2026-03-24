#!/usr/bin/env python3
"""
HMRC Capital Gains Tax Tracker (UK Share Pooling)

UK CGT rules for shares use the Section 104 "Share Pooling" method:
  - All shares of the same company held at a given time are treated as a
    single pool with a single average cost basis.
  - The pool grows on each purchase (add shares + cost to pool).
  - A disposal reduces the pool pro-rata and crystallises a gain/loss.

Special matching rules (checked first, in this order):
  1. Same-day rule: disposal matched against same-day acquisition first.
  2. Bed-and-breakfast rule (30-day rule): disposal matched against any
     acquisition within 30 days AFTER the disposal date (prevents wash sales).
  3. Section 104 pool: remaining disposal quantity matched against the pool.

Annual CGT allowance: £3,000 (2024/25 and 2025/26 rates).
Basic rate CGT on shares: 18% (was 10% before Oct 2024 budget).
Higher rate CGT on shares: 24% (was 20% before Oct 2024 budget).

This tracker:
  1. Reads all closed trades from apex-outcomes.json.
  2. Builds the Section 104 pool per instrument.
  3. Calculates gain/loss per disposal using share pooling.
  4. Flags 30-day rule violations.
  5. Reports annual CGT summary and allowance usage.
  6. Produces a tax report for HMRC Self Assessment.

Note: This is a calculation aid, not tax advice. Verify with an accountant.
"""
import json
import sys
from datetime import datetime, timezone, timedelta, date as date_cls

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None): return d or {}
    def log_error(m): print(f'ERROR: {m}')

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
TAX_FILE      = '/home/ubuntu/.picoclaw/logs/apex-tax-report.json'

# UK CGT annual exempt amount (2024/25 → 2025/26)
CGT_ANNUAL_EXEMPT    = 3000.0

# CGT rates (post-Oct 2024 budget)
CGT_RATE_BASIC       = 0.18
CGT_RATE_HIGHER      = 0.24

# UK tax year: 6 April → 5 April
def tax_year_for_date(d):
    """Return tax year string e.g. '2025/26' for a given date."""
    if isinstance(d, str):
        d = datetime.fromisoformat(d).date()
    elif isinstance(d, datetime):
        d = d.date()
    if d.month > 4 or (d.month == 4 and d.day >= 6):
        return f"{d.year}/{str(d.year + 1)[-2:]}"
    else:
        return f"{d.year - 1}/{str(d.year)[-2:]}"


def tax_year_start(year_str):
    """Return the start date (6 April) of a tax year string like '2024/25'."""
    y = int(year_str[:4])
    return date_cls(y, 4, 6)


def tax_year_end(year_str):
    """Return the end date (5 April) of a tax year string like '2024/25'."""
    y = int(year_str[:4])
    return date_cls(y + 1, 4, 5)


class Section104Pool:
    """
    HMRC Section 104 share pool for a single instrument.
    Tracks total shares held and total allowable cost.
    """

    def __init__(self, name):
        self.name       = name
        self.shares     = 0.0
        self.total_cost = 0.0   # allowable cost in GBP

    @property
    def avg_cost(self):
        return (self.total_cost / self.shares) if self.shares > 0 else 0.0

    def acquire(self, qty, cost_per_share, trade_date, costs=0.0):
        """Add shares to pool."""
        self.shares     += qty
        self.total_cost += qty * cost_per_share + costs

    def dispose(self, qty, proceeds_per_share, trade_date, costs=0.0):
        """
        Dispose of qty shares from pool.
        Returns (gain_loss_gbp, allowable_cost_used, pool_cost_before).
        """
        if qty > self.shares + 0.001:
            log_error(f"Pool {self.name}: disposing {qty} but only {self.shares} in pool")
            qty = self.shares

        allowable_cost    = qty * self.avg_cost
        gross_proceeds    = qty * proceeds_per_share - costs
        gain_loss         = gross_proceeds - allowable_cost

        self.shares     -= qty
        self.total_cost -= allowable_cost

        if self.shares < 0.001:
            self.shares     = 0
            self.total_cost = 0

        return round(gain_loss, 2), round(allowable_cost, 2)


def load_trades():
    """Load and sort all closed trades from outcomes file."""
    outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
    trades   = outcomes.get('trades', [])

    # Keep only closed trades (have exit_date and pnl)
    closed = [t for t in trades if t.get('exit_date') or t.get('closed_at')]

    # Normalise date field
    for t in closed:
        if not t.get('exit_date') and t.get('closed_at'):
            t['exit_date'] = t['closed_at'][:10]
        if not t.get('entry_date') and t.get('opened_at'):
            t['entry_date'] = t['opened_at'][:10]

    # Sort by exit date
    closed.sort(key=lambda x: x.get('exit_date', '1970-01-01'))
    return closed


def build_tax_report(trades):
    """
    Apply UK share pooling rules to all trades.
    Returns per-year CGT summary and per-disposal detail.
    """
    # Group acquisitions and disposals by instrument and date for matching rules
    by_instrument   = {}
    disposals       = []

    for t in trades:
        name         = t.get('name', t.get('ticker', 'UNKNOWN'))
        qty          = float(t.get('quantity', t.get('qty', 0)) or 0)
        entry_price  = float(t.get('entry', t.get('entry_price', 0)) or 0)
        exit_price   = float(t.get('exit_price', t.get('close_price', 0)) or 0)
        entry_date   = t.get('entry_date', '')
        exit_date    = t.get('exit_date', '')
        currency     = t.get('currency', 'GBP')
        fx_cost      = float(t.get('fx_cost', 0) or 0)

        if not name or qty <= 0 or entry_price <= 0 or not entry_date:
            continue

        if name not in by_instrument:
            by_instrument[name] = {
                'pool':        Section104Pool(name),
                'acquisitions': [],  # (date, qty, cost_per_share)
                'disposals':    [],
            }

        # Record acquisition
        by_instrument[name]['acquisitions'].append({
            'date':      entry_date,
            'qty':       qty,
            'price':     entry_price,
            'fx_cost':   fx_cost / 2,  # Split FX cost between entry and exit
        })

        # Record disposal (if position was closed)
        if exit_price > 0 and exit_date:
            by_instrument[name]['disposals'].append({
                'date':       exit_date,
                'entry_date': entry_date,
                'qty':        qty,
                'entry_price':entry_price,
                'exit_price': exit_price,
                'fx_cost':    fx_cost / 2,
                'pnl_gbp':    float(t.get('pnl', 0) or 0),
                'trade_ref':  t.get('id', f"{name}_{exit_date}"),
            })

    # Now calculate CGT for each disposal
    all_disposals_cgt = []

    for name, data in by_instrument.items():
        pool       = data['pool']
        acqs       = sorted(data['acquisitions'], key=lambda x: x['date'])
        disps      = sorted(data['disposals'],    key=lambda x: x['date'])

        # Build a combined timeline of acquisitions and disposals
        # Apply matching rules:
        #   1. Same-day (Section 105)
        #   2. 30-day bed-and-breakfast (Section 106A)
        #   3. Section 104 pool

        # For simplicity with small portfolio: build pool chronologically
        # and detect 30-day rule violations as warnings (flag for accountant)
        acq_idx = 0

        for disp in disps:
            disp_date = disp['date']
            disp_qty  = disp['qty']
            exit_p    = disp['exit_price']
            entry_p   = disp['entry_price']
            fx_cost   = disp['fx_cost']
            tax_yr    = tax_year_for_date(disp_date)

            # Process all acquisitions up to and including disposal date
            while acq_idx < len(acqs) and acqs[acq_idx]['date'] <= disp_date:
                a = acqs[acq_idx]
                pool.acquire(a['qty'], a['price'], a['date'], a.get('fx_cost', 0))
                acq_idx += 1

            # Check for 30-day rule: any acquisition within 30 days AFTER this disposal
            disp_date_obj = datetime.fromisoformat(disp_date).date()
            future_acqs   = [
                a for a in acqs[acq_idx:]
                if datetime.fromisoformat(a['date']).date() <= disp_date_obj + timedelta(days=30)
                and a['date'] > disp_date
            ]
            bed_and_breakfast = bool(future_acqs)

            # Section 104 disposal
            gain_loss, cost_used = pool.dispose(disp_qty, exit_p, disp_date, fx_cost)

            all_disposals_cgt.append({
                'name':               name,
                'disposal_date':      disp_date,
                'entry_date':         disp['entry_date'],
                'qty':                disp_qty,
                'proceeds_per_share': exit_p,
                'cost_per_share':     cost_used / disp_qty if disp_qty else 0,
                'gross_proceeds':     round(disp_qty * exit_p, 2),
                'allowable_cost':     round(cost_used, 2),
                'gain_loss':          gain_loss,
                'tax_year':           tax_yr,
                'bed_and_breakfast':  bed_and_breakfast,
                'pnl_gbp':            disp['pnl_gbp'],
                'trade_ref':          disp['trade_ref'],
            })

    # Aggregate by tax year
    by_year = {}
    for d in all_disposals_cgt:
        yr = d['tax_year']
        if yr not in by_year:
            by_year[yr] = {
                'tax_year':        yr,
                'total_gains':     0.0,
                'total_losses':    0.0,
                'net_gain':        0.0,
                'taxable_gain':    0.0,
                'allowance_used':  0.0,
                'allowance_remaining': CGT_ANNUAL_EXEMPT,
                'disposals':       0,
                'bed_and_breakfast_warnings': [],
                'estimated_tax_basic_rate':  0.0,
                'estimated_tax_higher_rate': 0.0,
            }

        yr_data = by_year[yr]
        if d['gain_loss'] >= 0:
            yr_data['total_gains']  += d['gain_loss']
        else:
            yr_data['total_losses'] += abs(d['gain_loss'])
        yr_data['disposals'] += 1

        if d['bed_and_breakfast']:
            yr_data['bed_and_breakfast_warnings'].append(
                f"{d['name']} disposal {d['disposal_date']} — re-acquired within 30 days "
                f"(bed-and-breakfast rule applies; cost basis may differ)"
            )

    # Final calculations per year
    for yr, data in by_year.items():
        net           = round(data['total_gains'] - data['total_losses'], 2)
        taxable       = max(net - CGT_ANNUAL_EXEMPT, 0)
        allowance_used = min(net, CGT_ANNUAL_EXEMPT) if net > 0 else 0

        data['net_gain']                 = net
        data['taxable_gain']             = round(taxable, 2)
        data['allowance_used']           = round(allowance_used, 2)
        data['allowance_remaining']      = round(CGT_ANNUAL_EXEMPT - allowance_used, 2)
        data['estimated_tax_basic_rate'] = round(taxable * CGT_RATE_BASIC, 2)
        data['estimated_tax_higher_rate']= round(taxable * CGT_RATE_HIGHER, 2)

    return all_disposals_cgt, by_year


def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== HMRC CGT TAX TRACKER ===")
    print(f"Date: {now.strftime('%Y-%m-%d')}")
    print(f"Annual exempt amount: £{CGT_ANNUAL_EXEMPT:,.0f}")
    print(f"CGT rates: {CGT_RATE_BASIC*100:.0f}% (basic) / {CGT_RATE_HIGHER*100:.0f}% (higher)")

    trades = load_trades()
    if not trades:
        print("\n  No closed trades found in apex-outcomes.json")
        print("  Tax tracker requires completed trades to calculate gains/losses.")
        return

    print(f"\n  Loaded {len(trades)} closed trades")

    disposals, by_year = build_tax_report(trades)

    if not by_year:
        print("\n  No disposals found — nothing to report")
        return

    # Print summary by tax year
    for yr in sorted(by_year.keys()):
        data = by_year[yr]
        print(f"\n  ─── Tax Year {yr} ───")
        print(f"    Disposals:              {data['disposals']}")
        print(f"    Total gains:           £{data['total_gains']:8.2f}")
        print(f"    Total losses:          £{data['total_losses']:8.2f}")
        print(f"    Net gain:              £{data['net_gain']:8.2f}")
        print(f"    Annual allowance:      £{CGT_ANNUAL_EXEMPT:8.2f}")
        print(f"    Allowance used:        £{data['allowance_used']:8.2f}")
        print(f"    Allowance remaining:   £{data['allowance_remaining']:8.2f}")
        print(f"    Taxable gain:          £{data['taxable_gain']:8.2f}")
        if data['taxable_gain'] > 0:
            print(f"    Est. tax (basic 18%):  £{data['estimated_tax_basic_rate']:8.2f}")
            print(f"    Est. tax (higher 24%): £{data['estimated_tax_higher_rate']:8.2f}")
        else:
            icon = '✅' if data['net_gain'] >= 0 else '🔴'
            print(f"    {icon} No CGT liability — within annual allowance")

        if data['bed_and_breakfast_warnings']:
            print(f"\n    ⚠️  Bed-and-breakfast rule warnings ({len(data['bed_and_breakfast_warnings'])}):")
            for w in data['bed_and_breakfast_warnings']:
                print(f"      • {w}")

    # Current year loss harvesting opportunity
    current_tax_yr = tax_year_for_date(now.date())
    if current_tax_yr in by_year:
        cur = by_year[current_tax_yr]
        year_end = tax_year_end(current_tax_yr)
        days_left = (year_end - now.date()).days
        if cur['net_gain'] > 0 and cur['taxable_gain'] > 0 and days_left > 0:
            print(f"\n  ⚡ LOSS HARVESTING OPPORTUNITY")
            print(f"    {days_left} days left in tax year (ends {year_end})")
            print(f"    Crystallising £{cur['taxable_gain']:.2f} of losses before year end")
            print(f"    would eliminate £{cur['estimated_tax_basic_rate']:.2f}–"
                  f"£{cur['estimated_tax_higher_rate']:.2f} estimated CGT liability")

    # Save full report
    report = {
        'generated_at':       now.isoformat(),
        'annual_exempt':      CGT_ANNUAL_EXEMPT,
        'cgt_rate_basic':     CGT_RATE_BASIC,
        'cgt_rate_higher':    CGT_RATE_HIGHER,
        'by_year':            by_year,
        'all_disposals':      disposals,
        'disclaimer':         (
            "Calculation aid only — not tax advice. UK share pooling (Section 104) "
            "applied. Same-day and 30-day matching rules flagged but not fully automated. "
            "Verify all figures with a qualified accountant before filing Self Assessment."
        ),
    }
    atomic_write(TAX_FILE, report)
    print(f"\n✅ Tax report saved to apex-tax-report.json")
    print(f"   ⚠️  Verify with an accountant before filing Self Assessment")


if __name__ == '__main__':
    run()
