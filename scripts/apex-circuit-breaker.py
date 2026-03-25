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
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram, t212_request
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

try:
    from apex_config import (CB_WARNING, CB_CAUTION, CB_SUSPEND, CB_CRITICAL, CB_RESUME,
                              CB_MULT_WARNING, CB_MULT_CAUTION, CB_MULT_SUSPEND,
                              CB_MULT_CRITICAL, CB_MULT_UNKNOWN)
except ImportError:
    CB_WARNING      = -3.0
    CB_CAUTION      = -5.0
    CB_SUSPEND      = -8.0
    CB_CRITICAL     = -12.0
    CB_RESUME       = -4.0
    CB_MULT_WARNING  = 0.75
    CB_MULT_CAUTION  = 0.50
    CB_MULT_SUSPEND  = 0.0
    CB_MULT_CRITICAL = 0.0
    CB_MULT_UNKNOWN  = 0.5

BREAKER_FILE   = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
PAUSE_FLAG     = '/home/ubuntu/.picoclaw/logs/apex-paused.flag'
ROLLING_FILE   = '/home/ubuntu/.picoclaw/logs/apex-rolling-pnl.json'

# Thresholds sourced from apex_config — edit thresholds there, not here
THRESHOLDS = {
    'WARNING':  CB_WARNING,
    'CAUTION':  CB_CAUTION,
    'SUSPEND':  CB_SUSPEND,
    'CRITICAL': CB_CRITICAL,
}

# After SUSPEND auto-resume: trade at 50% sizing for this many trades
RECOVERY_RAMP_TRADES = 5

# Rolling N-day thresholds — catches death-by-a-thousand-cuts
# e.g. three consecutive -3% days = -9% cumulative, but daily CB never triggers
ROLLING_THRESHOLDS = {
    3:  -8.0,    # 3-day cumulative loss > 8% → CAUTION
    5:  -10.0,   # 5-day cumulative loss > 10% → SUSPEND
    10: -15.0,   # 10-day cumulative loss > 15% → HALT (same as drawdown)
}

def get_portfolio_value():
    """Get current portfolio value from T212 via centralised rate-limited caller."""
    try:
        cash = t212_request('/equity/account/cash', timeout=10)
        if cash is None:
            return None, None
        free     = float(cash.get('free', 0))
        invested = float(cash.get('invested', 0))
        total    = round(float(cash.get('total', free + invested)), 2)

        portfolio = t212_request('/equity/portfolio', timeout=10)
        open_pnl  = round(sum(float(p.get('ppl', 0)) for p in
                          (portfolio if isinstance(portfolio, list) else [])), 2)

        return total, open_pnl
    except Exception as e:
        log_error(f"get_portfolio_value failed: {e}")
        return None, None

def record_session_close(session_pnl_pct):
    """
    Record today's session P&L in the rolling history.
    Called at end of session (or when circuit breaker runs at session close time).
    Maintains a rolling 10-day log of session P&L percentages.
    """
    now    = datetime.now(timezone.utc)
    today  = now.strftime('%Y-%m-%d')
    rolling = safe_read(ROLLING_FILE, {'sessions': []})
    sessions = rolling.get('sessions', [])

    # Avoid duplicate entries for same day
    sessions = [s for s in sessions if s.get('date') != today]
    sessions.append({'date': today, 'pnl_pct': round(session_pnl_pct, 3)})

    # Keep last 15 sessions
    rolling['sessions']    = sessions[-15:]
    rolling['last_updated'] = now.isoformat()
    atomic_write(ROLLING_FILE, rolling)
    return rolling


