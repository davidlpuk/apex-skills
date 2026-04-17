#!/usr/bin/env python3
"""apex-tool-runner.py — Agent-native tool executor for the APEX trading system.

Provides a structured, safety-gated interface so any agent (Claude, cron, future
agents) can invoke APEX capabilities by name and receive structured JSON back.

Usage:
    python3 apex-tool-runner.py --list
    python3 apex-tool-runner.py --list --tag risk
    python3 apex-tool-runner.py --describe <tool-name>
    python3 apex-tool-runner.py --run <tool-name>
    python3 apex-tool-runner.py --run <tool-name> --force   # execute-trade only
    python3 apex-tool-runner.py --chains                    # list all chains
    python3 apex-tool-runner.py --chain <chain-name>        # run a chain

All output is JSON to stdout. Logs go to stderr.

Safety gates:
    read / write-log / external-fetch  → run freely
    execute-signal                     → run freely (no orders placed)
    execute-trade                      → blocked unless --force is passed
"""

import json
import os
import subprocess
import sys
import time
import argparse
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR    = os.path.join(os.path.dirname(SCRIPTS_DIR), 'logs')
MANIFEST    = os.path.join(SCRIPTS_DIR, 'apex-tool-manifest.json')
CHAINS_FILE = os.path.join(SCRIPTS_DIR, 'apex-tool-chains.json')

SAFE_TO_RUN = {'read', 'write-log', 'external-fetch', 'execute-signal'}
GATED       = {'execute-trade'}


def load_manifest():
    with open(MANIFEST) as f:
        return json.load(f)


def load_chains():
    with open(CHAINS_FILE) as f:
        return json.load(f)


def find_tool(manifest, name):
    for tool in manifest['tools']:
        if tool['name'] == name:
            return tool
    return None


def result(status, **kwargs):
    out = {'status': status, 'timestamp': datetime.now(timezone.utc).isoformat(), **kwargs}
    print(json.dumps(out, indent=2))
    return 0 if status == 'ok' else 1


def cmd_list(manifest, tag_filter=None):
    tools = manifest['tools']
    if tag_filter:
        tools = [t for t in tools if tag_filter in t.get('tags', [])]
    rows = []
    for t in tools:
        rows.append({
            'name':   t['name'],
            'safety': t['safety'],
            'tags':   t['tags'],
            'description': t['description'][:80] + ('…' if len(t['description']) > 80 else '')
        })
    print(json.dumps({'tools': rows, 'count': len(rows)}, indent=2))
    return 0


def cmd_describe(manifest, name):
    tool = find_tool(manifest, name)
    if not tool:
        return result('error', error=f"Unknown tool: {name}",
                      available=[t['name'] for t in manifest['tools']])
    print(json.dumps(tool, indent=2))
    return 0


def _check_preconditions(tool):
    """Return a list of precondition warnings for input files.

    Checks that every declared input exists and (for JSON state files) is not
    alarmingly stale. Returns a list of dicts describing missing/stale inputs.
    """
    warnings = []
    for fname in tool.get('inputs', []):
        if not fname.endswith('.json'):
            continue  # only check state files, not Python modules
        fpath = os.path.join(LOGS_DIR, fname)
        if not os.path.exists(fpath):
            warnings.append({'input': fname, 'issue': 'missing'})
            continue
        age_s = time.time() - os.path.getmtime(fpath)
        # 6h default staleness threshold; manifest can override with max_age_s
        max_age = tool.get('max_input_age_s', 21600)
        if age_s > max_age:
            warnings.append({
                'input': fname,
                'issue': 'stale',
                'age_minutes': int(age_s / 60),
                'threshold_minutes': int(max_age / 60),
            })
    return warnings


def _next_steps(tool, outcome_status):
    """Suggest likely next tools based on this tool's tags and outcome.

    This is a lightweight heuristic — it reads the manifest-declared `next`
    hint if present, otherwise falls back to tag-based suggestions. Goal:
    every tool response tells the agent what's sensible to do next so the
    agent doesn't flounder after an unfamiliar result.
    """
    explicit = tool.get('next_on_ok') if outcome_status == 'ok' else tool.get('next_on_error')
    if explicit:
        return explicit

    tags = set(tool.get('tags', []))
    if outcome_status == 'error':
        return ['staleness-check', 'data-integrity']
    if 'regime' in tags:
        return ['query-regime', 'circuit-breaker']
    if 'signals' in tags or 'scan' in tags:
        return ['expected-value', 'vwap-gate', 'query-signals']
    if 'exits' in tags:
        return ['intraday-momentum', 'query-positions']
    if 'context' in tags:
        return ['build-context']
    return []


