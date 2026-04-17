#!/usr/bin/env python3
"""
LLM Exit Timing
Adjusts the partial-close fraction when a position hits Target 1.

The Sortino-based model sets a base fraction (33/50/66%).
This module uses Gemini to adjust that fraction based on:
  - Current news sentiment for the instrument
  - Market regime and macro backdrop
  - Whether the current price action suggests continuation toward T2

get_exit_fraction(position, base_fraction) -> (float, str)

Returns:
    (fraction, reason)
    fraction: 0.25–0.75 (clamped)
    reason:   one-line explanation, or 'flag_disabled' / 'not_applicable'

Always fail-open: any exception returns (base_fraction, 'timing_error').
"""
import json
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import safe_read, log_warning, log_info
    from apex_llm_flags import get_llm_flag, record_llm_call, build_regime_preamble
    from apex_llm_ab_tracker import get_module_performance
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m):    print(f'INFO: {m}')
    def get_llm_flag(n): return True
    def record_llm_call(*a, **k): pass
    def build_regime_preamble(): return ''
    def get_module_performance(m, **k): return ''

SENTIMENT_FILE  = '/home/ubuntu/.picoclaw/logs/apex-sentiment.json'
REGIME_FILE     = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
MULTIFRAME_FILE = '/home/ubuntu/.picoclaw/logs/apex-multiframe.json'

# Minimum data needed to bother asking Gemini
_MIN_FRACTION = 0.25
_MAX_FRACTION = 0.75


def _instrument_news(name: str) -> str:
    """Pull recent news snippet for the instrument."""
    try:
        sent = safe_read(SENTIMENT_FILE, {})
        inst = (sent.get('instrument_scores', {}).get(name) or
                sent.get('instrument_scores', {}).get(name.upper()))
        if not inst:
            return 'no recent instrument news'
        label    = inst.get('label', 'NEUTRAL')
        score    = inst.get('sentiment', 0)
        titles   = [h.get('title', '') if isinstance(h, dict) else str(h)
                    for h in inst.get('headlines', [])[:3]]
        snippet  = ' | '.join(t[:70] for t in titles if t)
        return f"{label} ({score:+.2f})" + (f" — {snippet}" if snippet else '')
    except Exception:
        return 'sentiment unavailable'


def _weekly_trend(name: str) -> str:
    """Pull weekly trend class from multi-timeframe cache."""
    try:
        mtf  = safe_read(MULTIFRAME_FILE, {})
        inst = mtf.get('data', {}).get(name.upper(), {})
        w    = inst.get('weekly', {})
        return w.get('trend_class', 'UNKNOWN') if w else 'UNKNOWN'
    except Exception:
        return 'UNKNOWN'


def get_exit_fraction(position: dict, base_fraction: float) -> tuple[float, str]:
    """
    Advise on partial-close fraction at T1.

    Args:
        position:      position dict from apex-positions.json
        base_fraction: fraction already determined by Sortino/trajectory logic

    Returns:
        (fraction, reason)
    """
    if not get_llm_flag('exit_timing_llm'):
        return base_fraction, 'flag_disabled'

    name        = position.get('name', position.get('ticker', '?'))
    entry       = float(position.get('entry', 0))
    current     = float(position.get('current', entry))
    target1     = float(position.get('target1', 0))
    target2     = float(position.get('target2', 0))
    stop        = float(position.get('stop', entry * 0.94))
    signal_type = position.get('signal_type', 'TREND')
    sector      = position.get('sector', 'UNKNOWN')

    if not entry or not target1:
        return base_fraction, 'not_applicable'

    r_current = round((current - entry) / (entry - stop), 2) if entry != stop else 0
    r_to_t2   = round((target2 - current) / (entry - stop), 2) if entry != stop and target2 else 0

    news         = _instrument_news(name)
    weekly_trend = _weekly_trend(name)

    try:
        regime = safe_read(REGIME_FILE, {})
        regime_label = regime.get('regime_label', regime.get('regime', 'NEUTRAL'))
        vix          = regime.get('vix', '?')
    except Exception:
        regime_label, vix = 'NEUTRAL', '?'

    try:
        from apex_llm_flags import call_gemini_json

        pct_base     = int(base_fraction * 100)
        track_record = get_module_performance('exit_timing', last_n=20)
        preamble     = build_regime_preamble()
        track_str    = (track_record + '\n') if track_record else ''

        prompt = (
            preamble +
            track_str +
            'You are an exit timing advisor for an automated trading system. '
            'A position has just hit Target 1 and a partial close is being executed.\n\n'
            f'Position details:\n'
            f'  Instrument: {name} (sector: {sector}, type: {signal_type})\n'
            f'  Entry: £{entry} | Current: £{current} | T1: £{target1} | T2: £{target2}\n'
            f'  R achieved: {r_current}R | Remaining R to T2: {r_to_t2}R\n\n'
            f'Market context:\n'
            f'  Regime: {regime_label} | VIX: {vix}\n'
            f'  Weekly trend (institutional): {weekly_trend}\n'
            f'  Recent news: {news}\n\n'
            f'Current plan: take {pct_base}% off the table now (Sortino-based model).\n\n'
            'Your task: recommend a partial-close fraction between 25% and 75%.\n\n'
            'Take MORE profit now (higher fraction, toward 75%) if:\n'
            '  • Negative news about the instrument suggests the rally may fade\n'
            '  • Regime is CAUTIOUS or HOSTILE (less likely to reach T2)\n'
            '  • Weekly trend is BEAR (counter-trend rally, limited upside)\n'
            '  • T2 is very far away (high R remaining) relative to current momentum\n\n'
            'Let it run (lower fraction, toward 25%) if:\n'
            '  • Positive news, strong momentum, clear runway to T2\n'
            '  • Weekly trend is STRONG_BULL confirming institutional support\n'
            '  • Regime is FAVOURABLE and VIX is low\n'
            '  • EARNINGS_DRIFT signal type (post-earnings drift tends to continue)\n\n'
            'Return ONLY valid JSON — no markdown:\n'
            '{"fraction": 0.50, "reason": "one-line explanation"}\n'
            'fraction must be between 0.25 and 0.75 inclusive.'
        )

        result = call_gemini_json(prompt)
        fraction = float(result.get('fraction', base_fraction))
        reason   = str(result.get('reason', ''))[:120]

        # Clamp to safe range
        fraction = round(max(_MIN_FRACTION, min(_MAX_FRACTION, fraction)), 2)

        moved = abs(fraction - base_fraction) >= 0.05
        record_llm_call('exit_timing_llm', used_llm=True,
                        result_summary=f"{int(fraction*100)}% moved={moved}")
        log_info(f"Exit timing [{name}]: {int(base_fraction*100)}% → {int(fraction*100)}% — {reason}")
        return fraction, reason

    except Exception as _e:
        log_warning(f"Exit timing LLM failed (fail-open, keeping base {int(base_fraction*100)}%): {_e}")
        record_llm_call('exit_timing_llm', used_llm=False,
                        result_summary=f'error:{type(_e).__name__}')
        return base_fraction, f'timing_error: {type(_e).__name__}'


if __name__ == '__main__':
    test_pos = {
        'name': 'AAPL', 'signal_type': 'TREND', 'sector': 'TECH',
        'entry': 185.0, 'current': 196.0, 'stop': 177.0,
        'target1': 195.0, 'target2': 207.0,
    }
    fraction, reason = get_exit_fraction(test_pos, 0.50)
    print(f"fraction={fraction:.0%}  reason={reason}")
