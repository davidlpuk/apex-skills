#!/usr/bin/env python3
"""
apex-agent-tighten-stop.py
Autonomous one-directional stop tightening for the Claude Agent.

Safety: This tool can ONLY move stops TIGHTER (closer to current price for longs).
It will refuse to loosen a stop. This makes it safe for autonomous operation —
the agent can only reduce risk, never increase it.

Usage:
    python3 apex-agent-tighten-stop.py <t212_ticker> <new_stop_price>
    python3 apex-agent-tighten-stop.py NFE_US_EQ 0.648

Returns JSON: {status, ticker, old_stop, new_stop, action, ...}
"""
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_utils import (
    atomic_write, safe_read, send_telegram, log_info, log_error, log_warning,
    locked_read_modify_write, t212_request,
)

LOG_DIR        = '/home/ubuntu/.picoclaw/logs'
POSITIONS_FILE = f'{LOG_DIR}/apex-positions.json'
TICKER_MAP     = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'
ACTIONS_FILE   = f'{LOG_DIR}/apex-agent-actions.json'
LOG_FILE       = f'{LOG_DIR}/apex-agent-tighten-stop.log'

logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)


def _get_currency(t212_ticker):
    """Look up currency for a T212 ticker."""
    tmap = safe_read(TICKER_MAP, {})
    for yahoo, info in tmap.items():
        if isinstance(info, dict) and info.get('t212') == t212_ticker:
            return info.get('currency', 'USD')
    return 'USD'


def _to_t212_price(price, currency):
    """GBX instruments require price in pence (x100) for T212 API."""
    if currency == 'GBX':
        return round(float(price) * 100, 2)
    return round(float(price), 4)


def _log_action(action_entry):
    """Append an action to the agent actions log."""
    def _update(data):
        if not isinstance(data, list):
            data = []
        data.append(action_entry)
        # Keep last 500 actions
        if len(data) > 500:
            data = data[-500:]
        return data
    locked_read_modify_write(ACTIONS_FILE, _update, default=[])


