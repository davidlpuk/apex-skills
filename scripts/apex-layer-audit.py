#!/usr/bin/env python3
"""
Apex Layer Audit — Multicollinearity Detection

Reads apex-decision-log.json, extracts each scoring layer's +1/-1/0 contribution
per signal, and computes pairwise Pearson correlations across all layers.

High correlation (|r| > 0.70) between two layers means they are contributing
near-identical information — the composite score treats them as independent but
they are not. The effective dimensionality of the 18-layer score is lower than
advertised, inflating confidence in composite scores.

Output:
  - Correlation matrix (terminal + apex-layer-audit.json)
  - Ranked list of highly correlated pairs
  - Effective dimensionality estimate (via eigenvalue variance explained)
  - Per-layer activation rate (how often each layer fires non-zero)

Usage:
  python3 apex-layer-audit.py            # full report
  python3 apex-layer-audit.py --summary  # correlated pairs only
"""
import json
import math
import sys
import re
from datetime import datetime, timezone
from collections import defaultdict

DECISION_LOG = '/home/ubuntu/.picoclaw/logs/apex-decision-log.json'
AUDIT_FILE   = '/home/ubuntu/.picoclaw/logs/apex-layer-audit.json'

# Correlation thresholds
HIGH_CORR    = 0.70   # Flag as strongly correlated — likely redundant
MEDIUM_CORR  = 0.50   # Note as moderately correlated


# ---------------------------------------------------------------------------
# Parse adjustment strings → layer contributions
# ---------------------------------------------------------------------------
# Example adjustments seen in log:
#   "MACRO: -1 (Copper FALLING...)"   → layer=MACRO,  value=-1
#   "FRED: -1 (FRED EXPANSION...)"    → layer=FRED,   value=-1
#   "Breadth: -1 (deterioration...)"  → layer=BREADTH, value=-1
#   "Backtest: +1 (Backtest val...)"  → layer=BACKTEST, value=+1
#   "Sector: -1 (Consumer is...)"     → layer=SECTOR,  value=-1
#   "Fundamentals: +1 (STRONG)"       → layer=FUND,    value=+1
#   "RS: +1 (...)"                    → layer=RS,      value=+1
#   "MTF: +1 (...)"                   → layer=MTF,     value=+1
#   "Insider: +1 (...)"               → layer=INSIDER, value=+1
#   "Sentiment: +1 (...)"             → layer=SENT,    value=+1
#   "Options: +1 (...)"               → layer=OPTIONS, value=+1

_LAYER_ALIAS = {
    'macro':        'MACRO',
    'fred':         'FRED',
    'breadth':      'BREADTH',
    'backtest':     'BACKTEST',
    'sector':       'SECTOR',
    'fundamentals': 'FUND',
    'fundamental':  'FUND',
    'rs':           'RS',
    'mtf':          'MTF',
    'insider':      'INSIDER',
    'sentiment':    'SENT',
    'options':      'OPTIONS',
    'geo':          'GEO',
    'regime':       'REGIME',
    'vix':          'VIX',
    'drawdown':     'DD',
    'earnings':     'EARN',
    'dividend':     'DIV',
}

_ADJ_RE = re.compile(r'^([A-Za-z_]+)\s*:\s*([+-]?\d+)', re.IGNORECASE)


def parse_adjustment(adj_str: str) -> tuple:
    """
    Parse a single adjustment string into (layer_name, value).
    Returns (None, 0) if unparseable.
    """
    m = _ADJ_RE.match(adj_str.strip())
    if not m:
        return None, 0
    raw_layer = m.group(1).lower().rstrip('_')
    value     = int(m.group(2))
    layer     = _LAYER_ALIAS.get(raw_layer, raw_layer.upper())
    return layer, value


def extract_signal_vectors(log_entries: list) -> tuple:
    """
    Build a {signal_key: {layer: value}} matrix from all log entries.

    signal_key = f"{date}_{name}_{signal_type}"
    Returns (matrix dict, sorted list of all layer names).
    """
    matrix     = {}
    all_layers = set()

    for entry in log_entries:
        date = entry.get('date', 'unknown')
        for sig in entry.get('signals', []):
            name     = sig.get('name', 'UNK')
            sig_type = sig.get('signal_type', 'UNK')
            key      = f"{date}_{name}_{sig_type}"

            # Each signal starts with all layers at 0 (not fired)
            layers = {}
            for adj in sig.get('adjustments', []):
                layer, val = parse_adjustment(adj)
                if layer:
                    all_layers.add(layer)
                    # Sum contributions from same layer (e.g. two MACRO lines)
                    layers[layer] = layers.get(layer, 0) + val

            if layers:  # Only include signals that had at least one layer fire
                matrix[key] = layers

    layer_list = sorted(all_layers)
    return matrix, layer_list


