#!/usr/bin/env python3
"""
Apex Outcomes Cleanup — Phase 2 one-shot migration.

Removes phantom BREAKEVEN rows from apex-outcomes.json that were created
by ghost fills (positions auto-reconciled with entry==exit, pnl==0).

These rows corrupt:
  - Edge-proof win-rate calculations (treats ghosts as losses)
  - MAE/MFE calibration (skews stop/target optimisation)
  - Kelly sizing (negative expectancy from phantom losers)

Usage:
  python3 apex-outcomes-cleanup.py --dry-run   # preview, no changes
  python3 apex-outcomes-cleanup.py --commit     # apply + write backup

Phantom rows moved to apex-outcomes-phantoms.json for audit trail.
Backup written to apex-outcomes.json.bak-2026-04-16 before any change.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone

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

OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
PHANTOMS_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes-phantoms.json'
BACKUP_FILE    = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json.bak-2026-04-16'


def is_phantom(trade: dict) -> bool:
    """
    True when a trade row is a ghost fill — limit order recorded as EXECUTED
    but never actually filled in T212, creating an entry==exit BREAKEVEN.
    """
    return (
        trade.get('outcome_type') == 'auto_reconciled_not_in_t212'
        and float(trade.get('pnl', 0)) == 0
        and trade.get('result') == 'BREAKEVEN'
        and float(trade.get('entry', 0)) == float(trade.get('exit', 0))
    )


def rebuild_summary(trades: list) -> dict:
    all_pnl = [t.get('pnl', 0) for t in trades]
    winners = [t for t in trades if t.get('pnl', 0) > 0]
    return {
        'total_trades': len(trades),
        'winners':      len(winners),
        'losers':       len(trades) - len(winners),
        'win_rate':     round(len(winners) / len(trades) * 100, 1) if trades else 0,
        'total_pnl':    round(sum(all_pnl), 2),
        'avg_r':        round(sum(t.get('r_achieved', 0) for t in trades) / len(trades), 2)
                        if trades else 0,
    }


def main():
    parser = argparse.ArgumentParser(description='Remove phantom BREAKEVEN rows from outcomes')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dry-run', action='store_true', help='Preview only — no files changed')
    group.add_argument('--commit',  action='store_true', help='Apply changes (writes backup first)')
    args = parser.parse_args()

    data = safe_read(OUTCOMES_FILE, {'trades': []})
    if not isinstance(data, dict):
        data = {'trades': []}
    trades = data.get('trades', [])

    phantoms = [t for t in trades if is_phantom(t)]
    real     = [t for t in trades if not is_phantom(t)]

    print(f"Apex Outcomes Cleanup — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\nTotal rows:   {len(trades)}")
    print(f"Real trades:  {len(real)}")
    print(f"Phantoms:     {len(phantoms)}")

    if not phantoms:
        print("\nNo phantom rows found — nothing to clean up.")
        return

    print(f"\nPhantom rows to remove:")
    for t in phantoms:
        print(f"  id={t.get('id'):3d}  {t.get('opened','?'):10s}  "
              f"{t.get('ticker','?'):20s}  "
              f"signal={t.get('signal_type','?'):15s}  "
              f"entry=exit={t.get('entry','?')}")

    # Recompute summaries
    before = rebuild_summary(trades)
    after  = rebuild_summary(real)

    print(f"\nSummary before: {before}")
    print(f"Summary after:  {after}")

    if args.dry_run:
        print(f"\nDRY RUN — no files changed. Re-run with --commit to apply.")
        return

    # Write backup
    shutil.copy2(OUTCOMES_FILE, BACKUP_FILE)
    print(f"\nBackup written: {BACKUP_FILE}")

    # Renumber real rows sequentially to avoid ID gaps
    for i, t in enumerate(real, start=1):
        t['id'] = i

    # Rebuild outcomes
    data['trades']  = real
    data['summary'] = after
    atomic_write(OUTCOMES_FILE, data)
    print(f"Updated: {OUTCOMES_FILE}  ({len(real)} rows)")

    # Save phantoms to audit file
    phantom_doc = {
        'removed_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'reason':     'apex-outcomes-cleanup.py Phase 2 migration (ghost fills with entry==exit)',
        'count':      len(phantoms),
        'trades':     phantoms,
    }
    atomic_write(PHANTOMS_FILE, phantom_doc)
    print(f"Phantoms archived: {PHANTOMS_FILE}  ({len(phantoms)} rows)")

    print(f"\nDone. Run apex-edge-proof.py and apex-mae-mfe.py to regenerate stats.")


if __name__ == '__main__':
    main()
