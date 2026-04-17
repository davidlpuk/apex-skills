#!/usr/bin/env python3
"""
LLM Portfolio Agent
Whole-book risk reasoning that no per-signal module can see.

Runs after morning brief (08:10 UTC) and after any trade execution.
Reasons about the portfolio AS A WHOLE — not individual signals:

  1. Correlation risk     — multiple positions in the same sector/theme
  2. Factor concentration — over-exposed to single factor (momentum, value, rate)
  3. Regime-position fit  — do open positions match the current HMM state?
  4. Tail risk            — what single event could hurt the whole book?
  5. Cash deployment      — is idle cash appropriate given current regime?
  6. Signal-type balance  — are we too TREND-heavy in a MEAN_REVERTING regime?

Output: apex-llm-portfolio-review.json
Telegram: sends alert when risk level is HIGH or when action is recommended

Flag: portfolio_agent_llm

Usage:
    python3 apex-llm-portfolio-agent.py              (run full review)
    python3 apex-llm-portfolio-agent.py status       (print last review)
    python3 apex-llm-portfolio-agent.py force        (run even if recent review exists)

Always fail-open: any LLM failure writes a neutral review so downstream
consumers never see a missing file.
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import atomic_write, safe_read, log_warning, log_info, send_telegram
    from apex_llm_flags import get_llm_flag, record_llm_call, call_llm_thinking, build_regime_preamble
except ImportError as _e:
    print(f"FATAL: import failed: {_e}")
    sys.exit(1)

LOGS        = '/home/ubuntu/.picoclaw/logs'
REVIEW_FILE = f'{LOGS}/apex-llm-portfolio-review.json'

# Don't re-run if we reviewed within this window (avoids double-runs)
_MIN_REVIEW_INTERVAL_MIN = 90


# ── Context Gathering ─────────────────────────────────────────────────────────

def _age_min(filepath: str) -> float:
    try:
        return (datetime.now(timezone.utc).timestamp() - os.path.getmtime(filepath)) / 60
    except Exception:
        return 999.0


def _gather_context() -> dict:
    ctx: dict = {}

    # ── Open positions ──────────────────────────────────────────────────────
    positions_raw = safe_read(f'{LOGS}/apex-positions.json', [])
    if not isinstance(positions_raw, list):
        positions_raw = []
    open_pos = [p for p in positions_raw
                if p.get('status') in ('protected', 'entry_placed')]

    positions = []
    for p in open_pos:
        name        = p.get('name', p.get('t212_ticker', '?'))
        entry       = float(p.get('entry', 0))
        current     = float(p.get('current', entry))
        stop        = float(p.get('stop', 0))
        t1          = float(p.get('target1', 0))
        t2          = float(p.get('target2', 0))
        notional    = float(p.get('notional', 0)) or (entry * float(p.get('quantity', 0)))
        pct         = round((current - entry) / entry * 100, 2) if entry else 0
        stop_pct    = round((current - stop) / current * 100, 2) if current else 0
        r_achieved  = round((current - entry) / (entry - stop), 2) if entry != stop else 0

        try:
            days_held = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(p['entry_time'].replace('Z', '+00:00'))
                        ).days if p.get('entry_time') else 0
        except Exception:
            days_held = 0

        positions.append({
            'name':         name,
            'signal_type':  p.get('signal_type', 'TREND'),
            'sector':       p.get('sector', 'UNKNOWN'),
            'currency':     p.get('currency', 'GBP'),
            'entry':        entry,
            'current':      current,
            'stop':         stop,
            'target1':      t1,
            'target2':      t2,
            'notional':     round(notional, 2),
            'pct_move':     pct,
            'stop_pct_away': stop_pct,
            'r_achieved':   r_achieved,
            'days_held':    days_held,
            'status':       p.get('status', 'protected'),
        })
    ctx['positions'] = positions

    # ── Portfolio totals ────────────────────────────────────────────────────
    portfolio = safe_read(f'{LOGS}/apex-portfolio-cache.json', {})
    free      = float(portfolio.get('free', 0))
    invested  = float(portfolio.get('invested', 0))
    nav       = free + invested
    ctx['portfolio'] = {
        'nav':      round(nav, 2),
        'cash':     round(free, 2),
        'invested': round(invested, 2),
        'cash_pct': round(free / nav * 100, 1) if nav else 0,
        'n_positions': len(positions),
    }

    # ── Signal type distribution ────────────────────────────────────────────
    sig_types: dict = {}
    sectors:   dict = {}
    currencies: dict = {}
    for p in positions:
        sig_types[p['signal_type']] = sig_types.get(p['signal_type'], 0) + 1
        sectors[p['sector']]        = sectors.get(p['sector'], 0) + 1
        currencies[p['currency']]   = currencies.get(p['currency'], 0) + 1
    ctx['signal_type_counts'] = sig_types
    ctx['sector_counts']      = sectors
    ctx['currency_counts']    = currencies

    # ── Regime ─────────────────────────────────────────────────────────────
    regime  = safe_read(f'{LOGS}/apex-regime.json', {})
    scaling = safe_read(f'{LOGS}/apex-regime-scaling.json', {})
    ctx['regime'] = {
        'overall':    regime.get('overall', 'UNKNOWN'),
        'vix':        regime.get('vix', '?'),
        'breadth':    regime.get('breadth_pct', '?'),
        'hmm_state':  scaling.get('hmm_state', 'UNKNOWN'),
        'label':      scaling.get('regime_label', 'NEUTRAL'),
        'block_reasons': regime.get('block_reason', []),
    }

    # ── Drawdown / circuit breaker ──────────────────────────────────────────
    draw = safe_read(f'{LOGS}/apex-drawdown.json', {})
    cb   = safe_read(f'{LOGS}/apex-circuit-breaker.json', {})
    ctx['drawdown'] = {
        'status':     draw.get('status', 'NORMAL'),
        'pct':        draw.get('drawdown_pct', 0),
        'multiplier': draw.get('multiplier', 1.0),
    }
    ctx['circuit_breaker'] = cb.get('status', 'NORMAL')

    # ── Rolling P&L ────────────────────────────────────────────────────────
    rolling = safe_read(f'{LOGS}/apex-rolling-pnl.json', {})
    ctx['rolling_pnl'] = {
        '1d':  rolling.get('day_1', 0),
        '3d':  rolling.get('day_3', 0),
        '5d':  rolling.get('day_5', 0),
        '10d': rolling.get('day_10', 0),
    }

    # ── Sector rotation / relative strength ────────────────────────────────
    sector_rot = safe_read(f'{LOGS}/apex-sector-rotation.json', {})
    rel_str    = safe_read(f'{LOGS}/apex-relative-strength.json', {})
    ctx['leading_sectors']  = sector_rot.get('leading', [])[:5]
    ctx['lagging_sectors']  = sector_rot.get('lagging', [])[:5]
    ctx['rs_top']           = [r.get('name', '') for r in rel_str.get('top', [])[:5]]
    ctx['rs_bottom']        = [r.get('name', '') for r in rel_str.get('bottom', [])[:5]]

    # ── Geo / macro / black swan ────────────────────────────────────────────
    geo = safe_read(f'{LOGS}/apex-geo-news.json', {})
    bs  = safe_read(f'{LOGS}/apex-blackswan.json', {})
    macro = safe_read(f'{LOGS}/apex-macro-signals.json', {})
    ctx['geo']       = geo.get('overall', 'CLEAR')
    ctx['blackswan'] = bs.get('status', 'NORMAL')
    ctx['macro']     = macro.get('signal', 'NEUTRAL')

    # ── Recent outcomes (5 trades) ──────────────────────────────────────────
    outcomes = safe_read(f'{LOGS}/apex-outcomes.json', {})
    trades   = outcomes.get('trades', [])
    ctx['recent_trades'] = [
        {'name': t.get('name', '?'), 'pnl': t.get('pnl', 0),
         'signal_type': t.get('signal_type', '?'),
         'date': (t.get('closed_at') or '')[:10]}
        for t in trades[-5:]
    ]

    # ── Multiframe weekly trend for each position ────────────────────────────
    mtf = safe_read(f'{LOGS}/apex-multiframe.json', {})
    for p in ctx['positions']:
        name = p['name'].upper()
        inst = mtf.get('data', {}).get(name, {})
        wk   = inst.get('weekly', {})
        p['weekly_trend'] = wk.get('trend_class', 'UNKNOWN')

    # ── Queued signals ──────────────────────────────────────────────────────
    queue_raw = safe_read(f'{LOGS}/apex-trade-queue.json', [])
    if isinstance(queue_raw, list):
        queue_entries = queue_raw
    elif isinstance(queue_raw, dict):
        queue_entries = queue_raw.get('queue', [])
    else:
        queue_entries = []
    queued = [e for e in queue_entries if e.get('status') == 'QUEUED']
    ctx['queued_signals'] = [
        {'name': e.get('name', '?'), 'signal_type': e.get('signal_type', '?'),
         'score': e.get('adjusted_score', 0)}
        for e in queued[:5]
    ]

    return ctx


# ── Prompt Builder ────────────────────────────────────────────────────────────

def _build_prompt(ctx: dict) -> str:
    today    = datetime.now(timezone.utc).strftime('%A %d %B %Y %H:%M UTC')
    regime   = ctx['regime']
    port     = ctx['portfolio']
    draw     = ctx['drawdown']
    rolling  = ctx['rolling_pnl']
    positions = ctx['positions']

    # Build position table
    pos_lines = ''
    for p in positions:
        wk = p.get('weekly_trend', 'UNKNOWN')
        pos_lines += (
            f"  {p['name']:8s} | {p['signal_type']:16s} | {p['sector']:16s} | "
            f"£{p['notional']:6.0f} notional | "
            f"{p['pct_move']:+5.1f}% | stop {p['stop_pct_away']:.1f}% away | "
            f"{p['r_achieved']:.1f}R | {p['days_held']}d | weekly: {wk}\n"
        )

    # Signal type breakdown
    sig_breakdown = ' | '.join(f"{k}: {v}" for k, v in ctx['signal_type_counts'].items())
    sector_breakdown = ' | '.join(f"{k}: {v}" for k, v in ctx['sector_counts'].items())
    currency_breakdown = ' | '.join(f"{k}: {v}" for k, v in ctx['currency_counts'].items())

    recent_trades_str = ''
    for t in ctx['recent_trades']:
        icon = '✅' if (t['pnl'] or 0) > 0 else '❌'
        recent_trades_str += f"  {icon} {t['name']} ({t['signal_type']}) £{t['pnl']:+.2f} on {t['date']}\n"

    queued_str = ''
    for q in ctx['queued_signals']:
        queued_str += f"  {q['name']} ({q['signal_type']}, score {q['score']:.1f})\n"

    # Signal type / regime fit guidance
    _hmm_priority = {
        'TRENDING':       ('TREND, EARNINGS_DRIFT', 'CONTRARIAN, INVERSE'),
        'MEAN_REVERTING': ('CONTRARIAN, INVERSE',   'TREND'),
        'CRISIS':         ('INVERSE, CONTRARIAN',   'TREND, EARNINGS_DRIFT'),
    }
    hmm = regime['hmm_state']
    good_types, bad_types = _hmm_priority.get(hmm, ('any', 'none'))

    prompt = f"""You are a portfolio risk advisor for an automated UK retail trading system.
