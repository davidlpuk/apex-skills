#!/usr/bin/env python3
# CRON: 0 17 * * 1-5
# Tracks TACO trade outcomes to feed the EXHAUSTED detection logic and
# send weekly performance summaries via Telegram.
#
# Runs daily at 17:00 UTC (EOD). Reads apex-outcomes.json for
# signal_type=TACO_CONTRARIAN trades and writes apex-taco-outcomes.json.
# Updates the exhausted flag in apex-taco-state.json if it changes.

import sys
import os
from datetime import datetime, timezone, timedelta
from datetime import date as DateType

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        atomic_write, safe_read, log_error, log_warning, log_info, send_telegram
    )
except ImportError as _e:
    print(f"FATAL: apex_utils import failed: {_e}")
    sys.exit(1)

# ── CONSTANTS ────────────────────────────────────────────────────────────────
LOGS          = '/home/ubuntu/.picoclaw/logs'
CONFIG_FILE   = '/home/ubuntu/.picoclaw/apex-taco-config.json'
OUTCOMES_FILE = f'{LOGS}/apex-outcomes.json'
TACO_OUT_FILE = f'{LOGS}/apex-taco-outcomes.json'
STATE_FILE    = f'{LOGS}/apex-taco-state.json'
LOG_FILE      = f'{LOGS}/apex-taco-log.json'
# ─────────────────────────────────────────────────────────────────────────────


def load_config():
    """Load TACO config with defaults."""
    return safe_read(CONFIG_FILE, {})


def load_taco_trades():
    """Load all TACO_CONTRARIAN trades from apex-outcomes.json."""
    data = safe_read(OUTCOMES_FILE, {"trades": []})
    all_trades = data.get('trades', [])
    return [t for t in all_trades if t.get('signal_type') == 'TACO_CONTRARIAN']


def is_within_30d(trade):
    """Return True if the trade closed within the last 30 calendar days."""
    closed_str = trade.get('closed', '')
    if not closed_str:
        return False
    try:
        closed_date = datetime.strptime(closed_str[:10], '%Y-%m-%d').date()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        return closed_date >= cutoff
    except Exception:
        return False


def compute_rolling_metrics(taco_trades):
    """Compute rolling 30-day TACO performance metrics."""
    recent = [t for t in taco_trades if is_within_30d(t)]
    count_30d = len(recent)

    if count_30d == 0:
        return {
            "count_30d": 0, "win_rate": 0.0, "avg_r": 0.0,
            "avg_recovery_magnitude_pct": 0.0, "avg_time_to_walkback_hours": 0.0
        }

    winners = [t for t in recent if t.get('pnl', 0) > 0]
    win_rate = len(winners) / count_30d
    avg_r    = sum(t.get('r_achieved', 0.0) for t in recent) / count_30d

    # Recovery magnitude: (exit - entry) / entry * 100 for each winner
    recovery_mags = []
    for t in winners:
        entry = t.get('entry', 0)
        exit_ = t.get('exit', 0)
        if entry and entry > 0 and exit_ > 0:
            recovery_mags.append((exit_ - entry) / entry * 100)
    avg_recovery_magnitude_pct = (
        round(sum(recovery_mags) / len(recovery_mags), 2) if recovery_mags else 0.0
    )

    # Time to walkback: read from taco-log by matching event_id
    walkback_hours = _compute_avg_walkback_time(recent)

    return {
        "count_30d":                   count_30d,
        "win_rate":                    round(win_rate, 4),
        "avg_r":                       round(avg_r, 4),
        "avg_recovery_magnitude_pct":  avg_recovery_magnitude_pct,
        "avg_time_to_walkback_hours":  walkback_hours,
    }


