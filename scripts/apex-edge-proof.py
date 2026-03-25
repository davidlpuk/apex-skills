#!/usr/bin/env python3
"""
Apex Edge Proof — weekly statistical edge validation per signal type.

Loads real trades (apex-outcomes.json) + backtest instrument stats
(apex-backtest-v2-results.json) and runs formal hypothesis tests to
determine whether each strategy type has a statistically proven edge.

Output: logs/apex-edge-proof.json
Runs:   07:08 UTC Monday (after weight optimizer)
"""
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

_LOGS = '/home/ubuntu/.picoclaw/logs'
_OUTCOMES_FILE   = f'{_LOGS}/apex-outcomes.json'
_BACKTEST_FILE   = f'{_LOGS}/apex-backtest-v2-results.json'
_OUTPUT_FILE     = f'{_LOGS}/apex-edge-proof.json'

# Min trades required for any statistical claim
_MIN_TRADES = 5

# Signal type normalisation — outcomes use various labels
_TYPE_ALIASES = {
    'TREND':            ['TREND'],
    'CONTRARIAN':       ['CONTRARIAN'],
    'INVERSE':          ['INVERSE'],
    'EARNINGS_DRIFT':   ['EARNINGS_DRIFT', 'EARNINGS'],
    'DIVIDEND_CAPTURE': ['DIVIDEND_CAPTURE', 'DIVIDEND'],
}

# p-value thresholds for verdict
_P_CONFIRMED = 0.10
_P_MARGINAL  = 0.25


