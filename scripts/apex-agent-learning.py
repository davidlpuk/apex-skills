#!/usr/bin/env python3
"""
apex-agent-learning.py
Calculates the agent's track record by comparing actions to outcomes.

Reads:  apex-agent-actions.json (what the agent did)
        apex-outcomes.json       (what actually happened)
        apex-positions.json      (current open positions)
Writes: apex-agent-track-record.json (agent's performance metrics)

Run after post-trade-autopsy or on-demand. The track record is injected into
the agent's system prompt so it can calibrate its confidence over time.

Usage:
    python3 apex-agent-learning.py
"""
import json
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_utils import atomic_write, safe_read, locked_read_modify_write

LOG_DIR         = '/home/ubuntu/.picoclaw/logs'
ACTIONS_FILE    = f'{LOG_DIR}/apex-agent-actions.json'
OUTCOMES_FILE   = f'{LOG_DIR}/apex-outcomes.json'
POSITIONS_FILE  = f'{LOG_DIR}/apex-positions.json'
TRACK_FILE      = f'{LOG_DIR}/apex-agent-track-record.json'
LOG_FILE        = f'{LOG_DIR}/apex-agent-learning.log'

logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)


def _load_actions():
    data = safe_read(ACTIONS_FILE, [])
    return data if isinstance(data, list) else []


def _load_outcomes():
    data = safe_read(OUTCOMES_FILE, {})
    if isinstance(data, dict):
        return data.get('trades', [])
    return data if isinstance(data, list) else []


def _load_positions():
    data = safe_read(POSITIONS_FILE, [])
    return data if isinstance(data, list) else []


def evaluate_stop_tightens(actions, outcomes, positions):
    """Evaluate stop-tightening actions against outcomes."""
    tightens = [a for a in actions if a.get('action_type') == 'stop_tightened']
    if not tightens:
        return {'count': 0, 'accuracy': 'no_data'}

    # Build outcome lookup: ticker -> most recent closed trade
    outcome_by_ticker = {}
    for t in outcomes:
        ticker = t.get('ticker', '')
        outcome_by_ticker[ticker] = t

    # Build current position lookup
    pos_by_ticker = {}
    for p in positions:
        pos_by_ticker[p.get('t212_ticker', '')] = p

    beneficial = 0
    premature = 0
    unknown = 0

    for action in tightens:
        ticker = action.get('ticker', '')
        details = action.get('details', '')

        # Parse old/new stop from details "Stop X -> Y. reason"
        old_stop = None
        new_stop = None
        try:
            parts = details.split('->')[0].split()
            old_stop = float(parts[-1])
            new_stop = float(details.split('->')[1].split('.')[0].strip())
        except (IndexError, ValueError):
            pass

        # Check if position has closed since the action
        outcome = outcome_by_ticker.get(ticker)
        current_pos = pos_by_ticker.get(ticker)

        if outcome and outcome.get('closed', '') >= action.get('timestamp', '')[:10]:
            # Position closed after our action
            exit_price = outcome.get('exit', 0)
            if old_stop and new_stop:
                if exit_price >= new_stop:
                    # Exited above our tightened stop — we didn't cause the exit
                    # Was it beneficial? Did the stock fall below old stop later?
                    # We can't know the counterfactual perfectly, but if exit was
                    # at or near new_stop, our tightening triggered it
                    if abs(exit_price - new_stop) / new_stop < 0.005:
                        # Exit was at our tightened stop level
                        # Check if it fell further — then we saved money
                        mae = outcome.get('mae_pct', 0)
                        if mae and float(mae) < -2:
                            beneficial += 1  # It fell further, we saved losses
                        else:
                            premature += 1  # It recovered, we got shaken out
                    else:
                        beneficial += 1  # Hit target, our tightening didn't cause exit
                else:
                    # Exited below our new stop — something else caused exit
                    unknown += 1
            else:
                unknown += 1
        elif current_pos:
            # Position still open — action is still in play
            unknown += 1
        else:
            unknown += 1

    total = beneficial + premature + unknown
    accuracy = round(beneficial / max(beneficial + premature, 1), 2)

    return {
        'count': len(tightens),
        'beneficial': beneficial,
        'premature_exits': premature,
        'unknown': unknown,
        'accuracy': accuracy if (beneficial + premature) > 0 else 'insufficient_data',
    }


