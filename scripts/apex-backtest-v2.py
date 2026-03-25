#!/usr/bin/env python3
"""
Apex Backtest Engine V2 — Walk-Forward Optimisation & Statistical Validation

Enhances the existing backtest engine with:
  1. Anchored walk-forward optimisation (train/test split)
  2. Staged parameter grid search
  3. Statistical confidence intervals on all metrics
  4. Per-instrument significance testing
  5. Layer ablation study
  6. Insights generation for live scoring consumption

Usage:
  python3 apex-backtest-v2.py TREND            # Full walk-forward + insights
  python3 apex-backtest-v2.py CONTRARIAN        # Contrarian mode
  python3 apex-backtest-v2.py TREND ablation    # Layer ablation only
  python3 apex-backtest-v2.py TREND all         # Walk-forward + ablation + insights
"""
import sys
import json
import math
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

# Import from existing backtest engine
from importlib import import_module
_bt = import_module('apex-backtest')
score_signal_base = _bt.score_signal_base
score_signal = _bt.score_signal
simulate_trade = _bt.simulate_trade
calculate_backtest_atr = _bt.calculate_backtest_atr
calculate_ema = _bt.calculate_ema
calculate_rsi = _bt.calculate_rsi
fix_pence = _bt.fix_pence
vix_scale = _bt.vix_scale
BACKTEST_UNIVERSE = _bt.BACKTEST_UNIVERSE
SLIPPAGE_PCT = _bt.SLIPPAGE_PCT
SECTOR_ETFS = _bt.SECTOR_ETFS

# Import stats library
from importlib import import_module as _imp
_stats = _imp('apex-backtest-stats')
binomial_ci_pct = _stats.binomial_ci_pct
bootstrap_ci = _stats.bootstrap_ci
permutation_test = _stats.permutation_test
instrument_significance = _stats.instrument_significance
sharpe_from_r_multiples = _stats.sharpe_from_r_multiples
analyse_with_confidence = _stats.analyse_with_confidence

# Import intelligence v2
_intel_v2 = _imp('apex-backtest-intelligence-v2')
BacktestIntelligenceV2 = _intel_v2.BacktestIntelligenceV2
fetch_backtest_data = _intel_v2.fetch_backtest_data
build_intelligence = _intel_v2.build_intelligence

try:
    from apex_utils import atomic_write, safe_read
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, default=None):
        try:
            with open(p) as f: return json.load(f)
        except: return default or {}

# Output files
RESULTS_FILE  = '/home/ubuntu/.picoclaw/logs/apex-backtest-v2-results.json'
INSIGHTS_FILE = '/home/ubuntu/.picoclaw/logs/apex-backtest-v2-insights.json'


# ---------------------------------------------------------------------------
# ParameterSet — tunable backtest parameters
# ---------------------------------------------------------------------------
@dataclass
class ParameterSet:
    score_threshold: int = 7
    atr_stop_mult: float = 2.0
    atr_target_t1: float = 2.0
    atr_target_t2: float = 3.5
    max_hold_days: int = 15
    vix_block: float = 33.0

    def label(self):
        return (f"thr={self.score_threshold} stop={self.atr_stop_mult} "
                f"t1={self.atr_target_t1} t2={self.atr_target_t2} "
                f"hold={self.max_hold_days} vix={self.vix_block}")

    @staticmethod
    def default(mode='TREND'):
        if mode == 'CONTRARIAN':
            return ParameterSet(score_threshold=6, atr_stop_mult=2.5,
                                atr_target_t1=2.0, atr_target_t2=3.5,
                                max_hold_days=20, vix_block=33.0)
        return ParameterSet()

    @staticmethod
    def grid_stage1(mode='TREND'):
        """Stage 1: score_threshold × atr_stop_mult × vix_block = 80 combos."""
        sets = []
        base = ParameterSet.default(mode)
        for thresh in [5, 6, 7, 8, 9]:
            for stop in [1.5, 2.0, 2.5, 3.0]:
                for vix in [28, 30, 33, 36]:
                    ps = ParameterSet(
                        score_threshold=thresh,
                        atr_stop_mult=stop,
                        atr_target_t1=base.atr_target_t1,
                        atr_target_t2=base.atr_target_t2,
                        max_hold_days=base.max_hold_days,
                        vix_block=vix,
                    )
                    sets.append(ps)
        return sets

    @staticmethod
    def grid_stage2(best_s1, mode='TREND'):
        """Stage 2: atr_target_t1 × atr_target_t2 × max_hold_days = 80 combos."""
        sets = []
        for t1 in [1.5, 2.0, 2.5, 3.0]:
            for t2 in [2.5, 3.0, 3.5, 4.0, 5.0]:
                if t2 <= t1:
                    continue  # T2 must be > T1
                for hold in [10, 15, 20, 25]:
                    ps = ParameterSet(
                        score_threshold=best_s1.score_threshold,
                        atr_stop_mult=best_s1.atr_stop_mult,
                        atr_target_t1=t1,
                        atr_target_t2=t2,
                        max_hold_days=hold,
                        vix_block=best_s1.vix_block,
                    )
                    sets.append(ps)
        return sets


