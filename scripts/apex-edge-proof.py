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
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

_LOGS = '/home/ubuntu/.picoclaw/logs'
_OUTCOMES_FILE   = f'{_LOGS}/apex-outcomes.json'
_BACKTEST_FILE   = f'{_LOGS}/apex-backtest-v2-results.json'
_OUTPUT_FILE     = f'{_LOGS}/apex-edge-proof.json'
_HISTORY_FILE    = f'{_LOGS}/apex-edge-proof-history.json'

# Cap history at 90 days — long enough for trend visibility, short enough
# to keep the file under ~50KB.
_HISTORY_MAX_DAYS = 90

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

# False Discovery Rate target across the family of strategy tests.
# We test ~5 strategies in parallel — without correction the chance of at
# least one false PROVEN verdict is ~41%.  BH-FDR controls the expected
# proportion of false discoveries.  See benjamini_hochberg() in stats lib.
_BH_FDR = 0.10

# Real-trade weighting policy.
#
# Original logic mixed real trades with backtest at a flat 30% weight when
# n_real < 5 — which meant 4 real trades + 30% × 372 backtest = 4 + 111 = 115
# combined trades, so the real signal was statistically invisible. Backtest
# results suffer from look-ahead bias, regime-shift, and silent assumption
# leakage; they should NEVER outweigh real trades in evidence terms.
#
# New policy:
#   - Each real trade is worth N backtest trades when computing the combined
#     pool.  N = _REAL_TRADE_EQUIVALENT (default 10).
#   - Backtest contribution is also CAPPED at 3× the real-trade count, so the
#     pool never has more than 25% backtest evidence by weight.
#   - Once n_real ≥ 20, backtest is dropped entirely — real trades stand alone.
_REAL_TRADE_EQUIVALENT = 10
_BACKTEST_CAP_RATIO    = 3   # backtest weight ≤ 3× real-trade weight
_REAL_ONLY_THRESHOLD   = 20  # above this, ignore backtest


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


