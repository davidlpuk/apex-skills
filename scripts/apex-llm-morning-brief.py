#!/usr/bin/env python3
"""
LLM Morning Brief
Runs at 07:55 UTC Mon-Fri — after all intelligence scripts have refreshed
but before the 08:30 morning scan fires.

Synthesises ALL available context into a strategic brief for the day:
  - Risk posture (FULL / REDUCED / CAUTIOUS / DEFENSIVE)
  - Key risks today (earnings, macro data, Fed speakers, geopolitics)
  - Sectors to favour / avoid
  - Per-position guidance for open positions
  - Overnight market context (Asian closes, FX moves, pre-market gaps)
  - Queue guidance (queued signals still valid?)
  - Plain-English Telegram summary

Output: apex-llm-morning-brief.json (consumed by decision engine)
Telegram: sends brief automatically

Always fail-open: if LLM call fails, writes a neutral brief so the
decision engine can proceed normally.

Flag: morning_brief_llm
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import atomic_write, safe_read, log_warning, log_info, send_telegram
    from apex_llm_flags import get_llm_flag, record_llm_call, call_llm_thinking
except ImportError as _e:
    print(f"FATAL: import failed: {_e}")
    sys.exit(1)

LOGS   = '/home/ubuntu/.picoclaw/logs'
BRIEF_FILE = f'{LOGS}/apex-llm-morning-brief.json'

# ── Context Gathering ─────────────────────────────────────────────────────────

def _age_min(filepath: str) -> float:
    try:
        return (datetime.now(timezone.utc).timestamp() - os.path.getmtime(filepath)) / 60
    except Exception:
        return 999.0


def _gather_context() -> dict:
    """Gather all available intelligence into a structured context dict."""
    ctx: dict = {}

    # 1. Market regime
    regime = safe_read(f'{LOGS}/apex-regime.json', {})
    ctx['regime'] = {
        'overall':  regime.get('overall', 'UNKNOWN'),
        'vix':      regime.get('vix', '?'),
        'breadth':  regime.get('breadth_pct', '?'),
        'block_reasons': regime.get('block_reason', []),
    }

    # 2. Regime scaling / HMM state
    scaling = safe_read(f'{LOGS}/apex-regime-scaling.json', {})
    ctx['hmm_state']     = scaling.get('hmm_state', 'UNKNOWN')
    ctx['regime_label']  = scaling.get('regime_label', 'NEUTRAL')

    # 3. Sentiment
    sent = safe_read(f'{LOGS}/apex-sentiment.json', {})
    ctx['sentiment'] = {
        'market_class':  sent.get('market_class', 'NEUTRAL'),
        'market_score':  sent.get('market_sentiment', 0),
        'top_headlines': [h.get('title', '') if isinstance(h, dict) else str(h)
                          for h in sent.get('top_headlines', [])[:5]],
        'worst_headlines': [h.get('title', '') if isinstance(h, dict) else str(h)
                            for h in sent.get('worst_headlines', [])[:5]],
        'stale': _age_min(f'{LOGS}/apex-sentiment.json') > 90,
    }

    # 4. Geo / macro news
    geo = safe_read(f'{LOGS}/apex-geo-news.json', {})
    ctx['geo'] = {
        'overall':    geo.get('overall', 'CLEAR'),
        'geo_flags':  [f.get('title', '') for f in geo.get('geo_flags', [])[:5]],
        'energy_flags': [f.get('title', '') for f in geo.get('energy_flags', [])[:3]],
    }

    # 5. TACO state (geopolitical event classification)
    taco = safe_read(f'{LOGS}/apex-taco-state.json', {})
    ctx['taco'] = {
        'status':     taco.get('status', 'NEUTRAL'),
        'confidence': taco.get('confidence', 0),
        'threat_type': taco.get('threat_type', 'NONE'),
    }

    # 6. Economic calendar — today's events
    cal = safe_read(f'{LOGS}/apex-econ-calendar.json', {})
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_events = [
        e for e in cal.get('events', [])
        if e.get('date', '') == today_str
    ]
    ctx['calendar_events'] = [
        f"{e.get('time', '?')} UTC — {e.get('name', '?')} "
        f"(importance: {e.get('importance', '?')})"
        for e in today_events[:10]
    ]

    # 7. Open positions
    positions = safe_read(f'{LOGS}/apex-positions.json', [])
    if not isinstance(positions, list):
        positions = []
    open_pos = [p for p in positions if p.get('status') in ('protected', 'entry_placed')]
    ctx['open_positions'] = []
    for p in open_pos:
        name    = p.get('name', p.get('t212_ticker', '?'))
        entry   = p.get('entry', 0)
        current = p.get('current', entry)
        stop    = p.get('stop', 0)
        t1      = p.get('target1', 0)
        pct     = round((current - entry) / entry * 100, 1) if entry else 0
        stop_pct = round((current - stop) / current * 100, 1) if current else 0
        ctx['open_positions'].append({
            'name':      name,
            'signal_type': p.get('signal_type', 'TREND'),
            'sector':    p.get('sector', 'UNKNOWN'),
            'entry':     entry,
            'current':   current,
            'pct_move':  pct,
            'stop':      stop,
            'stop_pct_away': stop_pct,
            'target1':   t1,
            'days_held': (datetime.now(timezone.utc) -
                          datetime.fromisoformat(p['entry_time'].replace('Z', '+00:00'))
                          ).days if p.get('entry_time') else '?',
        })

    # 8. Portfolio / drawdown
    drawdown = safe_read(f'{LOGS}/apex-drawdown.json', {})
    ctx['drawdown'] = {
        'status':     drawdown.get('status', 'NORMAL'),
        'pct':        drawdown.get('drawdown_pct', 0),
        'multiplier': drawdown.get('multiplier', 1.0),
    }
    portfolio = safe_read(f'{LOGS}/apex-portfolio-cache.json', {})
    ctx['portfolio'] = {
        'total_value': round(float(portfolio.get('free', 0)) +
                             float(portfolio.get('invested', 0)), 2),
        'cash':        round(float(portfolio.get('free', 0)), 2),
    }

    # 9. Recent trade outcomes (momentum context)
    outcomes = safe_read(f'{LOGS}/apex-outcomes.json', {})
    trades   = outcomes.get('trades', [])
    recent   = trades[-5:] if trades else []
    ctx['recent_trades'] = [
        {'name': t.get('name', '?'), 'pnl': t.get('pnl', 0),
         'signal_type': t.get('signal_type', '?'), 'date': t.get('closed_at', '')[:10]}
        for t in recent
    ]

    # 10. Trade queue (signals waiting to execute)
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
         'score': e.get('adjusted_score', 0), 'queued_at': e.get('queued_at', '')[:16]}
        for e in queued[:5]
    ]

    # 11. Macro signals
    macro = safe_read(f'{LOGS}/apex-macro-signals.json', {})
    ctx['macro'] = {
        'signal':  macro.get('signal', 'NEUTRAL'),
        'summary': macro.get('summary', ''),
    }

    # 12. Market direction
    direction = safe_read(f'{LOGS}/apex-market-direction.json', {})
    ctx['market_direction'] = {
        'status':   direction.get('direction_status', 'NEUTRAL'),
        'strength': direction.get('strength', 0),
    }

    # 13. Overnight index performance — fetch quickly from yfinance
    ctx['overnight_markets'] = _fetch_overnight_markets()

    # 14. Multiframe weekly trend for open positions
    mtf  = safe_read(f'{LOGS}/apex-multiframe.json', {})
    ctx['weekly_trends'] = {}
    for p in open_pos:
        name = p.get('name', '').upper()
        inst = mtf.get('data', {}).get(name, {})
        wk   = inst.get('weekly', {})
        ctx['weekly_trends'][name] = wk.get('trend_class', 'UNKNOWN')

    # 15. Black swan / circuit breaker
    bs = safe_read(f'{LOGS}/apex-blackswan.json', {})
    ctx['blackswan_status'] = bs.get('status', 'NORMAL')

    cb = safe_read(f'{LOGS}/apex-circuit-breaker.json', {})
    ctx['circuit_breaker']  = cb.get('status', 'NORMAL')

    return ctx


def _fetch_overnight_markets() -> dict:
    """Quick yfinance fetch for overnight index / FX moves. Non-blocking."""
    try:
        import yfinance as yf
        symbols = {
            'S&P500_futures': 'ES=F',
            'NASDAQ_futures':  'NQ=F',
            'FTSE100':         '^FTSE',
            'DAX':             '^GDAXI',
            'Nikkei':          '^N225',
            'VIX':             '^VIX',
            'GBPUSD':          'GBPUSD=X',
            'Gold':            'GC=F',
            'Oil_WTI':         'CL=F',
        }
        result = {}
        for label, ticker in symbols.items():
            try:
                hist = yf.Ticker(ticker).history(period='2d')
                if len(hist) >= 2:
                    prev  = float(hist['Close'].iloc[-2])
                    last  = float(hist['Close'].iloc[-1])
                    chg   = round((last - prev) / prev * 100, 2) if prev else 0
                    result[label] = {'price': round(last, 4), 'change_pct': chg}
                elif len(hist) == 1:
                    result[label] = {'price': round(float(hist['Close'].iloc[-1]), 4), 'change_pct': 0}
            except Exception:
                pass
        return result
    except ImportError:
        return {}
    except Exception as _e:
        log_warning(f"Morning brief: overnight markets fetch failed: {_e}")
        return {}


# ── Prompt Builder ────────────────────────────────────────────────────────────

def _build_prompt(ctx: dict) -> str:
    today = datetime.now(timezone.utc).strftime('%A %d %B %Y')
    pos_count = len(ctx.get('open_positions', []))

    pos_lines = ''
    for p in ctx.get('open_positions', []):
        wk_trend = ctx.get('weekly_trends', {}).get(p['name'].upper(), 'UNKNOWN')
        pos_lines += (
            f"  {p['name']} ({p['signal_type']}, {p['sector']}) — "
            f"entry £{p['entry']} | current £{p['current']} ({p['pct_move']:+.1f}%) | "
            f"stop £{p['stop']} ({p['stop_pct_away']:.1f}% away) | "
            f"T1 £{p['target1']} | days held: {p['days_held']} | weekly: {wk_trend}\n"
        )

    queue_lines = ''
    for q in ctx.get('queued_signals', []):
        queue_lines += f"  {q['name']} ({q['signal_type']}, score {q['score']}) queued {q['queued_at']}\n"

    calendar_lines = '\n'.join(f"  {e}" for e in ctx.get('calendar_events', [])) or '  None scheduled'

    recent_lines = ''
    for t in ctx.get('recent_trades', []):
        icon = '✅' if (t['pnl'] or 0) > 0 else '❌'
        recent_lines += f"  {icon} {t['name']} ({t['signal_type']}) £{t['pnl']:+.2f} on {t['date']}\n"

    overnight = ctx.get('overnight_markets', {})
    overnight_lines = ''
    for label, d in overnight.items():
        chg = d.get('change_pct', 0)
        icon = '🟢' if chg > 0.3 else ('🔴' if chg < -0.3 else '⚪')
        overnight_lines += f"  {icon} {label}: {d.get('price')} ({chg:+.2f}%)\n"

    sent   = ctx.get('sentiment', {})
    regime = ctx.get('regime', {})
    taco   = ctx.get('taco', {})
    macro  = ctx.get('macro', {})
    draw   = ctx.get('drawdown', {})
    port   = ctx.get('portfolio', {})

    prompt = f"""You are the strategic risk advisor for an automated UK retail trading system.
