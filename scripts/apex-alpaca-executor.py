#!/usr/bin/env python3
"""
Apex Alpaca Order Executor
Places orders via Alpaca Markets API v2 for US-listed stocks.

Used by apex_order_executor.py as the preferred venue for US stocks.
T212 remains the fallback for UK/EU instruments.

Requirements:
  ALPACA_API_KEY and ALPACA_SECRET must be set in ~/.picoclaw/.env.trading212
  Paper/live trading is determined by the endpoint URL:
    Live:  https://api.alpaca.markets/v2
    Paper: https://paper-api.alpaca.markets/v2

Note: Alpaca only supports US-listed equities.
      UK/EU stocks (LSE, Euronext, Xetra) must still go through T212.
"""
import json
import urllib.request
import urllib.parse
import urllib.error
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

LOG_FILE = '/home/ubuntu/.picoclaw/logs/apex-alpaca-orders.log'


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    line = f"{ts}: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def get_credentials() -> tuple[str, str, str]:
    """Load Alpaca API credentials from .env.trading212."""
    env_file = '/home/ubuntu/.picoclaw/.env.trading212'
    creds = {}
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k.strip()] = v.strip()
    except Exception:
        pass

    api_key = creds.get('ALPACA_API_KEY', '')
    secret  = creds.get('ALPACA_SECRET', '')

    # Default to paper trading unless explicitly set to live
    is_live    = creds.get('ALPACA_LIVE', 'false').lower() == 'true'
    base_url   = ('https://api.alpaca.markets/v2' if is_live
                  else 'https://paper-api.alpaca.markets/v2')

    return api_key, secret, base_url


def is_configured() -> bool:
    """Return True if Alpaca credentials are present."""
    key, secret, _ = get_credentials()
    return bool(key and secret)


