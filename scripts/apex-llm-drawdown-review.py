#!/usr/bin/env python3
"""
LLM Drawdown Review
Triggered when drawdown status reaches CAUTION, SUSPEND, or CRITICAL.

The mechanical circuit breaker (apex-circuit-breaker.py) handles the hard
rules: size reduction, halt, etc. This module adds qualitative judgment:

  "Is this drawdown from a strategy failure (pause/rethink) or from an
   unusual market event that has now passed (continue with caution)?"

Output written to apex-llm-drawdown-review.json.
Sends Telegram with assessment and recommendation.

Called from apex-drawdown-check.py when status changes to CAUTION or worse.
Also callable directly: python3 apex-llm-drawdown-review.py

Flag: drawdown_review_llm
"""
import json
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import atomic_write, safe_read, log_warning, log_info, send_telegram
    from apex_llm_flags import get_llm_flag, record_llm_call, call_llm_thinking
except ImportError as _e:
    print(f"FATAL: import failed: {_e}")
    sys.exit(1)

LOGS        = '/home/ubuntu/.picoclaw/logs'
REVIEW_FILE = f'{LOGS}/apex-llm-drawdown-review.json'


def _gather_drawdown_context() -> dict:
    ctx: dict = {}

    # Drawdown state
    draw = safe_read(f'{LOGS}/apex-drawdown.json', {})
    ctx['drawdown'] = {
        'status':     draw.get('status', 'NORMAL'),
        'pct':        draw.get('drawdown_pct', 0),
        'multiplier': draw.get('multiplier', 1.0),
        'session_start': draw.get('session_open_value', 0),
    }

    # Circuit breaker state
    cb = safe_read(f'{LOGS}/apex-circuit-breaker.json', {})
    ctx['circuit_breaker'] = {
        'status': cb.get('status', 'NORMAL'),
        'reason': cb.get('reason', ''),
    }

    # Rolling P&L context
    rolling = safe_read(f'{LOGS}/apex-rolling-pnl.json', {})
    ctx['rolling_pnl'] = {
        '1d':  rolling.get('day_1', 0),
        '3d':  rolling.get('day_3', 0),
        '5d':  rolling.get('day_5', 0),
        '10d': rolling.get('day_10', 0),
    }

    # Recent trade outcomes
    outcomes = safe_read(f'{LOGS}/apex-outcomes.json', {})
    trades   = outcomes.get('trades', [])
    recent   = trades[-10:] if trades else []
    ctx['recent_trades'] = []
    for t in recent:
        ctx['recent_trades'].append({
            'name':        t.get('name', '?'),
            'signal_type': t.get('signal_type', '?'),
            'pnl':         t.get('pnl', 0),
            'date':        (t.get('closed_at') or t.get('timestamp', ''))[:10],
            'exit_reason': t.get('exit_reason', '?'),
        })

    # Win/loss stats
    if recent:
        wins    = sum(1 for t in recent if (t.get('pnl', 0) or 0) > 0)
        losses  = len(recent) - wins
        total_pnl = sum(t.get('pnl', 0) or 0 for t in recent)
        ctx['recent_stats'] = {
            'last_n': len(recent),
            'wins': wins, 'losses': losses,
            'win_rate_pct': round(wins / len(recent) * 100, 1),
            'total_pnl': round(total_pnl, 2),
        }
    else:
        ctx['recent_stats'] = {}

    # Market context at time of drawdown
    regime  = safe_read(f'{LOGS}/apex-regime.json', {})
    scaling = safe_read(f'{LOGS}/apex-regime-scaling.json', {})
    ctx['regime'] = {
        'overall':    regime.get('overall', 'UNKNOWN'),
        'vix':        regime.get('vix', '?'),
        'breadth':    regime.get('breadth_pct', '?'),
        'hmm_state':  scaling.get('hmm_state', 'UNKNOWN'),
        'label':      scaling.get('regime_label', 'NEUTRAL'),
    }

    # Black swan / geo
    bs  = safe_read(f'{LOGS}/apex-blackswan.json', {})
    geo = safe_read(f'{LOGS}/apex-geo-news.json', {})
    ctx['market_events'] = {
        'blackswan':  bs.get('status', 'NORMAL'),
        'geo':        geo.get('overall', 'CLEAR'),
        'geo_flags':  [f.get('title', '') for f in geo.get('geo_flags', [])[:3]],
    }

    # Open positions (are they causing the drawdown or the market?)
    positions = safe_read(f'{LOGS}/apex-positions.json', [])
    if isinstance(positions, list):
        open_pos = [p for p in positions if p.get('status') in ('protected', 'entry_placed')]
        ctx['open_positions'] = [
            {'name': p.get('name', '?'), 'signal_type': p.get('signal_type', '?'),
             'sector': p.get('sector', '?'), 'pnl_est': p.get('current_pnl', 0)}
            for p in open_pos
        ]
    else:
        ctx['open_positions'] = []

    # Previous review (avoid repeating same assessment)
    prev = safe_read(REVIEW_FILE, {})
    ctx['previous_review'] = {
        'assessment': prev.get('assessment', ''),
        'timestamp':  prev.get('timestamp', ''),
    } if prev else {}

    return ctx


