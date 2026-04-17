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
    raw_layers = weights.get('layers') or weights.get('layer_weights') or {}
    # Convert layers dict {NAME: {weight: X, accuracy: Y}} → {NAME: weight}
    layer_w = {k: v['weight'] if isinstance(v, dict) else v
               for k, v in raw_layers.items()}
    n_matched = weights.get('n_signals_matched', 0)
    brier    = (weights.get('calibration') or {}).get('brier_score')

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
    results = edge.get('by_signal_type') or edge.get('results') or {}

    lines.append(f"*🔬 Edge Proof* (updated weekly)")
    for sig_type, data in results.items():
        verdict  = data.get('verdict', 'NOT_PROVEN')
        wr_real  = data.get('win_rate_pct') or data.get('win_rate_real') or 0
        if wr_real > 1:  # stored as 50.0 not 0.50
            wr_real = wr_real / 100
        n_real   = data.get('n_real', 0)
        icon = '✅' if verdict == 'CONFIRMED' else ('🟡' if verdict == 'MARGINAL' else '❌')
        lines.append(f"  {icon} {sig_type:<18} {wr_real*100:.0f}% WR (n={n_real})")
    if not results:
        lines.append("  No edge data yet")
    lines.append("")

    # ── 5. Trajectory insights ─────────────────────────────────────────────
    traj = safe_read(f'{LOGS}/apex-trajectory-insights.json', {})
    early_cut = traj.get('early_cut') or {}
    t2_runner = traj.get('t2_runner') or {}
    day1      = traj.get('day1_direction_accuracy') or {}
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


# ─────────────────────────────────────────────────────────────────────────────
# Weekly synthesis (runs Mondays 07:05 UTC)
# ─────────────────────────────────────────────────────────────────────────────

STATE_FILE = f'{LOGS}/apex-learning-digest-state.json'


def _stats_by_type_weekly(trades: list) -> dict:
    """Return WR, avg_r, avg_hold_days per signal type."""
    by_type: dict = {}
    for t in trades:
        st = t.get('signal_type', 'UNKNOWN')
        if st not in by_type:
            by_type[st] = {'wins': 0, 'total': 0, 'r_sum': 0.0, 'hold_days': []}
        by_type[st]['total'] += 1
        pnl = t.get('pnl', 0)
        if pnl > 0:
            by_type[st]['wins'] += 1
        by_type[st]['r_sum'] += t.get('r_achieved', 0)
        opened = t.get('opened', '')
        closed = t.get('closed', '')
        try:
            from datetime import datetime as _dtp
            d0 = _dtp.strptime(opened, '%Y-%m-%d')
            d1 = _dtp.strptime(closed, '%Y-%m-%d')
            by_type[st]['hold_days'].append((d1 - d0).days)
        except Exception:
            pass
    result = {}
    for st, v in by_type.items():
        n = v['total']
        result[st] = {
            'n':        n,
            'wr':       round(v['wins'] / n * 100, 1) if n else 0,
            'avg_r':    round(v['r_sum'] / n, 2) if n else 0,
            'avg_hold': round(sum(v['hold_days']) / len(v['hold_days']), 1) if v['hold_days'] else 0,
        }
    return result


def _recommend_weekly(by_type, model_ev, empirical_ev, t1_reach_pct, stop_eff) -> str:
    if model_ev and empirical_ev and empirical_ev < model_ev * 0.5:
        return "Lower T1 targets — model over-predicts by >2x. Empirical peak is below T1."
    if t1_reach_pct is not None and t1_reach_pct < 0.15:
        return f"T1 reach {t1_reach_pct:.0%} — tighten T1 or take profits earlier near optimal exit."
    if stop_eff == 'WIDE':
        return "Stops are too loose — tighten initial stop width to reduce capital at risk."
    worst = min(by_type.items(), key=lambda x: x[1]['avg_r'], default=(None, {}))
    if worst[0] and worst[1].get('n', 0) >= 3 and worst[1].get('avg_r', 0) < 0:
        return f"Review {worst[0]} strategy — negative avg R ({worst[1]['avg_r']}R). Consider pausing."
    return "Continue current strategy mix — no major issues detected."


