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
AUTOPILOT_FILE = '/home/ubuntu/.picoclaw/logs/apex-autopilot.json'

def load_queue():
    try:
        with open(QUEUE_FILE) as f:
            return json.load(f)
    except:
        return []

def save_queue(queue):
    atomic_write(QUEUE_FILE, queue)

def _is_duplicate(queue, ticker, signal_type):
    """
    Returns the existing entry if the same ticker+signal_type is already
    QUEUED, EXECUTED, or FAILED for today's session, None otherwise.
    Prevents re-scans or TACO events from double-queuing the same instrument,
    including instruments already executed or failed earlier the same day.
    FAILED is included to prevent thrashing when execution repeatedly fails
    (e.g. price feed error, limit non-fill) — the next day's scan will retry.
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for t in queue:
        if (t.get('status') in ('QUEUED', 'EXECUTED', 'FAILED')
                and t.get('t212_ticker') == ticker
                and t.get('signal_type', '').upper() == signal_type.upper()
                and t.get('queued_at', '').startswith(today)):
            return t
    return None


def add_to_queue(signal):
    queue = load_queue()
    now   = datetime.now(timezone.utc)

    ticker      = signal.get('t212_ticker', '')
    signal_type = signal.get('signal_type', 'TREND')
    dup = _is_duplicate(queue, ticker, signal_type)
    if dup:
        print(f"  Dedup: {signal.get('name','?')} ({ticker}) already QUEUED as ID #{dup['id']} — skipping")
        return None

    next_id = max((t.get('id', 0) for t in queue), default=0) + 1
    entry = {
        'id':           next_id,
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

    # Second-layer duplicate guard: block if ticker already in open positions.
    # Primary guard is is_blocked() in apex_filters.py; this catches any slip-through.
    _ticker_check = signal.get('t212_ticker', '')
    if _ticker_check and isinstance(_positions, list):
        _held = {p.get('t212_ticker', '') for p in _positions}
        if _ticker_check in _held:
            print(f"  Already held: {signal.get('name','?')} ({_ticker_check}) — skipping queue")
            return None

    queue = load_queue()
    now   = datetime.now(timezone.utc)

    ticker      = signal.get('t212_ticker', '')
    signal_type = signal.get('signal_type', 'TREND')
    dup = _is_duplicate(queue, ticker, signal_type)
    if dup:
        print(f"  Dedup: {signal.get('name','?')} ({ticker}) already QUEUED as ID #{dup['id']} — skipping")
        return None

    next_id = max((t.get('id', 0) for t in queue), default=0) + 1
    entry = {
        'id':              next_id,
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

def purge_stale_entries(queue, max_age_days=7):
    """
    Remove terminal entries (EXECUTED, FAILED, CANCELLED) older than
    max_age_days.  QUEUED entries are never purged here — they decay via
    score-check or market-hours guards.
    Returns (cleaned_queue, purge_count).
    """
    now      = datetime.now(timezone.utc)
    cutoff   = max_age_days * 86400  # seconds
    terminal = {'EXECUTED', 'FAILED', 'CANCELLED'}
    kept, purged = [], 0
    for t in queue:
        if t.get('status') in terminal:
            try:
                age = (now - datetime.fromisoformat(t['queued_at'])).total_seconds()
                if age > cutoff:
                    purged += 1
                    continue
            except Exception:
                pass
        kept.append(t)
    return kept, purged


QUEUE_LOCK_FILE = '/home/ubuntu/.picoclaw/logs/apex-queue-execute.lock'

def _acquire_queue_lock() -> bool:
    """
    Write a PID lock file.  Returns True if the lock was acquired (no other
    execute_queue() instance is running), False if one is already active.
    Stale locks (PID no longer alive) are cleared automatically.
    """
    import os, signal as _signal
    if os.path.exists(QUEUE_LOCK_FILE):
        try:
            with open(QUEUE_LOCK_FILE) as _f:
                old_pid = int(_f.read().strip())
            # Check if PID is still alive
            os.kill(old_pid, 0)   # raises OSError if dead
            print(f"Queue execute already running (PID {old_pid}) — skipping")
            return False
        except (OSError, ValueError):
            # Dead PID or corrupt file — clear it
            os.remove(QUEUE_LOCK_FILE)
    try:
        with open(QUEUE_LOCK_FILE, 'w') as _f:
            _f.write(str(os.getpid()))
        return True
    except Exception as _e:
        log_warning(f"Could not write queue lock: {_e}")
        return True   # fail-open: allow execution if lock can't be written

def _release_queue_lock():
    import os
    try:
        os.remove(QUEUE_LOCK_FILE)
    except FileNotFoundError:
        pass


def execute_queue():
    """Execute all queued trades — called at market open."""
    # ── Concurrent execution guard ────────────────────────────────────────────
    # The executor polls for up to 3 min per trade.  Without a lock, two cron
    # triggers fired 5 min apart can overlap and attempt to execute the same
    # QUEUED entry simultaneously — producing duplicate orders.
    if not _acquire_queue_lock():
        return

    try:
        _execute_queue_inner()
    finally:
        _release_queue_lock()


def _execute_queue_inner():
    """Inner queue execution logic — always called inside a lock."""
    queue    = load_queue()

    # Purge stale terminal entries to keep the queue file lean
    queue, purged = purge_stale_entries(queue)
    if purged:
        save_queue(queue)
        print(f"Purged {purged} stale queue entries (>7 days, terminal status)")

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

    EARNINGS_FILE = '/home/ubuntu/.picoclaw/logs/apex-earnings-flags.json'
    NEWS_FILE     = '/home/ubuntu/.picoclaw/logs/apex-news-flags.json'

    for trade in pending:
        # ── Intraday safety re-check ─────────────────────────────────────────
        # Re-read earnings and news flags from disk at execution time.
        # These may have been updated since signals were generated at 08:30.
        trade_name = trade.get('name', '')
        try:
            _earnings_raw = safe_read(EARNINGS_FILE, [])
            if isinstance(_earnings_raw, list):
                _earnings_blocked = [d['name'] if isinstance(d, dict) else d for d in _earnings_raw]
            elif isinstance(_earnings_raw, dict):
                _earnings_blocked = list(_earnings_raw.keys())
            else:
                _earnings_blocked = []
        except Exception:
            _earnings_blocked = []

        try:
            _news_blocked = safe_read(NEWS_FILE, [])
            if not isinstance(_news_blocked, list):
                _news_blocked = list(_news_blocked) if _news_blocked else []
        except Exception:
            _news_blocked = []

        if trade_name in _earnings_blocked:
            trade['status'] = 'CANCELLED'
            trade['notes']  = 'Earnings block detected at execution time — skipped'
            send_telegram(
                f"⚠️ QUEUE TRADE SKIPPED — EARNINGS BLOCK\n\n"
                f"{trade_name}\n"
                f"Earnings flag detected since signal was queued. Order not placed."
            )
            print(f"  Earnings block at execution: {trade_name} — cancelled")
            continue

        if trade_name in _news_blocked:
            trade['status'] = 'CANCELLED'
            trade['notes']  = 'News block detected at execution time — skipped'
            send_telegram(
                f"⚠️ QUEUE TRADE SKIPPED — NEWS BLOCK\n\n"
                f"{trade_name}\n"
                f"News flag detected since signal was queued. Order not placed."
            )
            print(f"  News block at execution: {trade_name} — cancelled")
            continue

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

            # Verify position actually appeared in positions file
            _ticker = trade.get('t212_ticker', '')
            try:
                _pos_data = safe_read(POSITIONS_FILE, [])
                if isinstance(_pos_data, dict):
                    _pos_data = _pos_data.get('positions', [])
                _pos_tickers = {p.get('t212_ticker', '') for p in _pos_data if isinstance(p, dict)}
                if _ticker and _ticker not in _pos_tickers:
                    trade['status'] = 'FAILED'
                    trade['error']  = 'Subprocess returned 0 but position not found in apex-positions.json'
                    failed.append(trade)
                    log_warning(f"Queue: {trade['name']} ({_ticker}) — executor returned success but position missing from positions file")
                    print(f"⚠️ Position verification failed: {trade['name']} — marking FAILED")
                    continue
            except Exception as _ve:
                log_warning(f"Queue: could not verify position for {trade['name']}: {_ve}")

            executed.append(trade)
            print(f"✅ Executed: {trade['name']}")
            # Increment autopilot trade counters so dashboard reflects queue executions
            try:
                ap = safe_read(AUTOPILOT_FILE, {})
                ap['trades_today']            = ap.get('trades_today', 0) + 1
                ap['total_autonomous_trades'] = ap.get('total_autonomous_trades', 0) + 1
                ap['last_trade_time']         = now.isoformat()
                ap['last_action']             = f"QUEUE:{trade.get('signal_type','?')}:{trade['name']}"
                ap['last_action_ts']          = now.isoformat()
                atomic_write(AUTOPILOT_FILE, ap)
            except Exception as _e:
                log_warning(f"Autopilot counter write failed — trades_today may be stale: {_e}")
                send_telegram(f"⚠️ Autopilot counter write failed — max_trades_per_day gate may be inaccurate.\n{_e}")
        else:
            stderr = result.stderr or ''
            # Detect T212 instrument-invisible — permanent block (MiFID II restriction or
            # account type restriction). Do NOT retry — it will never succeed this session.
            # Contrast with transient suspensions during extreme volatility which resolve
            # within minutes; those would show a different error.
            is_permanently_blocked = ('instrument-invisible' in stderr or
                                      'instrument can not be traded' in stderr.lower() or
                                      'Instrument can not be traded' in stderr)
            # Detect T212 transient suspension (server overload, circuit breaker) — retry
            # These show up as 5xx errors or TooManyRequests, NOT instrument-invisible
            is_suspended = (not is_permanently_blocked and
                            ('TooManyRequests' in stderr or
                             'HTTP Error 5' in stderr or
                             'temporarily' in stderr.lower()))
            MAX_RETRIES = 3  # ~3 executor runs (90 min), enough for transient suspensions
            retry_count = trade.get('retry_count', 0)
            if is_permanently_blocked:
                trade['status'] = 'FAILED'
                trade['error']  = 'T212: Instrument can not be traded (MiFID II restriction or account type block)'
                trade['notes']  = 'Permanent — remove ticker from scanning universe'
                failed.append(trade)
                print(f"🚫 Permanently blocked: {trade['name']} ({trade['t212_ticker']}) — not retrying")
                log_warning(f"Queue: {trade['t212_ticker']} permanently blocked by T212 (instrument-invisible) — cancel, not retrying")
                send_telegram(
                    f"🚫 INSTRUMENT BLOCKED BY T212\n\n"
                    f"{trade['name']} ({trade['t212_ticker']})\n"
                    f"T212 says: Instrument can not be traded.\n"
                    f"Likely MiFID II restriction on US-listed leveraged ETF.\n"
                    f"Action: replace ticker with a UK/EU-listed equivalent."
                )
            elif is_suspended and retry_count < MAX_RETRIES:
                trade['status']      = 'QUEUED'
                trade['retry_count'] = retry_count + 1
                trade['error']       = stderr[:300]
                trade['notes']       = f"T212 transient suspension — auto-retry {retry_count+1}/{MAX_RETRIES}"
                print(f"⏳ Suspended (retry {retry_count+1}/{MAX_RETRIES}): {trade['name']} — re-queued for next run")
                log_warning(f"Queue: {trade['t212_ticker']} transient suspension, retry {retry_count+1}/{MAX_RETRIES}")
            else:
                trade['status'] = 'FAILED'
                trade['error']  = stderr[:300]
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