Today is {today}. Markets open in ~35 minutes (LSE 08:00 UTC, NYSE 14:30 UTC).

Your job: synthesise all available intelligence into a STRATEGIC BRIEF that the system
and the human trader will use to set risk posture and make decisions today.

Be specific, direct, and evidence-based. The trader's capital is real.

═══════════════════════════════════════
MARKET INTELLIGENCE
═══════════════════════════════════════

REGIME & TECHNICALS:
  Overall regime: {regime.get('overall','?')} | HMM state: {ctx.get('hmm_state','?')}
  Regime label: {ctx.get('regime_label','?')}
  VIX: {regime.get('vix','?')} | Market breadth: {regime.get('breadth','?')}%
  Market direction: {ctx.get('market_direction',{}).get('status','?')}
  Circuit breaker: {ctx.get('circuit_breaker','NORMAL')} | Black swan: {ctx.get('blackswan_status','NORMAL')}
  Drawdown: {draw.get('status','NORMAL')} ({draw.get('pct',0):.1f}%) | Sizing: {int(draw.get('multiplier',1)*100)}%
  Block reasons: {'; '.join(regime.get('block_reasons',[]) or ['none'])}

PORTFOLIO:
  Value: £{port.get('total_value',0):.2f} | Cash: £{port.get('cash',0):.2f}
  Open positions ({pos_count}):
{pos_lines or '  None\n'}
GEOPOLITICAL & MACRO:
  Geo status: {ctx.get('geo',{}).get('overall','CLEAR')}
  TACO: {taco.get('status','NEUTRAL')} (confidence {taco.get('confidence',0):.0f}%) — threat: {taco.get('threat_type','NONE')}
  Macro signal: {macro.get('signal','NEUTRAL')} — {macro.get('summary','')[:100]}
  Geo headlines: {'; '.join(ctx.get('geo',{}).get('geo_flags',[])[:3]) or 'none'}
  Energy alerts: {'; '.join(ctx.get('geo',{}).get('energy_flags',[])[:2]) or 'none'}

