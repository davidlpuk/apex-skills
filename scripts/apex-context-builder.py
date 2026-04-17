#!/usr/bin/env python3
"""apex-context-builder.py — Build the agent's session-bootstrap context file.

Produces /home/ubuntu/.picoclaw/logs/apex-context.md — a single markdown doc
the agent reads at the start of every run. Replaces ad-hoc tool calls to
discover "what do I know right now".

Pattern from https://every.to/guides/agent-native — a context.md that
describes: who the agent is, current state, available resources, recent
activity, and current guidelines.

This script is idempotent and safe to run on cron. Writes atomically.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
LOGS_DIR    = SCRIPTS_DIR.parent / 'logs'
OUT_FILE    = LOGS_DIR / 'apex-context.md'
MANIFEST    = SCRIPTS_DIR / 'apex-tool-manifest.json'
CHAINS      = SCRIPTS_DIR / 'apex-tool-chains.json'
GATES_FILE  = LOGS_DIR / 'apex-decision-gates.json'


def _safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _age_mins(path):
    try:
        mtime = os.path.getmtime(path)
        return int((datetime.now().timestamp() - mtime) / 60)
    except OSError:
        return None


def _fmt_age(mins):
    if mins is None:
        return 'never'
    if mins < 60:
        return f'{mins}m ago'
    if mins < 1440:
        return f'{mins // 60}h{mins % 60:02d}m ago'
    return f'{mins // 1440}d ago'


# ── Sections ──────────────────────────────────────────────────────────────────

def section_identity():
    return """\
# APEX Agent Context

You are the **Apex Trading Agent** — an autonomous assistant managing a real-money
Trading 212 portfolio. This file is your session bootstrap: read it first, then act.

**Operating philosophy:** reduce risk autonomously, ask before increasing it.
"""


def section_market():
    cal = _safe_read(LOGS_DIR / 'apex-market-calendar.json', {})
    today = cal.get('today', {}) if isinstance(cal, dict) else {}
    status = today.get('status', 'UNKNOWN')
    us_open = today.get('us_currently_open', False)
    uk_open = today.get('uk_currently_open', False)
    us_hol = today.get('us_holiday') or '—'
    uk_hol = today.get('uk_holiday') or '—'
    age = _age_mins(LOGS_DIR / 'apex-market-calendar.json')
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return f"""\
## Market Status

- **Now:** {now_utc}
- **Status:** `{status}`
- **NYSE/NASDAQ open now:** {'yes' if us_open else 'no'} (holiday: {us_hol})
- **LSE open now:** {'yes' if uk_open else 'no'} (holiday: {uk_hol})
- Calendar file updated {_fmt_age(age)}.

Never guess market hours — they drive whether stop placements succeed (T212 rejects
stop orders for US stocks outside 14:30–21:00 UTC).
"""


def section_regime():
    regime = _safe_read(LOGS_DIR / 'apex-regime.json', {}) or {}
    scaling = _safe_read(LOGS_DIR / 'apex-regime-scaling.json', {}) or {}
    breaker = _safe_read(LOGS_DIR / 'apex-circuit-breaker.json', {}) or {}
    drawdown = _safe_read(LOGS_DIR / 'apex-drawdown.json', {}) or {}

    regime_label = regime.get('regime', regime.get('current', 'unknown'))
    vix = regime.get('vix', regime.get('vix_level', 'n/a'))
    mult = scaling.get('size_multiplier', scaling.get('multiplier', 1.0))
    cb_level = breaker.get('level', breaker.get('status', 'UNKNOWN'))
    dd = drawdown.get('current_drawdown_pct', drawdown.get('drawdown_pct', 'n/a'))

    age_regime = _age_mins(LOGS_DIR / 'apex-regime.json')
    return f"""\
## Regime & Health