# ---------------------------------------------------------------------------
# Parameterised backtest (uses pre-fetched data, no API calls)
# ---------------------------------------------------------------------------
def simulate_trade_v2(closes, entry_idx, params, mode='TREND'):
    """
    simulate_trade with configurable parameters.
    Returns: (outcome, pnl_r, days_held, exit_reason)
    """
    if entry_idx >= len(closes) - 1:
        return 'TIMEOUT', 0, 0, 'insufficient_data'

    atr = calculate_backtest_atr(closes, entry_idx)
    if not atr or atr <= 0:
        return 'TIMEOUT', 0, 0, 'no_atr'

    entry = closes[entry_idx] * (1 + SLIPPAGE_PCT)
    stop = entry - atr * params.atr_stop_mult
    t1 = entry + atr * params.atr_target_t1
    t2 = entry + atr * params.atr_target_t2
    risk = entry - stop

    if risk <= 0:
        return 'TIMEOUT', 0, 0, 'invalid_risk'

    max_days = params.max_hold_days
    for day in range(1, min(max_days + 1, len(closes) - entry_idx)):
        price = closes[entry_idx + day]
        if price <= stop:
            return 'LOSS', -1.0, day, 'stop_hit'
        if price >= t2:
            pnl_r = round((price * (1 - SLIPPAGE_PCT) - entry) / risk, 2)
            return 'WIN', pnl_r, day, 'target2_hit'
        if price >= t1:
            stop = entry  # Breakeven ratchet

    final = closes[entry_idx + min(max_days, len(closes) - entry_idx - 1)]
    pnl_r = round((final * (1 - SLIPPAGE_PCT) - entry) / risk, 2)
    outcome = 'WIN' if pnl_r > 0 else 'LOSS'
    return outcome, pnl_r, max_days, 'time_stop'


def backtest_instrument_v2(name, closes, volumes, dates, params,
                           mode='TREND', bt_intel=None,
                           start_date=None, end_date=None,
                           use_v2_layers=True):
    """
    Backtest one instrument with configurable parameters.
    Uses pre-fetched data (no API calls).
    """
    if len(closes) < 200:
        return []

    trades = []
    for i in range(200, len(closes) - 21):
        date_str = dates[i]
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue

        # Get intelligence for this date
        if bt_intel:
            intel = bt_intel.get_intel(date_str, ticker=name, signal_type=mode)
        else:
            intel = None

        # Regime/direction filter
        if intel:
            if mode == 'TREND':
                if intel.get('regime_status') == 'BLOCKED':
                    continue
                if intel.get('direction_status') == 'BLOCKED':
                    continue
            # VIX block with configurable threshold
            vix = intel.get('vix', 20)
            if vix >= params.vix_block:
                continue

        # Score
        base_score, rsi = score_signal_base(closes[:i+1], volumes[:i+1], mode)

        # Add v2 layer adjustments if available
        adj_score = base_score
        if use_v2_layers and intel and '_bt_v2_total_adj' in intel:
            adj_score = base_score + intel['_bt_v2_total_adj']
            adj_score = max(0, min(10, adj_score))

        if adj_score < params.score_threshold:
            continue

        # Check not already in trade
        if trades and trades[-1].get('entry_idx', 0) + trades[-1].get('days_held', 20) > i:
            continue

        outcome, pnl_r, days, reason = simulate_trade_v2(closes, i, params, mode)

        trades.append({
            'date': date_str,
            'entry_idx': i,
            'name': name,
            'score': round(adj_score, 1),
            'base_score': base_score,
            'rsi': rsi,
            'entry': round(closes[i], 2),
            'outcome': outcome,
            'pnl_r': pnl_r,
            'days_held': days,
            'reason': reason,
            'vix': round(intel.get('vix', 20), 1) if intel else 20,
        })

    return trades


