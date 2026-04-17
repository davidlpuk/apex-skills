#!/usr/bin/env python3
"""
Apex Target Profile — Phase 5 MAE/MFE-calibrated stop and target helper.

Reads apex-mae-mfe-calibration.json and returns per-signal-type target
profiles used by the scanner and sizer.

Key insight from Phase 2 baseline data:
  - Current T1 at 2.0R has only 23% hit rate
  - Median MFE = 0.84R → optimal T1 should be ~0.9R
  - Current stops are TOO TIGHT: 90.9% of losses close before stop
  - p90 loss = 0.71R → minimum stop floor 0.8R

get_target_profile() returns these calibrated values. Falls back to
hardcoded defaults from apex_config when calibration data is insufficient.
"""
import json
import sys

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

MAE_MFE_FILE = '/home/ubuntu/.picoclaw/logs/apex-mae-mfe-calibration.json'

# ── Conservative defaults (pre-calibration values) ───────────────────────────
# Widen stops from the original 2.0R-based multiplier to 0.8R minimum.
# Lower T1 to 0.9R to match empirical median MFE, scale 60% there.
_DEFAULTS = {
    'stop_floor_r':   0.80,    # Minimum stop distance (fraction of ATR-based risk)
    't1_r':           0.90,    # T1 target at 0.9R (from median MFE = 0.84R)
    't1_fraction':    0.60,    # Scale out 60% at T1
    't2_r':           2.00,    # T2 at 2.0R (from p75 MFE = 1.95R)
    't2_fraction':    0.30,    # Scale out 30% at T2
    'runner_fraction':0.10,    # Leave 10% as runner for the heavy tail
    'source':         'default',
}

# Per-type overrides for hardcoded defaults when calibration data insufficient
_TYPE_DEFAULTS = {
    'CONTRARIAN':       {**_DEFAULTS},
    'TREND':            {**_DEFAULTS, 't1_r': 1.20, 't2_r': 2.50},  # trend trades need more room
    'INVERSE':          {**_DEFAULTS, 't1_r': 0.70, 't2_r': 1.50, 'stop_floor_r': 0.60},
    'MANUAL':           {**_DEFAULTS},
    'GEO_REVERSAL':     {**_DEFAULTS},
    'EARNINGS_DRIFT':   {**_DEFAULTS, 't1_r': 0.80, 't2_r': 1.80},
    'DIVIDEND_CAPTURE': {**_DEFAULTS, 't1_r': 0.50, 't2_r': 1.00},
    'TACO_CONTRARIAN':  {**_DEFAULTS},
}


def get_target_profile(signal_type: str) -> dict:
    """
    Return stop + target profile for signal_type.

    When apex-mae-mfe-calibration.json has sufficient data (n >= 8, not
    insufficient=True), returns empirically calibrated values.
    Falls back to hardcoded defaults for signal types with insufficient n.

    Returns dict with keys:
        stop_floor_r    — minimum stop distance as fraction of risk (1.0 = full ATR)
        t1_r            — T1 target in R units
        t1_fraction     — fraction of position to close at T1
        t2_r            — T2 target in R units
        t2_fraction     — fraction of position to close at T2
        runner_fraction — fraction to hold as runner
        source          — 'empirical' | 'default'
    """
    base = dict(_TYPE_DEFAULTS.get(signal_type, _DEFAULTS))

    try:
        with open(MAE_MFE_FILE) as f:
            cal = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return base

    # Aggregate calibration (all types combined)
    agg = cal.get('aggregate', {})
    agg_mfe = agg.get('mfe', {})
    agg_mae = agg.get('mae', {})

    # Per-type calibration
    by_type = cal.get('by_signal_type', {})
    type_data = by_type.get(signal_type, {})
    type_insufficient = type_data.get('insufficient', True) or type_data.get('n', 0) < 8

    # Use per-type if sufficient, else use aggregate
    if not type_insufficient:
        mfe_data = type_data.get('mfe', {})
        mae_data = type_data.get('mae', {})
        data_source = f'empirical-{signal_type}'
    elif agg_mfe and not agg.get('insufficient', False):
        mfe_data = agg_mfe
        mae_data = agg_mae
        data_source = 'empirical-aggregate'
    else:
        return base

    # Build calibrated profile
    result = dict(base)

    # Stop floor from p90 loss (with a small buffer above median)
    p90_loss = mae_data.get('p90_loss_r')
    if p90_loss and p90_loss > 0.1:
        result['stop_floor_r'] = round(p90_loss * 1.15, 2)  # 15% buffer above p90

    # T1 from optimal_exit_r or p50 MFE, whichever is larger
    optimal_exit = mfe_data.get('optimal_exit_r')
    p50_mfe      = mfe_data.get('p50')
    if optimal_exit and optimal_exit > 0.3:
        result['t1_r'] = round(max(optimal_exit, p50_mfe or 0), 2)
    elif p50_mfe and p50_mfe > 0.3:
        result['t1_r'] = round(p50_mfe, 2)

    # T2 from optimal_t1_r (named oddly in the calibration — it's the optimal T2)
    optimal_t2 = mfe_data.get('optimal_t1_r')
    if optimal_t2 and optimal_t2 > result['t1_r']:
        result['t2_r'] = round(optimal_t2, 2)

    result['source'] = data_source
    return result


def apply_targets_to_signal(signal: dict, atr: float, entry: float) -> dict:
    """
    Return updated signal dict with calibrated target1/target2/stop values.
    Preserves all other signal fields.

    Only overrides if the calibrated values are more conservative than current:
    - Stop: only widens (never tightens beyond what was calculated)
    - Targets: can be moved closer (T1) or further (T2)
    """
    if not atr or not entry or atr <= 0 or entry <= 0:
        return signal

    st = signal.get('signal_type', 'CONTRARIAN')
    profile = get_target_profile(st)

    current_stop = float(signal.get('stop', 0) or 0)
    if current_stop <= 0:
        return signal

    current_risk = entry - current_stop
    if current_risk <= 0:
        return signal

    # Enforce stop floor: if current stop is too tight, widen it
    min_risk = atr * profile['stop_floor_r']
    if current_risk < min_risk:
        new_stop = round(entry - min_risk, 4)
        signal = {**signal, 'stop': new_stop,
                  'stop_widened': True,
                  'stop_reason': f'MAE floor {profile["stop_floor_r"]}R (was {current_risk/atr:.2f}R ATR)'}
        current_risk = min_risk

    # Recalculate targets using calibrated R values
    t1 = round(entry + current_risk * profile['t1_r'], 4)
    t2 = round(entry + current_risk * profile['t2_r'], 4)

    return {
        **signal,
        'target1':         t1,
        'target2':         t2,
        't1_fraction':     profile['t1_fraction'],
        't2_fraction':     profile['t2_fraction'],
        'runner_fraction': profile['runner_fraction'],
        'target_source':   profile['source'],
    }