def build_weekly_digest(dry_run: bool = False) -> str:
    """Weekly synthesis: richer learning analysis for Monday morning."""
    today_str = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    lines = []
    lines.append(f"📚 WEEKLY LEARNING DIGEST — {today_str}")
    lines.append("")

    # ── 1. Outcomes ──────────────────────────────────────────────────────────
    outcomes    = safe_read(f'{LOGS}/apex-outcomes.json', {'trades': []})
    if not isinstance(outcomes, dict):
        outcomes = {'trades': []}
    all_trades  = outcomes.get('trades', [])
    total       = len(all_trades)
    winners     = [t for t in all_trades if t.get('pnl', 0) > 0]
    wr_all      = _pct(len(winners), total)
    avg_r_all   = round(sum(t.get('r_achieved', 0) for t in all_trades) / total, 2) if total else 0
    week_trades = [t for t in all_trades if (t.get('closed') or t.get('opened', '')) >= week_ago]
    week_wins   = [t for t in week_trades if t.get('pnl', 0) > 0]
    week_wr     = _pct(len(week_wins), len(week_trades)) if week_trades else None
    week_avg_r  = round(sum(t.get('r_achieved', 0) for t in week_trades) / len(week_trades), 2) if week_trades else None
    by_type     = _stats_by_type_weekly(all_trades)

    state       = safe_read(STATE_FILE, {})
    last_count  = state.get('last_trade_count', 0)
    new_this_wk = total - last_count

    if week_trades:
        lines.append(f"📊 This week: {len(week_trades)} trades | WR {week_wr}% | avg {week_avg_r}R")
    lines.append(f"   All-time:  {total} trades | WR {wr_all}% | avg {avg_r_all}R")
    lines.append(f"   +{new_this_wk} new trades vs last week")
    lines.append("")

    lines.append("📈 By strategy:")
    for st, v in sorted(by_type.items(), key=lambda x: -x[1]['n']):
        lines.append(f"  {st}: n={v['n']} WR={v['wr']}% avgR={v['avg_r']} hold={v['avg_hold']}d")
    lines.append("")

    # ── 2. Track record ───────────────────────────────────────────────────────
    track = safe_read(f'{LOGS}/apex-agent-track-record.json', {})
    if isinstance(track, dict):
        by_act = track.get('by_type', {})
        stop_d  = by_act.get('stop_tightened', {})
        veto_d  = by_act.get('signal_vetoed', {})
        bene    = stop_d.get('beneficial', 0)
        prem    = stop_d.get('premature_exits', 0)
        st_tot  = bene + prem
        st_acc  = f"{round(bene/st_tot*100)}%" if st_tot else "n/a"
        vc_ok   = veto_d.get('correct', 0)
        vc_tot  = veto_d.get('count', 0)
        vc_acc  = f"{round(vc_ok/vc_tot*100)}%" if vc_tot else "n/a"
        lines.append(f"🤖 Agent: stop-tighten accuracy={st_acc} | veto accuracy={vc_acc}")
        lines.append("")

    # ── 3. MAE/MFE ───────────────────────────────────────────────────────────
    cal  = safe_read(f'{LOGS}/apex-mae-mfe-calibration.json', {})
    agg  = cal.get('aggregate', {}) if isinstance(cal, dict) else {}
    mfe  = agg.get('mfe', {})
    mae  = agg.get('mae', {})
    ev_c = agg.get('ev_cmp', {})
    model_ev     = ev_c.get('model_ev')
    empirical_ev = ev_c.get('empirical_ev')
    t1_reach_pct = mfe.get('reached_t1_pct')
    stop_eff     = mae.get('stop_efficiency', 'UNKNOWN')
    opt_exit     = mfe.get('optimal_exit_r')
    n_cal        = cal.get('n_trades_total', total)
    ev_err_pct   = ev_c.get('ev_model_error_pct')

    if model_ev and empirical_ev and n_cal >= 5:
        overest = f"{ev_err_pct:.0f}%" if ev_err_pct else "?"
        lines.append(f"🎯 EV: model={model_ev:.2f}R | empirical={empirical_ev:.2f}R | err={overest}")
        lines.append(f"   T1 reach={t1_reach_pct:.0%} | opt exit={opt_exit}R | stop={stop_eff}")
    else:
        lines.append(f"🎯 EV calibration: {n_cal} trades (need 5+ for analysis)")
    lines.append("")

    # ── 4. Learned weights ────────────────────────────────────────────────────
    wt_data = safe_read(f'{LOGS}/apex-learned-weights.json', {})
    layers  = wt_data.get('layers', {}) if isinstance(wt_data, dict) else {}
    sorted_l  = sorted(layers.items(), key=lambda x: x[1].get('weight', 1.0))
    penalised = sorted_l[:3]
    boosted   = sorted_l[-3:][::-1]
    lines.append("⚖️ Scoring weights (vs neutral=1.0):")
    for nm, d in boosted:
        lines.append(f"  ↑ {nm}: {d.get('weight', 1.0):.3f}")
    for nm, d in penalised:
        lines.append(f"  ↓ {nm}: {d.get('weight', 1.0):.3f}")
    lines.append("")

    # ── 5. Edge proof ─────────────────────────────────────────────────────────
    edge  = safe_read(f'{LOGS}/apex-edge-proof.json', {})
    ep_by = edge.get('by_signal_type', {}) if isinstance(edge, dict) else {}
    lines.append("🔬 Edge proof:")
    for st, ep in ep_by.items():
        verdict  = ep.get('verdict', 'UNKNOWN')
        n_real   = ep.get('n_real', 0)
        needed   = ep.get('trades_needed_to_pass', None)
        needed_s = f" (need {needed} more)" if needed else ""
        wr_ep    = ep.get('win_rate_pct') or 0
        lines.append(f"  {st}: {verdict} | n={n_real} | WR={wr_ep:.0f}%{needed_s}")
    lines.append("")

    # ── Recommended action ────────────────────────────────────────────────────
    rec = _recommend_weekly(by_type, model_ev, empirical_ev, t1_reach_pct, stop_eff)
    lines.append(f"💡 Action: {rec}")

    msg = "\n".join(lines[:30])
    print(msg)

    if not dry_run:
        send_telegram(msg)
        try:
            import json as _json
            with open(STATE_FILE, 'w') as _sf:
                _json.dump({'last_run': today_str, 'last_trade_count': total}, _sf, indent=2)
        except Exception as _se:
            log_warning(f"Could not save digest state: {_se}")

    return msg


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--weekly', action='store_true', help='Run weekly synthesis (default: daily)')
    parser.add_argument('--dry-run', action='store_true', help='Print only, no Telegram')
    args = parser.parse_args()

    if args.weekly:
        build_weekly_digest(dry_run=args.dry_run)
    else:
        digest = build_digest()
        print(digest)
        if not args.dry_run:
            send_telegram(digest)