# ---------------------------------------------------------------------------
# Grid Search
# ---------------------------------------------------------------------------
def evaluate_params(params, instrument_data, bt_intel, start_date, end_date,
                    mode='TREND', use_v2_layers=True):
    """
    Run backtest with specific parameters on date range.
    Returns stats dict or None if insufficient trades.
    """
    all_trades = []
    for name, (closes, volumes, dates) in instrument_data.items():
        trades = backtest_instrument_v2(
            name, closes, volumes, dates, params,
            mode=mode, bt_intel=bt_intel,
            start_date=start_date, end_date=end_date,
            use_v2_layers=use_v2_layers,
        )
        all_trades.extend(trades)

    if len(all_trades) < 10:
        return None

    wins = sum(1 for t in all_trades if t['outcome'] == 'WIN')
    total = len(all_trades)
    r_multiples = [t['pnl_r'] for t in all_trades]
    expectancy = sum(r_multiples) / total

    return {
        'n_trades': total,
        'win_rate': round(wins / total * 100, 1),
        'expectancy': round(expectancy, 3),
        'params': asdict(params),
        'trades': all_trades,
    }


def grid_search(instrument_data, bt_intel, start_date, end_date,
                mode='TREND', use_v2_layers=True):
    """
    Two-stage grid search. Returns (best_params, best_stats).
    Selection: highest expectancy among sets with ≥20 trades.
    """
    # Stage 1
    candidates = ParameterSet.grid_stage1(mode)
    best_s1 = None
    best_exp_s1 = -999

    for ps in candidates:
        result = evaluate_params(ps, instrument_data, bt_intel,
                                 start_date, end_date, mode, use_v2_layers)
        if result and result['n_trades'] >= 20 and result['expectancy'] > best_exp_s1:
            best_exp_s1 = result['expectancy']
            best_s1 = ps

    if best_s1 is None:
        # Fall back to defaults if no params had 20+ trades
        best_s1 = ParameterSet.default(mode)

    # Stage 2
    candidates2 = ParameterSet.grid_stage2(best_s1, mode)
    best_ps = best_s1
    best_exp = best_exp_s1

    for ps in candidates2:
        result = evaluate_params(ps, instrument_data, bt_intel,
                                 start_date, end_date, mode, use_v2_layers)
        if result and result['n_trades'] >= 20 and result['expectancy'] > best_exp:
            best_exp = result['expectancy']
            best_ps = ps

    # Get final stats with best params
    final = evaluate_params(best_ps, instrument_data, bt_intel,
                            start_date, end_date, mode, use_v2_layers)

    return best_ps, final