def tighten_stop(t212_ticker, new_stop_price):
    """
    Tighten a stop for a given position. One-directional: can only move
    the stop CLOSER to current price (higher for longs), never farther.

    Returns dict with status and details.
    """
    now = datetime.now(timezone.utc)

    # Validate input
    if not t212_ticker or not new_stop_price:
        return {'status': 'error', 'reason': 'Missing ticker or stop price'}

    new_stop = float(new_stop_price)
    if not math.isfinite(new_stop) or new_stop <= 0:
        return {'status': 'error', 'reason': f'Invalid stop price: {new_stop_price}'}

    # Find position
    positions = safe_read(POSITIONS_FILE, [])
    if not isinstance(positions, list):
        return {'status': 'error', 'reason': 'Cannot read positions'}

    pos = None
    for p in positions:
        if p.get('t212_ticker') == t212_ticker:
            pos = p
            break

    if not pos:
        return {'status': 'error', 'reason': f'Position {t212_ticker} not found'}

    if pos.get('status') not in ('protected', 'entry_placed'):
        return {'status': 'error', 'reason': f'Position status is {pos.get("status")}, not active'}

    old_stop = pos.get('stop')
    if not old_stop:
        return {'status': 'error', 'reason': 'No existing stop price in position'}

    # ONE-DIRECTIONAL CHECK: new stop must be TIGHTER (higher for longs)
    # All Apex positions are long — new stop must be higher than old stop
    if new_stop <= old_stop:
        return {
            'status': 'blocked',
            'reason': f'New stop ({new_stop}) is not tighter than current ({old_stop}). '
                      f'Agent can only tighten stops, never loosen.',
            'old_stop': old_stop,
            'new_stop': new_stop,
        }

    # Don't tighten above current price (would trigger immediate sell)
    current_price = pos.get('current', pos.get('current_price'))
    if current_price and new_stop >= float(current_price):
        return {
            'status': 'blocked',
            'reason': f'New stop ({new_stop}) is at or above current price ({current_price}). '
                      f'Use a market sell instead.',
            'old_stop': old_stop,
            'new_stop': new_stop,
        }

    stop_order_id = pos.get('stop_order_id')
    if not stop_order_id or str(stop_order_id) in ('None', ''):
        return {'status': 'error', 'reason': 'No stop_order_id — cannot modify stop'}

    currency = _get_currency(t212_ticker)
    quantity = pos.get('quantity', 0)

    log.info(f"Tightening stop for {t212_ticker}: {old_stop} -> {new_stop} "
             f"(currency={currency}, order_id={stop_order_id})")

    # Step 1: Cancel existing stop order
    cancel_result = t212_request(f'/equity/orders/{stop_order_id}', method='DELETE')
    # 404 = already gone, that's OK
    time.sleep(0.5)

    # Step 2: Place new tighter stop
    t212_price = _to_t212_price(new_stop, currency)
    neg_qty = round(float(quantity) * -1, 8)
    new_order = t212_request('/equity/orders/stop', method='POST', payload={
        'ticker':       t212_ticker,
        'quantity':     neg_qty,
        'stopPrice':    t212_price,
        'timeValidity': 'GOOD_TILL_CANCEL',
    })

    if new_order is None:
        # Failed to place new stop — try to restore old one
        log.error(f"Failed to place new stop for {t212_ticker}. Attempting to restore old stop.")
        restore_price = _to_t212_price(old_stop, currency)
        restore_order = t212_request('/equity/orders/stop', method='POST', payload={
            'ticker':       t212_ticker,
            'quantity':     neg_qty,
            'stopPrice':    restore_price,
            'timeValidity': 'GOOD_TILL_CANCEL',
        })
        if restore_order:
            # Update positions with restored order ID
            _update_position_stop(t212_ticker, old_stop, restore_order.get('id'))
            log.info(f"Restored original stop for {t212_ticker}")
        else:
            log.error(f"CRITICAL: Failed to restore stop for {t212_ticker}! Position unprotected!")
            send_telegram(
                f"🚨 AGENT CRITICAL: Failed to tighten AND restore stop for {t212_ticker}. "
                f"Position may be unprotected. Manual intervention required."
            )

        return {
            'status': 'error',
            'reason': 'Failed to place new stop order. Attempted to restore original.',
            'old_stop': old_stop,
            'new_stop': new_stop,
        }

    new_order_id = new_order.get('id')
    log.info(f"New stop placed for {t212_ticker}: order_id={new_order_id}, "
             f"price={new_stop} (t212: {t212_price})")

    # Step 3: Update positions.json
    _update_position_stop(t212_ticker, new_stop, new_order_id)

    # Step 4: Log the action
    action = {
        'timestamp': now.isoformat(),
        'action': 'tighten_stop',
        'ticker': t212_ticker,
        'old_stop': old_stop,
        'new_stop': new_stop,
        'stop_order_id': new_order_id,
        'entry': pos.get('entry'),
        'current_price': current_price,
        'reason': 'agent_exit_optimizer',
    }
    _log_action(action)

    return {
        'status': 'success',
        'ticker': t212_ticker,
        'old_stop': old_stop,
        'new_stop': new_stop,
        'stop_order_id': new_order_id,
        'action': 'stop_tightened',
    }


def _update_position_stop(t212_ticker, new_stop, new_order_id):
    """Update the stop price and order ID in positions.json."""
    def _update(positions):
        if not isinstance(positions, list):
            return positions
        for p in positions:
            if p.get('t212_ticker') == t212_ticker:
                p['stop'] = new_stop
                if new_order_id:
                    p['stop_order_id'] = str(new_order_id)
                p['stop_tightened_by'] = 'agent'
                p['stop_tightened_at'] = datetime.now(timezone.utc).isoformat()
        return positions
    locked_read_modify_write(POSITIONS_FILE, _update, default=[])


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(json.dumps({'status': 'error', 'reason': 'Usage: apex-agent-tighten-stop.py <ticker> <new_stop>'}))
        sys.exit(1)

    result = tighten_stop(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get('status') == 'success' else 1)
