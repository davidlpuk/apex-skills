#!/usr/bin/env python3
"""
apex-trajectory-tracker.py
Record daily unrealised P&L snapshots for every open position.
When a trade closes (disappears from positions), archive its full trajectory
with the outcome.
"""
import json
import os
import sys
from datetime import datetime, timezone

LOGS = '/home/ubuntu/.picoclaw/logs'
STATE_FILE = os.path.join(LOGS, 'apex-trajectory-state.json')
POSITIONS_FILE = os.path.join(LOGS, 'apex-positions.json')
OUTCOMES_FILE = os.path.join(LOGS, 'apex-outcomes.json')


def safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default if default is not None else {}


def atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def classify_trajectory_shape(snapshots, final_r, days_held, outcome):
    """Classify the shape of a trade trajectory from its snapshots."""
    if not snapshots:
        return 'mixed'

    r_values = [s.get('r_current', 0) for s in snapshots]

    # stop_and_reverse: implied by stop-out outcome or r dropped below -0.9
    if outcome in ('STOP', 'STOPPED_OUT') or any(r < -0.9 for r in r_values):
        return 'stop_and_reverse'

    # instant_winner: r > 0.5 by snapshot index 2 (third snapshot, 0-indexed)
    if len(r_values) >= 3 and r_values[2] > 0.5:
        return 'instant_winner'
    elif len(r_values) < 3 and r_values[-1] > 0.5:
        return 'instant_winner'

    # v_recovery: dipped below -0.3 but ended positive
    if min(r_values) < -0.3 and final_r > 0:
        return 'v_recovery'

    # slow_grind: ended near flat and held a while
    if -0.3 <= final_r <= 0.3 and days_held >= 10:
        return 'slow_grind'

    # steady_climber: all snapshots positive and trending upward overall
    if all(r >= 0 for r in r_values) and len(r_values) >= 2 and r_values[-1] >= r_values[0]:
        return 'steady_climber'

    return 'mixed'


def classify_early_signal(snapshots):
    """Classify the early signal from the first snapshot."""
    if not snapshots:
        return 'neutral'
    first_r = snapshots[0].get('r_current', 0)
    if first_r < 0:
        return 'negative_day1'
    elif first_r > 0.3:
        return 'strong_start'
    return 'neutral'


def find_outcome(ticker, outcomes_trades):
    """Find the most recent matching outcome for a closed ticker."""
    matches = [t for t in outcomes_trades if t.get('ticker') == ticker]
    if not matches:
        return None
    # Return most recently closed
    def closed_key(t):
        return t.get('closed', '') or ''
    return sorted(matches, key=closed_key, reverse=True)[0]


