#!/usr/bin/env python3
"""
Apex Bayesian Layer Weight Optimizer
Maintains Beta(alpha, beta) distributions per scoring layer, updated from
decision logs + trade outcomes. Outputs continuous weights [0.3, 1.5].

Each time it runs it:
  1. Loads existing state (alpha/beta) from apex-learned-weights.json (if present)
  2. Parses new (signal, outcome) pairs not yet seen
  3. Updates Beta posteriors for each layer that fired on those signals
  4. Converts posteriors to continuous weights [0.3, 1.5]
  5. Writes apex-learned-weights.json

CLI flags:
  --reset   Clear all learned state and restart from priors
  --dry-run Print analysis without writing output
"""

import json
import os
import sys
import math
import re
import argparse
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, log_info
except ImportError:
    def atomic_write(p, d):
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, p)

    def safe_read(p, d=None):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return d if d is not None else {}

    def log_error(m):
        print(f'ERROR: {m}')

    def log_warning(m):
        print(f'WARNING: {m}')

    def log_info(m):
        print(f'INFO: {m}')


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOGS           = '/home/ubuntu/.picoclaw/logs'
DECISION_LOG   = f'{LOGS}/apex-decision-log.json'
OUTCOMES_FILE  = f'{LOGS}/apex-outcomes.json'
INSIGHTS_FILE  = f'{LOGS}/apex-backtest-v2-insights.json'
OUTPUT_FILE    = f'{LOGS}/apex-learned-weights.json'

# ---------------------------------------------------------------------------
# Layer aliases — map raw strings from adjustment logs to canonical names
# ---------------------------------------------------------------------------
LAYER_ALIASES = {
    'macro':               'MACRO',
    'fred':                'FRED',
    'breadth':             'BREADTH',
    'backtest':            'BACKTEST',
    'sector':              'SECTOR',
    'fundamentals':        'FUND',
    'fund5':               'FUND5',
    'sentiment':           'SENTIMENT',
    'insider':             'INSIDER',
    'options':             'OPTIONS',
    'volume':              'VOLUME',
    'rs':                  'RS',
    'relative strength':   'RS',
    'mtf':                 'MTF',
    'multi-timeframe':     'MTF',
    'divergence':          'DIVERGENCE',
    'earnings revision':   'EARNINGS_REV',
    'geo':                 'GEO',
    'vix':                 'VIX',
    'redundancy discount': 'REDUNDANCY',
}

# All known canonical layer names (used to seed priors)
ALL_LAYERS = [
    'MACRO', 'FRED', 'BREADTH', 'BACKTEST', 'SECTOR',
    'FUND', 'FUND5', 'SENTIMENT', 'INSIDER', 'OPTIONS',
    'VOLUME', 'RS', 'MTF', 'DIVERGENCE', 'EARNINGS_REV',
    'GEO', 'VIX', 'REDUNDANCY',
]

# Uninformative prior: Beta(5, 5) => posterior mean 0.5, moderate uncertainty
PRIOR_ALPHA = 5
PRIOR_BETA  = 5

# Weight scaling: weight = 0.3 + 1.2 * posterior_mean
#   posterior_mean=0.0 => weight=0.30  (layer actively hurting — downweighted)
#   posterior_mean=0.5 => weight=0.90  (neutral / uninformative)
#   posterior_mean=1.0 => weight=1.50  (layer reliably predictive — upweighted)
WEIGHT_SCALE_MIN = 0.3
WEIGHT_SCALE_RANGE = 1.2  # 1.5 - 0.3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wilson_ci(wins: int, total: int, z: float = 1.96):
    """Return (center, lower, upper) Wilson score confidence interval."""
    if total == 0:
        return 0.5, 0.0, 1.0
    p = wins / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return center, max(0.0, center - margin), min(1.0, center + margin)


def posterior_mean(alpha: float, beta: float) -> float:
    return alpha / (alpha + beta)


def weight_from_posterior(pm: float) -> float:
    """Scale posterior mean [0,1] to weight [0.3, 1.5]."""
    return round(WEIGHT_SCALE_MIN + WEIGHT_SCALE_RANGE * pm, 4)


def resolve_layer(raw_name: str) -> str | None:
    """Map a raw layer name from adjustment strings to canonical layer name."""
    key = raw_name.strip().lower()
    return LAYER_ALIASES.get(key)


