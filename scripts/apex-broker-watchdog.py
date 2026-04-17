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
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram, t212_request
    from apex_queue_audit import record_transition as _audit
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARRANTY: {m}')
    def _audit(*a, **kw): pass

WATCHDOG_FILE  = '/home/ubuntu/.picoclaw/logs/apex-broker-watchdog.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'


def _log_drift_to_sqlite(ticker, stop_local, stop_t212, delta):
    """Write a stop drift event to the SQLite audit log (non-blocking)."""
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            'apex_state_db', '/home/ubuntu/.picoclaw/scripts/apex-state-db.py')
        _sdb = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_sdb)
        _sdb.log_stop_drift(ticker, stop_local, stop_t212, delta)
    except Exception as _e:
        log_warning(f"SQLite drift log failed (non-blocking): {_e}")

def load_positions():
    """Load local positions file to get stop prices."""
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _to_t212_price(price, currency):
    """
    Convert a signal price (always stored in pounds/dollars) to the unit
    expected by the T212 API.  LSE stocks quoted in GBX (pence) need their
    price multiplied by 100 before being sent — T212 requires the native
    exchange currency, not the normalised pounds value.
    """
    if currency == 'GBX':
        return round(float(price) * 100, 2)
    return round(float(price), 4)


def place_stop_order(ticker, quantity, stop_price, currency='GBP'):
    """
    Place a GTC stop-sell order in T212 via centralised rate-limited caller.
    Returns order ID on success, None on failure.
    currency: instrument currency from positions file ('GBP', 'USD', 'GBX').
              GBX instruments require price in pence (×100).
    """
    neg_qty    = round(float(quantity) * -1, 8)
    t212_price = _to_t212_price(stop_price, currency)
    data = t212_request('/equity/orders/stop', method='POST', payload={
        "ticker":       ticker,
        "quantity":     neg_qty,
        "stopPrice":    t212_price,
        "timeValidity": "GOOD_TILL_CANCEL"
    })
    if data is None:
        log_error(f"place_stop_order: t212_request returned None for {ticker}")
        return None
    order_id = data.get('id')
    if not order_id:
        log_error(f"place_stop_order unexpected response for {ticker}: {data}")
    return order_id

STOP_FAILURES_FILE = '/home/ubuntu/.picoclaw/logs/apex-stop-fix-failures.json'
STOP_FIX_COOLDOWN_HRS = 6   # back off for 6h after 3 consecutive failures
STOP_FIX_MAX_TRIES    = 3   # attempts before entering cooldown