def _run_one(manifest, name, force=False, check_preconditions=True):
    """Run a single tool. Returns a result dict (not printed)."""
    tool = find_tool(manifest, name)
    if not tool:
        return {'status': 'error', 'tool': name, 'error': f'Unknown tool: {name}'}

    safety = tool['safety']
    if safety in GATED and not force:
        return {
            'status': 'blocked',
            'tool': name,
            'safety': safety,
            'reason': 'execute-trade tools require --force flag.',
            'next_steps': ['request_confirmation', 'send_telegram'],
        }

    # Precondition check — surfaces missing or stale inputs BEFORE running.
    precondition_warnings = _check_preconditions(tool) if check_preconditions else []

    # Script field may include args e.g. "apex-query.py positions"
    script_parts = tool['script'].split()
    script_path  = os.path.join(SCRIPTS_DIR, script_parts[0])
    script_args  = script_parts[1:]
    if not os.path.exists(script_path):
        return {'status': 'error', 'tool': name, 'error': f"Script not found: {script_parts[0]}"}

    timeout_s = tool.get('timeout_s', 300)
    t_start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, script_path] + script_args,
            capture_output=True,
            text=True,
            cwd=SCRIPTS_DIR,
            timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'tool': name, 'error': f'Script timed out after {timeout_s}s'}
    except Exception as e:
        return {'status': 'error', 'tool': name, 'error': str(e)}

    elapsed = round(time.monotonic() - t_start, 2)
    success = proc.returncode == 0

    output_data = {}
    for fname in tool.get('outputs', []):
        fpath = os.path.join(LOGS_DIR, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    output_data[fname] = json.load(f)
            except Exception:
                output_data[fname] = None

    outcome = 'ok' if success else 'error'
    result = {
        'status': outcome,
        'tool': name,
        'safety': safety,
        'elapsed_s': elapsed,
        'exit_code': proc.returncode,
        'stdout': proc.stdout[-4000:] if proc.stdout else '',
        'stderr': proc.stderr[-2000:] if proc.stderr else '',
        'outputs': output_data,
        'next_steps': _next_steps(tool, outcome),
    }
    if precondition_warnings:
        result['precondition_warnings'] = precondition_warnings
    return result


def cmd_run(manifest, name, force=False):
    r = _run_one(manifest, name, force=force)
    r['timestamp'] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(r, indent=2))
    return 0 if r['status'] == 'ok' else 1


def cmd_chains():
    chains = load_chains()
    rows = []
    for name, chain in chains.get('chains', {}).items():
        rows.append({
            'name':        name,
            'description': chain['description'],
            'steps':       chain['steps'],
            'tags':        chain.get('tags', []),
            'stop_on_error': chain.get('stop_on_error', False),
        })
    print(json.dumps({'chains': rows, 'count': len(rows)}, indent=2))
    return 0


def cmd_chain(manifest, chain_name, force=False):
    chains = load_chains()
    chain = chains.get('chains', {}).get(chain_name)
    if not chain:
        return result('error', chain=chain_name,
                      error=f"Unknown chain: {chain_name}",
                      available=list(chains.get('chains', {}).keys()))

    steps = chain['steps']
    stop_on_error = chain.get('stop_on_error', False)
    t_chain_start = time.monotonic()

    step_results = []
    aborted_at = None

    for step in steps:
        print(f'  → running {step}…', file=sys.stderr)
        r = _run_one(manifest, step, force=force)
        r['timestamp'] = datetime.now(timezone.utc).isoformat()
        step_results.append(r)

        if r['status'] in ('error', 'blocked') and stop_on_error:
            aborted_at = step
            break

    total_elapsed = round(time.monotonic() - t_chain_start, 2)
    ok_count  = sum(1 for r in step_results if r['status'] == 'ok')
    err_count = len(step_results) - ok_count
    overall   = 'ok' if err_count == 0 and aborted_at is None else 'partial' if ok_count > 0 else 'error'

    out = {
        'status':        overall,
        'timestamp':     datetime.now(timezone.utc).isoformat(),
        'chain':         chain_name,
        'description':   chain['description'],
        'total_elapsed_s': total_elapsed,
        'steps_run':     len(step_results),
        'steps_ok':      ok_count,
        'steps_failed':  err_count,
        'aborted_at':    aborted_at,
        'results':       step_results,
    }
    print(json.dumps(out, indent=2))
    return 0 if overall == 'ok' else 1


def main():
    parser = argparse.ArgumentParser(
        description='APEX agent-native tool runner',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--list',     action='store_true', help='List all available tools')
    group.add_argument('--describe', metavar='TOOL',      help='Describe a specific tool')
    group.add_argument('--run',      metavar='TOOL',      help='Run a tool by name')
    group.add_argument('--chains',   action='store_true', help='List all chains')
    group.add_argument('--chain',    metavar='CHAIN',     help='Run a named chain of tools')

    parser.add_argument('--tag',   metavar='TAG',  help='Filter --list by tag')
    parser.add_argument('--force', action='store_true',
                        help='Allow execute-trade tools (places real orders)')

    args = parser.parse_args()
    manifest = load_manifest()

    if args.list:
        return cmd_list(manifest, tag_filter=args.tag)
    elif args.describe:
        return cmd_describe(manifest, args.describe)
    elif args.run:
        return cmd_run(manifest, args.run, force=args.force)
    elif args.chains:
        return cmd_chains()
    elif args.chain:
        return cmd_chain(manifest, args.chain, force=args.force)


if __name__ == '__main__':
    sys.exit(main())