Today is {today}.

Your task: reason about the WHOLE PORTFOLIO — correlation, concentration, and regime fit.
Per-signal modules handle individual trade decisions. You handle book-level risk.

{build_regime_preamble()}
═══════════════════════════════════════
PORTFOLIO STATE
═══════════════════════════════════════

PORTFOLIO SUMMARY:
  NAV: £{port['nav']:.2f} | Cash: £{port['cash']:.2f} ({port['cash_pct']:.0f}%)
  Open positions: {port['n_positions']} | Invested: £{port['invested']:.2f}
  Circuit breaker: {ctx['circuit_breaker']} | Drawdown: {draw['status']} ({draw['pct']:.1f}%)

ROLLING P&L:
  Today: £{rolling['1d']:+.2f} | 3d: £{rolling['3d']:+.2f} | 5d: £{rolling['5d']:+.2f} | 10d: £{rolling['10d']:+.2f}

OPEN POSITIONS:
  Name     | Signal Type      | Sector           | Notional | Move  | Stop  | R   | Days | Weekly
{pos_lines or '  (none)\n'}
BREAKDOWN:
  Signal types: {sig_breakdown or 'none'}
  Sectors:      {sector_breakdown or 'none'}
  Currencies:   {currency_breakdown or 'none'}

═══════════════════════════════════════
MARKET CONTEXT
═══════════════════════════════════════

