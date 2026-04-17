#!/usr/bin/env python3
"""
LLM Cost Tracker
Tracks token usage and estimated USD cost for every LLM call.
Enforces the daily budget cap — callers check is_over_daily_budget() before
making expensive thinking-tier calls.

All functions are non-blocking: they catch every exception internally so
cost tracking failure never breaks trading logic.

Cost file: apex-llm-costs.json
  {
    "calls": [
      {"ts": "2026-04-14T08:02:17Z", "module": "preflight", "model": "claude-sonnet-4-6",
       "input_tok": 812, "output_tok": 243, "cost_usd": 0.00608},
      ...
    ],
    "daily_totals": {"2026-04-14": 0.012},
    "mtd_totals":   {"2026-04": 0.14}
  }

CLI:
    python3 apex_llm_cost_tracker.py status
    python3 apex_llm_cost_tracker.py today
    python3 apex_llm_cost_tracker.py reset
"""
import sys
import json
import os
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import locked_read_modify_write, safe_read, log_warning, send_telegram
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d
    def log_warning(m): print(f'WARNING: {m}')
    def send_telegram(m): print(m)
    def locked_read_modify_write(path, fn, default=None):
        import tempfile
        try:
            data = safe_read(path, default)
            data = fn(data)
            d = os.path.dirname(path)
            with tempfile.NamedTemporaryFile(mode='w', dir=d, delete=False, suffix='.tmp') as tf:
                json.dump(data, tf, indent=2)
                tmp = tf.name
            os.replace(tmp, path)
        except Exception as e:
            print(f'ERROR: locked_read_modify_write failed: {e}')

COST_FILE = '/home/ubuntu/.picoclaw/logs/apex-llm-costs.json'

# ── Pricing per million tokens (USD) ──────────────────────────────────────────
# Output tokens include thinking tokens for Anthropic billing.
# Update these when provider pricing changes.
_RATES = {
    'claude-sonnet-4-6': {'input': 3.0,   'output': 15.0},
    'claude-opus-4-6':   {'input': 15.0,  'output': 75.0},
    'gemini-2.5-pro':    {'input': 1.25,  'output': 10.0},
    'gemini-2.5-flash':  {'input': 0.075, 'output': 0.30},
}

# Keep at most this many call records to prevent unbounded file growth
_MAX_CALLS_STORED = 500


def _calc_cost(model: str, input_tok: int, output_tok: int) -> float:
    """Return estimated USD cost for a single call."""
    rates = _RATES.get(model, {'input': 3.0, 'output': 15.0})
    return round(
        (input_tok  / 1_000_000) * rates['input'] +
        (output_tok / 1_000_000) * rates['output'],
        6
    )


def record_cost(module: str, model: str, input_tok: int, output_tok: int):
    """
    Record a single LLM call. Non-blocking — never raises.
    Sends Telegram alert if daily budget is approaching or exceeded.
    """
    try:
        from apex_config import LLM_DAILY_BUDGET_USD, LLM_BUDGET_ALERT_PCT
    except ImportError:
        LLM_DAILY_BUDGET_USD = 0.50
        LLM_BUDGET_ALERT_PCT = 0.80

    cost = _calc_cost(model, input_tok, output_tok)
    now  = datetime.now(timezone.utc)
    date_str  = now.strftime('%Y-%m-%d')
    month_str = now.strftime('%Y-%m')
    ts_str    = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _update(data):
        if not isinstance(data, dict):
            data = {}
        calls = data.get('calls', [])
        calls.append({
            'ts':         ts_str,
            'module':     module,
            'model':      model,
            'input_tok':  input_tok,
            'output_tok': output_tok,
            'cost_usd':   cost,
        })
        # Trim oldest records
        if len(calls) > _MAX_CALLS_STORED:
            calls = calls[-_MAX_CALLS_STORED:]
        data['calls'] = calls

        # Update daily total
        daily = data.get('daily_totals', {})
        daily[date_str] = round(daily.get(date_str, 0.0) + cost, 6)
        data['daily_totals'] = daily

        # Update MTD total
        mtd = data.get('mtd_totals', {})
        mtd[month_str] = round(mtd.get(month_str, 0.0) + cost, 6)
        data['mtd_totals'] = mtd

        return data

    try:
        locked_read_modify_write(COST_FILE, _update, default={})
    except Exception as _e:
        log_warning(f"apex_llm_cost_tracker: record_cost failed: {_e}")
        return

    # Budget status is included in the daily digest (apex-digest.py at 12:28 UTC).
    # No standalone Telegram alerts — they fire on every call once the limit is crossed.