- **Regime:** `{regime_label}` (VIX: {vix})
- **Size multiplier:** {mult}x
- **Circuit breaker:** `{cb_level}`
- **Drawdown:** {dd}%
- Regime updated {_fmt_age(age_regime)}.

If circuit breaker is `SUSPEND` or `CRITICAL`, protective actions only — never open new positions.
"""


def section_positions():
    positions = _safe_read(LOGS_DIR / 'apex-positions.json', []) or []
    if isinstance(positions, dict):
        positions = positions.get('positions', [])

    if not positions:
        return "## Open Positions\n\nNone.\n"

    lines = ["## Open Positions\n", "| Ticker | Qty | Entry | Stop | Type | Status | Venue |",
             "|---|---|---|---|---|---|---|"]
    for p in positions[:30]:
        tkr = p.get('t212_ticker') or p.get('ticker') or p.get('name', '?')
        qty = p.get('qty') or p.get('quantity', '?')
        entry = p.get('entry') or p.get('entry_price', '?')
        stop = p.get('stop') or p.get('stop_price', '?')
        sig = p.get('signal_type') or p.get('type', '?')
        stat = p.get('status', '?')
        ven = p.get('venue') or 'T212'
        lines.append(f"| {tkr} | {qty} | {entry} | {stop} | {sig} | {stat} | {ven} |")
    if len(positions) > 30:
        lines.append(f"\n_...and {len(positions) - 30} more_")
    return '\n'.join(lines) + '\n'


def section_signals():
    pending = _safe_read(LOGS_DIR / 'apex-pending-signal.json', {}) or {}
    queue = _safe_read(LOGS_DIR / 'apex-trade-queue.json', []) or []
    if isinstance(queue, dict):
        queue = queue.get('queue', queue.get('items', []))

    lines = ["## Signals & Queue\n"]
    if pending and pending.get('ticker'):
        age = _age_mins(LOGS_DIR / 'apex-pending-signal.json')
        lines.append(f"- **Pending signal:** {pending.get('ticker')} "
                     f"({pending.get('signal_type', '?')}, score {pending.get('score', '?')}) "
                     f"— generated {_fmt_age(age)}")
    else:
        lines.append("- No pending signal.")

    active_q = [q for q in queue if q.get('status') in ('QUEUED', 'EXECUTING')]
    if active_q:
        lines.append(f"- **Queue:** {len(active_q)} active entries")
        for q in active_q[:5]:
            lines.append(f"  - {q.get('t212_ticker', q.get('ticker', '?'))} "
                         f"[{q.get('status')}] {q.get('signal_type', '')}")
    else:
        lines.append("- Queue is empty.")
    return '\n'.join(lines) + '\n'


def section_recent_actions():
    actions_path = LOGS_DIR / 'apex-agent-actions.json'
    actions = _safe_read(actions_path, []) or []
    if isinstance(actions, dict):
        actions = actions.get('actions', [])
    if not actions:
        return "## Recent Agent Actions\n\nNo recorded actions yet.\n"

    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    recent = []
    for a in actions[-50:]:
        ts = a.get('timestamp', '')
        try:
            if datetime.fromisoformat(ts.replace('Z', '+00:00')) >= cutoff:
                recent.append(a)
        except (ValueError, TypeError):
            continue

    if not recent:
        return "## Recent Agent Actions\n\nNone in the last 48h.\n"

    lines = ["## Recent Agent Actions (last 48h)\n"]
    for a in recent[-10:]:
        ts = a.get('timestamp', '')[:16].replace('T', ' ')
        lines.append(f"- `{ts}` — {a.get('action_type', '?')} "
                     f"{a.get('ticker', '')}: {a.get('reason', '')[:120]}")
    return '\n'.join(lines) + '\n'


def section_ledger():
    """Agent Economic Value Ledger — the single £ number for 'is the agent worth it?'"""
    l = _safe_read(LOGS_DIR / 'apex-agent-ledger.json', {}) or {}
    if not l:
        return ""
    period = l.get('period_days', '?')
    gross = l.get('gross_pnl_impact_gbp', 0)
    cost = l.get('llm_cost_gbp', 0)
    net = l.get('net_agent_value_gbp', 0)
    total_actions = l.get('total_actions', 0)
    attributed = l.get('attributed_actions', 0)
    age = _age_mins(LOGS_DIR / 'apex-agent-ledger.json')
    sign = '+' if net >= 0 else ''
    verdict = 'net-positive' if net > 0 else ('neutral' if net == 0 else 'NET-NEGATIVE')
    lines = [
        f"## Agent Economic Value — last {period}d\n",
        f"- **Net agent value: {sign}£{net}** ({verdict})",
        f"- Gross impact: £{gross} · LLM cost: £{cost}",
        f"- {attributed}/{total_actions} actions attributed a £ impact "
        f"({total_actions - attributed} had no closed trade to measure against yet)",
        f"- Ledger refreshed {_fmt_age(age)}.",
        "",
        "If net is negative, your autonomous authority is at risk. Protect trades that "
        "moved against us; don't veto signals lightly.",
    ]
    by_type = l.get('by_action_type', {})
    if by_type:
        lines.append("\n**By action type:**")
        for atype, stats in by_type.items():
            lines.append(f"- `{atype}`: {stats.get('count', 0)} actions, "
                         f"£{stats.get('pnl_gbp_sum', 0)} attributed")
    return '\n'.join(lines) + '\n'


def section_accountability():
    """Tier + baseline + calibration — the agent's authority envelope and
    whether recent work justifies it. Read before any destructive action."""
    tier = _safe_read(LOGS_DIR / 'apex-agent-tier.json', {}) or {}
    baseline = _safe_read(LOGS_DIR / 'apex-agent-baseline.json', {}) or {}
    calib = _safe_read(LOGS_DIR / 'apex-agent-calibration.json', {}) or {}
    if not (tier or baseline or calib):
        return ""

    lines = ["## Accountability & Authority\n"]

    t = tier.get('tier', 'Probation')
    auth = tier.get('authority', {}) or {}
    caps = [k.replace('may_', '').replace('_', ' ')
            for k, v in auth.items() if v]
    lines.append(f"- **Tier:** `{t}` — capabilities: {', '.join(caps) or 'none'}")
    if tier.get('reasons'):
        lines.append(f"  - {'; '.join(tier['reasons'])}")

    if baseline:
        alpha = baseline.get('agent_net_alpha_gbp')
        verdict = baseline.get('verdict', '—')
        realised = baseline.get('realised_pnl_gbp')
        null_pnl = baseline.get('null_agent_pnl_gbp')
        lines.append(f"- **Baseline α ({baseline.get('period_days', '?')}d):** "
                     f"£{alpha} ({verdict}); realised £{realised} vs null £{null_pnl}")

    if calib:
        brier = calib.get('brier_score')
        diag = (calib.get('diagnosis') or {}).get('diagnosis', '—')
        drift = (calib.get('diagnosis') or {}).get('confidence_drift')
        drift_str = f", drift {drift:+.2f}" if isinstance(drift, (int, float)) else ""
        lines.append(f"- **Calibration:** Brier {brier} ({diag}{drift_str})")

    lines.append("")
    lines.append("If tier is Probation, close_position and veto authority are **blocked** "
                 "at the dispatcher. Tighten and log only. Promotion is automatic when the "
                 "ledger and calibration clear the gates.")
    return '\n'.join(lines) + '\n'


def section_track_record():
    tr = _safe_read(LOGS_DIR / 'apex-agent-track-record.json', {}) or {}
    if not tr.get('total_actions'):
        return "## Your Track Record\n\nNo data yet. Act, log, and learn.\n"
    lines = ["## Your Track Record\n",
             f"- Total actions: **{tr['total_actions']}**"]
    for atype, stats in (tr.get('by_type') or {}).items():
        lines.append(f"- {atype}: {stats.get('count', 0)} actions, "
                     f"accuracy={stats.get('accuracy', 'unknown')}")
    if tr.get('pnl_impact'):
        lines.append(f"- Estimated P&L impact: {tr['pnl_impact']}")
    if tr.get('lesson'):
        lines.append(f"- **Key lesson:** {tr['lesson']}")
    return '\n'.join(lines) + '\n'


def section_gates():
    gates = _safe_read(GATES_FILE, {}) or {}
    if not gates:
        return ""
    lines = ["## Decision Gates (live, editable)\n",
             "These thresholds control behaviour. Read before judging a signal.\n"]
    for section, items in gates.items():
        if section.startswith('_'):
            continue
        lines.append(f"### {section}")
        if isinstance(items, dict):
            for k, v in items.items():
                lines.append(f"- `{k}`: **{v}**")
        lines.append("")
    return '\n'.join(lines) + '\n'


def section_capabilities():
    manifest = _safe_read(MANIFEST, {}) or {}
    chains = _safe_read(CHAINS, {}) or {}
    tools = manifest.get('tools', [])

    by_safety = {}
    for t in tools:
        by_safety.setdefault(t['safety'], []).append(t['name'])

    lines = ["## Capabilities You Have\n",
             "Invoke via `python3 apex-tool-runner.py --run <name>`. "
             "All results come back as JSON with an explicit `status` field.\n"]

    order = ['read', 'write-log', 'external-fetch', 'execute-signal', 'execute-trade']
    for safety in order:
        names = sorted(by_safety.get(safety, []))
        if not names:
            continue
        gate = ' _(requires --force)_' if safety == 'execute-trade' else ''
        lines.append(f"**{safety}{gate}** — {len(names)} tools:")
        lines.append('  ' + ', '.join(f"`{n}`" for n in names))
        lines.append("")

    chain_dict = chains.get('chains', {})
    if chain_dict:
        lines.append("### Chains (multi-step workflows)\n")
        for name, c in chain_dict.items():
            lines.append(f"- **{name}** — {c.get('description', '')} "
                         f"({len(c.get('steps', []))} steps)")
        lines.append("")

    return '\n'.join(lines)


def section_guidelines():
    return """\
