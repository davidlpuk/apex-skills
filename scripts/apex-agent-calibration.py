#!/usr/bin/env python3
"""apex-agent-calibration.py — Brier score + calibration curve.

When the agent says confidence=0.9, is it right 9 times out of 10? Or is
it systematically overconfident? We need the number, not the vibe.

Method:
  - For each attributable action in the ledger, assign correctness ∈ {0, 1}
    from the sign of attributed pnl_gbp (pnl > 0 → correct).
  - Pull the agent's self-reported `confidence` from apex-agent-actions.json
    (joined by timestamp + ticker).
  - Brier = mean((confidence − correctness)²) over paired observations.
    Lower is better. 0.25 = always-50/50 guess. <0.1 is genuinely well-calibrated.
  - Bucket by confidence decile and report hit-rate per bucket →
    calibration curve. Overconfidence = mean confidence >> hit rate.

Skips actions with zero/null attributed pnl (inert tightens; unattributable
vetoes) — they carry no signal for calibration.

Writes apex-agent-calibration.json.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
LOGS_DIR    = SCRIPTS_DIR.parent / 'logs'

LEDGER_FILE  = LOGS_DIR / 'apex-agent-ledger.json'
ACTIONS_FILE = LOGS_DIR / 'apex-agent-actions.json'
OUT_FILE     = LOGS_DIR / 'apex-agent-calibration.json'


def _safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _join_confidence(attributed, actions):
    """For each ledger row, pull confidence from the matching action record.

    Join key: (ticker, timestamp prefix to the second). Actions may carry
    confidence=null; those rows are dropped from the calibration set.
    """
    index = {}
    for a in actions:
        ts = (a.get('timestamp') or '')[:19]
        index[(a.get('ticker'), ts)] = a

    paired = []
    for row in attributed:
        ts = (row.get('timestamp') or '')[:19]
        a = index.get((row.get('ticker'), ts))
        if not a:
            continue
        conf = a.get('confidence')
        if conf is None:
            continue
        try:
            conf = float(conf)
        except (ValueError, TypeError):
            continue
        if not (0.0 <= conf <= 1.0):
            continue
        paired.append({
            'ticker':     row.get('ticker'),
            'timestamp':  row.get('timestamp'),
            'action_type': row.get('action_type'),
            'confidence': conf,
            'pnl_gbp':    row.get('pnl_gbp', 0),
            'correct':    1 if (row.get('pnl_gbp') or 0) > 0 else 0,
        })
    return paired


def _brier(paired):
    if not paired:
        return None
    return round(sum((p['confidence'] - p['correct']) ** 2 for p in paired)
                 / len(paired), 4)


def _calibration_curve(paired, bucket_edges=(0.0, 0.3, 0.5, 0.7, 0.9, 1.01)):
    """Per-bucket: mean confidence, hit rate, count. Perfect calibration
    puts mean_confidence ≈ hit_rate on every bucket."""
    buckets = defaultdict(list)
    for p in paired:
        for i in range(len(bucket_edges) - 1):
            if bucket_edges[i] <= p['confidence'] < bucket_edges[i + 1]:
                buckets[i].append(p)
                break

    curve = []
    for i in range(len(bucket_edges) - 1):
        rows = buckets.get(i, [])
        if not rows:
            continue
        mean_conf = sum(r['confidence'] for r in rows) / len(rows)
        hit_rate = sum(r['correct'] for r in rows) / len(rows)
        curve.append({
            'bucket':         f'[{bucket_edges[i]:.2f}, {bucket_edges[i+1]:.2f})',
            'n':              len(rows),
            'mean_confidence': round(mean_conf, 3),
            'hit_rate':       round(hit_rate, 3),
            'over_confidence': round(mean_conf - hit_rate, 3),
        })
    return curve


def _overall_diagnosis(paired, brier, curve):
    if not paired:
        return {
            'diagnosis': 'insufficient_data',
            'recommendation': 'Keep acting and logging. Need ≥10 attributable '
                              'actions before calibration has signal.',
        }
    avg_conf = sum(p['confidence'] for p in paired) / len(paired)
    avg_hit  = sum(p['correct']    for p in paired) / len(paired)
    drift = avg_conf - avg_hit

    if brier is None:
        status = 'insufficient_data'
    elif brier < 0.15:
        status = 'well_calibrated'
    elif brier < 0.25:
        status = 'acceptable'
    else:
        status = 'poorly_calibrated'

    rec = []
    if drift > 0.15:
        rec.append('systematically overconfident — haircut stated confidence '
                   'by ~{:.0f}% before using it in sizing'.format(drift * 100))
    elif drift < -0.15:
        rec.append('systematically underconfident — agent is better than it '
                   'thinks; consider trusting higher-confidence calls more')
    else:
        rec.append('confidence drift within tolerance')

    return {
        'n_paired':              len(paired),
        'mean_confidence':       round(avg_conf, 3),
        'mean_hit_rate':         round(avg_hit, 3),
        'confidence_drift':      round(drift, 3),
        'diagnosis':             status,
        'recommendation':        '; '.join(rec),
    }


def build(lookback_days=90):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)

    ledger  = _safe_read(LEDGER_FILE, {}) or {}
    actions = _safe_read(ACTIONS_FILE, []) or []

    attributed = [
        d for d in (ledger.get('actions_detail') or [])
        if d.get('pnl_gbp')  # only rows with a signed £ impact
    ]

    paired = _join_confidence(attributed, actions)
    brier = _brier(paired)
    curve = _calibration_curve(paired)
    diag  = _overall_diagnosis(paired, brier, curve)

    out = {
        'generated_at':  now.isoformat(),
        'period_days':   lookback_days,
        'period_start':  since.isoformat(),
        'brier_score':   brier,
        'diagnosis':     diag,
        'calibration_curve': curve,
        'n_attributable_actions': len(attributed),
        'n_paired_with_confidence': len(paired),
        'note': ('Correctness = sign of attributed pnl_gbp. Ties and '
                 'inert actions (pnl=0) are excluded. Confidence missing '
                 'from older log entries is also excluded.'),
    }

    tmp = OUT_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, OUT_FILE)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--lookback-days', type=int, default=90)
    args = p.parse_args()
    out = build(lookback_days=args.lookback_days)
    print(json.dumps({'status': 'ok', **out}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
