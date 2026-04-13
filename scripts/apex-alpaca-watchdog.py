#!/usr/bin/env python3
"""
Apex Alpaca Position Watchdog
==============================
Polls Alpaca for positions in 'awaiting_fill' state (venue=ALPACA).
When a fill is detected:
  1. Updates positions.json with actual fill price and quantity
  2. Places a GTC stop-loss via Alpaca
  3. Promotes position to 'protected'

Runs every 5 minutes during US market hours via cron (14:30–21:00 UTC Mon-Fri).
This is the Alpaca equivalent of apex-broker-watchdog.py's check_deferred_stops().

Cron entry (added automatically by this script if missing):
  */5 14-20 * * 1-5 /home/ubuntu/bin/python3 /home/ubuntu/.picoclaw/scripts/apex-alpaca-watchdog.py
"""
import sys
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_utils import (
    safe_read, locked_read_modify_write, log_error, log_warning, send_telegram
)

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
LOG_FILE       = '/home/ubuntu/.picoclaw/logs/apex-alpaca-orders.log'


def _log(msg: str) -> None:
    ts   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    line = f"{ts}: [alpaca-watchdog] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _load_alpaca_executor():
    """Import the Alpaca executor module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'alpaca_exec',
        '/home/ubuntu/.picoclaw/scripts/apex-alpaca-executor.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    """
    Main watchdog loop.
    Checks all Alpaca positions in 'awaiting_fill' state and handles fills.
    """
    positions = safe_read(POSITIONS_FILE, [])
    if not isinstance(positions, list):
        positions = []

    alpaca_pending = [
        p for p in positions
        if p.get('venue') == 'ALPACA' and p.get('status') == 'awaiting_fill'
    ]

    if not alpaca_pending:
        _log("No Alpaca positions awaiting fill")
        return

    _log(f"Found {len(alpaca_pending)} Alpaca position(s) awaiting fill")

    try:
        alpaca = _load_alpaca_executor()
    except Exception as e:
        log_error(f"alpaca-watchdog: could not load executor module: {e}")
        send_telegram(f"⚠️ Alpaca watchdog error — executor module failed to load: {e}")
        return

    now = datetime.now(timezone.utc)

    for pos in alpaca_pending:
        ticker    = pos.get('t212_ticker', '')
        name      = pos.get('name', ticker)
        entry_id  = pos.get('entry_order_id', '')
        stop      = float(pos.get('stop', 0) or 0)
        qty       = float(pos.get('quantity', 0) or 0)
        opened_iso = pos.get('opened_iso', '')

        if not entry_id or not stop:
            log_warning(f"alpaca-watchdog: {name} missing entry_order_id or stop — skipping")
            continue

        _log(f"Checking {name} ({ticker}) — entry order {entry_id}")

        # ── Check order status ────────────────────────────────────────────────
        try:
            order = alpaca.get_order(entry_id)
        except Exception as e:
            log_error(f"alpaca-watchdog: get_order failed for {name}: {e}")
            continue

        if order is None:
            _log(f"  {name}: order {entry_id} not found — may have been cancelled")
            # If the order is gone and we're well past open, alert
            try:
                opened_dt = datetime.fromisoformat(opened_iso.replace('Z', '+00:00'))
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                age_min = (now - opened_dt).total_seconds() / 60
                if age_min > 60:
                    log_error(f"alpaca-watchdog: {name} order gone after {age_min:.0f} min — removing position")
                    send_telegram(
                        f"⚠️ ALPACA ORDER GONE\n\n"
                        f"{name} ({ticker})\n"
                        f"Entry order {entry_id} not found after {age_min:.0f} min.\n"
                        f"Order was likely cancelled/expired. Removing from tracking."
                    )
                    def _remove(positions, _t=ticker):
                        return [p for p in (positions or [])
                                if not (p.get('t212_ticker') == _t
                                        and p.get('venue') == 'ALPACA'
                                        and p.get('status') == 'awaiting_fill')]
                    locked_read_modify_write(POSITIONS_FILE, _remove, default=[])
            except Exception:
                pass
            continue

        status     = order.get('status', 'unknown')
        filled_qty = float(order.get('filled_qty', 0) or 0)
        avg_price  = float(order.get('filled_avg_price') or order.get('limit_price', pos.get('entry', 0)) or 0)

        _log(f"  {name}: status={status} filled_qty={filled_qty} avg_price={avg_price}")

        # ── Terminal without fill ─────────────────────────────────────────────
        if status in ('canceled', 'expired', 'rejected', 'done_for_day') and filled_qty == 0:
            log_warning(f"alpaca-watchdog: {name} order {status} with no fill — removing")
            send_telegram(
                f"⚠️ ALPACA ORDER {status.upper()}\n\n"
                f"{name} ({ticker})\n"
                f"Entry order expired/cancelled without filling.\n"
                f"No position opened — signal will re-qualify on next scan."
            )
            def _remove_terminal(positions, _t=ticker):
                return [p for p in (positions or [])
                        if not (p.get('t212_ticker') == _t
                                and p.get('venue') == 'ALPACA'
                                and p.get('status') == 'awaiting_fill')]
            locked_read_modify_write(POSITIONS_FILE, _remove_terminal, default=[])
            continue

        # ── Filled ────────────────────────────────────────────────────────────
        if filled_qty > 0:
            _log(f"  {name}: FILLED {filled_qty} shares @ ${avg_price} — placing stop @ ${stop}")

            # Place GTC stop-loss
            stop_order = None
            stop_id    = None
            for attempt in range(1, 4):
                try:
                    stop_order = alpaca.place_stop_order(
                        ticker.replace('_US_EQ', '').replace('_EQ', ''),
                        filled_qty,
                        stop
                    )
                    stop_id = (stop_order or {}).get('id')
                    if stop_id:
                        break
                    _log(f"  Stop attempt {attempt}/3 failed — retrying in 3s")
                    time.sleep(3)
                except Exception as _se:
                    _log(f"  Stop attempt {attempt}/3 exception: {_se}")
                    time.sleep(3)

            if stop_id:
                _log(f"  Stop placed: {stop_id}")
                new_status  = 'protected'
                unprotected = False
            else:
                log_error(f"alpaca-watchdog: stop placement FAILED for {name} — unprotected!")
                new_status  = 'unprotected'
                unprotected = True
                send_telegram(
                    f"🚨 ALPACA POSITION UNPROTECTED\n\n"
                    f"{name} ({ticker})\n"
                    f"Filled {filled_qty} shares @ ${avg_price}\n"
                    f"STOP PLACEMENT FAILED after 3 attempts.\n"
                    f"Log in to Alpaca and set stop at ${stop} IMMEDIATELY."
                )

            # Update positions.json
            def _update_pos(positions, _t=ticker, _qty=filled_qty, _price=avg_price,
                            _sid=stop_id, _status=new_status, _unprot=unprotected):
                for p in (positions or []):
                    if (p.get('t212_ticker') == _t
                            and p.get('venue') == 'ALPACA'
                            and p.get('status') == 'awaiting_fill'):
                        p['quantity']      = _qty
                        p['entry']         = round(_price, 4)
                        p['stop_order_id'] = str(_sid) if _sid else None
                        p['status']        = _status
                        p['unprotected']   = _unprot
                return positions

            locked_read_modify_write(POSITIONS_FILE, _update_pos, default=[])

            if stop_id:
                send_telegram(
                    f"✅ ALPACA TRADE FILLED\n"
                    f"🏷 {name} ({ticker.replace('_US_EQ','').replace('_EQ','')})\n"
                    f"📐 Qty: {filled_qty} shares\n"
                    f"💰 Fill price: ${avg_price}\n"
                    f"🛑 Stop: ${stop} (GTC) ✅\n"
                    f"🔖 Stop ID: {stop_id}\n"
                    f"🏦 Venue: Alpaca"
                )

        else:
            # Still pending
            try:
                opened_dt = datetime.fromisoformat(opened_iso.replace('Z', '+00:00'))
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                age_min = (now - opened_dt).total_seconds() / 60
                _log(f"  {name}: still pending (age={age_min:.0f}m, status={status})")
            except Exception:
                _log(f"  {name}: still pending (status={status})")


if __name__ == '__main__':
    run()