SENTIMENT:
  Market: {sent.get('market_class','?')} ({sent.get('market_score',0):+.2f}){' [STALE]' if sent.get('stale') else ''}
  Top positive: {' | '.join(sent.get('top_headlines',[])[:3])}
  Top negative: {' | '.join(sent.get('worst_headlines',[])[:3])}

OVERNIGHT MARKETS:
{overnight_lines or '  Data unavailable\n'}
ECONOMIC CALENDAR (today):
{calendar_lines}

RECENT TRADES (momentum context):
{recent_lines or '  No recent trades\n'}
QUEUED SIGNALS (waiting to execute at 08:05):
{queue_lines or '  None queued\n'}

═══════════════════════════════════════
YOUR TASK
═══════════════════════════════════════

Produce a strategic brief with these fields:

1. risk_posture: FULL | REDUCED | CAUTIOUS | DEFENSIVE
   FULL = trade normally, standard sizing
   REDUCED = trade but smaller (50-75% size), be selective
   CAUTIOUS = only exceptional setups, cut all sizes to 50%
   DEFENSIVE = no new entries today, protect capital

2. risk_posture_reason: one sentence explaining why

3. max_trades_today: integer 0-3 (null = use system default of 2)
   Set to 0 if DEFENSIVE. Set to 1 if CAUTIOUS and conditions are poor.