# ---------------------------------------------------------------------------
# Statistics (no numpy required)
# ---------------------------------------------------------------------------
def _col(matrix: dict, layer: str, keys: list) -> list:
    """Extract column vector for a layer (0 if layer absent for that signal)."""
    return [matrix[k].get(layer, 0) for k in keys]


def _pearson(x: list, y: list) -> float:
    """Pearson correlation coefficient between two equal-length lists."""
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num   = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom = math.sqrt(
        sum((xi - mx) ** 2 for xi in x) *
        sum((yi - my) ** 2 for yi in y)
    )
    if denom == 0:
        return 0.0
    return round(num / denom, 4)


def build_corr_matrix(matrix: dict, layer_list: list) -> dict:
    """Compute full pairwise Pearson correlation matrix."""
    keys  = list(matrix.keys())
    corr  = {}
    for i, la in enumerate(layer_list):
        xa = _col(matrix, la, keys)
        for j, lb in enumerate(layer_list):
            if j < i:
                corr[(la, lb)] = corr.get((lb, la), 0.0)
            elif j == i:
                corr[(la, lb)] = 1.0
            else:
                xb = _col(matrix, lb, keys)
                corr[(la, lb)] = _pearson(xa, xb)
    return corr


def effective_dimensionality(matrix: dict, layer_list: list) -> dict:
    """
    Estimate effective dimensionality from the correlation matrix eigenvalues
    using the participation ratio (PR):

        PR = (Σλᵢ)² / Σλᵢ²

    PR = n  → all layers independent (best case)
    PR = 1  → all layers perfectly correlated (worst case)

    We approximate eigenvalues using the Gershgorin circle theorem bound and
    a simple power-iteration estimate since we avoid numpy.
    Here we use the simpler variance-explained approach: compute row sums of
    |correlation| as a proxy for each layer's redundancy.
    """
    keys  = list(matrix.keys())
    n     = len(layer_list)
    if n == 0:
        return {'effective_n': 0, 'redundancy_pct': 0}

    # Average absolute off-diagonal correlation per layer
    avg_off_diag = {}
    for la in layer_list:
        xa   = _col(matrix, la, keys)
        cors = []
        for lb in layer_list:
            if lb == la:
                continue
            xb = _col(matrix, lb, keys)
            cors.append(abs(_pearson(xa, xb)))
        avg_off_diag[la] = round(sum(cors) / len(cors), 4) if cors else 0.0

    mean_abs_corr = sum(avg_off_diag.values()) / n
    # PR approximation: layers with mean |r|=0 are fully independent → eff_n = n
    # layers with mean |r|=1 are fully redundant → eff_n = 1
    eff_n        = round(n * (1 - mean_abs_corr), 1)
    redundancy   = round(mean_abs_corr * 100, 1)

    return {
        'n_layers':        n,
        'effective_n':     max(1.0, eff_n),
        'mean_abs_corr':   round(mean_abs_corr, 4),
        'redundancy_pct':  redundancy,
        'per_layer_avg_abs_corr': avg_off_diag,
    }


