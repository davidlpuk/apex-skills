#!/usr/bin/env python3
"""
LLM Multi-Provider Client
Single interface for thinking-tier and fast-tier LLM calls.
Supports Anthropic (Claude extended thinking) and Google (Gemini).

Provider is read from apex-llm-config.json at runtime — switch without restart.
Falls back to fast model (Gemini Flash) when over daily budget.

Public API:
    call_llm_thinking(prompt, module, budget_tokens) -> dict
    call_llm_fast(prompt, module)                   -> dict
    get_provider() -> str                            ('anthropic' | 'gemini')
    set_provider(provider) -> str                    (Telegram-ready status)

CLI:
    python3 apex_llm_client.py provider anthropic|gemini
    python3 apex_llm_client.py status
    python3 apex_llm_client.py test-thinking
    python3 apex_llm_client.py test-fast
"""
import json
import os
import sys
import time

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import safe_read, log_warning, log_info, atomic_write
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m):    print(f'INFO: {m}')
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True

# ── Config file (runtime provider switching, no code deploy needed) ───────────
LLM_CONFIG_FILE = '/home/ubuntu/.picoclaw/logs/apex-llm-config.json'

# ── Config cache (avoid file read on every call) ──────────────────────────────
_config_cache: dict = {}
_config_cache_ts: float = 0
_CONFIG_TTL: float = 30.0   # seconds


def _load_config() -> dict:
    global _config_cache, _config_cache_ts
    now = time.monotonic()
    if now - _config_cache_ts < _CONFIG_TTL and _config_cache:
        return _config_cache
    data = safe_read(LLM_CONFIG_FILE, {})
    if not isinstance(data, dict):
        data = {}
    _config_cache    = data
    _config_cache_ts = now
    return data


# ── Per-module provider overrides ────────────────────────────────────────────
# These modules use Claude's extended thinking regardless of the global provider
# setting — they make high-stakes binary decisions where auditable chain-of-thought
# matters more than cost. Only applies when the Anthropic key is available.
_MODULE_PROVIDER_OVERRIDES: dict[str, str] = {
    'preflight':       'anthropic',  # falling knife filter — binary, high-stakes
    'drawdown_review': 'anthropic',  # adversarial risk reasoning
    'portfolio_agent': 'anthropic',  # whole-book risk synthesis
}


def get_provider() -> str:
    """Return active provider: 'anthropic' or 'gemini'."""
    cfg = _load_config()
    provider = cfg.get('provider', '').lower()
    if provider in ('anthropic', 'gemini'):
        return provider
    # Fall back to apex_config default
    try:
        from apex_config import LLM_PROVIDER
        return LLM_PROVIDER.lower()
    except ImportError:
        return 'anthropic'


def get_effective_provider(module: str = '') -> str:
    """
    Return the provider to use for a specific module.
    Module-level overrides take precedence over the global setting, but only
    when the required API key is available — falls back gracefully if not.
    """
    override = _MODULE_PROVIDER_OVERRIDES.get(module, '')
    if override in ('anthropic', 'gemini'):
        try:
            from apex_config import ANTHROPIC_API_KEY, GEMINI_API_KEY
            key = ANTHROPIC_API_KEY if override == 'anthropic' else GEMINI_API_KEY
            if key:
                return override
        except ImportError:
            pass
    return get_provider()


