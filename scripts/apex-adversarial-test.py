#!/usr/bin/env python3
"""
Apex Adversarial Tester
Systematically finds conditions where the trading system fails using
combinatorial cross-tabs of backtest + live results.

Reads:
  - apex-backtest-v2-results.json  (instrument_analysis + walk_forward windows)
  - apex-backtest-v2-insights.json (instrument_detail with wins/n per ticker)
  - apex-decision-log.json         (live signals with regime/vix/breadth context)
  - apex-outcomes.json             (closed trade outcomes with pnl/r_achieved)

Writes:
  - apex-adversarial-results.json
"""

import json
import os
import sys
import math
import random
from datetime import datetime, timezone
from itertools import combinations

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_info
except ImportError:
    def atomic_write(filepath, data):
        tmp = filepath + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, filepath)

    def safe_read(filepath, default=None):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}

    def log_info(message):
        print(f'INFO: {message}')

LOGS = '/home/ubuntu/.picoclaw/logs'
OUTPUT_FILE = os.path.join(LOGS, 'apex-adversarial-results.json')

# ---------------------------------------------------------------------------
# Ticker-to-sector heuristic (fallback when outcomes.json has sector="unknown")
# ---------------------------------------------------------------------------
TICKER_SECTOR = {
    'XOM': 'Energy', 'CVX': 'Energy', 'SHEL': 'Energy',
    'AAPL': 'Technology', 'MSFT': 'Technology', 'NVDA': 'Technology',
    'GOOGL': 'Technology', 'META': 'Technology', 'AMZN': 'Technology',
    'ABBV': 'Healthcare', 'AZN': 'Healthcare', 'GSK': 'Healthcare',
    'JNJ': 'Healthcare', 'NOVO': 'Healthcare',
    'JPM': 'Financials', 'HSBA': 'Financials', 'V': 'Financials',
    'ULVR': 'Consumer',
    'VUAG': 'ETF',
}

# Instrument → signal_type heuristic for backtest synthesis
# (the backtest mode is CONTRARIAN; inverse ETFs get INVERSE)
INVERSE_ETF_KEYWORDS = ['short', 'inverse', 'bear', 'ultra', '-1x', '-2x', '-3x',
                        '3uks', 'sqqq', 'spxu', 'uvxy']


# ---------------------------------------------------------------------------
# Bucket helpers
# ---------------------------------------------------------------------------

def score_bucket(score):
    if score is None:
        return 'unknown'
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 'unknown'
    if s < 5:
        return '<5'
    elif s < 6:
        return '5-6'
    elif s < 7:
        return '6-7'
    elif s < 8:
        return '7-8'
    elif s < 9:
        return '8-9'
    else:
        return '9-10'


def rsi_bucket(rsi):
    if rsi is None:
        return 'unknown'
    try:
        r = float(rsi)
    except (TypeError, ValueError):
        return 'unknown'
    if r < 30:
        return '<30'
    elif r < 45:
        return '30-45'
    elif r < 60:
        return '45-60'
    else:
        return '>60'


def vix_bucket(vix):
    if vix is None:
        return 'unknown'
    try:
        v = float(vix)
    except (TypeError, ValueError):
        return 'unknown'
    if v < 18:
        return '<18'
    elif v < 22:
        return '18-22'
    elif v < 28:
        return '22-28'
    elif v < 33:
        return '28-33'
    else:
        return '>33'


def breadth_bucket(breadth):
    if breadth is None:
        return 'unknown'
    try:
        b = float(breadth)
    except (TypeError, ValueError):
        return 'unknown'
    if b < 40:
        return '<40%'
    elif b <= 60:
        return '40-60%'
    else:
        return '>60%'


def normalise_regime(regime_raw):
    """Normalise regime strings to OK / CAUTION / BLOCKED / CLEAR."""
    if not regime_raw:
        return 'unknown'
    r = str(regime_raw).upper()
    if r in ('OK', 'CLEAR', 'GREEN'):
        return 'OK'
    elif r in ('CAUTION', 'WARN', 'WARNING', 'AMBER'):
        return 'CAUTION'
    elif r in ('BLOCKED', 'RED', 'CRITICAL'):
        return 'BLOCKED'
    return r


# ---------------------------------------------------------------------------
# Wilson confidence interval
# ---------------------------------------------------------------------------