def parse_adjustment(adj_str: str):
    """
    Parse an adjustment string like 'MACRO: -1 (reason...)' or 'Sector: +3 (...)'
    Returns (layer_canonical, value) or None if unparseable or meta-entry.
    """
    # Skip meta entries like 'Adjustment cap: ...'
    if re.match(r'adjustment\s+cap', adj_str, re.IGNORECASE):
        return None

    # Match pattern: WORD_OR_PHRASE: [+-]NUMBER
    m = re.match(r'^([A-Za-z][A-Za-z0-9 _\-]*?):\s*([+-]?\d+(?:\.\d+)?)', adj_str.strip())
    if not m:
        return None

    raw_name = m.group(1).strip()
    value    = float(m.group(2))
    layer    = resolve_layer(raw_name)
    if layer is None:
        # Try direct uppercase match for layers not in alias map
        layer = raw_name.upper().replace(' ', '_')
    return layer, value


def build_outcomes_lookup(trades: list) -> dict:
    """
    Return a dict keyed by lowercase ticker and lowercase name -> trade dict.
    Only includes closed trades (have a 'closed' field).
    """
    lookup = {}
    for t in trades:
        if not t.get('closed'):
            continue
        ticker = (t.get('ticker') or '').lower().strip()
        name   = (t.get('name') or '').lower().strip()
        if ticker:
            lookup[ticker] = t
        if name:
            lookup[name] = t
        # Also try just the root ticker without exchange suffix (e.g. 'xom' from 'XOMl_EQ')
        if '_' in ticker:
            root = ticker.split('_')[0].rstrip('l').lower()
            if root:
                lookup[root] = t
    return lookup


def find_outcome_for_signal(signal_name: str, ticker_hint: str | None,
                             outcomes_lookup: dict):
    """
    Try to match a decision-log signal name/ticker to a closed trade.
    Returns the trade dict or None.
    """
    candidates = [
        signal_name.lower().strip(),
        (ticker_hint or '').lower().strip(),
    ]
    # Also try first word of name (e.g. 'Exxon Mobil' -> 'exxon')
    first_word = signal_name.split()[0].lower() if signal_name else ''
    if first_word:
        candidates.append(first_word)

    for c in candidates:
        if c and c in outcomes_lookup:
            return outcomes_lookup[c]
    return None


def is_win(trade: dict) -> bool:
    """Return True if the trade was a net winner."""
    result = (trade.get('result') or '').upper()
    if result in ('WIN', 'MANUAL_WIN', 'TARGET1', 'TARGET2', 'PARTIAL_WIN'):
        return True
    if result in ('LOSS', 'STOP', 'MANUAL_LOSS'):
        return False
    # Fall back to pnl
    return trade.get('pnl', 0) > 0


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_existing_state() -> dict:
    """Load previously saved apex-learned-weights.json, or return empty dict."""
    return safe_read(OUTPUT_FILE, {})


def build_initial_layers(existing_state: dict, reset: bool) -> dict:
    """
    Build the layer-state dict.
    Structure per layer:
      { alpha, beta, n_fires, n_wins, seen_run_keys: [] }
    """
    if reset or 'layers' not in existing_state:
        layers = {}
        for l in ALL_LAYERS:
            layers[l] = {
                'alpha':          float(PRIOR_ALPHA),
                'beta':           float(PRIOR_BETA),
                'n_fires':        0,
                'n_wins':         0,
                'seen_run_keys':  [],
            }
        return layers

    layers = {}
    for l in ALL_LAYERS:
        prev = existing_state.get('layers', {}).get(l, {})
        layers[l] = {
            'alpha':         float(prev.get('alpha', PRIOR_ALPHA)),
            'beta':          float(prev.get('beta',  PRIOR_BETA)),
            'n_fires':       int(prev.get('n_fires', 0)),
            'n_wins':        int(prev.get('n_wins',  0)),
            'seen_run_keys': list(prev.get('seen_run_keys', [])),
        }
    return layers


