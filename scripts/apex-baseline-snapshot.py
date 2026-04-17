#!/usr/bin/env python3
"""
Apex Baseline Snapshot — Phase 0 of performance improvement plan.
Captures pre-improvement metrics for before/after comparison.
Run once manually: python3 apex-baseline-snapshot.py
"""
import json
import sys
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
    def safe_read(p, default=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return default if default is not None else {}

LOGS = '/home/ubuntu/.picoclaw/logs'
OUTPUT = f'{LOGS}/apex-baseline-2026-04-16.json'


def snapshot_outcomes():
    data = safe_read(f'{LOGS}/apex-outcomes.json', {'trades': []})
    if not isinstance(data, dict):
        data = {'trades': []}
    trades = data.get('trades', [])

    ghost_count = sum(
        1 for t in trades
        if t.get('outcome_type') == 'auto_reconciled_not_in_t212'
        and t.get('pnl', 0) == 0
        and t.get('result') == 'BREAKEVEN'
    )

    by_type = {}
    for t in trades:
        st = t.get('signal_type', 'UNKNOWN')
        if st not in by_type:
            by_type[st] = {'n': 0, 'wins': 0, 'pnl': 0.0, 'ghosts': 0}
        by_type[st]['n'] += 1
        if t.get('pnl', 0) > 0:
            by_type[st]['wins'] += 1
        by_type[st]['pnl'] = round(by_type[st]['pnl'] + t.get('pnl', 0), 2)
        if (t.get('outcome_type') == 'auto_reconciled_not_in_t212'
                and t.get('pnl', 0) == 0):
            by_type[st]['ghosts'] += 1

    return {
        'total_trades':   len(trades),
        'total_pnl':      round(sum(t.get('pnl', 0) for t in trades), 2),
        'ghost_count':    ghost_count,
        'ghost_rate_pct': round(ghost_count / len(trades) * 100, 1) if trades else 0,
        'by_signal_type': by_type,
        'result_counts':  dict(Counter(t.get('result', '?') for t in trades)),
    }


def snapshot_edge_proof():
    data = safe_read(f'{LOGS}/apex-edge-proof.json', {})
    return {
        'n_real_trades':    data.get('n_real_trades', 0),
        'by_signal_type':   {
            k: {
                'n_real':       v.get('n_real', 0),
                'win_rate_pct': v.get('win_rate_pct'),
                'expectancy_r': v.get('expectancy_r'),
                'verdict':      v.get('verdict'),
            }
            for k, v in data.get('by_signal_type', {}).items()
        },
        'confirmed_types':    data.get('summary', {}).get('confirmed', []),
        'not_proven_types':   data.get('summary', {}).get('not_proven', []),
    }


def snapshot_mae_mfe():
    data = safe_read(f'{LOGS}/apex-mae-mfe-calibration.json', {})
    agg = data.get('aggregate', {})
    return {
        'n_wins':          data.get('n_wins_total', 0),
        'n_losses':        data.get('n_losses_total', 0),
        'stop_efficiency': agg.get('mae', {}).get('stop_efficiency'),
        'early_exit_pct':  agg.get('mae', {}).get('early_exit_pct'),
        'optimal_exit_r':  agg.get('mfe', {}).get('optimal_exit_r'),
        'optimal_t1_r':    agg.get('mfe', {}).get('optimal_t1_r'),
        't1_r_used':       agg.get('mfe', {}).get('t1_r_used'),
        'reached_t1_pct':  agg.get('mfe', {}).get('reached_t1_pct'),
    }


def snapshot_rolling_pnl():
    data = safe_read(f'{LOGS}/apex-rolling-pnl.json', {})
    sessions = data.get('sessions', [])
    return {
        'n_sessions':    len(sessions),
        'total_return':  data.get('total_return_pct'),
        'last_session':  sessions[-1] if sessions else None,
        'negative_days': sum(1 for s in sessions if s.get('pnl', 0) < 0),
    }


def main():
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"Apex baseline snapshot — {ts}")

    snapshot = {
        'timestamp':   ts,
        'description': 'Pre-improvement-plan baseline (2026-04-16)',
        'outcomes':    snapshot_outcomes(),
        'edge_proof':  snapshot_edge_proof(),
        'mae_mfe':     snapshot_mae_mfe(),
        'rolling_pnl': snapshot_rolling_pnl(),
    }

    atomic_write(OUTPUT, snapshot)
    print(f"\nSnapshot written to: {OUTPUT}")

    # Summary to stdout
    o = snapshot['outcomes']
    print(f"\n--- OUTCOMES ---")
    print(f"  Total trades:  {o['total_trades']}")
    print(f"  Total P&L:     £{o['total_pnl']}")
    print(f"  Ghost fills:   {o['ghost_count']} ({o['ghost_rate_pct']}%)")
    print(f"  By type:")
    for st, v in sorted(o['by_signal_type'].items()):
        wr = round(v['wins'] / v['n'] * 100, 1) if v['n'] else 0
        print(f"    {st:20s} n={v['n']:2d}  WR={wr:5.1f}%  "
              f"pnl=£{v['pnl']:7.2f}  ghosts={v['ghosts']}")

    m = snapshot['mae_mfe']
    print(f"\n--- MAE/MFE ---")
    print(f"  Stop efficiency: {m['stop_efficiency']}")
    print(f"  Early exit %:    {m['early_exit_pct']}")
    print(f"  Optimal exit:    {m['optimal_exit_r']}R  (current T1: {m['t1_r_used']}R)")
    print(f"  T1 reached:      {m['reached_t1_pct']}%")


if __name__ == '__main__':
    main()
