#!/usr/bin/env python3
"""
LLM Pre-Entry Falling Knife Filter
Checks a CONTRARIAN signal against recent headlines before execution.

Distinguishes genuine oversold bounces (macro selloff, temporary sentiment)
from falling knives (earnings collapse, fraud, structural decline).

Uses the thinking-tier LLM (Claude Extended Thinking or Gemini Pro) for
higher-quality reasoning on this high-stakes binary decision.

check_preflight(signal, intel) -> (allow: bool, reason: str)

Always fail-open: any exception returns (True, 'preflight_error') so an
LLM failure never silently blocks a valid trade.
"""
import json
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import safe_read, log_warning, log_info
    from apex_llm_flags import get_llm_flag, record_llm_call, call_llm_thinking, build_regime_preamble
    from apex_llm_ab_tracker import record_decision as _record_ab, get_module_performance
except ImportError as _e:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m):    print(f'INFO: {m}')
    def get_llm_flag(n): return True
    def record_llm_call(*a, **k): pass
    def call_llm_thinking(p, **k): raise RuntimeError('apex_llm_flags not available')
    def _record_ab(*a, **k): pass
    def build_regime_preamble(): return ''
    def get_module_performance(m, **k): return ''

SENTIMENT_FILE = '/home/ubuntu/.picoclaw/logs/apex-sentiment.json'
GEO_FILE       = '/home/ubuntu/.picoclaw/logs/apex-geo-news.json'


def _get_headlines_for_instrument(name: str) -> tuple[list[str], bool]:
    """Pull the most recent headlines for this instrument from sentiment cache.
    Returns (headlines, is_stale) — is_stale=True if cache is >90 min old."""
    headlines = []
    is_stale  = False

    sent = safe_read(SENTIMENT_FILE, {})

    # Check cache freshness
    from datetime import datetime, timezone
    ts_str = sent.get('timestamp', '')
    if ts_str:
        try:
            cache_time = datetime.strptime(ts_str, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - cache_time).total_seconds() / 60
            if age_min > 90:
                is_stale = True
                log_warning(f"Preflight: sentiment cache is {int(age_min)}m old — headlines may be stale")
        except Exception:
            pass

    # Primary: per-instrument headlines from apex-sentiment.json
    inst_scores = sent.get('instrument_scores', {})
    inst_data = inst_scores.get(name) or inst_scores.get(name.upper())
    if inst_data:
        for h in inst_data.get('headlines', []):
            t = h.get('title', '') if isinstance(h, dict) else str(h)
            if t:
                headlines.append(t[:120])

    # Secondary: top/worst market headlines for macro context
    for key in ('top_headlines', 'worst_headlines'):
        for h in sent.get(key, [])[:3]:
            t = h.get('title', '') if isinstance(h, dict) else str(h)
            if t and t not in headlines:
                headlines.append(f"[market] {t[:100]}")

    # Tertiary: geo flags from apex-geo-news.json (often relevant for contrarians)
    geo = safe_read(GEO_FILE, {})
    for flag in geo.get('geo_flags', [])[:3]:
        t = flag.get('title', '')
        if t and t not in headlines:
            headlines.append(f"[geo] {t[:100]}")

    return headlines[:20], is_stale


