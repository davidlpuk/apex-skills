#!/usr/bin/env python3
"""
Broker API Failure Watchdog
Detects when T212 API fails mid-trade and alerts immediately.

Scenarios it catches:
1. Limit order placed but stop order failed — unprotected position
2. T212 API returns error on order placement
3. Order placed but never confirmed — unknown state
4. Position mismatch after execution — partial fill

Runs after every order execution and on the 30-min stop monitor cycle.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram, t212_request
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARRANTY: {m}')

WATCHDOG_FILE  = '/home/ubuntu/.picoclaw/logs/apex-broker-watchdog.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

def load_positions():
    """Load local positions file to get stop prices."""
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def place_stop_order(ticker, quantity, stop_price):
    """
    Place a GTC stop-sell order in T212 via centralised rate-limited caller.
    Returns order ID on success, None on failure.
    """
    neg_qty = round(float(quantity) * -1, 8)
    data = t212_request('/equity/orders/stop', method='POST', payload={
        "ticker":       ticker,
        "quantity":     neg_qty,
        "stopPrice":    round(float(stop_price), 4),
        "timeValidity": "GOOD_TILL_CANCEL"
    })
    if data is None:
        log_error(f"place_stop_order: t212_request returned None for {ticker}")
        return None
    order_id = data.get('id')
    if not order_id:
        log_error(f"place_stop_order unexpected response for {ticker}: {data}")
    return order_id

def auto_fix_unprotected(unprotected):
    """
    For each unprotected position, look up the stop price from
    apex-positions.json and place a stop order automatically.
    Returns lists of fixed and failed tickers.
    """
    positions = load_positions()
    stop_map  = {p['t212_ticker']: p['stop'] for p in positions if 'stop' in p}

    fixed  = []
    failed = []

    for pos in unprotected:
        ticker   = pos['ticker']
        quantity = pos['quantity']
        stop     = stop_map.get(ticker)

        if not stop:
            log_error(f"auto_fix: no stop price found for {ticker} in positions file")
            failed.append({'ticker': ticker, 'reason': 'no stop price in positions file'})
            continue

        print(f"  🔧 Auto-fixing {ticker}: placing stop @ £{stop} qty={quantity}")
        order_id = place_stop_order(ticker, quantity, stop)
        # No manual sleep needed — t212_request rate limiter handles spacing

        if order_id:
            fixed.append({'ticker': ticker, 'stop': stop, 'order_id': order_id})
            print(f"  ✅ Stop placed for {ticker} — order {order_id}")
        else:
            failed.append({'ticker': ticker, 'reason': 'T212 API error'})
            print(f"  ❌ Failed to place stop for {ticker}")

    return fixed, failed

def get_open_orders():
    """Fetch all open orders from T212 via centralised rate-limited caller."""
    data = t212_request('/equity/orders')
    if data is None:
        return None
    return data if isinstance(data, list) else []

def get_portfolio():
    """Fetch live portfolio from T212 via centralised rate-limited caller."""
    data = t212_request('/equity/portfolio')
    if data is None:
        return None
    return data if isinstance(data, list) else []

def check_unprotected_positions(portfolio=None, orders=None):
    """
    Check for positions that have no stop loss order in T212.
    Every open position should have a corresponding STOP order.
    Accepts pre-fetched portfolio/orders to avoid duplicate API calls.
    """
    if portfolio is None:
        portfolio = get_portfolio()
    if orders is None:
        orders = get_open_orders()

    if portfolio is None or orders is None:
        return [], "Cannot fetch T212 data"

    # Build set of tickers with active stop orders
    protected_tickers = set()
    for order in orders:
        if (order.get('type') == 'STOP' and
            order.get('status') in ['NEW', 'WORKING']):
            protected_tickers.add(order.get('ticker',''))

    # Check each position
    unprotected = []
    for pos in portfolio:
        ticker = pos.get('ticker','')
        qty    = float(pos.get('quantity', 0))
        price  = float(pos.get('currentPrice', 0))

        if qty > 0 and ticker not in protected_tickers:
            unprotected.append({
                'ticker':   ticker,
                'quantity': qty,
                'value':    round(qty * price, 2),
                'price':    price,
            })

    return unprotected, "OK"

def check_order_consistency(portfolio=None, orders=None):
    """
    Check for orphaned orders — stop orders for positions
    that no longer exist, or duplicate orders.
    Accepts pre-fetched portfolio/orders to avoid duplicate API calls.
    """
    if portfolio is None:
        portfolio = get_portfolio()
    if orders is None:
        orders = get_open_orders()

    if portfolio is None or orders is None:
        return [], []

    live_tickers = {p.get('ticker','') for p in portfolio}
    issues       = []
    warnings     = []

    # Stop orders for non-existent positions
    for order in orders:
        ticker   = order.get('ticker','')
        order_type = order.get('type','')
        status   = order.get('status','')

        if (order_type == 'STOP' and
            status in ['NEW','WORKING'] and
            ticker not in live_tickers):
            issues.append({
                'type':    'ORPHANED_STOP',
                'ticker':  ticker,
                'order_id':order.get('id',''),
                'note':    f"Stop order exists but no position found for {ticker}",
            })

    # Duplicate stop orders for same ticker
    stop_by_ticker = {}
    for order in orders:
        if order.get('type') == 'STOP' and order.get('status') in ['NEW','WORKING']:
            ticker = order.get('ticker','')
            if ticker not in stop_by_ticker:
                stop_by_ticker[ticker] = []
            stop_by_ticker[ticker].append(order)

    for ticker, stops in stop_by_ticker.items():
        if len(stops) > 1:
            warnings.append({
                'type':    'DUPLICATE_STOPS',
                'ticker':  ticker,
                'count':   len(stops),
                'note':    f"{ticker} has {len(stops)} stop orders — potential double protection",
            })

    return issues, warnings

def check_api_health():
    """Quick T212 API health check via centralised rate-limited caller."""
    try:
        data = t212_request('/equity/account/cash', timeout=8)
        if data is None:
            return False, "API check failed or TooManyRequests after retries"
        if 'free' in data:
            return True, f"API healthy — cash: £{data.get('free', 0):.2f}"
        elif 'error' in str(data).lower():
            return False, f"API error: {str(data)[:100]}"
        return True, "API responding"
    except Exception as e:
        return False, f"API check failed: {e}"

def check_stale_pending_positions():
    """
    Detect positions stuck in 'pending' or 'entry_placed' status.
    These indicate the execute-order script crashed mid-flight.
    A position stuck in these states for > 30 minutes needs investigation.
    Returns list of stale entries.
    """
    positions = load_positions()
    now = datetime.now(timezone.utc)
    stale = []
    for p in positions:
        status = p.get('status', '')
        if status not in ('pending', 'entry_placed'):
            continue
        opened = p.get('opened', '')
        try:
            opened_dt = datetime.fromisoformat(opened) if 'T' in str(opened) else datetime.strptime(opened, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
            age_mins = (now - opened_dt).total_seconds() / 60
            if age_mins > 30:
                stale.append({
                    'ticker':   p.get('t212_ticker', '?'),
                    'name':     p.get('name', '?'),
                    'status':   status,
                    'age_mins': round(age_mins),
                })
        except Exception:
            # Can't parse date — treat as stale
            stale.append({'ticker': p.get('t212_ticker', '?'), 'name': p.get('name', '?'), 'status': status, 'age_mins': '?'})
    return stale


def check_addon_orders():
    """
    Handle positions with a pending addon order (e.g. XOM has 4 shares protected
    but a 1.87-share limit order is still pending).  When the addon fills, place
    an additional stop for those shares.
    """
    from apex_utils import locked_read_modify_write
    positions = load_positions()
    actions   = []

    for pos in positions:
        addon_id  = pos.get('pending_addon_order_id')
        if not addon_id:
            continue
        ticker    = pos.get('t212_ticker', '')
        name      = pos.get('name', ticker)
        addon_qty = float(pos.get('pending_addon_qty', 0))
        stop      = float(pos.get('pending_addon_stop') or pos.get('stop', 0))

        try:
            order = t212_request(f'/equity/orders/{addon_id}') or {}
        except Exception as e:
            log_error(f"addon check: order fetch failed for {ticker}: {e}")
            continue

        filled_qty = float(order.get('filledQuantity', 0))
        status_str = order.get('status', 'UNKNOWN')

        if filled_qty == 0 and status_str in ('CANCELLED', 'REJECTED', 'EXPIRED'):
            def _clear_addon(positions, _t=ticker):
                for p in (positions or []):
                    if p.get('t212_ticker') == _t:
                        p.pop('pending_addon_order_id', None)
                        p.pop('pending_addon_qty', None)
                        p.pop('pending_addon_stop', None)
                return positions
            locked_read_modify_write(POSITIONS_FILE, _clear_addon, default=[])
            send_telegram(f"⚠️ ADDON ORDER EXPIRED\n\n{name} ({ticker})\nLimit order for {addon_qty} shares expired without filling.")
            actions.append(f"ADDON_EXPIRED: {ticker}")
            continue

        if filled_qty == 0:
            print(f"  ⏳ {ticker}: addon order {addon_id} still pending (status={status_str})")
            continue

        # Filled — place stop for the addon quantity
        print(f"  ✅ {ticker}: addon filled {filled_qty} shares — placing stop @ £{stop}")
        neg_qty = round(filled_qty * -1, 8)
        stop_id = None
        for attempt in range(1, 4):
            stop_data = t212_request('/equity/orders/stop', method='POST', payload={
                "ticker":       ticker,
                "quantity":     neg_qty,
                "stopPrice":    round(stop, 4),
                "timeValidity": "GOOD_TILL_CANCEL",
            })
            stop_id = (stop_data or {}).get('id')
            if stop_id:
                break
            if attempt < 3:
                time.sleep(2)

        def _clear_addon(positions, _t=ticker):
            for p in (positions or []):
                if p.get('t212_ticker') == _t:
                    p.pop('pending_addon_order_id', None)
                    p.pop('pending_addon_qty', None)
                    p.pop('pending_addon_stop', None)
            return positions
        locked_read_modify_write(POSITIONS_FILE, _clear_addon, default=[])

        if stop_id:
            send_telegram(
                f"✅ ADDON STOP PLACED\n\n{name} ({ticker})\n"
                f"+{filled_qty} shares filled — stop at £{stop} (order {stop_id})"
            )
            actions.append(f"ADDON_STOP: {ticker} @ £{stop}")
        else:
            try:
                open(f'/home/ubuntu/.picoclaw/logs/STOP_MISSING_{ticker}', 'w').close()
            except Exception:
                pass
            send_telegram(
                f"🚨 ADDON STOP FAILED\n\n{name} ({ticker})\n"
                f"+{filled_qty} shares filled but stop failed. Set manual stop at £{stop} in T212."
            )
            log_error(f"addon stop failed for {ticker}")
            actions.append(f"ADDON_STOP_FAILED: {ticker}")

    return actions


def check_and_place_deferred_stops():
    """
    Find positions in 'awaiting_fill' state (limit order placed but not
    yet filled when executor ran).  For each one, check if the entry order
    has now filled and — if so — place the stop automatically.
    Returns list of actions taken.
    """
    from apex_utils import locked_read_modify_write
    positions = load_positions()
    actions   = []

    for pos in positions:
        if pos.get('status') != 'awaiting_fill':
            continue

        ticker   = pos.get('t212_ticker', '')
        name     = pos.get('name', ticker)
        entry_id = pos.get('entry_order_id', '')
        stop     = pos.get('stop_price') or pos.get('stop', 0)

        if not entry_id or not stop:
            continue

        # Check fill status
        try:
            order = t212_request(f'/equity/orders/{entry_id}') or {}
        except Exception as e:
            log_error(f"deferred stop: order fetch failed for {ticker}: {e}")
            continue

        filled_qty = float(order.get('filledQuantity', 0))
        status_str = order.get('status', 'UNKNOWN')

        if filled_qty == 0 and status_str in ('CANCELLED', 'REJECTED', 'EXPIRED'):
            # Order never filled — remove from tracking
            def _remove(positions, _t=ticker):
                return [p for p in (positions or [])
                        if not (p.get('t212_ticker') == _t
                                and p.get('status') == 'awaiting_fill')]
            locked_read_modify_write(POSITIONS_FILE, _remove, default=[])
            send_telegram(
                f"⚠️ ENTRY ORDER {status_str}\n\n{name} ({ticker})\n"
                f"Limit order expired without filling. Position removed from tracking."
            )
            actions.append(f"EXPIRED: {ticker} — entry never filled, removed")
            continue

        if filled_qty == 0:
            print(f"  ⏳ {ticker}: awaiting fill (status={status_str})")
            continue  # still pending — check again next cycle

        # Order filled — place stop now
        print(f"  ✅ {ticker}: filled {filled_qty} shares — placing stop @ £{stop}")
        neg_qty  = round(filled_qty * -1, 8)
        stop_id  = None
        for attempt in range(1, 4):
            stop_data = t212_request('/equity/orders/stop', method='POST', payload={
                "ticker":       ticker,
                "quantity":     neg_qty,
                "stopPrice":    round(float(stop), 4),
                "timeValidity": "GOOD_TILL_CANCEL",
            })
            stop_id = (stop_data or {}).get('id')
            if stop_id:
                break
            if attempt < 3:
                time.sleep(2)

        if stop_id:
            def _mark_protected(positions, _t=ticker, _sid=str(stop_id)):
                for p in (positions or []):
                    if p.get('t212_ticker') == _t and p.get('status') == 'awaiting_fill':
                        p['status']        = 'protected'
                        p['stop_order_id'] = _sid
                        p['unprotected']   = False
                        p.pop('deferred_stop', None)
                return positions
            locked_read_modify_write(POSITIONS_FILE, _mark_protected, default=[])
            send_telegram(
                f"✅ DEFERRED STOP PLACED\n\n{name} ({ticker})\n"
                f"Entry filled ({filled_qty} shares) — stop now active at £{stop} (order {stop_id})\n"
                f"Position is fully protected."
            )
            actions.append(f"STOP_PLACED: {ticker} @ £{stop} (order {stop_id})")
        else:
            # Stop still failing — flag as unprotected
            def _mark_unprot(positions, _t=ticker):
                for p in (positions or []):
                    if p.get('t212_ticker') == _t and p.get('status') == 'awaiting_fill':
                        p['status']      = 'unprotected'
                        p['unprotected'] = True
                return positions
            locked_read_modify_write(POSITIONS_FILE, _mark_unprot, default=[])
            try:
                open(f'/home/ubuntu/.picoclaw/logs/STOP_MISSING_{ticker}', 'w').close()
            except Exception:
                pass
            send_telegram(
                f"🚨 DEFERRED STOP FAILED\n\n{name} ({ticker})\n"
                f"Entry filled but stop placement failed after 3 attempts.\n"
                f"Log in to T212 and set manual stop at £{stop}"
            )
            log_error(f"deferred stop failed for {ticker} after fill")
            actions.append(f"STOP_FAILED: {ticker}")

    return actions


def run():
    """Run full broker watchdog check."""
    now = datetime.now(timezone.utc)
    print(f"\n=== BROKER WATCHDOG ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    alerts   = []
    warnings = []

    # API health (call 1: /equity/account/cash)
    api_ok, api_msg = check_api_health()
    print(f"  {'✅' if api_ok else '❌'} API: {api_msg}")
    if not api_ok:
        alerts.append(f"T212 API FAILURE: {api_msg}")

    # Fetch portfolio and orders ONCE — rate limiter spaces calls automatically
    orders    = get_open_orders()   # /equity/orders
    portfolio = get_portfolio()     # /equity/portfolio

    # Unprotected positions — detect then auto-fix
    unprotected, msg = check_unprotected_positions(portfolio=portfolio, orders=orders)
    if unprotected:
        print(f"  ⚠️  {len(unprotected)} unprotected position(s) — attempting auto-fix...")
        fixed, failed = auto_fix_unprotected(unprotected)

        for f in fixed:
            alerts.append(f"AUTO-FIXED: stop placed for {f['ticker']} @ £{f['stop']} (order {f['order_id']})")
        for f in failed:
            alerts.append(f"UNPROTECTED: {f['ticker']} — stop placement failed ({f['reason']})")

        if fixed:
            fix_msg = (
                f"🛡️ WATCHDOG AUTO-FIX\n\n"
                f"Stop orders placed automatically:\n"
                + "\n".join(f"✅ {f['ticker']} @ £{f['stop']}" for f in fixed)
                + (f"\n\n❌ Failed: {', '.join(f['ticker'] for f in failed)}" if failed else "")
            )
            send_telegram(fix_msg)

        if failed:
            fail_msg = (
                f"🚨 WATCHDOG: Stop placement failed\n\n"
                + "\n".join(f"• {f['ticker']}: {f['reason']}" for f in failed)
                + "\n\nManual intervention required."
            )
            send_telegram(fail_msg)
            log_error(f"auto_fix failed for: {[f['ticker'] for f in failed]}")
    else:
        print(f"  ✅ All positions protected with stop orders")

    # Order consistency (uses pre-fetched data — no extra API calls)
    issues, order_warnings = check_order_consistency(portfolio=portfolio, orders=orders)
    for issue in issues:
        alerts.append(f"ORDER ISSUE: {issue['note']}")
        print(f"  ❌ {issue['note']}")
    for warn in order_warnings:
        warnings.append(f"ORDER WARNING: {warn['note']}")
        print(f"  ⚠️  {warn['note']}")

    if not issues and not unprotected:
        print(f"  ✅ Order consistency OK")

    # Addon orders — extra shares from pre-market limits that have since filled
    addon_actions = check_addon_orders()
    for a in addon_actions:
        if 'FAILED' in a:
            alerts.append(f"ADDON STOP FAILED: {a}")

    # Deferred stops — limit orders placed pre-market that have since filled
    deferred_actions = check_and_place_deferred_stops()
    for a in deferred_actions:
        if a.startswith('STOP_FAILED'):
            alerts.append(f"DEFERRED STOP FAILED: {a}")
        else:
            print(f"  ✅ Deferred: {a}")

    # Stale pending/entry_placed positions — script crashed mid-execution
    stale_pending = check_stale_pending_positions()
    if stale_pending:
        for sp in stale_pending:
            msg = f"STALE {sp['status'].upper()}: {sp['ticker']} ({sp['name']}) stuck for {sp['age_mins']}m — execute-order may have crashed"
            alerts.append(msg)
            print(f"  ⚠️  {msg}")
        send_telegram(
            f"⚠️ WATCHDOG: Stale in-flight order(s)\n\n"
            + "\n".join(f"• {sp['ticker']} — status={sp['status']}, age={sp['age_mins']}m" for sp in stale_pending)
            + "\n\nCheck apex-positions.json and T212 manually."
        )
    else:
        print(f"  ✅ No stale in-flight orders")

    # Alert only on issues that couldn't be auto-fixed (API failures, order issues, etc.)
    critical = [a for a in alerts if not a.startswith('AUTO-FIXED:')]
    if critical:
        msg = (
            f"🚨 BROKER WATCHDOG ALERT\n\n"
            f"{len(critical)} issue(s) detected:\n"
            + "\n".join(f"• {a}" for a in critical[:5])
            + f"\n\nImmediate action required."
        )
        send_telegram(msg)
        log_error(f"Broker watchdog: {critical}")

    # Save report
    output = {
        'timestamp':   now.strftime('%Y-%m-%d %H:%M UTC'),
        'api_healthy': api_ok,
        'alerts':      alerts,
        'warnings':    warnings,
        'status':      'CLEAR' if not alerts else 'ISSUES',
    }
    atomic_write(WATCHDOG_FILE, output)

    print(f"\n  Status: {'✅ CLEAR' if not alerts else '❌ ISSUES DETECTED'}")
    return output

if __name__ == '__main__':
    run()
