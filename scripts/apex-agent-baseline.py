#!/usr/bin/env python3
"""apex-agent-baseline.py — Null-agent counterfactual P&L.

The ledger tells us what £ the agent claimed. The null baseline tells us
what the book would have done if the agent had stayed silent.

    α = realised_pnl − null_agent_pnl

If α ≈ 0, the agent is expensive decoration. If α < 0, we're paying tokens
to lose money. If α > 0, the agent earns its keep.

This is a metadata computation on top of apex-agent-ledger.json and
apex-outcomes.json — it does not re-run attribution. The ledger's
gross_pnl_impact_gbp IS the α (by construction: ledger impact is defined
as "what the agent saved/cost vs. doing nothing"). This script publishes
it alongside the realised and null baselines, per-period, with a verdict.

Writes apex-agent-baseline.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
LOGS_DIR    = SCRIPTS_DIR.parent / 'logs'

LEDGER_FILE   = LOGS_DIR / 'apex-agent-ledger.json'
OUTCOMES_FILE = LOGS_DIR / 'apex-outcomes.json'
OUT_FILE      = LOGS_DIR / 'apex-agent-baseline.json'


def _safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _realised_pnl_since(since, trades):
    """Sum pnl_gbp for trades closed in the window."""
    total = 0.0
    n = 0
    wins = 0
    for t in trades:
        closed = _parse_date(t.get('closed'))
        if not closed or closed < since:
            continue
        pnl = float(t.get('pnl_gbp') or t.get('pnl') or 0)
        total += pnl
        n += 1
        if pnl > 0:
            wins += 1
    return {
        'realised_pnl_gbp': round(total, 2),
        'trade_count':      n,
        'win_rate_pct':     round(100 * wins / n, 1) if n else None,
    }


def _verdict(alpha_gbp, realised_gbp):
    """Turn α into an opinion. Desk-head tone: calls it as it sees it."""
    if alpha_gbp is None:
        return 'insufficient_data'
    if alpha_gbp > max(5.0, 0.1 * abs(realised_gbp or 1)):
        return 'additive'          # agent meaningfully contributed
    if alpha_gbp < -max(5.0, 0.1 * abs(realised_gbp or 1)):
        return 'subtractive'       # agent cost us money — review authority
    return 'neutral'               # wash, within noise


def build(lookback_days=90):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)

    ledger = _safe_read(LEDGER_FILE, {}) or {}
    outcomes = _safe_read(OUTCOMES_FILE, {}) or {}
    trades = outcomes.get('trades', []) if isinstance(outcomes, dict) else []

    realised = _realised_pnl_since(since, trades)

    # Agent's gross contribution (signed). Already the α by the ledger's
    # attribution model — null baseline = realised − gross_agent_impact.
    gross_impact = float(ledger.get('gross_pnl_impact_gbp') or 0)
    llm_cost_gbp = float(ledger.get('llm_cost_gbp') or 0)

    null_pnl = round(realised['realised_pnl_gbp'] - gross_impact, 2)
    alpha_gross = round(gross_impact, 2)                  # pre-cost α
    alpha_net   = round(gross_impact - llm_cost_gbp, 2)   # post-cost α

    verdict = _verdict(alpha_net, realised['realised_pnl_gbp'])

    # "Agent alpha as % of null baseline" — useful when £ amounts are small.
    if null_pnl and abs(null_pnl) > 0.01:
        alpha_ratio_pct = round(100 * alpha_net / abs(null_pnl), 1)
    else:
        alpha_ratio_pct = None

    out = {
        'generated_at':          now.isoformat(),
        'period_days':           lookback_days,
        'period_start':          since.isoformat(),
        'realised_pnl_gbp':      realised['realised_pnl_gbp'],
        'trade_count':           realised['trade_count'],
        'win_rate_pct':          realised['win_rate_pct'],
        'null_agent_pnl_gbp':    null_pnl,
        'agent_gross_alpha_gbp': alpha_gross,
        'llm_cost_gbp':          llm_cost_gbp,
        'agent_net_alpha_gbp':   alpha_net,
        'alpha_ratio_pct':       alpha_ratio_pct,
        'verdict':               verdict,
        'note':                  ('realised_pnl − null_agent_pnl = gross α. '
                                  'Null baseline is the model-implied "what '
                                  'would have happened with no agent"; it '
                                  'inherits the ledger\'s attribution '
                                  'confidence, which is medium for tightens '
                                  'and low for closes/vetoes without counterfactuals.'),
    }

    tmp = OUT_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, OUT_FILE)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--lookback-days', type=int, default=90)
    args = p.parse_args()
    out = build(lookback_days=args.lookback_days)
    print(json.dumps({'status': 'ok', **out}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
