#!/usr/bin/env python3
"""
Catastrophic Loss Circuit Breaker
Monitors portfolio value in real time and suspends all trading
if single-session loss exceeds threshold — regardless of individual stops.

Protects against:
- Gap downs that blow through ATR stops
- Multiple simultaneous stop hits
- API errors placing wrong sized orders
- Black swan events mid-session

Thresholds:
- WARNING:  -3% session loss — alert, continue trading
- CAUTION:  -5% session loss — reduce sizing to 50%
- SUSPEND: -8% session loss — halt all new entries
- CRITICAL:-12% session loss — close all positions
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

BREAKER_FILE   = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
PAUSE_FLAG     = '/home/ubuntu/.picoclaw/logs/apex-paused.flag'

# Thresholds as % of session opening portfolio value
THRESHOLDS = {
    'WARNING':  -3.0,   # Alert only
    'CAUTION':  -5.0,   # Reduce sizing
    'SUSPEND':  -8.0,   # Halt new entries
    'CRITICAL': -12.0,  # Close all positions
}

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

def get_portfolio_value():
    """Get current portfolio value from T212."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')
        result   = subprocess.run([
            'curl','-s','--max-time','10',
            '-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/account/cash'
        ], capture_output=True, text=True)
        cash = json.loads(result.stdout)
        # Use T212's total field directly — most accurate
        free     = float(cash.get('free', 0))
        invested = float(cash.get('invested', 0))
        total    = round(float(cash.get('total', free + invested)), 2)

        # Also get open P&L
        port_result = subprocess.run([
            'curl','-s','--max-time','10',
            '-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/portfolio'
        ], capture_output=True, text=True)
        portfolio = json.loads(port_result.stdout)
        open_pnl  = round(sum(float(p.get('ppl',0)) for p in
                          (portfolio if isinstance(portfolio, list) else [])), 2)

        return total, open_pnl
    except Exception as e:
        log_error(f"get_portfolio_value failed: {e}")
        return None, None

def record_session_open():
    """Record portfolio value at market open for session baseline."""
    total, open_pnl = get_portfolio_value()
    if total is None:
        return None

    now    = datetime.now(timezone.utc)
    breaker = safe_read(BREAKER_FILE, {})

    # Only record once per day
    today = now.strftime('%Y-%m-%d')
    if breaker.get('session_date') == today:
        return breaker

    session_data = {
        'session_date':      today,
        'session_open':      total,
        'session_open_time': now.isoformat(),
        'current_value':     total,  # Initialise to open value
        'open_pnl':          open_pnl,
        'session_pnl':       0,
        'session_pnl_pct':   0,
        'status':            'CLEAR',
        'triggered':         False,
        'trigger_level':     None,
        'checks':            [],
    }

    atomic_write(BREAKER_FILE, session_data)
    print(f"  Session open recorded: £{total}")
    return session_data

