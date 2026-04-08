#!/usr/bin/env python3
"""
Apex Daily Learning Digest

Runs every morning at 07:50 UTC (after all intelligence refreshes complete).
Sends a Telegram summary of how the system is learning and whether performance
is trending in the right direction.

Covers:
  1. Win rate trend (recent vs lifetime)
  2. Bayesian layer weights — which layers are gaining/losing weight
  3. EV accuracy — are predicted EVs matching actual outcomes?
  4. Edge proof status per signal type
  5. Trajectory insights — any new early-cut or T2-runner patterns
  6. Opportunity cost — what blocked signals did yesterday
  7. Action items — flags if any metric needs human attention
"""

import json
import os
import sys
from datetime import datetime, timezone, date, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, send_telegram, log_warning
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except: return d
    def send_telegram(m): print(f'TELEGRAM: {m}')
    def log_warning(m): print(f'WARNING: {m}')

LOGS = '/home/ubuntu/.picoclaw/logs'

def _pct(val, total):
    return round(val / total * 100, 1) if total else 0

def _trend_arrow(recent_wr, lifetime_wr):
    if recent_wr >= lifetime_wr + 5:   return '📈'
    if recent_wr <= lifetime_wr - 5:   return '📉'
    return '➡️'

def build_digest():
    lines = []
    flags = []  # Action items requiring attention
    today = date.today().isoformat()

    lines.append(f"🧠 *APEX LEARNING DIGEST — {today}*\n")

    # ── 1. Win rate trend ──────────────────────────────────────────────────
    outcomes = safe_read(f'{LOGS}/apex-outcomes.json', {})
    trades   = outcomes.get('trades', [])
    summary  = outcomes.get('summary', {})

    total_trades = len(trades)
    lifetime_wr  = summary.get('win_rate', 0)
    lifetime_r   = summary.get('avg_r', 0)

    # Last 7 days
    cutoff_7  = (date.today() - timedelta(days=7)).isoformat()
    recent_7  = [t for t in trades if (t.get('date_closed') or t.get('opened','')) >= cutoff_7]
    wins_7    = sum(1 for t in recent_7 if t.get('pnl', 0) > 0)
    wr_7      = _pct(wins_7, len(recent_7)) if recent_7 else None

    # Last 20 trades (rolling performance)
    last_20   = trades[-20:]
    wins_20   = sum(1 for t in last_20 if t.get('pnl', 0) > 0)
    wr_20     = _pct(wins_20, len(last_20))

    arrow = _trend_arrow(wr_20, lifetime_wr) if len(last_20) >= 5 else '⏳'
    lines.append(f"*📊 Performance*")
    lines.append(f"Total trades: {total_trades} | Lifetime WR: {lifetime_wr}% | Avg R: {lifetime_r}")
    lines.append(f"Last 20 WR: {wr_20}% {arrow}")
    if wr_7 is not None:
        lines.append(f"Last 7 days: {wins_7}/{len(recent_7)} wins ({wr_7}%)")
    lines.append("")

    if wr_20 < 40 and len(last_20) >= 10:
        flags.append("⚠️ Win rate in last 20 trades below 40% — review signal quality")

    # ── 2. Bayesian layer weights ──────────────────────────────────────────
    weights = safe_read(f'{LOGS}/apex-learned-weights.json', {})
    layer_w = weights.get('layer_weights', {})
    n_matched = weights.get('n_signals_matched', 0)
    brier    = weights.get('calibration', {}).get('brier_score')

    lines.append(f"*🎯 Learning Weights* (n={n_matched} matched trades)")
    if layer_w:
        # Show top-3 strongest and bottom-2 weakest
        sorted_w = sorted(layer_w.items(), key=lambda x: x[1], reverse=True)
        top = sorted_w[:3]
        bot = sorted_w[-2:] if len(sorted_w) > 3 else []
        for layer, w in top:
            bar = '█' * int(w * 5)
            lines.append(f"  {layer:<12} {w:.3f} {bar}")
        if bot:
            lines.append(f"  ...")
            for layer, w in bot:
                bar = '░' * max(1, int(w * 5))
                lines.append(f"  {layer:<12} {w:.3f} {bar} ↓")
    else:
        lines.append("  No weight data yet — need 10 matched trades")

    if brier is not None:
        brier_quality = "good" if brier < 0.20 else ("fair" if brier < 0.25 else "poor")
        lines.append(f"  Calibration Brier: {brier:.3f} ({brier_quality})")
    lines.append("")

    if n_matched < 10:
        flags.append(f"⏳ Only {n_matched} matched trades — weights stabilise at 30+")

    # ── 3. Score adapter (tier) status ────────────────────────────────────
    score_adapt  = safe_read(f'{LOGS}/apex-scoring-weights.json', {})
    _meta        = score_adapt.get('_meta', score_adapt)
    adapt_status = _meta.get('status', score_adapt.get('status', 'INACTIVE'))
    _gadj        = score_adapt.get('global_adjustment', {})
    global_adj   = _gadj.get('adjustment', 0) if isinstance(_gadj, dict) else float(_gadj or 0)
    baseline_exp = _meta.get('baseline_expectancy', _gadj.get('expectancy', 0) if isinstance(_gadj, dict) else 0)

    lines.append(f"*⚙️ Score Adapter*: {adapt_status} | Global adj: {global_adj:+.1f} | Expectancy: {baseline_exp:.3f}R")
    tier2_ready = total_trades >= 15
    lines.append(f"  Tier 2 (per-type learning): {'ACTIVE' if tier2_ready else f'Needs {15-total_trades} more trades'}")
    lines.append("")

    # ── 4. Edge proof ──────────────────────────────────────────────────────
    edge = safe_read(f'{LOGS}/apex-edge-proof.json', {})
    results = edge.get('results', {})

    lines.append(f"*🔬 Edge Proof* (updated weekly)")
    for sig_type, data in results.items():
        verdict  = data.get('verdict', 'NOT_PROVEN')
        wr_real  = data.get('win_rate_real', 0)
        n_real   = data.get('n_real', 0)
        icon = '✅' if verdict == 'CONFIRMED' else ('🟡' if verdict == 'MARGINAL' else '❌')
        lines.append(f"  {icon} {sig_type:<18} {wr_real*100:.0f}% WR (n={n_real})")
    if not results:
        lines.append("  No edge data yet")
    lines.append("")

    # ── 5. Trajectory insights ─────────────────────────────────────────────
    traj = safe_read(f'{LOGS}/apex-trajectory-insights.json', {})
    early_cut = traj.get('early_cut', {})
    t2_runner = traj.get('t2_runner', {})
    day1      = traj.get('day1_direction_accuracy', {})
    n_traj    = traj.get('n_trajectories', 0)

    lines.append(f"*📉 Trajectory Learning* (n={n_traj} trades)")
    day1 = day1 or {}
    lines.append(f"  Day-1 negative → loss: {day1.get('negative_day1_loss_rate', 0)*100:.0f}% | Day-1 positive → win: {day1.get('positive_day1_win_rate', 0)*100:.0f}%")
    ec_rec = early_cut.get('recommended', False)
    t2_rec = t2_runner.get('recommended', False)
    lines.append(f"  Early-cut rule: {'✅ ACTIVE' if ec_rec else '⏳ not yet confirmed'}")
    lines.append(f"  T2-runner rule: {'✅ ACTIVE' if t2_rec else '⏳ not yet confirmed'}")
    lines.append("")

    # ── 6. Yesterday's missed signals (opportunity cost) ───────────────────
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    missed_log = safe_read(f'{LOGS}/apex-missed-signals.json', [])
    yesterdays_missed = [
        e for e in (missed_log or [])
        if e.get('date') == yesterday and e.get('outcome_pct') is not None
    ]

    if yesterdays_missed:
        would_have_won  = sum(1 for e in yesterdays_missed if e.get('would_have_won') is True)
        would_have_lost = sum(1 for e in yesterdays_missed if e.get('would_have_won') is False)
        total_missed    = len(yesterdays_missed)
        lines.append(f"*🚫 Yesterday's Blocked Signals* ({total_missed} blocked)")
        lines.append(f"  Would have won: {would_have_won} | Would have lost: {would_have_lost}")
        # Show top 3 notable misses
        notable = sorted(yesterdays_missed, key=lambda x: abs(x.get('outcome_pct', 0)), reverse=True)[:3]
        for e in notable:
            pct = e.get('outcome_pct', 0)
            win = e.get('would_have_won')
            icon = '✅' if win else ('❌' if win is False else '➡️')
            lines.append(f"  {icon} {e['name']}: {pct:+.1f}% (blocked: {e['block_reason'][:40]})")
        if would_have_won > would_have_lost and total_missed >= 3:
            flags.append(f"⚠️ Gates blocked {would_have_won} winners yesterday — review gate calibration")
        lines.append("")

    # ── 6b. Gate calibration stats (30-day FPR per gate) ──────────────────
    gate_stats = safe_read(f'{LOGS}/apex-gate-stats.json', {})
    gate_data  = gate_stats.get('gates', {})
    if gate_data:
        flagged_gates = [
            (g, d) for g, d in gate_data.items()
            if d.get('n', 0) >= 5 and d.get('false_positive_rate', 0) > 0.50
        ]
        if flagged_gates:
            lines.append(f"*🔬 Gate Calibration (30-day FPR)*")
            for gate, d in sorted(flagged_gates, key=lambda x: -x[1]['false_positive_rate']):
                fpr = d['false_positive_rate']
                lines.append(
                    f"  ⚠️ {gate}: {fpr:.0%} FPR "
                    f"({d['blocked_winners']}W blocked / {d['blocked_losers']}L blocked, n={d['n']})"
                )
            flags.append(f"⚠️ {len(flagged_gates)} gate(s) blocking >50% winners — loosen thresholds")
            lines.append("")
        else:
            top_gates = sorted(gate_data.items(), key=lambda x: -x[1].get('n', 0))[:3]
            if top_gates:
                lines.append(f"*🔬 Gate Calibration (30-day)* — no miscalibration detected")
                for gate, d in top_gates:
                    lines.append(
                        f"  ✅ {gate}: {d.get('false_positive_rate', 0):.0%} FPR (n={d.get('n', 0)})"
                    )
                lines.append("")

    # ── 7. Action items ────────────────────────────────────────────────────
    if flags:
        lines.append(f"*🚨 Action Items*")
        for f in flags:
            lines.append(f"  {f}")
    else:
        lines.append(f"✅ _No action items — system learning normally_")

    return '\n'.join(lines)

if __name__ == '__main__':
    digest = build_digest()
    print(digest)
    send_telegram(digest)
