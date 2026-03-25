#!/usr/bin/env python3
"""
Apex Real-Time Regime Monitor
Polls VIX every 5 minutes during market hours and updates apex-regime.json.

Strategy:
  - Every 5 min: fetch VIX only (fast, 1 API call)
  - Every 30 min: full breadth recalc (30 stocks × 200-day EMA)
  - Significant VIX move (>2pts since last run) → Telegram alert
  - Writes apex-regime.json + triggers apex-regime-scaling.py recalc
  - Runs as systemd service: apex-regime-realtime.service

Market hours check: 07:00–17:00 UTC Mon–Fri (covers London + NY opens)
"""
import json
import sys
import time
import subprocess
import importlib.util
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, send_telegram
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
    def safe_read(p, default=None):
        try:
            with open(p) as f: return json.load(f)
        except: return default or {}
    def log_error(m): print(f'ERROR: {m}', flush=True)
    def send_telegram(m):
        try:
            subprocess.run(['/home/ubuntu/.picoclaw/scripts/apex-telegram.sh', m],
                           capture_output=True, timeout=10)
        except: pass

REGIME_FILE   = '/home/ubuntu/.picoclaw/logs/apex-regime.json'
SCALING_FILE  = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
REGIME_SCRIPT = '/home/ubuntu/.picoclaw/scripts/apex-regime-check.py'
SCALING_SCRIPT = '/home/ubuntu/.picoclaw/scripts/apex-regime-scaling.py'
LOG_FILE      = '/home/ubuntu/.picoclaw/logs/apex-regime-realtime.log'

POLL_INTERVAL_SECS  = 300    # 5 minutes — fast VIX-only update
BREADTH_INTERVAL    = 1800   # 30 minutes — full breadth recalc
VIX_ALERT_THRESHOLD = 2.0    # Alert if VIX moves this much in one poll
VIX_EXTREME_LEVEL   = 35.0   # Always alert above this