def _combine_with_backtest(wins_real, n_real, bt_pool):
    """
    Real-trade-dominant pooling.

    Returns (combined_wins, combined_n, backtest_used, bt_weight_applied).

    See _REAL_TRADE_EQUIVALENT/_BACKTEST_CAP_RATIO constants for rationale.
    Backtest is dropped entirely once n_real ≥ _REAL_ONLY_THRESHOLD.
    """
    if n_real >= _REAL_ONLY_THRESHOLD or bt_pool['n'] == 0:
        return wins_real, n_real, False, 0.0

    # How many backtest trades may we include?  Cap at _BACKTEST_CAP_RATIO × n_real
    # (or _BACKTEST_CAP_RATIO × _REAL_TRADE_EQUIVALENT when n_real == 0, so a
    # cold-start strategy still gets some prior).
    real_equiv = max(n_real, 1) * _REAL_TRADE_EQUIVALENT
    bt_cap_n   = max(n_real, 1) * _BACKTEST_CAP_RATIO * _REAL_TRADE_EQUIVALENT
    bt_cap_n   = min(bt_cap_n, bt_pool['n'])
    if bt_cap_n <= 0:
        return wins_real, n_real, False, 0.0

    bt_wr = bt_pool['wins'] / bt_pool['n']
    # Express both in REAL-trade units (so significance test sees the right scale)
    real_units_wins = wins_real * _REAL_TRADE_EQUIVALENT
    real_units_n    = n_real    * _REAL_TRADE_EQUIVALENT
    bt_units_n      = bt_cap_n  // _REAL_TRADE_EQUIVALENT * _REAL_TRADE_EQUIVALENT
    bt_units_n      = max(bt_units_n // _REAL_TRADE_EQUIVALENT, 1)
    bt_units_wins   = round(bt_units_n * bt_wr)

    # Final combined pool (in real-trade units divided back out for downstream)
    combined_wins = (real_units_wins // _REAL_TRADE_EQUIVALENT) + bt_units_wins
    combined_n    = (real_units_n    // _REAL_TRADE_EQUIVALENT) + bt_units_n
    bt_weight     = bt_units_n / max(combined_n, 1)
    return combined_wins, combined_n, True, round(bt_weight, 3)


def analyse_edge(stats_lib):
    """
    Run edge proof analysis for all signal types.
    Returns list of result dicts, one per signal type, plus the best PROVEN
    type after multiple-testing correction.
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

    # ── First pass: per-strategy stats, p-values, DSR ─────────────────────────
    for stype in sorted(by_type.keys()):
        entry = by_type[stype]
        wins     = entry['wins']
        n        = entry['n']
        r_values = entry['r_values']

        # Real-trade-dominant pooling (replaces the old 30%-flat weighting)
        combined_wins, combined_n, backtest_used, bt_weight = \
            _combine_with_backtest(wins, n, bt_pool)

        # Win-rate significance test (one-sided binomial vs 50% null)
        sig = stats_lib.instrument_significance(
            wins=combined_wins,
            n=combined_n,
            baseline_wr=0.50,
            confidence=0.95,
            significance_level=_P_CONFIRMED,
        )
        p_value  = sig['p_value']

        # Expectancy from REAL trades only — no synthetic backtest contribution
        exp, avg_win, avg_loss = _expectancy(wins, n, r_values)

        # Wilson CI on real trades only (for display)
        real_ci = stats_lib.binomial_ci_pct(wins, n) if n > 0 else (0.0, 100.0)

        # Deflated Sharpe Ratio on REAL R-multiples only.
        # n_trials = number of strategies we're testing in parallel — the
        # selection-bias correction in DSR uses this.
        n_trials = len(_TYPE_ALIASES)
        dsr = stats_lib.deflated_sharpe_ratio(
            r_multiples=r_values,
            n_trials=n_trials,
            trades_per_year=100,
        )

        result = {
            'signal_type':       stype,
            'n_real':            n,
            'wins_real':         wins,
            'win_rate_pct':      round(wins / n * 100, 1) if n > 0 else None,
            'ci_95':             list(real_ci),
            'backtest_used':     backtest_used,
            'backtest_weight':   bt_weight,
            'combined_n':        combined_n,
            'combined_wins':     combined_wins,
            'p_value':           p_value,
            'expectancy_r':      exp,
            'avg_win_r':         avg_win,
            'avg_loss_r':        avg_loss,
            # DSR block (selection-bias-corrected; uses real trades only)
            'dsr_sharpe':        dsr['sharpe_observed'],
            'dsr_expected_max':  dsr['sharpe_expected_max'],
            'dsr_probability':   dsr['dsr_probability'],
            'dsr_verdict':       dsr['verdict'],
            'return_skew':       dsr['skew'],
            'return_excess_kurt': dsr['excess_kurtosis'],
            # Filled in by second pass after BH-FDR
            'p_adjusted':        None,
            'wr_verdict_raw':    _verdict_from_p(p_value, combined_n),
            'wr_verdict_fdr':    None,
            'verdict':           None,
        }
        results.append(result)

    # ── Second pass: BH-FDR correction across the family of strategies ────────
    # Apply FDR only to strategies with enough data — INSUFFICIENT_DATA cases
    # carry uninformative p-values that would distort the BH ranking.
    testable_idx = [i for i, r in enumerate(results)
                    if r['wr_verdict_raw'] != 'INSUFFICIENT_DATA']
    if testable_idx:
        p_subset = [results[i]['p_value'] for i in testable_idx]
        rejected, adjusted = stats_lib.benjamini_hochberg(p_subset, fdr=_BH_FDR)
        for j, i in enumerate(testable_idx):
            results[i]['p_adjusted'] = adjusted[j]
            results[i]['wr_verdict_fdr'] = (
                'CONFIRMED' if rejected[j] else
                'MARGINAL'  if adjusted[j] < _P_MARGINAL else
                'NOT_PROVEN'
            )

    # ── Third pass: combine WR-FDR verdict with DSR verdict ───────────────────
    # CONFIRMED requires BOTH:
    #   - Win-rate significant after FDR correction
    #   - Deflated Sharpe Ratio probability ≥ 0.95
    # This is intentionally conservative — graduating a NOT_PROVEN strategy to
    # PROVEN affects real capital allocation via apex_sizer NAV caps.
    best_confirmed = None
    best_expectancy = -999
    for r in results:
        wr_v = r['wr_verdict_fdr'] or r['wr_verdict_raw']
        dsr_v = r['dsr_verdict']

        if wr_v == 'CONFIRMED' and dsr_v == 'CONFIRMED':
            verdict = 'CONFIRMED'
        elif wr_v == 'INSUFFICIENT_DATA' or dsr_v == 'INSUFFICIENT_DATA':
            verdict = 'INSUFFICIENT_DATA'
        elif wr_v == 'CONFIRMED' or dsr_v == 'CONFIRMED':
            verdict = 'MARGINAL'   # one passed but not both — treat as marginal
        elif wr_v == 'MARGINAL' or dsr_v == 'MARGINAL':
            verdict = 'MARGINAL'
        else:
            verdict = 'NOT_PROVEN'
        r['verdict'] = verdict

        if verdict == 'CONFIRMED' and r['expectancy_r'] > best_expectancy:
            best_expectancy = r['expectancy_r']
            best_confirmed = r['signal_type']

    return results, best_confirmed


def _print_report(results, best_type):
    """Print human-readable edge proof report."""
    print()
    print("=" * 72)
    print("  APEX EDGE PROOF — Statistical Validation Report")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Multi-test correction: BH-FDR @ {_BH_FDR:.0%}  |  "
          f"DSR n_trials = {len(_TYPE_ALIASES)}")
    print("=" * 72)

    for r in results:
        stype   = r['signal_type']
        n       = r['n_real']
        wr      = f"{r['win_rate_pct']}%" if r['win_rate_pct'] is not None else "N/A"
        ci      = r['ci_95']
        pv      = r['p_value']
        pv_adj  = r['p_adjusted']
        exp     = r['expectancy_r']
        verdict = r['verdict']
        dsr_sr  = r['dsr_sharpe']
        dsr_max = r['dsr_expected_max']
        dsr_p   = r['dsr_probability']
        dsr_v   = r['dsr_verdict']
        skew    = r['return_skew']
        kurt    = r['return_excess_kurt']

        icon = {'CONFIRMED': '✓', 'MARGINAL': '~', 'NOT_PROVEN': '✗',
                'INSUFFICIENT_DATA': '?'}.get(verdict, ' ')

        print(f"\n  [{icon}] {stype}                      [{verdict}]")
        print(f"      Trades: {n}  |  Win Rate: {wr}  |  95% CI: [{ci[0]}%, {ci[1]}%]")
        adj_str = f"{pv_adj:.3f}" if pv_adj is not None else "N/A"
        print(f"      WR p-value: raw={pv:.3f}  →  BH-adjusted={adj_str}")
        print(f"      Expectancy: {exp:+.2f}R  |  avg_win={r['avg_win_r']:+.2f}R  "
              f"avg_loss={r['avg_loss_r']:.2f}R")
        print(f"      Deflated Sharpe: SR={dsr_sr:+.2f} vs E[max]={dsr_max:+.2f}  "
              f"|  P(SR>0)={dsr_p:.2f}  →  {dsr_v}")
        print(f"      Return shape: skew={skew:+.2f}  excess_kurt={kurt:+.2f}")
        if r['backtest_used']:
            print(f"      (Backtest supplement: {r['backtest_weight']:.0%} of pool — "
                  f"capped at {_BACKTEST_CAP_RATIO}× real)")

    print()
    if best_type:
        print(f"  BEST PROVEN TYPE: {best_type}  "
              f"(passed BOTH BH-FDR and Deflated Sharpe gates)")
    else:
        print("  No signal type has confirmed statistical edge yet.")
        print("  CONFIRMED requires BOTH:")
        print("    1. Win-rate p-value passes BH-FDR correction across all strategies")
        print("    2. Deflated Sharpe probability ≥ 0.95 (selection-bias adjusted)")
        print("  Accumulate more live trades before drawing conclusions.")
    print("=" * 72)
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
        'timestamp':  datetime.now(timezone.utc).isoformat() + 'Z',
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

    # ── Append to history (one snapshot per run, capped at 90 days) ───────────
    # Used by apex-edge-progress.py to compute CI-tightening rates and
    # days-to-CONFIRMED projections.
    _append_history(results, output['n_real_trades'])


def _append_history(results, total_n_real):
    """
    Append a compact snapshot of the current edge-proof to the history file.
    Each snapshot stores only the fields needed to track improvement over time:
      timestamp, per-strategy n_real, ci_width, p_value, p_adjusted,
      dsr_probability, verdict.
    """
    snapshot = {
        'timestamp':     datetime.now(timezone.utc).isoformat(),
        'total_n_real':  total_n_real,
        'by_signal_type': {},
    }
    for r in results:
        ci_lo, ci_hi = r['ci_95']
        snapshot['by_signal_type'][r['signal_type']] = {
            'n_real':           r['n_real'],
            'ci_width':         round(ci_hi - ci_lo, 2),
            'p_value':          r['p_value'],
            'p_adjusted':       r.get('p_adjusted'),
            'dsr_probability':  r.get('dsr_probability', 0.0),
            'verdict':          r['verdict'],
        }

    # Load existing history (tolerate missing/corrupted file)
    history = []
    try:
        with open(_HISTORY_FILE) as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    history.append(snapshot)

    # Trim to last _HISTORY_MAX_DAYS days
    cutoff_iso = (datetime.now(timezone.utc).timestamp()
                  - _HISTORY_MAX_DAYS * 86400)
    history = [
        s for s in history
        if datetime.fromisoformat(
            s['timestamp'].replace('Z', '+00:00')
        ).timestamp() >= cutoff_iso
    ]

    with open(_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)
    print(f"  Appended snapshot to {_HISTORY_FILE} (now {len(history)} entries)")


if __name__ == '__main__':
    main()