# ---------------------------------------------------------------------------
# Main Report
# ---------------------------------------------------------------------------
def run(summary_only: bool = False):
    now = datetime.now(timezone.utc)
    print(f"\n{'='*65}")
    print(f"APEX LAYER AUDIT — Multicollinearity Report")
    print(f"{'='*65}")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    try:
        with open(DECISION_LOG) as f:
            log_entries = json.load(f)
        if not isinstance(log_entries, list):
            log_entries = [log_entries]
    except Exception as e:
        print(f"ERROR: Could not load {DECISION_LOG}: {e}")
        sys.exit(1)

    matrix, layer_list = extract_signal_vectors(log_entries)

    n_signals = len(matrix)
    n_layers  = len(layer_list)
    print(f"  Signals parsed:  {n_signals}")
    print(f"  Layers detected: {n_layers}  ({', '.join(layer_list)})")

    if n_signals < 5:
        print(f"\n  WARNING: Only {n_signals} signal vectors — correlations unreliable.")
        print(f"  Need ≥20 signals for meaningful multicollinearity analysis.")
        print(f"  Re-run after more decision-log entries accumulate.\n")
        # Still generate the output, but flag it
        low_data = True
    else:
        low_data = False

    # --- Activation rates ---
    keys = list(matrix.keys())
    print(f"\n  Layer Activation Rates (% of signals where layer fired non-zero):")
    print(f"  {'Layer':12} {'Active%':8} {'Mean contrib':12} {'Pos':6} {'Neg':6}")
    print(f"  {'-'*50}")
    activation = {}
    for la in layer_list:
        col      = _col(matrix, la, keys)
        active   = sum(1 for v in col if v != 0)
        pos      = sum(1 for v in col if v > 0)
        neg      = sum(1 for v in col if v < 0)
        mean_val = round(sum(col) / len(col), 3) if col else 0
        act_pct  = round(active / len(col) * 100, 1) if col else 0
        activation[la] = {'active_pct': act_pct, 'mean': mean_val, 'pos': pos, 'neg': neg}
        print(f"  {la:12} {act_pct:7.1f}%  {mean_val:+.3f}        {pos:5}  {neg:5}")

    # --- Pairwise correlations ---
    corr = build_corr_matrix(matrix, layer_list)

    # Extract off-diagonal pairs sorted by |r|
    pairs = []
    for i, la in enumerate(layer_list):
        for j, lb in enumerate(layer_list):
            if j <= i:
                continue
            r = corr[(la, lb)]
            pairs.append((abs(r), r, la, lb))
    pairs.sort(reverse=True)

    high_pairs   = [(r, la, lb) for (ar, r, la, lb) in pairs if ar >= HIGH_CORR]
    medium_pairs = [(r, la, lb) for (ar, r, la, lb) in pairs if MEDIUM_CORR <= ar < HIGH_CORR]

    print(f"\n  Highly Correlated Pairs (|r| ≥ {HIGH_CORR}) — likely redundant:")
    if high_pairs:
        for r, la, lb in high_pairs:
            direction = 'move together' if r > 0 else 'move opposite'
            print(f"    {la:12} ↔ {lb:12}  r={r:+.3f}  ({direction})")
    else:
        print(f"    None — no pairs above threshold")

    print(f"\n  Moderately Correlated Pairs ({MEDIUM_CORR} ≤ |r| < {HIGH_CORR}):")
    if medium_pairs:
        for r, la, lb in medium_pairs[:10]:  # top 10
            print(f"    {la:12} ↔ {lb:12}  r={r:+.3f}")
        if len(medium_pairs) > 10:
            print(f"    ... and {len(medium_pairs)-10} more")
    else:
        print(f"    None")

    # --- Effective dimensionality ---
    eff = effective_dimensionality(matrix, layer_list)
    print(f"\n  Effective Dimensionality:")
    print(f"    Nominal layers:  {eff['n_layers']}")
    print(f"    Effective dims:  ~{eff['effective_n']}  (higher = more independent)")
    print(f"    Mean |r|:        {eff['mean_abs_corr']}")
    print(f"    Redundancy est:  {eff['redundancy_pct']}%")

    if eff['redundancy_pct'] >= 40:
        print(f"\n    !! HIGH REDUNDANCY: composite score treats {eff['n_layers']} layers")
        print(f"       as independent but effective information content is ~{eff['effective_n']}.")
        print(f"       Consider merging or deactivating the most correlated pairs.")
    elif eff['redundancy_pct'] >= 20:
        print(f"\n    MODERATE REDUNDANCY: {eff['n_layers']} layers, ~{eff['effective_n']} independent dimensions.")
        print(f"       Monitor correlated pairs — if new layers are added, audit again.")
    else:
        print(f"\n    LOW REDUNDANCY: layers are largely independent.")

    # --- Full correlation matrix ---
    if not summary_only and n_layers <= 20:
        print(f"\n  Full Correlation Matrix:")
        header = f"  {'':12}" + "".join(f"{la:>8}" for la in layer_list)
        print(header)
        for la in layer_list:
            row = f"  {la:12}"
            for lb in layer_list:
                r = corr[(la, lb)]
                if la == lb:
                    row += f"{'1.00':>8}"
                elif abs(r) >= HIGH_CORR:
                    row += f"  !!{r:+.2f}"
                elif abs(r) >= MEDIUM_CORR:
                    row += f"   ~{r:+.2f}"
                else:
                    row += f"  {r:+.3f}"
            print(row)

    # --- Save output ---
    output = {
        'timestamp':        now.strftime('%Y-%m-%d %H:%M UTC'),
        'n_signals':        n_signals,
        'n_layers':         n_layers,
        'layers':           layer_list,
        'low_data_warning': low_data,
        'activation':       activation,
        'effective_dims':   eff,
        'high_corr_pairs':  [{'la': la, 'lb': lb, 'r': r}
                              for r, la, lb in high_pairs],
        'medium_corr_pairs': [{'la': la, 'lb': lb, 'r': r}
                               for r, la, lb in medium_pairs],
        'all_pairs': [{'la': la, 'lb': lb, 'r': r}
                      for (_, r, la, lb) in sorted(pairs, key=lambda x: -abs(x[0]))],
    }

    try:
        with open(AUDIT_FILE, 'w') as f:
            json.dump(output, f, indent=2)
        print(f"\n  Saved to {AUDIT_FILE}")
    except Exception as e:
        print(f"  WARNING: Could not save audit file: {e}")

    print(f"{'='*65}\n")
    return output


if __name__ == '__main__':
    summary = '--summary' in sys.argv
    run(summary_only=summary)
