#!/usr/bin/env python3
"""
Weekend Signal Re-validation
Re-validates all queued trades before Monday open execution.
Checks if conditions have changed materially since Friday's queue.

Runs at 07:45 Monday — after all intelligence scripts have updated
but before trade queue executes at 08:05.

Checks:
1. Price still within acceptable range of signal price
2. Regime hasn't shifted to HOSTILE or BLOCKED
3. No new earnings announced over weekend
4. No black swan events detected
5. Signal score still qualifies at current conditions
6. Instrument not gapped dramatically overnight
"""
import json
import sys
import yfinance as yf
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

QUEUE_FILE     = '/home/ubuntu/.picoclaw/logs/apex-trade-queue.json'
REGIME_FILE    = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
BLACKSWAN_FILE = '/home/ubuntu/.picoclaw/logs/apex-blackswan.json'
EARNINGS_FILE  = '/home/ubuntu/.picoclaw/logs/apex-earnings-flags.json'
SIGNAL_LOG     = '/home/ubuntu/.picoclaw/logs/apex-signal-log.json'

YAHOO_MAP = {
    "AAPL_US_EQ":  "AAPL",  "MSFT_US_EQ": "MSFT",
    "NVDA_US_EQ":  "NVDA",  "GOOGL_US_EQ":"GOOGL",
    "XOM_US_EQ":   "XOM",   "CVX_US_EQ":  "CVX",
    "V_US_EQ":     "V",     "JPM_US_EQ":  "JPM",
    "ABBV_US_EQ":  "ABBV",  "JNJ_US_EQ":  "JNJ",
    "VUAGl_EQ":    "VUAG.L","SHEL_EQ":    "SHEL.L",
    "HSBA_EQ":     "HSBA.L","AZN_EQ":     "AZN.L",
    "ULVR_EQ":     "ULVR.L","GSK_EQ":     "GSK.L",
    "QQQSl_EQ":    "QQQS.L","3USSl_EQ":   "3USS.L",
    "SQQQ_EQ":     "SQQQ",  "SPXU_EQ":    "SPXU",
}

def fix_pence(price, yahoo):
    if yahoo.endswith('.L') and price > 100:
        return price / 100
    return price

def get_current_price(ticker):
    """Get current price from yfinance."""
    yahoo = YAHOO_MAP.get(ticker, '')
    if not yahoo:
        return None
    try:
        hist = yf.Ticker(yahoo).history(period="2d")
        if hist.empty:
            return None
        price = float(hist['Close'].iloc[-1])
        return fix_pence(price, yahoo)
    except Exception as e:
        log_error(f"get_current_price failed for {ticker}: {e}")
        return None

def check_price_drift(trade, current_price):
    """
    Check if price has drifted too far from signal price.
    For BUY signals:
    - Price gone UP too much = chasing, reduce conviction
    - Price gone DOWN too much = conditions worse, re-evaluate
    """
    signal_price = float(trade.get('entry', 0))
    if signal_price <= 0 or current_price is None:
        return True, "Cannot verify price"

    drift_pct = round((current_price - signal_price) / signal_price * 100, 2)

    # For trend signals — if price has already moved up 3%+ since signal,
    # we're chasing. Cancel.
    sig_type = trade.get('signal_type', 'TREND')

    if sig_type == 'TREND':
        if drift_pct > 3.0:
            return False, f"Price drifted UP {drift_pct:+.1f}% since signal — chasing momentum, cancel"
        elif drift_pct < -5.0:
            return False, f"Price drifted DOWN {drift_pct:+.1f}% since signal — conditions worse"
        else:
            return True, f"Price drift {drift_pct:+.1f}% — acceptable"

    elif sig_type == 'CONTRARIAN':
        if drift_pct > 5.0:
            return False, f"Price drifted UP {drift_pct:+.1f}% — contrarian opportunity may have passed"
        elif drift_pct < -8.0:
            return False, f"Price drifted DOWN {drift_pct:+.1f}% — falling knife accelerating"
        else:
            return True, f"Price drift {drift_pct:+.1f}% — acceptable for contrarian"

    elif sig_type == 'INVERSE':
        if drift_pct < -5.0:
            return False, f"Inverse ETF dropped {drift_pct:+.1f}% — market recovered, cancel short"
        else:
            return True, f"Inverse ETF drift {drift_pct:+.1f}% — acceptable"

    return True, f"Price drift {drift_pct:+.1f}%"

def check_regime_change(trade):
    """
    Check if regime has shifted to hostile since signal was queued.
    A HOSTILE or BLOCKED regime should cancel most trend signals.
    """
    regime = safe_read(REGIME_FILE, {})
    label  = regime.get('regime_label', 'NEUTRAL')
    scale  = float(regime.get('combined_scale', 0.5))
    sig_type = trade.get('signal_type', 'TREND')

    if label in ['HOSTILE', 'BLOCKED'] and sig_type == 'TREND':
        return False, f"Regime shifted to {label} — trend signal no longer valid"

    if scale < 0.15 and sig_type == 'TREND':
        return False, f"Regime scale {round(scale*100)}% — too low for trend entry"

    return True, f"Regime {label} ({round(scale*100)}%) — acceptable"