# Breadth sample — 30 large-cap US stocks (same as apex-regime-check.py)
SAMPLE_BREADTH = [
    'AAPL','MSFT','NVDA','GOOGL','AMZN','META','JPM','JNJ','V','XOM',
    'CVX','PG','KO','MCD','WMT','GS','BAC','UNH','ABBV','DHR',
    'TSLA','ORCL','CRM','AMD','QCOM','BLK','AXP','TMO','DHR','PFE'
]


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    line = f"{ts}: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def is_market_hours() -> bool:
    """True during Mon–Fri 07:00–17:30 UTC (covers LSE open + NY close)."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return 7 <= now.hour < 18


def fetch_vix() -> float | None:
    """Single fast yfinance call for VIX close price."""
    try:
        import yfinance as yf
        hist = yf.Ticker('^VIX').history(period='2d')
        if hist.empty:
            return None
        return round(float(hist['Close'].iloc[-1]), 2)
    except Exception as e:
        log_error(f"VIX fetch failed: {e}")
        return None


def fetch_breadth() -> float | None:
    """Full 30-stock breadth calculation (slow — run every 30 min)."""
    try:
        import yfinance as yf
        above = 0
        checked = 0
        for ticker in SAMPLE_BREADTH:
            try:
                h = yf.Ticker(ticker).history(period='1y')
                if h.empty:
                    continue
                price  = float(h['Close'].iloc[-1])
                ema200 = float(h['Close'].ewm(span=200).mean().iloc[-1])
                if price > ema200:
                    above += 1
                checked += 1
            except:
                pass
        return round(above / checked * 100, 1) if checked > 0 else None
    except Exception as e:
        log_error(f"Breadth fetch failed: {e}")
        return None


def classify_vix(vix: float) -> str:
    if vix < 15:   return "LOW_FEAR"
    if vix < 20:   return "NORMAL"
    if vix < 25:   return "ELEVATED"
    if vix < 30:   return "HIGH"
    return "EXTREME"


def update_regime(vix: float, breadth: float | None, prev_vix: float | None) -> dict:
    """Update apex-regime.json with fresh VIX (and optionally breadth)."""
    existing = safe_read(REGIME_FILE, {})

    # If we don't have a fresh breadth reading, keep the last one
    if breadth is None:
        breadth = existing.get('breadth_pct', 50.0)

    block_reason = []
    if vix >= 35:
        block_reason.append(f"VIX {vix} — extreme fear, no new longs")
    elif vix >= 28:
        block_reason.append(f"VIX {vix} — high fear, reduce position sizes by 50%")
    if breadth < 30:
        block_reason.append(f"Breadth {breadth}% — fewer than 30% of stocks healthy, avoid new longs")
    elif breadth < 60:
        block_reason.append(f"Breadth {breadth}% — neutral, be selective")

    blocked = any('no new longs' in r or 'avoid new longs' in r for r in block_reason)

    result = {
        **existing,   # preserve any extra fields (geo, sentiment, etc.)
        "vix":            vix,
        "vix_regime":     classify_vix(vix),
        "breadth_pct":    breadth,
        "breadth_regime": "BULLISH" if breadth >= 60 else ("NEUTRAL" if breadth >= 40 else "BEARISH"),
        "overall":        "BLOCKED" if blocked else "CLEAR",
        "block_reason":   block_reason,
        "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source":         "realtime",
    }

    atomic_write(REGIME_FILE, result)
    return result


def trigger_scaling() -> None:
    """Run apex-regime-scaling.py to recompute position size multipliers."""
    try:
        subprocess.run(
            ['/home/ubuntu/bin/python3', SCALING_SCRIPT],
            capture_output=True, timeout=30
        )
    except Exception as e:
        log_error(f"Scaling recalc failed: {e}")


def check_vix_alerts(vix: float, prev_vix: float | None) -> None:
    """Send Telegram alert on significant VIX moves."""
    if prev_vix is None:
        return

    move = vix - prev_vix
    if abs(move) >= VIX_ALERT_THRESHOLD:
        direction = "⬆️ SPIKED" if move > 0 else "⬇️ FELL"
        regime    = classify_vix(vix)
        _log(f"VIX alert: {prev_vix} → {vix} ({move:+.1f})")
        send_telegram(
            f"📊 VIX {direction}\n\n"
            f"VIX moved from {prev_vix} → {vix} ({move:+.1f} pts)\n"
            f"Regime: {regime}\n"
            f"{'⚠️ High fear — check position sizes' if vix >= 28 else '✅ Within normal range'}"
        )

    elif vix >= VIX_EXTREME_LEVEL and (prev_vix is None or prev_vix < VIX_EXTREME_LEVEL):
        send_telegram(
            f"🚨 VIX EXTREME: {vix}\n\n"
            f"Market fear at extreme levels. Regime: BLOCKED.\n"
            f"No new longs — review open positions immediately."
        )


def run() -> None:
    _log("=== Apex Real-Time Regime Monitor starting ===")
    _log(f"Poll: every {POLL_INTERVAL_SECS//60} min (VIX) / {BREADTH_INTERVAL//60} min (breadth)")

    last_breadth_time = 0.0
    prev_vix: float | None = None

    # Load last known VIX from existing regime file
    existing = safe_read(REGIME_FILE, {})
    prev_vix = existing.get('vix')
    if prev_vix:
        _log(f"Last known VIX from file: {prev_vix}")

    while True:
        now_ts = time.time()

        if not is_market_hours():
            # Sleep 5 minutes then re-check
            time.sleep(POLL_INTERVAL_SECS)
            continue

        # ── Fetch VIX (every poll) ──────────────────────────────────────────
        vix = fetch_vix()
        if vix is None:
            _log("VIX fetch returned None — skipping this cycle")
            time.sleep(POLL_INTERVAL_SECS)
            continue

        # ── Full breadth recalc every 30 min ───────────────────────────────
        do_breadth = (now_ts - last_breadth_time) >= BREADTH_INTERVAL
        breadth = None
        if do_breadth:
            _log(f"Running full breadth recalc...")
            breadth = fetch_breadth()
            if breadth is not None:
                last_breadth_time = now_ts
                _log(f"Breadth recalc: {breadth}% above 200 EMA")
            else:
                _log("Breadth recalc failed — keeping last value")

        # ── Update regime file ─────────────────────────────────────────────
        result = update_regime(vix, breadth, prev_vix)
        trigger_scaling()

        _log(
            f"VIX={vix} ({result['vix_regime']}) "
            f"Breadth={result['breadth_pct']}% "
            f"Regime={result['overall']} "
            f"{'[breadth updated]' if do_breadth and breadth else ''}"
        )

        # ── Alerts ─────────────────────────────────────────────────────────
        check_vix_alerts(vix, prev_vix)
        prev_vix = vix

        time.sleep(POLL_INTERVAL_SECS)


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        _log("Stopped by user.")
    except Exception as e:
        _log(f"FATAL: {e}")
        sys.exit(1)