def run(trigger_status: str = None):
    """
    Run the drawdown review.

    Args:
        trigger_status: the drawdown status that triggered this call
                        (CAUTION / SUSPEND / CRITICAL)
                        If None, reads from apex-drawdown.json.
    """
    if not get_llm_flag('drawdown_review_llm'):
        log_info("Drawdown review: flag disabled — skipping")
        record_llm_call('drawdown_review_llm', used_llm=False, result_summary='flag_disabled')
        return

    ctx = _gather_drawdown_context()
    status = trigger_status or ctx['drawdown']['status']

    if status == 'NORMAL':
        log_info("Drawdown review: status is NORMAL — no review needed")
        return

    # Check if we already reviewed this drawdown episode recently (within 4h)
    prev = ctx.get('previous_review', {})
    if prev.get('timestamp'):
        try:
            prev_time = datetime.fromisoformat(prev['timestamp'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - prev_time).seconds < 4 * 3600:
                log_info("Drawdown review: reviewed within 4h — skipping duplicate")
                return
        except Exception:
            pass

    log_info(f"Drawdown review: running for status={status}")

    today = datetime.now(timezone.utc).strftime('%A %d %B %Y')
    draw  = ctx['drawdown']
    stats = ctx.get('recent_stats', {})
    events = ctx.get('market_events', {})
    regime = ctx.get('regime', {})

    trades_str = ''
    for t in ctx.get('recent_trades', []):
        icon = '✅' if (t['pnl'] or 0) > 0 else '❌'
        trades_str += (f"  {icon} {t['name']} ({t['signal_type']}) "
                       f"£{t['pnl']:+.2f} — exit: {t['exit_reason']} ({t['date']})\n")

    pos_str = ''
    for p in ctx.get('open_positions', []):
        pos_str += f"  {p['name']} ({p['signal_type']}, {p['sector']})\n"

    prompt = (
        f"You are a risk assessment advisor for an automated trading system.\n"
        f"Today is {today}. The system has hit a DRAWDOWN status of: {status}\n\n"
        f"DRAWDOWN DATA:\n"
        f"  Current drawdown: {draw.get('pct',0):.1f}%\n"
        f"  Status: {status} | Sizing multiplier: {int(draw.get('multiplier',1)*100)}%\n"
        f"  Circuit breaker: {ctx['circuit_breaker']['status']}\n\n"
        f"ROLLING P&L:\n"
        f"  Today: £{ctx['rolling_pnl'].get('1d',0):+.2f}\n"
        f"  3-day: £{ctx['rolling_pnl'].get('3d',0):+.2f}\n"
        f"  5-day: £{ctx['rolling_pnl'].get('5d',0):+.2f}\n"
        f"  10-day: £{ctx['rolling_pnl'].get('10d',0):+.2f}\n\n"
        f"RECENT TRADES (last {stats.get('last_n',0)}):\n"
        f"  Win rate: {stats.get('win_rate_pct','?')}% | "
        f"Wins: {stats.get('wins',0)} | Losses: {stats.get('losses',0)}\n"
        f"  Total P&L: £{stats.get('total_pnl',0):+.2f}\n"
        f"{trades_str or '  No recent trades\n'}"
        f"\nMARKET CONTEXT:\n"
        f"  VIX: {regime.get('vix','?')} | Breadth: {regime.get('breadth','?')}%\n"
        f"  Regime: {regime.get('label','?')} (HMM: {regime.get('hmm_state','?')})\n"
        f"  Black swan: {events.get('blackswan','NORMAL')} | Geo: {events.get('geo','CLEAR')}\n"
        f"  Geo alerts: {'; '.join(events.get('geo_flags',[]) or ['none'])}\n\n"
        f"OPEN POSITIONS:\n"
        f"{pos_str or '  None\n'}"
        f"\nYOUR ASSESSMENT TASK:\n"
        f"Determine the PRIMARY CAUSE of this drawdown and the appropriate response.\n\n"
        f"Assessment options:\n"
        f"  MARKET_EVENT — drawdown caused by a market-wide event (VIX spike, geo shock, "
        f"broad selloff). Strategy is intact. Resume when event passes.\n"
        f"  STRATEGY_VARIANCE — normal statistical variance. Win rate and expectancy intact. "
        f"Continue with reduced sizing as per current rules.\n"
        f"  STRATEGY_CONCERN — pattern of losses suggests a strategy issue. Stop new entries "
        f"until reviewed. Exit high-risk positions if possible.\n"
        f"  REGIME_MISMATCH — strategy is firing signals that don't fit current regime "
        f"(e.g. trend signals in a mean-reverting market). Adjust signal types.\n\n"
        f"Return ONLY valid JSON:\n"
        f'{{"assessment": "MARKET_EVENT", '
        f'"confidence": 0.8, '
        f'"reasoning": "2-3 sentence explanation of what caused the drawdown", '
        f'"recommendation": "specific action: e.g. pause until VIX < 25, or exit SECTOR positions, or continue reduced sizing", '
        f'"resume_condition": "what needs to happen before full sizing resumes", '
        f'"telegram_summary": "2-3 sentence plain-English message for the trader"}}'
    )

    try:
        result = call_llm_thinking(prompt, module='drawdown_review', budget_tokens=3000)

        assessment    = result.get('assessment', 'STRATEGY_VARIANCE')
        confidence    = float(result.get('confidence', 0.5))
        reasoning     = str(result.get('reasoning', ''))[:300]
        recommendation = str(result.get('recommendation', ''))[:200]
        resume_cond   = str(result.get('resume_condition', ''))[:150]
        tg_summary    = str(result.get('telegram_summary', ''))[:400]

        review = {
            'timestamp':      datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'trigger_status': status,
            'assessment':     assessment,
            'confidence':     confidence,
            'reasoning':      reasoning,
            'recommendation': recommendation,
            'resume_condition': resume_cond,
            'drawdown_pct':   draw.get('pct', 0),
            'llm_generated':  True,
        }
        atomic_write(REVIEW_FILE, review)

        record_llm_call('drawdown_review_llm', used_llm=True,
                        result_summary=f"assessment={assessment} conf={confidence:.1f}")
        log_info(f"Drawdown review: {assessment} (confidence {confidence:.1f})")

        # Telegram alert
        assessment_icons = {
            'MARKET_EVENT':       '🌊',
            'STRATEGY_VARIANCE':  '📊',
            'STRATEGY_CONCERN':   '⚠️',
            'REGIME_MISMATCH':    '🔄',
        }
        icon = assessment_icons.get(assessment, '❓')

        send_telegram(
            f"🔴 DRAWDOWN REVIEW — {status}\n"
            f"{icon} Assessment: {assessment} (confidence {confidence:.0%})\n\n"
            f"{tg_summary}\n\n"
            f"📋 Recommendation: {recommendation}\n"
            f"🔓 Resume when: {resume_cond}"
        )

    except Exception as _e:
        log_warning(f"Drawdown review LLM failed (fail-open): {_e}")
        record_llm_call('drawdown_review_llm', used_llm=False,
                        result_summary=f'error:{type(_e).__name__}')


if __name__ == '__main__':
    trigger = sys.argv[1].upper() if len(sys.argv) > 1 else None
    run(trigger_status=trigger)
