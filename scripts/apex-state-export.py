#!/usr/bin/env python3
"""
Apex State Export — Primary VM side
Exports apex-positions.json to a secret GitHub Gist so the secondary
watchdog can access last-known position state without querying this VM.

Required env vars (add to .env.trading212):
  GIST_TOKEN  — GitHub Personal Access Token with 'gist' scope only
  GIST_ID     — ID of a pre-created secret Gist (create once manually)

Setup (one-time):
  1. Create a secret Gist at github.com with a file named apex-state.json
  2. Copy the Gist ID from the URL (the hex string after gist.github.com/)
  3. Create a GitHub PAT with only 'gist' scope
  4. Add GIST_TOKEN=<token> and GIST_ID=<id> to .env.trading212

Called by:
  - apex-eod-review.sh  (after market close, daily)
  - apex_order_executor.py  (after each trade placement)
  - apex-broker-watchdog.py (after each watchdog run)
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

ENV_FILE       = '/home/ubuntu/.picoclaw/.env.trading212'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
CB_FILE        = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'
REGIME_FILE    = '/home/ubuntu/.picoclaw/logs/apex-regime.json'
GIST_API       = 'https://api.github.com/gists'


def _load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, _, v = line.partition('=')
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    env.update(os.environ)
    return env


def _safe_read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def export_state():
    env = _load_env()
    gist_token = env.get('GIST_TOKEN', '')
    gist_id    = env.get('GIST_ID', '')

    if not gist_token or not gist_id:
        print("STATE EXPORT: GIST_TOKEN or GIST_ID not set — skipping Gist export")
        print("  Add GIST_TOKEN and GIST_ID to .env.trading212 to enable secondary watchdog")
        return False

    positions    = _safe_read(POSITIONS_FILE)
    cb_state     = _safe_read(CB_FILE)
    regime_state = _safe_read(REGIME_FILE)

    export_payload = {
        'exported':      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'positions':     positions if isinstance(positions, list) else [],
        'circuit_breaker': {
            'status':       cb_state.get('status', 'UNKNOWN'),
            'session_pnl_pct': cb_state.get('session_pnl_pct', 0),
        },
        'regime': {
            'label':   regime_state.get('label', 'UNKNOWN'),
            'vix':     regime_state.get('vix', 0),
            'breadth': regime_state.get('breadth_pct', 0),
        },
    }

    content = json.dumps(export_payload, indent=2)

    payload = json.dumps({
        'files': {
            'apex-state.json': {'content': content}
        }
    }).encode('utf-8')

    try:
        req = urllib.request.Request(
            f'{GIST_API}/{gist_id}',
            data=payload,
            method='PATCH',
            headers={
                'Authorization': f'token {gist_token}',
                'Accept':        'application/vnd.github.v3+json',
                'Content-Type':  'application/json',
                'User-Agent':    'apex-state-exporter/1.0',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                n = len(export_payload['positions'])
                print(f"STATE EXPORT: Gist updated — {n} positions exported at "
                      f"{export_payload['exported']}")
                return True
            else:
                print(f"STATE EXPORT: Unexpected HTTP {resp.status}")
                return False
    except urllib.error.HTTPError as e:
        print(f"STATE EXPORT: GitHub API error {e.code}: {e.reason}")
        return False
    except Exception as e:
        print(f"STATE EXPORT: Failed — {e}")
        return False


if __name__ == '__main__':
    success = export_state()
    sys.exit(0 if success else 1)