4. key_risks: list of up to 5 specific risks today (time + nature if scheduled)

5. avoid_sectors: list of sectors to avoid (e.g. ["TECH", "ENERGY"])
   Only include if there is specific evidence to avoid them today.

6. favour_sectors: list of sectors that have a tailwind today

7. position_guidance: for each open position, one of:
   HOLD_TIGHT (stop is correctly placed, no action needed)
   CONSIDER_TIGHTENING (position profitable, consider moving stop to protect gains)
   WATCH_CLOSELY (position has a risk today — earnings, news, key level)
   CONSIDER_EXIT (thesis may be broken)
   Format: [{{"name": "ULVR", "action": "WATCH_CLOSELY", "note": "earnings today"}}]

8. queue_guidance: for each queued signal, PROCEED or CANCEL with reason
   Format: [{{"name": "AAPL", "action": "PROCEED", "note": "thesis intact"}}]

9. overnight_interpretation: 2-3 sentences interpreting the overnight market moves

10. brief_text: 3-4 sentence plain-English summary for the trader (Telegram-friendly)

Return ONLY valid JSON with exactly these 10 fields."""

    return prompt


# ── Main ──────────────────────────────────────────────────────────────────────

def _neutral_brief() -> dict:
    """Fallback brief when LLM is unavailable or flag is off."""
    return {
        'timestamp':          datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'expires_at':         (datetime.now(timezone.utc).replace(hour=17, minute=0, second=0)
                               ).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'risk_posture':       'FULL',
        'risk_posture_reason': 'LLM brief unavailable — using system defaults',
        'max_trades_today':   None,
        'key_risks':          [],
        'avoid_sectors':      [],
        'favour_sectors':     [],
        'position_guidance':  [],
        'queue_guidance':     [],
        'overnight_interpretation': '',
        'brief_text':         'Morning brief unavailable — system running on standard rules.',
        'llm_generated':      False,
    }


def run():
    if not get_llm_flag('morning_brief_llm'):
        log_info("Morning brief: flag disabled — skipping")
        record_llm_call('morning_brief_llm', used_llm=False, result_summary='flag_disabled')
        return

    log_info("Morning brief: gathering context...")
    ctx = _gather_context()

    try:
        prompt = _build_prompt(ctx)
        # Higher budget — this is the most context-rich call of the day
        result = call_llm_thinking(prompt, module='morning_brief', budget_tokens=4096)

        # Validate and normalise key fields
        risk_posture = result.get('risk_posture', 'FULL')
        if risk_posture not in ('FULL', 'REDUCED', 'CAUTIOUS', 'DEFENSIVE'):
            risk_posture = 'FULL'

        brief = {
            'timestamp':            datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'expires_at':           (datetime.now(timezone.utc).replace(
                                        hour=17, minute=0, second=0)
                                     ).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'risk_posture':         risk_posture,
            'risk_posture_reason':  str(result.get('risk_posture_reason', ''))[:200],
            'max_trades_today':     result.get('max_trades_today'),
            'key_risks':            result.get('key_risks', [])[:5],
            'avoid_sectors':        result.get('avoid_sectors', []),
            'favour_sectors':       result.get('favour_sectors', []),
            'position_guidance':    result.get('position_guidance', []),
            'queue_guidance':       result.get('queue_guidance', []),
            'overnight_interpretation': str(result.get('overnight_interpretation', ''))[:400],
            'brief_text':           str(result.get('brief_text', ''))[:600],
            'llm_generated':        True,
            'context_positions':    len(ctx.get('open_positions', [])),
            'context_queued':       len(ctx.get('queued_signals', [])),
        }

        atomic_write(BRIEF_FILE, brief)
        record_llm_call('morning_brief_llm', used_llm=True,
                        result_summary=f"posture={risk_posture}")
        log_info(f"Morning brief: written (posture={risk_posture})")

        # Send Telegram
        posture_icon = {'FULL': '✅', 'REDUCED': '⚠️', 'CAUTIOUS': '🟠', 'DEFENSIVE': '🔴'}
        icon = posture_icon.get(risk_posture, '❓')
        pos_guidance_lines = ''
        for pg in brief.get('position_guidance', []):
            act_icon = {'HOLD_TIGHT': '🔒', 'CONSIDER_TIGHTENING': '📏',
                        'WATCH_CLOSELY': '👁️', 'CONSIDER_EXIT': '⚠️'}.get(pg.get('action', ''), '•')
            pos_guidance_lines += f"\n  {act_icon} {pg.get('name','?')}: {pg.get('note','')}"

        risks_text = ''
        for r in brief.get('key_risks', [])[:3]:
            risks_text += f"\n  • {r}"

        msg = (
            f"🌅 LLM MORNING BRIEF\n"
            f"{icon} Risk Posture: {risk_posture}\n"
            f"   {brief['risk_posture_reason']}\n"
            f"\n{brief['brief_text']}"
        )
        if risks_text:
            msg += f"\n\n⚠️ Key risks today:{risks_text}"
        if brief.get('avoid_sectors'):
            msg += f"\n\n🚫 Avoid sectors: {', '.join(brief['avoid_sectors'])}"
        if pos_guidance_lines:
            msg += f"\n\n📊 Positions:{pos_guidance_lines}"
        if brief.get('max_trades_today') is not None:
            msg += f"\n\n📌 Max trades today: {brief['max_trades_today']}"

        send_telegram(msg)

    except Exception as _e:
        log_warning(f"Morning brief LLM failed (fail-open): {_e}")
        record_llm_call('morning_brief_llm', used_llm=False,
                        result_summary=f'error:{type(_e).__name__}')
        brief = _neutral_brief()
        atomic_write(BRIEF_FILE, brief)


if __name__ == '__main__':
    run()
