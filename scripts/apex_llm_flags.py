#!/usr/bin/env python3
"""
LLM Feature Flags
Runtime on/off gates for all LLM modules. Fail-open: if the flags file is
missing or unreadable, every flag defaults to its DEFAULTS value.

Usage:
    from apex_llm_flags import get_llm_flag, record_llm_call, call_llm_thinking
    if get_llm_flag('preflight_llm'):
        result = call_llm_thinking(prompt, module='preflight')
        record_llm_call('preflight_llm', used_llm=True, result_summary='ok')
    else:
        record_llm_call('preflight_llm', used_llm=False)

    # Fast-tier (Gemini Flash) — unchanged behaviour:
    result = call_gemini_json(prompt)

CLI:
    python3 apex_llm_flags.py status
    python3 apex_llm_flags.py set sentiment_llm false
    python3 apex_llm_flags.py reset
    python3 apex_llm_flags.py provider anthropic|gemini
    python3 apex_llm_flags.py budget
"""
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from datetime import datetime, timezone

try:
    from apex_utils import locked_read_modify_write, safe_read, log_warning
except ImportError:
    import json as _j

    def safe_read(p, default=None):
        try:
            with open(p) as f:
                return _j.load(f)
        except Exception:
            return default

    def log_warning(m):
        print(f'WARNING: {m}')

    def locked_read_modify_write(path, fn, default=None):
        import tempfile, os
        try:
            data = safe_read(path, default)
            data = fn(data)
            d = os.path.dirname(path)
            with tempfile.NamedTemporaryFile(mode='w', dir=d, delete=False, suffix='.tmp') as tf:
                _j.dump(data, tf, indent=2)
                tmp = tf.name
            os.replace(tmp, path)
        except Exception as e:
            print(f'ERROR: locked_read_modify_write failed: {e}')


FLAGS_FILE = '/home/ubuntu/.picoclaw/logs/apex-llm-flags.json'

# ── TTL cache for flag reads (avoids repeated file I/O on hot path) ──────────
import time as _time
_flag_cache = {}       # {flag_name: (bool, monotonic_ts)}
_FLAG_TTL   = 30       # seconds — flags change rarely, 30s is fine

KNOWN_FLAGS = {
    'sentiment_llm',
    'taco_llm',
    'preflight_llm',
    'exit_timing_llm',
    'signal_tiebreaker_llm',
    'morning_brief_llm',
    'queue_revalidate_llm',
    'drawdown_review_llm',
    'portfolio_agent_llm',
}

# Default enabled state:
#   Fast-tier modules (sentiment, taco): ON — cheap, already battle-tested
#   Thinking-tier modules: OFF until explicitly enabled (budget awareness)
#   New modules: OFF until tested
_DEFAULTS = {
    'sentiment_llm':         True,
    'taco_llm':              True,
    'preflight_llm':         False,
    'exit_timing_llm':       False,
    'signal_tiebreaker_llm': False,
    'morning_brief_llm':     False,
    'queue_revalidate_llm':  False,
    'drawdown_review_llm':   False,
    'portfolio_agent_llm':   False,
}

_FLAG_LABELS = {
    'sentiment_llm':         'Sentiment (fast)',
    'taco_llm':              'TACO classifier (thinking)',
    'preflight_llm':         'Pre-entry filter (thinking)',
    'exit_timing_llm':       'Exit timing (fast)',
    'signal_tiebreaker_llm': 'Signal tiebreaker (thinking)',
    'morning_brief_llm':     'Morning brief (thinking)',
    'queue_revalidate_llm':  'Queue revalidation (thinking)',
    'drawdown_review_llm':   'Drawdown review (thinking)',
    'portfolio_agent_llm':   'Portfolio agent (thinking)',
}