## Operating Guidelines

1. **Read before acting.** Current state is in the sections above; don't re-query
   if it's already here. If a section shows data >2h stale, treat with suspicion.
2. **Completion is explicit.** Every tool returns `{"status": "ok"|"error"|"blocked"}`.
   Stop looping when you have the information you need — don't chase tools.
3. **Safety is layered.** `execute-trade` tools are gated; `tighten_stop` is the one
   exception (one-directional safety baked in). If you need to close a position or
   open a new one, call `request_confirmation` and wait.
4. **Log what you do.** Every autonomous action → `log_agent_action` with a reason
   and confidence. Your track record depends on this.
5. **Fail closed.** If state is missing, circuit breaker is unknown, or the market
   calendar is stale → protect only, do not open new risk.
"""


# ── Orchestrator ──────────────────────────────────────────────────────────────

def build():
    parts = [
        section_identity(),
        section_market(),
        section_regime(),
        section_positions(),
        section_signals(),
        section_recent_actions(),
        section_ledger(),
        section_accountability(),
        section_track_record(),
        section_gates(),
        section_capabilities(),
        section_guidelines(),
        f"\n---\n_Generated {datetime.now(timezone.utc).isoformat()} by apex-context-builder.py_\n",
    ]
    body = '\n'.join(p for p in parts if p)

    tmp = OUT_FILE.with_suffix('.md.tmp')
    tmp.write_text(body)
    os.replace(tmp, OUT_FILE)
    return OUT_FILE, len(body)


def main():
    path, size = build()
    print(json.dumps({
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'path': str(path),
        'bytes': size,
    }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
