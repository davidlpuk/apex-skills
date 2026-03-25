#!/usr/bin/env python3
"""
Trade Queue System
Allows queuing trades outside market hours for execution at next market open.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


QUEUE_FILE     = '/home/ubuntu/.picoclaw/logs/apex-trade-queue.json'
SIGNAL_FILE    = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

def load_queue():
    try:
        with open(QUEUE_FILE) as f:
            return json.load(f)
    except:
        return []

def save_queue(queue):
    atomic_write(QUEUE_FILE, queue)

def add_to_queue(signal):
    queue = load_queue()
    now   = datetime.now(timezone.utc)

    entry = {
        'id':           len(queue) + 1,
        'queued_at':    now.isoformat(),
        'queued_date':  now.strftime('%Y-%m-%d %H:%M UTC'),
        'name':         signal.get('name','?'),
        't212_ticker':  signal.get('t212_ticker',''),
        'entry':        signal.get('entry', 0),
        'stop':         signal.get('stop', 0),
        'target1':      signal.get('target1', 0),
        'target2':      signal.get('target2', 0),
        'quantity':     signal.get('quantity', 0),
        'score':        signal.get('score', 0),
        'signal_type':  signal.get('signal_type','TREND'),
        'rsi':          signal.get('rsi', 0),
        'sector':       signal.get('sector',''),
        'currency':     signal.get('currency','USD'),
        'status':       'QUEUED',
        'notes':        signal.get('notes',''),
    }

    queue.append(entry)
    save_queue(queue)

    send_telegram(
        f"📋 TRADE QUEUED\n\n"
        f"{entry['name']}\n"
        f"Entry: £{entry['entry']} | Stop: £{entry['stop']}\n"
        f"T1: £{entry['target1']} | T2: £{entry['target2']}\n"
        f"Qty: {entry['quantity']} | Score: {entry['score']}/10\n\n"
        f"Will execute at next market open.\n"
        f"Queue ID: #{entry['id']}\n"
        f"Reply QUEUE CANCEL {entry['id']} to remove."
    )

    print(f"✅ Trade queued: {entry['name']} (ID #{entry['id']})")
    return entry

def add_scored_signal(signal):
    """
    Queue a fully-scored signal from the decision engine.
    Unlike add_to_queue() (manual), this preserves all scored fields and
    sends a quieter notification — used for 2nd/3rd signals in multi-signal day.
    All safety gates still apply at execution time via autopilot.
    """
    # Position limit guard — don't queue if already at max open + queued
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_cfg", "/home/ubuntu/.picoclaw/scripts/apex_config.py")
        _cm = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_cm)
        _max_pos = getattr(_cm, 'MAX_OPEN_POSITIONS', 6)
    except Exception:
        _max_pos = 6
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-positions.json') as _pf:
            _positions = json.load(_pf)
        _open_count = len(_positions) if isinstance(_positions, list) else len(_positions.get('positions', []))
    except Exception:
        _open_count = 0
    _existing_queued = len([t for t in load_queue() if t.get('status') == 'QUEUED'])
    if (_open_count + _existing_queued) >= _max_pos:
        print(f"  Skipping queue: position limit reached ({_open_count} open + {_existing_queued} queued = {_max_pos})")
        return None

    queue = load_queue()
    now   = datetime.now(timezone.utc)

    entry = {
        'id':              len(queue) + 1,
        'queued_at':       now.isoformat(),
        'queued_date':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'source':          'decision_engine',
        'name':            signal.get('name', '?'),
        't212_ticker':     signal.get('t212_ticker', ''),
        'entry':           signal.get('entry', 0),
        'stop':            signal.get('stop', 0),
        'target1':         signal.get('target1', 0),
        'target2':         signal.get('target2', 0),
        'quantity':        signal.get('quantity', 0),
        'score':           signal.get('score', 0),
        'adjusted_score':  signal.get('adjusted_score', signal.get('score', 0)),
        'signal_type':     signal.get('signal_type', 'TREND'),
        'rsi':             signal.get('rsi', 0),
        'sector':          signal.get('sector', ''),
        'currency':        signal.get('currency', 'USD'),
        'ev':              signal.get('ev', 0),
        'risk_amount':     signal.get('risk_amount', 0),
        'notional':        signal.get('notional', 0),
        'sizing_rationale': signal.get('sizing_rationale', ''),
        'reasons':         signal.get('reasons', []),
        'status':          'QUEUED',
        'notes':           signal.get('notes', ''),
    }

    queue.append(entry)
    save_queue(queue)

    score = entry['adjusted_score']
    send_telegram(
        f"📋 SIGNAL QUEUED (#{entry['id']})\n\n"
        f"{entry['name']} | Score {score:.1f}/10 | {entry['signal_type']}\n"
        f"Entry: {entry['entry']} | Stop: {entry['stop']}\n"
        f"EV: {entry.get('ev', '?')} | Risk: £{entry.get('risk_amount', '?')}\n\n"
        f"Will execute at 09:30 UTC (after primary signal).\n"
        f"Reply QUEUE CANCEL {entry['id']} to remove."
    )

    print(f"✅ Scored signal queued: {entry['name']} score={score:.1f} (ID #{entry['id']})")
    return entry


def cancel_queue(trade_id):
    queue = load_queue()
    trade = next((t for t in queue if t['id'] == trade_id), None)

    if not trade:
        send_telegram(f"⚠️ Queue ID #{trade_id} not found.")
        return False

    queue = [t for t in queue if t['id'] != trade_id]
    save_queue(queue)

    send_telegram(
        f"❌ TRADE REMOVED FROM QUEUE\n\n"
        f"{trade['name']} (ID #{trade_id})\n"
        f"Entry: £{trade['entry']} cancelled."
    )

    print(f"✅ Removed queue ID #{trade_id}: {trade['name']}")
    return True

def show_queue():
    queue = load_queue()
    pending = [t for t in queue if t['status'] == 'QUEUED']

    if not pending:
        send_telegram("📋 TRADE QUEUE\n\nNo trades queued.\n\nUse 'buy [instrument]' to add a trade to the queue outside market hours.")
        return

    lines = [f"📋 TRADE QUEUE — {len(pending)} pending\n"]
    for t in pending:
        lines.append(
            f"#{t['id']} {t['name']}\n"
            f"  Entry: £{t['entry']} | Stop: £{t['stop']}\n"
            f"  Qty: {t['quantity']} | Score: {t['score']}/10\n"
            f"  Queued: {t['queued_date']}\n"
        )
    lines.append("Executes at next market open (08:30 UTC Mon-Fri)")
    lines.append("QUEUE CANCEL [ID] to remove a trade")

    send_telegram('\n'.join(lines))

def execute_queue():
    """Execute all queued trades — called at market open."""
    queue    = load_queue()
    pending  = [t for t in queue if t['status'] == 'QUEUED']

    if not pending:
        print("No queued trades to execute")
        return

    now = datetime.now(timezone.utc)

    # Check market hours — only execute Mon-Fri 08:00-15:30
    if now.weekday() >= 5:
        print("Weekend — not executing queue")
        return

    hour_min = now.hour * 60 + now.minute
    if hour_min < 480 or hour_min > 930:
        print(f"Outside market hours ({now.hour}:{now.minute:02d}) — not executing")
        return

    send_telegram(
        f"🔔 MARKET OPEN — EXECUTING QUEUE\n\n"
        f"{len(pending)} trade(s) queued for execution.\n"
        f"Placing orders now..."
    )

    executed = []
    failed   = []

    for trade in pending:
        # Save as pending signal and execute
        signal = {
            'name':        trade['name'],
            't212_ticker': trade['t212_ticker'],
            'quantity':    trade['quantity'],
            'entry':       trade['entry'],
            'stop':        trade['stop'],
            'target1':     trade['target1'],
            'target2':     trade['target2'],
            'score':       trade['score'],
            'rsi':         trade['rsi'],
            'macd':        0,
            'sector':      trade['sector'],
            'signal_type': trade['signal_type'],
            'currency':    trade['currency'],
            'generated_at':now.isoformat(),
        }

        atomic_write(SIGNAL_FILE, signal)

        # Execute
        result = subprocess.run(
            ['bash', '/home/ubuntu/.picoclaw/scripts/apex-execute-order.sh'],
            capture_output=True, text=True
        )

        if result.returncode == 0:
            trade['status']      = 'EXECUTED'
            trade['executed_at'] = now.isoformat()
            executed.append(trade)
            print(f"✅ Executed: {trade['name']}")
        else:
            trade['status'] = 'FAILED'
            trade['error']  = result.stderr[:200]
            failed.append(trade)
            print(f"❌ Failed: {trade['name']}")

    save_queue(queue)

    # Summary
    summary = f"📋 QUEUE EXECUTION COMPLETE\n\n"
    if executed:
        summary += f"✅ Executed ({len(executed)}):\n"
        for t in executed:
            summary += f"  {t['name']} @ £{t['entry']}\n"
    if failed:
        summary += f"\n❌ Failed ({len(failed)}):\n"
        for t in failed:
            summary += f"  {t['name']} — check T212\n"

    send_telegram(summary)

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'show'

    if mode == 'show':
        show_queue()
    elif mode == 'execute':
        execute_queue()
    elif mode == 'cancel' and len(sys.argv) > 2:
        cancel_queue(int(sys.argv[2]))
    elif mode == 'queue_signal':
        # Queue the current pending signal
        try:
            with open(SIGNAL_FILE) as f:
                signal = json.load(f)
            add_to_queue(signal)
        except Exception as e:
            print(f"Error queuing signal: {e}")
    elif mode == 'add':
        # Test add
        test_signal = {
            'name': 'Visa', 't212_ticker': 'V_US_EQ',
            'entry': 300.0, 'stop': 282.0,
            'target1': 327.0, 'target2': 345.0,
            'quantity': 1.32, 'score': 7,
            'signal_type': 'CONTRARIAN', 'rsi': 21,
            'sector': 'Financials', 'currency': 'USD'
        }
        add_to_queue(test_signal)