def _load_stats_lib():
    """Import functions from apex-backtest-stats.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'apex_backtest_stats',
        '/home/ubuntu/.picoclaw/scripts/apex-backtest-stats.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_real_trades():
    """Load closed trades from apex-outcomes.json."""
    try:
        with open(_OUTCOMES_FILE) as f:
            data = json.load(f)
        return data.get('trades', [])
    except Exception as e:
        print(f"  [warn] Could not load outcomes: {e}")
        return []


def _load_backtest_instruments():
    """
    Load per-instrument stats from apex-backtest-v2-results.json.
    Returns list of instrument stat dicts (wins, n, win_rate).
    """
    try:
        with open(_BACKTEST_FILE) as f:
            data = json.load(f)
        ia = data.get('instrument_analysis', {})
        return list(ia.values())
    except Exception as e:
        print(f"  [warn] Could not load backtest results: {e}")
        return []


def _classify_trade(trade):
    """Return normalised signal type string for a trade."""
    raw = (trade.get('signal_type') or trade.get('outcome_type') or '').upper()
    for stype, aliases in _TYPE_ALIASES.items():
        if any(a in raw for a in aliases):
            return stype
    # Fallback: treat non-manual wins/losses as TREND
    if 'MANUAL' not in raw:
        return 'TREND'
    return None


def _collect_by_type(trades):
    """
    Group real trades by signal type.
    Returns dict: {signal_type: {'wins': int, 'n': int, 'r_values': list}}
    """
    by_type = {}
    for t in trades:
        stype = _classify_trade(t)
        if stype is None:
            continue
        if stype not in by_type:
            by_type[stype] = {'wins': 0, 'n': 0, 'r_values': []}
        r = t.get('r_achieved', 0) or 0
        result = t.get('result', '').upper()
        is_win = r > 0 or 'WIN' in result
        by_type[stype]['n'] += 1
        by_type[stype]['wins'] += int(is_win)
        by_type[stype]['r_values'].append(r)
    return by_type


def _backtest_aggregate(instruments):
    """
    Aggregate all backtest instrument stats into a single pool.
    Used as supplemental evidence when real-trade n is small.
    Returns {'wins': int, 'n': int}
    """
    total_wins = sum(i.get('wins', 0) for i in instruments)
    total_n    = sum(i.get('n', 0) for i in instruments)
    return {'wins': total_wins, 'n': total_n}


def _expectancy(wins, n, r_values):
    """
    Calculate expectancy in R multiples.
    expectancy = WR × avg_win_R - (1-WR) × avg_loss_R
    Returns (expectancy, avg_win_r, avg_loss_r).
    """
    if n == 0:
        return 0.0, 0.0, 0.0

    win_rs  = [r for r in r_values if r > 0]
    loss_rs = [abs(r) for r in r_values if r <= 0]

    avg_win_r  = sum(win_rs)  / len(win_rs)  if win_rs  else 0.0
    avg_loss_r = sum(loss_rs) / len(loss_rs) if loss_rs else 1.0  # assume 1R loss if no data

    wr = wins / n
    exp = wr * avg_win_r - (1 - wr) * avg_loss_r
    return round(exp, 3), round(avg_win_r, 3), round(avg_loss_r, 3)


def _verdict_from_p(p_value, n):
    """Translate p-value + n into edge verdict."""
    if n < _MIN_TRADES:
        return 'INSUFFICIENT_DATA'
    if p_value < _P_CONFIRMED:
        return 'CONFIRMED'
    if p_value < _P_MARGINAL:
        return 'MARGINAL'
    return 'NOT_PROVEN'


def analyse_edge(stats_lib):
    """
    Run edge proof analysis for all signal types.
    Returns list of result dicts, one per signal type.
    """
    real_trades  = _load_real_trades()
    bt_instruments = _load_backtest_instruments()
    bt_pool      = _backtest_aggregate(bt_instruments)

    by_type = _collect_by_type(real_trades)

    # Ensure all known types appear even with zero trades
    for stype in _TYPE_ALIASES:
        if stype not in by_type:
            by_type[stype] = {'wins': 0, 'n': 0, 'r_values': []}

    results = []
    best_confirmed = None
    best_expectancy = -999

    for stype in sorted(by_type.keys()):
        entry = by_type[stype]
        wins     = entry['wins']
        n        = entry['n']
        r_values = entry['r_values']

        # Combine with backtest pool for types with < MIN_TRADES real data
        combined_wins = wins
        combined_n    = n
        backtest_used = False

        if n < _MIN_TRADES and bt_pool['n'] > 0:
            # Weight backtest at 30% to real trades
            bt_weight = 0.3
            bt_w = int(bt_pool['wins'] * bt_weight)
            bt_n = int(bt_pool['n'] * bt_weight)
            combined_wins = wins + bt_w
            combined_n    = n + bt_n
            backtest_used = True

        # Significance test
        sig = stats_lib.instrument_significance(
            wins=combined_wins,
            n=combined_n,
            baseline_wr=0.50,
            confidence=0.95,
            significance_level=_P_CONFIRMED,
        )
        p_value  = sig['p_value']
        ci_lo, ci_hi = sig['ci']

        # Expectancy
        exp, avg_win, avg_loss = _expectancy(wins, n, r_values)

        # Wilson CI on real trades only (for display)
        real_ci = stats_lib.binomial_ci_pct(wins, n) if n > 0 else (0.0, 100.0)

        verdict = _verdict_from_p(p_value, combined_n)

        result = {
            'signal_type':    stype,
            'n_real':         n,
            'wins_real':      wins,
            'win_rate_pct':   round(wins / n * 100, 1) if n > 0 else None,
            'ci_95':          list(real_ci),
            'backtest_used':  backtest_used,
            'combined_n':     combined_n,
            'combined_wins':  combined_wins,
            'p_value':        p_value,
            'expectancy_r':   exp,
            'avg_win_r':      avg_win,
            'avg_loss_r':     avg_loss,
            'verdict':        verdict,
        }
        results.append(result)

        if verdict == 'CONFIRMED' and exp > best_expectancy:
            best_expectancy = exp
            best_confirmed = stype

    return results, best_confirmed


def _print_report(results, best_type):
    """Print human-readable edge proof report."""
    print()
    print("=" * 62)
    print("  APEX EDGE PROOF — Statistical Validation Report")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 62)

    for r in results:
        stype   = r['signal_type']
        n       = r['n_real']
        wr      = f"{r['win_rate_pct']}%" if r['win_rate_pct'] is not None else "N/A"
        ci      = r['ci_95']
        pv      = r['p_value']
        exp     = r['expectancy_r']
        verdict = r['verdict']

        icon = {'CONFIRMED': '✓', 'MARGINAL': '~', 'NOT_PROVEN': '✗',
                'INSUFFICIENT_DATA': '?'}.get(verdict, ' ')

        print(f"\n  [{icon}] {stype}")
        print(f"      Trades: {n}  |  Win Rate: {wr}  |  95% CI: [{ci[0]}%, {ci[1]}%]")
        print(f"      p-value: {pv:.3f}  |  Expectancy: {exp:+.2f}R  |  Verdict: {verdict}")
        if r['backtest_used']:
            print(f"      (Backtest supplement used — real-trade n too small)")

    print()
    if best_type:
        print(f"  BEST PROVEN TYPE: {best_type}")
    else:
        print("  No signal type has confirmed statistical edge yet.")
        print("  Accumulate more live trades before drawing conclusions.")
    print("=" * 62)
    print()


def main():
    print("Apex Edge Proof — running statistical validation...")

    try:
        stats_lib = _load_stats_lib()
    except Exception as e:
        print(f"  ERROR: Could not load apex-backtest-stats.py: {e}")
        sys.exit(1)

    results, best_type = analyse_edge(stats_lib)

    _print_report(results, best_type)

    output = {
        'timestamp':  datetime.utcnow().isoformat() + 'Z',
        'n_real_trades': sum(r['n_real'] for r in results),
        'best_confirmed_type': best_type,
        'by_signal_type': {r['signal_type']: r for r in results},
        'summary': {
            'confirmed':        [r['signal_type'] for r in results if r['verdict'] == 'CONFIRMED'],
            'marginal':         [r['signal_type'] for r in results if r['verdict'] == 'MARGINAL'],
            'not_proven':       [r['signal_type'] for r in results if r['verdict'] == 'NOT_PROVEN'],
            'insufficient':     [r['signal_type'] for r in results if r['verdict'] == 'INSUFFICIENT_DATA'],
        },
    }

    with open(_OUTPUT_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"  Written: {_OUTPUT_FILE}")


if __name__ == '__main__':
    main()