REGIME: {regime['overall']} | HMM: {hmm} | Label: {regime['label']}
  VIX: {regime['vix']} | Breadth: {regime['breadth']}%
  Preferred signal types for {hmm}: {good_types}
  Types to avoid in {hmm}:          {bad_types}
  Block reasons: {'; '.join(regime['block_reasons'] or ['none'])}

SECTOR ROTATION:
  Leading (favour): {', '.join(ctx['leading_sectors']) or 'none'}
  Lagging (avoid):  {', '.join(ctx['lagging_sectors']) or 'none'}

MACRO / GEO:
  Macro: {ctx['macro']} | Geo: {ctx['geo']} | Black swan: {ctx['blackswan']}

RECENT TRADES (momentum context):
{recent_trades_str or '  None\n'}
QUEUED SIGNALS (pending execution):
{queued_str or '  None\n'}
═══════════════════════════════════════
YOUR RISK ANALYSIS TASK
═══════════════════════════════════════

Analyse the book for FIVE specific risks. Be concrete — reference specific positions by name.

1. CORRELATION_RISK: Are multiple positions in the same sector or theme?
   State which positions are correlated and what single event would hurt them all.

2. REGIME_FIT: Do open positions match the HMM state ({hmm})?
   Flag any position whose signal_type is mismatched with current regime.
   E.g. TREND signals in MEAN_REVERTING regime are fighting the tape.

