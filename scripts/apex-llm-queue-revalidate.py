#!/usr/bin/env python3
"""
LLM Queue Revalidation
Runs at 07:58 UTC Mon-Fri — after intelligence refresh, before 08:05 execution.

For each QUEUED signal, asks the thinking model:
  "Given overnight developments, is this signal still valid?"

Can CANCEL signals whose thesis has been broken by overnight news.
Preserves all signals whose thesis is intact.

Complements (does not replace) the rule-based apex-queue-revalidate.py which
runs only on Mondays and checks price gaps / regime shifts mechanically.
This LLM layer adds qualitative news judgment every day.

Flag: queue_revalidate_llm
Output: updates apex-trade-queue.json status + appends to apex-decision-log.json
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import (atomic_write, safe_read, locked_read_modify_write,
                             log_warning, log_info, send_telegram)
    from apex_llm_flags import get_llm_flag, record_llm_call, call_llm_thinking
except ImportError as _e:
    print(f"FATAL: import failed: {_e}")
    sys.exit(1)

LOGS           = '/home/ubuntu/.picoclaw/logs'
QUEUE_FILE     = f'{LOGS}/apex-trade-queue.json'
DECISION_LOG   = f'{LOGS}/apex-decision-log.json'
SENTIMENT_FILE = f'{LOGS}/apex-sentiment.json'
GEO_FILE       = f'{LOGS}/apex-geo-news.json'
REGIME_FILE    = f'{LOGS}/apex-regime-scaling.json'


def _get_overnight_context() -> str:
    """Build a concise overnight context string for the prompt."""
    sent   = safe_read(SENTIMENT_FILE, {})
    geo    = safe_read(GEO_FILE, {})
    regime = safe_read(REGIME_FILE, {})

    sent_class  = sent.get('market_class', 'NEUTRAL')
    sent_score  = sent.get('market_sentiment', 0)
    geo_overall = geo.get('overall', 'CLEAR')
    hmm         = regime.get('hmm_state', 'UNKNOWN')
    reg_label   = regime.get('regime_label', 'NEUTRAL')

    top_hls = [h.get('title', '') if isinstance(h, dict) else str(h)
               for h in sent.get('top_headlines', [])[:3]]
    worst   = [h.get('title', '') if isinstance(h, dict) else str(h)
               for h in sent.get('worst_headlines', [])[:3]]
    geo_flags = [f.get('title', '') for f in geo.get('geo_flags', [])[:3]]

    lines = [
        f"Market sentiment: {sent_class} ({sent_score:+.2f})",
        f"Regime: {reg_label} (HMM: {hmm})",
        f"Geo: {geo_overall}",
    ]
    if top_hls:
        lines.append(f"Positive headlines: {' | '.join(t[:80] for t in top_hls if t)}")
    if worst:
        lines.append(f"Negative headlines: {' | '.join(t[:80] for t in worst if t)}")
    if geo_flags:
        lines.append(f"Geo alerts: {' | '.join(t[:80] for t in geo_flags if t)}")

    return '\n'.join(lines)


def _revalidate_signals(queued_signals: list) -> list:
    """
    Run LLM revalidation on all queued signals in a single call.
    Returns list of {name, action, reason} dicts.
    """
    if not queued_signals:
        return []

    overnight = _get_overnight_context()
    today = datetime.now(timezone.utc).strftime('%A %d %B %Y')

    signal_blocks = ''
    for i, s in enumerate(queued_signals):
        name        = s.get('name', '?')
        stype       = s.get('signal_type', 'TREND')
        score       = s.get('adjusted_score', 0)
        entry       = s.get('entry', 0)
        stop        = s.get('stop', 0)
        sector      = s.get('sector', 'UNKNOWN')
        queued_at   = s.get('queued_at', '')[:16]
        adjs        = s.get('adjustments', [])
        adj_str     = '; '.join(str(a)[:60] for a in adjs[:3]) if adjs else 'none'
        signal_blocks += (
            f"[{i+1}] {name} ({stype}, {sector}) — score {score}\n"
            f"   Entry £{entry} | Stop £{stop} | Queued: {queued_at}\n"
            f"   Thesis: {adj_str}\n"
        )

    prompt = (
        f"You are reviewing queued trading signals for today ({today}) before market open.\n"
        f"The signals were queued previously. Overnight developments may have changed the picture.\n\n"
        f"OVERNIGHT CONTEXT:\n{overnight}\n\n"
        f"QUEUED SIGNALS TO REVIEW:\n{signal_blocks}\n"
        f"For each signal, decide: PROCEED (thesis intact) or CANCEL (thesis broken).\n\n"
        f"CANCEL only if:\n"
        f"  • Overnight news directly and materially undermines the entry thesis\n"
        f"  • Company-specific bad news for a TREND/EARNINGS signal\n"
        f"  • Regime has shifted severely AGAINST the signal type\n"
        f"    (e.g. CONTRARIAN signal but market is now strongly trending up = CANCEL)\n"
        f"  • A geo/macro event has removed the catalyst for this trade\n\n"
        f"DO NOT CANCEL if:\n"
        f"  • There is only general market noise with no signal-specific impact\n"
        f"  • The signal is CONTRARIAN and market sold off more overnight (thesis stronger)\n"
        f"  • The change is minor and the risk/reward still makes sense\n\n"
        f"Return ONLY a JSON array, one entry per signal:\n"
        f'[{{"name": "ULVR", "action": "PROCEED", "reason": "thesis intact, no news"}}, ...]'
    )

    return call_llm_thinking(prompt, module='queue_revalidate', budget_tokens=2048)


def run():
    if not get_llm_flag('queue_revalidate_llm'):
        log_info("Queue revalidate: flag disabled — skipping")
        record_llm_call('queue_revalidate_llm', used_llm=False, result_summary='flag_disabled')
        return

    queue_data = safe_read(QUEUE_FILE, {})
    if not isinstance(queue_data, dict):
        queue_data = {}

    all_entries = queue_data.get('queue', [])
    queued      = [e for e in all_entries if e.get('status') == 'QUEUED']

    if not queued:
        log_info("Queue revalidate: no QUEUED signals — nothing to check")
        record_llm_call('queue_revalidate_llm', used_llm=False, result_summary='no_signals')
        return

    log_info(f"Queue revalidate: checking {len(queued)} queued signal(s) via LLM")

    try:
        raw = _revalidate_signals(queued)

        # Normalise — expect a list
        if isinstance(raw, dict):
            # Some models wrap the array
            decisions = raw.get('signals', raw.get('results', [raw]))
        elif isinstance(raw, list):
            decisions = raw
        else:
            decisions = []

        # Build lookup
        decision_map = {d.get('name', ''): d for d in decisions if isinstance(d, dict)}

        cancelled = []
        proceeded = []

        def _update_queue(data):
            if not isinstance(data, dict):
                return data
            for entry in data.get('queue', []):
                if entry.get('status') != 'QUEUED':
                    continue
                name = entry.get('name', '')
                dec  = decision_map.get(name, {})
                action = str(dec.get('action', 'PROCEED')).upper()
                reason = str(dec.get('reason', 'no_llm_decision'))[:120]

                if action == 'CANCEL':
                    entry['status']       = 'CANCELLED'
                    entry['cancel_reason'] = f"LLM revalidation: {reason}"
                    entry['cancelled_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    cancelled.append(f"{name}: {reason}")
                else:
                    proceeded.append(name)
            return data

        locked_read_modify_write(QUEUE_FILE, _update_queue, default={})

        summary = f"proceed={len(proceeded)} cancel={len(cancelled)}"
        record_llm_call('queue_revalidate_llm', used_llm=True, result_summary=summary)
        log_info(f"Queue revalidate done: {summary}")

        if cancelled:
            msg = (
                f"🔄 LLM Queue Revalidation\n"
                f"✅ Proceeding: {', '.join(proceeded) or 'none'}\n"
                f"❌ Cancelled ({len(cancelled)}):\n"
            )
            for c in cancelled:
                msg += f"  • {c}\n"
            send_telegram(msg)
        else:
            log_info(f"Queue revalidate: all {len(proceeded)} signal(s) cleared for execution")

    except Exception as _e:
        log_warning(f"Queue revalidate LLM failed (fail-open — all signals proceed): {_e}")
        record_llm_call('queue_revalidate_llm', used_llm=False,
                        result_summary=f'error:{type(_e).__name__}')


if __name__ == '__main__':
    run()
