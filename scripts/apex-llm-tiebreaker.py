#!/usr/bin/env python3
"""
LLM Signal Tiebreaker
When the top qualified signals are within TIEBREAK_THRESHOLD points of each other,
Gemini re-ranks them based on qualitative context — current news, macro backdrop,
setup clarity — that pure numeric scoring cannot see.

rerank_signals(signals, intel) -> list[signal]

Always fail-open: any exception returns the original order unchanged.
Only fires when signal_tiebreaker_llm flag is ON and signals are genuinely tied.
"""
import json
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import safe_read, log_warning, log_info
    from apex_llm_flags import get_llm_flag, record_llm_call
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m):    print(f'INFO: {m}')
    def get_llm_flag(n): return True
    def record_llm_call(*a, **k): pass

SENTIMENT_FILE     = '/home/ubuntu/.picoclaw/logs/apex-sentiment.json'
REGIME_FILE        = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
TIEBREAK_THRESHOLD = 1.0   # re-rank when top signals are within 1.0 pts of each other
MAX_CANDIDATES     = 5     # pass at most this many signals to Gemini


def _effective_score(signal: dict) -> float:
    return signal.get('adjusted_score', 0) + signal.get('regime_priority_bonus', 0.0)


def _instrument_sentiment_summary(name: str) -> str:
    """Pull a one-line sentiment note for this instrument from the sentiment cache."""
    try:
        sent  = safe_read(SENTIMENT_FILE, {})
        inst  = sent.get('instrument_scores', {}).get(name) or \
                sent.get('instrument_scores', {}).get(name.upper())
        if not inst:
            return 'no recent news'
        score  = inst.get('sentiment', 0)
        label  = inst.get('label', 'NEUTRAL')
        titles = [h.get('title', '') if isinstance(h, dict) else str(h)
                  for h in inst.get('headlines', [])[:2]]
        snippet = ' | '.join(t[:60] for t in titles if t)
        return f"{label} ({score:+.2f})" + (f" — {snippet}" if snippet else '')
    except Exception:
        return 'sentiment unavailable'


def _build_signal_summary(signal: dict) -> str:
    """One compact block describing a signal for the Gemini prompt."""
    name    = signal.get('name', '?')
    stype   = signal.get('signal_type', 'TREND')
    score   = _effective_score(signal)
    rsi     = signal.get('rsi', '?')
    sector  = signal.get('sector', 'UNKNOWN')
    entry   = signal.get('entry', 0)
    stop    = signal.get('stop', 0)
    risk_r  = round((float(entry) - float(stop)) / float(stop), 3) if stop else 0
    adj     = signal.get('adjustments', [])
    adj_str = '; '.join(str(a)[:60] for a in adj[:3]) if adj else 'none'
    sent    = _instrument_sentiment_summary(name)

    return (
        f"  Name: {name} | Type: {stype} | Score: {score:.2f} | RSI: {rsi}\n"
        f"  Sector: {sector} | Entry: £{entry} | Risk/R: {risk_r:.1%}\n"
        f"  Scoring context: {adj_str}\n"
        f"  Recent sentiment: {sent}"
    )


