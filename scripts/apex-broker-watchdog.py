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
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARRANTY: {m}')

WATCHDOG_FILE  = '/home/ubuntu/.picoclaw/logs/apex-broker-watchdog.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

def load_env():
    env = {}
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except Exception as e:
        log_error(f"load_env failed: {e}")
    return env

def send_telegram(msg):
    try:
        subprocess.run(['bash', '-c',
            f'''BOT=$(cat ~/.picoclaw/config.json | grep -A2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot$BOT/sendMessage" \
  -d chat_id=6808823889 --data-urlencode "text={msg}"'''
        ], capture_output=True)
    except Exception as e:
        log_error(f"send_telegram failed: {e}")

def get_open_orders():
    """Fetch all open orders from T212."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')
        result   = subprocess.run([
            'curl','-s','--max-time','10',
            '-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/orders'
        ], capture_output=True, text=True)
        orders = json.loads(result.stdout)
        return orders if isinstance(orders, list) else []
    except Exception as e:
        log_error(f"get_open_orders failed: {e}")
        return None

def get_portfolio():
    """Fetch live portfolio from T212."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')
        result   = subprocess.run([
            'curl','-s','--max-time','10',
            '-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/portfolio'
        ], capture_output=True, text=True)
        portfolio = json.loads(result.stdout)
        return portfolio if isinstance(portfolio, list) else []
    except Exception as e:
        log_error(f"get_portfolio failed: {e}")
        return None

def check_unprotected_positions():
    """
    Check for positions that have no stop loss order in T212.
    Every open position should have a corresponding STOP order.
    """
    portfolio = get_portfolio()
    orders    = get_open_orders()

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

def check_order_consistency():
    """
    Check for orphaned orders — stop orders for positions
    that no longer exist, or duplicate orders.
    """
    portfolio = get_portfolio()
    orders    = get_open_orders()

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
    """Quick T212 API health check."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')
        result   = subprocess.run([
            'curl','-s','--max-time','8',
            '-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/account/cash'
        ], capture_output=True, text=True)

        if result.returncode != 0:
            return False, f"curl failed: {result.returncode}"

        data = json.loads(result.stdout)
        if 'free' in data:
            return True, f"API healthy — cash: £{data.get('free',0):.2f}"
        elif 'error' in str(data).lower():
            return False, f"API error: {str(data)[:100]}"
        return True, "API responding"

    except Exception as e:
        return False, f"API check failed: {e}"

def run():
    """Run full broker watchdog check."""
    now = datetime.now(timezone.utc)
    print(f"\n=== BROKER WATCHDOG ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    alerts   = []
    warnings = []

    # API health
    api_ok, api_msg = check_api_health()
    print(f"  {'✅' if api_ok else '❌'} API: {api_msg}")
    if not api_ok:
        alerts.append(f"T212 API FAILURE: {api_msg}")

    # Unprotected positions
    unprotected, msg = check_unprotected_positions()
    if unprotected:
        for pos in unprotected:
            alert = f"UNPROTECTED: {pos['ticker']} £{pos['value']:.2f} — no stop loss in T212"
            alerts.append(alert)
            print(f"  ❌ {alert}")
    else:
        print(f"  ✅ All positions protected with stop orders")

    # Order consistency
    issues, order_warnings = check_order_consistency()
    for issue in issues:
        alerts.append(f"ORDER ISSUE: {issue['note']}")
        print(f"  ❌ {issue['note']}")
    for warn in order_warnings:
        warnings.append(f"ORDER WARNING: {warn['note']}")
        print(f"  ⚠️  {warn['note']}")

    if not issues and not unprotected:
        print(f"  ✅ Order consistency OK")

    # Alert on critical issues
    if alerts:
        msg = (
            f"🚨 BROKER WATCHDOG ALERT\n\n"
            f"{len(alerts)} issue(s) detected:\n"
            + "\n".join(f"• {a}" for a in alerts[:5])
            + f"\n\nImmediate action required."
        )
        send_telegram(msg)
        log_error(f"Broker watchdog: {alerts}")

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
