#!/usr/bin/env python3
"""
Position Reconciliation
Syncs apex-positions.json with T212 live portfolio.
Runs at start of morning scan and every stop monitor cycle.

Detects and resolves:
1. Positions closed in T212 but still tracked by Apex
2. Positions open in T212 but not tracked by Apex
3. Quantity mismatches between Apex and T212
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, log_info
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m): print(f'INFO: {m}')

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
RECON_FILE     = '/home/ubuntu/.picoclaw/logs/apex-reconciliation.json'

def load_env():
    env = {}
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except Exception as e:
        log_error(f"load_env failed: {e}")
    return env

def send_telegram(msg):
    try:
        subprocess.run(['bash', '-c',
            f'''BOT=$(cat ~/.picoclaw/config.json | grep -A2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot$BOT/sendMessage" \
  -d chat_id=6808823889 --data-urlencode "text={msg}"'''
        ], capture_output=True)
    except Exception as e:
        log_error(f"send_telegram failed: {e}")

def get_t212_portfolio():
    """Fetch live portfolio from T212."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH', '')
        endpoint = env.get('T212_ENDPOINT', 'https://demo.trading212.com/api/v0')
        result   = subprocess.run([
            'curl', '-s', '--max-time', '15',
            '-H', f'Authorization: Basic {auth}',
            f'{endpoint}/equity/portfolio'
        ], capture_output=True, text=True)
        data = json.loads(result.stdout)
        if isinstance(data, list):
            return {p['ticker']: p for p in data}
        return {}
    except Exception as e:
        log_error(f"get_t212_portfolio failed: {e}")
        return {}

def load_apex_positions():
    """Load Apex position tracking."""
    try:
        return safe_read(POSITIONS_FILE, [])
    except Exception as e:
        log_error(f"load_apex_positions failed: {e}")
        return []

def log_closed_position(pos, reason):
    """Log a position closure to outcomes."""
    try:
        outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
        trades   = outcomes.get('trades', [])
        trades.append({
            'name':       pos.get('name', '?'),
            'ticker':     pos.get('t212_ticker', '?'),
            'entry':      pos.get('entry', 0),
            'exit':       0,
            'pnl':        0,
            'opened':     pos.get('opened', ''),
            'closed':     datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'signal_type':pos.get('signal_type', 'UNKNOWN'),
            'close_reason':reason,
            'auto_reconciled': True,
        })
        outcomes['trades'] = trades
        atomic_write(OUTCOMES_FILE, outcomes)
    except Exception as e:
        log_error(f"log_closed_position failed: {e}")

