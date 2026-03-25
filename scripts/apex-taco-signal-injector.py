#!/usr/bin/env python3
# INVOCATION: subprocess called by apex-taco-monitor.py — not a standalone cron.
# Bridges apex-taco-pending.json into apex-pending-signal.json for autopilot execution.
#
# Critical safety properties:
#   1. Collision guard — never overwrites a live normal signal
#   2. Friday 14:00 UTC blackout — no TACO entries into the weekend
#   3. Single lock covers both collision read AND signal write (atomic)
#   4. Stop placed BELOW the VIX-spike session low, not just ATR-based

import importlib.util
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        atomic_write, safe_read, log_error, log_warning, log_info,
        send_telegram, locked_read_modify_write, file_lock, get_portfolio_value
    )
except ImportError as _e:
    print(f"FATAL: apex_utils import failed: {_e}")
    sys.exit(1)

# ── CONSTANTS ────────────────────────────────────────────────────────────────
LOGS          = '/home/ubuntu/.picoclaw/logs'
SCRIPTS       = '/home/ubuntu/.picoclaw/scripts'
CONFIG_FILE   = '/home/ubuntu/.picoclaw/apex-taco-config.json'
PENDING_FILE  = f'{LOGS}/apex-taco-pending.json'
SIGNAL_FILE   = f'{LOGS}/apex-pending-signal.json'
LOG_FILE      = f'{LOGS}/apex-taco-log.json'
DRAWDOWN_FILE = f'{LOGS}/apex-drawdown.json'
REGIME_FILE   = f'{LOGS}/apex-regime.json'

# Signal TTL: a signal less than this many seconds old is considered live
_SIGNAL_TTL_SECONDS = 7200  # 2 hours
# ─────────────────────────────────────────────────────────────────────────────


def load_config():
    """Load TACO config with defaults."""
    return safe_read(CONFIG_FILE, {})


def is_friday_blackout():
    """Return True if it is Friday after 14:00 UTC — weekend gap risk."""
    now = datetime.now(timezone.utc)
    return now.weekday() == 4 and now.hour >= 14


