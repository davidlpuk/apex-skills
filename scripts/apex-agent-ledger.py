#!/usr/bin/env python3
"""apex-agent-ledger.py — Agent Economic Value Ledger.

For every logged agent action, compute a £ P&L impact by joining against
closed-trade outcomes. Subtract LLM API cost. Output net agent value.

This is the scoreboard. Without it, every conversation about whether the
agent is "good" is vibes. With it, we have a single number to point at.

What it does:
  1. Read apex-agent-actions.json (per-action log).
  2. Read apex-outcomes.json (closed-trade P&L).
  3. For each action type, compute attributable £ impact:
     - stop_tightened   → saved/lost £ vs. the pre-tighten stop
     - signal_vetoed    → counterfactual £ if the same ticker re-entered later
     - close_position   → £ vs. what the standing stop would have realised
     - signal_approved  → baseline (0); approval is the default
     - no_action        → baseline (0)
  4. Read apex-agent-reasoning.jsonl for LLM cost per run.
  5. Write apex-agent-ledger.json with per-action detail + aggregates.

Attribution is transparent about uncertainty: every line carries a `method`
field and a `confidence` flag so downstream analysis can filter out weak
signal. We'd rather publish "unknown" than fabricate a £ figure.

Run daily at EOD (cron), or on demand via:
    python3 apex-tool-runner.py --run agent-ledger
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
LOGS_DIR    = SCRIPTS_DIR.parent / 'logs'

ACTIONS_FILE   = LOGS_DIR / 'apex-agent-actions.json'
OUTCOMES_FILE  = LOGS_DIR / 'apex-outcomes.json'
REASONING_FILE = LOGS_DIR / 'apex-agent-reasoning.jsonl'
LEDGER_FILE    = LOGS_DIR / 'apex-agent-ledger.json'

# FX proxy when outcomes-supplied rates aren't available.
USD_GBP_FX_PROXY = 0.80

# Close-enough tolerances for attribution.
STOP_MATCH_PCT = 0.02   # exit within ±2% of new_stop attributed to tighten
CLOSE_MATCH_DAYS = 3    # veto → re-entry window


def _safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _parse_trade_date(d):
    """Outcomes use YYYY-MM-DD strings for opened/closed."""
    if not d:
        return None
    try:
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── Parsers for action details (free-text fields) ────────────────────────────

_STOP_PATTERNS = [
    re.compile(r'from\s+([\d.]+)\s+to\s+([\d.]+)', re.I),   # "from X to Y"
    re.compile(r'Stop\s+([\d.]+)\s*->\s*([\d.]+)', re.I),    # "Stop X -> Y"
    re.compile(r'([\d.]+)\s*->\s*([\d.]+)'),                 # bare "X -> Y"
]


def _extract_stop_prices(details):
    """Parse old/new stop from the free-text details field.
    Supports 'from X to Y', 'Stop X -> Y', and bare 'X -> Y' formats."""
    if not details:
        return None, None
    for rx in _STOP_PATTERNS:
        m = rx.search(details)
        if m:
            try:
                return float(m.group(1)), float(m.group(2))
            except (ValueError, TypeError):
                continue
    return None, None


def _action_type(action):
    """Some entries use 'action_type' (from apex-agent.py dispatcher), others
    use 'action' (from apex-agent-tighten-stop.py direct writes). Normalise."""
    t = action.get('action_type') or action.get('action') or 'unknown'
    # Canonicalise: 'tighten_stop' → 'stop_tightened'.
    aliases = {'tighten_stop': 'stop_tightened',
               'close_position': 'close_position',
               'exit_recommended': 'close_position'}
    return aliases.get(t, t)


# ── Attribution logic ───────────────────────────────────────────────────────

def _find_trade_covering(ticker, when, trades):
    """Find the closed trade where `when` falls between opened and closed."""
    for t in trades:
        if t.get('ticker') != ticker and t.get('name') != ticker:
            continue
        opened = _parse_trade_date(t.get('opened'))
        closed = _parse_trade_date(t.get('closed'))
        if not opened or not closed:
            continue
        # Allow small slack either side to tolerate timestamp noise.
        if opened - timedelta(days=1) <= when <= closed + timedelta(days=1):
            return t
    return None


def _find_trade_after(ticker, when, trades, window_days=CLOSE_MATCH_DAYS):
    """Find a trade opened on `ticker` shortly after `when` — used for veto
    counterfactuals when a signal was re-entered despite a VETO."""
    for t in trades:
        if t.get('ticker') != ticker and t.get('name') != ticker:
            continue
        opened = _parse_trade_date(t.get('opened'))
        if not opened:
            continue
        if when <= opened <= when + timedelta(days=window_days):
            return t
    return None


def attribute_stop_tightened(action, trades):
    """Impact of a stop tighten. Needs a closed trade to attribute against."""
    ticker = action.get('ticker')
    when = _parse_ts(action.get('timestamp'))
    # Prefer explicit numeric fields (from apex-agent-tighten-stop.py direct
    # writes); fall back to regex on the details string (from apex-agent.py's
    # wrapped log entries which embed the prices in free text).
    old_stop = action.get('old_stop')
    new_stop = action.get('new_stop')
    if old_stop is None or new_stop is None:
        old_stop, new_stop = _extract_stop_prices(
            action.get('details') or action.get('reason') or ''
        )

    base = {
        'action_type': 'stop_tightened',
        'ticker': ticker,
        'timestamp': action.get('timestamp'),
        'confidence_in_attribution': 'none',
        'pnl_gbp': 0.0,
        'method': '',
    }

    if not (ticker and when and old_stop and new_stop):
        return {**base, 'method': 'could_not_parse_stops'}

    trade = _find_trade_covering(ticker, when, trades)
    if not trade:
        return {**base, 'method': 'position_still_open_or_unmatched'}

    exit_px = trade.get('exit')
    qty = trade.get('qty', 0)
    if exit_px is None or qty is None:
        return {**base, 'method': 'missing_exit_or_qty'}

    currency = trade.get('currency') or ''
    fx = trade.get('fx_at_close') or (USD_GBP_FX_PROXY if currency == 'USD' else 1.0)

    # GBX adjustment: exit price in pounds in outcomes (per CLAUDE.md); stops
    # in apex-agent-actions are also in pounds (per tighten_stop convention).
    # No conversion needed here.

    if abs(exit_px - new_stop) / max(new_stop, 1e-9) <= STOP_MATCH_PCT:
        # Exit happened at tightened stop — attribute the saved distance.
        # Net: (new_stop - old_stop) × qty, converted to GBP.
        # Caveat: we don't know whether old_stop would've ultimately been hit.
        # Use MFE after tighten as a sanity proxy: if mfe_pct > 3% *after*
        # tighten, the tighten potentially cut short further upside.
        raw = (new_stop - old_stop) * qty * fx
        return {
            **base,
            'pnl_gbp': round(raw, 2),
            'method': 'tightened_stop_triggered_exit',
            'confidence_in_attribution': 'medium',
            'old_stop': old_stop,
            'new_stop': new_stop,
            'exit_price': exit_px,
            'note': 'Assumes old stop would have eventually triggered; true £ '
                    'depends on counterfactual path.',
        }

    if exit_px < new_stop:
        # Gap-down through the tightened stop — tighten caught a down-move.
        raw = (new_stop - old_stop) * qty * fx
        return {
            **base,
            'pnl_gbp': round(raw, 2),
            'method': 'gap_down_through_tightened_stop',
            'confidence_in_attribution': 'high',
            'old_stop': old_stop,
            'new_stop': new_stop,
            'exit_price': exit_px,
        }

    # exit_px is well above new_stop → stop never triggered, tighten had no effect.
    return {
        **base,
        'method': 'stop_not_hit_tighten_inert',
        'confidence_in_attribution': 'high',
        'pnl_gbp': 0.0,
        'old_stop': old_stop,
        'new_stop': new_stop,
        'exit_price': exit_px,
    }


def attribute_veto(action, trades):
    """A signal was vetoed. If the same ticker was re-entered within N days,
    the veto 'cost' the agent -(pnl_of_the_re_entry). Veto of a winner = bad."""
    ticker = action.get('ticker')
    when = _parse_ts(action.get('timestamp'))
    if not (ticker and when):
        return None

    trade = _find_trade_after(ticker, when, trades)
    if not trade:
        return {
            'action_type':               'signal_vetoed',
            'ticker':                    ticker,
            'timestamp':                 action.get('timestamp'),
            'pnl_gbp':                   0.0,
            'method':                    'no_re_entry_within_window',
            'confidence_in_attribution': 'low',
            'note':                      'True counterfactual requires price '
                                         'history; not computed in v1.',
        }

    pnl = trade.get('pnl_gbp', trade.get('pnl', 0.0)) or 0.0
    # Veto of a +£ trade is -£ for the agent; veto of a -£ trade is +£.
    return {
        'action_type':               'signal_vetoed',
        'ticker':                    ticker,
        'timestamp':                 action.get('timestamp'),
        'pnl_gbp':                   round(-pnl, 2),
        'method':                    're_entry_counterfactual',
        'confidence_in_attribution': 'medium',
        're_entry_opened':           trade.get('opened'),
        're_entry_pnl_gbp':          pnl,
    }


def attribute_close(action, trades):
    """Agent closed a position. Compare realised close price vs what the
    standing stop would have yielded. Imperfect — we don't know when/if the
    stop would have triggered."""
    ticker = action.get('ticker')
    when = _parse_ts(action.get('timestamp'))
    if not (ticker and when):
        return None

    trade = _find_trade_covering(ticker, when, trades)
    if not trade:
        return {
            'action_type':               'close_position',
            'ticker':                    ticker,
            'timestamp':                 action.get('timestamp'),
            'pnl_gbp':                   0.0,
            'method':                    'trade_not_found_for_close',
            'confidence_in_attribution': 'none',
        }

    # Without a recorded counterfactual stop, we can only report the realised
    # P&L of the close itself, flagged as "realised — not an agent delta".
    return {
        'action_type':               'close_position',
        'ticker':                    ticker,
        'timestamp':                 action.get('timestamp'),
        'pnl_gbp':                   round(trade.get('pnl_gbp', 0.0) or 0.0, 2),
        'method':                    'realised_pnl_no_counterfactual',
        'confidence_in_attribution': 'low',
        'note':                      'Realised P&L of the closed trade. True '
                                     'agent impact = this - what_stop_would_have_done.',
    }


def _attribute_one(action, trades):
    """Dispatch to the right attribution function; returns dict or None."""
    atype = _action_type(action)
    if atype == 'stop_tightened':
        return attribute_stop_tightened(action, trades)
    if atype == 'signal_vetoed':
        return attribute_veto(action, trades)
    if atype == 'close_position':
        return attribute_close(action, trades)
    # signal_approved, no_action, exit_recommended, unknown → no attribution
    return {
        'action_type':               atype,
        'ticker':                    action.get('ticker', ''),
        'timestamp':                 action.get('timestamp'),
        'pnl_gbp':                   0.0,
        'method':                    'baseline_no_attribution',
        'confidence_in_attribution': 'n/a',
    }


# ── LLM cost ─────────────────────────────────────────────────────────────────

def _llm_cost_since(since_iso):
    """Sum agent LLM spend for runs since `since_iso` (ISO string)."""
    since = _parse_ts(since_iso)
    runs = _safe_read(REASONING_FILE, []) or []
    if not isinstance(runs, list):
        return {'usd': 0.0, 'run_count': 0}
    total_usd = 0.0
    n = 0
    for r in runs:
        started = _parse_ts(r.get('started'))
        if not started or (since and started < since):
            continue
        total_usd += float(r.get('cost_usd', 0) or 0)
        n += 1
    return {'usd': round(total_usd, 4), 'run_count': n}


# ── Orchestrator ─────────────────────────────────────────────────────────────

def build(lookback_days=90):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)

    actions  = _safe_read(ACTIONS_FILE, []) or []
    outcomes = _safe_read(OUTCOMES_FILE, {}) or {}
    trades   = outcomes.get('trades', []) if isinstance(outcomes, dict) else []

    # Filter actions to the window.
    windowed = []
    for a in actions:
        ts = _parse_ts(a.get('timestamp'))
        if ts and ts >= since:
            windowed.append(a)

    # Attribute each action.
    detail = []
    for a in windowed:
        attributed = _attribute_one(a, trades)
        if attributed:
            attributed['confidence'] = a.get('confidence')
            attributed['mode'] = a.get('mode')
            detail.append(attributed)

    # Aggregate.
    by_type = {}
    for d in detail:
        bucket = by_type.setdefault(d['action_type'], {
            'count': 0,
            'attributed_count': 0,
            'pnl_gbp_sum': 0.0,
            'by_confidence': {'high': 0, 'medium': 0, 'low': 0, 'none': 0, 'n/a': 0},
        })
        bucket['count'] += 1
        conf = d.get('confidence_in_attribution', 'n/a')
        bucket['by_confidence'][conf] = bucket['by_confidence'].get(conf, 0) + 1
        if d['pnl_gbp']:
            bucket['pnl_gbp_sum'] = round(bucket['pnl_gbp_sum'] + d['pnl_gbp'], 2)
            bucket['attributed_count'] += 1

    gross = round(sum(d['pnl_gbp'] for d in detail), 2)

    # LLM cost over the same window.
    cost = _llm_cost_since(since.isoformat())
    cost_gbp = round(cost['usd'] * USD_GBP_FX_PROXY, 2)
    net = round(gross - cost_gbp, 2)

    ledger = {
        'generated_at':           now.isoformat(),
        'period_days':            lookback_days,
        'period_start':           since.isoformat(),
        'total_actions':          len(windowed),
        'attributed_actions':     sum(1 for d in detail if d.get('pnl_gbp')),
        'gross_pnl_impact_gbp':   gross,
        'llm_cost_usd':           cost['usd'],
        'llm_cost_gbp':           cost_gbp,
        'llm_run_count':          cost['run_count'],
        'net_agent_value_gbp':    net,
        'note':                   ('Positive net_agent_value_gbp means the '
                                   'agent added £ after paying for its own '
                                   'tokens. Compare vs. null-agent baseline '
                                   '(not computed in v1) for true alpha.'),
        'by_action_type':         by_type,
        'actions_detail':         detail,
    }

    tmp = LEDGER_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(ledger, indent=2, default=str))
    os.replace(tmp, LEDGER_FILE)
    return ledger


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--lookback-days', type=int, default=90,
                   help='Period to build ledger over (default 90)')
    args = p.parse_args()

    ledger = build(lookback_days=args.lookback_days)

    # Print a concise summary to stdout (full detail in the file).
    print(json.dumps({
        'status': 'ok',
        'timestamp': ledger['generated_at'],
        'path': str(LEDGER_FILE),
        'period_days': ledger['period_days'],
        'total_actions': ledger['total_actions'],
        'attributed_actions': ledger['attributed_actions'],
        'gross_pnl_impact_gbp': ledger['gross_pnl_impact_gbp'],
        'llm_cost_gbp': ledger['llm_cost_gbp'],
        'net_agent_value_gbp': ledger['net_agent_value_gbp'],
        'by_action_type': {
            k: {'count': v['count'],
                'attributed_count': v['attributed_count'],
                'pnl_gbp_sum': v['pnl_gbp_sum']}
            for k, v in ledger['by_action_type'].items()
        },
    }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