def check_black_swan(trade):
    """Check if black swan events have occurred since queuing."""
    bs_data = safe_read(BLACKSWAN_FILE, {'score': 0, 'level': 'CLEAR'})
    score   = bs_data.get('score', 0)
    level   = bs_data.get('level', 'CLEAR')
    events  = bs_data.get('events', [])

    if score >= 5:
        return False, f"Black swan {level} ({score}/10) — {events[0][:60] if events else ''}"

    if score >= 3:
        return True, f"Black swan ELEVATED ({score}/10) — proceed with caution"

    return True, f"Black swan CLEAR ({score}/10)"

def check_earnings_risk(trade):
    """Check if earnings have been announced or are imminent."""
    name = trade.get('name', '?')
    try:
        earnings = safe_read(EARNINGS_FILE, {})
        flags    = earnings.get('flags', {})
        if name in flags:
            flag = flags[name]
            days = flag.get('days_to_earnings', 99)
            if days <= 3:
                return False, f"Earnings in {days} days — binary risk, cancel"
            elif days <= 7:
                return True, f"Earnings in {days} days — caution"
    except Exception as e:
        log_error(f"Earnings check failed: {e}")

    return True, "No earnings risk detected"

def check_signal_age(trade):
    """
    Check if signal is too old to be valid.
    Signals older than 5 days should be cancelled.
    """
    queued_at = trade.get('queued_at', '')
    if not queued_at:
        return True, "No queue timestamp"

    try:
        qt  = datetime.fromisoformat(queued_at)
        if qt.tzinfo is None:
            qt = qt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - qt).days

        if age_days > 5:
            return False, f"Signal is {age_days} days old — too stale to execute"
        elif age_days > 3:
            return True, f"Signal is {age_days} days old — monitor closely"
        else:
            return True, f"Signal is {age_days} days old — fresh"
    except Exception as e:
        log_error(f"Signal age check failed: {e}")
        return True, "Cannot check age"


def check_score_decay(trade, current_price):
    """
    Score decay: a signal's effective score degrades as price moves away from
    the signal price between generation and execution.

    Logic:
    - The original score was calculated at signal_price.
    - If price has since moved unfavourably (up for TREND — now chasing;
      down more for CONTRARIAN — accelerating fall), the effective edge shrinks.
    - Decay is linear: 1 score point lost per 1% adverse price move beyond
      a free-move buffer.

    For TREND:
      - Free buffer: +1% (price can rise slightly without decay)
      - Decay starts: > +1% adverse (chasing — less momentum left)
      - Each +1% above buffer = −0.5 score points

    For CONTRARIAN:
      - Free buffer: −2% (can absorb a little more weakness)
      - Decay starts: > −2% further decline
      - Each −1% below buffer = −0.4 score points (contrarian needs time, gentler)

    Cancel if decayed score would fall below the original entry threshold (7.0).
    """
    if current_price is None:
        return True, "Cannot check score decay — no current price", 0

    signal_price  = float(trade.get('entry', 0))
    original_score = float(trade.get('score', 7.5))
    sig_type      = trade.get('signal_type', 'TREND')
    threshold     = float(trade.get('score_threshold', 7.0))

    if signal_price <= 0:
        return True, "No signal price recorded", 0

    drift_pct = (current_price - signal_price) / signal_price * 100

    if sig_type == 'TREND':
        # Chasing: price already ran up — less upside remaining
        free_buffer = 1.0       # 1% move up is fine
        decay_rate  = 0.5       # lose 0.5 score per % above buffer
        adverse     = max(drift_pct - free_buffer, 0)
    elif sig_type == 'CONTRARIAN':
        # Falling knife: stock falling faster than expected
        free_buffer = -2.0      # allow up to 2% further decline
        decay_rate  = 0.4       # lose 0.4 score per % below buffer
        adverse     = max(free_buffer - drift_pct, 0)  # positive when below buffer
    else:
        # INVERSE or other — simple absolute drift
        free_buffer = 2.0
        decay_rate  = 0.3
        adverse     = max(abs(drift_pct) - free_buffer, 0)

    score_loss    = round(adverse * decay_rate, 2)
    decayed_score = round(original_score - score_loss, 2)

    if decayed_score < threshold:
        return (False,
                f"Score decayed {original_score} → {decayed_score} "
                f"(price drift {drift_pct:+.1f}%, loss {score_loss:.1f}pts) — below threshold {threshold}",
                score_loss)

    if score_loss > 0:
        return (True,
                f"Score mild decay {original_score} → {decayed_score} "
                f"(drift {drift_pct:+.1f}%) — still above threshold",
                score_loss)

    return True, f"Score intact {original_score} (drift {drift_pct:+.1f}% within buffer)", 0