def _load() -> dict:
    data = safe_read(FLAGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    return data


def get_llm_flag(name: str) -> bool:
    """Return True if the named LLM module is enabled. Fail-open on any error.
    Uses a 30-second TTL cache to avoid repeated file reads on the hot path."""
    if name not in KNOWN_FLAGS:
        log_warning(f"apex_llm_flags: unknown flag '{name}' — defaulting ON")
        return True
    try:
        # Check TTL cache first
        cached = _flag_cache.get(name)
        if cached and (_time.monotonic() - cached[1]) < _FLAG_TTL:
            return cached[0]
        flag_data = _load().get(name, {})
        if isinstance(flag_data, dict):
            result = bool(flag_data.get('enabled', _DEFAULTS.get(name, True)))
        else:
            result = _DEFAULTS.get(name, True)
        _flag_cache[name] = (result, _time.monotonic())
        return result
    except Exception as _e:
        log_warning(f"apex_llm_flags: get_llm_flag({name}) failed ({_e}) — defaulting ON")
        return True


def set_llm_flag(name: str, enabled: bool, updated_by: str = 'telegram') -> str:
    """Enable or disable a named LLM module. Returns a Telegram-ready status string."""
    if name not in KNOWN_FLAGS:
        return f"Unknown flag: {name}\n\nKnown flags: {', '.join(sorted(KNOWN_FLAGS))}"

    def _update(data):
        if not isinstance(data, dict):
            data = {}
        if name not in data or not isinstance(data[name], dict):
            data[name] = {
                'enabled': _DEFAULTS.get(name, False),
                'call_count': 0,
                'fallback_call_count': 0,
                'last_used': None,
                'last_result_summary': None,
            }
        data[name]['enabled']    = enabled
        data[name]['updated_by'] = updated_by
        data[name]['updated_at'] = datetime.now(timezone.utc).isoformat()
        return data

    locked_read_modify_write(FLAGS_FILE, _update, default={})
    _flag_cache.pop(name, None)  # invalidate cache
    state = 'ON' if enabled else 'OFF'
    label = _FLAG_LABELS.get(name, name)
    icon  = '✅' if enabled else '❌'
    return f"{icon} {label}: {state}"


def record_llm_call(name: str, used_llm: bool, result_summary: str = None):
    """Increment call counters and record last result. Non-blocking — never raises."""
    if name not in KNOWN_FLAGS:
        return
    try:
        def _update(data):
            if not isinstance(data, dict):
                data = {}
            if name not in data or not isinstance(data[name], dict):
                data[name] = {
                    'enabled': _DEFAULTS.get(name, False),
                    'call_count': 0,
                    'fallback_call_count': 0,
                    'last_used': None,
                    'last_result_summary': None,
                }
            key = 'call_count' if used_llm else 'fallback_call_count'
            data[name][key] = data[name].get(key, 0) + 1
            data[name]['last_used'] = datetime.now(timezone.utc).isoformat()
            if result_summary is not None:
                data[name]['last_result_summary'] = str(result_summary)[:100]
            return data
        locked_read_modify_write(FLAGS_FILE, _update, default={})
    except Exception as _e:
        log_warning(f"apex_llm_flags: record_llm_call failed (non-blocking): {_e}")


def set_all_flags(enabled: bool, updated_by: str = 'telegram') -> str:
    """Enable or disable every known LLM module at once."""
    def _update(data):
        if not isinstance(data, dict):
            data = {}
        now = datetime.now(timezone.utc).isoformat()
        for name in KNOWN_FLAGS:
            if name not in data or not isinstance(data[name], dict):
                data[name] = {
                    'enabled': _DEFAULTS.get(name, False),
                    'call_count': 0,
                    'fallback_call_count': 0,
                    'last_used': None,
                    'last_result_summary': None,
                }
            data[name]['enabled']    = enabled
            data[name]['updated_by'] = updated_by
            data[name]['updated_at'] = now
        return data

    locked_read_modify_write(FLAGS_FILE, _update, default={})
    _flag_cache.clear()  # invalidate all cached flags
    state = 'ON' if enabled else 'OFF'
    icon  = '✅' if enabled else '❌'
    names = '\n'.join(f"  {icon} {_FLAG_LABELS.get(f, f)}" for f in sorted(KNOWN_FLAGS))
    return f"All LLM modules {state}:\n{names}"


def get_all_flags() -> dict:
    """Return full flag state dict."""
    return _load()


def format_status_message() -> str:
    """Format a Telegram-ready status block for all flags, provider, and budget."""
    data  = _load()
    lines = ['🤖 LLM MODULE FLAGS\n']

    # Provider + budget header
    try:
        from apex_llm_client import get_provider, _thinking_model_name
        from apex_llm_cost_tracker import get_daily_total, get_mtd_total
        from apex_config import LLM_DAILY_BUDGET_USD
        provider    = get_provider()
        model       = _thinking_model_name(provider)
        daily       = get_daily_total()
        mtd         = get_mtd_total()
        budget_pct  = daily / LLM_DAILY_BUDGET_USD * 100 if LLM_DAILY_BUDGET_USD else 0
        bicon       = '✅' if budget_pct < 60 else ('⚠️' if budget_pct < 90 else '🚨')
        picon       = '🧠' if provider == 'anthropic' else '🔷'
        lines.append(f"{picon} Provider: {provider.upper()} ({model})")
        lines.append(f"{bicon} Budget: ${daily:.4f}/${LLM_DAILY_BUDGET_USD:.2f} today | MTD ${mtd:.4f}\n")
    except Exception:
        pass

    for flag in sorted(KNOWN_FLAGS):
        flag_data = data.get(flag, {})
        if isinstance(flag_data, dict):
            enabled = flag_data.get('enabled', _DEFAULTS.get(flag, False))
            calls   = flag_data.get('call_count', 0)
            fallbk  = flag_data.get('fallback_call_count', 0)
            last    = (flag_data.get('last_used') or '')[:16].replace('T', ' ')
        else:
            enabled = _DEFAULTS.get(flag, False)
            calls = fallbk = 0
            last  = ''
        icon  = '✅' if enabled else '❌'
        label = _FLAG_LABELS.get(flag, flag)
        lines.append(f"{icon} {label}")
        lines.append(f"   Calls: {calls} LLM | {fallbk} fallback | Last: {last or 'never'}")
    lines.append('\nLLM ON/OFF <flag> | LLM PROVIDER <anthropic|gemini> | LLM BUDGET')
    return '\n'.join(lines)


# ============================================================
# REGIME PREAMBLE — prepend to every LLM prompt for context
# ============================================================

_LOGS = '/home/ubuntu/.picoclaw/logs'


def build_regime_preamble() -> str:
    """
    Return a concise regime-conditioned preamble for LLM prompts.
    Reads the current regime, HMM state, VIX, breadth, and market hours.
    Non-blocking — returns empty string on any failure so callers are unaffected.

    Usage:
        prompt = build_regime_preamble() + '\\n' + your_prompt
    """
    try:
        import json as _json

        def _r(fname, default=None):
            try:
                with open(f'{_LOGS}/{fname}') as _f:
                    return _json.load(_f)
            except Exception:
                return default or {}

        regime   = _r('apex-regime.json')
        scaling  = _r('apex-regime-scaling.json')
        calendar = _r('apex-market-calendar.json')
        cb       = _r('apex-circuit-breaker.json')
        draw     = _r('apex-drawdown.json')

        overall      = regime.get('overall', 'UNKNOWN')
        vix          = regime.get('vix', '?')
        breadth      = regime.get('breadth_pct', '?')
        hmm_state    = scaling.get('hmm_state', 'UNKNOWN')
        regime_label = scaling.get('regime_label', 'NEUTRAL')
        cb_status    = cb.get('status', 'NORMAL')
        dd_status    = draw.get('status', 'NORMAL')
        dd_pct       = draw.get('drawdown_pct', 0)

        today        = calendar.get('today', {})
        us_open      = today.get('us_currently_open', False)
        uk_open      = today.get('uk_currently_open', False)
        markets      = ', '.join(filter(None, [
            'NYSE/NASDAQ OPEN' if us_open else '',
            'LSE OPEN'         if uk_open else '',
        ])) or 'ALL MARKETS CLOSED'

        # Signal type priority by HMM state — mirrors decision engine priority matrix
        _priority_map = {
            'TRENDING':       'TREND > EARNINGS_DRIFT > DIVIDEND_CAPTURE > CONTRARIAN > INVERSE',
            'MEAN_REVERTING': 'CONTRARIAN > INVERSE > DIVIDEND_CAPTURE > EARNINGS_DRIFT > TREND',
            'CRISIS':         'INVERSE > CONTRARIAN > DIVIDEND_CAPTURE > EARNINGS_DRIFT > TREND',
        }
        priority = _priority_map.get(hmm_state, '')

        lines = [
            '╔══ LIVE MARKET CONTEXT (read before reasoning) ══╗',
            f'  Regime: {overall} | Label: {regime_label} | HMM: {hmm_state}',
            f'  VIX: {vix} | Breadth: {breadth}% | Markets: {markets}',
        ]
        if cb_status not in ('NORMAL', 'CLEAR', ''):
            lines.append(f'  ⚠️  Circuit breaker: {cb_status}')
        if dd_status != 'NORMAL':
            lines.append(f'  ⚠️  Drawdown: {dd_status} ({dd_pct:.1f}%)')
        if priority:
            lines.append(f'  Signal priority ({hmm_state}): {priority}')
        lines.append('╚═════════════════════════════════════════════════╝')
        lines.append('')
        return '\n'.join(lines)

    except Exception:
        return ''


# ============================================================
# THINKING-TIER WRAPPER — delegates to apex_llm_client
# ============================================================

def call_llm_thinking(prompt: str, module: str = 'unknown', budget_tokens: int = None) -> dict:
    """
    Call the configured thinking-tier LLM (Claude or Gemini Pro).
    Wrapper around apex_llm_client.call_llm_thinking — import here for
    convenience so all LLM modules only need to import apex_llm_flags.

    Returns parsed dict. Raises on failure — callers must catch and fail-open.
    """
    from apex_llm_client import call_llm_thinking as _call
    return _call(prompt, module=module, budget_tokens=budget_tokens)


# ============================================================
# SHARED GEMINI HELPERS — fast-tier, unchanged
# ============================================================

def parse_gemini_json(text: str) -> dict:
    """Parse a Gemini response into a dict. Handles None, markdown fences, empty responses."""
    import json as _json
    text = (text or '').strip()
    if not text:
        raise ValueError('Gemini returned empty response')
    if '```' in text:
        parts = text.split('```')
        text = parts[1].lstrip('json').lstrip('\n').strip() if len(parts) > 1 else text
        if not text:
            raise ValueError('Empty response after markdown stripping')
    return _json.loads(text)


def call_gemini_json(prompt: str) -> dict:
    """Call Gemini with structured JSON output. Returns parsed dict.
    Raises on any failure — callers should catch and fall back."""
    from google import genai
    from google.genai import types as _gtypes
    from apex_config import GEMINI_API_KEY, LLM_SENTIMENT_MODEL, LLM_TIMEOUT

    if not GEMINI_API_KEY:
        raise ValueError('no_api_key')

    client = genai.Client(api_key=GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=LLM_SENTIMENT_MODEL,
        contents=prompt,
        config=_gtypes.GenerateContentConfig(
            response_mime_type='application/json',
            http_options=_gtypes.HttpOptions(timeout=LLM_TIMEOUT * 1000),
        ),
    )
    return parse_gemini_json(resp.text)


if __name__ == '__main__':
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else 'status'

    if cmd == 'status':
        print(format_status_message())

    elif cmd == 'set' and len(sys.argv) >= 4:
        flag_name = sys.argv[2].lower()
        enabled   = sys.argv[3].lower() in ('true', 'on', '1', 'yes')
        if flag_name == 'all':
            print(set_all_flags(enabled))
        else:
            print(set_llm_flag(flag_name, enabled))

    elif cmd == 'reset':
        def _reset(data):
            if not isinstance(data, dict):
                return {}
            for f in list(data.keys()):
                if isinstance(data[f], dict):
                    data[f]['call_count']          = 0
                    data[f]['fallback_call_count']  = 0
                    data[f]['last_used']            = None
                    data[f]['last_result_summary']  = None
            return data
        locked_read_modify_write(FLAGS_FILE, _reset, default={})
        print('✅ LLM call counters reset')

    elif cmd == 'provider' and len(sys.argv) >= 3:
        from apex_llm_client import set_provider as _sp
        print(_sp(sys.argv[2]))

    elif cmd == 'budget':
        from apex_llm_cost_tracker import format_digest_section
        print(format_digest_section())

    else:
        print('Usage: apex_llm_flags.py [status | set <flag> <true|false> | reset | provider <anthropic|gemini> | budget]')
        sys.exit(1)