3. CONCENTRATION_RISK: Is any single position too large relative to NAV?
   Flag positions where notional > 15% of NAV (£{port['nav'] * 0.15:.0f}).
   Also flag if >50% of positions are in the same sector.

4. TAIL_RISK: What single event (earnings, geo, macro release) could damage
   the most positions simultaneously? Name the scenario and which positions are exposed.

5. CASH_POSTURE: Is the cash level ({port['cash_pct']:.0f}%) appropriate?
   In HOSTILE/CRISIS regimes, >40% cash is correct. In TRENDING regimes with
   good setups queued, <20% cash may be leaving money on the table.

Then provide:

6. position_actions: for each open position, one of:
   - NO_ACTION: thesis intact, stop correctly placed
   - CONSIDER_TIGHTENING: profitable position, tighten stop to protect gains
   - WATCH_FOR_EXIT: regime or news is working against this position
   - REDUCE_SIZE: position too large for current risk environment
   Format: [{{"name": "XOM", "action": "CONSIDER_TIGHTENING", "note": "3R achieved, HOSTILE regime"}}]

7. book_risk_level: LOW | MEDIUM | HIGH | CRITICAL
   LOW = no material risks identified
   MEDIUM = 1-2 manageable risks, monitor
   HIGH = significant correlation or regime mismatch, action recommended
   CRITICAL = book is fragile, immediate action required

8. overall_summary: 3-4 sentence plain-English summary for the trader (Telegram-friendly)

