#!/usr/bin/env python3
"""
Trevor — APEX Investment Partner
Dry, analytical advisor personality that explains decisions and proactively improves portfolio.
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def _load(filename, default=None):
    """Load JSON from logs directory. Returns default ({} if not specified) on any failure."""
    path = f'/home/ubuntu/.picoclaw/logs/{filename}'
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning('Trevor: missing data file %s', filename)
        return default if default is not None else {}
    except json.JSONDecodeError as e:
        logger.error('Trevor: corrupt JSON in %s — %s', filename, e)
        return default if default is not None else {}
    except OSError as e:
        logger.warning('Trevor: cannot read %s — %s', filename, e)
        return default if default is not None else {}

def _safe_get(d, *keys, default=None):
    """Safe nested dict access. Returns default if any key is missing or value is None."""
    if d is None:
        return default
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key)
        else:
            return default
    return d if d is not None else default

# ─────────────────────────────────────────────────────────────────────
# Signal Explainer — Trevor's take on pending signals
# ─────────────────────────────────────────────────────────────────────

def explain_signal(pending_signal=None, portfolio_data=None, regime_data=None):
    """
    Generate Trevor's explanation of a pending signal.
    Returns dict with conviction, EV, risks, thesis, recommendations.
    """
    if pending_signal is None:
        pending_signal = _load('apex-pending-signal.json')
    if portfolio_data is None:
        portfolio_data = _load('apex-positions.json')
    if regime_data is None:
        regime_data = _load('apex-regime.json')

    if not pending_signal or not pending_signal.get('name'):
        return {'ok': False, 'message': 'No pending signal'}

    sig = pending_signal
    ticker = sig.get('name', '?')

    # Extract signal properties
    conviction = sig.get('conviction', 5)
    ev = sig.get('ev_pct', 0)
    score = sig.get('adjusted_score', 0)
    kelly_size = sig.get('kelly_size', 0)
    entry = sig.get('entry_price', 0)
    stop = sig.get('stop_price', 0)
    target1 = sig.get('target1', 0)
    signal_type = sig.get('signal_type', 'unknown')

    # Calculate risk/reward
    risk = abs(entry - stop) if entry and stop else 0
    reward = abs(target1 - entry) if entry and target1 else 0
    rr_ratio = reward / risk if risk > 0 else 0

    # Regime context
    regime = _safe_get(regime_data, 'regime', 'unknown')
    regime_conf = _safe_get(regime_data, 'confidence', 0)

    # Portfolio context
    current_positions = _safe_get(portfolio_data, 'positions') or []
    total_portfolio = sum(_safe_get(p, 'value', 0) or 0 for p in current_positions)

    # Check for correlation risk
    same_sector_exposure = 0
    if total_portfolio > 0:
        ticker_lower = ticker.lower()
        for pos in current_positions:
            if ticker_lower in _safe_get(pos, 'name', '').lower():
                same_sector_exposure += _safe_get(pos, 'value', 0)

    # Tax context
    tax_impact = "Long-term (no harvest conflict)"
    if sig.get('days_held', 365) < 365:
        tax_impact = "SHORT-TERM tax event"

    # Trevor's risk assessment (dry)
    risks = []
    if regime != 'risk-on':
        risks.append("Regime is not risk-on — signal might be headwind")
    if conviction < 6:
        risks.append("Conviction below 6 — I'm uncertain here")
    if rr_ratio < 1.5:
        risks.append("Risk/reward < 1.5:1 — limited margin of safety")
    if same_sector_exposure > total_portfolio * 0.2:
        risks.append("Sector concentration — redundancy risk")

    # Trevor's suggestion
    suggested_size = kelly_size * 0.85  # Conservative: 85% of Kelly
    size_note = "I'm suggesting 85% Kelly (conservative) due to regime uncertainty."
    if conviction > 7 and regime == 'risk-on':
        suggested_size = kelly_size
        size_note = "High conviction + regime fit. 100% Kelly justified."
    elif conviction < 5:
        suggested_size = kelly_size * 0.6
        size_note = "Low conviction. I'd cut this to 60% Kelly or skip it."

    return {
        'ok': True,
        'ticker': ticker,
        'signal_type': signal_type,
        'conviction': conviction,
        'ev_pct': ev,
        'confidence_pct': min(100, conviction * 12),  # Rough mapping
        'kelly_size': kelly_size,
        'suggested_size': suggested_size,
        'suggested_note': size_note,
        'entry_price': entry,
        'stop_price': stop,
        'target1': target1,
        'risk_per_trade': risk,
        'reward_per_trade': reward,
        'rr_ratio': round(rr_ratio, 2),
        'regime': regime,
        'regime_confidence': regime_conf,
        'tax_impact': tax_impact,
        'risks': risks,
        'alternatives': _get_alternatives(ticker, conviction),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

def _get_alternatives(ticker, conviction):
    """Suggest alternatives (simplified)."""
    alt_map = {
        'TSLA': [{'ticker': 'XLK', 'note': 'Sector ETF, lower volatility'},
                 {'ticker': 'MSTR', 'note': 'Pure tech, higher risk'}],
        'QQQ': [{'ticker': 'XLV', 'note': 'Defensive alternative'}],
    }
    return alt_map.get(ticker, [])

# ─────────────────────────────────────────────────────────────────────
# Portfolio Health Monitor
# ─────────────────────────────────────────────────────────────────────

def check_portfolio_health(portfolio_data=None, pending_signal=None):
    """
    Check for health issues: concentration, thesis decay, correlation, stops.
    Returns list of alerts (empty = healthy).
    """
    if portfolio_data is None:
        portfolio_data = _load('apex-positions.json')

    alerts = []

    # Check concentration
    positions = _safe_get(portfolio_data, 'positions', [])
    if not positions:
        return alerts

    total_value = sum(_safe_get(p, 'value', 0) for p in positions)
    if total_value == 0:
        return alerts

    growth_value = sum(_safe_get(p, 'value', 0) for p in positions
                       if 'tech' in _safe_get(p, 'sector', '').lower() or
                          'growth' in _safe_get(p, 'name', '').lower())
    growth_pct = (growth_value / total_value * 100) if total_value > 0 else 0

    if growth_pct > 75:
        alerts.append({
            'type': 'concentration',
            'severity': 'warn',
            'message': f'Growth bias at {growth_pct:.0f}% — portfolio is fragile to regime shift',
            'action': 'Gate growth signals, prioritize defensive trades',
        })

    # Check for correlation creep
    avg_correlation = _estimate_portfolio_correlation(positions)
    if avg_correlation > 0.80:
        alerts.append({
            'type': 'correlation',
            'severity': 'warn',
            'message': f'Average correlation {avg_correlation:.2f} — not diversified',
            'action': 'New signals should skew defensive. Consider trimming winners.',
        })

    # Check for stale positions (simplified)
    for pos in positions:
        entry_date = _safe_get(pos, 'entry_date')
        if entry_date:
            try:
                days_held = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_date)).days
                if days_held > 90 and _safe_get(pos, 'unrealised_pnl_pct', 0) > 20:
                    alerts.append({
                        'type': 'thesis_decay',
                        'severity': 'info',
                        'ticker': _safe_get(pos, 'name'),
                        'message': f'{_safe_get(pos, "name")} +{_safe_get(pos, "unrealised_pnl_pct", 0):.1f}% at 90 days — thesis decayed?',
                        'action': 'Consider trimming 50% to lock gains.',
                    })
            except (ValueError, TypeError, KeyError):
                pass

    return alerts

def _estimate_portfolio_correlation(positions):
    """Rough correlation estimate from sector overlap."""
    if len(positions) < 2:
        return 0
    # Simplified: count sector overlap
    sectors = [_safe_get(p, 'sector', 'unknown') for p in positions]
    sector_counts = {}
    for s in sectors:
        sector_counts[s] = sector_counts.get(s, 0) + 1
    concentration_score = sum((c / len(sectors)) ** 2 for c in sector_counts.values())
    return min(0.99, 0.5 + concentration_score * 0.49)

# ─────────────────────────────────────────────────────────────────────
# Morning Brief
# ─────────────────────────────────────────────────────────────────────

def morning_brief(portfolio_data=None, regime_data=None, pending_signal=None):
    """Trevor's 7am briefing."""
    if portfolio_data is None:
        portfolio_data = _load('apex-positions.json')
    if regime_data is None:
        regime_data = _load('apex-regime.json')
    if pending_signal is None:
        pending_signal = _load('apex-pending-signal.json')

    portfolio_value = _safe_get(portfolio_data, 'total_value') or _safe_get(portfolio_data, 'total') or 0
    portfolio_pnl = _safe_get(portfolio_data, 'total_pnl_pct') or _safe_get(portfolio_data, 'total_pnl') or 0
    regime = _safe_get(regime_data, 'regime') or _safe_get(regime_data, 'overall') or 'unknown'
    regime_conf = _safe_get(regime_data, 'confidence') or 0

    alerts = check_portfolio_health(portfolio_data, pending_signal)

    # Format the brief
    brief = {
        'greeting': f"Good morning. Here's what you need to know.",
        'macro': {
            'headline': f'Regime: {str(regime).upper()} ({regime_conf:.0f}% confidence)',
            'status': f'Portfolio: ${portfolio_value:,.0f} | {portfolio_pnl:+.2f}%',
        },
        'pending': {
            'count': 1 if pending_signal.get('name') else 0,
            'ticker': pending_signal.get('name', 'None'),
        },
        'alerts': [a['message'] for a in alerts[:2]],  # Top 2 alerts
        'watch': [],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    # Add watch items
    positions = _safe_get(portfolio_data, 'positions') or []
    for pos in positions[:3]:  # Top 3 by value
        brief['watch'].append({
            'ticker': _safe_get(pos, 'name'),
            'value': _safe_get(pos, 'value', 0),
            'pnl_pct': _safe_get(pos, 'unrealised_pnl_pct', 0),
        })

    return brief

# ─────────────────────────────────────────────────────────────────────
# EOD Wrap
# ─────────────────────────────────────────────────────────────────────

def eod_wrap(portfolio_data=None, outcomes_data=None):
    """Trevor's 4:30pm reflection."""
    if portfolio_data is None:
        portfolio_data = _load('apex-positions.json')
    if outcomes_data is None:
        outcomes_data = _load('apex-outcomes.json')

    portfolio_value = _safe_get(portfolio_data, 'total_value') or _safe_get(portfolio_data, 'total') or 0
    portfolio_pnl = _safe_get(portfolio_data, 'total_pnl_pct') or _safe_get(portfolio_data, 'total_pnl') or 0

    # Count wins/losses from recent closes (simplified)
    all_trades = _safe_get(outcomes_data, 'trades') or []
    recent_trades = all_trades[-5:]
    wins = sum(1 for t in recent_trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) > 0)
    losses = sum(1 for t in recent_trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) <= 0)

    wrap = {
        'greeting': f"Day wrap-up.",
        'portfolio': {
            'value': portfolio_value,
            'pnl_pct': portfolio_pnl,
        },
        'recent_trades': {
            'count': len(recent_trades),
            'wins': wins,
            'losses': losses,
            'win_rate': wins / len(recent_trades) * 100 if recent_trades else 0,
        },
        'themes': [],
        'next_actions': [],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    # Add themes (simplified)
    if portfolio_pnl > 1.0:
        wrap['themes'].append("Strong momentum. Stay long.")
    elif portfolio_pnl < -0.5:
        wrap['themes'].append("Drawdown alert. Review guards.")

    if wins > losses:
        wrap['next_actions'].append("Keep executing. Conviction is working.")
    else:
        wrap['next_actions'].append("Review signal quality. Conviction may need recalibration.")

    return wrap

# ─────────────────────────────────────────────────────────────────────
# Conviction Calibration
# ─────────────────────────────────────────────────────────────────────

def conviction_calibration(outcomes_data=None):
    """
    Analyze conviction accuracy: your stated conviction vs actual returns.
    Returns chart data for conviction vs return scatter.
    """
    if outcomes_data is None:
        outcomes_data = _load('apex-outcomes.json')

    trades = _safe_get(outcomes_data, 'trades', [])
    if not trades:
        return {'ok': False, 'message': 'No completed trades yet'}

    # Group trades by stated conviction
    conviction_buckets = {}
    for trade in trades:
        conv = _safe_get(trade, 'conviction', 5)
        bucket = int(conv)
        if bucket not in conviction_buckets:
            conviction_buckets[bucket] = {'returns': [], 'count': 0}
        pnl = _safe_get(trade, 'pnl_pct', 0)
        conviction_buckets[bucket]['returns'].append(pnl)
        conviction_buckets[bucket]['count'] += 1

    # Calculate accuracy per bucket
    calibration = []
    for conv in sorted(conviction_buckets.keys()):
        bucket = conviction_buckets[conv]
        avg_return = sum(bucket['returns']) / len(bucket['returns'])
        calibration.append({
            'conviction': conv,
            'actual_return': avg_return,
            'count': bucket['count'],
            'accuracy': 'high' if abs(avg_return) > conv * 0.3 else 'low',
        })

    # Trevor's assessment
    assessment = "Your conviction calibration is reasonable."
    if calibration:
        macro_accuracy = sum(1 for c in calibration if c['conviction'] >= 7 and c['actual_return'] > 0) / max(1, sum(1 for c in calibration if c['conviction'] >= 7)) * 100
        sector_accuracy = sum(1 for c in calibration if c['conviction'] < 6 and c['actual_return'] > 0) / max(1, sum(1 for c in calibration if c['conviction'] < 6)) * 100

        if macro_accuracy > 70 and sector_accuracy < 50:
            assessment = "You're strong on macro, weak on sectors. Trust macro more."
        elif macro_accuracy < 50 and sector_accuracy > 70:
            assessment = "Opposite: sectors are your edge, macro isn't."

    return {
        'ok': True,
        'calibration': calibration,
        'assessment': assessment,
        'trades_analyzed': len(trades),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────
# Weekly Postmortem Analysis
# ─────────────────────────────────────────────────────────────────────

def weekly_postmortem(outcomes_data=None, signal_log_path=None):
    """
    Analyze this week's performance: wins/losses, signal quality, skill breakdown.
    """
    if outcomes_data is None:
        outcomes_data = _load('apex-outcomes.json')

    trades = _safe_get(outcomes_data, 'trades') or []
    if not trades:
        return {'ok': False, 'message': 'No trades this week'}

    # Calculate stats
    total_trades = len(trades)
    wins = sum(1 for t in trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) > 0)
    losses = total_trades - wins
    total_pnl = sum(_safe_get(t, 'pnl_pct', 0) or 0 for t in trades if isinstance(t, dict))
    avg_win = sum(_safe_get(t, 'pnl_pct', 0) or 0 for t in trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) > 0) / max(1, wins) if wins > 0 else 0
    avg_loss = abs(sum(_safe_get(t, 'pnl_pct', 0) or 0 for t in trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) <= 0) / max(1, losses)) if losses > 0 else 0

    # Skill breakdown (simplified)
    signal_quality = 'good' if wins / max(1, total_trades) > 0.55 else 'needs work'
    timing_quality = 'early' if avg_loss > avg_win * 1.5 else 'good'

    return {
        'ok': True,
        'period': 'this_week',
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'win_rate': wins / max(1, total_trades) * 100,
        'total_pnl_pct': total_pnl,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': (sum(_safe_get(t, 'pnl_pct', 0) or 0 for t in trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) > 0)) / max(0.01, abs(sum(_safe_get(t, 'pnl_pct', 0) or 0 for t in trades if isinstance(t, dict) and (_safe_get(t, 'pnl_pct', 0) or 0) <= 0))) if losses > 0 else 0,
        'signal_quality': signal_quality,
        'timing_quality': timing_quality,
        'assessment': f"Win rate {wins}/{total_trades} ({wins/max(1,total_trades)*100:.0f}%). {signal_quality.capitalize()} signal selection, {timing_quality} timing.",
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────
# Override/Decline Pattern Analysis
# ─────────────────────────────────────────────────────────────────────

def analyze_override_patterns(log_path=None):
    """
    Analyze patterns in when user accepts vs declines signals.
    Returns insights about user's instincts.
    """
    if log_path is None:
        log_path = '/home/ubuntu/.picoclaw/logs/apex-trevor-signal-log.jsonl'

    actions = {'accepted': [], 'declined': [], 'waited': []}

    try:
        with open(log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    action = entry.get('action', 'unknown')
                    if action in actions:
                        actions[action].append(entry)
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
    except (FileNotFoundError, OSError):
        pass

    total = sum(len(v) for v in actions.values())
    if total == 0:
        return {'ok': False, 'message': 'No signal actions logged yet'}

    # Patterns
    patterns = []
    if actions['accepted'] and actions['declined']:
        patterns.append(f"You accept {len(actions['accepted'])} signals, decline {len(actions['declined'])} — selectivity: good")
    if actions['waited']:
        patterns.append(f"You pause {len(actions['waited'])} times to wait for clarity — patience: {len(actions['waited'])/max(1,total)*100:.0f}%")

    return {
        'ok': True,
        'accepted_count': len(actions['accepted']),
        'declined_count': len(actions['declined']),
        'waited_count': len(actions['waited']),
        'acceptance_rate': len(actions['accepted']) / max(1, total) * 100,
        'decline_reasons': [a.get('reason', '') for a in actions['declined'][:5]],
        'patterns': patterns,
        'recent_actions': (actions['accepted'] + actions['declined'])[-10:],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────
# Thesis Decay Detector
# ─────────────────────────────────────────────────────────────────────

def detect_thesis_decay(portfolio_data=None, outcomes_data=None):
    """
    Flag positions where original thesis is degrading.
    Returns alerts for positions held >60 days with conviction drops.
    """
    if portfolio_data is None:
        portfolio_data = _load('apex-positions.json')

    positions = _safe_get(portfolio_data, 'positions') or []
    if not positions:
        return {'ok': False, 'message': 'No positions'}

    alerts = []
    now = datetime.now(timezone.utc)

    for pos in (positions or []):
        if not isinstance(pos, dict):
            continue

        entry_date_str = _safe_get(pos, 'entry_date')
        if not entry_date_str:
            continue

        try:
            entry_date = datetime.fromisoformat(entry_date_str.replace('Z', '+00:00'))
            days_held = (now - entry_date).days
        except (ValueError, TypeError):
            continue

        unrealised_pnl_pct = _safe_get(pos, 'unrealised_pnl_pct', 0) or 0

        # Flag: held >60 days with >15% gain (thesis should be re-evaluated)
        if days_held > 60 and unrealised_pnl_pct > 15:
            alerts.append({
                'ticker': _safe_get(pos, 'name', '?'),
                'days_held': days_held,
                'gain_pct': unrealised_pnl_pct,
                'severity': 'info',
                'message': f"Held {days_held} days, +{unrealised_pnl_pct:.1f}% gain. Original thesis valid or degraded?",
                'action': 'Consider: lock gains (trim 50%), or increase if thesis strengthened',
            })

        # Flag: >25% gain (usually time to take profits)
        elif unrealised_pnl_pct > 25:
            alerts.append({
                'ticker': _safe_get(pos, 'name', '?'),
                'days_held': days_held,
                'gain_pct': unrealised_pnl_pct,
                'severity': 'warn',
                'message': f"+{unrealised_pnl_pct:.1f}% gain. Strong conviction or lock profits?",
                'action': 'Trim 30-50% to lock gains, let rest run',
            })

    return {
        'ok': True,
        'alerts': alerts,
        'position_count': len([p for p in (positions or []) if isinstance(p, dict)]),
        'thesis_decay_count': len(alerts),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────
# Main API — for dashboard integration
# ─────────────────────────────────────────────────────────────────────

def get_trevor_status():
    """Single endpoint for all Trevor data needed by dashboard."""
    portfolio_data = _load('apex-positions.json')
    regime_data = _load('apex-regime.json')
    pending_signal = _load('apex-pending-signal.json')
    outcomes_data = _load('apex-outcomes.json')

    return {
        'ok': True,
        'signal': explain_signal(pending_signal, portfolio_data, regime_data),
        'health_alerts': check_portfolio_health(portfolio_data, pending_signal),
        'morning_brief': morning_brief(portfolio_data, regime_data, pending_signal),
        'eod_wrap': eod_wrap(portfolio_data, outcomes_data),
        'conviction': conviction_calibration(outcomes_data),
        'thesis_decay': detect_thesis_decay(portfolio_data, outcomes_data),
        'postmortem': weekly_postmortem(outcomes_data),
        'overrides': analyze_override_patterns(),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

if __name__ == '__main__':
    import json
    data = get_trevor_status()
    print(json.dumps(data, indent=2, default=str))
