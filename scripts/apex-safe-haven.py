#!/usr/bin/env python3
"""
Safe Haven Early Warning Monitor (Item 6)

Monitors classic flight-to-safety flows every 30 minutes during market hours.
These lead equity sell-offs by 30-90 minutes and are often the first sign of
a geopolitical shock or financial crisis.

Instruments monitored:
  GC=F   — Gold futures (up = fear)
  TLT    — US 20Y Treasury ETF (up = flight to bonds)
  JPY=X  — USD/JPY (down = yen strengthening = fear)
  CHFUSD=X — CHF/USD (up = franc strengthening = fear)
  ES=F   — S&P 500 futures (down = risk-off)
  ^VIX   — VIX (up = fear)

Scoring:
  Each instrument contributes 0-2 points based on move size.
  Total 0-12, classified as CLEAR/ELEVATED/WARNING/CRISIS.

Alert thresholds:
  0-3   CLEAR      — no safe haven flow
  4-5   ELEVATED   — early signs, monitor closely
  6-8   WARNING    — significant safe haven flow, reduce risk
  9-12  CRISIS     — extreme flow, halt new entries
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, atomic_write, log_error, send_telegram
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def send_telegram(m): print(f'TELEGRAM: {m[:80]}')

SAFE_HAVEN_FILE = '/home/ubuntu/.picoclaw/logs/apex-safe-haven.json'

# Instruments: (yahoo_ticker, direction_of_fear, description)
# direction: 'up' = rising price = fear, 'down' = falling price = fear
SAFE_HAVEN_INSTRUMENTS = [
    ('GC=F',     'up',   'Gold futures',           2.0, 1.0),   # >2% = 2pts, >1% = 1pt
    ('TLT',      'up',   'US 20Y Treasury ETF',    1.5, 0.7),   # >1.5% = 2pts, >0.7% = 1pt
    ('JPY=X',    'down', 'USD/JPY (yen strength)', 1.0, 0.5),   # >1% fall = 2pts (yen up)
    ('CHFUSD=X', 'up',   'CHF/USD (franc strength)',1.0, 0.5),  # >1% rise = 2pts
    ('ES=F',     'down', 'S&P 500 futures',        1.5, 0.7),   # >1.5% fall = 2pts
    ('^VIX',     'up',   'VIX fear index',         25.0, 15.0), # >25% spike = 2pts (absolute: >20 = 1pt, >30 = 2pt)
]


def _score_instrument(ticker, direction, big_thresh, small_thresh):
    """
    Fetch intraday return and score 0/1/2 based on move.
    Returns (score, pct_change, current_price).
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period='2d', interval='1h')
        if hist is None or hist.empty or len(hist) < 2:
            return 0, 0.0, None

        current = float(hist['Close'].iloc[-1])
        prev    = float(hist['Close'].iloc[-2])
        if prev == 0:
            return 0, 0.0, current

        pct = (current - prev) / prev * 100

        # VIX uses absolute level, not just pct change
        if ticker == '^VIX':
            if current > 30:
                return 2, pct, current
            elif current > 20:
                return 1, pct, current
            return 0, pct, current

        move = pct if direction == 'up' else -pct  # positive = fear direction

        if move >= big_thresh:
            return 2, pct, current
        elif move >= small_thresh:
            return 1, pct, current
        return 0, pct, current

    except Exception:
        return 0, 0.0, None


def check_safe_haven_flow(verbose=True):
    """
    Check all safe haven instruments and return composite score.
    Returns (score, level, signals, details).
    """
    now     = datetime.now(timezone.utc)
    total   = 0
    signals = []
    details = {}

    for ticker, direction, desc, big_thresh, small_thresh in SAFE_HAVEN_INSTRUMENTS:
        score, pct, price = _score_instrument(ticker, direction, big_thresh, small_thresh)
        total += score

        icon = '🔴' if score == 2 else ('🟡' if score == 1 else '✅')
        entry = {
            'ticker':    ticker,
            'desc':      desc,
            'score':     score,
            'pct':       round(pct, 2),
            'price':     round(price, 4) if price else None,
            'direction': direction,
        }
        details[ticker] = entry

        if score > 0:
            fear_dir = 'rising' if direction == 'up' else 'falling'
            signals.append(f"{icon} {desc}: {pct:+.1f}% ({fear_dir} = fear)")

        if verbose:
            print(f"  {icon} {desc:30} {pct:+6.1f}%  score={score}")

    # Classify level
    if total >= 9:
        level = 'CRISIS'
    elif total >= 6:
        level = 'WARNING'
    elif total >= 4:
        level = 'ELEVATED'
    else:
        level = 'CLEAR'

    return total, level, signals, details


def run(verbose=True):
    """Main entry point — check safe haven flow, alert if needed."""
    now = datetime.now(timezone.utc)

    if verbose:
        print(f"\n=== SAFE HAVEN EARLY WARNING ===")
        print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Load previous state for comparison
    prev_data  = safe_read(SAFE_HAVEN_FILE, {'score': 0, 'level': 'CLEAR'})
    prev_score = prev_data.get('score', 0)
    prev_level = prev_data.get('level', 'CLEAR')

    score, level, signals, details = check_safe_haven_flow(verbose=verbose)

    output = {
        'timestamp': now.isoformat(),
        'score':     score,
        'level':     level,
        'signals':   signals,
        'details':   details,
        'max_score': 12,
    }
    atomic_write(SAFE_HAVEN_FILE, output)

    if verbose:
        icon = {'CLEAR': '✅', 'ELEVATED': '🟡', 'WARNING': '🟠', 'CRISIS': '🔴'}.get(level, '⚠️')
        print(f"\n{icon} Safe Haven Score: {score}/12 — {level}")
        for s in signals:
            print(f"  {s}")

    # Alert when level escalates (not every run)
    levels_order = ['CLEAR', 'ELEVATED', 'WARNING', 'CRISIS']
    prev_idx = levels_order.index(prev_level) if prev_level in levels_order else 0
    curr_idx = levels_order.index(level) if level in levels_order else 0

    if curr_idx > prev_idx and score >= 4:
        icon_map = {'ELEVATED': '🟡', 'WARNING': '🟠', 'CRISIS': '🔴'}
        icon = icon_map.get(level, '⚠️')
        signal_text = '\n'.join(f"  {s}" for s in signals) if signals else '  (no individual signals)'
        msg = (
            f"{icon} SAFE HAVEN FLOW — {level} ({score}/12)\n\n"
            f"Flight-to-safety detected:\n{signal_text}\n\n"
            f"This may precede an equity sell-off by 30-90 min.\n"
            f"Consider reducing position risk."
        )
        send_telegram(msg)

    return score, level


def get_safe_haven_score():
    """Quick read of cached safe haven score — used by autopilot."""
    data = safe_read(SAFE_HAVEN_FILE, {'score': 0, 'level': 'CLEAR'})
    return data.get('score', 0), data.get('level', 'CLEAR')


if __name__ == '__main__':
    run(verbose=True)
