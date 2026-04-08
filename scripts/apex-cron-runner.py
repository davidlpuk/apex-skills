#!/usr/bin/env python3
"""apex-cron-runner.py — Chain-driven cron entry point.

Any cron job can call this instead of a hardcoded script path. It:
  1. Runs the named chain via apex-tool-runner.py
  2. Appends a structured entry to apex-tool-run-log.jsonl
  3. Returns non-zero on failure (cron sees the error)

Usage (in crontab):
    30 8 * * 1-5  python3 /home/ubuntu/.picoclaw/scripts/apex-cron-runner.py full-morning
    35 16 * * 1-5 python3 /home/ubuntu/.picoclaw/scripts/apex-cron-runner.py learning-cycle

Or to run a single tool:
    20 7 * * 1-5  python3 /home/ubuntu/.picoclaw/scripts/apex-cron-runner.py --tool regime-check

The run log lets the dashboard show recent activity without reading every cron log.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR    = os.path.join(os.path.dirname(SCRIPTS_DIR), 'logs')
RUN_LOG     = os.path.join(LOGS_DIR, 'apex-tool-run-log.jsonl')
RUNNER      = os.path.join(SCRIPTS_DIR, 'apex-tool-runner.py')
PYTHON      = sys.executable


def append_run_log(entry: dict):
    """Append one JSON line to the run log (JSONL format, one record per line)."""
    try:
        with open(RUN_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        print(f'WARNING: could not write run log: {e}', file=sys.stderr)


def run_chain(chain_name: str, force: bool = False) -> int:
    cmd = [PYTHON, RUNNER, '--chain', chain_name]
    if force:
        cmd.append('--force')

    t_start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=SCRIPTS_DIR)
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - t_start, 2)
        append_run_log({
            'ts': datetime.now(timezone.utc).isoformat(),
            'type': 'chain',
            'name': chain_name,
            'status': 'error',
            'error': 'timed out after 600s',
            'elapsed_s': elapsed,
            'triggered_by': 'cron',
        })
        print(f'ERROR: chain {chain_name} timed out', file=sys.stderr)
        return 1

    elapsed = round(time.monotonic() - t_start, 2)
    try:
        result = json.loads(proc.stdout)
    except Exception:
        result = {'status': 'error', 'error': 'invalid JSON output'}

    log_entry = {
        'ts':           datetime.now(timezone.utc).isoformat(),
        'type':         'chain',
        'name':         chain_name,
        'status':       result.get('status', 'error'),
        'steps_ok':     result.get('steps_ok'),
        'steps_run':    result.get('steps_run'),
        'elapsed_s':    elapsed,
        'triggered_by': 'cron',
    }
    if result.get('aborted_at'):
        log_entry['aborted_at'] = result['aborted_at']

    append_run_log(log_entry)

    # Echo step summary to stdout (captured by cron → apex-cron.log)
    print(f'[{chain_name}] {log_entry["status"]} — {log_entry.get("steps_ok","?")}/'
          f'{log_entry.get("steps_run","?")} steps in {elapsed}s')

    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    return 0 if result.get('status') in ('ok', 'partial') else 1


def run_tool(tool_name: str, force: bool = False) -> int:
    cmd = [PYTHON, RUNNER, '--run', tool_name]
    if force:
        cmd.append('--force')

    t_start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360, cwd=SCRIPTS_DIR)
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - t_start, 2)
        append_run_log({
            'ts': datetime.now(timezone.utc).isoformat(),
            'type': 'tool',
            'name': tool_name,
            'status': 'error',
            'error': 'timed out',
            'elapsed_s': elapsed,
            'triggered_by': 'cron',
        })
        return 1

    elapsed = round(time.monotonic() - t_start, 2)
    try:
        result = json.loads(proc.stdout)
    except Exception:
        result = {'status': 'error', 'error': 'invalid JSON output'}

    append_run_log({
        'ts':           datetime.now(timezone.utc).isoformat(),
        'type':         'tool',
        'name':         tool_name,
        'status':       result.get('status', 'error'),
        'elapsed_s':    elapsed,
        'triggered_by': 'cron',
    })

    print(f'[{tool_name}] {result.get("status","error")} in {elapsed}s')
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    return 0 if result.get('status') == 'ok' else 1


def log_only(name: str, exit_code: int, elapsed_s: float) -> int:
    """Record a shell script's completion in the run log without running anything."""
    append_run_log({
        'ts':           datetime.now(timezone.utc).isoformat(),
        'type':         'shell',
        'name':         name,
        'status':       'ok' if exit_code == 0 else 'error',
        'exit_code':    exit_code,
        'elapsed_s':    elapsed_s,
        'triggered_by': 'cron',
    })
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Chain-driven cron runner with structured logging')
    parser.add_argument('chain', nargs='?', help='Chain name to run')
    parser.add_argument('--tool',     metavar='TOOL', help='Run a single tool instead of a chain')
    parser.add_argument('--log-only', metavar='NAME', dest='log_only', help='Log a shell script completion without running (NAME EXIT_CODE ELAPSED_S)')
    parser.add_argument('--force',    action='store_true', help='Pass --force to tool-runner (execute-trade)')
    args, extra = parser.parse_known_args()

    if args.log_only:
        try:
            exit_code = int(extra[0]) if extra else 0
            elapsed   = float(extra[1]) if len(extra) > 1 else 0.0
        except (ValueError, IndexError):
            exit_code, elapsed = 0, 0.0
        return log_only(args.log_only, exit_code, elapsed)
    elif args.tool:
        return run_tool(args.tool, force=args.force)
    elif args.chain:
        return run_chain(args.chain, force=args.force)
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())
