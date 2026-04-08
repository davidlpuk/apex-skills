#!/usr/bin/env python3
"""
Apex Opportunity Cost Reviewer

Runs at EOD (16:35 UTC). For every signal that was BLOCKED today by a
safety gate, looks up what actually happened to the price. Reports:
  - What was missed (ticker, block reason, score)
  - What the price did (% move from entry level)
  - Whether it would have been a winner (hit T1) or loser (hit stop)

Output: updates apex-missed-signals.json with outcome data
        sends Telegram digest if anything meaningful was blocked
"""

import json
import os
import sys
from datetime import datetime, timezone, date, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, send_telegram, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except: return d
    def send_telegram(m): print(f'TELEGRAM: {m}')
    def log_warning(m): print(f'WARNING: {m}')

MISSED_FILE = '/home/ubuntu/.picoclaw/logs/apex-missed-signals.json'

def get_price(ticker_yahoo: str) -> float:
    """Fetch current price via yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker_yahoo)
        hist = t.history(period='1d', interval='1m')
        if hist.empty:
            hist = t.history(period='2d')
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
    except Exception:
        pass
    return 0.0

def yahoo_ticker(name: str) -> str:
    """Convert quality universe name to Yahoo Finance ticker."""
    try:
        tmap = safe_read('/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json', {})
        for ticker, data in tmap.items():
            if data.get('name', '').lower() == name.lower():
                # Convert T212 format to Yahoo (e.g. ABBV_US_EQ → ABBV, BPl_EQ → BP.L)
                t212 = data.get('t212', ticker)
                if '_US_EQ' in t212:
                    return t212.replace('_US_EQ', '')
                elif t212.endswith('l_EQ') or t212.endswith('L_EQ'):
                    base = t212.replace('l_EQ', '').replace('L_EQ', '')
                    return base + '.L'
                return ticker
        return name  # fallback — try name as ticker
    except Exception:
        return name

def run():
    today_str = date.today().isoformat()
    missed = safe_read(MISSED_FILE, [])
    if not isinstance(missed, list):
        missed = []

    # Find today's blocked signals not yet evaluated
    todays = [
        e for e in missed
        if e.get('date') == today_str and e.get('outcome_pct') is None
    ]

    if not todays:
        print(f"No unevaluated missed signals for {today_str}")
        return

    print(f"Evaluating {len(todays)} missed signal(s) from today...")

    notable = []  # signals where outcome was meaningful (>1%)

    for entry in todays:
        name        = entry.get('name', '?')
        entry_price = float(entry.get('entry_price', 0))
        target1     = float(entry.get('target1', 0))
        stop_price  = float(entry.get('stop', 0))
        signal_type = entry.get('signal_type', '?')
        block       = entry.get('block_reason', '?')

        if not entry_price:
            continue

        yticker = yahoo_ticker(name)
        current = get_price(yticker)

        if not current:
            log_warning(f"Could not fetch price for {name} ({yticker})")
            continue

        outcome_pct = round((current - entry_price) / entry_price * 100, 2)
        entry['outcome_pct'] = outcome_pct

        # Determine if it would have been a winner
        would_win = None
        if target1 and stop_price:
            if current >= target1:
                would_win = True   # hit T1
            elif current <= stop_price:
                would_win = False  # hit stop
            else:
                would_win = outcome_pct > 0  # in between — use direction

        entry['would_have_won']  = would_win
        entry['evaluated_price'] = current
        entry['evaluated_at']    = datetime.now(timezone.utc).isoformat()

        win_str = '✅ WIN' if would_win else ('❌ LOSS' if would_win is False else '➡️ OPEN')
        direction = '📈' if outcome_pct > 0 else '📉'
        print(f"  {name} ({signal_type}): entry {entry_price} → {current} "
              f"({outcome_pct:+.1f}%) | {win_str} | blocked: {block[:50]}")

        if abs(outcome_pct) >= 1.0:
            notable.append((name, signal_type, outcome_pct, would_win, block))

    # Save updated log
    atomic_write(MISSED_FILE, missed)

    # Send Telegram digest for notable misses
    if notable:
        lines = [f"🔍 *MISSED SIGNALS — EOD Review* ({today_str})\n"]
        for name, sig_type, pct, win, block in sorted(notable, key=lambda x: abs(x[2]), reverse=True):
            icon = '✅' if win else ('❌' if win is False else '➡️')
            dir_icon = '📈' if pct > 0 else '📉'
            lines.append(f"{icon} {dir_icon} *{name}* ({sig_type}): {pct:+.1f}%")
            lines.append(f"   Blocked: _{block[:60]}_\n")

        wins   = sum(1 for *_, w, __ in notable if w is True)
        losses = sum(1 for *_, w, __ in notable if w is False)
        lines.append(f"\n_If gates hadn't blocked: {wins}W / {losses}L_")
        lines.append(f"_Review gate calibration if >2 notable misses per week._")

        send_telegram('\n'.join(lines))
    else:
        print("No notable missed signals today (all moves < 1%)")

GATE_STATS_FILE = '/home/ubuntu/.picoclaw/logs/apex-gate-stats.json'

# Maps first-word patterns in block_reason to a canonical gate name.
# Order matters — more specific patterns first.
_GATE_PATTERNS = [
    ('VIX EXTREME',        'VIX_EXTREME'),
    ('VIX HIGH',           'VIX_HIGH'),
    ('Regime blocked',     'REGIME'),
    ('Sector breadth',     'SECTOR_BREADTH'),
    ('Market direction',   'MARKET_DIRECTION'),
    ('Geo risk',           'GEO'),
    ('Earnings block',     'EARNINGS'),
    ('News block',         'NEWS'),
    ('Portfolio heat',     'PORTFOLIO_HEAT'),
    ('Adversarial block',  'ADVERSARIAL'),
    ('Win rate',           'WIN_RATE'),
    ('EV negative',        'EV_GATE'),
    ('Kelly ABORT',        'KELLY'),
    ('Circuit breaker',    'CIRCUIT_BREAKER'),
    ('Drawdown',           'DRAWDOWN'),
    ('Futures gap',        'FUTURES_GAP'),
]

def _classify_gate(block_reason: str) -> str:
    """Map a block_reason string to a canonical gate name."""
    for pattern, gate in _GATE_PATTERNS:
        if block_reason.startswith(pattern):
            return gate
    return 'OTHER'


def compute_gate_stats():
    """
    Aggregate last 30 days of evaluated missed signals by gate type.
    Calculates false positive rate (FPR) per gate — signals blocked but
    would have been winners. High FPR = gate is miscalibrated.

    Writes apex-gate-stats.json and sends Telegram alert if any gate
    has FPR > 50% with n >= 5 evaluated signals in the last 30 days.
    """
    from datetime import date, timedelta
    cutoff    = (date.today() - timedelta(days=30)).isoformat()
    missed    = safe_read(MISSED_FILE, [])
    if not isinstance(missed, list):
        missed = []

    # Only use evaluated entries from the last 30 days
    evaluated = [
        e for e in missed
        if e.get('date', '') >= cutoff
        and e.get('outcome_pct') is not None
        and e.get('would_have_won') is not None
    ]

    if not evaluated:
        print("No evaluated signals in last 30 days — gate stats skipped")
        return

    # Aggregate per gate
    from collections import defaultdict
    gate_wins   = defaultdict(int)
    gate_losses = defaultdict(int)
    gate_total  = defaultdict(int)

    for entry in evaluated:
        gate = _classify_gate(entry.get('block_reason', ''))
        gate_total[gate] += 1
        if entry.get('would_have_won') is True:
            gate_wins[gate] += 1
        else:
            gate_losses[gate] += 1

    all_gates = sorted(gate_total.keys(), key=lambda g: gate_total[g], reverse=True)

    stats = {}
    warnings = []
    for gate in all_gates:
        n    = gate_total[gate]
        wins = gate_wins[gate]
        fpr  = round(wins / n, 3) if n > 0 else 0.0
        stats[gate] = {
            'n':        n,
            'blocked_winners': wins,
            'blocked_losers':  gate_losses[gate],
            'false_positive_rate': fpr,
        }
        print(f"  Gate {gate:20s}: n={n:3d} | FPR={fpr:.0%} ({wins}W/{gate_losses[gate]}L)")

        # Flag gates with high FPR — they're blocking more winners than losers
        if n >= 5 and fpr > 0.50:
            warnings.append((gate, n, fpr, wins, gate_losses[gate]))

    gate_stats_out = {
        'computed_at':   datetime.now(timezone.utc).isoformat(),
        'window_days':   30,
        'total_evaluated': len(evaluated),
        'gates':         stats,
    }
    atomic_write(GATE_STATS_FILE, gate_stats_out)
    print(f"\nGate stats written → {GATE_STATS_FILE}")

    # Alert if any gate is blocking more winners than losers (n >= 5)
    if warnings:
        lines = ["⚠️ *GATE CALIBRATION ALERT*\n"]
        lines.append("The following gates blocked more winners than losers (last 30 days):\n")
        for gate, n, fpr, wins, losses in sorted(warnings, key=lambda x: -x[2]):
            lines.append(f"  • *{gate}*: {fpr:.0%} false positive rate "
                         f"({wins}W blocked / {losses}L blocked, n={n})")
        lines.append("\n_Action: Review gate thresholds. High FPR means the gate is too aggressive._")
        lines.append("_Check: apex-gate-stats.json for full breakdown._")
        send_telegram('\n'.join(lines))
        log_warning(f"Gate calibration alert: {[g for g,*_ in warnings]}")


if __name__ == '__main__':
    run()
    compute_gate_stats()