Return ONLY valid JSON with exactly these 8 fields:
{{"correlation_risk": "...", "regime_fit": "...", "concentration_risk": "...",
  "tail_risk": "...", "cash_posture": "...",
  "position_actions": [...], "book_risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
  "overall_summary": "..."}}"""

    return prompt


# ── Main ──────────────────────────────────────────────────────────────────────

def _neutral_review(reason: str = 'unavailable') -> dict:
    return {
        'timestamp':          datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'book_risk_level':    'UNKNOWN',
        'correlation_risk':   '',
        'regime_fit':         '',
        'concentration_risk': '',
        'tail_risk':          '',
        'cash_posture':       '',
        'position_actions':   [],
        'overall_summary':    f'Portfolio review unavailable ({reason}).',
        'llm_generated':      False,
        'n_positions':        0,
    }


def run(force: bool = False) -> dict:
    """
    Run the portfolio risk review.

    Args:
        force: skip the minimum interval check and always run.

    Returns the review dict (also written to REVIEW_FILE).
    """
    if not get_llm_flag('portfolio_agent_llm'):
        log_info("Portfolio agent: flag disabled — skipping")
        record_llm_call('portfolio_agent_llm', used_llm=False, result_summary='flag_disabled')
        return _neutral_review('flag_disabled')

    # Avoid back-to-back runs unless forced
    if not force:
        prev = safe_read(REVIEW_FILE, {})
        if prev.get('timestamp'):
            try:
                prev_time = datetime.fromisoformat(prev['timestamp'].replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - prev_time).total_seconds() / 60
                if age_min < _MIN_REVIEW_INTERVAL_MIN:
                    log_info(f"Portfolio agent: reviewed {age_min:.0f}m ago — skipping (use force to override)")
                    return prev
            except Exception:
                pass

    log_info("Portfolio agent: gathering context...")
    ctx = _gather_context()

    n_pos = len(ctx['positions'])
    if n_pos == 0:
        log_info("Portfolio agent: no open positions — writing neutral review")
        review = _neutral_review('no_open_positions')
        review['overall_summary'] = 'No open positions. Book is fully in cash.'
        atomic_write(REVIEW_FILE, review)
        record_llm_call('portfolio_agent_llm', used_llm=False, result_summary='no_positions')
        return review

    try:
        prompt = _build_prompt(ctx)
        # High budget — this is the most comprehensive, whole-book call
        result = call_llm_thinking(prompt, module='portfolio_agent', budget_tokens=5000)

        risk_level  = result.get('book_risk_level', 'MEDIUM')
        if risk_level not in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'):
            risk_level = 'MEDIUM'

        review = {
            'timestamp':          datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'book_risk_level':    risk_level,
            'correlation_risk':   str(result.get('correlation_risk', ''))[:300],
            'regime_fit':         str(result.get('regime_fit', ''))[:300],
            'concentration_risk': str(result.get('concentration_risk', ''))[:300],
            'tail_risk':          str(result.get('tail_risk', ''))[:300],
            'cash_posture':       str(result.get('cash_posture', ''))[:200],
            'position_actions':   result.get('position_actions', []),
            'overall_summary':    str(result.get('overall_summary', ''))[:600],
            'llm_generated':      True,
            'n_positions':        n_pos,
            'regime_at_review':   ctx['regime']['overall'],
            'hmm_at_review':      ctx['regime']['hmm_state'],
        }

        atomic_write(REVIEW_FILE, review)
        record_llm_call('portfolio_agent_llm', used_llm=True,
                        result_summary=f"risk={risk_level} n_pos={n_pos}")
        log_info(f"Portfolio agent: review written (risk={risk_level} positions={n_pos})")

        # Send Telegram when risk is elevated or actions are recommended
        risk_icons = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🟠', 'CRITICAL': '🔴'}
        icon = risk_icons.get(risk_level, '❓')

        actionable = [a for a in review['position_actions']
                      if a.get('action') != 'NO_ACTION']

        should_alert = risk_level in ('HIGH', 'CRITICAL') or bool(actionable)

        if should_alert:
            action_lines = ''
            for a in actionable:
                act_icon = {
                    'CONSIDER_TIGHTENING': '📏',
                    'WATCH_FOR_EXIT':      '👁️',
                    'REDUCE_SIZE':         '✂️',
                }.get(a.get('action', ''), '•')
                action_lines += f"\n  {act_icon} {a.get('name','?')}: {a.get('note','')}"

            msg = (
                f"📊 PORTFOLIO RISK REVIEW\n"
                f"{icon} Book risk: {risk_level} | {n_pos} positions\n\n"
                f"{review['overall_summary']}"
            )
            if review['correlation_risk']:
                msg += f"\n\n🔗 Correlation: {review['correlation_risk'][:150]}"
            if review['regime_fit'] and 'mismatch' in review['regime_fit'].lower():
                msg += f"\n\n⚠️ Regime fit: {review['regime_fit'][:150]}"
            if review['tail_risk']:
                msg += f"\n\n🌊 Tail risk: {review['tail_risk'][:150]}"
            if action_lines:
                msg += f"\n\n📋 Recommended actions:{action_lines}"

            send_telegram(msg)

        return review

    except Exception as _e:
        log_warning(f"Portfolio agent LLM failed (fail-open): {_e}")
        record_llm_call('portfolio_agent_llm', used_llm=False,
                        result_summary=f'error:{type(_e).__name__}')
        review = _neutral_review(f'llm_error:{type(_e).__name__}')
        atomic_write(REVIEW_FILE, review)
        return review


if __name__ == '__main__':
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else 'run'

    if cmd == 'status':
        data = safe_read(REVIEW_FILE, {})
        if not data:
            print("No portfolio review found. Run: python3 apex-llm-portfolio-agent.py")
        else:
            ts    = data.get('timestamp', '?')[:16].replace('T', ' ')
            risk  = data.get('book_risk_level', 'UNKNOWN')
            n_pos = data.get('n_positions', '?')
            icons = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🟠', 'CRITICAL': '🔴'}
            print(f"{icons.get(risk,'?')} Portfolio review @ {ts}")
            print(f"   Risk level:  {risk}")
            print(f"   Positions:   {n_pos}")
            print(f"   Regime:      {data.get('regime_at_review','?')} / {data.get('hmm_at_review','?')}")
            print(f"\nSummary:\n  {data.get('overall_summary','')}")
            print(f"\nCorrelation risk:\n  {data.get('correlation_risk','')[:200]}")
            print(f"\nRegime fit:\n  {data.get('regime_fit','')[:200]}")
            print(f"\nTail risk:\n  {data.get('tail_risk','')[:200]}")
            actions = [a for a in data.get('position_actions', []) if a.get('action') != 'NO_ACTION']
            if actions:
                print(f"\nRecommended actions:")
                for a in actions:
                    print(f"  {a.get('name','?')}: {a.get('action','')} — {a.get('note','')}")

    elif cmd == 'force':
        review = run(force=True)
        print(f"Risk level: {review.get('book_risk_level','?')}")
        print(f"Summary: {review.get('overall_summary','')}")

    else:  # 'run' or default
        review = run(force=False)
        print(f"Risk level: {review.get('book_risk_level','?')}")
        print(f"LLM generated: {review.get('llm_generated', False)}")