def _compute_avg_walkback_time(recent_trades):
    """Estimate avg hours from ARMED to WALKBACK via taco-log entries."""
    try:
        log_data = safe_read(LOG_FILE, [])
        if not isinstance(log_data, list):
            return 0.0

        # Build map: event_id → ARMED timestamp
        armed_times = {}
        walkback_times = {}
        for entry in log_data:
            eid = entry.get('event_id', '')
            if not eid:
                continue
            if entry.get('event') == 'STATE_TRANSITION' and entry.get('to') == 'ARMED':
                armed_times[eid] = entry.get('timestamp', '')
            if entry.get('event') == 'STATE_TRANSITION' and entry.get('to') == 'RECOVERY':
                walkback_times[eid] = entry.get('timestamp', '')

        deltas = []
        for trade in recent_trades:
            eid = trade.get('taco_event_id', '')
            if eid in armed_times and eid in walkback_times:
                try:
                    t_armed    = datetime.fromisoformat(armed_times[eid])
                    t_walkback = datetime.fromisoformat(walkback_times[eid])
                    if t_armed.tzinfo is None:
                        t_armed = t_armed.replace(tzinfo=timezone.utc)
                    if t_walkback.tzinfo is None:
                        t_walkback = t_walkback.replace(tzinfo=timezone.utc)
                    delta_h = (t_walkback - t_armed).total_seconds() / 3600
                    if delta_h > 0:
                        deltas.append(delta_h)
                except Exception:
                    pass

        return round(sum(deltas) / len(deltas), 1) if deltas else 0.0
    except Exception as e:
        log_error(f"TACO outcomes _compute_avg_walkback_time: {e}", exc=e)
        return 0.0


def check_diminishing_returns(taco_trades):
    """Return True if the last 3 closed TACO trades show strictly declining R."""
    sorted_trades = sorted(
        taco_trades,
        key=lambda t: t.get('closed', ''),
        reverse=True
    )
    last_3 = sorted_trades[:3]
    if len(last_3) < 3:
        return False
    r_vals = [t.get('r_achieved', 0.0) for t in last_3]
    # Strict decline: most recent < second most recent < third most recent
    return r_vals[0] < r_vals[1] < r_vals[2]


def check_exhausted(metrics, is_declining, config):
    """Determine if TACO strategy is EXHAUSTED based on rolling metrics."""
    out_cfg   = config.get('outcomes', {})
    max_count = out_cfg.get('exhausted_event_count_30d', 4)
    min_wr    = out_cfg.get('exhausted_win_rate_threshold', 0.55)
    return (
        metrics['count_30d'] >= max_count
        and is_declining
        and metrics['win_rate'] < min_wr
    )


def trading_days_between(from_date, to_date):
    """Count US weekday trading days between two dates (exclusive of endpoints)."""
    count   = 0
    current = from_date
    while current < to_date:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon–Fri
            count += 1
    return count


def check_exhausted_recovery(config):
    """Return True if 15+ trading days have passed since the last TACO event."""
    try:
        out_cfg        = config.get('outcomes', {})
        recovery_days  = out_cfg.get('exhausted_recovery_trading_days', 15)

        log_data = safe_read(LOG_FILE, [])
        if not isinstance(log_data, list) or not log_data:
            return False

        # Find most recent classified_at timestamp
        latest_ts = None
        for entry in reversed(log_data):
            ts_str = entry.get('classified_at', '') or entry.get('timestamp', '')
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    latest_ts = ts
                    break
                except Exception:
                    pass

        if latest_ts is None:
            return False

        today = datetime.now(timezone.utc).date()
        last_event_date = latest_ts.date()
        days_elapsed = trading_days_between(last_event_date, today)
        return days_elapsed >= recovery_days

    except Exception as e:
        log_error(f"TACO outcomes check_exhausted_recovery: {e}", exc=e)
        return False


