#!/usr/bin/env python3
# CRON: */5 8-16 * * 1-5
# Watchdog that manages the TACO regime lifecycle.
# Runs the state machine: NEUTRAL → ARMED → ACTIVE → RECOVERY → NEUTRAL
#
# Polls apex-taco-state.json every 5 minutes.
# Writes position intent to apex-taco-pending.json for the signal injector.
# Does NOT execute trades — routes through apex-taco-signal-injector.py.

import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        atomic_write, safe_read, log_error, log_warning, log_info,
        send_telegram, locked_read_modify_write
    )
except ImportError as _e:
    print(f"FATAL: apex_utils import failed: {_e}")
    sys.exit(1)

# ── CONSTANTS ────────────────────────────────────────────────────────────────
LOGS         = '/home/ubuntu/.picoclaw/logs'
SCRIPTS      = '/home/ubuntu/.picoclaw/scripts'
CONFIG_FILE  = '/home/ubuntu/.picoclaw/apex-taco-config.json'
STATE_FILE   = f'{LOGS}/apex-taco-state.json'
MONITOR_FILE = f'{LOGS}/apex-taco-monitor-state.json'
PENDING_FILE = f'{LOGS}/apex-taco-pending.json'
LOG_FILE     = f'{LOGS}/apex-taco-log.json'
POSITIONS_FILE = f'{LOGS}/apex-positions.json'
INJECTOR     = f'{SCRIPTS}/apex-taco-signal-injector.py'
PYTHON       = '/home/ubuntu/bin/python3'
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_MONITOR_STATE = {
    "state":             "NEUTRAL",
    "armed_date":        None,
    "event_id":          None,
    "taco_status_at_arm": None,
    "pending_injection": False,
    "injection_retries": 0,
    "last_transition":   None,
    "last_updated":      None,
}


def load_config():
    """Load TACO config with defaults."""
    return safe_read(CONFIG_FILE, {})


def load_monitor_state():
    """Load persistent monitor state, returning defaults if missing."""
    return safe_read(MONITOR_FILE, dict(_DEFAULT_MONITOR_STATE))


def save_monitor_state(state_dict):
    """Persist monitor state atomically."""
    state_dict['last_updated'] = datetime.now(timezone.utc).isoformat()
    atomic_write(MONITOR_FILE, state_dict)


def is_friday_blackout():
    """Return True if it is Friday after 14:00 UTC — no new TACO entries."""
    now = datetime.now(timezone.utc)
    return now.weekday() == 4 and now.hour >= 14


def is_state_stale(state):
    """Return True if taco-state.json TTL has expired."""
    expires_at = state.get('expires_at', '')
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp
    except Exception:
        return True


def trading_days_since(from_date_str):
    """Count US weekday trading days from from_date_str to today (inclusive of today)."""
    if not from_date_str:
        return 0
    try:
        from_dt = datetime.fromisoformat(from_date_str)
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=timezone.utc)
        from_date = from_dt.date()
        today     = datetime.now(timezone.utc).date()

        # Try to load market calendar holidays
        holidays = set()
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "cal", f"{SCRIPTS}/apex-market-calendar.py")
            cal = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cal)
            holidays = set(getattr(cal, 'US_HOLIDAYS', []))
        except Exception:
            pass  # Fall back to weekday-only counting

        count   = 0
        current = from_date
        while current < today:
            current += timedelta(days=1)
            if current.weekday() < 5 and str(current) not in holidays:
                count += 1
        return count
    except Exception as e:
        log_error(f"TACO monitor trading_days_since: {e}", exc=e)
        return 0


def generate_event_id():
    """Generate a unique event ID for this TACO activation."""
    return f"event_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"


def append_to_log(entry):
    """Append a structured entry to the append-only taco audit log."""
    try:
        def _modifier(data):
            if not isinstance(data, list):
                data = []
            data.append(entry)
            return data
        locked_read_modify_write(LOG_FILE, _modifier, default=[])
    except Exception as e:
        log_error(f"TACO monitor append_to_log: {e}", exc=e)