def set_provider(provider: str) -> str:
    """Switch provider at runtime. Returns Telegram-ready message."""
    provider = provider.lower()
    if provider not in ('anthropic', 'gemini'):
        return f"❌ Unknown provider '{provider}'. Use: anthropic | gemini"
    global _config_cache, _config_cache_ts
    cfg = safe_read(LLM_CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg['provider'] = provider
    atomic_write(LLM_CONFIG_FILE, cfg)
    _config_cache    = cfg
    _config_cache_ts = time.monotonic()
    icon = '🧠' if provider == 'anthropic' else '🔷'
    return f"{icon} LLM Provider → {provider.upper()}\nThinking-tier calls now use {_thinking_model_name(provider)}"


def _thinking_model_name(provider: str) -> str:
    try:
        from apex_config import LLM_THINKING_MODEL_ANTHROPIC, LLM_THINKING_MODEL_GEMINI
        return LLM_THINKING_MODEL_ANTHROPIC if provider == 'anthropic' else LLM_THINKING_MODEL_GEMINI
    except ImportError:
        return 'claude-sonnet-4-6' if provider == 'anthropic' else 'gemini-2.5-pro'


def _parse_json_response(text: str) -> dict:
    """Parse JSON from LLM response text. Handles markdown fences."""
    text = (text or '').strip()
    if not text:
        raise ValueError('Empty response from LLM')
    if '```' in text:
        parts = text.split('```')
        for part in parts:
            candidate = part.lstrip('json').lstrip('\n').strip()
            if candidate.startswith('{') or candidate.startswith('['):
                text = candidate
                break
    # Handle case where model includes explanation before/after JSON
    start = text.find('{')
    end   = text.rfind('}')
    if start >= 0 and end > start:
        text = text[start:end+1]
    return json.loads(text)


# ── Anthropic (Claude Extended Thinking) ──────────────────────────────────────

def _call_anthropic_thinking(prompt: str, module: str, budget_tokens: int) -> tuple[dict, dict]:
    """
    Call Claude with extended thinking enabled.
    Returns (result_dict, usage_dict).
    Raises on any failure.
    """
    try:
        import anthropic as _anthropic
    except ImportError:
        raise ImportError("anthropic package not installed. Run: pip install anthropic")

    try:
        from apex_config import ANTHROPIC_API_KEY, LLM_THINKING_MODEL_ANTHROPIC, LLM_THINKING_TIMEOUT
    except ImportError:
        ANTHROPIC_API_KEY = ''
        LLM_THINKING_MODEL_ANTHROPIC = 'claude-sonnet-4-6'
        LLM_THINKING_TIMEOUT = 90

    if not ANTHROPIC_API_KEY:
        raise ValueError('ANTHROPIC_API_KEY not set in .env.trading212')

    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=LLM_THINKING_TIMEOUT)

    # Anthropic requires budget_tokens >= 1024
    budget_tokens = max(budget_tokens, 1024)
    # max_tokens must exceed budget_tokens (thinking) + expected output tokens
    max_tokens = budget_tokens + 2000

    response = client.messages.create(
        model=LLM_THINKING_MODEL_ANTHROPIC,
        max_tokens=max_tokens,
        thinking={'type': 'enabled', 'budget_tokens': budget_tokens},
        messages=[{
            'role': 'user',
            'content': (
                prompt +
                '\n\nIMPORTANT: Respond with ONLY a valid JSON object. '
                'No markdown fences, no preamble, no explanation outside the JSON.'
            )
        }]
    )

    # Extract text blocks only (skip thinking blocks)
    text = ''.join(
        block.text
        for block in response.content
        if hasattr(block, 'text') and block.type == 'text'
    )

    result = _parse_json_response(text)
    usage  = {
        'input_tokens':  response.usage.input_tokens,
        'output_tokens': response.usage.output_tokens,  # includes thinking tokens
        'model':         LLM_THINKING_MODEL_ANTHROPIC,
    }
    return result, usage


# ── Gemini (2.5 Pro with thinking) ────────────────────────────────────────────

