#!/usr/bin/env python3
"""
Apex Trajectory Learner
Analyzes completed trade trajectories to learn:
- Early cut signals: if r < -0.3R by day 2, should we exit early?
- T2 runner signals: if edge_velocity high at midpoint, let winners run?
"""
import json
import os
import sys
import math
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_info
except ImportError:
    def atomic_write(p, d):
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, p)

    def safe_read(p, d=None):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return d if d is not None else {}

    def log_info(m):
        print(f'INFO: {m}')

LOGS = '/home/ubuntu/.picoclaw/logs'
TRAJECTORY_FILE = f'{LOGS}/apex-trajectory-state.json'
INSIGHTS_FILE   = f'{LOGS}/apex-trajectory-insights.json'
MIN_TRAJECTORIES = 10

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value, default=0.0):
    """Return float, falling back to default on None / non-numeric."""
    if value is None:
        return default
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _snapshots(traj):
    """Return the snapshots list for a trajectory, always a list."""
    return traj.get('snapshots') or []


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def analyse_early_cut(completed):
    """
    For trades with >= 3 snapshots, identify those where r_current < -0.3
    within the first 2 snapshots. Measure what fraction of those recovered
    (final_r > 0).

    Returns dict with fields:
        recommended, threshold_r, day, recovery_rate, sample_size, note
    """
    THRESHOLD = -0.3
    went_negative_early = []

    for traj in completed:
        snaps = _snapshots(traj)
        if len(snaps) < 3:
            continue
        # Check first 2 snapshots for a dip below threshold
        early_snaps = snaps[:2]
        if any(_safe_float(s.get('r_current')) < THRESHOLD for s in early_snaps):
            final_r = _safe_float(traj.get('final_r'))
            went_negative_early.append(final_r)

    sample_size = len(went_negative_early)
    if sample_size == 0:
        return {
            'recommended': False,
            'threshold_r': THRESHOLD,
            'day': 2,
            'recovery_rate': None,
            'sample_size': 0,
            'note': 'No trades went below threshold in first 2 days — cannot evaluate',
        }

    recovered = sum(1 for r in went_negative_early if r > 0)
    recovery_rate = round(recovered / sample_size, 4)
    recommended = recovery_rate < 0.30

    if recommended:
        note = (f'{recovery_rate*100:.0f}% of early-negative trades recovered — '
                f'early cut IS recommended (recovery rate < 30%)')
    else:
        note = (f'{recovery_rate*100:.0f}% of early-negative trades recovered — '
                f'early cut NOT recommended')

    return {
        'recommended': recommended,
        'threshold_r': THRESHOLD,
        'day': 2,
        'recovery_rate': recovery_rate,
        'sample_size': sample_size,
        'note': note,
    }


def analyse_t2_runner(completed):
    """
    For trades with >= 4 snapshots, check whether edge_velocity > 0.2 at the
    midpoint snapshot. Of those, what fraction reached T2 (outcome contains
    't2', 'target2', 'runner', or final_r is meaningfully positive vs. T1
    proxy ~1.0)?

    Returns dict with fields:
        recommended, velocity_threshold, midpoint_velocity_t2_rate,
        sample_size, partial_fraction_override, note
    """
    VELOCITY_THRESHOLD = 0.2
    # Proxy: consider T2 reached if final_r > 1.0 OR outcome string hints at it
    T2_OUTCOMES = {'t2', 'target2', 'runner', 'held', 'extended'}

    high_velocity = []

    for traj in completed:
        snaps = _snapshots(traj)
        if len(snaps) < 4:
            continue
        midpoint_idx = len(snaps) // 2
        mid_snap = snaps[midpoint_idx]
        ev = _safe_float(mid_snap.get('edge_velocity'))
        if ev > VELOCITY_THRESHOLD:
            # Determine if T2 was reached
            final_r = _safe_float(traj.get('final_r'))
            outcome = str(traj.get('outcome', '')).lower()
            traj_shape = str(traj.get('trajectory_shape', '')).lower()
            reached_t2 = (
                final_r > 1.0
                or any(kw in outcome for kw in T2_OUTCOMES)
                or any(kw in traj_shape for kw in T2_OUTCOMES)
            )
            high_velocity.append(reached_t2)

    sample_size = len(high_velocity)
    if sample_size == 0:
        return {
            'recommended': False,
            'velocity_threshold': VELOCITY_THRESHOLD,
            'midpoint_velocity_t2_rate': None,
            'sample_size': 0,
            'partial_fraction_override': 0.50,
            'note': 'No high-velocity midpoint trades found — cannot evaluate',
        }

    t2_count = sum(1 for v in high_velocity if v)
    t2_rate = round(t2_count / sample_size, 4)
    recommended = t2_rate > 0.60
    partial_override = 0.33 if recommended else 0.50  # smaller T1 close if confirmed

    if recommended:
        note = (f'{t2_rate*100:.0f}% of high-velocity trades reached T2 — '
                f'T2 runner IS confirmed (>60%); use partial_fraction_override={partial_override}')
    else:
        note = (f'{t2_rate*100:.0f}% of high-velocity trades reached T2 — '
                f'T2 runner NOT confirmed (need >60%)')

    return {
        'recommended': recommended,
        'velocity_threshold': VELOCITY_THRESHOLD,
        'midpoint_velocity_t2_rate': t2_rate,
        'sample_size': sample_size,
        'partial_fraction_override': partial_override,
        'note': note,
    }