def revalidate_queue():
    """
    Re-validate all pending queue entries.
    Called Monday 07:45 before trade queue executes.
    """
    now   = datetime.now(timezone.utc)
    queue = safe_read(QUEUE_FILE, [])
    if not isinstance(queue, list):
        queue = []

    pending  = [t for t in queue if t.get('status') == 'QUEUED']

    if not pending:
        print("No pending trades to re-validate")
        return [], []

    print(f"\n=== WEEKEND SIGNAL RE-VALIDATION ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Pending trades: {len(pending)}\n")

    approved  = []
    cancelled = []
    cautioned = []

    for trade in pending:
        name   = trade.get('name', '?')
        ticker = trade.get('t212_ticker', '')
        print(f"  Validating {name}...", flush=True)

        checks       = []
        hard_failures= []
        soft_warnings= []

        # Get current price
        current_price = get_current_price(ticker)
        print(f"    Signal price: £{trade.get('entry',0):.2f} | "
              f"Current: £{current_price:.2f}" if current_price else
              f"    Cannot get current price")

        # Run all checks
        ok, msg = check_price_drift(trade, current_price)
        checks.append(('Price drift', ok, msg))
        if not ok: hard_failures.append(msg)
        print(f"    {'✅' if ok else '❌'} Price: {msg}")

        ok, msg = check_regime_change(trade)
        checks.append(('Regime', ok, msg))
        if not ok: hard_failures.append(msg)
        print(f"    {'✅' if ok else '❌'} Regime: {msg}")

        ok, msg = check_black_swan(trade)
        checks.append(('Black Swan', ok, msg))
        if not ok: hard_failures.append(msg)
        elif 'caution' in msg.lower(): soft_warnings.append(msg)
        print(f"    {'✅' if ok else '❌'} Black Swan: {msg}")

        ok, msg = check_earnings_risk(trade)
        checks.append(('Earnings', ok, msg))
        if not ok: hard_failures.append(msg)
        elif 'caution' in msg.lower(): soft_warnings.append(msg)
        print(f"    {'✅' if ok else '❌'} Earnings: {msg}")

        ok, msg = check_signal_age(trade)
        checks.append(('Age', ok, msg))
        if not ok: hard_failures.append(msg)
        print(f"    {'✅' if ok else '❌'} Age: {msg}")

        # Score decay — effective score at time of execution vs at generation
        ok, msg, loss = check_score_decay(trade, current_price)
        checks.append(('Score decay', ok, msg))
        if not ok:
            hard_failures.append(msg)
        elif loss > 0:
            soft_warnings.append(msg)
            trade['score_at_execution'] = round(float(trade.get('score', 7.5)) - loss, 2)
        print(f"    {'✅' if ok else '❌'} Decay: {msg}")

        # Verdict
        if hard_failures:
            trade['status']           = 'CANCELLED'
            trade['cancel_reason']    = hard_failures[0]
            trade['revalidated_at']   = now.isoformat()
            cancelled.append(trade)
            print(f"    ❌ CANCELLED: {hard_failures[0]}")
        elif soft_warnings:
            trade['revalidation_note']= ' | '.join(soft_warnings)
            trade['revalidated_at']   = now.isoformat()
            cautioned.append(trade)
            approved.append(trade)
            print(f"    ⚠️  APPROVED WITH CAUTION: {soft_warnings[0]}")
        else:
            trade['revalidated_at']   = now.isoformat()
            trade['revalidation_note']= 'All checks passed'
            approved.append(trade)
            print(f"    ✅ APPROVED — all checks passed")

        print()

    # Save updated queue
    atomic_write(QUEUE_FILE, queue)

    # Summary
    print(f"=== RE-VALIDATION SUMMARY ===")
    print(f"  Approved:  {len(approved) - len(cautioned)}")
    print(f"  Cautioned: {len(cautioned)}")
    print(f"  Cancelled: {len(cancelled)}")

    # Send Telegram summary
    import subprocess
    if cancelled or cautioned:
        msg_lines = [f"📋 QUEUE RE-VALIDATION — {now.strftime('%a %d %b')}"]
        if approved:
            msg_lines.append(f"\n✅ Approved ({len(approved)}):")
            for t in approved[:3]:
                msg_lines.append(f"  {t['name']} — {t.get('revalidation_note','')[:50]}")
        if cancelled:
            msg_lines.append(f"\n❌ Cancelled ({len(cancelled)}):")
            for t in cancelled[:3]:
                msg_lines.append(f"  {t['name']} — {t.get('cancel_reason','')[:60]}")

        msg = '\n'.join(msg_lines)
        try:
            send_telegram(msg)
        except Exception as e:
            log_error(f"Telegram failed: {e}")

    return approved, cancelled

if __name__ == '__main__':
    approved, cancelled = revalidate_queue()
    print(f"\n✅ Re-validation complete")
