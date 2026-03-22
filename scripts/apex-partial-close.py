#!/usr/bin/env python3
"""
Partial Close at T1
When a position hits Target 1, close 50% and let the rest run to T2.
Locks in profit on half while maintaining upside exposure.

Called by stop monitor when price >= target1.

Professional approach:
- Hit T1: close 50%, move stop to breakeven on remainder
- Hit T2: close remaining 50%
- Never let a winner turn into a loser
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'

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

def execute_partial_close(ticker, name, close_qty, current_price):
    """Place a market sell order for partial quantity."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')

        result = subprocess.run([
            'curl','-s','-X','POST',
            '-H',f'Authorization: Basic {auth}',
            '-H','Content-Type: application/json',
            '-d',f'{{"ticker":"{ticker}","quantity":{close_qty}}}',
            f'{endpoint}/equity/orders/market'
        ], capture_output=True, text=True)

        data = json.loads(result.stdout)
        if data.get('id'):
            return True, data['id']
        return False, result.stdout
    except Exception as e:
        log_error(f"execute_partial_close failed: {e}")
        return False, str(e)

def update_stop_in_t212(ticker, entry_price, remaining_qty):
    """Update stop loss to breakeven after partial close."""
    try:
        env      = load_env()
        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')

        # Cancel existing stop orders for this ticker
        orders_result = subprocess.run([
            'curl','-s','-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/orders'
        ], capture_output=True, text=True)

        orders = json.loads(orders_result.stdout)
        if isinstance(orders, list):
            for order in orders:
                if (order.get('ticker') == ticker and
                    order.get('type') == 'STOP' and
                    order.get('status') == 'NEW'):
                    # Cancel old stop
                    subprocess.run([
                        'curl','-s','-X','DELETE',
                        '-H',f'Authorization: Basic {auth}',
                        f'{endpoint}/equity/orders/{order["id"]}'
                    ], capture_output=True)

        # Place new stop at breakeven
        new_stop = round(entry_price * 0.999, 2)  # Slightly below entry
        result   = subprocess.run([
            'curl','-s','-X','POST',
            '-H',f'Authorization: Basic {auth}',
            '-H','Content-Type: application/json',
            '-d',json.dumps({
                "ticker":        ticker,
                "quantity":      remaining_qty,
                "stopPrice":     new_stop,
                "timeValidity":  "GOOD_TILL_CANCEL"
            }),
            f'{endpoint}/equity/orders/stop'
        ], capture_output=True, text=True)

        data = json.loads(result.stdout)
        return data.get('id') is not None, new_stop

    except Exception as e:
        log_error(f"update_stop_in_t212 failed: {e}")
        return False, entry_price