def analyse_shape_distribution(completed):
    """Count trajectory_shape occurrences across all completed trades."""
    known_shapes = {
        'instant_winner', 'v_recovery', 'slow_grind',
        'stop_and_reverse', 'steady_climber', 'mixed',
    }
    counts = defaultdict(int)
    for traj in completed:
        shape = str(traj.get('trajectory_shape', 'mixed')).lower()
        if shape not in known_shapes:
            shape = 'mixed'
        counts[shape] += 1

    return {
        'instant_winner':   counts.get('instant_winner', 0),
        'v_recovery':       counts.get('v_recovery', 0),
        'slow_grind':       counts.get('slow_grind', 0),
        'stop_and_reverse': counts.get('stop_and_reverse', 0),
        'steady_climber':   counts.get('steady_climber', 0),
        'mixed':            counts.get('mixed', 0),
    }


def analyse_by_signal_type(completed):
    """
    Group completed trajectories by signal_type and compute:
    n, win_rate, avg_r, avg_days
    """
    groups = defaultdict(list)
    for traj in completed:
        sig = str(traj.get('signal_type', 'UNKNOWN')).upper()
        groups[sig].append(traj)

    result = {}
    for sig_type, trajs in sorted(groups.items()):
        n = len(trajs)
        final_rs = [_safe_float(t.get('final_r')) for t in trajs]
        days_held = [_safe_float(t.get('days_held'), default=0.0) for t in trajs]
        wins = sum(1 for r in final_rs if r > 0)
        win_rate = round(wins / n, 4) if n else 0.0
        avg_r = round(sum(final_rs) / n, 4) if n else 0.0
        avg_days = round(sum(days_held) / n, 1) if n else 0.0
        avg_mae = round(
            sum(_safe_float(t.get('mae_peak')) for t in trajs) / n, 4
        ) if n else 0.0
        avg_mfe = round(
            sum(_safe_float(t.get('mfe_peak')) for t in trajs) / n, 4
        ) if n else 0.0
        result[sig_type] = {
            'n':         n,
            'win_rate':  win_rate,
            'avg_r':     avg_r,
            'avg_days':  avg_days,
            'avg_mae_peak': avg_mae,
            'avg_mfe_peak': avg_mfe,
        }
    return result