def reconcile(silent=False):
    """
    Main reconciliation function.
    Returns: (changes_made, summary)
    """
    now = datetime.now(timezone.utc)

    if not silent:
        print(f"\n=== POSITION RECONCILIATION ===")
        print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    # Fetch both sources
    t212_positions  = get_t212_portfolio()
    apex_positions  = load_apex_positions()

    if not isinstance(apex_positions, list):
        apex_positions = []

    t212_tickers = set(t212_positions.keys())
    apex_tickers = set(p.get('t212_ticker', '') for p in apex_positions)

    changes      = []
    alerts       = []

    # ── 1. Positions in Apex but NOT in T212 ──────────────────────
    # These were closed externally — stop fired, manual close, expiry
    ghost_tickers = apex_tickers - t212_tickers - {''}
    for ticker in ghost_tickers:
        pos = next((p for p in apex_positions if p.get('t212_ticker') == ticker), {})
        name = pos.get('name', ticker)

        if not silent:
            print(f"  ⚠️  Ghost position: {name} ({ticker}) — in Apex but not T212")

        log_warning(f"Ghost position removed: {name} ({ticker})")
        log_closed_position(pos, 'auto_reconciled_not_in_t212')
        changes.append(f"REMOVED: {name} — closed in T212")
        alerts.append(f"⚠️ {name} closed externally (stop loss or manual)")

    # Remove ghost positions from Apex tracking
    apex_positions = [p for p in apex_positions
                     if p.get('t212_ticker', '') not in ghost_tickers]

    # ── 2. Positions in T212 but NOT in Apex ──────────────────────
    # These were opened manually via T212 app or another system
    orphan_tickers = t212_tickers - apex_tickers
    for ticker in orphan_tickers:
        t212_pos = t212_positions[ticker]
        name     = t212_pos.get('ticker', ticker)
        qty      = float(t212_pos.get('quantity', 0))
        price    = float(t212_pos.get('currentPrice', 0))
        avg      = float(t212_pos.get('averagePrice', 0))

        if not silent:
            print(f"  ⚠️  Orphan position: {name} ({ticker}) — in T212 but not Apex")

        # Add to Apex tracking with minimal data
        new_pos = {
            't212_ticker':  ticker,
            'name':         name,
            'quantity':     qty,
            'entry':        round(avg, 2),
            'current':      round(price, 2),
            'stop':         round(avg * 0.94, 2),  # Default 6% stop
            'target1':      round(avg * 1.08, 2),
            'target2':      round(avg * 1.15, 2),
            'signal_type':  'MANUAL',
            'opened':       now.strftime('%Y-%m-%d'),
            'reconciled':   True,
            'reconcile_note': 'Added by reconciliation — opened outside Apex'
        }
        apex_positions.append(new_pos)
        log_warning(f"Orphan position added to tracking: {name} ({ticker})")
        changes.append(f"ADDED: {name} — found in T212 not tracked by Apex")
        alerts.append(f"⚠️ {name} found in T212 but not tracked — added with default stop")

    # ── 3. Quantity mismatches ──────────────────────────────────────
    for pos in apex_positions:
        ticker   = pos.get('t212_ticker', '')
        if ticker not in t212_positions:
            continue
        t212_qty = float(t212_positions[ticker].get('quantity', 0))
        apex_qty = float(pos.get('quantity', 0))

        if abs(t212_qty - apex_qty) > 0.01:
            name = pos.get('name', ticker)
            if not silent:
                print(f"  ⚠️  Qty mismatch: {name} — Apex:{apex_qty} T212:{t212_qty}")
            pos['quantity'] = t212_qty
            log_warning(f"Qty mismatch fixed: {name} Apex:{apex_qty} → T212:{t212_qty}")
            changes.append(f"QTY FIX: {name} {apex_qty} → {t212_qty}")

    # ── 4. Deduplicate — keep only one entry per ticker ───────────
    seen_tickers = {}
    deduped = []
    for pos in apex_positions:
        ticker = pos.get('t212_ticker', '')
        if ticker not in seen_tickers:
            seen_tickers[ticker] = True
            deduped.append(pos)
        else:
            name = pos.get('name', ticker)
            log_warning(f"Duplicate position removed: {name} ({ticker})")
            changes.append(f"DEDUP: {name} — duplicate entry removed")
    apex_positions = deduped

    # ── 5. Update current prices from T212 ─────────────────────────
    for pos in apex_positions:
        ticker = pos.get('t212_ticker', '')
        if ticker in t212_positions:
            pos['current'] = float(t212_positions[ticker].get('currentPrice', 0))
            pos['ppl']     = float(t212_positions[ticker].get('ppl', 0))

    # Save reconciled positions
    if changes or True:  # Always save to update current prices
        atomic_write(POSITIONS_FILE, apex_positions)

    # Save reconciliation report
    report = {
        'timestamp':       now.strftime('%Y-%m-%d %H:%M UTC'),
        'apex_count':      len(apex_positions),
        't212_count':      len(t212_positions),
        'changes':         changes,
        'ghost_removed':   list(ghost_tickers),
        'orphans_added':   list(orphan_tickers),
        'status':          'CLEAN' if not changes else 'RECONCILED'
    }
    atomic_write(RECON_FILE, report)

    if not silent:
        if changes:
            print(f"\n  Changes made ({len(changes)}):")
            for c in changes:
                print(f"    → {c}")
        else:
            print(f"\n  ✅ Positions in sync — {len(apex_positions)} tracked / {len(t212_positions)} in T212")

    # Send Telegram alert if significant changes
    if alerts:
        msg = "🔄 POSITION RECONCILIATION\n\n" + "\n".join(alerts)
        msg += f"\n\nApex now tracking {len(apex_positions)} positions."
        send_telegram(msg)
        log_info(f"Reconciliation: {len(changes)} changes — {', '.join(changes[:3])}")

    return bool(changes), report

if __name__ == '__main__':
    changes_made, report = reconcile(silent=False)
    print(f"\n  Status: {report['status']}")
    print(f"  Apex: {report['apex_count']} | T212: {report['t212_count']}")