# ---------------------------------------------------------------------------
# Walk-Forward Optimisation
# ---------------------------------------------------------------------------
def run_walkforward_v2(mode='TREND', n_windows=6, train_months=18,
                       test_months=6, period='5y', data=None):
    """
    Anchored walk-forward optimisation.

    For each window:
      1. Grid search on training data
      2. Validate on out-of-sample test data
      3. Record both metrics with confidence intervals
    """
    now = datetime.now(timezone.utc)
    print(f"\n{'='*70}")
    print(f"APEX BACKTEST V2 — WALK-FORWARD OPTIMISATION")
    print(f"Mode: {mode} | Windows: {n_windows} | Train: {train_months}m | Test: {test_months}m")
    print(f"{'='*70}")

    # Fetch data if not provided
    if data is None:
        data = fetch_backtest_data(period=period)

    bt_intel = build_intelligence(data)

    # Pre-process instrument data into {name: (closes, volumes, dates)}
    instrument_data = {}
    for name, yahoo in BACKTEST_UNIVERSE.items():
        inst = data['instrument_closes'].get(name, [])
        if len(inst) < 200:
            continue
        closes = [c for _, c in inst]
        dates = [d for d, _ in inst]
        # Volumes not in close-only data — use synthetic
        volumes = [1.0] * len(closes)
        instrument_data[name] = (closes, volumes, dates)

    print(f"  Instruments loaded: {len(instrument_data)}")

    # Calculate window boundaries
    period_days = {'1y': 365, '2y': 730, '3y': 1095, '5y': 1825, '10y': 3650}
    lookback = period_days.get(period, 1825)
    end_date = now.date()
    start_date = end_date - timedelta(days=lookback)

    window_results = []
    all_oos_trades = []
    param_history = []

    for i in range(n_windows):
        # Anchored training: always starts at start_date, expands
        train_end = start_date + timedelta(days=(train_months + i * test_months) * 30)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_months * 30)
        if test_end > end_date:
            test_end = end_date

        train_start_str = start_date.isoformat()
        train_end_str = train_end.isoformat()
        test_start_str = test_start.isoformat()
        test_end_str = test_end.isoformat()

        print(f"\n  Window {i+1}/{n_windows}: "
              f"Train [{train_start_str[:7]}→{train_end_str[:7]}] "
              f"Test [{test_start_str[:7]}→{test_end_str[:7]}]")

        # Grid search on training data
        best_params, train_stats = grid_search(
            instrument_data, bt_intel,
            train_start_str, train_end_str, mode
        )

        # Validate on test data with best params
        test_result = evaluate_params(
            best_params, instrument_data, bt_intel,
            test_start_str, test_end_str, mode
        )

        if train_stats:
            print(f"    Train: {train_stats['n_trades']} trades, "
                  f"WR={train_stats['win_rate']}%, E={train_stats['expectancy']:.3f}R")
        if test_result:
            oos_analysis = analyse_with_confidence(test_result['trades'])
            all_oos_trades.extend(test_result['trades'])

            edge = 'EDGE' if test_result['expectancy'] > 0 else 'NO EDGE'
            print(f"    Test:  {test_result['n_trades']} trades, "
                  f"WR={test_result['win_rate']}%, E={test_result['expectancy']:.3f}R [{edge}]")
            print(f"    Best params: thr={best_params.score_threshold} "
                  f"stop={best_params.atr_stop_mult} "
                  f"t1={best_params.atr_target_t1} t2={best_params.atr_target_t2}")
        else:
            oos_analysis = {}
            print(f"    Test:  insufficient trades")

        param_history.append(best_params.score_threshold)

        window_results.append({
            'window': i + 1,
            'train_start': train_start_str,
            'train_end': train_end_str,
            'test_start': test_start_str,
            'test_end': test_end_str,
            'best_params': asdict(best_params),
            'in_sample': {
                'n_trades': train_stats['n_trades'] if train_stats else 0,
                'win_rate': train_stats['win_rate'] if train_stats else 0,
                'expectancy': train_stats['expectancy'] if train_stats else 0,
            },
            'out_of_sample': oos_analysis if oos_analysis else {
                'n_trades': 0, 'win_rate': 0, 'expectancy': 0,
            },
        })

    # Aggregate OOS analysis
    print(f"\n{'='*70}")
    print(f"AGGREGATE OUT-OF-SAMPLE RESULTS")
    print(f"{'='*70}")

    if all_oos_trades:
        agg = analyse_with_confidence(all_oos_trades)
        print(f"  Total OOS trades: {agg['n_trades']}")
        print(f"  Win rate:         {agg['win_rate']}% "
              f"[{agg['win_rate_ci'][0]}, {agg['win_rate_ci'][1]}] 95% CI")
        print(f"  Expectancy:       {agg['expectancy']:.3f}R "
              f"[{agg['expectancy_ci'][0]}, {agg['expectancy_ci'][1]}] 95% CI")
        print(f"  Profit factor:    {agg['profit_factor']}")
        print(f"  Sharpe:           {agg['sharpe']}")
        print(f"  p-value vs 50%:   {agg['p_value_vs_random']}")
        print(f"  Significant:      {'YES' if agg['significant'] else 'NO'}")
    else:
        agg = {}
        print("  No OOS trades generated")

    # Parameter stability
    if param_history:
        from collections import Counter
        mode_val = Counter(param_history).most_common(1)[0][0]
        param_std = (sum((p - sum(param_history)/len(param_history))**2
                         for p in param_history) / len(param_history)) ** 0.5
        stable = param_std < 1.5
        print(f"\n  Parameter stability:")
        print(f"    Score threshold history: {param_history}")
        print(f"    Mode: {mode_val}, Std: {param_std:.1f}")
        print(f"    {'STABLE' if stable else 'UNSTABLE'}")
    else:
        mode_val = 7
        param_std = 0
        stable = True

    # Per-instrument significance
    print(f"\n  Per-instrument significance:")
    inst_analysis = {}
    inst_trades = {}
    for t in all_oos_trades:
        name = t.get('name', 'UNKNOWN')
        if name not in inst_trades:
            inst_trades[name] = {'wins': 0, 'total': 0}
        inst_trades[name]['total'] += 1
        if t['outcome'] == 'WIN':
            inst_trades[name]['wins'] += 1

    include_list = []
    exclude_list = []
    for name, counts in sorted(inst_trades.items(), key=lambda x: x[1]['total'], reverse=True):
        sig = instrument_significance(counts['wins'], counts['total'])
        inst_analysis[name] = sig
        icon = {'INCLUDE': '+', 'MARGINAL': '~', 'EXCLUDE': '-', 'INSUFFICIENT': '?'}
        print(f"    [{icon.get(sig['verdict'], '?')}] {name:8}: "
              f"{sig['win_rate']}% ({sig['n']} trades) "
              f"CI [{sig['ci'][0]}, {sig['ci'][1]}] p={sig['p_value']:.3f} "
              f"→ {sig['verdict']}")
        if sig['verdict'] == 'INCLUDE':
            include_list.append(name)
        elif sig['verdict'] == 'EXCLUDE':
            exclude_list.append(name)

    # Verdict
    print(f"\n{'='*70}")
    if agg and agg.get('expectancy', 0) > 0.1 and agg.get('p_value_vs_random', 1) < 0.10:
        print("VERDICT: VALIDATED EDGE — OOS expectancy positive with statistical significance")
    elif agg and agg.get('expectancy', 0) > 0:
        print("VERDICT: MARGINAL EDGE — OOS positive but not yet statistically significant")
    else:
        print("VERDICT: NO EDGE — OOS expectancy non-positive")
    print(f"{'='*70}")

    # Build output
    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'version': 2,
        'mode': mode,
        'walk_forward': {
            'n_windows': n_windows,
            'train_months': train_months,
            'test_months': test_months,
            'windows': window_results,
            'aggregate_oos': agg,
            'parameter_stability': {
                'score_threshold_values': param_history,
                'score_threshold_mode': mode_val,
                'score_threshold_std': round(param_std, 1),
                'stable': stable,
            },
        },
        'instrument_analysis': inst_analysis,
        'instruments_significant': include_list,
        'instruments_excluded': exclude_list,
    }

    atomic_write(RESULTS_FILE, output)
    print(f"\nResults saved to {RESULTS_FILE}")

    return output, data