def _call_gemini_thinking(prompt: str, module: str, budget_tokens: int) -> tuple[dict, dict]:
    """
    Call Gemini 2.5 Pro with thinking budget.
    Returns (result_dict, usage_dict).
    Raises on any failure.
    """
    try:
        from google import genai
        from google.genai import types as _gtypes
    except ImportError:
        raise ImportError("google-genai package not installed. Run: pip install google-genai")

    try:
        from apex_config import GEMINI_API_KEY, LLM_THINKING_MODEL_GEMINI, LLM_THINKING_TIMEOUT
    except ImportError:
        GEMINI_API_KEY = ''
        LLM_THINKING_MODEL_GEMINI = 'gemini-2.5-pro'
        LLM_THINKING_TIMEOUT = 90

    if not GEMINI_API_KEY:
        raise ValueError('GEMINI_API_KEY not set in .env.trading212')

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build thinking config — gracefully degrade if SDK doesn't support it
    try:
        thinking_cfg = _gtypes.ThinkingConfig(thinking_budget=budget_tokens)
        gen_config = _gtypes.GenerateContentConfig(
            response_mime_type='application/json',
            thinking_config=thinking_cfg,
            http_options=_gtypes.HttpOptions(timeout=LLM_THINKING_TIMEOUT * 1000),
        )
    except (AttributeError, TypeError):
        # Older SDK version — use Pro without explicit thinking config
        gen_config = _gtypes.GenerateContentConfig(
            response_mime_type='application/json',
            http_options=_gtypes.HttpOptions(timeout=LLM_THINKING_TIMEOUT * 1000),
        )

    resp = client.models.generate_content(
        model=LLM_THINKING_MODEL_GEMINI,
        contents=prompt,
        config=gen_config,
    )

    result = _parse_json_response(resp.text)

    # Extract usage metadata (field names vary by SDK version)
    meta = getattr(resp, 'usage_metadata', None) or {}
    usage = {
        'input_tokens':  getattr(meta, 'prompt_token_count', 0),
        'output_tokens': getattr(meta, 'candidates_token_count', 0),
        'model':         LLM_THINKING_MODEL_GEMINI,
    }
    return result, usage


# ── Public interface ──────────────────────────────────────────────────────────

def call_llm_thinking(
    prompt: str,
    module: str = 'unknown',
    budget_tokens: int = None,
) -> dict:
    """
    Call the configured thinking-tier LLM.
    Returns parsed dict. Raises on failure (callers must catch and fail-open).

    If daily budget is exceeded, falls back to fast model automatically
    and logs a warning (does NOT raise — trading must continue).

    budget_tokens: override the default thinking budget for this call.
                   Use higher values (4096–8192) for harder, higher-stakes decisions.
    """
    try:
        from apex_config import LLM_THINKING_BUDGET_TOKENS
    except ImportError:
        LLM_THINKING_BUDGET_TOKENS = 2048

    if budget_tokens is None:
        budget_tokens = LLM_THINKING_BUDGET_TOKENS

    # Budget cap: fall back to fast model if over daily limit
    try:
        from apex_llm_cost_tracker import is_over_daily_budget
        if is_over_daily_budget():
            log_warning(f"LLM budget exceeded — falling back to fast model for {module}")
            return call_llm_fast(prompt, module=module)
    except ImportError:
        pass

    provider = get_effective_provider(module)
    log_info(f"LLM thinking call: module={module} provider={provider} budget={budget_tokens}")

    result, usage = None, {}

    if provider == 'anthropic':
        try:
            result, usage = _call_anthropic_thinking(prompt, module, budget_tokens)
        except Exception as _e:
            log_warning(f"Anthropic thinking failed ({_e}) — trying Gemini fallback")
            result, usage = _call_gemini_thinking(prompt, module, budget_tokens)
    else:  # gemini
        try:
            result, usage = _call_gemini_thinking(prompt, module, budget_tokens)
        except Exception as _e:
            log_warning(f"Gemini thinking failed ({_e}) — trying Anthropic fallback")
            result, usage = _call_anthropic_thinking(prompt, module, budget_tokens)

    # Record cost (non-blocking)
    try:
        from apex_llm_cost_tracker import record_cost
        record_cost(
            module=module,
            model=usage.get('model', 'unknown'),
            input_tok=usage.get('input_tokens', 0),
            output_tok=usage.get('output_tokens', 0),
        )
    except Exception:
        pass

    return result