def get_already_seen_pairs(existing_state: dict) -> set:
    """
    Return set of (run_key) strings already incorporated into the model.
    run_key = f"{run_timestamp}|{signal_name}"
    """
    return set(existing_state.get('seen_pairs', []))


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_decision_log(decision_log: list, outcomes_lookup: dict,
                          layers: dict, already_seen: set):
    """
    Iterate all decision-log runs. For each run that had a best_signal:
      - Try to match best_signal to a closed trade
      - For each adjustment on that signal, Bayesian-update the relevant layer

    Returns:
      n_signals_matched, n_adj_observations, new_seen_pairs (list)
    """
    n_signals_matched    = 0
    n_adj_observations   = 0
    new_seen_pairs       = []

    for run in decision_log:
        run_ts    = run.get('timestamp') or run.get('date') or 'unknown'
        best_name = run.get('best_signal') or ''
        if not best_name:
            continue

        # Find the best_signal's entry in this run's signals list
        best_sig = None
        for sig in run.get('signals', []):
            if sig.get('name', '').strip().upper() == best_name.strip().upper():
                best_sig = sig
                break

        if best_sig is None:
            continue

        pair_key = f"{run_ts}|{best_name}"
        if pair_key in already_seen:
            continue

        # Try to match to an outcome
        trade = find_outcome_for_signal(
            best_name,
            best_sig.get('ticker'),
            outcomes_lookup,
        )
        if trade is None:
            # No outcome yet — skip (we'll process when outcome arrives)
            continue

        won = is_win(trade)
        signal_type = (best_sig.get('signal_type') or '').upper()
        n_signals_matched += 1
        new_seen_pairs.append(pair_key)

        for adj_str in best_sig.get('adjustments', []):
            parsed = parse_adjustment(adj_str)
            if parsed is None:
                continue
            layer, value = parsed

            # Ensure the layer exists (might be a novel layer from future versions)
            if layer not in layers:
                layers[layer] = {
                    'alpha':         float(PRIOR_ALPHA),
                    'beta':          float(PRIOR_BETA),
                    'n_fires':       0,
                    'n_wins':        0,
                    'seen_run_keys': [],
                }

            # Did this layer's adjustment agree with the outcome?
            # For INVERSE signals: a bullish adjustment (value > 0) on an inverse
            # instrument means the script expects downside — a "win" for an inverse
            # trade is when the market goes down, which matches the positive inverse adj.
            # We treat it symmetrically: positive adj => expected gain, negative => expected loss.
            # layer_agreed = (positive adj AND trade won) OR (negative adj AND trade lost)
            layer_agreed = (value > 0 and won) or (value < 0 and not won)

            layers[layer]['n_fires'] += 1
            if layer_agreed:
                layers[layer]['alpha'] += 1.0
                layers[layer]['n_wins'] += 1
            else:
                layers[layer]['beta'] += 1.0

            n_adj_observations += 1

    return n_signals_matched, n_adj_observations, new_seen_pairs


def compute_calibration(decision_log: list, outcomes_lookup: dict) -> dict:
    """
    Global calibration: compare adj_score (proxy for win probability) to actual outcomes.
    Returns a dict with n_matched, predicted_avg_score, actual_win_rate, brier_score.
    """
    matched_scores  = []
    matched_outcomes = []

    for run in decision_log:
        best_name = run.get('best_signal') or ''
        if not best_name:
            continue

        best_sig = None
        for sig in run.get('signals', []):
            if sig.get('name', '').strip().upper() == best_name.strip().upper():
                best_sig = sig
                break

        if best_sig is None:
            continue

        trade = find_outcome_for_signal(best_name, best_sig.get('ticker'), outcomes_lookup)
        if trade is None:
            continue

        adj_score = best_sig.get('adj_score', best_sig.get('raw_score', 5.0))
        matched_scores.append(float(adj_score))
        matched_outcomes.append(1 if is_win(trade) else 0)

    n = len(matched_scores)
    if n == 0:
        return {'n_matched': 0, 'predicted_avg_score': None,
                'actual_win_rate': None, 'brier_score': None}

    avg_score   = sum(matched_scores) / n
    actual_wr   = sum(matched_outcomes) / n

    # Normalise scores to [0, 1] for Brier: assume score range 0-15
    predicted_probs = [min(1.0, max(0.0, s / 15.0)) for s in matched_scores]
    brier = sum((p - o) ** 2 for p, o in zip(predicted_probs, matched_outcomes)) / n

    return {
        'n_matched':           n,
        'predicted_avg_score': round(avg_score, 2),
        'actual_win_rate':     round(actual_wr, 4),
        'brier_score':         round(brier, 4),
    }