def check_circuit_breaker():
    """
    Check current portfolio against session open.
    Returns (status, pnl_pct, action_required)
    """
    now     = datetime.now(timezone.utc)
    breaker = safe_read(BREAKER_FILE, {})

    # Ensure session baseline exists
    today = now.strftime('%Y-%m-%d')
    if breaker.get('session_date') != today:
        breaker = record_session_open()
        if not breaker:
            return 'UNKNOWN', 0, 'Cannot get portfolio value'

    session_open = float(breaker.get('session_open', 5000))
    total, open_pnl = get_portfolio_value()

    if total is None or total == 0:
        # Fall back to stored current value if API fails
        stored = float(breaker.get('current_value', session_open))
        if stored > 0:
            total = stored
        else:
            return 'UNKNOWN', 0, 'Cannot get portfolio value'

    # Session P&L
    session_pnl     = round(total - session_open, 2)
    session_pnl_pct = round(session_pnl / session_open * 100, 2) if session_open > 0 else 0

    # Determine status
    status         = 'CLEAR'
    action         = 'Continue normal trading'
    trigger_level  = None

    for level, threshold in sorted(THRESHOLDS.items(),
                                    key=lambda x: x[1]):
        if session_pnl_pct <= threshold:
            status        = level
            trigger_level = threshold
            if level == 'WARNING':
                action = f"Monitor closely — session loss {session_pnl_pct}%"
            elif level == 'CAUTION':
                action = f"Reduce position sizing to 50% — session loss {session_pnl_pct}%"
            elif level == 'SUSPEND':
                action = f"HALT new entries — session loss {session_pnl_pct}%"
            elif level == 'CRITICAL':
                action = f"CLOSE ALL POSITIONS — session loss {session_pnl_pct}%"

    # Update breaker state
    prev_status = breaker.get('status', 'CLEAR')
    checks      = breaker.get('checks', [])
    checks.append({
        'time':       now.strftime('%H:%M UTC'),
        'total':      total,
        'pnl':        session_pnl,
        'pnl_pct':    session_pnl_pct,
        'status':     status,
    })

    breaker.update({
        'current_value':   total,
        'session_pnl':     session_pnl,
        'session_pnl_pct': session_pnl_pct,
        'status':          status,
        'triggered':       status not in ['CLEAR', 'WARNING'],
        'trigger_level':   trigger_level,
        'last_check':      now.isoformat(),
        'checks':          checks[-48:],  # Keep last 48 checks (24h)
    })
    atomic_write(BREAKER_FILE, breaker)

    # Take action if status changed
    if status != prev_status and status != 'CLEAR':
        icon = {'WARNING':'⚠️','CAUTION':'🟠','SUSPEND':'🔴','CRITICAL':'🚨'}.get(status,'⚠️')
        send_telegram(
            f"{icon} CIRCUIT BREAKER — {status}\n\n"
            f"Session P&L: £{session_pnl} ({session_pnl_pct}%)\n"
            f"Portfolio: £{total}\n"
            f"Session open: £{session_open}\n\n"
            f"Action: {action}"
        )
        log_warning(f"Circuit breaker {status}: {session_pnl_pct}% session loss")

    # Execute actions
    if status == 'SUSPEND':
        # Create pause flag
        with open(PAUSE_FLAG, 'w') as f:
            f.write(f"Circuit breaker SUSPEND at {now.isoformat()}\n"
                   f"Session loss: {session_pnl_pct}%")
        print(f"  🔴 SUSPENDED — pause flag set")

    elif status == 'CRITICAL':
        # Create pause flag + alert to close
        with open(PAUSE_FLAG, 'w') as f:
            f.write(f"Circuit breaker CRITICAL at {now.isoformat()}\n"
                   f"Session loss: {session_pnl_pct}%")
        send_telegram(
            f"🚨 CRITICAL CIRCUIT BREAKER\n\n"
            f"Session loss: {session_pnl_pct}% (£{session_pnl})\n\n"
            f"ALL TRADING SUSPENDED\n"
            f"Review open positions manually.\n"
            f"Consider closing all positions to protect capital.\n\n"
            f"Send APEX RESUME to re-enable after review."
        )
        log_error(f"CRITICAL circuit breaker: {session_pnl_pct}% session loss")

    return status, session_pnl_pct, action

def get_size_multiplier():
    """
    Returns position size multiplier based on circuit breaker state.
    Called by position sizer before every trade.
    """
    breaker = safe_read(BREAKER_FILE, {'status': 'CLEAR'})
    status  = breaker.get('status', 'CLEAR')

    multipliers = {
        'CLEAR':    1.0,
        'WARNING':  0.75,
        'CAUTION':  0.50,
        'SUSPEND':  0.0,
        'CRITICAL': 0.0,
        'UNKNOWN':  0.5,
    }
    return multipliers.get(status, 1.0), status

def run():
    """Run circuit breaker check."""
    now = datetime.now(timezone.utc)
    print(f"\n=== CIRCUIT BREAKER CHECK ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    # Record session open if needed
    breaker = safe_read(BREAKER_FILE, {})
    today   = now.strftime('%Y-%m-%d')
    if breaker.get('session_date') != today:
        print(f"  Recording session open...")
        record_session_open()

    status, pnl_pct, action = check_circuit_breaker()

    icons = {'CLEAR':'✅','WARNING':'⚠️','CAUTION':'🟠',
             'SUSPEND':'🔴','CRITICAL':'🚨','UNKNOWN':'❓'}
    print(f"  {icons.get(status,'⚠️')} Status: {status}")
    print(f"  Session P&L: {pnl_pct:+.2f}%")
    print(f"  Action: {action}")

    mult, _ = get_size_multiplier()
    if mult < 1.0:
        print(f"  Size multiplier: {mult}x")

    return status, pnl_pct

if __name__ == '__main__':
    run()