def call_llm_fast(prompt: str, module: str = 'unknown') -> dict:
    """
    Call the fast-tier LLM (Gemini Flash). Identical interface to call_llm_thinking.
    Used for: batch sentiment scoring, any call where reasoning depth adds minimal value.
    Always uses Gemini regardless of provider setting — fast path stays cheap.
    """
    try:
        from google import genai
        from google.genai import types as _gtypes
        from apex_config import GEMINI_API_KEY, LLM_SENTIMENT_MODEL, LLM_TIMEOUT
    except ImportError as _e:
        raise ImportError(f"google-genai or apex_config import failed: {_e}")

    if not GEMINI_API_KEY:
        raise ValueError('GEMINI_API_KEY not set in .env.trading212')

    client = genai.Client(api_key=GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=LLM_SENTIMENT_MODEL,
        contents=prompt,
        config=_gtypes.GenerateContentConfig(
            response_mime_type='application/json',
            http_options=_gtypes.HttpOptions(timeout=LLM_TIMEOUT * 1000),
        ),
    )

    result = _parse_json_response(resp.text)

    # Record cost
    try:
        from apex_llm_cost_tracker import record_cost
        meta = getattr(resp, 'usage_metadata', None) or {}
        record_cost(
            module=module,
            model=LLM_SENTIMENT_MODEL,
            input_tok=getattr(meta, 'prompt_token_count', 0),
            output_tok=getattr(meta, 'candidates_token_count', 0),
        )
    except Exception:
        pass

    return result


def format_status() -> str:
    """Return Telegram-ready provider + budget status."""
    provider = get_provider()
    model    = _thinking_model_name(provider)

    try:
        from apex_llm_cost_tracker import get_daily_total, get_mtd_total
        from apex_config import LLM_DAILY_BUDGET_USD, LLM_THINKING_BUDGET_TOKENS
        daily = get_daily_total()
        mtd   = get_mtd_total()
        budget_pct = daily / LLM_DAILY_BUDGET_USD * 100 if LLM_DAILY_BUDGET_USD else 0
        budget_str = (f"${daily:.4f} / ${LLM_DAILY_BUDGET_USD:.2f} today "
                      f"({budget_pct:.0f}%) | MTD ${mtd:.4f}")
        tokens_str = f"Thinking budget: {LLM_THINKING_BUDGET_TOKENS} tokens/call"
    except Exception:
        budget_str = 'unavailable'
        tokens_str = ''

    icon = '🧠' if provider == 'anthropic' else '🔷'
    lines = [
        f'{icon} LLM PROVIDER: {provider.upper()} (global default)',
        f'   Thinking model: {model}',
        f'   Fast model:     gemini-2.5-flash',
        f'   Budget: {budget_str}',
    ]
    if tokens_str:
        lines.append(f'   {tokens_str}')
    # Show per-module overrides
    override_lines = []
    for mod, prov in _MODULE_PROVIDER_OVERRIDES.items():
        effective = get_effective_provider(mod)
        override_lines.append(f'   {mod}: {effective.upper()}')
    if override_lines:
        lines.append('Module overrides (Claude for high-stakes):')
        lines.extend(override_lines)
    lines.append('\nLLM PROVIDER anthropic|gemini  to switch')
    return '\n'.join(lines)


if __name__ == '__main__':
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else 'status'

    if cmd == 'status':
        print(format_status())

    elif cmd == 'provider' and len(sys.argv) >= 3:
        print(set_provider(sys.argv[2]))

    elif cmd == 'test-thinking':
        print(f"Testing thinking-tier with provider: {get_provider()}")
        try:
            result = call_llm_thinking(
                'Return {"test": "ok", "provider": "working", "value": 42}',
                module='test',
                budget_tokens=512,
            )
            print(f"✅ Success: {result}")
        except Exception as e:
            print(f"❌ Failed: {e}")

    elif cmd == 'test-fast':
        print("Testing fast-tier (Gemini Flash)")
        try:
            result = call_llm_fast(
                'Return {"test": "ok", "model": "flash", "value": 1}',
                module='test',
            )
            print(f"✅ Success: {result}")
        except Exception as e:
            print(f"❌ Failed: {e}")

    else:
        print('Usage: apex_llm_client.py [status | provider <anthropic|gemini> | test-thinking | test-fast]')