def rerank_signals(signals: list, intel: dict) -> list:
    """
    Re-rank the top qualified signals using Gemini when they are within
    TIEBREAK_THRESHOLD points of each other.

    Returns the reranked list (all signals, only the top slice reordered).
    On any failure returns the original order unchanged.
    """
    if not signals:
        return signals

    if not get_llm_flag('signal_tiebreaker_llm'):
        return signals

    # Only fire when genuinely tied — top two within threshold
    if len(signals) < 2:
        return signals

    top_score    = _effective_score(signals[0])
    second_score = _effective_score(signals[1])
    if abs(top_score - second_score) > TIEBREAK_THRESHOLD:
        log_info(f"Tiebreaker: scores spread {abs(top_score - second_score):.2f} > {TIEBREAK_THRESHOLD} — no rerank needed")
        record_llm_call('signal_tiebreaker_llm', used_llm=False, result_summary='spread_too_wide')
        return signals

    candidates   = signals[:MAX_CANDIDATES]
    rest         = signals[MAX_CANDIDATES:]

    try:
        from apex_llm_flags import call_gemini_json

        # Build regime context snippet with HMM state for signal priority guidance
        regime_label  = intel.get('regime_label', intel.get('regime_status', 'UNKNOWN'))
        hmm_state     = intel.get('hmm_state', 'UNKNOWN')
        vix           = intel.get('vix', '?')
        breadth       = intel.get('breadth', '?')
        direction     = intel.get('direction_status', '?')
        taco_status   = intel.get('taco_status', 'NEUTRAL')

        # Signal type priority order by HMM state (matches decision engine priority matrix)
        _priority_map = {
            'TRENDING':       'TREND > EARNINGS_DRIFT > DIVIDEND_CAPTURE > CONTRARIAN > INVERSE',
            'MEAN_REVERTING': 'CONTRARIAN > INVERSE > DIVIDEND_CAPTURE > EARNINGS_DRIFT > TREND',
            'CRISIS':         'INVERSE > CONTRARIAN > DIVIDEND_CAPTURE > EARNINGS_DRIFT > TREND',
        }
        priority_note = _priority_map.get(hmm_state, '')
        priority_str  = f'\n  Signal type priority for {hmm_state} regime: {priority_note}' if priority_note else ''

        regime_ctx = (
            f"Market context: regime={regime_label} (HMM: {hmm_state}) | VIX={vix} | "
            f"breadth={breadth}% | direction={direction} | TACO={taco_status}"
            f"{priority_str}"
        )

        # Build signal blocks
        names         = [s.get('name', '?') for s in candidates]
        signal_blocks = '\n\n'.join(
            f"[{i+1}] {_build_signal_summary(s)}"
            for i, s in enumerate(candidates)
        )

        prompt = (
            'You are a signal selection judge for an automated trading system. '
            'These signals have passed all quantitative filters and have similar numeric scores. '
            'Your job is to rank them from best to worst based on:\n'
            '  1. Setup quality — cleaner entry, better risk/reward, RSI confirming the signal\n'
            '  2. News alignment — positive/neutral sentiment for trend signals; negative sentiment '
            'for contrarian signals confirms the oversold opportunity\n'
            '  3. Regime fit — which signal type fits the current market regime best\n'
            '  4. Sector momentum — leading sectors have a tailwind, lagging sectors face headwinds\n\n'
            f'{regime_ctx}\n\n'
            f'Signals to rank:\n\n{signal_blocks}\n\n'
            'Return ONLY a JSON array of names in ranked order (best first), '
            'plus a one-line reason for your top pick. '
            'Use EXACTLY the names as given — do not modify them.\n'
            f'Example format: {{"ranked": {json.dumps(names)}, "reason": "brief reason for #1"}}'
        )

        result = call_gemini_json(prompt)
        ranked     = result.get('ranked', [])
        reason     = str(result.get('reason', ''))[:120]

        # Validate — Gemini must return exactly our input names
        valid_names = {s.get('name', '?') for s in candidates}
        if not ranked or not all(n in valid_names for n in ranked):
            log_warning(f"Tiebreaker: Gemini returned unexpected names {ranked} — keeping original order")
            record_llm_call('signal_tiebreaker_llm', used_llm=False, result_summary='invalid_names')
            return signals

        # Build reordered candidates list — any signal not in ranked goes to the end
        name_to_signal = {s.get('name', '?'): s for s in candidates}
        reranked       = [name_to_signal[n] for n in ranked if n in name_to_signal]
        # Append any candidates Gemini omitted (shouldn't happen, but be safe)
        seen = set(ranked)
        reranked += [s for s in candidates if s.get('name', '?') not in seen]

        # Final guard: never return an empty list to the caller
        if not reranked:
            log_warning(f"Tiebreaker: reordering produced empty list — keeping original order")
            record_llm_call('signal_tiebreaker_llm', used_llm=False, result_summary='empty_rerank')
            return signals

        original_top = signals[0].get('name', '?')
        new_top      = reranked[0].get('name', '?')
        moved        = original_top != new_top

        record_llm_call('signal_tiebreaker_llm', used_llm=True,
                        result_summary=f"top={new_top} moved={moved}")
        log_info(f"Tiebreaker: ranked {[s.get('name') for s in reranked]} — {reason}")

        if moved:
            log_info(f"Tiebreaker RERANKED: {original_top} → {new_top} | {reason}")

        return reranked + rest

    except Exception as _e:
        log_warning(f"Tiebreaker failed (fail-open, keeping original order): {_e}")
        record_llm_call('signal_tiebreaker_llm', used_llm=False,
                        result_summary=f'error:{type(_e).__name__}')
        return signals


if __name__ == '__main__':
    # Smoke-test with two dummy signals
    dummy = [
        {'name': 'AAPL', 'signal_type': 'TREND',      'adjusted_score': 7.8,
         'regime_priority_bonus': 0.5, 'rsi': 58, 'sector': 'TECH',
         'entry': 195.0, 'stop': 185.0,
         'adjustments': ['Weekly BULL', 'MACD bullish', 'RS strong']},
        {'name': 'XOM',  'signal_type': 'TREND',      'adjusted_score': 7.6,
         'regime_priority_bonus': 0.5, 'rsi': 55, 'sector': 'ENERGY',
         'entry': 112.0, 'stop': 106.0,
         'adjustments': ['Weekly BULL', 'FRED macro positive', 'sector leading']},
        {'name': 'MSFT', 'signal_type': 'EARNINGS_DRIFT', 'adjusted_score': 7.4,
         'regime_priority_bonus': 0.2, 'rsi': 61, 'sector': 'TECH',
         'entry': 420.0, 'stop': 400.0,
         'adjustments': ['Earnings revision +', 'insider buying']},
    ]
    intel = {'regime_label': 'NEUTRAL', 'vix': 18, 'breadth': 55,
             'direction_status': 'UP', 'taco_status': 'NEUTRAL'}
    result = rerank_signals(dummy, intel)
    print("Ranked order:", [s['name'] for s in result])
