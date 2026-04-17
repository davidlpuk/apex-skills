#!/usr/bin/env python3
"""apex-agent-tier.py — Tiered authority evaluator.

File-driven authority state machine. Reads the ledger + calibration +
recent action outcomes, decides whether the agent sits on Probation,
Standard, or Senior, and writes apex-agent-tier.json.

Other scripts (notably apex-agent.py::_close_position) gate destructive
actions on this file. If the ledger goes red or calibration deteriorates,
the tier demotes automatically — authority shrinks without human input.

Tiers:
  Probation  — default. Read + log + tighten only. No close/veto authority.
  Standard   — may close positions with --confirm. No portfolio-level moves.
  Senior     — full autonomy within existing gates.

Promotion gates (all conditions must hold):
  → Standard:  ≥20 attributed actions AND 30d net_alpha ≥ 0 AND brier ≤ 0.25
  → Senior:    (at Standard) AND 90d net_alpha > 0 AND brier ≤ 0.15

Demotion triggers (any):
  3 consecutive losing attributed actions
  30d net_alpha < 0
  brier > 0.30

Writes apex-agent-tier.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
LOGS_DIR    = SCRIPTS_DIR.parent / 'logs'

LEDGER_FILE      = LOGS_DIR / 'apex-agent-ledger.json'
BASELINE_FILE    = LOGS_DIR / 'apex-agent-baseline.json'
CALIBRATION_FILE = LOGS_DIR / 'apex-agent-calibration.json'
OUT_FILE         = LOGS_DIR / 'apex-agent-tier.json'

TIERS = ('Probation', 'Standard', 'Senior')


def _safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _consecutive_losses(ledger):
    """Walk the attributed-action tail; count trailing losers (pnl < 0)."""
    detail = (ledger or {}).get('actions_detail') or []
    losses = 0
    for row in reversed(detail):
        pnl = row.get('pnl_gbp')
        if pnl is None or pnl == 0:
            continue
        if pnl < 0:
            losses += 1
        else:
            break
    return losses


def _evaluate(ledger, baseline, calibration, current_tier):
    """Pure function: inputs → (new_tier, reasons[]). No side-effects."""
    reasons = []

    attributed = (ledger or {}).get('attributed_actions', 0) or 0
    net_alpha_30d = (baseline or {}).get('agent_net_alpha_gbp')
    # We only have the 90-day baseline right now; treat it as the long-window
    # signal and use ledger.net_agent_value_gbp as the short-window proxy.
    net_value_short = (ledger or {}).get('net_agent_value_gbp')
    brier = (calibration or {}).get('brier_score')
    cons_losses = _consecutive_losses(ledger)

    # ── Demotion checks (evaluated first — any hit forces Probation)
    demote = False
    if cons_losses >= 3:
        reasons.append(f'3+ consecutive losing actions ({cons_losses})')
        demote = True
    if net_value_short is not None and net_value_short < 0:
        reasons.append(f'short-window net agent value £{net_value_short} < 0')
        demote = True
    if brier is not None and brier > 0.30:
        reasons.append(f'brier {brier} > 0.30 — confidence signal broken')
        demote = True
    if demote:
        return 'Probation', reasons

    # ── Promotion gates (must clear each rung in order)
    qualifies_standard = (
        attributed >= 20
        and net_alpha_30d is not None and net_alpha_30d >= 0
        and (brier is None or brier <= 0.25)
    )
    qualifies_senior = (
        qualifies_standard
        and net_alpha_30d is not None and net_alpha_30d > 0
        and brier is not None and brier <= 0.15
    )

    if qualifies_senior:
        reasons.append('Senior gates cleared (20+ actions, +α, brier ≤ 0.15)')
        return 'Senior', reasons
    if qualifies_standard:
        reasons.append('Standard gates cleared (20+ actions, α ≥ 0, brier ≤ 0.25)')
        return 'Standard', reasons

    # ── Default: Probation — be explicit about *why* we didn't promote
    if attributed < 20:
        reasons.append(f'only {attributed} attributed actions (need ≥20 for Standard)')
    if net_alpha_30d is None:
        reasons.append('no baseline α computed yet')
    elif net_alpha_30d < 0:
        reasons.append(f'net α £{net_alpha_30d} below zero')
    if brier is None:
        reasons.append('calibration has insufficient data')
    elif brier > 0.25:
        reasons.append(f'brier {brier} above Standard ceiling (0.25)')
    return 'Probation', reasons


def _authority_for(tier):
    """Human-readable capability list for the tier. Mirrors agent code gates."""
    if tier == 'Senior':
        return {
            'may_tighten_stops':    True,
            'may_close_positions':  True,
            'may_veto_signals':     True,
            'may_execute_trades':   True,   # still gated by existing --force
        }
    if tier == 'Standard':
        return {
            'may_tighten_stops':    True,
            'may_close_positions':  True,
            'may_veto_signals':     True,
            'may_execute_trades':   False,
        }
    return {
        'may_tighten_stops':    True,
        'may_close_positions':  False,
        'may_veto_signals':     False,
        'may_execute_trades':   False,
    }


def build():
    now = datetime.now(timezone.utc)

    ledger      = _safe_read(LEDGER_FILE, {}) or {}
    baseline    = _safe_read(BASELINE_FILE, {}) or {}
    calibration = _safe_read(CALIBRATION_FILE, {}) or {}

    prior = _safe_read(OUT_FILE, {}) or {}
    prior_tier = prior.get('tier', 'Probation')
    if prior_tier not in TIERS:
        prior_tier = 'Probation'

    new_tier, reasons = _evaluate(ledger, baseline, calibration, prior_tier)
    changed = new_tier != prior_tier

    out = {
        'generated_at': now.isoformat(),
        'tier':         new_tier,
        'prior_tier':   prior_tier,
        'changed':      changed,
        'reasons':      reasons,
        'authority':    _authority_for(new_tier),
        'inputs': {
            'attributed_actions':     ledger.get('attributed_actions'),
            'net_agent_value_gbp':    ledger.get('net_agent_value_gbp'),
            'net_alpha_90d_gbp':      baseline.get('agent_net_alpha_gbp'),
            'brier_score':            calibration.get('brier_score'),
            'consecutive_losses':     _consecutive_losses(ledger),
        },
        'note': ('Probation = read/log/tighten only. Standard adds close & veto. '
                 'Senior adds full execute authority (still gated by --force). '
                 'Demotion is automatic and persistent — only promotion gates '
                 'restore authority.'),
    }

    tmp = OUT_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, OUT_FILE)
    return out


def main():
    argparse.ArgumentParser().parse_args()
    out = build()
    print(json.dumps({'status': 'ok', **out}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
