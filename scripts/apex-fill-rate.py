#!/usr/bin/env python3
"""
Apex Fill Rate Metric — Phase 1 observability script.
Reads apex-queue-audit.jsonl and computes fill / ghost / failure rates.
Writes apex-fill-rate.json for dashboard and morning digest.

Run standalone or import compute_fill_rate() for API use.
"""
import json
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)

AUDIT_FILE   = '/home/ubuntu/.picoclaw/logs/apex-queue-audit.jsonl'
OUTPUT_FILE  = '/home/ubuntu/.picoclaw/logs/apex-fill-rate.json'


def _load_recent_transitions(hours: int = 168) -> list:
    """Load audit lines from the last `hours` hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')
    lines = []
    try:
        with open(AUDIT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get('ts', '') >= cutoff_str:
                        lines.append(rec)
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return lines


def _compute_stats(transitions: list, window_label: str) -> dict:
    """
    From a list of transitions, compute fill/ghost/fail rates.

    A signal lifecycle from QUEUED:
      - 'filled':  reached 'protected' or 'unprotected' with filled_qty > 0
      - 'ghost':   reached REMOVED/CANCELLED with filled_qty == 0 after EXECUTED
      - 'failed':  to-state is 'FAILED' in queue
      - 'queued':  from-state is None and to-state is 'QUEUED'
    """
    # Group by signal_id to trace full lifecycles
    by_id: dict = {}
    for t in transitions:
        sid = t.get('signal_id')
        if sid is None:
            continue
        if sid not in by_id:
            by_id[sid] = {
                'ticker':       t.get('ticker'),
                'signal_type':  t.get('signal_type'),
                'states':       [],
                'filled_qty':   0.0,
                't212_order_id': None,
            }
        by_id[sid]['states'].append((t.get('from'), t.get('to'), t.get('detail', '')))
        if t.get('filled_qty', 0) > 0:
            by_id[sid]['filled_qty'] = t['filled_qty']
        if t.get('t212_order_id'):
            by_id[sid]['t212_order_id'] = t['t212_order_id']

    queued_count = 0
    filled_count = 0
    ghost_count  = 0
    failed_count = 0
    by_type: dict = {}

    for sid, info in by_id.items():
        state_froms = {s[0] for s in info['states']}
        state_tos   = {s[1] for s in info['states']}
        st = info.get('signal_type', 'UNKNOWN')

        if None in state_froms:
            queued_count += 1

        is_filled = 'protected' in state_tos or (
            'unprotected' in state_tos and info['filled_qty'] > 0
        )
        is_failed  = 'FAILED' in state_tos
        is_removed = 'REMOVED' in state_tos or 'CANCELLED' in state_tos

        if is_filled:
            filled_count += 1
        elif is_failed:
            failed_count += 1
        elif is_removed and 'EXECUTED' in state_tos:
            # Was marked EXECUTED (limit placed) but position removed = ghost
            ghost_count += 1

        if st not in by_type:
            by_type[st] = {'queued': 0, 'filled': 0, 'ghosts': 0, 'failed': 0}
        if None in state_froms:
            by_type[st]['queued'] += 1
        if is_filled:
            by_type[st]['filled'] += 1
        elif is_failed:
            by_type[st]['failed'] += 1
        elif is_removed and 'EXECUTED' in state_tos:
            by_type[st]['ghosts'] += 1

    fill_rate  = round(filled_count / queued_count * 100, 1) if queued_count else None
    ghost_rate = round(ghost_count  / queued_count * 100, 1) if queued_count else None

    return {
        'window':        window_label,
        'queued':        queued_count,
        'filled':        filled_count,
        'ghosts':        ghost_count,
        'failed':        failed_count,
        'fill_rate_pct': fill_rate,
        'ghost_rate_pct':ghost_rate,
        'by_signal_type':by_type,
        'status':        (
            'NO_DATA'  if queued_count == 0 else
            'HEALTHY'  if (fill_rate or 0) >= 80 else
            'DEGRADED' if (fill_rate or 0) >= 50 else
            'CRITICAL'
        ),
    }


def compute_fill_rate() -> dict:
    """Compute fill rates for 24h and 7d windows. Returns full result dict."""
    lines_7d  = _load_recent_transitions(hours=168)
    lines_24h = [l for l in lines_7d
                 if l.get('ts', '') >= (
                     datetime.now(timezone.utc) - timedelta(hours=24)
                 ).strftime('%Y-%m-%dT%H:%M:%SZ')]

    result = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        '24h':       _compute_stats(lines_24h, '24h'),
        '7d':        _compute_stats(lines_7d,  '7d'),
    }
    return result


def main():
    result = compute_fill_rate()
    atomic_write(OUTPUT_FILE, result)

    h = result['24h']
    d = result['7d']
    print(f"Fill rate (24h): {h['fill_rate_pct']}%  "
          f"[queued={h['queued']} filled={h['filled']} "
          f"ghosts={h['ghosts']} failed={h['failed']}]  status={h['status']}")
    print(f"Fill rate (7d):  {d['fill_rate_pct']}%  "
          f"[queued={d['queued']} filled={d['filled']} "
          f"ghosts={d['ghosts']} failed={d['failed']}]  status={d['status']}")
    print(f"Written: {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
