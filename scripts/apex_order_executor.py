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
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        safe_read, atomic_write, log_error, log_warning,
        locked_read_modify_write, t212_request, send_telegram,
    )
except ImportError as _e:
    print(f"FATAL: apex_utils not available — {_e}")
    sys.exit(2)

SIGNAL_FILE    = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
LOG            = '/home/ubuntu/.picoclaw/logs/apex-orders.log'
TRADING_STATE  = '/home/ubuntu/.picoclaw/workspace/skills/apex-trading/TRADING_STATE.md'


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


def execute(signal: dict, dry_run: bool = False) -> bool:
    """
    Execute a trade from a signal dict.
    Returns True on full success (entry + stop placed).
    """
    ticker   = signal.get('t212_ticker', '')
    name     = signal.get('name', ticker)
    quantity = float(signal.get('quantity', 0))
    entry    = float(signal.get('entry', 0))
    stop     = float(signal.get('stop', 0))
    target1  = float(signal.get('target1', 0))
    target2  = float(signal.get('target2', 0))
    score    = float(signal.get('score', 0))
    rsi      = float(signal.get('rsi', 0))
    macd     = float(signal.get('macd', 0))
    sector   = signal.get('sector') or 'ETF'
    atr      = signal.get('atr', 0)
    signal_type = signal.get('signal_type', 'TREND')
    currency = signal.get('currency', 'GBP')

    if not ticker or not quantity:
        _log(f"ERROR: Signal missing ticker or quantity — aborting")
        send_telegram("⚠️ Signal file incomplete — no ticker or quantity.")
        return False

    if dry_run:
        _log(f"DRY-RUN: Would place {quantity} × {ticker} @ £{entry} (stop £{stop})")
        send_telegram(f"🔬 DRY-RUN: {name} ({ticker}) {quantity}×£{entry} stop:£{stop}")
        return True

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    now_iso = datetime.now(timezone.utc).isoformat()

    # ─────────────────────────────────────────────────────────────────────────
    # Step 0: Write PENDING entry BEFORE any API call
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 0: Writing PENDING entry for {ticker}")

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
            "opened":         today,
            "opened_iso":     now_iso,
            "entry_order_id": None,
            "stop_order_id":  None,
            "status":         "pending",
            "order_type":     "LIMIT+STOP",
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
    for _poll in range(18):   # 18 × 10 s = 3 minutes
        order_status = t212_request(f'/equity/orders/{entry_id}') or {}
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
        _log(f"  Poll {_poll+1}/18: filledQty={filled_qty} status={status_str} — waiting 10s")
        time.sleep(10)

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

    success = execute(signal, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