def evaluate_signal_reviews(actions, outcomes):
    """Evaluate signal veto/approve decisions against outcomes."""
    vetoes = [a for a in actions if a.get('action_type') == 'signal_vetoed']
    approvals = [a for a in actions if a.get('action_type') == 'signal_approved']

    # For vetoes: check if the signal would have been a winner or loser
    # We can't perfectly know because vetoed signals weren't traded,
    # but we can check price movement after the veto
    veto_results = {'count': len(vetoes), 'correct': 0, 'incorrect': 0, 'unknown': 0}
    for v in vetoes:
        # Future enhancement: track price of vetoed ticker over next N days
        veto_results['unknown'] += 1

    approval_results = {'count': len(approvals), 'winners': 0, 'losers': 0, 'unknown': 0}
    outcome_by_ticker = {}
    for t in outcomes:
        ticker = t.get('ticker', '')
        outcome_by_ticker[ticker] = t

    for a in approvals:
        ticker = a.get('ticker', '')
        outcome = outcome_by_ticker.get(ticker)
        if outcome and outcome.get('closed', '') >= a.get('timestamp', '')[:10]:
            pnl = outcome.get('pnl', 0)
            if pnl > 0:
                approval_results['winners'] += 1
            elif pnl < 0:
                approval_results['losers'] += 1
            else:
                approval_results['unknown'] += 1
        else:
            approval_results['unknown'] += 1

    return {
        'vetoes': veto_results,
        'approvals': approval_results,
    }


def calculate_track_record():
    """Build the full track record."""
    actions = _load_actions()
    outcomes = _load_outcomes()
    positions = _load_positions()

    if not actions:
        track = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'total_actions': 0,
            'by_type': {},
            'pnl_impact': 'No actions yet',
            'lesson': 'Take actions to build a track record.',
        }
        atomic_write(TRACK_FILE, track)
        return track

    stop_eval = evaluate_stop_tightens(actions, outcomes, positions)
    review_eval = evaluate_signal_reviews(actions, outcomes)

    # Count by type
    by_type = {}
    for a in actions:
        atype = a.get('action_type', 'unknown')
        if atype not in by_type:
            by_type[atype] = {'count': 0, 'avg_confidence': 0, 'confidences': []}
        by_type[atype]['count'] += 1
        conf = a.get('confidence', 0.5)
        by_type[atype]['confidences'].append(conf)

    for atype, stats in by_type.items():
        confs = stats.pop('confidences')
        stats['avg_confidence'] = round(sum(confs) / len(confs), 2) if confs else 0.5

    # Add evaluation results
    if 'stop_tightened' in by_type:
        by_type['stop_tightened']['accuracy'] = stop_eval.get('accuracy', 'unknown')
        by_type['stop_tightened']['beneficial'] = stop_eval.get('beneficial', 0)
        by_type['stop_tightened']['premature_exits'] = stop_eval.get('premature_exits', 0)

    if 'signal_vetoed' in by_type:
        by_type['signal_vetoed']['correct'] = review_eval['vetoes'].get('correct', 0)
        by_type['signal_vetoed']['incorrect'] = review_eval['vetoes'].get('incorrect', 0)

    if 'signal_approved' in by_type:
        by_type['signal_approved']['winners'] = review_eval['approvals'].get('winners', 0)
        by_type['signal_approved']['losers'] = review_eval['approvals'].get('losers', 0)

    # Generate lesson
    lesson = _generate_lesson(by_type, stop_eval, review_eval)

    track = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total_actions': len(actions),
        'by_type': by_type,
        'stop_evaluation': stop_eval,
        'review_evaluation': review_eval,
        'pnl_impact': 'Accumulating data — check after 10+ closed trades with agent actions',
        'lesson': lesson,
    }

    atomic_write(TRACK_FILE, track)
    log.info(f"Track record updated: {len(actions)} actions, lesson: {lesson}")
    return track


def _generate_lesson(by_type, stop_eval, review_eval):
    """Generate the most important lesson from the track record."""
    lessons = []

    # Stop tightening lessons
    if stop_eval.get('premature_exits', 0) > stop_eval.get('beneficial', 0):
        lessons.append(
            'Stop tightening is causing premature exits more often than saving profits. '
            'Be more conservative — only tighten when reversal from high exceeds 3%.'
        )
    elif stop_eval.get('beneficial', 0) > 2:
        lessons.append(
            f"Stop tightening is working — {stop_eval['beneficial']} beneficial actions. "
            'Continue protecting gains when momentum fades.'
        )

    # Veto lessons
    veto_count = by_type.get('signal_vetoed', {}).get('count', 0)
    approve_count = by_type.get('signal_approved', {}).get('count', 0)
    if veto_count > 0 and approve_count > 0:
        veto_rate = veto_count / (veto_count + approve_count)
        if veto_rate > 0.8:
            lessons.append(
                f'Veto rate is {veto_rate:.0%} — you may be too conservative. '
                'Consider approving more signals to build sample size.'
            )
        elif veto_rate < 0.2:
            lessons.append(
                f'Veto rate is {veto_rate:.0%} — you may be too permissive. '
                'Edge proof shows most signal types are NOT_PROVEN.'
            )

    if not lessons:
        return 'Insufficient data for lessons. Keep acting and logging.'

    return ' | '.join(lessons)


if __name__ == '__main__':
    result = calculate_track_record()
    print(json.dumps(result, indent=2))