def analyse_day1_direction(completed):
    """
    For completed trajectories, check early_signal and final outcome.
    - What fraction where early_signal == 'negative_day1' ultimately lost (final_r <= 0)?
    - What fraction where early_signal == 'positive_day1' ultimately won (final_r > 0)?
    """
    neg_day1 = [t for t in completed if str(t.get('early_signal', '')).lower() == 'negative_day1']
    pos_day1 = [t for t in completed if str(t.get('early_signal', '')).lower() == 'positive_day1']

    neg_loss_rate = None
    pos_win_rate  = None

    if neg_day1:
        losses = sum(1 for t in neg_day1 if _safe_float(t.get('final_r')) <= 0)
        neg_loss_rate = round(losses / len(neg_day1), 4)

    if pos_day1:
        wins = sum(1 for t in pos_day1 if _safe_float(t.get('final_r')) > 0)
        pos_win_rate = round(wins / len(pos_day1), 4)

    sample_size = len(neg_day1) + len(pos_day1)

    if neg_loss_rate is not None and pos_win_rate is not None:
        if neg_loss_rate >= 0.55 and pos_win_rate >= 0.55:
            note = 'Day-1 direction has strong predictive value'
        elif neg_loss_rate >= 0.45 or pos_win_rate >= 0.45:
            note = 'Day-1 direction has moderate predictive value'
        else:
            note = 'Day-1 direction has weak predictive value'
    elif neg_loss_rate is not None:
        note = 'Positive day-1 signal data missing'
    elif pos_win_rate is not None:
        note = 'Negative day-1 signal data missing'
    else:
        note = 'No day-1 signal labels found in trajectories'

    return {
        'negative_day1_loss_rate': neg_loss_rate,
        'positive_day1_win_rate':  pos_win_rate,
        'sample_size':             sample_size,
        'note':                    note,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    today   = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    state = safe_read(TRAJECTORY_FILE, {})
    completed  = state.get('completed_trajectories') or []
    active     = state.get('active_trajectories') or {}

    n_completed = len(completed)
    n_active    = len(active)

    print(f'🧬 APEX TRAJECTORY LEARNER — {today}')
    print(f'  Completed trajectories: {n_completed} (need {MIN_TRAJECTORIES} to activate)')

    if n_completed < MIN_TRAJECTORIES:
        insights = {
            'version':        1,
            'generated':      now_str,
            'n_trajectories': n_completed,
            'status':         'INSUFFICIENT_DATA',
            'note':           (
                f'Only {n_completed} completed trajectories recorded; '
                f'need at least {MIN_TRAJECTORIES} to generate reliable insights.'
            ),
            'early_cut':      None,
            't2_runner':      None,
            'shape_distribution': None,
            'day1_direction_accuracy': None,
            'by_signal_type': None,
        }
        atomic_write(INSIGHTS_FILE, insights)
        print(f'  Status: INSUFFICIENT_DATA')
        print(f'')
        print(f'  Active positions tracked: {n_active}')
        print(f'  Insights saved to apex-trajectory-insights.json')
        return

    # --- Run all analyses ---
    early_cut      = analyse_early_cut(completed)
    t2_runner      = analyse_t2_runner(completed)
    shape_dist     = analyse_shape_distribution(completed)
    by_signal      = analyse_by_signal_type(completed)
    day1_accuracy  = analyse_day1_direction(completed)

    insights = {
        'version':        1,
        'generated':      now_str,
        'n_trajectories': n_completed,
        'status':         'OK',

        'early_cut':       early_cut,
        't2_runner':       t2_runner,
        'shape_distribution': shape_dist,
        'day1_direction_accuracy': day1_accuracy,
        'by_signal_type':  by_signal,
    }

    atomic_write(INSIGHTS_FILE, insights)

    # --- CLI summary ---
    print(f'  Completed trajectories: {n_completed}')
    print()

    # Early cut
    ec = early_cut
    rr = f"{ec['recovery_rate']*100:.0f}%" if ec['recovery_rate'] is not None else 'N/A'
    rec_str = 'RECOMMENDED' if ec['recommended'] else 'NOT recommended'
    print(f'  Early cut analysis:')
    print(f'    Negative by day 2: {ec["sample_size"]} trades | Recovery rate: {rr} → {rec_str}')

    # T2 runner
    t2 = t2_runner
    tr = f"{t2['midpoint_velocity_t2_rate']*100:.0f}%" if t2['midpoint_velocity_t2_rate'] is not None else 'N/A'
    t2_rec_str = 'CONFIRMED' if t2['recommended'] else 'NOT confirmed'
    print()
    print(f'  T2 runner analysis:')
    print(f'    High velocity at midpoint: {t2["sample_size"]} trades | T2 rate: {tr} → {t2_rec_str}')

    # Shape distribution
    sd = shape_dist
    other = sd.get('steady_climber', 0) + sd.get('mixed', 0)
    print()
    print(f'  Shape distribution:')
    print(
        f'    instant_winner={sd["instant_winner"]}  '
        f'v_recovery={sd["v_recovery"]}  '
        f'slow_grind={sd["slow_grind"]}  '
        f'stop_and_reverse={sd["stop_and_reverse"]}  '
        f'other={other}'
    )

    # Day-1 accuracy
    d1 = day1_accuracy
    neg_pct = f"{d1['negative_day1_loss_rate']*100:.0f}%" if d1['negative_day1_loss_rate'] is not None else 'N/A'
    pos_pct = f"{d1['positive_day1_win_rate']*100:.0f}%"  if d1['positive_day1_win_rate']  is not None else 'N/A'
    print()
    print(f'  Day-1 accuracy: negative day1 → loss {neg_pct} | positive day1 → win {pos_pct}')

    print()
    print(f'  ✅ Insights saved to apex-trajectory-insights.json')


if __name__ == '__main__':
    main()