def get_daily_total(date: str = None) -> float:
    """Return total USD spent on the given date (default today)."""
    try:
        if date is None:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        data = safe_read(COST_FILE, {})
        return data.get('daily_totals', {}).get(date, 0.0)
    except Exception:
        return 0.0


def get_mtd_total(month: str = None) -> float:
    """Return total USD spent this calendar month (YYYY-MM)."""
    try:
        if month is None:
            month = datetime.now(timezone.utc).strftime('%Y-%m')
        data = safe_read(COST_FILE, {})
        return data.get('mtd_totals', {}).get(month, 0.0)
    except Exception:
        return 0.0


def is_over_daily_budget() -> bool:
    """Return True if today's spend has reached or exceeded the daily limit."""
    try:
        from apex_config import LLM_DAILY_BUDGET_USD
    except ImportError:
        LLM_DAILY_BUDGET_USD = 0.50
    return get_daily_total() >= LLM_DAILY_BUDGET_USD


def get_today_breakdown() -> list:
    """Return list of today's calls for the digest."""
    try:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        data  = safe_read(COST_FILE, {})
        return [c for c in data.get('calls', []) if c.get('ts', '')[:10] == today]
    except Exception:
        return []


def format_digest_section() -> str:
    """Return Telegram-ready cost summary for the daily digest."""
    try:
        from apex_config import LLM_DAILY_BUDGET_USD
    except ImportError:
        LLM_DAILY_BUDGET_USD = 0.50

    today_usd = get_daily_total()
    mtd_usd   = get_mtd_total()
    calls     = get_today_breakdown()

    # Per-module breakdown
    module_costs: dict = {}
    for c in calls:
        m = c.get('module', 'unknown')
        module_costs[m] = module_costs.get(m, 0.0) + c.get('cost_usd', 0.0)

    budget_pct = today_usd / LLM_DAILY_BUDGET_USD * 100 if LLM_DAILY_BUDGET_USD else 0
    budget_icon = '✅' if budget_pct < 60 else ('⚠️' if budget_pct < 90 else '🚨')

    lines = [f'🧠 LLM COST {budget_icon}']
    lines.append(f'  Today: ${today_usd:.4f} / ${LLM_DAILY_BUDGET_USD:.2f} ({budget_pct:.0f}%)')
    lines.append(f'  MTD:   ${mtd_usd:.4f}')

    if module_costs:
        parts = [f'{m}×{sum(1 for c in calls if c.get("module")==m)} (${v:.4f})'
                 for m, v in sorted(module_costs.items(), key=lambda x: -x[1])]
        lines.append(f'  Breakdown: {" · ".join(parts[:5])}')

    # Provider
    try:
        from apex_llm_client import get_provider
        lines.append(f'  Provider: {get_provider().upper()}')
    except Exception:
        pass

    return '\n'.join(lines)


if __name__ == '__main__':
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else 'status'

    if cmd == 'status':
        print(format_digest_section())

    elif cmd == 'today':
        calls = get_today_breakdown()
        if not calls:
            print('No LLM calls recorded today.')
        else:
            for c in calls:
                print(f"  {c['ts'][11:19]} {c['module']:20s} {c['model']:25s} "
                      f"in={c['input_tok']:5d} out={c['output_tok']:5d} ${c['cost_usd']:.5f}")
            print(f"\nTotal today: ${get_daily_total():.4f}  MTD: ${get_mtd_total():.4f}")

    elif cmd == 'reset':
        def _reset(data):
            if not isinstance(data, dict):
                return {}
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            month = datetime.now(timezone.utc).strftime('%Y-%m')
            data['calls'] = [c for c in data.get('calls', [])
                             if c.get('ts', '')[:10] != today]
            data.setdefault('daily_totals', {})[today] = 0.0
            data.setdefault('mtd_totals', {})[month] = sum(
                v for d, v in data.get('daily_totals', {}).items() if d.startswith(month)
            )
            return data
        locked_read_modify_write(COST_FILE, _reset, default={})
        print("✅ Today's cost records reset")

    else:
        print('Usage: apex_llm_cost_tracker.py [status | today | reset]')
