#!/usr/bin/env python3
"""
HMM Regime Detection
Fits a 3-state Gaussian Hidden Markov Model on SPY daily returns + VIX changes
to identify latent market regimes (TRENDING / MEAN_REVERTING / CRISIS).

Runs at 07:22 UTC Mon-Fri — before apex-regime-scaling.py (07:25).
Output consumed by apex-decision-engine.py for regime-aware signal priority (P8).

States (assigned post-hoc by emission means):
  TRENDING      — low vol, positive drift       → TREND signals favoured
  MEAN_REVERTING— elevated vol, reverting        → CONTRARIAN signals favoured
  CRISIS        — high vol, correlated drawdown  → INVERSE signals favoured / TREND halted

Output: /home/ubuntu/.picoclaw/logs/apex-regime-hmm.json
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f:
            json.dump(d, f, indent=2)
        return True
    def log_error(m, exc=None): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

LOG_DIR     = '/home/ubuntu/.picoclaw/logs'
OUTPUT_FILE = os.path.join(LOG_DIR, 'apex-regime-hmm.json')

logging.basicConfig(
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, 'apex-regime-hmm.log'))],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)


def _label_states(model, n_components=3):
    """Assign semantic labels to HMM states based on emission means.

    State with highest mean return → TRENDING
    State with lowest mean return  → CRISIS
    Remaining                      → MEAN_REVERTING
    """
    means = model.means_  # shape: (n_components, n_features)
    # Sort by mean return (feature 0)
    by_return = sorted(range(n_components), key=lambda i: means[i][0])
    state_map = {
        by_return[2]: 'TRENDING',
        by_return[0]: 'CRISIS',
        by_return[1]: 'MEAN_REVERTING',
    }
    return state_map


def fit_hmm():
    """Fit 3-state Gaussian HMM and return state info dict, or None on failure."""
    import numpy as np
    import yfinance as yf
    from hmmlearn.hmm import GaussianHMM

    # Fetch SPY (returns) and VIX (vol change) over 180 days
    spy = yf.download('^GSPC', period='180d', interval='1d',
                      progress=False, auto_adjust=True)
    vix = yf.download('^VIX',  period='180d', interval='1d',
                      progress=False, auto_adjust=True)

    if spy is None or vix is None or len(spy) < 90 or len(vix) < 90:
        log_warning('HMM: insufficient SPY/VIX history')
        return None

    spy_ret = spy['Close'].pct_change().dropna()
    vix_chg = vix['Close'].pct_change().dropna()

    common_idx = spy_ret.index.intersection(vix_chg.index)
    if len(common_idx) < 60:
        log_warning(f'HMM: only {len(common_idx)} common dates — need ≥60')
        return None

    spy_ret = np.array(spy_ret.loc[common_idx].values, dtype=float).flatten()
    vix_chg = np.array(vix_chg.loc[common_idx].values, dtype=float).flatten()

    # Feature matrix: [daily_pct_return, vix_pct_change]
    # Standardise so neither feature dominates due to scale differences
    raw_features = np.column_stack([spy_ret, vix_chg])
    col_means = raw_features.mean(axis=0)
    col_stds  = raw_features.std(axis=0)
    col_stds[col_stds < 1e-8] = 1.0   # avoid divide-by-zero on flat columns
    features = (raw_features - col_means) / col_stds

    model = GaussianHMM(
        n_components=3,
        covariance_type='diag',   # diagonal covariance — numerically stable, adequate for 2 features
        n_iter=200,
        random_state=42,
        tol=0.01,
    )
    model.fit(features)

    states      = model.predict(features)
    probs       = model.predict_proba(features)
    state_map   = _label_states(model)

    current_state_idx = int(states[-1])
    current_label     = state_map[current_state_idx]
    current_probs     = {
        state_map[i]: round(float(p), 4)
        for i, p in enumerate(probs[-1])
    }

    # How many consecutive days in current state?
    run_length = 1
    for i in range(len(states) - 2, -1, -1):
        if states[i] == current_state_idx:
            run_length += 1
        else:
            break

    # State emission means (returns context)
    state_means = {
        state_map[i]: {
            'daily_return_pct': round(float(model.means_[i][0]) * 100, 3),
            'vix_change_pct':   round(float(model.means_[i][1]) * 100, 3),
        }
        for i in range(3)
    }

    # Transition matrix
    transmat = {
        state_map[i]: {
            state_map[j]: round(float(model.transmat_[i][j]), 4)
            for j in range(3)
        }
        for i in range(3)
    }

    # Log-likelihood score (higher = better fit)
    model_score = round(float(model.score(features)), 2)

    return {
        'current_state':      current_label,
        'current_state_idx':  current_state_idx,
        'state_probabilities': current_probs,
        'run_length_days':    run_length,
        'state_means':        state_means,
        'transition_matrix':  transmat,
        'model_score':        model_score,
        'n_observations':     len(features),
    }


def run():
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    try:
        result = fit_hmm()
    except Exception as _e:
        log_error(f'HMM fit exception: {_e}', exc=_e)
        result = None

    if result is None:
        output = {
            'timestamp':     timestamp,
            'available':     False,
            'current_state': 'UNKNOWN',
            'fallback':      'Using VIX/breadth rule-based regime only',
        }
    else:
        output = {
            'timestamp': timestamp,
            'available': True,
            **result,
        }

    atomic_write(OUTPUT_FILE, output)

    state = output.get('current_state', 'UNKNOWN')
    run_d = output.get('run_length_days', '?')
    probs = output.get('state_probabilities', {})
    prob_str = ', '.join(f'{k}: {v:.0%}' for k, v in probs.items()) if probs else 'n/a'
    logging.info(f"HMM regime: {state} (run: {run_d}d) | probs: [{prob_str}]")
    print(f"HMM regime: {state}  run: {run_d} days  [{prob_str}]")


if __name__ == '__main__':
    run()
