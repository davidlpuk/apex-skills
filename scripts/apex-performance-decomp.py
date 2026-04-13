#!/usr/bin/env python3
"""
Apex Performance Decomposition
Computes per-signal-family Sharpe, Sortino, win rate, expectancy,
and slippage cost from closed trade outcomes and slippage records.

Reads:  apex-outcomes.json, apex-slippage.json
Writes: apex-performance-decomp.json

Runs: after each EOD review (append to eod-review.sh), and monthly.
Dashboard: /api/performance returns 'by_family' key from this file.
"""
import json
import math
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f:
            json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return d if d is not None else {}
    def log_warning(m): print(f'WARNING: {m}')

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
SLIPPAGE_FILE = '/home/ubuntu/.picoclaw/logs/apex-slippage.json'
OUTPUT_FILE   = '/home/ubuntu/.picoclaw/logs/apex-performance-decomp.json'

FAMILIES       = ['TREND', 'CONTRARIAN', 'INVERSE', 'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE']
RISK_FREE_RATE = 0.05   # annual (5%)
TRADES_PER_YEAR = 100   # assumed frequency for annualisation
LOOKBACK_DAYS   = 90    # rolling window for recency-weighted metrics


def _sharpe(returns):
    """Annualised Sharpe ratio from a list of per-trade R returns."""
    n = len(returns)
    if n < 2:
        return None
    avg = sum(returns) / n
    rf  = RISK_FREE_RATE / TRADES_PER_YEAR
    variance = sum((r - avg) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return round(((avg - rf) / std) * math.sqrt(TRADES_PER_YEAR), 3)


def _sortino(returns):
    """Annualised Sortino ratio (downside deviation only)."""
    n = len(returns)
    if n < 2:
        return None
    avg = sum(returns) / n
    rf  = RISK_FREE_RATE / TRADES_PER_YEAR
    downside_sq = [(r - rf) ** 2 for r in returns if r < rf]
    if not downside_sq:
        return None
    downside_std = math.sqrt(sum(downside_sq) / len(downside_sq))
    if downside_std == 0:
        return None
    return round(((avg - rf) / downside_std) * math.sqrt(TRADES_PER_YEAR), 3)


def _family_of(trade):
    """Extract signal family from a trade record, normalised to uppercase."""
    raw = (trade.get('signal_type')
           or trade.get('result', '')
           or trade.get('outcome_type', ''))
    raw = str(raw).upper()
    for fam in FAMILIES:
        if fam in raw:
            return fam
    return 'UNKNOWN'


def _analyse_family(trades, slip_records, family):
    """Compute metrics for one signal family."""
    fam_trades = [t for t in trades if _family_of(t) == family]
    n = len(fam_trades)

    if n < 2:
        return {'status': 'INSUFFICIENT', 'n': n}

    returns  = [t.get('r_achieved', 0) for t in fam_trades]
    pnls     = [t.get('pnl', 0) for t in fam_trades]
    wins     = sum(1 for p in pnls if p > 0)
    win_rate = round(wins / n, 4)

    avg_win_r  = (sum(r for r in returns if r > 0) / wins
                  if wins else 0)
    losses     = n - wins
    avg_loss_r = (sum(abs(r) for r in returns if r <= 0) / losses
                  if losses else 0)
    expectancy = round((win_rate * avg_win_r) - ((1 - win_rate) * avg_loss_r), 4)

    sharpe  = _sharpe(returns)
    sortino = _sortino(returns)

    # Slippage for this family
    fam_slip = [r for r in slip_records
                if r.get('signal_type', 'UNKNOWN').upper() == family]
    n_slip = len(fam_slip)
    avg_slip_pct = round(sum(r.get('slip_pct', 0) for r in fam_slip) / n_slip, 4) if n_slip else 0
    avg_slip_gbp = round(sum(r.get('slip_cost', 0) for r in fam_slip) / n_slip, 3) if n_slip else 0
    total_slip_gbp = round(sum(r.get('slip_cost', 0) for r in fam_slip), 2)

    return {
        'status':        'ACTIVE',
        'n':             n,
        'win_rate':      win_rate,
        'expectancy':    expectancy,
        'avg_win_r':     round(avg_win_r, 3),
        'avg_loss_r':    round(avg_loss_r, 3),
        'sharpe':        sharpe,
        'sortino':       sortino,
        'avg_slip_pct':  avg_slip_pct,
        'avg_slip_gbp':  avg_slip_gbp,
        'total_slip_gbp': total_slip_gbp,
        'n_slip_records': n_slip,
    }


def run():
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    outcomes     = safe_read(OUTCOMES_FILE, {'trades': []})
    all_trades   = outcomes.get('trades', [])
    slip_data    = safe_read(SLIPPAGE_FILE, {'records': []})
    slip_records = slip_data.get('records', [])

    # Rolling window subset for recency metrics
    rolling_trades = []
    for t in all_trades:
        opened = t.get('closed') or t.get('opened', '')
        try:
            if datetime.fromisoformat(opened).replace(tzinfo=timezone.utc) >= cutoff:
                rolling_trades.append(t)
        except Exception:
            pass

    by_family = {}
    for family in FAMILIES:
        by_family[family] = _analyse_family(all_trades, slip_records, family)

    # UNKNOWN bucket for untagged records
    unknown = _analyse_family(all_trades, slip_records, 'UNKNOWN')
    if unknown.get('n', 0) > 0:
        by_family['UNKNOWN'] = unknown

    # Aggregate across all known families
    agg_trades  = [t for t in all_trades if _family_of(t) in FAMILIES]
    agg_returns = [t.get('r_achieved', 0) for t in agg_trades]
    agg_wins    = sum(1 for t in agg_trades if t.get('pnl', 0) > 0)
    agg_n       = len(agg_trades)
    total_slip  = round(sum(r.get('slip_cost', 0) for r in slip_records), 2)

    aggregate = {
        'n':            agg_n,
        'win_rate':     round(agg_wins / agg_n, 4) if agg_n else 0,
        'sharpe':       _sharpe(agg_returns),
        'sortino':      _sortino(agg_returns),
        'total_slip_gbp': total_slip,
        'n_rolling':    len(rolling_trades),
    }

    result = {
        'generated':    now.strftime('%Y-%m-%d %H:%M UTC'),
        'lookback_days': LOOKBACK_DAYS,
        'by_family':    by_family,
        'aggregate':    aggregate,
    }

    atomic_write(OUTPUT_FILE, result)

    print(f"\n=== PERFORMANCE DECOMPOSITION ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"All-time trades: {len(all_trades)}  |  Rolling ({LOOKBACK_DAYS}d): {len(rolling_trades)}\n")
    print(f"  {'Family':<22} {'N':>4}  {'WR':>6}  {'Exp':>6}  {'Sharpe':>7}  {'Sortino':>8}  {'Slip%':>6}  {'SlipGBP':>8}")
    print(f"  {'-'*22} {'-'*4}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*8}  {'-'*6}  {'-'*8}")
    for fam in FAMILIES + ['UNKNOWN']:
        fd = by_family.get(fam)
        if not fd or fd.get('status') == 'INSUFFICIENT':
            n = fd.get('n', 0) if fd else 0
            print(f"  {fam:<22} {n:>4}  {'—':>6}  {'—':>6}  {'—':>7}  {'—':>8}  {'—':>6}  {'—':>8}")
            continue
        sharpe_str  = f"{fd['sharpe']:.2f}"  if fd.get('sharpe')  is not None else '—'
        sortino_str = f"{fd['sortino']:.2f}" if fd.get('sortino') is not None else '—'
        print(f"  {fam:<22} {fd['n']:>4}  {fd['win_rate']*100:>5.1f}%  "
              f"{fd['expectancy']:>+6.3f}  {sharpe_str:>7}  {sortino_str:>8}  "
              f"{fd['avg_slip_pct']:>5.3f}%  £{fd['total_slip_gbp']:>7.2f}")
    print(f"\n  Aggregate Sharpe: {aggregate['sharpe']}  |  Total slippage: £{total_slip}")
    print(f"\n  ✅ Saved to apex-performance-decomp.json")


if __name__ == '__main__':
    run()
