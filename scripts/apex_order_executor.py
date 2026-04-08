#!/usr/bin/env python3
"""
Apex Order Executor
Pure-Python replacement for apex-execute-order.sh.

Logic:
  Step 0: Write status="pending" to positions BEFORE any API call
  Step 1: Place limit entry order (falls back to market on failure)
  Step 2: Upgrade to status="entry_placed"
  Step 3: Place GTC stop-loss order (3 attempts, rate-limited)
  Step 4a: On stop success  → upgrade to status="protected"
  Step 4b: On stop failure  → upgrade to status="unprotected", alert
  Step 5: Telegram trade confirmation
  Step 6: Slippage check (non-blocking)

All T212 API calls go through t212_request() (rate limiter + retry).
All position writes go through locked_read_modify_write() (file locking).
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        safe_read, atomic_write, log_error, log_warning,
        locked_read_modify_write, t212_request, send_telegram,
        get_fx_rate,
    )
except ImportError as _e:
    print(f"FATAL: apex_utils not available — {_e}")
    sys.exit(2)

try:
    from apex_config import T212_FILL_POLL_COUNT, T212_FILL_POLL_INTERVAL, SIGNAL_MAX_AGE_HOURS
except ImportError:
    T212_FILL_POLL_COUNT    = 18
    T212_FILL_POLL_INTERVAL = 10
    SIGNAL_MAX_AGE_HOURS    = 6

SIGNAL_FILE    = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
LOG            = '/home/ubuntu/.picoclaw/logs/apex-orders.log'
TRADING_STATE  = '/home/ubuntu/.picoclaw/workspace/skills/apex-trading/TRADING_STATE.md'

# Alpaca executor — preferred for US stocks when credentials are configured
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "apex_alpaca_executor",
        "/home/ubuntu/.picoclaw/scripts/apex-alpaca-executor.py")
    _alpaca_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_alpaca_mod)
    _ALPACA_AVAILABLE = _alpaca_mod.is_configured()
except Exception:
    _alpaca_mod = None
    _ALPACA_AVAILABLE = False

# US tickers that qualify for Alpaca execution (from apex-alpaca.py)
_ALPACA_US_TICKERS = {
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
    "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
    "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
    "WMT","PG","XOM","CVX","NVO"
}


_AUTOPILOT_FILE = '/home/ubuntu/.picoclaw/logs/apex-autopilot.json'


def _is_practice_mode() -> bool:
    """
    Returns True if apex-autopilot.json mode is PRACTICE (or file absent/unreadable).
    The mode field in apex-autopilot.json is authoritative:
      "mode": "PRACTICE"  → dry-run enforced regardless of CLI flags
      "mode": "LIVE"      → real execution proceeds (user must set this explicitly)
    Safe default: PRACTICE.
    """
    try:
        with open(_AUTOPILOT_FILE) as f:
            config = json.load(f)
        return config.get('mode', 'PRACTICE') != 'LIVE'
    except Exception:
        return True  # Safe default — never accidentally go live


def _get_mode() -> str:
    """Return mode string from autopilot config ('LIVE' or 'PRACTICE')."""
    try:
        with open(_AUTOPILOT_FILE) as f:
            return json.load(f).get('mode', 'PRACTICE')
    except Exception:
        return 'PRACTICE'


# Maximum favourable/unfavourable drift from signal price before rejection.
# Tighter than queue-revalidate thresholds because this fires at execution time,
# not at morning revalidation. A 1% drift between scan and execution means EV
# is already off by significant basis points, and slippage is compounded.
# For LONG entries:
#   drift_pct > 0 = price went UP  (bad for TREND — chasing)
#   drift_pct < 0 = price went DOWN (bad for INVERSE — already moved)
_STALENESS_LIMITS = {
    # signal_type: (max_up_drift_pct, max_down_drift_pct)
    'TREND':            (1.0,  3.0),  # Up 1% = chasing; down 3% = broken
    'EARNINGS_DRIFT':   (1.0,  3.0),
    'DIVIDEND_CAPTURE': (1.5,  2.5),
    'CONTRARIAN':       (3.0,  5.0),  # Contrarian tolerates wider drift
    'TACO_CONTRARIAN':  (3.0,  5.0),
    'INVERSE':          (2.0,  2.0),  # Inverse ETF — tight on either side
    'GEO_REVERSAL':     (3.0,  3.0),
    'DEFAULT':          (1.5,  3.0),
}


def _check_entry_staleness(ticker: str, signal_entry: float, signal_type: str) -> dict:
    """
    Verify the signal's entry price is still close to the current market price.
    Returns {'ok': bool, 'current': float, 'drift_pct': float, 'reason': str}.

    Fail-open: if price fetch fails, allow the trade (don't block on data
    failures — queue revalidation already did the morning check).
    """
    try:
        import yfinance as yf
        # Convert T212 ticker to yahoo equivalent via existing map
        from apex_utils import safe_read
        ticker_map = safe_read('/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json', {}) or {}
        # apex-ticker-map.json stores {yahoo: t212} pairs — invert it
        reverse_map = {v: k for k, v in ticker_map.items() if isinstance(k, str) and isinstance(v, str)}
        yahoo_ticker = reverse_map.get(ticker)
        if not yahoo_ticker:
            # Best-effort: strip T212 suffix
            yahoo_ticker = ticker.replace('_US_EQ', '').replace('_EQ', '')

        hist = yf.Ticker(yahoo_ticker).history(period='1d', interval='5m')
        if hist.empty:
            return {'ok': True, 'current': None, 'drift_pct': 0.0,
                    'reason': 'no live price available — allowing'}
        current = float(hist['Close'].iloc[-1])

        # Handle LSE pence quotes on UK instruments
        if yahoo_ticker.endswith('.L') and current > signal_entry * 10:
            current = round(current / 100, 4)

        if signal_entry <= 0:
            return {'ok': True, 'current': current, 'drift_pct': 0.0, 'reason': 'no signal entry to compare'}

        drift_pct = (current - signal_entry) / signal_entry * 100

        max_up, max_down = _STALENESS_LIMITS.get(signal_type, _STALENESS_LIMITS['DEFAULT'])

        if drift_pct > max_up:
            return {
                'ok':        False,
                'current':   round(current, 4),
                'drift_pct': round(drift_pct, 2),
                'reason':    (f"Price drifted UP {drift_pct:+.2f}% "
                              f"(limit +{max_up}% for {signal_type}) — chasing, entry invalidated")
            }
        if drift_pct < -max_down:
            return {
                'ok':        False,
                'current':   round(current, 4),
                'drift_pct': round(drift_pct, 2),
                'reason':    (f"Price drifted DOWN {drift_pct:+.2f}% "
                              f"(limit -{max_down}% for {signal_type}) — thesis broken, entry invalidated")
            }
        return {
            'ok':        True,
            'current':   round(current, 4),
            'drift_pct': round(drift_pct, 2),
            'reason':    'within tolerance'
        }
    except Exception as e:
        # Fail-closed: price feed errors block execution.
        # A stale price assumption invalidates EV and R-multiple calculations.
        # The morning queue-revalidation already passed — if the live feed is
        # now down, do not commit capital on an unverified entry.
        return {'ok': False, 'current': None, 'drift_pct': 0.0,
                'reason': f'staleness check failed (price feed down): {e} — blocking until feed recovers'}


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    line = f"{ts}: {msg}"
    print(line)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _update_position(ticker: str, updates: dict) -> None:
    """Apply *updates* to the position matching *ticker*, under file lock."""
    def _apply(positions):
        positions = positions or []
        for p in positions:
            if p.get('t212_ticker') == ticker:
                p.update(updates)
                return positions
        return positions
    locked_read_modify_write(POSITIONS_FILE, _apply, default=[])


def _remove_pending(ticker: str) -> None:
    """Remove a 'pending' entry — order never left, no orphan risk."""
    def _rm(positions):
        return [p for p in (positions or [])
                if not (p.get('t212_ticker') == ticker
                        and p.get('status') == 'pending')]
    locked_read_modify_write(POSITIONS_FILE, _rm, default=[])


def execute(signal: dict, dry_run: bool = False, _mode: str = None) -> bool:
    """
    Execute a trade from a signal dict.
    Returns True on full success (entry + stop placed).
    _mode: pre-read mode string — callers should pass this to avoid a
    re-read race where mode could change between signal generation and execution.
    """
    # ── Mode locked at call time — do not re-read mid-execution ──────────────
    # If mode flips PRACTICE → LIVE between signal generation and execution,
    # the executor uses whichever mode was captured at main() entry.
    if _mode is None:
        _mode = _get_mode()

    ticker   = signal.get('t212_ticker', '')
    name     = signal.get('name', ticker)
    quantity = float(signal.get('quantity', 0) or 0)
    entry    = float(signal.get('entry', 0) or 0)
    stop     = float(signal.get('stop', 0) or 0)
    target1  = float(signal.get('target1', 0) or 0)
    target2  = float(signal.get('target2', 0) or 0)
    score    = float(signal.get('score', 0) or 0)
    rsi      = float(signal.get('rsi', 0) or 0)
    macd     = float(signal.get('macd', 0) or 0)
    sector   = signal.get('sector') or 'UNKNOWN'
    atr      = signal.get('atr', 0)
    signal_type = signal.get('signal_type', 'TREND')
    currency = signal.get('currency', 'GBP')

    if not ticker or not quantity:
        _log(f"ERROR: Signal missing ticker or quantity — aborting")
        send_telegram("⚠️ Signal file incomplete — no ticker or quantity.")
        return False

    # ── NaN/Inf pre-flight gate ───────────────────────────────────────────────
    # Python's json module serialises float('nan') as the bare word NaN which
    # is invalid JSON.  If a NaN leaks through (e.g. from a failed price fetch
    # or calculation error), it must be caught before any API call is made.
    _nan_fields = []
    for _fname, _fval in [('quantity', quantity), ('entry', entry), ('stop', stop)]:
        if math.isnan(_fval) or math.isinf(_fval) or _fval <= 0:
            _nan_fields.append(f"{_fname}={_fval}")
    if _nan_fields:
        _log(f"ERROR: Signal has NaN/zero critical fields: {', '.join(_nan_fields)} — aborting and removing signal")
        send_telegram(
            f"⚠️ SIGNAL REJECTED — NaN/INVALID FIELDS\n\n"
            f"{name} ({ticker})\n"
            f"Bad fields: {', '.join(_nan_fields)}\n\n"
            f"Signal deleted. Re-run morning scan to regenerate."
        )
        try:
            os.remove(SIGNAL_FILE)
        except FileNotFoundError:
            pass
        return False

    # ── ATR sanity check ─────────────────────────────────────────────────────
    try:
        _atr_val = float(atr) if atr else 0.0
        if _atr_val <= 0:
            log_warning(f"Signal for {name} has ATR={atr} — trailing stop and target calculations may be degenerate")
    except (TypeError, ValueError):
        log_warning(f"Signal for {name} has non-numeric ATR={atr!r}")

    if entry > 0 and stop > 0 and stop >= entry:
        _log(f"ERROR: Invalid stops for {name}: entry={entry} stop={stop} — stop must be below entry")
        send_telegram(f"⚠️ Trade rejected — invalid stops: entry {entry} <= stop {stop} for {name}")
        return False

    # Practice mode gate — mode was captured at main() entry and is now fixed
    if dry_run or _mode != 'LIVE':
        _log(f"DRY-RUN [{_mode}]: Would place {quantity} × {ticker} @ £{entry} (stop £{stop})")
        send_telegram(f"🔬 DRY-RUN [{_mode}]: {name} ({ticker}) {quantity}×£{entry} stop:£{stop}")
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Alpaca routing: US stocks with Alpaca credentials configured go via Alpaca
    # (DMA, smart order routing, fractional shares) — T212 is fallback.
    # ─────────────────────────────────────────────────────────────────────────
    alpaca_ticker = signal.get('ticker', ticker).replace('_US_EQ', '').replace('_EQ', '')
    use_alpaca = (
        _ALPACA_AVAILABLE
        and _alpaca_mod is not None
        and alpaca_ticker.upper() in _ALPACA_US_TICKERS
    )

    if use_alpaca:
        _log(f"Routing {alpaca_ticker} → Alpaca (US stock, DMA available)")
        alpaca_signal = {**signal, 'ticker': alpaca_ticker}
        ap_result = _alpaca_mod.execute(alpaca_signal, dry_run=dry_run)

        if ap_result['success']:
            entry_id   = ap_result.get('entry_order_id')
            stop_id    = ap_result.get('stop_order_id')
            filled_qty = ap_result.get('filled_qty', quantity)
            venue      = 'ALPACA'

            # Write position to positions file (same schema as T212 path)
            today    = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            now_iso  = datetime.now(timezone.utc).isoformat()
            status   = 'protected' if stop_id else ('awaiting_fill' if filled_qty == 0 else 'unprotected')
            unprotected = not stop_id and filled_qty > 0

            def _write_alpaca(positions):
                positions = positions or []
                positions = [p for p in positions
                             if not (p.get('t212_ticker') == ticker and p.get('status') == 'pending')]
                positions.append({
                    "t212_ticker": ticker, "name": name,
                    "quantity": quantity, "entry": entry, "stop": stop,
                    "target1": target1, "target2": target2, "score": score,
                    "rsi": rsi, "macd": macd, "sector": sector, "atr": atr,
                    "signal_type": signal_type, "currency": currency,
                    "fx_at_entry": get_fx_rate(currency),
                    "opened": today, "opened_iso": now_iso,
                    "entry_order_id": str(entry_id) if entry_id else None,
                    "stop_order_id": str(stop_id) if stop_id else None,
                    "status": status, "order_type": f"{ap_result.get('order_type','LIMIT')}+STOP",
                    "venue": "ALPACA", "unprotected": unprotected,
                    "unrealised_pnl": 0.0,
                })
                return positions
            locked_read_modify_write(POSITIONS_FILE, _write_alpaca, default=[])

            if unprotected:
                send_telegram(
                    f"🚨 UNPROTECTED POSITION (Alpaca) — ACTION REQUIRED\n\n"
                    f"{name} ({alpaca_ticker})\nEntry placed but stop loss FAILED.\n"
                    f"Log in to Alpaca and set stop at ${stop}"
                )
            elif status == 'protected':
                send_telegram(
                    f"✅ TRADE PLACED (Alpaca)\n"
                    f"🏷 {name} ({alpaca_ticker})\n"
                    f"📐 Qty: {filled_qty} shares\n"
                    f"💰 Entry: ${entry} ({ap_result.get('order_type','LIMIT')})\n"
                    f"🛑 Stop: ${stop} (GTC)\n"
                    f"🎯 T1: ${target1} | T2: ${target2}\n"
                    f"📊 Score: {score}/10\n"
                    f"🔖 Entry ID: {entry_id} | Stop ID: {stop_id}\n"
                    f"🏦 Venue: Alpaca (DMA)"
                )
                try:
                    os.remove(SIGNAL_FILE)
                except FileNotFoundError:
                    pass
            return True
        else:
            _log(f"Alpaca execution failed: {ap_result.get('error')} — falling back to T212")

    # ─────────────────────────────────────────────────────────────────────────
    # Pre-flight: Re-validate portfolio heat at execution time.
    # The heat gate already ran during signal evaluation (apex-autopilot.py
    # safety_check), but new positions may have been added since then.
    # This ensures we never exceed the 8% heat cap at the moment of order
    # placement, not just at signal generation time.
    # Non-blocking on error — never let monitoring failure abort execution.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "ph", "/home/ubuntu/.picoclaw/scripts/apex-portfolio-heat.py")
        _ph = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_ph)
        _heat_mult, _heat_status, _heat_pct = _ph.get_heat_multiplier()
        if _heat_status == 'CRITICAL':
            _log(f"EXECUTION ABORTED: portfolio heat {_heat_pct:.1f}% exceeds 8% cap at order time")
            send_telegram(
                f"🛑 ORDER ABORTED — portfolio heat\n\n"
                f"{name} ({ticker})\n"
                f"Heat at execution time: {_heat_pct:.1f}% (max 8%).\n"
                f"Signal preserved — will retry when risk reduces."
            )
            return False
    except Exception as _heat_err:
        log_warning(f"Portfolio heat pre-flight check failed (non-blocking): {_heat_err}")

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    now_iso = datetime.now(timezone.utc).isoformat()

    # ─────────────────────────────────────────────────────────────────────────
    # Staleness gate: reject if price has drifted unfavourably since scan.
    # The signal was generated at scan time (e.g. 08:30 UTC) but may not be
    # executing until 09:30 or 13:05 UTC. Price can drift significantly in
    # that window, invalidating the R-multiple and EV calculation.
    # Thresholds per signal type are tighter here than in queue-revalidate
    # because this is the last check before committing capital.
    # ─────────────────────────────────────────────────────────────────────────
    stale_check = _check_entry_staleness(ticker, entry, signal_type)
    if not stale_check['ok']:
        _log(f"STALENESS ABORT: {stale_check['reason']}")
        feed_down = stale_check.get('current') is None  # True = feed error, False = actual drift
        if feed_down:
            # Price feed is unavailable — preserve the signal and retry next cycle.
            # Deleting here would silently kill a valid signal on a temporary outage.
            send_telegram(
                f"⏱ ENTRY DEFERRED — PRICE FEED DOWN\n\n"
                f"{name} ({ticker})\n"
                f"Signal entry: £{entry}\n\n"
                f"{stale_check['reason']}\n"
                f"Signal preserved — will retry when feed recovers."
            )
        else:
            # Price has genuinely drifted — thesis is invalidated, delete signal.
            send_telegram(
                f"⏱ ENTRY REJECTED — PRICE DRIFT\n\n"
                f"{name} ({ticker})\n"
                f"Signal entry: £{entry}\n"
                f"Current price: £{stale_check['current']}\n"
                f"Drift: {stale_check['drift_pct']:+.2f}%\n\n"
                f"{stale_check['reason']}\n"
                f"No position opened — waiting for better entry."
            )
            try:
                os.remove(SIGNAL_FILE)
            except FileNotFoundError:
                pass
        return False

    if stale_check.get('current'):
        _log(f"Staleness OK: signal £{entry} vs current £{stale_check['current']} "
             f"({stale_check['drift_pct']:+.2f}%)")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 0: Write PENDING entry BEFORE any API call
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 0: Writing PENDING entry for {ticker}")

    try:
        fx_at_entry = get_fx_rate(currency)
    except Exception as _fx_e:
        log_warning(f"FX snapshot failed for {currency}: {_fx_e}")
        fx_at_entry = 1.0

    def _write_pending(positions):
        positions = positions or []
        # Remove stale pending for same ticker (safety dedup)
        positions = [p for p in positions
                     if not (p.get('t212_ticker') == ticker
                             and p.get('status') == 'pending')]
        positions.append({
            "t212_ticker":    ticker,
            "name":           name,
            "quantity":       quantity,
            "entry":          entry,
            "stop":           stop,
            "target1":        target1,
            "target2":        target2,
            "score":          score,
            "rsi":            rsi,
            "macd":           macd,
            "sector":         sector,
            "atr":            atr,
            "signal_type":    signal_type,
            "currency":       currency,
            "fx_at_entry":    fx_at_entry,
            "opened":         today,
            "opened_iso":     now_iso,
            "entry_order_id": None,
            "stop_order_id":  None,
            "status":         "pending",
            "order_type":     "LIMIT+STOP",
            "unrealised_pnl": 0.0,
        })
        return positions

    locked_read_modify_write(POSITIONS_FILE, _write_pending, default=[])

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Place limit entry order (fallback to market)
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 1: Placing LIMIT entry — {ticker} ×{quantity} @ £{entry}")

    entry_data = t212_request('/equity/orders/limit', method='POST', payload={
        "ticker":       ticker,
        "quantity":     quantity,
        "limitPrice":   round(entry, 4),
        "timeValidity": "DAY",
    })

    entry_id = (entry_data or {}).get('id')
    order_type_used = "LIMIT"

    if not entry_id:
        _log(f"Step 1b: Limit failed — falling back to market order")
        market_data = t212_request('/equity/orders/market', method='POST', payload={
            "ticker":   ticker,
            "quantity": quantity,
        })
        entry_id = (market_data or {}).get('id')
        order_type_used = "MARKET"

    if not entry_id:
        _log(f"FATAL: Entry order failed for {ticker}")
        _remove_pending(ticker)
        send_telegram(
            f"❌ ENTRY ORDER FAILED\n\n"
            f"{name} ({ticker})\n"
            f"Both limit and market orders failed.\n"
            f"Pending entry removed — no position opened."
        )
        return False

    _log(f"Step 1 OK: Entry ID {entry_id} ({order_type_used})")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Upgrade pending → entry_placed
    # ─────────────────────────────────────────────────────────────────────────
    _update_position(ticker, {
        'status':         'entry_placed',
        'entry_order_id': str(entry_id),
        'order_type':     f'{order_type_used}+STOP',
    })

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2b: Wait for entry fill before placing stop
    # T212 rejects stop orders for shares not yet owned (e.g. limit placed
    # pre-market won't fill until exchange opens).  Poll up to 3 minutes;
    # if still unfilled, save state and let fill-check.sh finish later.
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 2b: Waiting for entry fill — polling order {entry_id}")
    filled_qty = 0.0
    for _poll in range(T212_FILL_POLL_COUNT):
        _raw = t212_request(f'/equity/orders/{entry_id}')
        if _raw is None:
            # API returned None — 404 (order gone) or transient error.
            # Don't keep hammering a dead order ID every 10s for 3 minutes.
            _log(f"  Poll {_poll+1}: order {entry_id} not found (API error/404) — deferring stop")
            break
        order_status = _raw
        filled_qty = float(order_status.get('filledQuantity', 0))
        status_str = order_status.get('status', 'UNKNOWN')
        if filled_qty > 0:
            _log(f"  Entry filled: {filled_qty} shares (status: {status_str})")
            break
        if status_str in ('CANCELLED', 'REJECTED', 'EXPIRED'):
            _log(f"  Entry order {status_str} — aborting stop placement")
            _remove_pending(ticker)
            send_telegram(
                f"⚠️ ENTRY ORDER {status_str}\n\n{name} ({ticker})\n"
                f"Order {entry_id} was {status_str}. No position opened."
            )
            return False
        _log(f"  Poll {_poll+1}/{T212_FILL_POLL_COUNT}: filledQty={filled_qty} "
             f"status={status_str} — waiting {T212_FILL_POLL_INTERVAL}s")
        time.sleep(T212_FILL_POLL_INTERVAL)

    if filled_qty == 0:
        # Order not filled within 3 min (e.g. pre-market limit) — save
        # deferred stop state and let fill-check.sh finish it.
        _log(f"Entry not filled within 3 min — deferring stop to fill-check")
        _update_position(ticker, {
            'status':         'awaiting_fill',
            'entry_order_id': str(entry_id),
            'stop_price':     stop,
            'deferred_stop':  True,
        })
        send_telegram(
            f"⏳ ENTRY PENDING — STOP DEFERRED\n\n"
            f"{name} ({ticker})\n"
            f"Limit order {entry_id} not yet filled (pre-market or low liquidity).\n"
            f"Stop at £{stop} will be placed automatically once the order fills.\n"
            f"Apex will check every 30 minutes."
        )
        # Spawn watchdog immediately in the background so it can catch a quick
        # fill and place the stop without waiting up to 30 min for the next
        # scheduled cron cycle.  This closes the largest part of the protection gap.
        try:
            import subprocess as _sp_deferred
            _sp_deferred.Popen(
                [sys.executable,
                 '/home/ubuntu/.picoclaw/scripts/apex-broker-watchdog.py'],
                stdout=_sp_deferred.DEVNULL,
                stderr=_sp_deferred.DEVNULL,
            )
            _log("Watchdog spawned in background to monitor fill and place deferred stop")
        except Exception as _wd_e:
            log_warning(f"Could not spawn background watchdog: {_wd_e}")
        return True   # not an error — position is being managed

    # Use actual filled quantity for the stop (may differ from requested)
    neg_qty = round(filled_qty * -1, 8)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Place GTC stop-loss (3 attempts, rate limiter handles spacing)
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 3: Placing STOP LOSS — {ticker} @ £{stop} for {filled_qty} shares")

    stop_id  = None
    attempts = 3

    for attempt in range(1, attempts + 1):
        _log(f"  Stop attempt {attempt}/{attempts}")
        stop_data = t212_request('/equity/orders/stop', method='POST', payload={
            "ticker":       ticker,
            "quantity":     neg_qty,
            "stopPrice":    round(stop, 4),
            "timeValidity": "GOOD_TILL_CANCEL",
        })
        stop_id = (stop_data or {}).get('id')
        if stop_id:
            break
        if attempt < attempts:
            time.sleep(2)   # extra wait between stop retries only

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4a: Stop success → protected
    # ─────────────────────────────────────────────────────────────────────────
    if stop_id:
        _log(f"Step 3 OK: Stop ID {stop_id} @ £{stop}")
        _update_position(ticker, {
            'status':        'protected',
            'stop_order_id': str(stop_id),
            'unprotected':   False,
        })

        # Append to TRADING_STATE.md
        try:
            with open(TRADING_STATE, 'a') as f:
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
                f.write(
                    f"{ts} | {order_type_used} BUY | {name} | {ticker} | "
                    f"qty:{quantity} | limit:{entry} | stop:{stop} (order:{stop_id}) | "
                    f"T1:{target1} | T2:{target2} | score:{score} | "
                    f"entry_id:{entry_id}\n"
                )
        except Exception as e:
            log_warning(f"TRADING_STATE.md write failed (non-fatal): {e}")

        # Remove signal file — consumed
        try:
            os.remove(SIGNAL_FILE)
        except FileNotFoundError:
            pass

        # ── Recovery ramp decrement ───────────────────────────────────────────
        # After a SUSPEND auto-resume, circuit breaker sets recovery_trades_remaining=N
        # and halves position sizing for that many trades.  Decrement here — the
        # executor is the only place that confirms a real trade was placed.
        try:
            from apex_utils import locked_read_modify_write as _lrmw
            _CB_FILE = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'
            def _decrement_ramp(cb):
                cb = cb or {}
                ramp = cb.get('recovery_trades_remaining', 0)
                if ramp > 0:
                    cb['recovery_trades_remaining'] = ramp - 1
                    _log(f"Recovery ramp: {ramp} → {ramp - 1} trades remaining at reduced sizing")
                return cb
            _lrmw(_CB_FILE, _decrement_ramp, default={})
        except Exception as _ramp_e:
            log_warning(f"Recovery ramp decrement failed (non-blocking): {_ramp_e}")

        send_telegram(
            f"✅ TRADE PLACED\n"
            f"🏷 {name} ({ticker})\n"
            f"📐 Qty: {quantity} shares\n"
            f"💰 Entry: £{entry} ({order_type_used} — DAY order)\n"
            f"🛑 Stop: £{stop} (GTC — protected in T212)\n"
            f"🎯 T1: £{target1} | T2: £{target2}\n"
            f"📊 Score: {score}/10\n"
            f"🔖 Entry ID: {entry_id}\n"
            f"✅ Stop loss order placed (ID: {stop_id})\n\n"
            f"Your position is now protected even if Apex goes offline.\n"
            f"Reply CANCEL to cancel entry order."
        )

        # Step 6: Slippage check (non-blocking, brief wait for fill)
        time.sleep(3)
        try:
            fill_data = t212_request(f'/equity/orders/{entry_id}')
            if fill_data:
                actual_price = fill_data.get('fillPrice') or fill_data.get('limitPrice') or 0
                if actual_price:
                    import subprocess
                    subprocess.run(
                        ['python3',
                         '/home/ubuntu/.picoclaw/scripts/apex-slippage-tracker.py',
                         'log', name, ticker, str(entry), str(actual_price),
                         str(quantity), 'BUY', str(stop)],
                        capture_output=True
                    )
        except Exception:
            pass

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4b: Stop failed → unprotected — ALERT, do not close position
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"CRITICAL: Stop loss FAILED for {ticker} after {attempts} attempts")

    # Create alert flag for health-check monitoring
    try:
        open(f'/home/ubuntu/.picoclaw/logs/STOP_MISSING_{ticker}', 'w').close()
    except Exception:
        pass

    # Find if pending entry already created the position record
    def _mark_unprotected(positions):
        positions = positions or []
        for p in positions:
            if (p.get('t212_ticker') == ticker
                    and p.get('status') in ('pending', 'entry_placed')):
                p['status']         = 'unprotected'
                p['entry_order_id'] = str(entry_id)
                p['stop_order_id']  = None
                p['unprotected']    = True
                return positions
        # Fallback: recreate if pending was somehow lost
        positions.append({
            "t212_ticker":    ticker,
            "name":           name,
            "quantity":       quantity,
            "entry":          entry,
            "stop":           stop,
            "target1":        target1,
            "target2":        target2,
            "score":          score,
            "rsi":            rsi,
            "macd":           macd,
            "sector":         sector,
            "atr":            atr,
            "signal_type":    signal_type,
            "currency":       currency,
            "fx_at_entry":    fx_at_entry,
            "opened":         today,
            "entry_order_id": str(entry_id),
            "stop_order_id":  None,
            "status":         "unprotected",
            "unprotected":    True,
            "order_type":     f"{order_type_used}+STOP",
        })
        return positions

    locked_read_modify_write(POSITIONS_FILE, _mark_unprotected, default=[])

    send_telegram(
        f"🚨 UNPROTECTED POSITION — ACTION REQUIRED\n\n"
        f"Ticker: {ticker} ({name})\n"
        f"Entry order placed (ID: {entry_id}) but STOP LOSS FAILED after {attempts} attempts.\n\n"
        f"⚠️ Position is OPEN with NO stop loss.\n"
        f"Log in to T212 and set a manual stop at £{stop}\n\n"
        f"To close: reply CLOSE {ticker}\n"
        f"To retry stop: log in to T212 app directly."
    )
    return False


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description='Apex Order Executor')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate without placing real orders')
    parser.add_argument('--signal', default=SIGNAL_FILE,
                        help='Path to signal JSON file')
    args = parser.parse_args()

    # ── Read mode ONCE here and lock it for the entire execution ─────────────
    # Prevents a mode flip (PRACTICE → LIVE) mid-execution from silently
    # changing behaviour after the signal was already validated in PRACTICE.
    mode_at_entry = _get_mode()

    signal_path = args.signal
    if not os.path.exists(signal_path):
        _log(f"ERROR: Signal file not found: {signal_path}")
        send_telegram("⚠️ No pending signal found.")
        sys.exit(1)

    try:
        signal = safe_read(signal_path, {})
    except Exception as e:
        _log(f"ERROR: Cannot read signal file: {e}")
        sys.exit(1)

    if not signal:
        _log("ERROR: Signal file is empty or invalid JSON")
        send_telegram("⚠️ Signal file empty or invalid.")
        sys.exit(1)

    # ── Signal TTL gate ───────────────────────────────────────────────────────
    # A signal generated at 08:30 is only valid for the current session.
    # If still pending after _SIGNAL_MAX_AGE_HOURS (default 6h), the thesis
    # may have changed — delete and force a fresh scan rather than executing
    # on stale entry assumptions.
    generated_at_str = signal.get('generated_at', '')
    if generated_at_str:
        try:
            generated_at = datetime.fromisoformat(
                generated_at_str.replace('Z', '+00:00'))
            age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
            if age_hours > SIGNAL_MAX_AGE_HOURS:
                _log(f"SIGNAL EXPIRED: {age_hours:.1f}h old (max {SIGNAL_MAX_AGE_HOURS}h) — deleting")
                send_telegram(
                    f"⏰ SIGNAL EXPIRED — NOT EXECUTED\n\n"
                    f"{signal.get('name', '?')} ({signal.get('t212_ticker', '?')})\n"
                    f"Signal was {age_hours:.1f}h old (max {SIGNAL_MAX_AGE_HOURS}h).\n\n"
                    f"Signal deleted. Re-run morning scan to regenerate a fresh entry."
                )
                try:
                    os.remove(signal_path)
                except FileNotFoundError:
                    pass
                sys.exit(1)
        except Exception as _ttl_e:
            _log(f"WARNING: Cannot parse generated_at '{generated_at_str}': {_ttl_e}")

    success = execute(signal, dry_run=args.dry_run, _mode=mode_at_entry)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