def _load_stop_failures():
    try:
        with open(STOP_FAILURES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_stop_failures(data):
    try:
        with open(STOP_FAILURES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def auto_fix_unprotected(unprotected):
    """
    For each unprotected position, look up the stop price from
    apex-positions.json and place a stop order automatically.
    Returns lists of fixed and failed tickers.
    Backs off after 3 consecutive failures to avoid log spam.
    """
    positions    = load_positions()
    stop_map     = {p['t212_ticker']: p['stop']                      for p in positions if 'stop' in p}
    currency_map = {p['t212_ticker']: p.get('currency', 'GBP')       for p in positions}
    failures     = _load_stop_failures()
    now        = datetime.now(timezone.utc)

    fixed    = []
    failed   = []
    deferred = []  # planned cooldowns (market closed, 0 real failures) — not alarmed

    for pos in unprotected:
        ticker   = pos['ticker']
        quantity = pos['quantity']
        stop     = stop_map.get(ticker)

        if not stop:
            log_error(f"auto_fix: no stop price found for {ticker} in positions file")
            failed.append({'ticker': ticker, 'reason': 'no stop price in positions file'})
            continue

        # Check cooldown — skip if we've failed too many times recently
        rec = failures.get(ticker, {})
        cooldown_until = rec.get('cooldown_until')
        if cooldown_until:
            try:
                cd_dt = datetime.fromisoformat(cooldown_until)
                if now < cd_dt:
                    remaining = round((cd_dt - now).total_seconds() / 3600, 1)
                    consec = rec.get('consecutive_failures', 0)
                    note   = rec.get('note', '')
                    if consec == 0:
                        # Planned market-hours defer — not a real failure, suppress alarm
                        log_warning(f"auto_fix: {ticker} deferred until market open ({remaining}h) — {note}")
                        deferred.append({'ticker': ticker, 'reason': f'awaiting market open ({remaining}h)', 'note': note})
                    else:
                        log_warning(f"auto_fix: {ticker} in cooldown for {remaining}h more after {consec} failures")
                        failed.append({'ticker': ticker, 'reason': f'in cooldown ({remaining}h remaining)'})
                    continue
                else:
                    # Cooldown expired — reset and try again
                    rec = {}
                    failures[ticker] = rec
            except Exception:
                pass

        _currency = currency_map.get(ticker, 'GBP')
        print(f"  🔧 Auto-fixing {ticker}: placing stop @ £{stop} qty={quantity} (currency={_currency})")
        order_id = None
        for attempt in range(1, 4):
            order_id = place_stop_order(ticker, quantity, stop, currency=_currency)
            if order_id:
                break
            if attempt < 3:
                print(f"  ⏳ {ticker}: attempt {attempt} failed, retrying...")
                time.sleep(2)

        if order_id:
            fixed.append({'ticker': ticker, 'stop': stop, 'order_id': order_id})
            failures.pop(ticker, None)  # clear failure record on success
            print(f"  ✅ Stop placed for {ticker} — order {order_id}")
            # Persist stop_order_id and clear unprotected flag immediately.
            # Without this, the next watchdog cycle re-flags the position as
            # unprotected and places a duplicate stop order.
            try:
                from apex_utils import locked_read_modify_write
                _oid = str(order_id)
                _tkr = ticker
                def _persist_stop(positions, _t=_tkr, _sid=_oid, _stop=stop):
                    for p in positions:
                        if p.get('t212_ticker') == _t:
                            p['stop_order_id'] = _sid
                            p['unprotected']   = False
                            p['status']        = 'protected'
                    return positions
                locked_read_modify_write(POSITIONS_FILE, _persist_stop, default=[])
            except Exception as _pe:
                log_warning(f"auto_fix: stop placed for {ticker} but could not persist order_id — {_pe}")
        else:
            consec = rec.get('consecutive_failures', 0) + 1
            rec['consecutive_failures'] = consec
            rec['last_attempt'] = now.isoformat()
            if consec >= STOP_FIX_MAX_TRIES:
                cooldown_dt = now + timedelta(hours=STOP_FIX_COOLDOWN_HRS)
                rec['cooldown_until'] = cooldown_dt.isoformat()
                failures[ticker] = rec
                reason = f'T212 API error ({consec} consecutive failures — entering {STOP_FIX_COOLDOWN_HRS}h cooldown)'
                log_warning(f"auto_fix: {ticker} entering {STOP_FIX_COOLDOWN_HRS}h cooldown after {consec} failures — T212 may not support GTC stops for this instrument")
            else:
                failures[ticker] = rec
                reason = 'T212 API error'
            failed.append({'ticker': ticker, 'reason': reason})
            print(f"  ❌ Failed to place stop for {ticker} after 3 attempts (total failures: {consec})")

    _save_stop_failures(failures)
    return fixed, failed, deferred

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
    Detect positions stuck in 'pending', 'entry_placed', or 'awaiting_fill' status.
    These indicate the execute-order script crashed mid-flight.
    A position stuck in these states for > 30 minutes needs investigation.
    Returns list of stale entries.
    """
    positions = load_positions()
    now = datetime.now(timezone.utc)
    stale = []
    for p in positions:
        status = p.get('status', '')
        if status not in ('pending', 'entry_placed', 'awaiting_fill'):
            continue
        # Alpaca awaiting_fill is expected behaviour when the US market hasn't
        # opened yet — the limit order sits pre-market.  Skip Alpaca positions.
        if p.get('venue') == 'ALPACA':
            continue
        # Prefer opened_iso (full datetime) over opened (date-only) to avoid
        # midnight-parse false positives — e.g. opened="2026-03-25" parses as
        # 00:00 UTC and appears 575m stale at 09:35 even if opened_iso shows 09:33.
        opened_raw = p.get('opened_iso') or p.get('opened', '')
        try:
            opened_dt = datetime.fromisoformat(str(opened_raw).replace('Z', '+00:00'))
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
        _pos_currency = pos.get('currency', 'GBP')
        _t212_stop    = _to_t212_price(stop, _pos_currency)
        print(f"  ✅ {ticker}: addon filled {filled_qty} shares — placing stop @ £{stop} "
              f"(T212={_t212_stop}, currency={_pos_currency})")

        # Duplicate guard: re-fetch live orders immediately before placement
        # to prevent two concurrent watchdog runs placing the same stop
        _live_orders = get_open_orders() or []
        if any(o.get('ticker') == ticker and o.get('type') == 'STOP'
               and o.get('status') in ('NEW', 'WORKING') for o in _live_orders):
            log_warning(f"addon stop: stop already exists for {ticker} — skipping (duplicate guard)")
            def _clear_addon_dup(positions, _t=ticker):
                for p in (positions or []):
                    if p.get('t212_ticker') == _t:
                        p.pop('pending_addon_order_id', None)
                        p.pop('pending_addon_qty', None)
                        p.pop('pending_addon_stop', None)
                return positions
            locked_read_modify_write(POSITIONS_FILE, _clear_addon_dup, default=[])
            actions.append(f"ADDON_STOP_SKIPPED: {ticker} (stop already exists)")
            continue

        neg_qty = round(filled_qty * -1, 8)
        stop_id = None
        for attempt in range(1, 4):
            stop_data = t212_request('/equity/orders/stop', method='POST', payload={
                "ticker":       ticker,
                "quantity":     neg_qty,
                "stopPrice":    _t212_stop,
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
        # Alpaca positions have UUID entry_order_ids — querying them in T212
        # returns HTTP 400 (expects Long).  Alpaca manages its own fill/stop flow.
        # Guard on both venue flag and UUID format to handle positions where
        # venue was not explicitly set to 'ALPACA'.
        if pos.get('venue') == 'ALPACA':
            continue

        ticker   = pos.get('t212_ticker', '')
        name     = pos.get('name', ticker)
        entry_id = pos.get('entry_order_id', '')
        stop     = pos.get('stop_price') or pos.get('stop', 0)

        if not entry_id or not stop:
            continue

        # UUID-format IDs are Alpaca orders — T212 expects Long (numeric) IDs.
        if '-' in str(entry_id):
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
        _pos_currency = pos.get('currency', 'GBP')
        _t212_stop    = _to_t212_price(stop, _pos_currency)
        print(f"  ✅ {ticker}: filled {filled_qty} shares — placing stop @ £{stop} "
              f"(T212={_t212_stop}, currency={_pos_currency})")

        # Duplicate guard: re-fetch live orders immediately before placement
        # to prevent two concurrent watchdog runs placing the same stop
        _live_orders = get_open_orders() or []
        if any(o.get('ticker') == ticker and o.get('type') == 'STOP'
               and o.get('status') in ('NEW', 'WORKING') for o in _live_orders):
            log_warning(f"deferred stop: stop already exists for {ticker} — skipping (duplicate guard)")
            def _mark_protected_dup(positions, _t=ticker):
                for p in (positions or []):
                    if p.get('t212_ticker') == _t and p.get('status') == 'awaiting_fill':
                        p['status']      = 'protected'
                        p['unprotected'] = False
                        p.pop('deferred_stop', None)
                return positions
            locked_read_modify_write(POSITIONS_FILE, _mark_protected_dup, default=[])
            actions.append(f"DEFERRED_SKIPPED: {ticker} (stop already exists)")
            continue

        neg_qty  = round(filled_qty * -1, 8)
        stop_id  = None
        for attempt in range(1, 4):
            stop_data = t212_request('/equity/orders/stop', method='POST', payload={
                "ticker":       ticker,
                "quantity":     neg_qty,
                "stopPrice":    _t212_stop,
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


def check_stop_price_drift(orders=None, portfolio=None):
    """
    Cross-check stop prices in positions.json against live T212 stop orders.

    The AAPL incident (2026-03-26) showed that local and broker stop prices
    can silently diverge. This check runs every watchdog cycle so any drift
    is caught intraday rather than waiting for the morning data-integrity run.

    portfolio: pre-fetched list from get_portfolio() — used to skip positions
    that have been manually closed in T212 (apex-reconcile.py will clean them
    up; we should not alert on stops for positions that no longer exist).

    Returns a list of drift dicts: {ticker, local_stop, t212_stop, delta}.
    Sends Telegram alert for each drifted or missing stop.
    """
    drifts = []
    try:
        # Resolve orders: use pre-fetched list, or fetch now, or SKIP if API unavailable.
        # Critically: if the orders API returns None (rate-limited / Cloudflare block),
        # we must NOT collapse to [] — that makes every stop appear "missing" and causes
        # false STOP MISSING alerts (e.g. INRG 2026-04-16 after burst rate-limit).
        if orders is not None:
            live_orders = orders
        else:
            _fetched = t212_request('/equity/orders', timeout=10)
            if _fetched is None:
                log_warning("check_stop_price_drift: /equity/orders returned None (API unavailable) — skipping drift check to avoid false STOP MISSING alerts")
                return []
            live_orders = _fetched if isinstance(_fetched, list) else []
        t212_stops = {
            str(o['id']): float(o.get('stopPrice', 0))
            for o in live_orders if o.get('type') == 'STOP'
        }
        # Reverse map: ticker → {id, stopPrice} for any live STOP order.
        # Used to heal a stale stop_order_id when T212 has a new order for the same ticker.
        t212_stop_by_ticker = {}
        for o in live_orders:
            if o.get('type') == 'STOP' and o.get('status') in ('NEW', 'WORKING'):
                t212_stop_by_ticker[o.get('ticker', '')] = {
                    'id': str(o['id']),
                    'stopPrice': float(o.get('stopPrice', 0)),
                }
        # Build set of live T212 tickers so we can skip ghost positions.
        # If portfolio wasn't passed, we accept a small false-positive risk
        # rather than making an extra API call here.
        live_tickers = {p.get('ticker', '') for p in (portfolio or [])} if portfolio is not None else None

        positions = safe_read(POSITIONS_FILE, [])
        for p in (positions or []):
            ticker    = p.get('t212_ticker', '')
            pos_stop  = float(p.get('stop', 0))
            # Skip Alpaca-routed positions — their stops are managed by Alpaca,
            # not T212.  Checking T212 for an Alpaca stop_order_id (UUID) causes
            # HTTP 400 (T212 expects a Long) and false STOP MISSING alerts.
            if p.get('venue') == 'ALPACA':
                continue
            raw_sid = p.get('stop_order_id')
            sid = str(raw_sid) if raw_sid is not None else ''
            if not sid or not pos_stop:
                continue
            # Skip positions not in T212 live portfolio — they were manually
            # closed and apex-reconcile.py will remove them on the next run.
            if live_tickers is not None and ticker not in live_tickers:
                log_warning(f"check_stop_price_drift: skipping {ticker} — not in T212 live portfolio (manually closed?)")
                drifts.append({'ticker': ticker, 'local_stop': pos_stop, 't212_stop': None,
                               'delta': None, 'note': 'NOT_IN_T212 — reconcile will clean up'})
                continue
            t212_stop_raw = t212_stops.get(sid)
            if t212_stop_raw is None:
                # Old order ID is gone. Check if T212 has a *new* stop order for
                # this ticker (placed by a previous watchdog cycle or manually).
                # If so, heal the stale stop_order_id in positions.json and continue
                # normally — no real gap, just a stale local reference.
                alt = t212_stop_by_ticker.get(ticker)
                if alt:
                    new_id = alt['id']
                    log_warning(
                        f"check_stop_price_drift: {ticker} old order {sid} gone but "
                        f"found replacement stop {new_id} @ £{alt['stopPrice']:.4f} — "
                        f"healing positions.json"
                    )
                    try:
                        from apex_utils import locked_read_modify_write
                        def _heal_sid(positions, _t=ticker, _new_id=new_id):
                            for _p in (positions or []):
                                if _p.get('t212_ticker') == _t:
                                    _p['stop_order_id'] = int(_new_id)
                            return positions
                        locked_read_modify_write(POSITIONS_FILE, _heal_sid, default=[])
                    except Exception as _he:
                        log_warning(f"heal stop_order_id failed for {ticker}: {_he}")
                    # Continue with the replacement order's price for drift check
                    t212_stop_raw = alt['stopPrice']
                else:
                    # Genuinely missing — no stop in T212 for this ticker at all.
                    msg = f"STOP MISSING: {ticker} order {sid} not in T212 live orders (local stop=£{pos_stop})"
                    log_error(msg)
                    send_telegram(
                        f"⚠️ STOP MISSING IN T212\n\n"
                        f"Ticker: {ticker}\nOrder ID: {sid}\n"
                        f"Local stop price: £{pos_stop}\n\n"
                        f"Run apex-data-integrity.py to reconcile."
                    )
                    drifts.append({'ticker': ticker, 'local_stop': pos_stop, 't212_stop': None, 'delta': None})
                    _log_drift_to_sqlite(ticker, pos_stop, None, None)
                    continue
            # GBX instruments: T212 returns stopPrice in pence, positions.json stores pounds.
            # Convert T212 pence → pounds before comparison so drift check doesn't false-fire.
            currency = p.get('currency', 'GBP')
            if currency == 'GBX' and t212_stop_raw > pos_stop * 10:
                t212_stop = round(t212_stop_raw / 100, 4)
            else:
                t212_stop = t212_stop_raw
            if abs(pos_stop - t212_stop) > 0.02:
                delta = round(pos_stop - t212_stop, 4)
                msg = f"STOP DRIFT: {ticker} local=£{pos_stop} T212=£{t212_stop} Δ={delta:+.4f}"
                log_error(msg)
                # Auto-correct: T212 is the source of truth for what price the
                # stop order will actually trigger at.  Update positions.json so
                # R-multiple, Kelly sizing, and drawdown estimates stay accurate.
                # (AAPL incident 2026-03-26: local=239.74, T212=233.11, diverged silently.)
                try:
                    from apex_utils import locked_read_modify_write
                    def _correct_stop(positions, _t=ticker, _new=t212_stop):
                        for p in (positions or []):
                            if p.get('t212_ticker') == _t:
                                p['stop'] = _new
                        return positions
                    locked_read_modify_write(POSITIONS_FILE, _correct_stop, default=[])
                    log_warning(f"Auto-corrected {ticker} local stop £{pos_stop} → £{t212_stop} (T212 authoritative)")
                    send_telegram(
                        f"⚠️ STOP PRICE DRIFT — AUTO-CORRECTED\n\n"
                        f"Ticker: {ticker}\n"
                        f"Local (was): £{pos_stop}\n"
                        f"T212 live:   £{t212_stop}\n"
                        f"Delta: {delta:+.4f}\n\n"
                        f"positions.json updated to match T212 (T212 is authoritative)."
                    )
                except Exception as _fix_e:
                    log_error(f"Auto-correct stop drift failed for {ticker}: {_fix_e}")
                    send_telegram(
                        f"⚠️ STOP PRICE DRIFT — MANUAL FIX NEEDED\n\n"
                        f"Ticker: {ticker}\n"
                        f"Local (positions.json): £{pos_stop}\n"
                        f"T212 live order:        £{t212_stop}\n"
                        f"Delta: {delta:+.4f}\n\n"
                        f"Auto-correct failed: {_fix_e}\n"
                        f"Run apex-data-integrity.py to reconcile."
                    )
                drifts.append({'ticker': ticker, 'local_stop': pos_stop, 't212_stop': t212_stop, 'delta': delta})
                _log_drift_to_sqlite(ticker, pos_stop, t212_stop, delta)
    except Exception as e:
        log_warning(f"check_stop_price_drift failed: {e}")
    return drifts


def check_dead_pending(orders=None):
    """
    Sweep for 'entry_placed' positions whose T212 limit order has been
    CANCELLED, REJECTED, or EXPIRED — meaning the fill never happened.

    These dead positions must be removed from apex-positions.json so that:
    1. The same ticker can be re-queued on the next scan cycle.
    2. reconcile() doesn't see them as ghost positions and emit phantom outcomes.

    Only runs during market hours (08:00–18:00 UTC Mon–Fri).
    Alpaca positions are skipped (venue guard).

    Returns list of tickers cleaned up.
    """
    from apex_utils import locked_read_modify_write
    cleaned = []
    now = datetime.now(timezone.utc)

    # Only run during market hours
    if now.weekday() >= 5 or not (8 <= now.hour < 18):
        return cleaned

    positions = load_positions()
    entry_placed = [
        p for p in positions
        if p.get('status') == 'entry_placed'
        and p.get('venue', 'T212') != 'ALPACA'
        and '-' not in str(p.get('entry_order_id', ''))  # Alpaca UUID guard
    ]

    if not entry_placed:
        return cleaned

    # Use pre-fetched orders if provided, else skip (avoid extra API call if orders=None)
    if orders is None:
        return cleaned

    # Build lookup: order_id → status
    order_status_map = {}
    for o in (orders or []):
        oid = str(o.get('id', ''))
        if oid:
            order_status_map[oid] = o.get('status', 'UNKNOWN')

    for pos in entry_placed:
        ticker   = pos.get('t212_ticker', '')
        order_id = str(pos.get('entry_order_id', ''))
        name     = pos.get('name', ticker)

        # Check T212 order status
        t212_status = order_status_map.get(order_id)
        if t212_status is None:
            # Order not in open orders — fetch individually to confirm terminal state
            try:
                raw = t212_request(f'/equity/orders/{order_id}')
                t212_status = (raw or {}).get('status', 'UNKNOWN')
            except Exception as _e:
                log_warning(f"check_dead_pending: could not fetch order {order_id} for {ticker}: {_e}")
                continue

        if t212_status in ('CANCELLED', 'REJECTED', 'EXPIRED'):
            log_warning(
                f"Dead entry_placed position: {name} ({ticker}) — "
                f"T212 order {order_id} is {t212_status}. Removing from tracking."
            )
            # Remove via locked write to avoid race with stop-monitor
            def _remove_dead(positions, _ticker=ticker):
                return [p for p in (positions or [])
                        if p.get('t212_ticker') != _ticker
                        or p.get('status') != 'entry_placed']
            locked_read_modify_write(POSITIONS_FILE, _remove_dead, default=[])

            _audit(signal_id=None, ticker=ticker,
                   signal_type=pos.get('signal_type', ''),
                   from_state='entry_placed', to_state='REMOVED',
                   detail=f'dead_pending sweep: T212 order {order_id} was {t212_status}')

            cleaned.append(ticker)
            send_telegram(
                f"🧹 DEAD ENTRY REMOVED\n\n"
                f"{name} ({ticker})\n"
                f"Limit order {order_id} was {t212_status} in T212.\n"
                f"Apex tracking removed — ticker is available for next scan."
            )

    return cleaned


def run():
    """Run full broker watchdog check."""
    now = datetime.now(timezone.utc)
    print(f"\n=== BROKER WATCHDOG ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    alerts   = []
    warnings = []

    # ── STOP_MISSING flag files ───────────────────────────────────────────────
    # Created by executor / deferred-stop logic when stop placement fails.
    # Surface immediately so they never go unnoticed.
    LOG_DIR = '/home/ubuntu/.picoclaw/logs'
    stop_missing_flags = glob.glob(f'{LOG_DIR}/STOP_MISSING_*')
    if stop_missing_flags:
        for flag_path in stop_missing_flags:
            flag_ticker = os.path.basename(flag_path).replace('STOP_MISSING_', '')
            msg = f"STOP_MISSING: {flag_ticker} — stop placement previously failed, position may be unprotected"
            alerts.append(msg)
            print(f"  🚨 {msg}")
    # ─────────────────────────────────────────────────────────────────────────

    # API health (call 1: /equity/account/cash)
    api_ok, api_msg = check_api_health()
    print(f"  {'✅' if api_ok else '❌'} API: {api_msg}")
    if not api_ok:
        alerts.append(f"T212 API FAILURE: {api_msg}")

    # Fetch portfolio and orders ONCE — rate limiter spaces calls automatically
    orders    = get_open_orders()   # /equity/orders
    portfolio = get_portfolio()     # /equity/portfolio

    # If BOTH API calls failed, we cannot verify anything — alert and bail
    if portfolio is None and orders is None:
        msg = "🚨 WATCHDOG: T212 API unreachable — cannot verify position protection"
        log_error(msg)
        send_telegram(msg)
        print(f"  ❌ {msg}")
        output = {
            'timestamp':   now.strftime('%Y-%m-%d %H:%M UTC'),
            'api_healthy': False,
            'alerts':      [msg] + alerts,
            'warnings':    warnings,
            'status':      'ISSUES',
        }
        atomic_write(WATCHDOG_FILE, output)
        return output

    # Unprotected positions — detect then auto-fix
    unprotected, msg = check_unprotected_positions(portfolio=portfolio, orders=orders)
    if unprotected:
        print(f"  ⚠️  {len(unprotected)} unprotected position(s) — attempting auto-fix...")
        fixed, failed, deferred = auto_fix_unprotected(unprotected)

        for f in fixed:
            alerts.append(f"AUTO-FIXED: stop placed for {f['ticker']} @ £{f['stop']} (order {f['order_id']})")
            # Clear the STOP_MISSING flag if it exists — position is now protected
            flag_path = f"{LOG_DIR}/STOP_MISSING_{f['ticker']}"
            try:
                os.remove(flag_path)
                print(f"  🧹 Cleared STOP_MISSING flag for {f['ticker']}")
            except FileNotFoundError:
                pass
        for f in failed:
            alerts.append(f"UNPROTECTED: {f['ticker']} — stop placement failed ({f['reason']})")
        for f in deferred:
            # Planned market-hours defer — log only, no alarm
            print(f"  ⏳ {f['ticker']}: stop deferred — {f['note'] or f['reason']}")

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

    # Stop price drift — compare positions.json stop prices against T212 live orders.
    # Reuses the pre-fetched orders list (no extra API call).
    # Alerts immediately via Telegram; AAPL-style silent drift caught intraday.
    drift_issues = check_stop_price_drift(orders=orders, portfolio=portfolio)
    for d in drift_issues:
        if d.get('delta') is not None:
            alerts.append(f"STOP DRIFT: {d['ticker']} Δ={d['delta']:+.4f}")
            print(f"  ⚠️  Stop drift: {d['ticker']} local=£{d['local_stop']} T212=£{d['t212_stop']}")
        else:
            alerts.append(f"STOP MISSING: {d['ticker']} not in T212 live orders")
            print(f"  🚨 Stop missing in T212: {d['ticker']}")
    if not drift_issues:
        print(f"  ✅ Stop prices in sync with T212")

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

    # Dead entry_placed sweeper — limit orders that T212 cancelled/rejected
    # Runs before stale-pending check so cleaned-up positions don't double-alert
    dead_cleaned = check_dead_pending(orders=orders)
    if dead_cleaned:
        for t in dead_cleaned:
            print(f"  🧹 Dead entry_placed removed: {t}")
    else:
        print(f"  ✅ No dead entry_placed positions")

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

    # Export current position state to Gist for secondary watchdog
    try:
        import subprocess as _sp
        _sp.Popen(
            ['python3', '/home/ubuntu/.picoclaw/scripts/apex-state-export.py'],
            stdout=open('/dev/null', 'w'), stderr=open('/dev/null', 'w')
        )
    except Exception:
        pass

    return output

if __name__ == '__main__':
    run()