def process_t1_hit(position, current_price):
    """
    Handle T1 hit — close 50%, move stop to breakeven.
    Called by stop monitor when price >= target1.
    """
    now     = datetime.now(timezone.utc)
    ticker  = position.get('t212_ticker','')
    name    = position.get('name','?')
    qty     = float(position.get('quantity', 0))
    entry   = float(position.get('entry', 0))
    t1      = float(position.get('target1', 0))
    t2      = float(position.get('target2', 0))
    stop    = float(position.get('stop', 0))

    # Check if already partially closed
    if position.get('partial_closed'):
        return False, "Already partially closed at T1"

    if qty <= 0:
        return False, "Invalid quantity"

    # Calculate partial close quantity
    close_qty     = round(qty * 0.5, 2)
    remaining_qty = round(qty - close_qty, 2)

    if close_qty < 0.01:
        return False, f"Quantity too small for partial close: {close_qty}"

    # Calculate P&L on closed portion
    pnl_closed = round(close_qty * (current_price - entry), 2)
    r_achieved = round((current_price - entry) / (entry - stop), 2) if entry > stop else 0

    print(f"  T1 HIT: {name} @ £{current_price:.2f}")
    print(f"  Closing {close_qty} of {qty} shares (50%)")
    print(f"  P&L on closed portion: £{pnl_closed}")

    # Execute partial close
    ok, order_id = execute_partial_close(ticker, name, close_qty, current_price)

    if not ok:
        send_telegram(
            f"⚠️ PARTIAL CLOSE FAILED\n\n"
            f"{name}\n"
            f"Could not close {close_qty} shares at T1\n"
            f"Error: {order_id}\n\n"
            f"Manual action required."
        )
        log_error(f"Partial close failed for {name}: {order_id}")
        return False, f"Order failed: {order_id}"

    # Update stop to breakeven in T212
    stop_ok, new_stop = update_stop_in_t212(ticker, entry, remaining_qty)

    # Update position tracking
    positions = safe_read(POSITIONS_FILE, [])
    for pos in positions:
        if pos.get('t212_ticker') == ticker:
            pos['quantity']      = remaining_qty
            pos['stop']          = new_stop
            pos['partial_closed']= True
            pos['partial_close_price'] = current_price
            pos['partial_close_qty']   = close_qty
            pos['partial_close_pnl']   = pnl_closed
            pos['partial_close_time']  = now.isoformat()
            pos['partial_close_r']     = r_achieved
            break

    atomic_write(POSITIONS_FILE, positions)

    # Log partial close to outcomes
    try:
        outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
        outcomes['trades'].append({
            'name':        name,
            'ticker':      ticker,
            'entry':       entry,
            'exit':        current_price,
            'pnl':         pnl_closed,
            'r':           r_achieved,
            'qty':         close_qty,
            'type':        'PARTIAL_T1',
            'opened':      position.get('opened',''),
            'closed':      now.strftime('%Y-%m-%d'),
            'signal_type': position.get('signal_type',''),
        })
        atomic_write(OUTCOMES_FILE, outcomes)
    except Exception as e:
        log_error(f"Outcome logging failed: {e}")

    # Telegram notification
    send_telegram(
        f"🎯 T1 HIT — PARTIAL CLOSE\n\n"
        f"{name}\n"
        f"Closed: {close_qty} shares @ £{current_price:.2f}\n"
        f"P&L locked: £{pnl_closed} ({r_achieved}R)\n\n"
        f"Remaining: {remaining_qty} shares\n"
        f"Stop moved to breakeven: £{new_stop:.2f}\n"
        f"T2 target: £{t2:.2f}\n\n"
        f"Position now risk-free. Let it run."
    )

    log_warning(f"Partial close executed: {name} {close_qty} shares @ £{current_price} P&L:£{pnl_closed}")
    return True, f"Partial close successful — {close_qty} shares @ £{current_price}"

def check_positions_for_t1(portfolio):
    """
    Check all positions for T1 hits.
    Called by stop monitor every 30 minutes.
    """
    positions = safe_read(POSITIONS_FILE, [])
    actions   = []

    for t212_pos in (portfolio if isinstance(portfolio, list) else []):
        ticker        = t212_pos.get('ticker','')
        current_price = float(t212_pos.get('currentPrice', 0))

        # Find matching tracked position
        pos = next((p for p in positions
                   if p.get('t212_ticker') == ticker), None)
        if not pos:
            continue

        t1            = float(pos.get('target1', 0))
        partial_closed= pos.get('partial_closed', False)

        # Check T1 hit
        if current_price >= t1 > 0 and not partial_closed:
            print(f"  🎯 T1 HIT: {pos.get('name','?')} £{current_price:.2f} >= T1 £{t1:.2f}")
            ok, msg = process_t1_hit(pos, current_price)
            actions.append({'ticker': ticker, 'action': 'PARTIAL_CLOSE', 'ok': ok, 'msg': msg})

    return actions

if __name__ == '__main__':
    # Test display
    positions = safe_read(POSITIONS_FILE, [])
    print(f"\n=== PARTIAL CLOSE STATUS ===")
    for pos in positions:
        t1      = float(pos.get('target1', 0))
        current = float(pos.get('current', 0))
        partial = pos.get('partial_closed', False)
        pct_to_t1 = round((t1 - current) / current * 100, 1) if current > 0 and t1 > current else 0
        icon    = "✅ PARTIAL CLOSED" if partial else f"⏳ {pct_to_t1}% to T1"
        print(f"  {pos.get('name','?'):25} T1:£{t1:.2f} Current:£{current:.2f} | {icon}")