# ---------------------------------------------------------------------------
# Layer Ablation Study
# ---------------------------------------------------------------------------
def run_ablation(mode='TREND', data=None, params=None):
    """
    Run OOS test with incrementally enabled layers.
    Tests contribution of each v2 layer.
    """
    now = datetime.now(timezone.utc)
    print(f"\n{'='*70}")
    print(f"LAYER ABLATION STUDY — {mode}")
    print(f"{'='*70}")

    if data is None:
        data = fetch_backtest_data(period='5y')

    bt_intel = build_intelligence(data)
    if params is None:
        params = ParameterSet.default(mode)

    # Pre-process instrument data
    instrument_data = {}
    for name, yahoo in BACKTEST_UNIVERSE.items():
        inst = data['instrument_closes'].get(name, [])
        if len(inst) < 200:
            continue
        closes = [c for _, c in inst]
        dates = [d for d, _ in inst]
        volumes = [1.0] * len(closes)
        instrument_data[name] = (closes, volumes, dates)

    # Use last 2 years as test period
    end_date = now.date()
    test_start = (end_date - timedelta(days=730)).isoformat()
    test_end = end_date.isoformat()

    configs = [
        ("Base only (4-factor)", False),
        ("Base + V2 layers (RS+MTF+FRED+Sentiment)", True),
    ]

    results = {}
    for label, use_v2 in configs:
        result = evaluate_params(
            params, instrument_data, bt_intel,
            test_start, test_end, mode, use_v2_layers=use_v2
        )
        if result:
            analysis = analyse_with_confidence(result['trades'])
            results[label] = analysis
            print(f"\n  {label}:")
            print(f"    Trades: {analysis['n_trades']}, WR: {analysis['win_rate']}% "
                  f"[{analysis['win_rate_ci'][0]}, {analysis['win_rate_ci'][1]}]")
            print(f"    Expectancy: {analysis['expectancy']:.3f}R "
                  f"[{analysis['expectancy_ci'][0]}, {analysis['expectancy_ci'][1]}]")
            print(f"    Sharpe: {analysis['sharpe']}")
        else:
            print(f"\n  {label}: insufficient trades")

    # Calculate lift
    if len(results) == 2:
        labels = list(results.keys())
        base_exp = results[labels[0]].get('expectancy', 0)
        v2_exp = results[labels[1]].get('expectancy', 0)
        lift = v2_exp - base_exp
        print(f"\n  V2 layers lift: {lift:+.3f}R expectancy")
        if lift > 0:
            print(f"  V2 layers IMPROVE out-of-sample performance")
        else:
            print(f"  V2 layers do NOT improve OOS (may be noise)")

    return results