def check_rolling_drawdown():
    """
    Check cumulative P&L across the last N trading sessions.
    Catches slow bleed (multiple moderate losing sessions) that the
    daily circuit breaker misses because it resets each morning.

    Returns (status, worst_window, cumulative_pct, action)
    """
    rolling  = safe_read(ROLLING_FILE, {'sessions': []})
    sessions = rolling.get('sessions', [])

    if len(sessions) < 3:
        return 'CLEAR', None, 0.0, 'Insufficient history for rolling check'

    worst_status = 'CLEAR'
    worst_window = None
    worst_pct    = 0.0
    action       = 'No rolling risk detected'

    for window, threshold in sorted(ROLLING_THRESHOLDS.items()):
        recent = sessions[-window:]
        if len(recent) < window:
            continue
        cumulative = sum(s['pnl_pct'] for s in recent)
        if cumulative <= threshold:
            status = ('CAUTION' if window == 3 else
                      'SUSPEND' if window == 5 else 'CRITICAL')
            if cumulative < worst_pct:
                worst_status = status
                worst_window = window
                worst_pct    = cumulative
                action       = (f"{window}-day cumulative loss {cumulative:.1f}% "
                                f"≤ threshold {threshold}% → {status}")

    return worst_status, worst_window, worst_pct, action


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

    # ── Auto-resume logic ──────────────────────────────────────────────
    # If a previous SUSPEND/CAUTION was set and P&L has recovered enough,
    # automatically lift the pause flag and notify.
    # CRITICAL is never auto-resumed — always requires manual review.
    RESUME_THRESHOLD = CB_RESUME  # auto-resume threshold — set in apex_config.py
    was_suspended = prev_status in ('SUSPEND', 'CAUTION')
    auto_resumed  = False

    if was_suspended and status in ('CLEAR', 'WARNING', 'CAUTION') \
            and session_pnl_pct > RESUME_THRESHOLD \
            and prev_status == 'SUSPEND':
        # Remove pause flag if it was set by circuit breaker (not manual)
        if os.path.exists(PAUSE_FLAG):
            try:
                with open(PAUSE_FLAG) as f:
                    flag_content = f.read()
                if 'Circuit breaker' in flag_content:
                    os.remove(PAUSE_FLAG)
                    auto_resumed = True
                    breaker['recovery_trades_remaining'] = RECOVERY_RAMP_TRADES
                    send_telegram(
                        f"✅ CIRCUIT BREAKER AUTO-RESUMED\n\n"
                        f"Session P&L recovered to {session_pnl_pct:+.2f}% "
                        f"(above -{abs(RESUME_THRESHOLD)}% threshold)\n"
                        f"Portfolio: £{total}\n\n"
                        f"Trading re-enabled at 50% sizing for next {RECOVERY_RAMP_TRADES} trades.\n"
                        f"Recovery ramp protects against false resumptions."
                    )
                    log_warning(f"Circuit breaker auto-resumed — recovery ramp: {RECOVERY_RAMP_TRADES} trades at 50%")
            except Exception as e:
                log_error(f"auto-resume failed to remove pause flag: {e}")

    # ── Status change alerts ───────────────────────────────────────────
    if status != prev_status and status != 'CLEAR' and not auto_resumed:
        icon = {'WARNING':'⚠️','CAUTION':'🟠','SUSPEND':'🔴','CRITICAL':'🚨'}.get(status,'⚠️')
        send_telegram(
            f"{icon} CIRCUIT BREAKER — {status}\n\n"
            f"Session P&L: £{session_pnl} ({session_pnl_pct:+.2f}%)\n"
            f"Portfolio: £{total}\n"
            f"Session open: £{session_open}\n\n"
            f"Action: {action}"
            + (f"\n\nAuto-resume at {RESUME_THRESHOLD}% — no action needed."
               if status == 'SUSPEND' else "")
        )
        log_warning(f"Circuit breaker {status}: {session_pnl_pct}% session loss")

    # ── Execute protective actions ─────────────────────────────────────
    if status == 'SUSPEND' and not auto_resumed:
        with open(PAUSE_FLAG, 'w') as f:
            f.write(f"Circuit breaker SUSPEND at {now.isoformat()}\n"
                    f"Session loss: {session_pnl_pct}%")
        print(f"  🔴 SUSPENDED — pause flag set (auto-resumes at {RESUME_THRESHOLD}%)")

    elif status == 'CRITICAL':
        with open(PAUSE_FLAG, 'w') as f:
            f.write(f"Circuit breaker CRITICAL at {now.isoformat()}\n"
                    f"Session loss: {session_pnl_pct}%")
        send_telegram(
            f"🚨 CRITICAL CIRCUIT BREAKER\n\n"
            f"Session loss: {session_pnl_pct}% (£{session_pnl})\n\n"
            f"ALL TRADING SUSPENDED\n"
            f"Review open positions manually.\n"
            f"Consider closing all positions to protect capital.\n\n"
            f"⚠️ CRITICAL requires manual resume — send APEX RESUME after review."
        )
        log_error(f"CRITICAL circuit breaker: {session_pnl_pct}% session loss")

        # Auto partial close on CRITICAL — configurable flag
        # Closes the largest losing position to reduce portfolio exposure
        try:
            _cfg = safe_read('/home/ubuntu/.picoclaw/logs/apex-autopilot.json', {})
            if _cfg.get('auto_partial_close_on_critical', False):
                _positions = safe_read('/home/ubuntu/.picoclaw/logs/apex-positions.json', [])
                if _positions:
                    # Find largest losing position by unrealised P&L (or fallback: largest by value)
                    def _get_loss(p):
                        pnl = p.get('unrealised_pnl', p.get('pnl', 0))
                        try:
                            return float(pnl)
                        except Exception:
                            return 0.0
                    worst = min(_positions, key=_get_loss)
                    worst_ticker = worst.get('t212_ticker', '')
                    worst_name   = worst.get('name', worst_ticker)
                    worst_pnl    = _get_loss(worst)
                    if worst_pnl < 0:
                        send_telegram(
                            f"🚨 AUTO PARTIAL CLOSE — CRITICAL\n\n"
                            f"Closing largest loser: {worst_name} ({worst_ticker})\n"
                            f"Unrealised P&L: £{worst_pnl:.2f}\n\n"
                            f"auto_partial_close_on_critical is enabled.\n"
                            f"Sending close order now..."
                        )
                        import subprocess as _sp2
                        _sp2.Popen([
                            '/home/ubuntu/bin/python3',
                            '/home/ubuntu/.picoclaw/scripts/apex-close-position.py',
                            worst_ticker,
                            '--reason=CRITICAL_AUTO_CLOSE',
                        ])
                        log_error(f"Auto partial close triggered for {worst_ticker} (P&L: £{worst_pnl:.2f})")
        except Exception as _e:
            log_error(f"Auto partial close failed: {_e}")

    # ── Rolling multi-day drawdown check ──────────────────────────────
    # Record today's session P&L into the rolling log, then check if
    # cumulative losses over 3/5/10 days breaches rolling thresholds.
    # This catches 3 × -3% days (= -9% cumulative) which daily CB misses.
    record_session_close(session_pnl_pct)
    roll_status, roll_window, roll_pct, roll_action = check_rolling_drawdown()

    if roll_status != 'CLEAR':
        # Escalate if rolling status is worse than daily status
        daily_severity = ['CLEAR', 'WARNING', 'CAUTION', 'SUSPEND', 'CRITICAL']
        if daily_severity.index(roll_status) > daily_severity.index(status):
            prev_status = status
            status      = roll_status
            action      = f"ROLLING {roll_window}d: {roll_action}"
            trigger_level = ROLLING_THRESHOLDS.get(roll_window)

            # Alert on escalation
            if status != prev_status:
                icon = {'CAUTION': '🟠', 'SUSPEND': '🔴', 'CRITICAL': '🚨'}.get(status, '⚠️')
                send_telegram(
                    f"{icon} ROLLING DRAWDOWN — {status}\n\n"
                    f"Cumulative {roll_window}-day P&L: {roll_pct:+.1f}%\n"
                    f"Threshold: {ROLLING_THRESHOLDS[roll_window]}%\n\n"
                    f"Daily CB resets each morning — this catches slow bleed.\n"
                    f"Action: {action}"
                )
                log_warning(f"Rolling {roll_window}d drawdown {roll_pct:.1f}% → {status}")

            # Set pause flag for SUSPEND/CRITICAL from rolling check
            if status in ('SUSPEND', 'CRITICAL') and not os.path.exists(PAUSE_FLAG):
                with open(PAUSE_FLAG, 'w') as f:
                    f.write(f"Rolling {roll_window}d drawdown {roll_pct:.1f}% at {now.isoformat()}")

    # Persist rolling status into breaker state for downstream consumers
    breaker['rolling_status']  = roll_status
    breaker['rolling_pct']     = roll_pct
    breaker['rolling_window']  = roll_window
    atomic_write(BREAKER_FILE, breaker)

    return status, session_pnl_pct, action