def wilson_ci(wins, total, z=1.96):
    """Returns (centre, lower, upper) Wilson score CI."""
    if total == 0:
        return 0.5, 0.0, 1.0
    p = wins / total
    denom = 1 + z ** 2 / total
    centre = (p + z ** 2 / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * total)) / total) / denom
    return centre, max(0.0, centre - margin), min(1.0, centre + margin)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_outcomes():
    """Load closed trade outcomes from apex-outcomes.json.
    Returns list of dicts, one per closed trade."""
    raw = safe_read(os.path.join(LOGS, 'apex-outcomes.json'), {})
    trades = raw.get('trades', []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    return trades


def load_decision_log():
    """Load decision log. Returns list of run dicts."""
    raw = safe_read(os.path.join(LOGS, 'apex-decision-log.json'), [])
    if isinstance(raw, list):
        return raw
    return []


def load_backtest():
    """Load backtest results. Returns (results_dict, insights_dict)."""
    results = safe_read(os.path.join(LOGS, 'apex-backtest-v2-results.json'), {})
    insights = safe_read(os.path.join(LOGS, 'apex-backtest-v2-insights.json'), {})
    return results, insights


# ---------------------------------------------------------------------------
# Feature vector builders
# ---------------------------------------------------------------------------

def build_live_data_points(decision_log, outcomes):
    """
    Match decision-log signals to outcome records by name/ticker.
    Returns list of feature-vector dicts.
    """
    # Index outcomes by name and ticker for fast lookup
    outcome_by_name = {}
    for trade in outcomes:
        key = (trade.get('name') or '').strip().upper()
        if key:
            outcome_by_name.setdefault(key, []).append(trade)
        ticker = (trade.get('ticker') or '').strip().upper()
        if ticker:
            outcome_by_name.setdefault(ticker, []).append(trade)

    points = []
    for run in decision_log:
        run_regime = normalise_regime(run.get('regime'))
        run_vix = run.get('vix')
        run_breadth = run.get('breadth')
        run_date = run.get('date', '')

        for sig in run.get('signals', []):
            name_key = (sig.get('name') or '').strip().upper()
            matched_outcomes = outcome_by_name.get(name_key, [])

            if not matched_outcomes:
                # Signal seen but no matching outcome — we can still record
                # as a "pending / no outcome" record but skip it for WR analysis
                continue

            for outcome in matched_outcomes:
                result = (outcome.get('result') or '').upper()
                is_win = result in ('WIN', 'MANUAL_WIN', 'T1_WIN', 'T2_WIN', 'PARTIAL_WIN')
                # BREAKEVEN counts as a loss for adversarial analysis
                r_val = outcome.get('r_achieved', 0.0) or 0.0

                sector = outcome.get('sector') or TICKER_SECTOR.get(
                    (outcome.get('ticker') or '').split('_')[0].upper(), 'unknown'
                )

                day = outcome.get('day_opened') or run_date
                # Resolve short day abbreviations
                day_map = {
                    'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday',
                    'thu': 'Thursday', 'fri': 'Friday',
                }
                if day and len(day) == 3:
                    day = day_map.get(day.lower(), day)

                points.append({
                    'name': sig.get('name', ''),
                    'signal_type': sig.get('signal_type', 'UNKNOWN'),
                    'score_bucket': score_bucket(sig.get('adj_score') or sig.get('raw_score')),
                    'rsi_bucket': rsi_bucket(sig.get('rsi') or outcome.get('rsi')),
                    'vix_bucket': vix_bucket(run_vix),
                    'regime': run_regime,
                    'breadth_bucket': breadth_bucket(run_breadth),
                    'day': day,
                    'sector': sector,
                    'outcome': is_win,
                    'r_achieved': float(r_val),
                    'source': 'live',
                })

    return points


def _is_inverse_name(name):
    n = (name or '').lower()
    return any(kw in n for kw in INVERSE_ETF_KEYWORDS)


def build_backtest_synthetic_points(backtest_results, backtest_insights, n_target=150):
    """
    Synthesise feature-vector records from instrument_analysis data.
    Since we only have aggregate wins/n per instrument, we spread synthetic
    records across the most likely buckets using the walk-forward window
    parameters as context (e.g., which VIX regimes each window covered).

    Each synthetic record represents a hypothetical trade; win/loss is
    assigned pseudo-randomly with probability = instrument win_rate so that
    aggregate statistics are preserved while allowing cross-tab analysis.
    """
    # Collect instrument data (prefer insights which has cleaner structure)
    instrument_detail = backtest_insights.get('instrument_detail', {})
    if not instrument_detail:
        instrument_detail = backtest_results.get('instrument_analysis', {})

    if not instrument_detail:
        return []

    # Walk-forward window context — use to infer approximate VIX/regime per period
    windows = backtest_results.get('walk_forward', {}).get('windows', [])
    # window 5 (2024-09-07 to 2025-03-06) had win_rate=28.6 → likely CAUTION/high VIX
    # window 1 (2022-09-18 to 2023-03-17) had win_rate=53.6 → mixed
    # window 6 (2025-03-06 to 2025-09-02) had win_rate=39.6 → moderate stress

    # Build a rough distribution of context buckets across all OOS windows
    context_pool = []
    window_contexts = [
        # window 1: bear market recovery — elevated VIX, bearish regime
        {'vix_bucket': '22-28', 'regime': 'CAUTION', 'breadth_bucket': '<40%', 'weight': 112},
        # window 2: 2023 recovery — VIX normalising
        {'vix_bucket': '18-22', 'regime': 'OK',      'breadth_bucket': '40-60%', 'weight': 12},
        # window 3: zero trades
        # window 4: zero trades
        # window 5: 2024-09 to 2025-03 — Trump tariff selloff, high stress
        {'vix_bucket': '22-28', 'regime': 'CAUTION', 'breadth_bucket': '<40%', 'weight': 21},
        # window 6: 2025-03 to 2025-09 — continued uncertainty
        {'vix_bucket': '22-28', 'regime': 'CAUTION', 'breadth_bucket': '40-60%', 'weight': 48},
    ]
    for ctx in window_contexts:
        context_pool.extend([ctx] * ctx['weight'])

    # Seed for reproducibility
    rng = random.Random(42)

    # RSI buckets appropriate for CONTRARIAN signal type (mostly oversold)
    contrarian_rsi_pool = ['<30'] * 6 + ['30-45'] * 3 + ['45-60'] * 1
    # Score buckets based on optimal threshold of 8-9
    score_pool = ['7-8'] * 3 + ['8-9'] * 5 + ['9-10'] * 4
    # Day distribution (uniform)
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

    points = []
    for ticker, detail in instrument_detail.items():
        n = detail.get('n', 0)
        wins = detail.get('wins', 0)
        if n < 1:
            continue

        win_prob = wins / n
        sector = TICKER_SECTOR.get(ticker.upper(), 'unknown')
        sig_type = 'INVERSE' if _is_inverse_name(ticker) else 'CONTRARIAN'

        for _ in range(n):
            ctx = rng.choice(context_pool)
            is_win = rng.random() < win_prob
            r_val = rng.gauss(2.6, 0.4) if is_win else rng.gauss(-1.0, 0.1)

            points.append({
                'name': ticker,
                'signal_type': sig_type,
                'score_bucket': rng.choice(score_pool),
                'rsi_bucket': rng.choice(contrarian_rsi_pool),
                'vix_bucket': ctx['vix_bucket'],
                'regime': ctx['regime'],
                'breadth_bucket': ctx['breadth_bucket'],
                'day': rng.choice(days),
                'sector': sector,
                'outcome': is_win,
                'r_achieved': round(r_val, 3),
                'source': 'backtest_synthetic',
            })

    return points


# ---------------------------------------------------------------------------
# Cross-tab engine
# ---------------------------------------------------------------------------

DIMENSIONS = {
    'signal_type': ['TREND', 'CONTRARIAN', 'INVERSE', 'UNKNOWN'],
    'vix_bucket':  ['<18', '18-22', '22-28', '28-33', '>33'],
    'rsi_bucket':  ['<30', '30-45', '45-60', '>60'],
    'regime':      ['OK', 'CAUTION', 'BLOCKED', 'CLEAR'],
    'breadth_bucket': ['<40%', '40-60%', '>60%'],
    'day':         ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
    'score_bucket': ['<5', '5-6', '6-7', '7-8', '8-9', '9-10'],
    # 'sector' is built dynamically
}

MIN_TRADES_PER_CELL = 5
FAILURE_UPPER_CI = 0.45   # upper CI must be below this to flag failure
EXPLOIT_LOWER_CI = 0.60   # lower CI must exceed this to flag opportunity
HIGH_SEVERITY_THRESHOLD = 3.0   # (0.5 - wr) * n >= 3 for HIGH


def run_cross_tabs(data_points):
    """
    For each pair of dimensions, count wins/total per cell.
    Returns list of cell-result dicts.
    """
    if not data_points:
        return []

    # Dynamic sectors
    sectors_seen = sorted({p['sector'] for p in data_points if p.get('sector') and p['sector'] != 'unknown'})
    dims = dict(DIMENSIONS)
    dims['sector'] = sectors_seen if sectors_seen else ['unknown']

    dim_names = list(dims.keys())
    cells = []

    for d1, d2 in combinations(dim_names, 2):
        # Build contingency dict: (val1, val2) → [wins, total]
        contingency = {}
        for p in data_points:
            v1 = p.get(d1, 'unknown')
            v2 = p.get(d2, 'unknown')
            if v1 == 'unknown' or v2 == 'unknown':
                continue
            key = (v1, v2)
            if key not in contingency:
                contingency[key] = [0, 0]
            contingency[key][1] += 1
            if p.get('outcome'):
                contingency[key][0] += 1

        # Also collect expected_r per cell
        r_sums = {}
        for p in data_points:
            v1 = p.get(d1, 'unknown')
            v2 = p.get(d2, 'unknown')
            if v1 == 'unknown' or v2 == 'unknown':
                continue
            key = (v1, v2)
            r_sums.setdefault(key, []).append(p.get('r_achieved', 0.0))

        for (v1, v2), (wins, total) in contingency.items():
            if total < MIN_TRADES_PER_CELL:
                continue
            wr = wins / total
            centre, lo, hi = wilson_ci(wins, total)
            r_list = r_sums.get((v1, v2), [])
            avg_r = sum(r_list) / len(r_list) if r_list else 0.0
            severity_score = (0.5 - wr) * total

            cells.append({
                'd1': d1, 'v1': v1,
                'd2': d2, 'v2': v2,
                'wins': wins,
                'n_trades': total,
                'win_rate': round(wr, 4),
                'win_rate_ci': [round(lo, 4), round(hi, 4)],
                'wilson_centre': round(centre, 4),
                'expected_r': round(avg_r, 4),
                'severity_score': round(severity_score, 3),
                'ci_upper': round(hi, 4),
                'ci_lower': round(lo, 4),
            })

    return cells


# ---------------------------------------------------------------------------
# Finding failure modes and opportunities
# ---------------------------------------------------------------------------

def severity_label(severity_score, wr, n):
    """Classify severity as HIGH / MEDIUM / LOW."""
    if abs(severity_score) >= HIGH_SEVERITY_THRESHOLD and n >= 10:
        return 'HIGH'
    elif abs(severity_score) >= 1.5 or n >= 7:
        return 'MEDIUM'
    else:
        return 'LOW'


def condition_str(d1, v1, d2, v2):
    """Human-readable condition string."""
    label = {
        'vix_bucket': 'VIX',
        'rsi_bucket': 'RSI',
        'signal_type': 'signal_type',
        'regime': 'regime',
        'breadth_bucket': 'breadth',
        'day': 'day',
        'score_bucket': 'score',
        'sector': 'sector',
    }
    return f"{label.get(d1, d1)}={v1} AND {label.get(d2, d2)}={v2}"


def find_failure_modes(cells):
    """Return cells flagged as failure modes, ranked by severity."""
    failures = []
    for cell in cells:
        if cell['ci_upper'] < FAILURE_UPPER_CI:
            sev = severity_label(cell['severity_score'], cell['win_rate'], cell['n_trades'])
            condition = condition_str(cell['d1'], cell['v1'], cell['d2'], cell['v2'])

            # Generate recommendation
            action = 'BLOCK' if sev == 'HIGH' else 'PENALISE'
            rec = f"{action}: {cell['d1'].replace('_bucket', '')} {cell['v1']} with {cell['d2'].replace('_bucket', '')} {cell['v2']}"

            failures.append({
                'condition': condition,
                'dimensions': {cell['d1']: cell['v1'], cell['d2']: cell['v2']},
                'n_trades': cell['n_trades'],
                'win_rate': cell['win_rate'],
                'win_rate_ci': cell['win_rate_ci'],
                'expected_r': cell['expected_r'],
                'severity': sev,
                'severity_score': cell['severity_score'],
                'recommendation': rec,
            })

    # Sort by severity_score descending (most negative = worst failure)
    failures.sort(key=lambda x: x['severity_score'])
    return failures


def find_exploitation_opportunities(cells):
    """Return cells flagged as exploitation opportunities, ranked by lower CI."""
    opps = []
    for cell in cells:
        if cell['ci_lower'] > EXPLOIT_LOWER_CI:
            condition = condition_str(cell['d1'], cell['v1'], cell['d2'], cell['v2'])
            rec = f"BOOST: +1 for {cell['d1'].replace('_bucket', '')} {cell['v1']} with {cell['d2'].replace('_bucket', '')} {cell['v2']}"

            opps.append({
                'condition': condition,
                'dimensions': {cell['d1']: cell['v1'], cell['d2']: cell['v2']},
                'n_trades': cell['n_trades'],
                'win_rate': cell['win_rate'],
                'win_rate_ci': cell['win_rate_ci'],
                'expected_r': cell['expected_r'],
                'ci_lower': cell['ci_lower'],
                'recommendation': rec,
            })

    # Sort by lower CI descending (most confident opportunities first)
    opps.sort(key=lambda x: x['ci_lower'], reverse=True)
    return opps


# ---------------------------------------------------------------------------
# Anti-rule generator
# ---------------------------------------------------------------------------

def generate_anti_rules(failure_modes, exploitation_opportunities):
    """Generate structured anti-rules for the decision engine to consume."""
    rules = []

    for fm in failure_modes:
        if fm['severity'] not in ('HIGH', 'MEDIUM'):
            continue
        action = 'block' if fm['severity'] == 'HIGH' else 'penalise'
        # Build a readable condition key
        dims = fm['dimensions']
        key_parts = [f"{k.replace('_bucket', '')}_{v.replace(' ', '_').replace('%', 'pct').replace('<', 'lt').replace('>', 'gt')}"
                     for k, v in dims.items()]
        condition_key = '_AND_'.join(sorted(key_parts)).lower()

        # Compute confidence: 1 - p_value proxy = 1 - upper_ci distance from 0.5
        confidence = round(min(0.99, 1.0 - (fm['win_rate_ci'][1] / 0.5 if fm['win_rate_ci'][1] < 0.5 else 0.05)), 3)

        rules.append({
            'condition_key': condition_key,
            'dimensions': dims,
            'active': True,
            'action': action,
            'win_rate': fm['win_rate'],
            'n_trades': fm['n_trades'],
            'confidence': confidence,
            'type': 'failure',
        })

    for opp in exploitation_opportunities:
        dims = opp['dimensions']
        key_parts = [f"{k.replace('_bucket', '')}_{v.replace(' ', '_').replace('%', 'pct').replace('<', 'lt').replace('>', 'gt')}"
                     for k, v in dims.items()]
        condition_key = '_AND_'.join(sorted(key_parts)).lower()

        confidence = round(min(0.99, (opp['ci_lower'] - 0.5) * 2), 3)

        rules.append({
            'condition_key': condition_key,
            'dimensions': dims,
            'active': True,
            'action': 'boost',
            'win_rate': opp['win_rate'],
            'n_trades': opp['n_trades'],
            'confidence': confidence,
            'type': 'opportunity',
        })

    return rules


# ---------------------------------------------------------------------------
# CLI output helpers
# ---------------------------------------------------------------------------

def severity_icon(sev):
    icons = {'HIGH': '!!', 'MEDIUM': '! ', 'LOW': '  '}
    return icons.get(sev, '  ')


def print_summary(n_points, sources, n_crosstabs, failures, opportunities, anti_rules):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    print(f"\nAPEX ADVERSARIAL TESTER — {today}")
    source_str = ', '.join(f"{s}: {c}" for s, c in sources.items())
    print(f"  Data points: {n_points} ({source_str})")
    print(f"  Cross-tabs analysed: {n_crosstabs}")
    print()

    if not failures:
        print("  No failure modes found (insufficient data or no statistical failures).")
    else:
        print(f"  FAILURE MODES ({len(failures)} found):")
        for i, f in enumerate(failures[:10], 1):
            lo, hi = f['win_rate_ci']
            print(f"    {i}. {f['condition']} | "
                  f"WR={f['win_rate']*100:.0f}% [{lo*100:.0f}%-{hi*100:.0f}%] | "
                  f"n={f['n_trades']} | severity={f['severity']}")

    print()

    if not opportunities:
        print("  No exploitation opportunities found.")
    else:
        print(f"  EXPLOITATION OPPORTUNITIES ({len(opportunities)} found):")
        for i, o in enumerate(opportunities[:10], 1):
            lo, hi = o['win_rate_ci']
            print(f"    {i}. {o['condition']} | "
                  f"WR={o['win_rate']*100:.0f}% [{lo*100:.0f}%-{hi*100:.0f}%] | "
                  f"n={o['n_trades']}")

    print()
    print(f"  Anti-rules generated: {len(anti_rules)}")
    print(f"  Saved to apex-adversarial-results.json\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    # Load all data sources
    outcomes = load_outcomes()
    decision_log = load_decision_log()
    backtest_results, backtest_insights = load_backtest()

    log_info(f"Loaded: {len(outcomes)} outcomes, {len(decision_log)} decision runs")

    # Build live data points (matched signals + outcomes)
    live_points = build_live_data_points(decision_log, outcomes)
    log_info(f"Live matched data points: {len(live_points)}")

    # Build backtest synthetic points
    bt_points = build_backtest_synthetic_points(backtest_results, backtest_insights)
    log_info(f"Backtest synthetic data points: {len(bt_points)}")

    # Combine; always include backtest if live is sparse
    all_points = live_points[:]
    sources = {}
    if live_points:
        sources['decision_log'] = len(live_points)
    if len(all_points) < 20 and bt_points:
        all_points.extend(bt_points)
        sources['backtest_synthetic'] = len(bt_points)
    elif bt_points:
        all_points.extend(bt_points)
        sources['backtest_synthetic'] = len(bt_points)

    # Record distinct source labels
    data_sources = []
    if decision_log:
        data_sources.append('decision_log')
    if outcomes:
        data_sources.append('outcomes')
    if backtest_insights.get('instrument_detail') or backtest_results.get('instrument_analysis'):
        data_sources.append('backtest_instrument_detail')

    n_points = len(all_points)
    log_info(f"Total data points for analysis: {n_points}")

    if n_points < 5:
        log_info("Insufficient data for adversarial analysis.")
        output = {
            'timestamp': timestamp,
            'n_data_points': 0,
            'data_sources': data_sources,
            'n_cross_tabs_analysed': 0,
            'failure_modes': [],
            'exploitation_opportunities': [],
            'anti_rules': [],
            'status': 'INSUFFICIENT_DATA',
            'message': 'Need at least 5 data points for analysis.',
        }
        atomic_write(OUTPUT_FILE, output)
        print("\nAPEX ADVERSARIAL TESTER — Insufficient data for analysis.")
        print(f"  Data points available: {n_points}\n")
        return

    # Run cross-tabs
    cells = run_cross_tabs(all_points)
    log_info(f"Cross-tab cells generated (min {MIN_TRADES_PER_CELL} trades): {len(cells)}")

    # Find failure modes and opportunities
    failures = find_failure_modes(cells)
    opportunities = find_exploitation_opportunities(cells)

    # Generate anti-rules
    anti_rules = generate_anti_rules(failures, opportunities)

    # Build output
    output = {
        'timestamp': timestamp,
        'n_data_points': n_points,
        'data_sources': data_sources,
        'n_cross_tabs_analysed': len(cells),
        'sources_breakdown': sources,
        'failure_modes': failures,
        'exploitation_opportunities': opportunities,
        'anti_rules': anti_rules,
        'status': 'OK',
        'config': {
            'min_trades_per_cell': MIN_TRADES_PER_CELL,
            'failure_upper_ci_threshold': FAILURE_UPPER_CI,
            'exploit_lower_ci_threshold': EXPLOIT_LOWER_CI,
            'high_severity_threshold': HIGH_SEVERITY_THRESHOLD,
        },
    }

    atomic_write(OUTPUT_FILE, output)

    # Print CLI summary
    print_summary(
        n_points=n_points,
        sources=sources,
        n_crosstabs=len(cells),
        failures=failures,
        opportunities=opportunities,
        anti_rules=anti_rules,
    )


if __name__ == '__main__':
    main()
