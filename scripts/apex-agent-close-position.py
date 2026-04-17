#!/usr/bin/env python3
"""apex-agent-close-position.py — Market-close a single open position.

Closes the parity gap: previously the agent could tighten stops but not
actually exit a position. This tool cancels any working stop order, then
submits a market-sell for the full quantity, and updates positions.json to
reflect the pending close.

Safety: this is an `execute-trade` tool. Gated by apex-tool-runner — it only
runs when --force is passed, OR when invoked directly with the --confirm flag.

It does NOT loop or retry. One shot, one result. If T212 rejects the order
(e.g. market closed for the venue), the stop order is RESTORED before return,
so the position remains protected.

Usage:
    apex-agent-close-position.py <t212_ticker> --reason "<why>" --confirm
    apex-agent-close-position.py NFE_US_EQ --reason "earnings risk" --confirm

Returns JSON: {status, ticker, quantity, order_id, reason, ...}
"""
import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_utils import (  # noqa: E402
    safe_read, send_telegram, t212_request,
    locked_read_modify_write,
)

LOG_DIR        = '/home/ubuntu/.picoclaw/logs'
POSITIONS_FILE = f'{LOG_DIR}/apex-positions.json'
TICKER_MAP     = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'
ACTIONS_FILE   = f'{LOG_DIR}/apex-agent-actions.json'
LOG_FILE       = f'{LOG_DIR}/apex-agent-close-position.log'
CALENDAR_FILE  = f'{LOG_DIR}/apex-market-calendar.json'

logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)


def _get_currency(t212_ticker):
    tmap = safe_read(TICKER_MAP, {})
    for _, info in tmap.items():
        if isinstance(info, dict) and info.get('t212') == t212_ticker:
            return info.get('currency', 'USD')
    return 'USD'


def _market_open_for(currency):
    """Return True if the venue for this currency is currently open."""
    cal = safe_read(CALENDAR_FILE, {})
    today = cal.get('today', {}) if isinstance(cal, dict) else {}
    if currency == 'GBX' or currency == 'GBP':
        return bool(today.get('uk_currently_open'))
    # USD / EUR / others treated as US venue
    return bool(today.get('us_currently_open'))


def _log_action(entry):
    def _update(data):
        if not isinstance(data, list):
            data = []
        data.append(entry)
        if len(data) > 500:
            data = data[-500:]
        return data
    locked_read_modify_write(ACTIONS_FILE, _update, default=[])


def _mark_closing(t212_ticker, order_id):
    def _update(positions):
        if not isinstance(positions, list):
            return positions
        for p in positions:
            if p.get('t212_ticker') == t212_ticker:
                p['status'] = 'closing'
                p['close_order_id'] = str(order_id) if order_id else None
                p['closed_by'] = 'agent'
                p['close_requested_at'] = datetime.now(timezone.utc).isoformat()
        return positions
    locked_read_modify_write(POSITIONS_FILE, _update, default=[])


def close_position(t212_ticker, reason):
    now = datetime.now(timezone.utc)

    if not t212_ticker:
        return {'status': 'error', 'reason': 'Missing ticker'}

    positions = safe_read(POSITIONS_FILE, [])
    if not isinstance(positions, list):
        return {'status': 'error', 'reason': 'Cannot read positions'}

    pos = next((p for p in positions if p.get('t212_ticker') == t212_ticker), None)
    if not pos:
        return {'status': 'error', 'reason': f'Position {t212_ticker} not found'}

    if pos.get('status') not in ('protected', 'entry_placed', 'open'):
        return {'status': 'error',
                'reason': f'Position status is {pos.get("status")} — not closable'}

    # Alpaca positions are not handled here — different venue, different API.
    if pos.get('venue') == 'ALPACA':
        return {'status': 'error',
                'reason': 'ALPACA positions must close via apex-alpaca-watchdog, not this tool'}

    quantity = float(pos.get('quantity', 0))
    if not math.isfinite(quantity) or quantity <= 0:
        return {'status': 'error', 'reason': f'Invalid quantity: {quantity}'}

    currency = _get_currency(t212_ticker)
    if not _market_open_for(currency):
        return {'status': 'blocked',
                'reason': f'{currency} venue is closed — T212 will reject market orders. '
                          f'Retry after venue open, or tighten stop to defer.',
                'precondition_failed': 'market_hours'}

    stop_order_id = pos.get('stop_order_id')
    had_stop = stop_order_id and str(stop_order_id) not in ('None', '')

    log.info(f"Closing {t212_ticker} qty={quantity} reason={reason!r} "
             f"(stop_order_id={stop_order_id})")

    # Step 1: cancel working stop so it doesn't fight the market sell
    if had_stop:
        t212_request(f'/equity/orders/{stop_order_id}', method='DELETE')
        time.sleep(0.5)

    # Step 2: submit market sell (negative quantity = SELL in T212)
    neg_qty = round(quantity * -1, 8)
    sell_order = t212_request('/equity/orders/market', method='POST', payload={
        'ticker':   t212_ticker,
        'quantity': neg_qty,
    })

    if sell_order is None:
        # Sell rejected — try to restore the stop so the position isn't naked
        log.error(f"Market sell rejected for {t212_ticker}. Attempting to restore stop.")
        restored_id = None
        if had_stop and pos.get('stop'):
            stop_px = pos['stop'] * 100 if currency == 'GBX' else pos['stop']
            restore = t212_request('/equity/orders/stop', method='POST', payload={
                'ticker':       t212_ticker,
                'quantity':     neg_qty,
                'stopPrice':    round(stop_px, 4),
                'timeValidity': 'GOOD_TILL_CANCEL',
            })
            restored_id = restore.get('id') if restore else None

        if had_stop and not restored_id:
            send_telegram(
                f"🚨 AGENT CRITICAL: close failed AND stop not restored for "
                f"{t212_ticker}. Position unprotected. Manual intervention."
            )
        elif restored_id:
            def _restore(positions):
                for p in positions:
                    if p.get('t212_ticker') == t212_ticker:
                        p['stop_order_id'] = str(restored_id)
                return positions
            locked_read_modify_write(POSITIONS_FILE, _restore, default=[])

        return {'status': 'error',
                'reason': 'Market sell rejected by T212',
                'stop_restored': bool(restored_id) if had_stop else None}

    order_id = sell_order.get('id')
    _mark_closing(t212_ticker, order_id)

    _log_action({
        'timestamp':     now.isoformat(),
        'action':        'close_position',
        'ticker':        t212_ticker,
        'quantity':      quantity,
        'entry':         pos.get('entry'),
        'stop':          pos.get('stop'),
        'close_order_id': str(order_id) if order_id else None,
        'reason':        reason,
    })

    log.info(f"Close order placed for {t212_ticker}: order_id={order_id}")
    send_telegram(
        f"🔻 Agent closed {t212_ticker} (qty {quantity})\nReason: {reason}"
    )

    return {
        'status':   'success',
        'ticker':   t212_ticker,
        'quantity': quantity,
        'order_id': order_id,
        'reason':   reason,
        'action':   'position_closing',
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('ticker', help='T212 ticker, e.g. NFE_US_EQ')
    p.add_argument('--reason', default='agent_close', help='Why this close is happening')
    p.add_argument('--confirm', action='store_true',
                   help='Required — prevents accidental invocation')
    args = p.parse_args()

    if not args.confirm:
        print(json.dumps({
            'status': 'blocked',
            'reason': 'close-position requires --confirm flag (or --force via tool runner)',
        }, indent=2))
        return 1

    result = close_position(args.ticker, args.reason)
    print(json.dumps(result, indent=2))
    return 0 if result.get('status') == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