def check_preflight(signal: dict, intel: dict) -> tuple[bool, str]:
    """
    Pre-entry falling knife filter for CONTRARIAN signals.

    Returns:
        (True,  reason)  — allow trade
        (False, reason)  — block trade (falling knife detected)

    Always fail-open: exceptions return (True, 'preflight_error').
    """
    signal_type = signal.get('signal_type', '')
    if signal_type not in ('CONTRARIAN', 'GEO_REVERSAL'):
        return True, 'not_applicable'

    if not get_llm_flag('preflight_llm'):
        return True, 'flag_disabled'

    name    = signal.get('name', signal.get('ticker', '?'))
    entry   = signal.get('entry', 0)
    stop    = signal.get('stop',  0)
    rsi     = signal.get('rsi',   0)
    sector  = signal.get('sector', 'UNKNOWN')
    adj     = signal.get('adjustments', [])

    # Market context from intel — critical for distinguishing company-specific
    # collapse from market-wide selloff (which is exactly when contrarians fire)
    vix      = intel.get('vix', '?')
    breadth  = intel.get('breadth', '?')
    regime   = intel.get('regime_label', intel.get('regime_status', 'UNKNOWN'))
    discount = signal.get('discount', '?')  # % off 52-week high if available

    headlines, headlines_stale = _get_headlines_for_instrument(name)

    try:
        headline_text = '\n'.join(f'- {h}' for h in headlines) if headlines else '(no recent headlines found)'
        adj_text      = '\n'.join(f'- {a}' for a in adj[:5]) if adj else '(none)'
        stale_note    = ' (STALE — >90 min old, may miss recent news)' if headlines_stale else ''

        # Prepend regime context and self-calibrating track record
        regime_preamble  = build_regime_preamble()
        track_record     = get_module_performance('preflight', last_n=20)
        track_record_str = (f'\n{track_record}\n') if track_record else ''

        prompt = (
            regime_preamble +
            track_record_str +
            'You are a pre-entry risk filter for an automated contrarian trading system. '
            'A contrarian signal fires when a stock is oversold (low RSI). '
            'Your job is to detect FALLING KNIVES — stocks falling due to fundamental '
            'problems that will keep declining. You must NOT block genuine bounces.\n\n'
            f'Signal details:\n'
            f'  Instrument: {name} (sector: {sector})\n'
            f'  Entry: £{entry} | Stop: £{stop} | RSI: {rsi}\n'
            f'  Discount from 52w high: {discount}%\n'
            f'  Scoring context:\n{adj_text}\n\n'
            f'Market context (CRITICAL — use this to distinguish company vs market selloff):\n'
            f'  VIX: {vix} | Market breadth: {breadth}% | Regime: {regime}\n'
            f'  If VIX > 25 and breadth < 40%, the market itself is selling off — '
            f'an oversold blue-chip is likely a GENUINE BOUNCE, not a falling knife.\n\n'
            f'Recent headlines{stale_note}:\n{headline_text}\n\n'
            'FALLING KNIFE (block) — company-specific deterioration:\n'
            '  • Earnings collapse, guidance cut, revenue miss with no recovery path\n'
            '  • Fraud, accounting scandal, or SEC/FCA investigation\n'
            '  • CEO/CFO departure amid a crisis (not planned succession)\n'
            '  • Structural business model failure (not cyclical)\n'
            '  • Debt crisis, covenant breach, or bankruptcy risk\n'
            '  • Regulatory action that permanently impairs revenue\n\n'
            'GENUINE BOUNCE (allow) — these are NOT falling knives:\n'
            '  • Broad market selloff (high VIX, low breadth) dragging the stock down\n'
            '  • Temporary negative sentiment, tariff/geopolitical noise, sector rotation\n'
            '  • No company-specific bad news — only macro/market headlines present\n'
            '  • Minor news that does not impair long-term fundamentals\n'
            '  • Stock down WITH its sector (cyclical, not structural)\n\n'
            'IMPORTANT: Default to ALLOW unless you find clear company-specific evidence.\n'
            'Headlines about the broader market, politics, or other companies are NOT grounds to block.\n\n'
            'Return ONLY valid JSON:\n'
            '{"allow": true, "reason": "one-line explanation", "risk_level": "LOW"}\n'
            'risk_level: LOW (clear bounce) / MEDIUM (uncertain) / HIGH (falling knife)'
        )

        # Thinking-tier call — Claude/Gemini Pro with extended reasoning
        # Higher budget for this call: it's binary, high-stakes, and rare
        result     = call_llm_thinking(prompt, module='preflight', budget_tokens=3000)
        allow      = bool(result.get('allow', True))
        reason     = str(result.get('reason', ''))[:120]
        risk_level = result.get('risk_level', 'MEDIUM')

        # A/B tracking — baseline always allows (fail-open rule)
        baseline = 'ALLOW'
        llm_dec  = 'ALLOW' if allow else 'BLOCK'
        _record_ab('preflight', name, llm_dec, baseline,
                   llm_reason=f"risk={risk_level} {reason}")

        record_llm_call('preflight_llm', used_llm=True,
                        result_summary=f"allow={allow} risk={risk_level}")
        log_info(f"Preflight [{name}]: headlines={len(headlines)} stale={headlines_stale} "
                 f"allow={allow} risk={risk_level} — {reason}")
        return allow, f"{risk_level}: {reason}"

    except Exception as _e:
        log_warning(f"Preflight check failed (fail-open): {_e}")
        record_llm_call('preflight_llm', used_llm=False, result_summary=f'error:{type(_e).__name__}')
        return True, f'preflight_error: {type(_e).__name__}'


if __name__ == '__main__':
    # Quick smoke-test
    test_signal = {
        'name': 'ULVR', 'signal_type': 'CONTRARIAN',
        'entry': 42.5, 'stop': 40.0, 'rsi': 22.3,
        'sector': 'CONSUMER_STAPLES',
        'adjustments': ['Weekly BULL confirms pullback', 'RSI oversold <25'],
    }
    test_intel = {'vix': 28, 'breadth': 38, 'regime_status': 'CAUTIOUS'}
    allow, reason = check_preflight(test_signal, test_intel)
    print(f"allow={allow}  reason={reason}")