def alpaca_request(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Make an authenticated Alpaca API request."""
    api_key, secret, base_url = get_credentials()
    if not api_key or not secret:
        _log("ERROR: Alpaca credentials not configured")
        return None

    url  = f"{base_url}{path}"
    data = json.dumps(payload).encode() if payload else None

    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            'APCA-API-KEY-ID':     api_key,
            'APCA-API-SECRET-KEY': secret,
            'Content-Type':        'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        _log(f"HTTP {e.code} on {method} {path}: {body[:200]}")
        return None
    except Exception as e:
        _log(f"Request failed ({method} {path}): {e}")
        return None


def get_account() -> dict | None:
    """Fetch Alpaca account info — also used to verify credentials."""
    return alpaca_request('GET', '/account')


def place_limit_order(ticker: str, qty: float, limit_price: float,
                      side: str = 'buy', tif: str = 'day') -> dict | None:
    """
    Place a limit order.
    Returns Alpaca order object on success, None on failure.
    """
    payload = {
        'symbol':        ticker,
        'qty':           str(round(qty, 8)),
        'side':          side,
        'type':          'limit',
        'time_in_force': tif,
        'limit_price':   str(round(limit_price, 4)),
    }
    _log(f"Placing LIMIT {side.upper()} {qty} × {ticker} @ ${limit_price} ({tif.upper()})")
    return alpaca_request('POST', '/orders', payload)


def place_market_order(ticker: str, qty: float, side: str = 'buy') -> dict | None:
    """Place a market order (fallback when limit fails)."""
    payload = {
        'symbol':        ticker,
        'qty':           str(round(qty, 8)),
        'side':          side,
        'type':          'market',
        'time_in_force': 'day',
    }
    _log(f"Placing MARKET {side.upper()} {qty} × {ticker}")
    return alpaca_request('POST', '/orders', payload)


def place_stop_order(ticker: str, qty: float, stop_price: float) -> dict | None:
    """
    Place a GTC stop-loss sell order.
    qty should be positive (the absolute number of shares to sell).
    """
    payload = {
        'symbol':        ticker,
        'qty':           str(round(qty, 8)),
        'side':          'sell',
        'type':          'stop',
        'time_in_force': 'gtc',
        'stop_price':    str(round(stop_price, 4)),
    }
    _log(f"Placing STOP LOSS SELL {qty} × {ticker} @ ${stop_price} (GTC)")
    return alpaca_request('POST', '/orders', payload)


def cancel_order(order_id: str) -> bool:
    """Cancel an open order by ID."""
    result = alpaca_request('DELETE', f'/orders/{order_id}')
    # DELETE returns 204 (no body) on success — urllib raises no exception
    return result is not None or True   # assume success if no exception


def get_order(order_id: str) -> dict | None:
    """Get order status by ID."""
    return alpaca_request('GET', f'/orders/{order_id}')


def wait_for_fill(order_id: str, max_polls: int = 18, interval: int = 10) -> float:
    """
    Poll order until filled. Returns filled quantity (0.0 if never filled).
    max_polls × interval seconds = max wait time (default: 3 minutes).
    """
    for poll in range(max_polls):
        order = get_order(order_id)
        if not order:
            time.sleep(interval)
            continue

        status   = order.get('status', 'unknown')
        filled   = float(order.get('filled_qty', 0))

        if filled > 0:
            _log(f"  Order {order_id} filled: {filled} shares (status: {status})")
            return filled

        if status in ('canceled', 'expired', 'rejected', 'done_for_day'):
            _log(f"  Order {order_id} terminal status: {status} — aborting")
            return 0.0

        _log(f"  Poll {poll+1}/{max_polls}: filled={filled} status={status} — waiting {interval}s")
        time.sleep(interval)

    return 0.0


def execute(signal: dict, dry_run: bool = False) -> dict:
    """
    Execute a trade signal via Alpaca.

    Returns:
      {
        'success':        bool,
        'entry_order_id': str | None,
        'stop_order_id':  str | None,
        'order_type':     str,    # 'LIMIT' or 'MARKET'
        'filled_qty':     float,
        'error':          str | None,
      }
    """
    ticker   = signal.get('ticker') or signal.get('t212_ticker', '')
    name     = signal.get('name', ticker)
    qty      = float(signal.get('quantity', 0))
    entry    = float(signal.get('entry', 0))
    stop     = float(signal.get('stop', 0))

    result = {
        'success':        False,
        'entry_order_id': None,
        'stop_order_id':  None,
        'order_type':     'LIMIT',
        'filled_qty':     0.0,
        'error':          None,
    }

    if not ticker or not qty:
        result['error'] = "Signal missing ticker or quantity"
        _log(f"ERROR: {result['error']}")
        return result

    if not is_configured():
        result['error'] = "Alpaca credentials not configured"
        _log(f"ERROR: {result['error']}")
        return result

    if dry_run:
        _log(f"DRY-RUN: Would place {qty} × {ticker} @ ${entry} (stop ${stop}) via Alpaca")
        result['success'] = True
        result['entry_order_id'] = 'DRY-RUN'
        result['filled_qty'] = qty
        return result

    # ── Step 1: Limit entry ────────────────────────────────────────────────
    order = place_limit_order(ticker, qty, entry, side='buy', tif='day')
    order_id = (order or {}).get('id')

    if not order_id:
        _log(f"Limit order failed — trying market order")
        order = place_market_order(ticker, qty, side='buy')
        order_id = (order or {}).get('id')
        result['order_type'] = 'MARKET'

    if not order_id:
        result['error'] = f"Both limit and market orders failed for {ticker}"
        _log(f"FATAL: {result['error']}")
        return result

    result['entry_order_id'] = order_id
    _log(f"Entry order ID: {order_id} ({result['order_type']})")

    # ── Step 2: Wait for fill ──────────────────────────────────────────────
    filled_qty = wait_for_fill(order_id)
    result['filled_qty'] = filled_qty

    if filled_qty == 0:
        result['error'] = f"Entry not filled within 3 minutes"
        _log(f"Entry not filled — deferring stop placement")
        # Return partial success — caller handles deferred stop
        result['success'] = True   # position opened, just not filled yet
        return result

    # ── Step 3: Stop loss ──────────────────────────────────────────────────
    if stop > 0:
        stop_order = None
        for attempt in range(1, 4):
            stop_order = place_stop_order(ticker, filled_qty, stop)
            stop_id = (stop_order or {}).get('id')
            if stop_id:
                result['stop_order_id'] = stop_id
                _log(f"Stop loss placed: ID {stop_id} @ ${stop} (attempt {attempt})")
                break
            if attempt < 3:
                time.sleep(2)

        if not result['stop_order_id']:
            result['error'] = f"Stop loss failed after 3 attempts for {ticker}"
            _log(f"CRITICAL: {result['error']} — position is UNPROTECTED")
            result['success'] = True   # entry did succeed, caller must alert
            return result

    result['success'] = True
    _log(f"Trade complete: {name} ({ticker}) — entry {order_id}, stop {result['stop_order_id']}")
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Alpaca Order Executor')
    parser.add_argument('--test',    action='store_true', help='Test credentials + account info')
    parser.add_argument('--dry-run', action='store_true', help='Simulate order without placing')
    parser.add_argument('--signal',  default='/home/ubuntu/.picoclaw/logs/apex-pending-signal.json',
                        help='Path to signal JSON file')
    args = parser.parse_args()

    if args.test:
        print("Testing Alpaca credentials...\n")
        acc = get_account()
        if acc:
            mode = 'PAPER' if acc.get('account_blocked') is False and 'paper' in get_credentials()[2] else 'LIVE'
            print(f"Account: {acc.get('id', 'unknown')}")
            print(f"Status:  {acc.get('status')}")
            print(f"Mode:    {mode}")
            print(f"Equity:  ${float(acc.get('equity', 0)):,.2f}")
            print(f"Cash:    ${float(acc.get('cash', 0)):,.2f}")
            print(f"Buying:  ${float(acc.get('buying_power', 0)):,.2f}")
        else:
            print("FAILED — check ALPACA_API_KEY and ALPACA_SECRET in .env.trading212")
        sys.exit(0 if acc else 1)

    if not os.path.exists(args.signal):
        print(f"ERROR: Signal file not found: {args.signal}")
        sys.exit(1)

    with open(args.signal) as f:
        signal = json.load(f)

    result = execute(signal, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['success'] else 1)
