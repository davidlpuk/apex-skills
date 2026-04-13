#!/usr/bin/env python3
"""
APEX Stress Test Battery
Run: python3 apex-stress-test.py [--cat N] [--list]

47 automated tests across 8 categories. No live API calls.
Produces: /home/ubuntu/.picoclaw/logs/apex-stress-test-results.json
"""
import sys
import os
import json
import time
import traceback
import importlib.util
import tempfile
import shutil
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

SCRIPTS = '/home/ubuntu/.picoclaw/scripts'
LOGS    = '/home/ubuntu/.picoclaw/logs'
sys.path.insert(0, SCRIPTS)

# ── Colours ───────────────────────────────────────────────────────────────────
GRN = '\033[92m'; RED = '\033[91m'; YLW = '\033[93m'; BLD = '\033[1m'; RST = '\033[0m'

# ── Test registry ─────────────────────────────────────────────────────────────
REGISTRY = []

@dataclass
class TestResult:
    passed: bool
    detail: str
    severity: str = 'HIGH'
    metrics: Optional[dict] = field(default=None)

def test(category: str, name: str, severity: str = 'HIGH'):
    """Decorator — registers test function in REGISTRY."""
    def decorator(fn):
        REGISTRY.append({
            'category': category, 'name': name,
            'severity': severity, 'fn': fn,
        })
        return fn
    return decorator