def send_friday_summary(metrics, taco_trades):
    """Send weekly Telegram performance summary on Fridays."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 4:  # 4 = Friday
        return

    total_trades = len(taco_trades)
    count_30d    = metrics['count_30d']
    win_rate_pct = round(metrics['win_rate'] * 100, 1)
    avg_r        = metrics['avg_r']
    avg_rec      = metrics['avg_recovery_magnitude_pct']
    exhausted    = metrics.get('exhausted', False)

    trend = ""
    if len(taco_trades) >= 3:
        last_3_r = [t.get('r_achieved', 0.0) for t in sorted(
            taco_trades, key=lambda x: x.get('closed', ''), reverse=True
        )[:3]]
        if last_3_r[0] > last_3_r[2]:
            trend = "improving"
        elif last_3_r[0] < last_3_r[2]:
            trend = "declining"
        else:
            trend = "flat"

    status_line = "EXHAUSTED — reducing size" if exhausted else "ACTIVE"
    msg = (
        f"🌮 TACO WEEKLY SUMMARY\n\n"
        f"Status: {status_line}\n"
        f"Trades (30d): {count_30d} | All-time: {total_trades}\n"
        f"Win rate (30d): {win_rate_pct}%\n"
        f"Avg R (30d): {avg_r:.2f}\n"
        f"Avg recovery: {avg_rec:.1f}%\n"
        f"Trend: {trend}\n"
        f"Avg time to walkback: {metrics['avg_time_to_walkback_hours']:.0f}h"
    )
    send_telegram(msg)


def main():
    """Compute TACO rolling metrics, update exhausted flag, and send Friday summary."""
    try:
        if not safe_read(CONFIG_FILE, {}).get('enabled', True):
            log_info("TACO module disabled — skipping outcomes tracker")
            return

        config       = load_config()
        taco_trades  = load_taco_trades()
        metrics      = compute_rolling_metrics(taco_trades)
        is_declining = check_diminishing_returns(taco_trades)

        # Determine exhausted state
        new_exhausted = check_exhausted(metrics, is_declining, config)

        # Check if exhausted should reset due to cooldown
        if new_exhausted:
            pass  # Keep exhausted
        else:
            # If previously exhausted, check if cooldown has elapsed
            prev_outcomes = safe_read(TACO_OUT_FILE, {})
            if prev_outcomes.get('exhausted', False):
                if check_exhausted_recovery(config):
                    new_exhausted = False  # Reset
                    log_info("TACO outcomes: EXHAUSTED cooldown elapsed — resetting to active")
                    send_telegram("🌮 TACO EXHAUSTED RESET\n\nCooldown period elapsed. TACO edge considered refreshed.")
                else:
                    new_exhausted = True  # Maintain exhausted

        metrics['exhausted']   = new_exhausted
        metrics['is_declining'] = is_declining

        # Find last event date
        last_event_date = None
        try:
            log_data = safe_read(LOG_FILE, [])
            if isinstance(log_data, list) and log_data:
                for entry in reversed(log_data):
                    ts_str = entry.get('classified_at', '') or entry.get('timestamp', '')
                    if ts_str:
                        last_event_date = ts_str[:10]
                        break
        except Exception:
            pass

        # Compute trading days since last event
        days_since = 0
        if last_event_date:
            try:
                last_d = datetime.strptime(last_event_date, '%Y-%m-%d').date()
                today  = datetime.now(timezone.utc).date()
                days_since = trading_days_between(last_d, today)
            except Exception:
                pass

        output = {
            "computed_at":                    datetime.now(timezone.utc).isoformat(),
            "count_30d":                      metrics['count_30d'],
            "win_rate":                       metrics['win_rate'],
            "avg_r":                          metrics['avg_r'],
            "avg_recovery_magnitude_pct":     metrics['avg_recovery_magnitude_pct'],
            "avg_time_to_walkback_hours":     metrics['avg_time_to_walkback_hours'],
            "is_declining":                   is_declining,
            "exhausted":                      new_exhausted,
            "last_event_date":                last_event_date,
            "trading_days_since_last_event":  days_since,
        }
        atomic_write(TACO_OUT_FILE, output)

        # Sync exhausted flag into taco-state.json if it changed
        prev_state = safe_read(STATE_FILE, {})
        if prev_state.get('exhausted') != new_exhausted:
            prev_state['exhausted'] = new_exhausted
            prev_state['exhausted_updated_at'] = datetime.now(timezone.utc).isoformat()
            atomic_write(STATE_FILE, prev_state)
            verb = "EXHAUSTED" if new_exhausted else "ACTIVE"
            send_telegram(f"🌮 TACO STATUS UPDATE\n\nStrategy marked {verb} by outcomes tracker.\n"
                          f"30d trades: {metrics['count_30d']} | "
                          f"Win rate: {metrics['win_rate']:.0%} | "
                          f"Declining: {is_declining}")

        send_friday_summary(metrics, taco_trades)

        log_info(f"TACO outcomes: count_30d={metrics['count_30d']} "
                 f"win_rate={metrics['win_rate']:.1%} exhausted={new_exhausted}")

    except Exception as e:
        log_error(f"TACO outcomes tracker fatal: {e}", exc=e)
        sys.exit(0)  # Always exit 0 for cron health


if __name__ == "__main__":
    main()
