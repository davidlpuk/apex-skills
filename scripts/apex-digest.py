#!/usr/bin/env python3
"""
Apex Digest — plain-English summary of what Apex is doing right now.
Called by Telegram DIGEST command and midday cron (12:30 UTC).
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import send_telegram, t212_request, safe_read, log_error
except ImportError:
    def send_telegram(m): print(m)
    def t212_request(p): return None
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except: return d
    def log_error(m): print(f'ERROR: {m}')

LOGS = '/home/ubuntu/.picoclaw/logs'

def _age_h(filepath):
    try:
        return (datetime.now(timezone.utc).timestamp() - os.path.getmtime(filepath)) / 3600
    except:
        return 999

def _fmt_pnl(pnl):
    return f'+£{pnl:.2f}' if pnl >= 0 else f'-£{abs(pnl):.2f}'

def build_digest():
    now   = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    lines = []

    # ── Header ────────────────────────────────────────────────────────
    lines.append(f'📋 APEX DIGEST — {now.strftime("%a %d %b, %H:%M UTC")}')
    lines.append('')

    # ── 1. Positions ──────────────────────────────────────────────────
    portfolio = t212_request('/equity/portfolio') or []
    apex_pos  = safe_read(f'{LOGS}/apex-positions.json', [])
    cash_data = t212_request('/equity/account/cash') or {}

    free     = float(cash_data.get('free', 0))
    invested = float(cash_data.get('invested', 0))
    total    = round(free + invested, 2)

    if portfolio:
        t212_map  = {p['ticker']: p for p in portfolio}
        total_pnl = sum(float(p.get('ppl', 0)) for p in portfolio)
        lines.append(f'💼 POSITIONS ({len(portfolio)}) — Portfolio £{total:.2f}')

        for ap in apex_pos:
            ticker  = ap.get('t212_ticker', '')
            t212    = t212_map.get(ticker, {})
            current = float(t212.get('currentPrice', ap.get('current', 0)))
            pnl     = float(t212.get('ppl', 0))
            entry   = float(ap.get('entry', 0))
            stop    = float(ap.get('stop', 0))
            t1      = float(ap.get('target1', 0))
            t2      = float(ap.get('target2', 0))
            pct     = round((current - entry) / entry * 100, 1) if entry else 0
            icon    = '🟢' if pnl >= 0 else '🔴'
            stop_dist = round((current - stop) / current * 100, 1) if current else 0

            lines.append(
                f'  {icon} {ap.get("name", ticker)}\n'
                f'     £{entry:.2f} → £{current:.2f} ({pct:+.1f}%) | P&L {_fmt_pnl(pnl)}\n'
                f'     Stop £{stop:.2f} ({stop_dist:.1f}% away) | T1 £{t1:.2f} | T2 £{t2:.2f}'
            )

        lines.append(f'  ─────────────────────────────────')
        lines.append(f'  Total open P&L: {_fmt_pnl(total_pnl)} | Cash free: £{free:.2f}')
    else:
        lines.append(f'💼 No open positions | Cash: £{free:.2f}')

    lines.append('')

    # ── 2. Pending signal ─────────────────────────────────────────────
    sig = safe_read(f'{LOGS}/apex-pending-signal.json', {})
    if sig and sig.get('name') and sig.get('entry'):
        sig_age = round(_age_h(f'{LOGS}/apex-pending-signal.json'), 1)
        sig_type = sig.get('signal_type', 'TREND')
        type_icon = {'CONTRARIAN': '🔄', 'TREND': '📈', 'GEO_REVERSAL': '🌍', 'INVERSE': '📉'}.get(sig_type, '📈')
        lines.append(f'📡 PENDING SIGNAL ({sig_age}h old)')
        lines.append(
            f'  {type_icon} {sig.get("name")} — {sig_type}\n'
            f'  Score: {sig.get("adjusted_score",0)}/10 | Confidence: {sig.get("confidence_pct",0):.0f}%\n'
            f'  Entry: £{sig.get("entry")} | Stop: £{sig.get("stop")} | Qty: {sig.get("quantity")}\n'
            f'  T1: £{sig.get("target1",0)} | T2: £{sig.get("target2",0)}'
        )
        adjs = sig.get('adjustments', [])
        if adjs:
            lines.append(f'  Why this signal:')
            for a in adjs[:4]:
                lines.append(f'    • {a}')
        lines.append('  ➡️ Reply CONFIRM to execute | REJECT to discard')
    else:
        lines.append(f'📡 No pending signal')
        # Tell them when the next scan is
        h = now.hour
        if h < 8:
            lines.append(f'  Next scan: 08:30 UTC today')
        elif now.weekday() < 4:  # Mon-Thu
            lines.append(f'  Next scan: 08:30 UTC tomorrow')
        else:
            lines.append(f'  Next scan: 08:30 UTC Monday')
    lines.append('')

    # ── 3. What Apex did today ─────────────────────────────────────────
    sig_log  = safe_read(f'{LOGS}/apex-signal-log.json', {'signals': []})
    today_sigs = [s for s in sig_log.get('signals', []) if s.get('date') == today]
    ap_config  = safe_read(f'{LOGS}/apex-autopilot.json', {})
    trades_today = ap_config.get('trades_today', 0)
    total_trades = ap_config.get('total_autonomous_trades', 0)

    lines.append(f'🤖 AUTOPILOT — {"ON" if ap_config.get("enabled") else "MANUAL"}')
    lines.append(f'  Trades today: {trades_today} | All-time autonomous: {total_trades}')

    if today_sigs:
        executed = [s for s in today_sigs if s.get('action') == 'CONFIRMED']
        blocked  = [s for s in today_sigs if s.get('action') not in ('CONFIRMED',)]
        if executed:
            lines.append(f'  ✅ Executed today: {", ".join(s["name"] for s in executed)}')
        if blocked:
            for s in blocked[:3]:
                reason = s.get('block_reason', s.get('action', '?'))
                lines.append(f'  ⛔ Blocked: {s.get("name","?")} — {reason}')
    else:
        lines.append(f'  No signals processed today yet')
    lines.append('')

    # ── 4. Market conditions ──────────────────────────────────────────
    lines.append('🌍 MARKET CONDITIONS')

    regime = safe_read(f'{LOGS}/apex-regime.json', {})
    vix     = regime.get('vix', '?')
    breadth = regime.get('breadth_pct', '?')
    overall = regime.get('overall', 'UNKNOWN')
    regime_icon = '✅' if overall == 'CLEAR' else ('🚨' if overall == 'BLOCKED' else '⚠️')
    regime_age  = round(_age_h(f'{LOGS}/apex-regime.json'), 0)
    lines.append(f'  {regime_icon} Regime: VIX {vix} | Breadth {breadth}% | {overall}')
    if regime.get('block_reason'):
        for r in regime.get('block_reason', [])[:2]:
            lines.append(f'    → {r}')

    sent = safe_read(f'{LOGS}/apex-sentiment.json', {})
    if sent:
        sent_val   = sent.get('market_sentiment', 0)
        sent_class = sent.get('market_class', '?')
        sent_age   = round(_age_h(f'{LOGS}/apex-sentiment.json'), 0)
        icon = '🟢' if sent_val > 0.1 else ('🔴' if sent_val < -0.1 else '🟡')
        stale = f' ⚠️ {sent_age:.0f}h old' if sent_age > 24 else ''
        lines.append(f'  {icon} Sentiment: {sent_class} ({sent_val:+.2f}){stale}')

    geo = safe_read(f'{LOGS}/apex-geo-news.json', {})
    geo_overall = geo.get('overall', 'CLEAR')
    geo_icon = '🚨' if geo_overall == 'ALERT' else ('⚠️' if geo_overall == 'WARN' else '✅')
    lines.append(f'  {geo_icon} Geo/macro: {geo_overall}')

    draw = safe_read(f'{LOGS}/apex-drawdown.json', {})
    draw_status = draw.get('status', 'NORMAL')
    draw_pct    = draw.get('drawdown_pct', 0)
    draw_mult   = draw.get('multiplier', 1.0)
    draw_icon   = '✅' if draw_status == 'NORMAL' else ('🚨' if draw_status == 'HALT' else '⚠️')
    lines.append(f'  {draw_icon} Drawdown: {draw_pct}% | Sizing at {int(draw_mult*100)}%')
    lines.append('')

    # ── 5. What happens next ──────────────────────────────────────────
    lines.append('⏱️ WHAT HAPPENS NEXT')
    h = now.hour
    m = now.minute
    is_trading_day = now.weekday() < 5

    if not is_trading_day:
        lines.append('  Weekend — next scan Monday 08:30 UTC')
    elif h < 7:
        lines.append('  07:28 — Sentiment refresh')
        lines.append('  08:03 — Gap protection check (pre-market)')
        lines.append('  08:30 — Morning scan + decision engine')
    elif h < 8:
        lines.append('  08:03 — Gap protection check')
        lines.append('  08:30 — Morning scan + decision engine')
    elif h < 9:
        lines.append('  08:30 — Morning scan starting soon')
    elif h < 14:
        lines.append('  14:32 — US market open gap check')
        lines.append('  Every 30min — stop monitor + broker watchdog')
    elif h < 16:
        lines.append('  Every 30min — stop monitor + broker watchdog')
        lines.append('  15:15 — Fill check')
    else:
        lines.append('  16:30 — End of day review + position summary')
        lines.append('  Tomorrow 07:00 — health check + intelligence refresh')

    # ── 6. System health ──────────────────────────────────────────────
    lines.append('')
    lines.append('🔧 SYSTEM')
    last_health = ''
    try:
        with open(f'{LOGS}/apex-health.log') as f:
            entries = [l.strip() for l in f if l.strip()]
            last_health = entries[-1] if entries else ''
    except: pass

    if 'OK' in last_health:
        health_icon = '✅'
    elif 'WARNING' in last_health:
        health_icon = '⚠️'
    elif 'CRITICAL' in last_health:
        health_icon = '🚨'
    else:
        health_icon = '❓'

    lines.append(f'  {health_icon} Last health check: {last_health[:60] if last_health else "unknown"}')

    # Error count in last 24h
    err_count = 0
    try:
        from datetime import timedelta
        cutoff = (now - timedelta(hours=24)).strftime('%Y-%m-%d')
        with open(f'{LOGS}/apex-errors.log') as f:
            err_count = sum(1 for l in f if l[:10] >= cutoff and '| ERROR |' in l)
    except: pass
    err_icon = '✅' if err_count == 0 else ('⚠️' if err_count <= 10 else '🚨')
    lines.append(f'  {err_icon} Errors (24h): {err_count}')

    return '\n'.join(lines)


def run(silent=False):
    msg = build_digest()
    if not silent:
        print(msg)
    send_telegram(msg)
    return msg


if __name__ == '__main__':
    run()