# ---------------------------------------------------------------------------
# Insights Generation (Phase 4)
# ---------------------------------------------------------------------------
def generate_insights(wf_output: dict, mode: str = 'TREND') -> dict:
    """
    Generate apex-backtest-v2-insights.json for live scoring consumption.
    Backward-compatible with existing apex-backtest-insights.json format.
    """
    now = datetime.now(timezone.utc)
    agg = wf_output.get('walk_forward', {}).get('aggregate_oos', {})
    stability = wf_output.get('walk_forward', {}).get('parameter_stability', {})
    windows = wf_output.get('walk_forward', {}).get('windows', [])

    # Find most recent window's optimal params
    if windows:
        latest_params = windows[-1].get('best_params', asdict(ParameterSet.default(mode)))
    else:
        latest_params = asdict(ParameterSet.default(mode))

    insights = {
        'version': 2,
        'generated': now.strftime('%Y-%m-%d'),
        'mode': mode,
        'oos_validation': {
            'mean_oos_win_rate': agg.get('win_rate', 0),
            'mean_oos_win_rate_ci': agg.get('win_rate_ci', [0, 0]),
            'mean_oos_expectancy': agg.get('expectancy', 0),
            'mean_oos_expectancy_ci': agg.get('expectancy_ci', [0, 0]),
            'oos_sharpe': agg.get('sharpe', 0),
            'n_oos_trades': agg.get('n_trades', 0),
            'p_value_vs_random': agg.get('p_value_vs_random', 1.0),
            'parameter_stability': 'STABLE' if stability.get('stable') else 'UNSTABLE',
        },
        'optimal_params': {
            mode: latest_params,
        },
        # Backward-compatible fields (consumed by apex_scoring.py)
        'backtest_boost_instruments': wf_output.get('instruments_significant', []),
        'backtest_penalise_instruments': wf_output.get('instruments_excluded', []),
        'best_instruments': wf_output.get('instruments_significant', []),
        'worst_instruments': wf_output.get('instruments_excluded', []),
        'instrument_detail': wf_output.get('instrument_analysis', {}),
    }

    atomic_write(INSIGHTS_FILE, insights)
    print(f"\nInsights saved to {INSIGHTS_FILE}")
    return insights


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run(mode='TREND', cmd='walkforward'):
    """Main entry: run walk-forward optimisation and generate insights."""

    if cmd == 'ablation':
        return run_ablation(mode)

    # Walk-forward
    wf_output, data = run_walkforward_v2(mode)

    # Generate insights
    insights = generate_insights(wf_output, mode)

    # Ablation if requested
    if cmd == 'all':
        # Use most recent window's params for ablation
        windows = wf_output.get('walk_forward', {}).get('windows', [])
        if windows:
            p = windows[-1].get('best_params', {})
            params = ParameterSet(**p)
        else:
            params = ParameterSet.default(mode)
        run_ablation(mode, data=data, params=params)

    return wf_output


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'TREND'
    cmd = sys.argv[2] if len(sys.argv) > 2 else 'walkforward'
    run(mode, cmd)