def build_output(layers: dict, n_signals_matched: int, n_adj_observations: int,
                 calibration: dict, seen_pairs: list) -> dict:
    """Assemble the output JSON structure."""
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    layer_output = {}
    for layer_name, state in sorted(layers.items()):
        a     = state['alpha']
        b     = state['beta']
        pm    = posterior_mean(a, b)
        w     = weight_from_posterior(pm)
        total = state['n_fires']
        wins  = state['n_wins']
        acc   = round(wins / total, 4) if total > 0 else None

        # Wilson CI on the posterior mean (using actual fire/win counts)
        obs_n = int(a + b - PRIOR_ALPHA - PRIOR_BETA)  # net observations added
        obs_w = int(a - PRIOR_ALPHA)                    # net wins added
        _, ci_low_raw, ci_hi_raw = wilson_ci(max(0, obs_w), max(0, obs_n))

        # Express CI as weights
        ci_low_w = round(weight_from_posterior(ci_low_raw), 4)
        ci_hi_w  = round(weight_from_posterior(ci_hi_raw),  4)

        layer_output[layer_name] = {
            'alpha':    round(a, 2),
            'beta':     round(b, 2),
            'weight':   w,
            'ci_low':   ci_low_w,
            'ci_high':  ci_hi_w,
            'n_fires':  total,
            'accuracy': acc,
        }

    return {
        'version':                   2,
        'generated':                 now_str,
        'n_signals_matched':         n_signals_matched,
        'n_adjustment_observations': n_adj_observations,
        'layers':                    layer_output,
        'global_calibration':        calibration,
        'seen_pairs':                seen_pairs,
    }


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(output: dict, decision_log: list, trades: list) -> None:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    n_runs    = len(decision_log)
    n_signals = sum(len(r.get('signals', [])) for r in decision_log)

    print(f"\n  APEX BAYESIAN WEIGHT OPTIMIZER -- {today}")
    print(f"  Decision log: {n_runs} runs, {n_signals} signals")
    print(f"  Outcomes:     {len(trades)} trades")
    print(f"  Matched signal->outcome pairs: {output['n_signals_matched']}")
    print(f"  Adjustment observations processed: {output['n_adjustment_observations']}")

    print(f"\n  Layer Performance (sorted by weight desc):")
    print(f"  {'Layer':<16} | {'alpha':>5}  {'beta':>5}  | {'weight':>6} | {'accuracy':>9} | {'fires':>5}")
    print(f"  {'-'*16}-+-{'-'*5}--{'-'*5}--+-{'-'*6}-+-{'-'*9}-+-{'-'*5}")

    layers = output.get('layers', {})
    for layer_name, info in sorted(layers.items(), key=lambda x: -x[1]['weight']):
        acc_str = f"{info['accuracy']*100:.0f}%" if info['accuracy'] is not None else "  n/a"
        print(
            f"  {layer_name:<16} | "
            f"a={info['alpha']:<5.1f}  b={info['beta']:<5.1f}  | "
            f"{info['weight']:>6.3f} | "
            f"{acc_str:>9} | "
            f"{info['n_fires']:>5}"
        )

    cal = output.get('global_calibration', {})
    if cal.get('n_matched'):
        print(
            f"\n  Calibration: predicted {cal['predicted_avg_score']} avg score -> "
            f"actual {cal['actual_win_rate']*100:.0f}% WR | "
            f"Brier: {cal['brier_score']}"
        )
    else:
        print("\n  Calibration: no matched (signal, outcome) pairs yet")

    print(f"\n  Weights saved to {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(reset: bool = False, dry_run: bool = False) -> dict:
    # --- Load inputs ---
    decision_log = safe_read(DECISION_LOG, [])
    if not isinstance(decision_log, list):
        decision_log = []

    outcomes_data  = safe_read(OUTCOMES_FILE, {'trades': []})
    trades         = outcomes_data.get('trades', [])
    outcomes_lookup = build_outcomes_lookup(trades)

    # --- Load/initialise state ---
    existing_state  = {} if reset else load_existing_state()
    layers          = build_initial_layers(existing_state, reset)
    already_seen    = set() if reset else get_already_seen_pairs(existing_state)

    # --- Process decision log ---
    n_matched, n_obs, new_pairs = process_decision_log(
        decision_log, outcomes_lookup, layers, already_seen
    )

    all_seen_pairs = sorted(already_seen | set(new_pairs))

    # --- Global calibration ---
    calibration = compute_calibration(decision_log, outcomes_lookup)

    # --- Build output ---
    output = build_output(layers, n_matched, n_obs, calibration, all_seen_pairs)

    if not dry_run:
        atomic_write(OUTPUT_FILE, output)
        log_info(f"apex-weight-optimizer: wrote {OUTPUT_FILE} "
                 f"(matched={n_matched}, obs={n_obs})")

    return output


def main():
    parser = argparse.ArgumentParser(
        description='Apex Bayesian Layer Weight Optimizer'
    )
    parser.add_argument('--reset',   action='store_true',
                        help='Clear all learned state and restart from Beta(5,5) priors')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute and print but do not write output file')
    args = parser.parse_args()

    # Re-load for printing summary
    decision_log  = safe_read(DECISION_LOG, [])
    outcomes_data = safe_read(OUTCOMES_FILE, {'trades': []})
    trades        = outcomes_data.get('trades', [])

    output = run(reset=args.reset, dry_run=args.dry_run)
    print_summary(output, decision_log if isinstance(decision_log, list) else [], trades)

    if args.dry_run:
        print("  [dry-run] Output NOT written.")
    if args.reset:
        print("  [reset]   All priors reset to Beta(5, 5).")


if __name__ == '__main__':
    main()
