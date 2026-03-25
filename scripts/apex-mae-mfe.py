#!/usr/bin/env python3
"""
Apex MAE/MFE Analysis Engine
Maximum Adverse Excursion & Maximum Favorable Excursion analysis.

Without intraday tick data, we approximate using R-multiples from closed trades:
  MAE proxy (losers): |r_achieved| — how far against entry before closing
  MFE proxy (winners): r_achieved — how far in favor before closing

This gives us:
  1. Optimal T1/T2 split from the empirical distribution of winning R-multiples
  2. Stop efficiency — are stops too tight (stops at exactly -1R) or too wide?
  3. Calibrated ATR multiplier suggestions for stops and targets
  4. EV comparison: T1/T2 model vs empirical distribution

Output: apex-mae-mfe-calibration.json
"""
import json
import math
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
    def log_warning(m): print(f'WARNING: {m}')

PARAM_FILE        = '/home/ubuntu/.picoclaw/logs/apex-param-log.json'
OUTCOMES_FILE     = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
BACKTEST_FILE     = '/home/ubuntu/.picoclaw/logs/apex-backtest-results.json'
CALIBRATION_FILE  = '/home/ubuntu/.picoclaw/logs/apex-mae-mfe-calibration.json'

MIN_TRADES_ANALYSIS = 10
DEFAULT_T1_R        = 2.0   # Default T1 target in R-multiples
DEFAULT_T2_R        = 3.5   # Default T2 target in R-multiples


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def load_all_trades() -> list:
    """
    Load closed trades from all available sources.
    Returns list of dicts with at minimum: outcome, r_achieved, signal_type.
    """
    trades = []
    seen   = set()

    def _add(source_trades):
        for t in source_trades:
            # Need at minimum r_achieved (or computable from pnl/risk)
            r = t.get('r_achieved', t.get('r', None))
            if r is None and 'pnl' in t and 'risk' in t and t.get('risk', 0) > 0:
                r = t['pnl'] / t['risk']
            if r is None:
                continue
            outcome = t.get('outcome', 'WIN' if float(r) > 0 else 'LOSS')
            key     = (t.get('name', ''), t.get('entry_date', ''), round(float(r), 3))
            if key in seen:
                continue
            seen.add(key)
            trades.append({
                'outcome':     outcome,
                'r_achieved':  round(float(r), 3),
                'signal_type': t.get('signal_type', 'UNKNOWN'),
                'entry':       t.get('entry', 0),
                'stop':        t.get('stop', 0),
                'target1':     t.get('target1', 0),
                'target2':     t.get('target2', 0),
                'name':        t.get('name', '?'),
                'source':      t.get('_source', 'unknown'),
            })

    # 1. Backtest results (most data)
    try:
        bt = safe_read(BACKTEST_FILE, {})
        bt_trades = bt.get('trades', [])
        for t in bt_trades:
            t['_source'] = 'backtest'
        _add(bt_trades)
    except Exception as e:
        log_warning(f"MAE/MFE: backtest load failed: {e}")

    # 2. Live param log
    try:
        log   = safe_read(PARAM_FILE, {'signals': []})
        sigs  = [s for s in log.get('signals', [])
                 if s.get('outcome') in ('WIN', 'LOSS') and 'r_achieved' in s]
        for s in sigs:
            s['_source'] = 'live'
        _add(sigs)
    except Exception as e:
        log_warning(f"MAE/MFE: live data load failed: {e}")

    # 3. Outcomes file
    try:
        db = safe_read(OUTCOMES_FILE, {'trades': []})
        for t in db.get('trades', []):
            t['_source'] = 'outcomes'
        _add(db.get('trades', []))
    except Exception as e:
        log_warning(f"MAE/MFE: outcomes load failed: {e}")

    return trades


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------
def _percentile(values: list, p: float) -> float:
    """Linear interpolation percentile."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    idx = p * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 4)


def _mean(values): return sum(values) / len(values) if values else 0.0
def _std(values, mu=None):
    if len(values) < 2: return 0.0
    m = mu if mu is not None else _mean(values)
    return math.sqrt(sum((x - m)**2 for x in values) / (len(values) - 1))


# ---------------------------------------------------------------------------
# MFE Distribution Analysis (winners)
# ---------------------------------------------------------------------------
def analyse_mfe(wins: list, t1_r: float = DEFAULT_T1_R, t2_r: float = DEFAULT_T2_R) -> dict:
    """
    Analyse winning trade R-multiple distribution to calibrate T1/T2 split.

    wins: list of r_achieved values for winning trades (all > 0)
    """
    if len(wins) < MIN_TRADES_ANALYSIS:
        return {
            'n': len(wins),
            'insufficient': True,
            't1_fraction': 0.60,  # conservative default
            't2_fraction': 0.40,
            'source': 'default (insufficient data)',
        }

    n          = len(wins)
    mu         = _mean(wins)
    sigma      = _std(wins, mu)
    p5, p25, p50, p75, p95 = [_percentile(wins, p) for p in [0.05, 0.25, 0.50, 0.75, 0.95]]

    # Count how many reached T1 and T2
    reached_t1 = sum(1 for r in wins if r >= t1_r)
    reached_t2 = sum(1 for r in wins if r >= t2_r)

    # Of wins, what fraction ran to T2?
    t2_frac = round(reached_t2 / n, 3)
    t1_frac = round(1.0 - t2_frac, 3)

    # Optimal single-exit R (maximises captured R)
    # Simple: median is the level that splits wins evenly — targets there
    optimal_exit_r = p50

    # Optimal T1 level: where would a partial exit (50% at T1, 50% running) maximise EV?
    # EV(T1_level) = mean(min(r, T1_level)) × 0.5 + mean(r for r > T1_level) × 0.5
    best_t1_ev  = -999
    best_t1_val = t1_r
    for candidate_t1 in [1.5, 2.0, 2.5, 3.0]:
        partial   = [min(r, candidate_t1) for r in wins]
        remaining = [r for r in wins if r > candidate_t1]
        ev_val = (
            _mean(partial) * 0.5
            + (_mean(remaining) if remaining else candidate_t1) * 0.5
        )
        if ev_val > best_t1_ev:
            best_t1_ev  = ev_val
            best_t1_val = candidate_t1

    # Histogram bucketed
    buckets = {}
    for bucket_top in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 99.0]:
        label = f"<{bucket_top}R"
        buckets[label] = sum(1 for r in wins if r < bucket_top)
    # Make cumulative
    sorted_wins = sorted(wins)

    return {
        'n':              n,
        'insufficient':   False,
        'mean_r':         round(mu, 3),
        'std_r':          round(sigma, 3),
        'p5':             p5,
        'p25':            p25,
        'p50':            p50,
        'p75':            p75,
        'p95':            p95,
        'reached_t1_pct': round(reached_t1 / n * 100, 1),
        'reached_t2_pct': round(reached_t2 / n * 100, 1),
        't1_fraction':    t1_frac,
        't2_fraction':    t2_frac,
        't1_r_used':      t1_r,
        't2_r_used':      t2_r,
        'optimal_exit_r': round(optimal_exit_r, 2),
        'optimal_t1_r':   round(best_t1_val, 2),
        'source': f'empirical ({n} winning trades)',
    }


# ---------------------------------------------------------------------------
# MAE Distribution Analysis (losers)
# ---------------------------------------------------------------------------
def analyse_mae(losses: list) -> dict:
    """
    Analyse losing trade R-multiple distribution.

    losses: list of r_achieved values for losing trades (all ≤ 0, stored as negative)
    We work with absolute values internally.
    """
    abs_losses = [abs(r) for r in losses if r <= 0]
    if len(abs_losses) < MIN_TRADES_ANALYSIS:
        return {
            'n': len(abs_losses),
            'insufficient': True,
            'stop_efficiency': 'UNKNOWN',
            'avg_loss_r': 1.0,
        }

    n   = len(abs_losses)
    mu  = _mean(abs_losses)
    p50 = _percentile(abs_losses, 0.50)
    p90 = _percentile(abs_losses, 0.90)
    p99 = _percentile(abs_losses, 0.99)

    # Stop efficiency: what fraction of losses were stopped at exactly 1R?
    at_stop    = sum(1 for r in abs_losses if 0.90 <= r <= 1.10)
    early_exit = sum(1 for r in abs_losses if r < 0.90)   # closed before stop (partial/early)
    beyond_stop= sum(1 for r in abs_losses if r > 1.10)   # slippage or stop not honoured

    at_stop_pct     = round(at_stop     / n * 100, 1)
    early_exit_pct  = round(early_exit  / n * 100, 1)
    beyond_stop_pct = round(beyond_stop / n * 100, 1)

    # Stop assessment
    if beyond_stop_pct > 20:
        stop_status = 'SLIPPAGE_RISK'    # Many losses exceed 1R — stop not reliable
        stop_note   = f"{beyond_stop_pct}% of losses exceed 1R — check stop execution"
    elif early_exit_pct > 40:
        stop_status = 'STOPS_TOO_TIGHT'  # Many close before reaching stop
        stop_note   = f"{early_exit_pct}% of losses close before stop — stops may be too tight"
    elif at_stop_pct > 70:
        stop_status = 'STOPS_MECHANICAL' # Most hit stop cleanly
        stop_note   = f"{at_stop_pct}% of losses hit stop exactly — mechanical exits working"
    else:
        stop_status = 'MIXED'
        stop_note   = f"Mixed stop behaviour: {at_stop_pct}% at stop, {early_exit_pct}% early, {beyond_stop_pct}% beyond"

    return {
        'n':               n,
        'insufficient':    False,
        'mean_loss_r':     round(mu, 3),
        'median_loss_r':   round(p50, 3),
        'p90_loss_r':      round(p90, 3),
        'p99_loss_r':      round(p99, 3),
        'at_stop_pct':     at_stop_pct,
        'early_exit_pct':  early_exit_pct,
        'beyond_stop_pct': beyond_stop_pct,
        'stop_efficiency': stop_status,
        'stop_note':       stop_note,
        'avg_loss_r':      round(mu, 3),
    }


# ---------------------------------------------------------------------------
# EV Comparison: Model vs Empirical
# ---------------------------------------------------------------------------
def compare_ev_model_vs_empirical(wins_r: list, losses_r: list,
                                  t1_frac: float, t1_r: float, t2_r: float) -> dict:
    """
    Compare EV from simplified T1/T2 model vs empirical distribution.

    Model EV  = win_rate × (t1_frac × t1_r + t2_frac × t2_r) - loss_rate × avg_|loss_r|
    Actual EV = win_rate × mean(wins_r) - loss_rate × mean(|losses_r|)
    """
    if not wins_r or not losses_r:
        return {}

    total_n    = len(wins_r) + len(losses_r)
    win_rate   = len(wins_r) / total_n
    loss_rate  = 1 - win_rate
    avg_win_r  = _mean(wins_r)
    avg_loss_r = _mean([abs(r) for r in losses_r])

    # Model EV
    model_avg_win = t1_frac * t1_r + (1 - t1_frac) * t2_r
    model_ev      = win_rate * model_avg_win - loss_rate * avg_loss_r

    # Empirical EV
    empirical_ev  = win_rate * avg_win_r - loss_rate * avg_loss_r

    # Calibration error: how much does the model over/underestimate?
    ev_error      = round(model_ev - empirical_ev, 4)
    ev_error_pct  = round(ev_error / max(abs(empirical_ev), 0.001) * 100, 1)

    return {
        'win_rate':          round(win_rate, 4),
        'avg_win_r':         round(avg_win_r, 3),
        'avg_loss_r':        round(avg_loss_r, 3),
        'model_avg_win_r':   round(model_avg_win, 3),
        'model_ev':          round(model_ev, 4),
        'empirical_ev':      round(empirical_ev, 4),
        'ev_model_error':    ev_error,
        'ev_model_error_pct': ev_error_pct,
        'model_overestimates': ev_error > 0,
    }


# ---------------------------------------------------------------------------
# Per-signal-type calibration
# ---------------------------------------------------------------------------
def calibrate_signal_type(trades: list, sig_type: str) -> dict:
    """Run full MAE/MFE analysis for a specific signal type."""
    filtered = [t for t in trades if t.get('signal_type') == sig_type]
    if len(filtered) < MIN_TRADES_ANALYSIS:
        return {'n': len(filtered), 'insufficient': True}

    wins   = [t['r_achieved'] for t in filtered if t['outcome'] == 'WIN']
    losses = [t['r_achieved'] for t in filtered if t['outcome'] == 'LOSS']

    # Derive T1/T2 R-levels from trade data if available
    t1_r_vals = [t['target1'] / (t['entry'] - t['stop'])
                 for t in filtered
                 if t.get('entry',0) > 0 and t.get('stop',0) > 0
                 and t.get('target1',0) > 0 and t['entry'] != t['stop']]
    t2_r_vals = [t['target2'] / (t['entry'] - t['stop'])
                 for t in filtered
                 if t.get('entry',0) > 0 and t.get('stop',0) > 0
                 and t.get('target2',0) > 0 and t['entry'] != t['stop']]

    t1_r = round(_mean(t1_r_vals), 2) if t1_r_vals else DEFAULT_T1_R
    t2_r = round(_mean(t2_r_vals), 2) if t2_r_vals else DEFAULT_T2_R

    mfe  = analyse_mfe(wins, t1_r, t2_r)
    mae  = analyse_mae(losses)
    ev_cmp = compare_ev_model_vs_empirical(wins, losses,
                                           mfe.get('t1_fraction', 0.60),
                                           t1_r, t2_r)

    # ATR calibration suggestion
    # If avg_loss_r >> 1.0, stops may be placed with ATR mult too large
    # If avg_loss_r << 1.0, exits are happening before stops
    stop_suggestion = None
    if mae.get('mean_loss_r', 1.0) < 0.75:
        stop_suggestion = 'Consider tightening ATR stop multiplier — losses closing before stop'
    elif mae.get('mean_loss_r', 1.0) > 1.25:
        stop_suggestion = 'Consider tightening ATR stop — average loss exceeds 1R (slippage or gaps)'

    target_suggestion = None
    if mfe.get('reached_t2_pct', 0) < 20:
        target_suggestion = f'T2 reached only {mfe.get("reached_t2_pct",0)}% of wins — consider lowering T2 or trailing to T1'
    elif mfe.get('reached_t2_pct', 0) > 60:
        target_suggestion = f'T2 reached {mfe.get("reached_t2_pct",0)}% of wins — T2 may be too conservative, could trail higher'

    return {
        'signal_type':        sig_type,
        'n_total':            len(filtered),
        'n_wins':             len(wins),
        'n_losses':           len(losses),
        't1_r':               t1_r,
        't2_r':               t2_r,
        'mfe':                mfe,
        'mae':                mae,
        'ev_comparison':      ev_cmp,
        'stop_suggestion':    stop_suggestion,
        'target_suggestion':  target_suggestion,
    }


# ---------------------------------------------------------------------------
# Aggregate summary across all signal types
# ---------------------------------------------------------------------------
def run_full_analysis(verbose: bool = True) -> dict:
    """Load trades, run MAE/MFE analysis, output calibration."""
    trades = load_all_trades()
    now    = datetime.now(timezone.utc)

    if verbose:
        print(f"\n=== MAE/MFE CALIBRATION ENGINE ===")
        print(f"Loaded {len(trades)} closed trades\n")

    signal_types = sorted(set(t['signal_type'] for t in trades
                              if t['signal_type'] != 'UNKNOWN'))
    if not signal_types:
        signal_types = ['TREND', 'CONTRARIAN', 'INVERSE', 'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE']

    calibrations = {}
    for sig_type in signal_types:
        cal = calibrate_signal_type(trades, sig_type)
        calibrations[sig_type] = cal

        if not verbose:
            continue
        if cal.get('insufficient'):
            print(f"  {sig_type:20} — INSUFFICIENT DATA ({cal.get('n', 0)} trades)")
            continue

        mfe = cal.get('mfe', {})
        mae = cal.get('mae', {})
        ev  = cal.get('ev_comparison', {})

        print(f"  {sig_type:20} ({cal['n_total']} trades, {cal['n_wins']} wins / {cal['n_losses']} losses)")
        if not mfe.get('insufficient'):
            print(f"    MFE: mean={mfe.get('mean_r','?'):.2f}R  p50={mfe.get('p50','?'):.2f}R  "
                  f"p95={mfe.get('p95','?'):.2f}R  "
                  f"→T1: {mfe.get('reached_t1_pct','?')}%  →T2: {mfe.get('reached_t2_pct','?')}%  "
                  f"split: {round(mfe.get('t1_fraction',0.6)*100)}%/{round(mfe.get('t2_fraction',0.4)*100)}% T1/T2")
        if not mae.get('insufficient'):
            print(f"    MAE: mean={mae.get('mean_loss_r','?'):.2f}R  "
                  f"stop status={mae.get('stop_efficiency','?')}  "
                  f"beyond_stop={mae.get('beyond_stop_pct','?')}%")
        if ev:
            error_sign = '+' if ev.get('model_overestimates') else ''
            print(f"    EV: empirical={ev.get('empirical_ev','?'):.3f}R  "
                  f"model={ev.get('model_ev','?'):.3f}R  "
                  f"error={error_sign}{ev.get('ev_model_error','?'):.3f}R "
                  f"({ev.get('ev_model_error_pct','?')}%)")
        if cal.get('stop_suggestion'):
            print(f"    STOP: {cal['stop_suggestion']}")
        if cal.get('target_suggestion'):
            print(f"    TARGET: {cal['target_suggestion']}")
        print()

    # All-trades aggregate
    all_wins   = [t['r_achieved'] for t in trades if t['outcome'] == 'WIN']
    all_losses = [t['r_achieved'] for t in trades if t['outcome'] == 'LOSS']
    agg_mfe    = analyse_mfe(all_wins)
    agg_mae    = analyse_mae(all_losses)
    agg_ev     = compare_ev_model_vs_empirical(
        all_wins, all_losses,
        agg_mfe.get('t1_fraction', 0.60), DEFAULT_T1_R, DEFAULT_T2_R
    )

    if verbose and len(all_wins) + len(all_losses) >= MIN_TRADES_ANALYSIS:
        print(f"  AGGREGATE ({len(all_wins)+len(all_losses)} trades)")
        print(f"    MFE: mean={agg_mfe.get('mean_r',0):.2f}R  T1/T2 split: "
              f"{round(agg_mfe.get('t1_fraction',0.6)*100)}%/{round(agg_mfe.get('t2_fraction',0.4)*100)}%")
        print(f"    MAE: stop status={agg_mae.get('stop_efficiency','?')}")
        if agg_ev:
            print(f"    EV model error: {agg_ev.get('ev_model_error_pct','?')}% "
                  f"({'overestimates' if agg_ev.get('model_overestimates') else 'underestimates'})")

    output = {
        'timestamp':       now.strftime('%Y-%m-%d %H:%M UTC'),
        'n_trades_total':  len(trades),
        'n_wins_total':    len(all_wins),
        'n_losses_total':  len(all_losses),
        'aggregate': {
            'mfe':    agg_mfe,
            'mae':    agg_mae,
            'ev_cmp': agg_ev,
        },
        'by_signal_type':  calibrations,
        # Flat summary consumed by get_t1_split() in apex-expected-value.py
        't1_splits': {
            sig: cal.get('mfe', {}).get('t1_fraction', 0.60)
            for sig, cal in calibrations.items()
            if not cal.get('insufficient') and not cal.get('mfe', {}).get('insufficient')
        },
        # Aggregate t1 split
        'aggregate_t1_fraction': agg_mfe.get('t1_fraction', 0.60),
        'aggregate_t2_fraction': agg_mfe.get('t2_fraction', 0.40),
    }

    atomic_write(CALIBRATION_FILE, output)
    if verbose:
        print(f"\nCalibration saved to {CALIBRATION_FILE}")

    return output


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if '--test' in sys.argv:
        print("MAE/MFE Engine — Self Tests")
        print("=" * 50)

        # 1. MFE analysis with synthetic data
        import random
        rng = random.Random(42)
        wins = [rng.gauss(2.2, 1.0) for _ in range(50)]
        wins = [max(0.1, w) for w in wins]  # all positive
        mfe  = analyse_mfe(wins, t1_r=2.0, t2_r=3.5)
        assert mfe['n'] == 50
        assert 0 <= mfe['t1_fraction'] <= 1
        assert 0 <= mfe['t2_fraction'] <= 1
        assert abs(mfe['t1_fraction'] + mfe['t2_fraction'] - 1.0) < 0.001
        assert mfe['mean_r'] > 0
        print(f"analyse_mfe (n=50): mean={mfe['mean_r']:.2f}R, "
              f"split={round(mfe['t1_fraction']*100)}%/{round(mfe['t2_fraction']*100)}%: PASS")

        # 2. MAE analysis — mechanical stops (all exactly at -1R)
        losses_mechanical = [-1.0] * 50
        mae = analyse_mae(losses_mechanical)
        assert mae['stop_efficiency'] == 'STOPS_MECHANICAL'
        assert mae['at_stop_pct'] == 100.0
        print(f"analyse_mae (mechanical stops): status={mae['stop_efficiency']}: PASS")

        # 3. MAE analysis — slippage risk
        losses_slip = [-1.0] * 30 + [-1.5, -1.3, -1.4, -2.0, -1.6] * 4
        mae2 = analyse_mae(losses_slip)
        assert mae2['stop_efficiency'] == 'SLIPPAGE_RISK'
        print(f"analyse_mae (slippage): status={mae2['stop_efficiency']}: PASS")

        # 4. EV comparison
        ev_cmp = compare_ev_model_vs_empirical(wins, [-abs(l) for l in losses_mechanical],
                                               t1_frac=0.6, t1_r=2.0, t2_r=3.5)
        assert 'empirical_ev' in ev_cmp
        assert 'model_ev' in ev_cmp
        print(f"compare_ev: empirical={ev_cmp['empirical_ev']:.3f}R, "
              f"model={ev_cmp['model_ev']:.3f}R: PASS")

        # 5. Per-signal calibration with synthetic trades
        test_trades = []
        for i in range(30):
            r = rng.gauss(1.5, 1.2)
            test_trades.append({
                'outcome': 'WIN' if r > 0 else 'LOSS',
                'r_achieved': round(r, 3),
                'signal_type': 'TREND',
                'entry': 100, 'stop': 95, 'target1': 110, 'target2': 120, 'name': 'TEST',
            })
        cal = calibrate_signal_type(test_trades, 'TREND')
        assert 'mfe' in cal or cal.get('insufficient')
        print(f"calibrate_signal_type (TREND, n={len(test_trades)}): PASS")

        # 6. Insufficient data
        few_trades = test_trades[:5]
        cal_few = calibrate_signal_type(few_trades, 'TREND')
        assert cal_few.get('insufficient')
        print("calibrate_signal_type (insufficient): PASS")

        # 7. Percentile correctness
        data = list(range(1, 101))  # 1..100
        assert _percentile(data, 0.50) == 50.5  # median
        assert _percentile(data, 0.95) == 95.05
        print(f"_percentile: median={_percentile(data,0.5)}, p95={_percentile(data,0.95):.2f}: PASS")

        print("\n" + "=" * 50)
        print("ALL TESTS PASSED")

    else:
        run_full_analysis(verbose=True)