def main():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # 1. Load existing state or initialise
    state = safe_read(STATE_FILE, default={
        'active_trajectories': {},
        'completed_trajectories': []
    })
    if 'active_trajectories' not in state:
        state['active_trajectories'] = {}
    if 'completed_trajectories' not in state:
        state['completed_trajectories'] = []

    # 2. Load positions and outcomes
    positions_raw = safe_read(POSITIONS_FILE, default=[])
    if isinstance(positions_raw, dict):
        positions = positions_raw.get('positions', [])
    else:
        positions = positions_raw if isinstance(positions_raw, list) else []

    outcomes_data = safe_read(OUTCOMES_FILE, default={'trades': [], 'summary': {}})
    outcomes_trades = outcomes_data.get('trades', []) if isinstance(outcomes_data, dict) else []

    active = state['active_trajectories']
    completed = state['completed_trajectories']

    # Track counters for CLI summary
    new_count = 0
    updated_count = 0
    closed_count = 0

    # Build set of current tickers
    current_tickers = set()
    for pos in positions:
        ticker = pos.get('t212_ticker')
        if not ticker:
            continue
        current_tickers.add(ticker)

    # 3. Process each current position
    for pos in positions:
        ticker = pos.get('t212_ticker')
        if not ticker:
            continue

        entry = pos.get('entry', 0) or 0
        stop = pos.get('stop', 0) or 0
        current = pos.get('current', entry) or entry
        opened_str = pos.get('opened', today)

        # Parse opened date for days_held
        try:
            opened_date = datetime.strptime(opened_str[:10], '%Y-%m-%d')
        except:
            opened_date = datetime.now(timezone.utc).replace(tzinfo=None)
        today_date = datetime.now(timezone.utc).replace(tzinfo=None)
        days_held = max(0, (today_date - opened_date).days)

        # Compute snapshot fields
        denominator = entry - stop
        if denominator != 0:
            r_current = (current - entry) / denominator
            stop_distance_pct = (current - stop) / denominator
        else:
            r_current = 0.0
            stop_distance_pct = 1.0

        mae = pos.get('mae_pct', 0.0) or 0.0
        mfe = pos.get('mfe_pct', 0.0) or 0.0
        edge_velocity = r_current / max(1, days_held)

        snapshot = {
            'date': today,
            'price': current,
            'r_current': round(r_current, 4),
            'mae': round(mae, 4),
            'mfe': round(mfe, 4),
            'days_held': days_held,
            'edge_velocity': round(edge_velocity, 6),
            'stop_distance_pct': round(stop_distance_pct, 4)
        }

        if ticker not in active:
            # New trajectory
            active[ticker] = {
                'ticker': ticker,
                'name': pos.get('name', ticker),
                'entry_date': opened_str,
                'entry_price': entry,
                'stop': stop,
                'target1': pos.get('target1'),
                'target2': pos.get('target2'),
                'signal_type': pos.get('signal_type', 'UNKNOWN'),
                'sector': pos.get('sector', 'UNKNOWN'),
                'score': pos.get('score'),
                'snapshots': []
            }
            new_count += 1

        traj = active[ticker]
        snapshots = traj.get('snapshots', [])

        # Deduplicate: don't add if last snapshot date == today
        if snapshots and snapshots[-1].get('date') == today:
            # Already have today's snapshot — skip (no update count increment)
            pass
        else:
            snapshots.append(snapshot)
            traj['snapshots'] = snapshots
            updated_count += 1

    # 4. Detect closed trades
    closed_tickers = [t for t in list(active.keys()) if t not in current_tickers]

    for ticker in closed_tickers:
        traj = active.pop(ticker)
        snapshots = traj.get('snapshots', [])

        # Try to match with outcomes
        matched_outcome = find_outcome(ticker, outcomes_trades)

        if matched_outcome:
            outcome_label = matched_outcome.get('result', matched_outcome.get('outcome_type', 'UNKNOWN'))
            final_r = matched_outcome.get('r_achieved', 0.0) or 0.0
            days_held = matched_outcome.get('days_held', 0) or 0
            mae_peak = matched_outcome.get('mae_pct', 0.0) or 0.0
            mfe_peak = matched_outcome.get('mfe_pct', 0.0) or 0.0
            name = matched_outcome.get('name', traj.get('name', ticker))
        else:
            outcome_label = 'UNKNOWN'
            # Use last snapshot for final values
            if snapshots:
                last = snapshots[-1]
                final_r = last.get('r_current', 0.0)
                days_held = last.get('days_held', 0)
                mae_peak = last.get('mae', 0.0)
                mfe_peak = last.get('mfe', 0.0)
            else:
                final_r = 0.0
                days_held = 0
                mae_peak = 0.0
                mfe_peak = 0.0
            name = traj.get('name', ticker)

        # Compute edge_velocity_avg from snapshots
        ev_values = [s.get('edge_velocity', 0) for s in snapshots if s.get('edge_velocity') is not None]
        edge_velocity_avg = round(sum(ev_values) / len(ev_values), 6) if ev_values else 0.0

        trajectory_shape = classify_trajectory_shape(snapshots, final_r, days_held, outcome_label)
        early_signal = classify_early_signal(snapshots)

        completed_entry = {
            'ticker': ticker,
            'name': name,
            'signal_type': traj.get('signal_type', 'UNKNOWN'),
            'sector': traj.get('sector', 'UNKNOWN'),
            'entry_date': traj.get('entry_date'),
            'entry_price': traj.get('entry_price'),
            'stop': traj.get('stop'),
            'target1': traj.get('target1'),
            'target2': traj.get('target2'),
            'score': traj.get('score'),
            'outcome': outcome_label,
            'final_r': round(final_r, 4),
            'days_held': days_held,
            'trajectory_shape': trajectory_shape,
            'early_signal': early_signal,
            'mae_peak': round(mae_peak, 4),
            'mfe_peak': round(mfe_peak, 4),
            'edge_velocity_avg': edge_velocity_avg,
            'closed_date': today,
            'snapshots': snapshots
        }

        completed.append(completed_entry)
        closed_count += 1

    # 7. Cap completed_trajectories at 200 (FIFO — remove oldest)
    if len(completed) > 200:
        completed = completed[-200:]

    state['active_trajectories'] = active
    state['completed_trajectories'] = completed
    state['last_updated'] = today

    # 8. Write state atomically
    atomic_write(STATE_FILE, state)

    # CLI summary
    print("Apex Trajectory Tracker")
    print(f"  Active: {len(active)} positions tracked")
    print(f"  New: {new_count} | Updated: {updated_count} | Closed: {closed_count}")
    print(f"  Completed archive: {len(completed)} trajectories")


if __name__ == '__main__':
    main()