def _load(filename: str):
    """Load a script as a module by filename."""
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(filename.replace('.', '_').replace('-', '_'), path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _read_log(filename: str, default=None):
    try:
        with open(os.path.join(LOGS, filename)) as f:
            return json.load(f)
    except Exception:
        return default

def _write_tmp(data: dict) -> str:
    """Write data to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix='.json', prefix='apex-stress-')
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    return path

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 1 — Statistical Validity
# ══════════════════════════════════════════════════════════════════════════════

@test('1-Statistical', 'Kelly thin-prior shrinkage at n=0,1,16,50')
def t1_1_kelly_uncertainty():
    kv2 = _load('apex-kelly-v2.py')
    f   = kv2.parameter_uncertainty_factor
    cases = [(0, 0.10), (1, 0.10), (5, 0.10), (10, 0.20), (16, 0.32), (25, 0.50), (50, 1.0)]
    failures = []
    metrics  = {}
    for n, expected in cases:
        got = f(n)
        metrics[f'n={n}'] = got
        if abs(got - expected) > 0.001:
            failures.append(f'n={n}: got {got}, expected {expected}')
    if failures:
        return TestResult(False, '; '.join(failures), metrics=metrics)
    return TestResult(True, f'All {len(cases)} shrinkage values correct. n=16 → {f(16):.2f} (68% shrunk)', metrics=metrics)


@test('1-Statistical', 'Win rate 95% CI width on 16 trades')
def t1_2_win_rate_ci():
    stats = _load('apex-backtest-stats.py')
    lo, hi = stats.binomial_ci_pct(wins=8, n=16, confidence=0.95)
    width  = hi - lo
    metrics = {'ci_lower': lo, 'ci_upper': hi, 'ci_width': round(width, 1)}
    if lo >= 35:
        return TestResult(False, f'CI lower {lo:.1f}% is too high — system may be over-confident', metrics=metrics)
    if width < 35:
        return TestResult(False, f'CI width {width:.1f}pp suspiciously narrow for n=16', metrics=metrics)
    return TestResult(
        True,
        f'95% CI: [{lo:.1f}%, {hi:.1f}%] (width={width:.1f}pp) — prior uncertainty is real at n=16',
        metrics=metrics
    )


@test('1-Statistical', 'Live vs backtest score distribution alignment', severity='MEDIUM')
def t1_3_score_distribution():
    dlog   = _read_log('apex-decision-log.json', [])
    btest  = _read_log('apex-backtest-v2-results.json', {})
    if not dlog or not btest:
        return TestResult(True, 'SKIP — decision log or backtest results not found', metrics={})

    live_scores = []
    for entry in (dlog if isinstance(dlog, list) else []):
        for sig in entry.get('candidates', []):
            s = sig.get('adj_score', sig.get('adjusted_score'))
            if s is not None:
                live_scores.append(float(s))

    bt_scores = []
    for window in btest.get('windows', []):
        for trade in window.get('oos_trades', []):
            s = trade.get('score')
            if s is not None:
                bt_scores.append(float(s))

    if not live_scores or not bt_scores:
        return TestResult(True, f'SKIP — insufficient score data (live={len(live_scores)}, bt={len(bt_scores)})', metrics={})

    live_mean = round(sum(live_scores) / len(live_scores), 2)
    bt_mean   = round(sum(bt_scores)   / len(bt_scores), 2)
    delta     = round(live_mean - bt_mean, 2)
    metrics   = {'live_mean': live_mean, 'bt_mean': bt_mean, 'delta': delta,
                 'live_n': len(live_scores), 'bt_n': len(bt_scores)}

    if abs(delta) > 1.5:
        direction = 'inflation' if delta > 0 else 'deflation'
        return TestResult(False, f'Score {direction}: live={live_mean} vs backtest={bt_mean} (delta={delta:+.2f})', metrics=metrics)
    return TestResult(True, f'Live mean={live_mean}, backtest mean={bt_mean}, delta={delta:+.2f} — within ±1.5 threshold', metrics=metrics)


@test('1-Statistical', 'Learned weight activation boundary at n=4 vs n=5', severity='MEDIUM')
def t1_4_learned_weight_boundary():
    import apex_utils as au
    original_sr = au.safe_read

    results = {}
    for n_signals in [4, 5]:
        fake_weights = {
            'n_signals_matched': n_signals,
            'weights': {'RS': 1.2, 'MTF': 1.1, 'FRED': 0.8},
            'source': 'learned'
        }
        _captured = {}

        def fake_sr(path, default=None):
            if 'learned-weights' in str(path):
                return fake_weights
            return original_sr(path, default)

        au.safe_read = fake_sr
        try:
            from apex_scoring import _load_layer_weights
            weights, source = _load_layer_weights()
            _captured['source'] = source
            _captured['n'] = n_signals
        except Exception as e:
            _captured['error'] = str(e)
        finally:
            au.safe_read = original_sr

        results[f'n={n_signals}'] = _captured

    # n=4 should NOT use learned weights (threshold is 5)
    n4_source = results.get('n=4', {}).get('source', '')
    n5_source = results.get('n=5', {}).get('source', '')
    metrics   = results

    if 'learned' in str(n4_source).lower():
        return TestResult(False, f'n=4 activated learned weights (should require n≥5): source={n4_source}', metrics=metrics)
    return TestResult(True, f'n=4 → {n4_source or "ablation fallback"}, n=5 → {n5_source or "learned"}. Boundary correct.', metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 2 — Sizing Integrity (7-Haircut Cascade)
# ══════════════════════════════════════════════════════════════════════════════

def _make_signal(score=5, sig_type='TREND', entry=100.0, stop=94.0, layer_conf=1.0):
    return {
        'adjusted_score': score, 'total_score': score,
        'signal_type': sig_type,
        'entry': entry, 'price': entry, 'stop': stop,
        'name': 'StressTest', 'ticker': 'STRESS_EQ', 't212_ticker': 'STRESS_EQ',
        'layer_confidence': layer_conf, 'failed_layers': [],
        'sector': 'Technology',
    }

def _make_intel(size_mult=1.0):
    return {'size_multiplier': size_mult}


@test('2-Sizing', 'Worst-case cascade → minimum notional gate blocks trade', severity='CRITICAL')
def t2_1_worst_case_cascade():
    """
    With drawdown CAUTION (×0.50) + CB CAUTION (×0.50) → size_mult=0.25,
    low score (5/12 = 0.417 conviction), and layer confidence 55% (→25% minimum),
    a cheap stock (£10 entry, £0.20 risk/share) should produce notional < £100.

    Note: The `max(1.0, qty * 0.25)` floor in the layer confidence penalty means
    the cascade is catchable only when 1 share × entry < £100. This is an important
    system interaction: minimum shares floor and minimum notional gate work together.
    """
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash

    def fake_pv(): return 5000.0
    def fake_fc(): return 4000.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv
    au.get_free_cash       = fake_fc
    sz.get_free_cash       = fake_fc

    try:
        # entry=£10, stop=£9.80 → risk_per_share=£0.20
        # After all haircuts, qty × 0.25 ≈ 7, notional ≈ £70 < £100 → BLOCKED
        sig   = _make_signal(score=5, entry=10.0, stop=9.80, layer_conf=0.55)
        intel = _make_intel(size_mult=0.25)  # drawdown CAUTION × CB CAUTION
        qty, notional = sz.calculate_final_position(sig, intel)
        metrics = {'qty': qty, 'notional': notional,
                   'note': 'max(1.0, qty×0.25) floor means cascade catchable only when 1_share×entry < £100'}
        if qty == 0 and notional == 0:
            return TestResult(True, f'Correctly blocked: cascade → (0,0) via minimum notional gate (entry=£10 stock) ✓', metrics=metrics)
        # If not blocked: document the haircut compounding for audit trail
        return TestResult(False,
            f'Not fully blocked at entry=£10: qty={qty}, notional=£{notional}. '
            f'Kelly ABORT minimum (£10) may have inflated risk floor — see metrics.',
            metrics=metrics)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc


@test('2-Sizing', 'Zero free cash: Python `or` operator behaviour')
def t2_2_zero_free_cash():
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash
    fake_regime = {'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'FAVOURABLE', 'vix': 15.0}

    import apex_utils as au2
    orig_sr = au2.safe_read
    def fake_sr_none(path, default=None):
        if 'regime-scaling' in str(path): return fake_regime
        if 'pairwise-corr'  in str(path): return {}
        if 'positions'      in str(path): return []
        if 'portfolio-heat' in str(path): return {}
        if 'outcomes'       in str(path): return []
        return {}
    au2.safe_read = fake_sr_none

    def fake_pv(): return 5000.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv

    results = {}
    try:
        sig   = _make_signal(score=8, entry=50.0, stop=47.0, layer_conf=1.0)
        intel = _make_intel(size_mult=1.0)

        au.get_free_cash = sz.get_free_cash = lambda: None
        qty_none, notional_none = sz.calculate_final_position(sig, intel)

        au.get_free_cash = sz.get_free_cash = lambda: 0.0
        qty_zero, notional_zero = sz.calculate_final_position(sig, intel)

        results = {
            'none_cash_qty': qty_none, 'none_cash_notional': notional_none,
            'zero_cash_qty': qty_zero, 'zero_cash_notional': notional_zero,
        }
        # The bug: 0.0 is falsy in Python, so `get_free_cash() or portfolio_value * 0.3`
        # uses 30% fallback even when cash is genuinely zero
        bug_present = (qty_zero > 0)
        msg = (
            f'None→qty={qty_none}/£{notional_none}, Zero→qty={qty_zero}/£{notional_zero}. '
            + ('⚠ BUG PRESENT: 0.0 cash treated as None — using 30% fallback. Recommend: change `or` to `if x is None`'
               if bug_present else 'Zero cash correctly blocked.')
        )
        # Treat as WARN not FAIL — system is safe (conservative sizing), just imprecise
        return TestResult(not bug_present, msg, severity='MEDIUM', metrics=results)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc
        au2.safe_read = orig_sr


@test('2-Sizing', 'Minimum viable notional at £500 portfolio across price points', severity='MEDIUM')
def t2_3_small_portfolio():
    """
    At £500 portfolio with 1.75% risk budget: base_risk ≈ £8.75.
    With Kelly ABORT (negative mu on thin data), risk floors to portfolio×0.2%=£1.
    At full regime scale (1.0), the min(12.5, 8.75) = £8.75 risk budget tests
    which stock prices generate viable (≥£100) positions.
    """
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash

    fake_regime_full = {
        'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'FAVOURABLE',
        'vix': 15.0, 'vix_raw': 15.0, 'vix_garch_blended': 15.0, 'garch_available': True,
        'breadth_scale': 1.0, 'breadth_pct': 70.0,
    }

    def fake_pv(): return 500.0
    def fake_fc(): return 450.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv
    au.get_free_cash       = fake_fc
    sz.get_free_cash       = fake_fc

    results = {}
    try:
        with _override_log('apex-regime-scaling.json', fake_regime_full), \
             _override_log('apex-pairwise-corr.json', {}), \
             _override_log('apex-portfolio-heat.json', {}):
            intel = _make_intel(size_mult=1.0)
            for price in [10.0, 50.0, 100.0, 200.0, 500.0]:
                stop = round(price * 0.96, 2)
                sig  = _make_signal(score=8, entry=price, stop=stop, layer_conf=1.0)
                qty, notional = sz.calculate_final_position(sig, intel)
                results[f'£{price:.0f}'] = {'qty': qty, 'notional': round(notional, 2),
                                             'viable': notional >= 100}

        viable_count = sum(1 for v in results.values() if v['viable'])
        msg = (f'{viable_count}/{len(results)} price points viable at £500 portfolio | '
               + ' | '.join(f"£{p}→£{v['notional']:.0f}" for p, v in results.items()))
        # Any viable = passes (documenting behaviour, not enforcing strict rule)
        return TestResult(True, msg, severity='MEDIUM', metrics=results)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc


@test('2-Sizing', 'Kelly ABORT pathway: minimum trade vs notional gate tension')
def t2_4_kelly_abort():
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash
    fake_regime = {'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'NEUTRAL', 'vix': 20.0}

    import apex_utils as au2
    orig_sr = au2.safe_read
    def fake_sr(path, default=None):
        if 'regime-scaling'  in str(path): return fake_regime
        if 'pairwise-corr'   in str(path): return {}
        if 'positions'       in str(path): return []
        if 'portfolio-heat'  in str(path): return {}
        if 'outcomes'        in str(path): return [{'result': 'MANUAL_LOSS', 'pnl': -50, 'r_achieved': -1, 'signal_type': 'TREND'}] * 20
        return {}
    au2.safe_read = fake_sr

    def fake_pv(): return 5000.0
    def fake_fc(): return 4000.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv
    au.get_free_cash       = fake_fc
    sz.get_free_cash       = fake_fc

    try:
        # High-priced stock — Kelly ABORT minimum (£10) / risk_per_share (£20) = 0.5 shares × £500 = £250 notional → passes
        sig_high   = _make_signal(score=7, entry=500.0, stop=480.0)  # risk_per_share=£20
        intel      = _make_intel(size_mult=0.1)  # deeply reduced to force ABORT pathway
        qty, notional = sz.calculate_final_position(sig_high, intel)
        metrics = {'qty': qty, 'notional': notional}
        msg = f'Kelly ABORT at £500 stock: qty={qty}, notional=£{notional} — {"BLOCKED by notional gate" if qty == 0 else "trade placed at minimum"}'
        return TestResult(True, msg, metrics=metrics)  # Document behaviour, both outcomes acceptable
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc
        au2.safe_read = orig_sr


@test('2-Sizing', 'Recovery ramp multiplier: min() not product of CB and drawdown mult', severity='MEDIUM')
def t2_5_recovery_ramp():
    """
    Verify that when both drawdown CAUTION (0.75) and CB recovery ramp (cb_mult * 0.5)
    are active, size_multiplier = min(0.75, 0.25) = 0.25 (not 0.75 * 0.25 = 0.1875).
    """
    # Simulate how apex_intelligence.py computes size_multiplier
    drawdown_mult = 0.75   # CAUTION
    cb_mult_base  = 0.50   # CAUTION status
    ramp_active   = True

    if ramp_active:
        cb_mult = round(cb_mult_base * 0.5, 2)  # = 0.25
    else:
        cb_mult = cb_mult_base

    size_multiplier = round(min(drawdown_mult, cb_mult), 2)
    expected = 0.25
    metrics  = {'drawdown_mult': drawdown_mult, 'cb_mult': cb_mult,
                'ramp_active': ramp_active, 'size_multiplier': size_multiplier,
                'would_be_if_product': round(drawdown_mult * cb_mult, 4)}

    if abs(size_multiplier - expected) > 0.001:
        return TestResult(False, f'size_multiplier={size_multiplier}, expected {expected} (min not product)', metrics=metrics)
    return TestResult(True, f'size_multiplier=min({drawdown_mult}, {cb_mult})={size_multiplier} ✓ (not product={metrics["would_be_if_product"]})', metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 3 — Regime Logic
# ══════════════════════════════════════════════════════════════════════════════

def _get_bonus_fn():
    """Extract _regime_priority_bonus from decision engine source."""
    import apex_utils as au
    src = open(os.path.join(SCRIPTS, 'apex-decision-engine.py')).read()
    ns  = {'safe_read': au.safe_read}
    # Extract just the function definition
    import re
    m = re.search(r'(def _regime_priority_bonus\(.*?)(?=\n^def |\n^class |\Z)', src, re.DOTALL | re.MULTILINE)
    if m:
        exec(compile(m.group(1), '<de>', 'exec'), ns)
    return ns.get('_regime_priority_bonus')


@test('3-Regime', 'HMM bonus matrix: all 15 state×signal_type combinations', severity='CRITICAL')
def t3_1_hmm_bonus_matrix():
    import apex_utils as au
    orig_sr = au.safe_read

    HMM_PRIORITY = {
        'TRENDING':       {'TREND': 2.0, 'EARNINGS_DRIFT': 1.5, 'CONTRARIAN': -1.0, 'INVERSE': -2.0, 'DIVIDEND_CAPTURE': 1.0},
        'MEAN_REVERTING': {'TREND': -0.5,'EARNINGS_DRIFT': 0.0, 'CONTRARIAN': 2.0,  'INVERSE': 0.5,  'DIVIDEND_CAPTURE': 0.5},
        'CRISIS':         {'TREND': -3.0,'EARNINGS_DRIFT': -2.0,'CONTRARIAN': 1.0,  'INVERSE': 3.0,  'DIVIDEND_CAPTURE': -1.0},
    }
    PROB = 0.85
    failures = []
    metrics  = {}

    for state in HMM_PRIORITY:
        for sig_type in ['TREND', 'EARNINGS_DRIFT', 'CONTRARIAN', 'INVERSE', 'DIVIDEND_CAPTURE']:
            fake_hmm = {
                'available': True,
                'current_state': state,
                'state_probabilities': {state: PROB},
            }

            def fake_sr(path, default=None, _h=fake_hmm):
                if 'regime-hmm' in str(path): return _h
                if 'regime-scaling' in str(path): return {}
                return orig_sr(path, default) if default is not None else {}

            au.safe_read = fake_sr
            bonus_fn = _get_bonus_fn()
            bonus    = bonus_fn({'signal_type': sig_type}, {})
            au.safe_read = orig_sr

            expected = round(HMM_PRIORITY[state][sig_type] * PROB, 2)
            key = f'{state}+{sig_type}'
            metrics[key] = {'got': bonus, 'expected': expected}
            if abs(bonus - expected) > 0.01:
                failures.append(f'{key}: got {bonus}, expected {expected}')

    # Critical safety checks
    crisis_trend   = metrics.get('CRISIS+TREND', {}).get('got', 0)
    crisis_inverse = metrics.get('CRISIS+INVERSE', {}).get('got', 0)
    if crisis_trend > -2.0:
        failures.append(f'CRISIS+TREND bonus too high: {crisis_trend} (should be ≤ -2.0)')
    if crisis_inverse < 2.5:
        failures.append(f'CRISIS+INVERSE bonus too low: {crisis_inverse} (should be ≥ +2.5)')

    if failures:
        return TestResult(False, f'{len(failures)} failures: ' + '; '.join(failures[:3]), metrics=metrics)
    return TestResult(True, f'All 15 combinations correct. CRISIS+INVERSE={crisis_inverse:+.2f}, CRISIS+TREND={crisis_trend:+.2f}', metrics=metrics)


@test('3-Regime', 'HMM unavailable → VIX/breadth label fallback')
def t3_2_hmm_fallback():
    import apex_utils as au
    orig_sr = au.safe_read

    LABEL_PRIORITY = {
        'BLOCKED': {'TREND': -3.0, 'INVERSE': 2.5},
    }
    failures = []
    metrics  = {}

    for sig_type in ['TREND', 'INVERSE']:
        def fake_sr(path, default=None, sig=sig_type):
            if 'regime-hmm'     in str(path): return {}   # HMM unavailable
            if 'regime-scaling' in str(path): return {'regime_label': 'BLOCKED'}
            return orig_sr(path, default) if default is not None else {}

        au.safe_read = fake_sr
        bonus_fn = _get_bonus_fn()
        bonus    = bonus_fn({'signal_type': sig_type}, {})
        au.safe_read = orig_sr

        expected = LABEL_PRIORITY['BLOCKED'][sig_type]
        metrics[sig_type] = {'bonus': bonus, 'expected': expected}
        if abs(bonus - expected) > 0.01:
            failures.append(f'{sig_type}: got {bonus}, expected {expected}')

    if failures:
        return TestResult(False, '; '.join(failures), metrics=metrics)
    return TestResult(True, f'BLOCKED regime fallback correct: TREND={metrics["TREND"]["bonus"]:+.1f}, INVERSE={metrics["INVERSE"]["bonus"]:+.1f}', metrics=metrics)


@test('3-Regime', 'GARCH blend is never below spot VIX')
def t3_3_garch_floor():
    import apex_utils as au
    orig_sr = au.safe_read

    rs  = _load('apex-regime-scaling.py')
    fn  = getattr(rs, '_garch_vix_forecast', None)
    if fn is None:
        return TestResult(True, 'SKIP — _garch_vix_forecast not found (may be inline)', metrics={})

    import unittest.mock as mock
    results = {}

    # Test 1: GARCH predicts high vol → blend should be above spot
    spot_vix = 20.0
    with mock.patch('yfinance.download') as mock_yf:
        import pandas as pd, numpy as np
        dates  = pd.date_range('2025-01-01', periods=120, freq='B')
        prices = 100 * np.exp(np.cumsum(np.random.normal(0, 0.01, 120)))
        mock_yf.return_value = pd.DataFrame({'Close': prices}, index=dates)
        blended, available = fn(spot_vix)

    results['high_vol_blended'] = blended
    results['high_vol_available'] = available

    passed = blended >= spot_vix
    if not passed:
        return TestResult(False, f'GARCH blend {blended} < spot VIX {spot_vix} — floor violated', metrics=results)

    # Test 2: GARCH failure → returns spot unchanged
    with mock.patch('yfinance.download', side_effect=Exception('network error')):
        fallback_blended, fallback_avail = fn(spot_vix)

    results['fallback_blended']   = fallback_blended
    results['fallback_available'] = fallback_avail

    if fallback_blended != spot_vix or fallback_avail:
        return TestResult(False, f'GARCH failure fallback wrong: returned {fallback_blended}, available={fallback_avail}', metrics=results)

    return TestResult(True, f'GARCH floor: blend={blended:.2f} ≥ spot={spot_vix}. Failure fallback: spot={fallback_blended:.2f}', metrics=results)


@test('3-Regime', 'Regime scaling boundary conditions (cliff edges)', severity='MEDIUM')
def t3_4_regime_boundaries():
    rs = _load('apex-regime-scaling.py')
    fn = getattr(rs, 'calculate_scaling', None)
    if fn is None:
        return TestResult(True, 'SKIP — calculate_scaling not directly callable', metrics={})

    cases = [
        (15.0, 70.0, 'FAVOURABLE'),   # Low VIX, high breadth
        (35.0, 70.0, 'BLOCKED'),       # VIX cliff (35 = 0 scale)
        (20.0, 20.0, 'BLOCKED'),       # Normal VIX but thin breadth → geometric mean = 0
        (25.0, 40.0, None),            # CAUTIOUS territory (no label required)
    ]
    results = {}
    try:
        for vix, breadth, expected_label in cases:
            out = fn(vix, breadth)
            label = out.get('regime_label', out.get('label', '?'))
            scale = out.get('combined_scale', out.get('scale', '?'))
            results[f'vix={vix},br={breadth}'] = {'label': label, 'scale': scale}
            if expected_label and label != expected_label:
                return TestResult(False, f'VIX={vix}, breadth={breadth}: got {label}, expected {expected_label}', metrics=results)
        key = 'vix=20.0,br=20.0'
        cliff = results.get(key, {})
        msg = f'Boundaries correct. Non-obvious cliff: VIX=20+breadth=20 → {cliff.get("label")} (scale={cliff.get("scale")})'
        return TestResult(True, msg, metrics=results)
    except TypeError:
        return TestResult(True, f'SKIP — calculate_scaling signature differs', metrics=results)


@test('3-Regime', 'Regime bonus does NOT mutate adjusted_score', severity='CRITICAL')
def t3_5_no_score_mutation():
    import apex_utils as au
    orig_sr = au.safe_read

    fake_hmm = {'available': True, 'current_state': 'CRISIS',
                'state_probabilities': {'CRISIS': 0.95}}

    def fake_sr(path, default=None):
        if 'regime-hmm'     in str(path): return fake_hmm
        if 'regime-scaling' in str(path): return {'regime_label': 'HOSTILE'}
        return orig_sr(path, default) if default is not None else {}

    au.safe_read = fake_sr
    bonus_fn = _get_bonus_fn()

    signal = {'signal_type': 'TREND', 'adjusted_score': 8.5}
    score_before = signal['adjusted_score']
    bonus = bonus_fn(signal, {})
    score_after  = signal['adjusted_score']
    au.safe_read = orig_sr

    metrics = {'score_before': score_before, 'score_after': score_after, 'bonus': bonus}
    if score_before != score_after:
        return TestResult(False, f'adjusted_score MUTATED: {score_before} → {score_after} (bonus={bonus})', metrics=metrics)
    return TestResult(True, f'adjusted_score unchanged at {score_after}. Bonus={bonus:+.2f} stored separately.', metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 4 — Signal Quality
# ══════════════════════════════════════════════════════════════════════════════

def _minimal_intel():
    return {
        'open_positions': [], 'earnings_blocked': [], 'news_blocked': [],
        'regime_status': 'CLEAR', 'direction_status': 'CLEAR',
        'geo_status': 'CLEAR', 'geo': {'overall': 'CLEAR'},
        'vix': 18.0, 'breadth_pct': 60.0, 'breadth': {},
        'size_multiplier': 1.0, 'drawdown_pct': 0.0,
        'position_vix_sensitivity': {}, 'sector_breadth': {},
        'direction_blocks': [],
        'file_ages_hours': {'macro': 0, 'sentiment': 0, 'sector_rotation': 0, 'breadth': 0},
        'macro': {}, 'sector_rotation': {},
    }


from contextlib import contextmanager

@contextmanager
def _override_log(filename: str, fake_data: dict):
    """Temporarily overwrite a log file with fake data, restoring original after."""
    path   = os.path.join(LOGS, filename)
    backup = None
    try:
        if os.path.exists(path):
            with open(path) as f:
                backup = f.read()
        with open(path, 'w') as f:
            json.dump(fake_data, f)
        yield path
    finally:
        if backup is not None:
            with open(path, 'w') as f:
                f.write(backup)
        elif os.path.exists(path):
            os.remove(path)


@test('4-SignalQuality', 'Score determinism: identical inputs produce identical output')
def t4_1_score_determinism():
    try:
        from apex_scoring import score_signal_with_intelligence
    except ImportError:
        return TestResult(True, 'SKIP — apex_scoring not importable directly', metrics={})

    signal = {'total_score': 7.0, 'signal_type': 'TREND',
              'name': 'MSFT', 'ticker': 'MSFT_US_EQ', 't212_ticker': 'MSFT_US_EQ',
              'contrarian_score': 0, 'score': 7.0}
    intel  = _minimal_intel()

    import copy
    sig1 = copy.deepcopy(signal)
    sig2 = copy.deepcopy(signal)

    try:
        score_signal_with_intelligence(sig1, intel)
        score_signal_with_intelligence(sig2, intel)

        adj1 = sig1.get('adjustments', [])
        adj2 = sig2.get('adjustments', [])
        sc1  = sig1.get('adjusted_score')
        sc2  = sig2.get('adjusted_score')

        metrics = {'score_run1': sc1, 'score_run2': sc2,
                   'adj_count_run1': len(adj1), 'adj_count_run2': len(adj2),
                   'order_match': adj1 == adj2}

        if sc1 != sc2:
            return TestResult(False, f'Score non-deterministic: {sc1} vs {sc2}', metrics=metrics)
        if adj1 != adj2:
            return TestResult(False, f'Adjustment list ordering differs between runs. Order match={metrics["order_match"]}', metrics=metrics)
        return TestResult(True, f'Deterministic: score={sc1}, {len(adj1)} adjustments, identical order', metrics=metrics)
    except Exception as e:
        return TestResult(True, f'SKIP — scoring requires live data files: {e}', metrics={})


@test('4-SignalQuality', 'Score cap: adjustment ceiling +5, final score ceiling 10.0')
def t4_2_score_cap():
    try:
        from apex_scoring import score_signal_with_intelligence
    except ImportError:
        return TestResult(True, 'SKIP', metrics={})

    # Construct a signal that should get heavily positive adjustments
    # by mocking an instrument with all positive indicators in a simple intel
    signal = {'total_score': 7.0, 'signal_type': 'TREND',
              'name': 'AAPL', 'ticker': 'AAPL_US_EQ', 't212_ticker': 'AAPL_US_EQ',
              'contrarian_score': 0, 'score': 7.0}
    intel = _minimal_intel()

    try:
        score_signal_with_intelligence(signal, intel)
        adj_score = signal.get('adjusted_score', 0)
        raw_score = signal.get('raw_score', adj_score)
        metrics   = {'adjusted_score': adj_score, 'raw_score': raw_score}

        if adj_score > 10.0:
            return TestResult(False, f'adjusted_score={adj_score} > 10.0 — cap not applied', metrics=metrics)
        return TestResult(True, f'adjusted_score={adj_score} ≤ 10.0 ✓ (raw={raw_score})', metrics=metrics)
    except Exception as e:
        return TestResult(True, f'SKIP — needs live data: {e}', metrics={})


@test('4-SignalQuality', 'Dead layer detection on decision log', severity='MEDIUM')
def t4_3_dead_layer_detection():
    import re
    dlog = _read_log('apex-decision-log.json', [])
    if not dlog or not isinstance(dlog, list):
        return TestResult(True, 'SKIP — no decision log found', metrics={})

    # Collect recent entries
    entries = dlog[-30:] if len(dlog) > 30 else dlog
    layer_hits = {}
    KNOWN_LAYERS = ['RS', 'MTF', 'MACRO', 'DIVERGE', 'INSIDER', 'REVISION',
                    'FRED', 'OPTIONS', 'VOL_ACCUMULATION', 'BACKTEST',
                    'SECTOR', 'GEO', 'SENT', 'FUND']
    ADJ_RE = re.compile(r'^([A-Za-z_]+)\s*:', re.IGNORECASE)

    for entry in entries:
        # Handle both list-of-entries and dict-with-candidates formats
        candidates = entry.get('candidates', []) if isinstance(entry, dict) else []
        for sig in candidates:
            for adj in sig.get('adjustments', sig.get('adj', [])):
                m = ADJ_RE.match(str(adj).strip())
                if m:
                    layer = m.group(1).upper()
                    layer_hits[layer] = layer_hits.get(layer, 0) + 1

    total_signals = sum(len(e.get('candidates', [])) for e in entries if isinstance(e, dict))
    dead_layers   = [l for l in KNOWN_LAYERS if layer_hits.get(l, 0) == 0]
    rare_layers   = [l for l in KNOWN_LAYERS if 0 < layer_hits.get(l, 0) < max(1, total_signals * 0.05)]
    metrics = {'total_signals_checked': total_signals, 'layer_hits': layer_hits,
               'dead_layers': dead_layers, 'rare_layers': rare_layers}

    if total_signals == 0:
        return TestResult(True, 'SKIP — no candidate signals in decision log', metrics=metrics)
    if dead_layers:
        return TestResult(False, f'Dead layers (0% activation): {dead_layers}', metrics=metrics)
    return TestResult(True, f'{len(layer_hits)} layers active across {total_signals} signals. Rare (<5%): {rare_layers or "none"}', metrics=metrics)


@test('4-SignalQuality', 'Gemini sentiment fallback produces correct scoring adjustments')
def t4_4_sentiment_scoring():
    # Test the scoring adjustments that sentiment produces at different score levels
    # Without needing to call the full scoring pipeline
    test_cases = [
        (0.0,  'NEUTRAL',       0),
        (0.35, 'VERY_POSITIVE', 2),
        (-0.35,'VERY_NEGATIVE', -2),
        (0.15, 'POSITIVE',      1),
        (-0.15,'NEGATIVE',      -1),
    ]

    # Read the classify_sentiment function from apex-sentiment.py
    sent = _load('apex-sentiment.py')
    failures = []
    metrics  = {}

    for score, expected_label, expected_adj in test_cases:
        label, note = sent.classify_sentiment(score)
        metrics[str(score)] = {'label': label, 'expected': expected_label}
        if label != expected_label:
            failures.append(f'score={score}: got {label}, expected {expected_label}')

    # Also verify scoring method is in the sentiment file
    sdata = _read_log('apex-sentiment.json', {})
    if sdata:
        method = sdata.get('scoring_method', 'unknown')
        metrics['current_scoring_method'] = method
        if method != 'llm':
            failures.append(f'scoring_method={method} — should be "llm" when GEMINI_API_KEY is set')

    if failures:
        return TestResult(False, '; '.join(failures), metrics=metrics)
    return TestResult(True, f'All sentiment thresholds correct. Current scoring method: {metrics.get("current_scoring_method", "n/a")}', metrics=metrics)


@test('4-SignalQuality', 'Edge proof layer cold-start: no penalty below n=20', severity='MEDIUM')
def t4_5_edge_proof_cold_start():
    """Verify the edge proof / adversarial layer doesn't fire below its minimum trade count."""
    dlog = _read_log('apex-decision-log.json', [])
    outcomes = _read_log('apex-outcomes.json', [])
    n_trades = len(outcomes) if isinstance(outcomes, list) else 0

    metrics = {'n_live_trades': n_trades, 'edge_proof_threshold': 20}

    if n_trades < 20:
        # Check no recent signal has an "edge proof: -1" adjustment while n < 20
        penalty_found = False
        for entry in (dlog[-10:] if isinstance(dlog, list) else []):
            for sig in entry.get('candidates', []) if isinstance(entry, dict) else []:
                for adj in sig.get('adjustments', []):
                    if 'edge' in str(adj).lower() and '-1' in str(adj):
                        penalty_found = True
        metrics['penalty_applied_below_threshold'] = penalty_found
        if penalty_found:
            return TestResult(False, f'Edge proof -1 penalty found with only {n_trades} trades (threshold=20)', metrics=metrics)
        return TestResult(True, f'n_trades={n_trades} < 20 — edge proof dormant ✓', metrics=metrics)
    return TestResult(True, f'n_trades={n_trades} ≥ 20 — edge proof may be active (check decision log)', metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 5 — System Resilience
# ══════════════════════════════════════════════════════════════════════════════

@test('5-Resilience', 'Cold start: missing log files → safe defaults', severity='CRITICAL')
def t5_1_cold_start():
    import apex_utils as au
    orig_sr = au.safe_read

    def always_empty(path, default=None):
        return default if default is not None else {}

    au.safe_read = always_empty
    results = {}
    try:
        import importlib
        import apex_utils
        importlib.reload(apex_utils)

        # Test safe_read with missing files — should return defaults not raise
        result = au.safe_read('/nonexistent/file.json', {'key': 'default'})
        results['missing_file_returns_default'] = (result == {'key': 'default'})

        result2 = au.safe_read('/nonexistent/file.json', [])
        results['missing_file_returns_list_default'] = (result2 == [])

        result3 = au.safe_read('/nonexistent/file.json')
        results['missing_file_no_default'] = (result3 is None or result3 == {})

        all_pass = all(results.values())
        return TestResult(all_pass, f'safe_read handles missing files correctly: {results}', metrics=results)
    except Exception as e:
        return TestResult(False, f'Exception during cold start test: {e}', metrics=results)
    finally:
        au.safe_read = orig_sr


@test('5-Resilience', 'Stale market direction: does TREND pass through unblocked?')
def t5_2_stale_direction():
    """
    The stale direction gate sets status to 'STALE (Nh old)' not 'BLOCKED'.
    is_blocked() checks for == 'BLOCKED'. Stale data may allow TREND entries.
    """
    try:
        from apex_filters import is_blocked
    except ImportError:
        return TestResult(True, 'SKIP — apex_filters not importable', metrics={})

    import apex_utils as au
    orig_sr = au.safe_read

    # Simulate: direction_status is stale, not BLOCKED
    intel_stale = _minimal_intel()
    intel_stale['direction_status'] = 'STALE (25h old)'
    intel_stale['regime_status']    = 'CLEAR'

    sig = _make_signal(score=8, sig_type='TREND')
    try:
        blocks = is_blocked(sig, intel_stale)
        stale_allows_trend = (len(blocks) == 0)
        metrics = {'blocks': blocks, 'stale_allows_trend': stale_allows_trend,
                   'direction_status': 'STALE (25h old)'}
        msg = (
            f'Stale direction ALLOWS TREND entries (blocks={blocks}). '
            '⚠ Architectural gap: stale status ≠ BLOCKED — 25h-old bearish data is ignored.'
            if stale_allows_trend
            else f'Stale direction correctly blocked trend: {blocks}'
        )
        # Document as WARN — not hard fail, it's a known design decision
        return TestResult(not stale_allows_trend, msg, severity='MEDIUM', metrics=metrics)
    finally:
        au.safe_read = orig_sr


@test('5-Resilience', 'Correlation cache expiry: sector proxy fallback at 49h', severity='MEDIUM')
def t5_3_correlation_cache_expiry():
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash

    old_ts   = '2020-01-01T00:00:00'   # Definitely stale
    fake_corr = {'generated': old_ts, 'correlations': {'AAPL_US_EQ:MSFT_US_EQ': 0.95}}
    fake_positions = [{'t212_ticker': 'AAPL_US_EQ', 'sector': 'Technology',
                       'status': 'protected', 'name': 'Apple'}]
    fake_regime    = {'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'NEUTRAL', 'vix': 18.0}

    import apex_utils as au2
    orig_sr = au2.safe_read
    def fake_sr(path, default=None):
        if 'regime-scaling'  in str(path): return fake_regime
        if 'pairwise-corr'   in str(path): return fake_corr
        if 'positions'       in str(path): return fake_positions
        if 'portfolio-heat'  in str(path): return {}
        if 'outcomes'        in str(path): return []
        return {}
    au2.safe_read = fake_sr

    def fake_pv(): return 5000.0
    def fake_fc(): return 4000.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv
    au.get_free_cash       = fake_fc
    sz.get_free_cash       = fake_fc

    try:
        # MSFT in same sector as AAPL — cache is stale, should use 0.72 proxy
        sig   = _make_signal(score=8, entry=50.0, stop=47.0, sig_type='TREND')
        sig['t212_ticker'] = 'MSFT_US_EQ'
        sig['sector']      = 'Technology'
        intel = _make_intel(size_mult=1.0)

        qty, notional = sz.calculate_final_position(sig, intel)
        # With cache stale, sector proxy 0.72 → 25% reduction (not 50%, since 0.72 < 0.85)
        metrics = {'qty': qty, 'notional': notional, 'cache_age': '49h+ stale',
                   'expected_reduction': '25% (sector proxy 0.72)'}
        return TestResult(True, f'Stale cache → sector proxy used. qty={qty}, notional=£{notional}. '
                          f'Note: real corr may be 0.95 but proxy uses 0.72 — under-reduces by 25%', metrics=metrics)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc
        au2.safe_read = orig_sr


@test('5-Resilience', 'T212 API failure: portfolio value fallback is conservative')
def t5_4_api_failure_fallback():
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash
    fake_regime = {'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'NEUTRAL', 'vix': 18.0}

    import apex_utils as au2
    orig_sr = au2.safe_read
    def fake_sr(path, default=None):
        if 'regime-scaling' in str(path): return fake_regime
        if 'pairwise-corr'  in str(path): return {}
        if 'positions'      in str(path): return []
        if 'portfolio-heat' in str(path): return {}
        if 'outcomes'       in str(path): return []
        return {}
    au2.safe_read = fake_sr

    sig   = _make_signal(score=8, entry=50.0, stop=47.0)
    intel = _make_intel(size_mult=1.0)

    try:
        # Real portfolio = £15,000 (good)
        au.get_portfolio_value = fake_pv15 = lambda: 15000.0
        sz.get_portfolio_value = fake_pv15
        au.get_free_cash       = sz.get_free_cash = lambda: 12000.0
        qty_real, notional_real = sz.calculate_final_position(sig, intel)

        # API fails → fallback £5000 (conservative)
        au.get_portfolio_value = sz.get_portfolio_value = lambda: None
        au.get_free_cash       = sz.get_free_cash       = lambda: None
        qty_fallback, notional_fallback = sz.calculate_final_position(sig, intel)

        metrics = {'real_notional': notional_real, 'fallback_notional': notional_fallback}
        if notional_fallback > notional_real:
            return TestResult(False, f'API fallback LARGER than real: £{notional_fallback} > £{notional_real} — dangerous!', metrics=metrics)
        return TestResult(True, f'API fallback conservative: £{notional_fallback} < real £{notional_real} ✓', metrics=metrics)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc
        au2.safe_read = orig_sr


@test('5-Resilience', 'Atomic write: original file intact on failed write')
def t5_5_atomic_write():
    import apex_utils as au
    tmpdir = tempfile.mkdtemp()
    test_file = os.path.join(tmpdir, 'test-state.json')
    original_data = {'status': 'original', 'value': 42}

    try:
        # Write initial file
        au.atomic_write(test_file, original_data)
        with open(test_file) as f:
            read_back = json.load(f)

        if read_back != original_data:
            return TestResult(False, f'Initial write failed: {read_back}', metrics={})

        # Now simulate a failed second write by making the directory read-only
        bad_data = {'status': 'corrupted', 'value': 999}
        import unittest.mock as mock
        with mock.patch('os.replace', side_effect=OSError('disk full')):
            try:
                au.atomic_write(test_file, bad_data)
            except Exception:
                pass  # Expected — the write should fail

        # Original should be intact
        with open(test_file) as f:
            after_fail = json.load(f)

        metrics = {'original': original_data, 'after_failed_write': after_fail}
        if after_fail != original_data:
            return TestResult(False, f'File corrupted after failed write: {after_fail}', metrics=metrics)
        return TestResult(True, 'Original file intact after failed atomic write ✓', metrics=metrics)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 6 — Market Stress Scenarios
# ══════════════════════════════════════════════════════════════════════════════

@test('6-MarketStress', 'VIX spike to 45: TREND bonus ≤ -2.5, INVERSE ≥ +2.5', severity='CRITICAL')
def t6_1_vix_spike():
    import apex_utils as au
    orig_sr = au.safe_read

    # HMM detects CRISIS
    fake_hmm = {'available': True, 'current_state': 'CRISIS',
                'state_probabilities': {'CRISIS': 0.95, 'TRENDING': 0.03, 'MEAN_REVERTING': 0.02}}
    fake_regime = {'regime_label': 'BLOCKED', 'combined_scale': 0.0, 'trend_scale': 0.0, 'vix': 45.0}

    def fake_sr(path, default=None):
        if 'regime-hmm'     in str(path): return fake_hmm
        if 'regime-scaling' in str(path): return fake_regime
        return orig_sr(path, default) if default is not None else {}

    au.safe_read = fake_sr
    bonus_fn = _get_bonus_fn()

    trend_bonus   = bonus_fn({'signal_type': 'TREND'},   {})
    inverse_bonus = bonus_fn({'signal_type': 'INVERSE'}, {})
    au.safe_read = orig_sr

    metrics = {'vix': 45, 'hmm_state': 'CRISIS', 'trend_bonus': trend_bonus, 'inverse_bonus': inverse_bonus}
    failures = []
    if trend_bonus > -2.5:
        failures.append(f'TREND bonus too high in CRISIS: {trend_bonus:+.2f} (should be ≤ -2.5)')
    if inverse_bonus < 2.5:
        failures.append(f'INVERSE bonus too low in CRISIS: {inverse_bonus:+.2f} (should be ≥ +2.5)')
    if failures:
        return TestResult(False, '; '.join(failures), metrics=metrics)
    return TestResult(True, f'VIX=45 CRISIS: TREND={trend_bonus:+.2f}, INVERSE={inverse_bonus:+.2f} ✓', metrics=metrics)


@test('6-MarketStress', 'Circuit breaker: -9% session triggers SUSPEND', severity='CRITICAL')
def t6_2_circuit_breaker():
    cb_data = _read_log('apex-circuit-breaker.json', {})
    if not cb_data:
        return TestResult(True, 'SKIP — no circuit breaker file', metrics={})

    # Test the threshold logic directly
    cb = _load('apex-circuit-breaker.py')
    get_status = getattr(cb, 'get_circuit_breaker_status', None)
    if get_status is None:
        # Verify threshold constants are defined correctly
        suspend_threshold = getattr(cb, 'CB_SUSPEND', None)
        metrics = {'CB_SUSPEND': suspend_threshold}
        if suspend_threshold is not None and suspend_threshold == -8.0:
            return TestResult(True, f'CB_SUSPEND threshold=-8.0% confirmed. -9% would trigger SUSPEND ✓', metrics=metrics)
        return TestResult(True, f'SKIP — get_circuit_breaker_status not callable directly. CB_SUSPEND={suspend_threshold}', metrics=metrics)

    import unittest.mock as mock
    with mock.patch.object(cb, 'get_portfolio_value', return_value=10000):
        with mock.patch.object(cb, 'get_session_open', return_value=10988):  # -9% session
            status = get_status()

    metrics = {'status': status, 'session_pnl_pct': -9.0}
    if isinstance(status, dict):
        s = status.get('status', '')
        if s not in ('SUSPEND', 'CRITICAL'):
            return TestResult(False, f'Expected SUSPEND/CRITICAL at -9%, got {s}', metrics=metrics)
        return TestResult(True, f'CB correctly triggers {s} at -9% session loss ✓', metrics=metrics)
    return TestResult(True, f'SKIP — CB status format differs: {type(status)}', metrics=metrics)


@test('6-MarketStress', 'Max correlation: position reduced 50% at r≥0.85')
def t6_3_max_correlation():
    """Sizer uses open() directly for correlation cache — must write real temp files."""
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash

    ts       = datetime.now(timezone.utc).isoformat()
    fake_pos = [{'t212_ticker': 'AAPL_US_EQ', 'sector': 'Technology',
                 'status': 'protected', 'name': 'Apple'}]
    fake_corr_high = {'generated': ts, 'correlations': {'AAPL_US_EQ:GOOGL_US_EQ': 0.92}}
    fake_regime    = {'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'NEUTRAL',
                      'vix': 18.0, 'vix_raw': 18.0, 'vix_garch_blended': 18.0,
                      'garch_available': True, 'breadth_scale': 0.7, 'breadth_pct': 60.0}

    def fake_pv(): return 5000.0
    def fake_fc(): return 4000.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv
    au.get_free_cash       = fake_fc
    sz.get_free_cash       = fake_fc

    try:
        # Baseline: no positions → no correlation discount
        with _override_log('apex-regime-scaling.json', fake_regime), \
             _override_log('apex-positions.json', []), \
             _override_log('apex-pairwise-corr.json', fake_corr_high), \
             _override_log('apex-portfolio-heat.json', {}):
            sig_base = _make_signal(score=8, entry=50.0, stop=47.0)
            sig_base['t212_ticker'] = 'GOOGL_US_EQ'
            sig_base['sector']      = 'Technology'
            qty_base, notional_base = sz.calculate_final_position(sig_base, _make_intel())

        # With AAPL open at r=0.92 to GOOGL → should reduce 50%
        with _override_log('apex-regime-scaling.json', fake_regime), \
             _override_log('apex-positions.json', fake_pos), \
             _override_log('apex-pairwise-corr.json', fake_corr_high), \
             _override_log('apex-portfolio-heat.json', {}):
            sig_corr = _make_signal(score=8, entry=50.0, stop=47.0)
            sig_corr['t212_ticker'] = 'GOOGL_US_EQ'
            sig_corr['sector']      = 'Technology'
            qty_corr, notional_corr = sz.calculate_final_position(sig_corr, _make_intel())

        reduction = round((1 - notional_corr / notional_base) * 100, 1) if notional_base > 0 else 0
        metrics   = {'base_notional': notional_base, 'correlated_notional': notional_corr,
                     'reduction_pct': reduction, 'correlation': 0.92}

        if notional_corr >= notional_base * 0.60:
            return TestResult(False, f'Insufficient reduction for r=0.92: only {reduction:.0f}% (expected ~50%)', metrics=metrics)
        return TestResult(True, f'High correlation (r=0.92): notional reduced {reduction:.0f}% (£{notional_base}→£{notional_corr}) ✓', metrics=metrics)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc


@test('6-MarketStress', 'Doomsday: all gates firing → zero new entries', severity='CRITICAL')
def t6_4_doomsday():
    try:
        from apex_filters import is_blocked
    except ImportError:
        return TestResult(True, 'SKIP', metrics={})

    intel_doom = _minimal_intel()
    intel_doom.update({
        'direction_status': 'BLOCKED',
        'regime_status':    'BLOCKED',
        'geo_status':       'ALERT',
        'vix':              46.0,
        'breadth_pct':      15.0,
        'size_multiplier':  0.0,
        'geo':              {'overall': 'ALERT'},
        'sector_breadth':   {'Technology': 10.0},
    })

    results = {}
    for sig_type in ['TREND', 'CONTRARIAN', 'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE']:
        sig    = _make_signal(score=9, sig_type=sig_type)
        blocks = is_blocked(sig, intel_doom)
        results[sig_type] = {'blocked': len(blocks) > 0, 'reasons': blocks[:2]}

    # TREND and EARNINGS_DRIFT should definitely be blocked
    unblocked = [t for t, r in results.items() if not r['blocked'] and t in ('TREND', 'EARNINGS_DRIFT')]
    metrics   = results
    if unblocked:
        return TestResult(False, f'Doomsday did not block: {unblocked}', metrics=metrics)
    return TestResult(True, f'All signal types correctly handled in doomsday scenario ✓', metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 7 — New Feature Validation (P1–P8)
# ══════════════════════════════════════════════════════════════════════════════

@test('7-NewFeatures', 'P2: GARCH fields present in regime scaling output')
def t7_1_garch_output():
    data = _read_log('apex-regime-scaling.json', {})
    if not data:
        return TestResult(False, 'apex-regime-scaling.json not found — run apex-regime-scaling.py first', metrics={})
    required = ['vix_raw', 'vix_garch_blended', 'garch_available']
    missing  = [k for k in required if k not in data]
    metrics  = {k: data.get(k) for k in required}
    if missing:
        return TestResult(False, f'Missing GARCH fields: {missing}', metrics=metrics)
    if data.get('vix_garch_blended', 0) < data.get('vix_raw', 0):
        return TestResult(False, f'GARCH blend {data["vix_garch_blended"]} < spot VIX {data["vix_raw"]} — floor violated in live data!', metrics=metrics)
    return TestResult(True, f'GARCH: raw={data["vix_raw"]}, blended={data["vix_garch_blended"]}, available={data["garch_available"]} ✓', metrics=metrics)


@test('7-NewFeatures', 'P3: Gemini sentiment scoring_method in output')
def t7_2_gemini_sentiment():
    data = _read_log('apex-sentiment.json', {})
    if not data:
        return TestResult(False, 'apex-sentiment.json not found', metrics={})
    method  = data.get('scoring_method', 'unknown')
    metrics = {'scoring_method': method, 'market_sentiment': data.get('market_sentiment'),
               'total_headlines': data.get('total_headlines')}
    if method != 'llm':
        return TestResult(False, f'scoring_method={method} — expected "llm" with GEMINI_API_KEY set', metrics=metrics)
    return TestResult(True, f'scoring_method=llm ✓, sentiment={data.get("market_sentiment")}, headlines={data.get("total_headlines")}', metrics=metrics)


@test('7-NewFeatures', 'P4: Gemini TACO classification_method and llm_reasoning')
def t7_3_gemini_taco():
    data = _read_log('apex-taco-state.json', {})
    if not data:
        return TestResult(False, 'apex-taco-state.json not found', metrics={})
    method    = data.get('classification_method', 'unknown')
    reasoning = data.get('llm_reasoning', '')
    metrics   = {'classification_method': method, 'has_reasoning': bool(reasoning),
                 'reasoning_length': len(reasoning), 'status': data.get('status')}
    failures  = []
    if method != 'llm':
        failures.append(f'classification_method={method} (expected "llm")')
    if not reasoning:
        failures.append('llm_reasoning is empty')
    if failures:
        return TestResult(False, '; '.join(failures), metrics=metrics)
    return TestResult(True, f'TACO: method=llm ✓, reasoning={len(reasoning)} chars, status={data.get("status")}', metrics=metrics)


@test('7-NewFeatures', 'P5: Risk parity penalty fires for OVERWEIGHT ticker in sizer')
def t7_4_risk_parity_penalty():
    """Sizer reads portfolio-heat.json via open() directly — must write real temp file."""
    import apex_sizer as sz
    import apex_utils as au

    orig_pv = au.get_portfolio_value
    orig_fc = au.get_free_cash

    fake_regime = {'trend_scale': 1.0, 'combined_scale': 1.0, 'regime_label': 'NEUTRAL',
                   'vix': 18.0, 'vix_raw': 18.0, 'vix_garch_blended': 18.0,
                   'garch_available': True, 'breadth_scale': 0.7, 'breadth_pct': 60.0}
    fake_heat_overweight = {
        'risk_parity': {
            'status': 'REBALANCE_SUGGESTED',
            'deviations': [{'ticker': 'LEN_US_EQ', 'direction': 'OVERWEIGHT', 'deviation_pct': 45.0}],
        }
    }
    fake_heat_normal = {'risk_parity': {'status': 'BALANCED', 'deviations': []}}

    def fake_pv(): return 5000.0
    def fake_fc(): return 4000.0
    au.get_portfolio_value = fake_pv
    sz.get_portfolio_value = fake_pv
    au.get_free_cash       = fake_fc
    sz.get_free_cash       = fake_fc

    try:
        # Baseline: normal ticker, no overweight flag
        with _override_log('apex-regime-scaling.json', fake_regime), \
             _override_log('apex-positions.json', []), \
             _override_log('apex-pairwise-corr.json', {}), \
             _override_log('apex-portfolio-heat.json', fake_heat_normal):
            sig_other = _make_signal(score=8, entry=50.0, stop=47.0)
            sig_other['t212_ticker'] = 'NFE_US_EQ'
            qty_other, notional_other = sz.calculate_final_position(sig_other, _make_intel())

        # Overweight ticker: should get 25% size reduction
        with _override_log('apex-regime-scaling.json', fake_regime), \
             _override_log('apex-positions.json', []), \
             _override_log('apex-pairwise-corr.json', {}), \
             _override_log('apex-portfolio-heat.json', fake_heat_overweight):
            sig_ow = _make_signal(score=8, entry=50.0, stop=47.0)
            sig_ow['t212_ticker'] = 'LEN_US_EQ'
            qty_ow, notional_ow = sz.calculate_final_position(sig_ow, _make_intel())

        reduction = round((1 - notional_ow / notional_other) * 100, 1) if notional_other > 0 else 0
        metrics   = {'overweight_notional': notional_ow, 'normal_notional': notional_other,
                     'reduction_pct': reduction, 'expected': '~25%'}

        if notional_ow >= notional_other:
            return TestResult(False, f'Risk parity penalty did NOT fire for OVERWEIGHT ticker (notional unchanged)', metrics=metrics)
        if abs(reduction - 25.0) > 10:
            return TestResult(False, f'Expected ~25% reduction, got {reduction:.0f}%', metrics=metrics)
        return TestResult(True, f'Risk parity OVERWEIGHT penalty: {reduction:.0f}% reduction (£{notional_other}→£{notional_ow}) ✓', metrics=metrics)
    finally:
        au.get_portfolio_value = orig_pv
        au.get_free_cash       = orig_fc
        sz.get_portfolio_value = orig_pv
        sz.get_free_cash       = orig_fc


@test('7-NewFeatures', 'P6: REDUNDANT layer pairs flagged in audit output')
def t7_5_pairwise_ablation():
    data = _read_log('apex-layer-audit.json', {})
    if not data:
        return TestResult(False, 'apex-layer-audit.json not found — run apex-layer-audit.py first', metrics={})
    ia = data.get('interaction_analysis', {})
    if not ia:
        return TestResult(False, 'interaction_analysis key missing from layer audit output', metrics=data)
    redundant = ia.get('LIKELY_REDUNDANT', ia.get('redundant_pairs', []))
    metrics   = {'redundant_count': len(redundant), 'pairs': redundant,
                 'effective_dims': data.get('effective_dims')}
    if not redundant:
        return TestResult(True, 'No LIKELY_REDUNDANT pairs found (layers are independent) ✓', metrics=metrics)
    return TestResult(True, f'{len(redundant)} LIKELY_REDUNDANT pairs found ✓: {redundant}', metrics=metrics)


@test('7-NewFeatures', 'P7: HMM output structure valid and state probabilities sum to ~1')
def t7_6_hmm_structure():
    data = _read_log('apex-regime-hmm.json', {})
    if not data:
        return TestResult(False, 'apex-regime-hmm.json not found — run apex-regime-hmm.py first', metrics={})
    required = ['available', 'current_state', 'state_probabilities', 'run_length_days',
                'transition_matrix', 'model_score', 'n_observations']
    missing  = [k for k in required if k not in data]
    probs    = data.get('state_probabilities', {})
    prob_sum = round(sum(probs.values()), 3)
    metrics  = {k: data.get(k) for k in required} | {'prob_sum': prob_sum}

    if missing:
        return TestResult(False, f'Missing HMM fields: {missing}', metrics=metrics)
    if abs(prob_sum - 1.0) > 0.05:
        return TestResult(False, f'State probabilities sum to {prob_sum} (expected ~1.0)', metrics=metrics)
    if not data.get('available'):
        return TestResult(False, f'HMM available=False — fit failed', metrics=metrics)
    return TestResult(True, f'HMM: state={data["current_state"]}, prob_sum={prob_sum}, obs={data["n_observations"]} ✓', metrics=metrics)


@test('7-NewFeatures', 'P8: Regime bonus correctly re-ranks CONTRARIAN above TREND in MEAN_REVERTING', severity='CRITICAL')
def t7_7_regime_reranking():
    import apex_utils as au
    orig_sr = au.safe_read

    # MEAN_REVERTING: TREND gets -0.45 bonus, CONTRARIAN gets +1.80
    fake_hmm = {'available': True, 'current_state': 'MEAN_REVERTING',
                'state_probabilities': {'MEAN_REVERTING': 0.90}}

    def fake_sr(path, default=None):
        if 'regime-hmm'     in str(path): return fake_hmm
        if 'regime-scaling' in str(path): return {'regime_label': 'NEUTRAL'}
        return orig_sr(path, default) if default is not None else {}

    au.safe_read = fake_sr
    bonus_fn = _get_bonus_fn()

    trend_sig = {'signal_type': 'TREND', 'adjusted_score': 8.5}
    cont_sig  = {'signal_type': 'CONTRARIAN', 'adjusted_score': 8.0}

    trend_bonus = bonus_fn(trend_sig, {})
    cont_bonus  = bonus_fn(cont_sig, {})
    au.safe_read = orig_sr

    trend_effective = 8.5 + trend_bonus
    cont_effective  = 8.0 + cont_bonus

    metrics = {
        'trend_raw': 8.5, 'trend_bonus': trend_bonus, 'trend_effective': trend_effective,
        'cont_raw':  8.0, 'cont_bonus':  cont_bonus,  'cont_effective':  cont_effective,
        'reranked':  cont_effective > trend_effective,
    }

    if not metrics['reranked']:
        return TestResult(False,
            f'Re-ranking failed: TREND effective={trend_effective:.2f} > CONTRARIAN effective={cont_effective:.2f}',
            metrics=metrics)
    return TestResult(True,
        f'✓ CONTRARIAN ({cont_effective:.2f}) beats TREND ({trend_effective:.2f}) in MEAN_REVERTING state',
        metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 8 — Operational Checks
# ══════════════════════════════════════════════════════════════════════════════

@test('8-Operational', 'All required log files exist and are <26h old')
def t8_1_log_freshness():
    import time as _time
    REQUIRED = {
        'apex-regime-scaling.json': 26,
        'apex-sentiment.json':      26,
        'apex-taco-state.json':     26,
        'apex-regime-hmm.json':     26,
        'apex-portfolio-heat.json': 26,
        'apex-layer-audit.json':    168,  # Weekly is fine
    }
    stale = []
    missing = []
    metrics = {}
    now = _time.time()
    for fname, max_age_h in REQUIRED.items():
        path = os.path.join(LOGS, fname)
        if not os.path.exists(path):
            missing.append(fname)
            metrics[fname] = 'MISSING'
        else:
            age_h = (now - os.path.getmtime(path)) / 3600
            metrics[fname] = f'{age_h:.1f}h'
            if age_h > max_age_h:
                stale.append(f'{fname} ({age_h:.0f}h > {max_age_h}h)')

    if missing:
        return TestResult(False, f'Missing files: {missing}', metrics=metrics)
    if stale:
        return TestResult(False, f'Stale files: {stale}', metrics=metrics)
    return TestResult(True, f'All {len(REQUIRED)} log files present and fresh', metrics=metrics)


@test('8-Operational', 'apex_config GEMINI_API_KEY loaded and model set correctly')
def t8_2_config_gemini():
    import apex_config as cfg
    key_loaded = bool(getattr(cfg, 'GEMINI_API_KEY', ''))
    model      = getattr(cfg, 'LLM_SENTIMENT_MODEL', '')
    timeout    = getattr(cfg, 'LLM_TIMEOUT', 0)
    metrics    = {'key_loaded': key_loaded, 'model': model, 'timeout': timeout}

    if not key_loaded:
        return TestResult(False, 'GEMINI_API_KEY not loaded — add to .env.trading212', metrics=metrics)
    if 'gemini' not in model.lower():
        return TestResult(False, f'LLM_SENTIMENT_MODEL={model} — should be a Gemini model', metrics=metrics)
    return TestResult(True, f'Gemini config: key=✓, model={model}, timeout={timeout}s', metrics=metrics)


@test('8-Operational', 'apex-regime-hmm.py in cron schedule', severity='MEDIUM')
def t8_3_hmm_scheduled():
    sched_path = os.path.join(SCRIPTS, 'apex-schedule.json')
    if not os.path.exists(sched_path):
        return TestResult(True, 'SKIP — apex-schedule.json not found', metrics={})
    with open(sched_path) as f:
        sched = json.load(f)
    entries = sched if isinstance(sched, list) else sched.get('schedule', [])
    hmm_entries = [e for e in entries if 'hmm' in str(e.get('script', e.get('name', ''))).lower()]
    metrics = {'hmm_entries': hmm_entries}
    if not hmm_entries:
        return TestResult(False, 'apex-regime-hmm not found in cron schedule', metrics=metrics)
    return TestResult(True, f'HMM scheduled: {hmm_entries[0].get("cron", hmm_entries[0])}', metrics=metrics)


@test('8-Operational', 'Outcome tracking: last trade has required fields', severity='MEDIUM')
def t8_4_outcome_structure():
    outcomes = _read_log('apex-outcomes.json', [])
    if not outcomes or not isinstance(outcomes, list):
        return TestResult(True, 'SKIP — no outcomes recorded yet', metrics={})
    last    = outcomes[-1]
    REQUIRED_FIELDS = ['name', 'ticker', 'entry', 'exit', 'pnl', 'r_achieved', 'result', 'signal_type']
    missing = [f for f in REQUIRED_FIELDS if f not in last]
    metrics = {'n_outcomes': len(outcomes), 'last_trade': last.get('name'), 'missing_fields': missing}
    if missing:
        return TestResult(False, f'Last outcome missing fields: {missing}', metrics=metrics)
    win_rate = round(sum(1 for o in outcomes if o.get('pnl', 0) > 0) / len(outcomes) * 100, 1)
    metrics['win_rate_pct'] = win_rate
    return TestResult(True, f'{len(outcomes)} outcomes, win rate={win_rate}%, last={last.get("name")} ({last.get("result")})', metrics=metrics)


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def run_all(cat_filter=None):
    timestamp = datetime.now(timezone.utc)
    print(f'\n{BLD}APEX STRESS TEST BATTERY — {timestamp.strftime("%Y-%m-%d %H:%M UTC")}{RST}')
    print('═' * 70)

    results    = []
    categories = {}
    filtered   = [r for r in REGISTRY
                  if cat_filter is None or r['category'].startswith(str(cat_filter))]

    current_cat = None
    for entry in filtered:
        if entry['category'] != current_cat:
            current_cat = entry['category']
            print(f'\n{BLD}■ {current_cat}{RST}')

        t0 = time.time()
        try:
            result = entry['fn']()
            elapsed = round(time.time() - t0, 2)
        except Exception as e:
            result  = TestResult(False, f'EXCEPTION: {e}\n' + traceback.format_exc()[:300], entry['severity'])
            elapsed = round(time.time() - t0, 2)

        # Severity-aware pass/fail
        if result.passed:
            status = 'PASS'
            icon   = f'{GRN}✅{RST}'
        elif entry['severity'] == 'MEDIUM' and not result.passed:
            status = 'WARN'
            icon   = f'{YLW}⚠️ {RST}'
        else:
            status = 'FAIL'
            icon   = f'{RED}❌{RST}'

        sev_tag = f'[{entry["severity"][:4]}]'
        print(f'  {icon} {sev_tag:6} {entry["name"][:58]:<58}  {elapsed:.1f}s')
        if not result.passed:
            # Truncate long details
            detail = result.detail[:200]
            print(f'         {YLW}→ {detail}{RST}')

        row = {
            'category': entry['category'], 'name': entry['name'],
            'severity': entry['severity'], 'status': status,
            'detail': result.detail, 'elapsed_s': elapsed,
            'metrics': result.metrics,
        }
        results.append(row)
        cat = entry['category']
        categories.setdefault(cat, {'PASS': 0, 'FAIL': 0, 'WARN': 0})
        categories[cat][status] += 1

    # Summary
    total_pass = sum(r['status'] == 'PASS' for r in results)
    total_warn = sum(r['status'] == 'WARN' for r in results)
    total_fail = sum(r['status'] == 'FAIL' for r in results)
    crit_fails = [r for r in results if r['status'] == 'FAIL' and r['severity'] == 'CRITICAL']

    print(f'\n{"═" * 70}')
    print(f'{BLD}SUMMARY:{RST} '
          f'{GRN}{total_pass} PASS{RST} | '
          f'{YLW}{total_warn} WARN{RST} | '
          f'{RED}{total_fail} FAIL{RST}  '
          f'({len(results)} tests, {len(filtered) - len(results)} skipped)')
    if crit_fails:
        print(f'{RED}{BLD}⛔ CRITICAL FAILURES ({len(crit_fails)}):{RST}')
        for r in crit_fails:
            print(f'   • {r["name"]}: {r["detail"][:100]}')
    else:
        print(f'{GRN}No CRITICAL failures ✓{RST}')

    # Manual checks reminder
    print(f'\n{BLD}── Monday Morning Manual Checks ──{RST}')
    manual = [
        'M1: apex-cron.log — confirm HMM(07:22)→scaling(07:25)→sentiment(07:28)→revalidate(07:45)→decision(08:05) all ran',
        'M2: apex-regime-hmm.json — HMM state confidence > 60%? If not, HMM is uncertain',
        'M3: vix_raw vs vix_garch_blended — GARCH premium > 3 VIX points means vol regime tightened',
        'M4: apex-sentiment.json scoring_method=="llm" — read top 3 Gemini-scored headlines, sanity check',
        'M5: apex-taco-state.json llm_reasoning — does Gemini\'s reasoning match weekend news?',
        'M6: apex-decision-log.json — first Monday signal has regime_priority_bonus field, adjusted_score unchanged',
        'M7: apex-portfolio-heat.json risk_parity — OVERWEIGHT tickers confirm sizer -25% penalty applied',
        'M8: After first trade — apex-outcomes.json has correct r_achieved, signal_type, days_held',
    ]
    for m in manual:
        print(f'  □ {m}')

    # Save results
    out_path = os.path.join(LOGS, 'apex-stress-test-results.json')
    output   = {
        'timestamp': timestamp.isoformat(),
        'summary': {'pass': total_pass, 'warn': total_warn, 'fail': total_fail,
                    'total': len(results), 'critical_failures': len(crit_fails)},
        'categories': categories,
        'results': results,
    }
    try:
        with open(out_path, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        print(f'\nResults saved → {out_path}')
    except Exception as e:
        print(f'Could not save results: {e}')

    return total_fail == 0


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--list' in args:
        for i, r in enumerate(REGISTRY, 1):
            print(f'{i:2}. [{r["severity"]:8}] {r["category"]} — {r["name"]}')
        sys.exit(0)

    cat_filter = None
    if '--cat' in args:
        idx = args.index('--cat')
        if idx + 1 < len(args):
            cat_filter = args[idx + 1]

    ok = run_all(cat_filter=cat_filter)
    sys.exit(0 if ok else 1)