def invoke_injector(pending_data):
    """Write pending data then invoke signal injector as subprocess."""
    try:
        atomic_write(PENDING_FILE, pending_data)
        result = subprocess.run(
            [PYTHON, INJECTOR],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            log_warning(f"TACO injector returned code {result.returncode}: "
                        f"{result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log_error("TACO monitor: injector timed out after 60s")
        return False
    except Exception as e:
        log_error(f"TACO monitor invoke_injector: {e}", exc=e)
        return False


def check_confirmed():
    """Return True if the Telegram listener has confirmed the pending TACO signal."""
    return safe_read(PENDING_FILE, {}).get('confirmed', False)


def check_taco_position_still_open(event_id):
    """Return True if there is still an open TACO_CONTRARIAN position."""
    try:
        positions = safe_read(POSITIONS_FILE, [])
        return any(
            p.get('signal_type') == 'TACO_CONTRARIAN'
            and p.get('taco_event_id') == event_id
            for p in positions
        )
    except Exception:
        return False


# ── State transition functions ────────────────────────────────────────────────

def neutral_to_armed(taco_state, monitor_state, config):
    """Transition NEUTRAL → ARMED: RHETORIC detected with sufficient confidence."""
    mon_cfg      = config.get('monitor', {})
    event_id     = generate_event_id()
    confidence   = taco_state.get('confidence', 0.0)
    spike_pct    = taco_state.get('vix_spike_pct', 0.0)
    threat_type  = taco_state.get('threat_type', 'DEFAULT')
    now          = datetime.now(timezone.utc)

    first_tranche_mult = mon_cfg.get('first_tranche_size_multiplier', 0.5)

    pending_data = {
        "event_id":             event_id,
        "threat_type":          threat_type,
        "confidence":           confidence,
        "taco_status":          "RHETORIC",
        "taco_tranche":         1,
        "size_multiplier":      first_tranche_mult,
        "trailing_stop":        False,
        "requires_confirmation": True,
        "confirmed":            False,
        "created_at":           now.isoformat(),
    }

    # Write pending and invoke injector
    injected = invoke_injector(pending_data)

    monitor_state.update({
        "state":              "ARMED",
        "armed_date":         now.isoformat(),
        "event_id":           event_id,
        "taco_status_at_arm": "RHETORIC",
        "pending_injection":  not injected,   # retry on next run if failed
        "injection_retries":  0 if injected else 1,
        "last_transition":    now.isoformat(),
    })

    send_telegram(
        f"🌮 TACO ARMED\n\n"
        f"Status: RHETORIC detected\n"
        f"Confidence: {confidence:.0%} | VIX spike: {spike_pct:+.1f}%\n"
        f"Threat type: {threat_type}\n"
        f"Event: {event_id}\n\n"
        f"Signal queued at 50% size (first tranche).\n"
        f"Reply CONFIRM TACO to execute.\n"
        f"Auto-expires in 5 trading days if not confirmed."
    )

    append_to_log({
        "event":      "STATE_TRANSITION",
        "from":       "NEUTRAL",
        "to":         "ARMED",
        "event_id":   event_id,
        "confidence": confidence,
        "threat_type": threat_type,
        "injected":   injected,
        "timestamp":  now.isoformat(),
    })
    return monitor_state


def armed_to_active(monitor_state):
    """Transition ARMED → ACTIVE after human confirmation received."""
    now = datetime.now(timezone.utc)
    monitor_state.update({
        "state":             "ACTIVE",
        "pending_injection": False,
        "injection_retries": 0,
        "last_transition":   now.isoformat(),
    })
    send_telegram(
        f"🌮 TACO ACTIVE\n\n"
        f"Event: {monitor_state.get('event_id')}\n"
        f"Signal confirmed — position entered.\n"
        f"Monitoring for walkback confirmation or stop-out."
    )
    append_to_log({
        "event":     "STATE_TRANSITION",
        "from":      "ARMED",
        "to":        "ACTIVE",
        "event_id":  monitor_state.get('event_id'),
        "timestamp": now.isoformat(),
    })
    return monitor_state


def armed_to_neutral_expiry(monitor_state):
    """Transition ARMED → NEUTRAL: 5 trading days elapsed without confirmation."""
    now = datetime.now(timezone.utc)
    event_id = monitor_state.get('event_id')
    monitor_state.update({
        "state":             "NEUTRAL",
        "armed_date":        None,
        "event_id":          None,
        "taco_status_at_arm": None,
        "pending_injection": False,
        "injection_retries": 0,
        "last_transition":   now.isoformat(),
    })
    send_telegram(
        f"🌮 TACO EXPIRED\n\n"
        f"Event {event_id} expired after 5 trading days without confirmation.\n"
        f"Returning to NEUTRAL. Market moved on without a walkback."
    )
    append_to_log({
        "event":     "STATE_TRANSITION",
        "from":      "ARMED",
        "to":        "NEUTRAL",
        "reason":    "EXPIRY",
        "event_id":  event_id,
        "timestamp": now.isoformat(),
    })
    return monitor_state


def armed_to_neutral_action(monitor_state):
    """Transition ARMED → NEUTRAL: real policy ACTION detected — invalidate signal."""
    now      = datetime.now(timezone.utc)
    event_id = monitor_state.get('event_id')
    # Clear the pending signal so autopilot doesn't execute a stale TACO entry
    atomic_write(PENDING_FILE, {})
    monitor_state.update({
        "state":             "NEUTRAL",
        "armed_date":        None,
        "event_id":          None,
        "taco_status_at_arm": None,
        "pending_injection": False,
        "injection_retries": 0,
        "last_transition":   now.isoformat(),
    })
    send_telegram(
        f"🔴 TACO INVALIDATED\n\n"
        f"Event {event_id}: Real policy ACTION detected — this is not a bluff.\n"
        f"TACO signal cancelled. Staying defensive."
    )
    append_to_log({
        "event":     "STATE_TRANSITION",
        "from":      "ARMED",
        "to":        "NEUTRAL",
        "reason":    "ACTION_DETECTED",
        "event_id":  event_id,
        "timestamp": now.isoformat(),
    })
    return monitor_state


def active_to_recovery(monitor_state, taco_state, config):
    """Transition ACTIVE → RECOVERY: walkback detected, switch to trail mode."""
    now        = datetime.now(timezone.utc)
    event_id   = monitor_state.get('event_id')
    confidence = taco_state.get('confidence', 0.0)
    threat_type = taco_state.get('threat_type', 'DEFAULT')
    mon_cfg    = config.get('monitor', {})

    # Second tranche: full or high-confidence sizing, trailing stop
    if confidence >= mon_cfg.get('high_confidence_threshold', 0.80):
        size_mult = mon_cfg.get('high_confidence_size_multiplier', 1.5)
    else:
        size_mult = mon_cfg.get('full_size_multiplier', 1.0)

    pending_data = {
        "event_id":             event_id,
        "threat_type":          threat_type,
        "confidence":           confidence,
        "taco_status":          "WALKBACK",
        "taco_tranche":         2,
        "size_multiplier":      size_mult,
        "trailing_stop":        True,
        "requires_confirmation": True,  # Second tranche still needs confirmation (first TACO)
        "confirmed":            False,
        "created_at":           now.isoformat(),
    }
    # If autopilot is enabled, second tranche can be autonomous per spec
    autopilot = safe_read(f'{LOGS}/apex-autopilot.json', {})
    if autopilot.get('enabled', False):
        pending_data['requires_confirmation'] = False
        pending_data['confirmed']             = True  # Autonomous second tranche

    injected = invoke_injector(pending_data)

    monitor_state.update({
        "state":             "RECOVERY",
        "pending_injection": not injected,
        "last_transition":   now.isoformat(),
    })
    send_telegram(
        f"🔄 TACO RECOVERY\n\n"
        f"Event {event_id}: WALKBACK confirmed!\n"
        f"Confidence: {confidence:.0%}\n"
        f"Switching to trailing stop mode.\n"
        f"Second tranche signal queued at {size_mult:.1f}x size."
    )
    append_to_log({
        "event":      "STATE_TRANSITION",
        "from":       "ACTIVE",
        "to":         "RECOVERY",
        "event_id":   event_id,
        "confidence": confidence,
        "timestamp":  now.isoformat(),
    })
    return monitor_state


def recovery_to_neutral(monitor_state, reason):
    """Transition RECOVERY → NEUTRAL: trade closed or walkback complete."""
    now      = datetime.now(timezone.utc)
    event_id = monitor_state.get('event_id')
    monitor_state.update({
        "state":             "NEUTRAL",
        "armed_date":        None,
        "event_id":          None,
        "taco_status_at_arm": None,
        "pending_injection": False,
        "injection_retries": 0,
        "last_transition":   now.isoformat(),
    })
    send_telegram(
        f"✅ TACO REGIME COMPLETE\n\n"
        f"Event {event_id} closed. Reason: {reason}\n"
        f"Returning to NEUTRAL. Outcomes tracker will record result."
    )
    append_to_log({
        "event":     "STATE_TRANSITION",
        "from":      "RECOVERY",
        "to":        "NEUTRAL",
        "reason":    reason,
        "event_id":  event_id,
        "timestamp": now.isoformat(),
    })
    return monitor_state


# ── Main state machine ────────────────────────────────────────────────────────

def run():
    """Execute one tick of the TACO monitor state machine."""
    config        = load_config()
    if not config.get('enabled', True):
        log_info("TACO module disabled — skipping monitor")
        return

    taco_state    = safe_read(STATE_FILE, {})
    monitor_state = load_monitor_state()
    mon_cfg       = config.get('monitor', {})
    clf_cfg       = config.get('classifier', {})
    max_retries   = mon_cfg.get('max_injection_retries', 6)

    # Treat stale taco-state as NEUTRAL
    if is_state_stale(taco_state):
        taco_status = "NEUTRAL"
        taco_confidence = 0.0
        taco_spike = 0.0
    else:
        taco_status     = taco_state.get('status', 'NEUTRAL')
        taco_confidence = taco_state.get('confidence', 0.0)
        taco_spike      = taco_state.get('vix_spike_pct', 0.0)

    current_state = monitor_state.get('state', 'NEUTRAL')
    min_conf      = mon_cfg.get('min_confidence_for_armed', 0.65)
    vix_thresh    = clf_cfg.get('vix_spike_threshold_pct', 15.0)

    # ── NEUTRAL ──────────────────────────────────────────────────────────────
    if current_state == 'NEUTRAL':
        if (taco_status == 'RHETORIC'
                and taco_confidence >= min_conf
                and taco_spike >= vix_thresh
                and not is_friday_blackout()):
            monitor_state = neutral_to_armed(taco_state, monitor_state, config)
        else:
            log_info(f"TACO monitor: NEUTRAL (taco={taco_status}, "
                     f"conf={taco_confidence:.2f}, spike={taco_spike:+.1f}%)")

    # ── ARMED ─────────────────────────────────────────────────────────────────
    elif current_state == 'ARMED':
        armed_date   = monitor_state.get('armed_date')
        days_elapsed = trading_days_since(armed_date)
        expiry_days  = mon_cfg.get('armed_expiry_trading_days', 5)

        if taco_status == 'ACTION':
            monitor_state = armed_to_neutral_action(monitor_state)

        elif days_elapsed >= expiry_days:
            monitor_state = armed_to_neutral_expiry(monitor_state)

        elif check_confirmed():
            monitor_state = armed_to_active(monitor_state)

        else:
            # Handle retry logic if injector previously failed
            if monitor_state.get('pending_injection', False):
                retries = monitor_state.get('injection_retries', 0)
                if retries < max_retries:
                    pending_data = safe_read(PENDING_FILE, {})
                    if pending_data.get('event_id'):
                        log_info(f"TACO monitor: retry injection attempt {retries + 1}/{max_retries}")
                        injected = invoke_injector(pending_data)
                        monitor_state['injection_retries'] = retries + 1
                        if injected:
                            monitor_state['pending_injection'] = False
                else:
                    log_warning(f"TACO monitor: max injection retries ({max_retries}) reached for "
                                f"{monitor_state.get('event_id')}")
                    monitor_state['pending_injection'] = False

            log_info(f"TACO monitor: ARMED — {days_elapsed}/{expiry_days} trading days elapsed, "
                     f"awaiting confirmation")

    # ── ACTIVE ────────────────────────────────────────────────────────────────
    elif current_state == 'ACTIVE':
        if taco_status == 'WALKBACK':
            monitor_state = active_to_recovery(monitor_state, taco_state, config)

        elif taco_status == 'ACTION':
            # Real policy — log warning but don't auto-exit (human has control)
            event_id = monitor_state.get('event_id')
            log_warning(f"TACO monitor: ACTION detected while ACTIVE for {event_id}")
            send_telegram(
                f"⚠️ TACO ACTION WARNING\n\n"
                f"Event {event_id}: Real policy ACTION detected while position is open.\n"
                f"Consider manual review. Stop loss is in place."
            )
        else:
            log_info(f"TACO monitor: ACTIVE — taco_status={taco_status}, "
                     f"watching for WALKBACK")

    # ── RECOVERY ──────────────────────────────────────────────────────────────
    elif current_state == 'RECOVERY':
        event_id = monitor_state.get('event_id')

        # Check if position was closed (stop hit or manual close)
        if event_id and not check_taco_position_still_open(event_id):
            # Position no longer in apex-positions.json
            monitor_state = recovery_to_neutral(monitor_state, "POSITION_CLOSED")

        elif taco_status == 'NEUTRAL':
            # Market returned to normal — walkback complete
            monitor_state = recovery_to_neutral(monitor_state, "WALKBACK_COMPLETE")

        else:
            log_info(f"TACO monitor: RECOVERY — trailing stop active, "
                     f"taco_status={taco_status}")

    save_monitor_state(monitor_state)


def main():
    """Entry point: run one state machine tick, always exit 0 for cron health."""
    try:
        run()
    except Exception as e:
        log_error(f"TACO monitor fatal: {e}", exc=e)
    sys.exit(0)


if __name__ == "__main__":
    main()