def get_size_multiplier():
    """
    Returns position size multiplier based on circuit breaker state.
    Uses the worse of: daily session status OR rolling multi-day status.
    Called by position sizer before every trade.
    """
    breaker = safe_read(BREAKER_FILE, {'status': 'CLEAR'})
    status  = breaker.get('status', 'CLEAR')

    # Also factor in rolling multi-day drawdown status
    roll_status = breaker.get('rolling_status', 'CLEAR')
    severity    = ['CLEAR', 'WARNING', 'CAUTION', 'SUSPEND', 'CRITICAL']
    if roll_status in severity and severity.index(roll_status) > severity.index(status):
        status = roll_status

    # Multipliers sourced from apex_config — edit there, not here
    multipliers = {
        'CLEAR':    1.0,
        'WARNING':  CB_MULT_WARNING,
        'CAUTION':  CB_MULT_CAUTION,
        'SUSPEND':  CB_MULT_SUSPEND,
        'CRITICAL': CB_MULT_CRITICAL,
        'UNKNOWN':  CB_MULT_UNKNOWN,
    }
    mult = multipliers.get(status, 1.0)

    # Recovery ramp: after auto-resume from SUSPEND, trade at 50% for N trades
    ramp = breaker.get('recovery_trades_remaining', 0)
    if ramp > 0 and mult > 0:
        mult = round(mult * 0.5, 2)

    return mult, status

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