def _dynamic_import(module_name, file_path):
    """Dynamically import a module from an absolute file path."""
    spec   = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calculate_rsi(closes, period=14):
    """Standard 14-period RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1 + rs)), 1)


def fetch_price_and_atr(yahoo_ticker):
    """Fetch current price, ATR, session low (5d), 20d high, and RSI from yfinance."""
    try:
        import yfinance as yf

        # 30 days for ATR(14) + session low lookback + RSI
        hist = yf.Ticker(yahoo_ticker).history(period='30d')
        if hist.empty or len(hist) < 20:
            log_error(f"TACO injector: insufficient price history for {yahoo_ticker}")
            return None

        closes = list(hist['Close'])
        highs  = list(hist['High'])
        lows   = list(hist['Low'])

        current_price = round(float(closes[-1]), 4)
        high_20d      = round(float(max(closes[-20:])), 4)
        session_low   = round(float(min(lows[-5:])), 4)  # VIX-spike session low proxy

        # ATR via shared apex_atr_stops module
        atr_value = None
        try:
            atr_mod   = _dynamic_import("atr_stops", f"{SCRIPTS}/apex_atr_stops.py")
            atr_value = atr_mod.calculate_atr(highs, lows, closes)
        except Exception:
            try:
                atr_mod   = _dynamic_import("atr_stops2", f"{SCRIPTS}/apex-atr-stops.py")
                atr_value = atr_mod.calculate_atr(highs, lows, closes)
            except Exception:
                pass

        # Fallback ATR: simple average of last 14 HL ranges
        if not atr_value:
            hl_ranges = [highs[i] - lows[i] for i in range(-14, 0)]
            atr_value = sum(hl_ranges) / len(hl_ranges) if hl_ranges else current_price * 0.015
        atr_value = round(float(atr_value), 4)

        rsi = calculate_rsi(closes)

        return {
            "price":       current_price,
            "atr":         atr_value,
            "session_low": session_low,
            "high_20d":    high_20d,
            "rsi":         rsi,
        }
    except Exception as e:
        log_error(f"TACO injector fetch_price_and_atr({yahoo_ticker}): {e}", exc=e)
        return None


def calculate_stops_and_targets(price_data):
    """Derive stop, target1, target2 from price context.

    Stop is placed BELOW the VIX-spike session low — the invalidation point.
    If price breaks session_low the market is treating this as ACTION not RHETORIC.
    """
    price       = price_data['price']
    atr         = price_data['atr']
    session_low = price_data['session_low']
    high_20d    = price_data['high_20d']

    # Stop: below spike session low by half an ATR, but never more than 3% below price
    stop_raw = session_low - (0.5 * atr)
    stop_cap = price * 0.97
    stop     = round(min(stop_raw, stop_cap), 4)

    # Target 1: pre-spike level (20-day high is a reasonable proxy)
    target1 = round(high_20d, 4)

    # Target 2: 2R extension — let walkback rallies run past the pre-spike level
    risk     = price - stop
    target2  = round(price + (risk * 2.0), 4) if risk > 0 else round(price * 1.06, 4)

    return {"stop": stop, "target1": target1, "target2": target2}


def calculate_quantity(price_data, stop, confidence, size_multiplier,
                       drawdown_mult, portfolio_value):
    """Risk-based position sizing using the APEX 2% risk model."""
    price          = price_data['price']
    risk_per_share = price - stop

    if risk_per_share <= 0:
        log_warning("TACO injector: risk_per_share <= 0, using minimum quantity")
        return 1.0

    mon_cfg  = safe_read(CONFIG_FILE, {}).get('monitor', {})
    high_conf_thresh = mon_cfg.get('high_confidence_threshold', 0.80)

    # Confidence multiplier
    if confidence >= high_conf_thresh:
        confidence_mult = mon_cfg.get('high_confidence_size_multiplier', 1.5)
    else:
        confidence_mult = mon_cfg.get('full_size_multiplier', 1.0)

    # 2% of portfolio as base risk
    base_risk = portfolio_value * 0.02
    adjusted_risk = base_risk * confidence_mult * size_multiplier * drawdown_mult

    # Hard £ risk caps (same boundaries as apex_sizer.py)
    adjusted_risk = max(10.0, min(100.0, adjusted_risk))

    quantity = adjusted_risk / risk_per_share
    return max(1.0, round(quantity, 2))


def build_signal(pending, ticker_entry, price_data, stops, quantity):
    """Construct the full APEX-compatible TACO signal dict."""
    now = datetime.now(timezone.utc)
    return {
        # Standard APEX signal fields
        "name":        ticker_entry["name"],
        "t212_ticker": ticker_entry["t212_ticker"],
        "entry":       price_data["price"],
        "stop":        stops["stop"],
        "target1":     stops["target1"],
        "target2":     stops["target2"],
        "score":       7.5,   # Fixed score for TACO (regime_override bypasses scoring)
        "rsi":         price_data["rsi"],
        "macd":        0.0,
        "sector":      "ETF",
        "signal_type": "TACO_CONTRARIAN",
        "currency":    ticker_entry["currency"],
        "quantity":    quantity,
        "generated_at": now.isoformat(),
        # TACO-specific metadata
        "regime_override":  True,
        "taco_confidence":  pending.get("confidence", 0.0),
        "taco_status":      pending.get("taco_status", "RHETORIC"),
        "taco_event_id":    pending.get("event_id", ""),
        "taco_tranche":     pending.get("taco_tranche", 1),
        "trailing_stop":    pending.get("trailing_stop", False),
    }


def append_to_log(entry):
    """Append a structured entry to the append-only taco audit log."""
    try:
        def _modifier(data):
            if not isinstance(data, list):
                data = []
            data.append(entry)
            return data
        locked_read_modify_write(LOG_FILE, _modifier, default=[])
    except Exception as e:
        log_error(f"TACO injector append_to_log: {e}", exc=e)


def _signal_is_live(existing):
    """Return True if existing apex-pending-signal.json contains a non-expired signal."""
    if not existing or not isinstance(existing, dict):
        return False
    # Must have a ticker to be a real signal
    if not existing.get('t212_ticker'):
        return False
    gen_at = existing.get('generated_at', '')
    if not gen_at:
        return True  # No timestamp — assume live
    try:
        gen_dt = datetime.fromisoformat(gen_at)
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - gen_dt).total_seconds()
        return age < _SIGNAL_TTL_SECONDS
    except Exception:
        return True  # Parse failure — assume live


def write_signal_with_collision_guard(signal):
    """Atomically check for existing signal and write, under a single file lock.

    Returns True on success, False if collision or write failure.
    The entire read+check+write happens inside one file_lock context so
    there is no TOCTOU race between the collision check and the write.
    """
    try:
        with file_lock(SIGNAL_FILE, timeout=10):
            existing = safe_read(SIGNAL_FILE, None)
            if _signal_is_live(existing):
                log_warning(
                    f"TACO injector: collision — apex-pending-signal.json is occupied by "
                    f"{existing.get('name','?')} ({existing.get('signal_type','?')}). "
                    f"Will retry on next monitor run."
                )
                append_to_log({
                    "event":       "COLLISION",
                    "event_id":    signal.get("taco_event_id"),
                    "blocked_by":  existing.get("name", "?"),
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                })
                return False
            # Safe to write
            atomic_write(SIGNAL_FILE, signal)
            return True
    except Exception as e:
        log_error(f"TACO injector write_signal_with_collision_guard: {e}", exc=e)
        return False


def main():
    """Read TACO pending, build signal, and inject into APEX pipeline."""
    try:
        config = load_config()
        if not config.get('enabled', True):
            log_info("TACO module disabled — skipping injector")
            sys.exit(0)

        # Load staging area written by monitor
        pending = safe_read(PENDING_FILE, {})
        if not pending or not pending.get('event_id'):
            log_info("TACO injector: no valid pending data — nothing to inject")
            sys.exit(0)

        # Friday blackout
        if is_friday_blackout():
            log_info("TACO injector: Friday 14:00 UTC blackout — injection suppressed")
            sys.exit(0)

        # Ticker selection based on threat type
        threat_type = pending.get('threat_type', 'DEFAULT')
        ticker_map  = config.get('ticker_map', {})
        t2t         = config.get('threat_type_to_ticker', {})
        ticker_key  = t2t.get(threat_type, t2t.get('DEFAULT', 'broad_market'))
        ticker_entry = ticker_map.get(ticker_key, ticker_map.get('broad_market'))

        if not ticker_entry:
            log_error(f"TACO injector: no ticker config found for key '{ticker_key}'")
            sys.exit(1)

        yahoo_ticker = ticker_entry['yahoo']

        # Fetch market data
        price_data = fetch_price_and_atr(yahoo_ticker)
        if price_data is None:
            log_error(f"TACO injector: price fetch failed for {yahoo_ticker} — aborting")
            sys.exit(1)

        stops = calculate_stops_and_targets(price_data)

        # Drawdown multiplier
        drawdown      = safe_read(DRAWDOWN_FILE, {})
        drawdown_mult = float(drawdown.get('multiplier', 1.0))

        # Portfolio value
        portfolio_value = get_portfolio_value() or 5000.0

        # Size multiplier from pending (set by monitor: 0.5x armed, 1.0/1.5x walkback)
        size_multiplier = float(pending.get('size_multiplier', 0.5))
        confidence      = float(pending.get('confidence', 0.65))

        quantity = calculate_quantity(
            price_data, stops['stop'], confidence,
            size_multiplier, drawdown_mult, portfolio_value
        )

        signal = build_signal(pending, ticker_entry, price_data, stops, quantity)

        # Write under collision guard
        written = write_signal_with_collision_guard(signal)
        if not written:
            sys.exit(1)  # Monitor will retry on next 5-min run

        # Audit log
        append_to_log({
            "event":        "SIGNAL_INJECTED",
            "event_id":     pending.get('event_id'),
            "ticker":       ticker_entry['t212_ticker'],
            "taco_status":  pending.get('taco_status'),
            "taco_tranche": pending.get('taco_tranche', 1),
            "entry":        signal['entry'],
            "stop":         signal['stop'],
            "target1":      signal['target1'],
            "quantity":     signal['quantity'],
            "confidence":   confidence,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        })

        tranche_label = f"Tranche {pending.get('taco_tranche', 1)}"
        conf_pct      = f"{confidence:.0%}"
        send_telegram(
            f"🌮 TACO SIGNAL INJECTED ({tranche_label})\n\n"
            f"{ticker_entry['name']} | {ticker_entry['t212_ticker']}\n"
            f"Status: {pending.get('taco_status')} | Confidence: {conf_pct}\n"
            f"Entry: ${signal['entry']} | Stop: ${signal['stop']}\n"
            f"Target 1: ${signal['target1']} | Target 2: ${signal['target2']}\n"
            f"Qty: {signal['quantity']} | Size: {size_multiplier:.1f}x\n"
            f"Trailing stop: {signal['trailing_stop']}\n\n"
            f"Awaiting autopilot execution..."
        )

        log_info(f"TACO injector: signal written — {ticker_entry['t212_ticker']} "
                 f"entry={signal['entry']} stop={signal['stop']} qty={signal['quantity']}")
        sys.exit(0)

    except Exception as e:
        log_error(f"TACO injector fatal: {e}", exc=e)
        sys.exit(1)


if __name__ == "__main__":
    main()
