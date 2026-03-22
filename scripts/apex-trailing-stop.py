#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
TRAILING_FILE  = '/home/ubuntu/.picoclaw/logs/apex-trailing-stops.json'
LOG            = '/home/ubuntu/.picoclaw/logs/apex-orders.log'

def load_positions():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except:
        return []

def save_positions(positions):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(positions, f, indent=2)

def load_trailing():
    try:
        with open(TRAILING_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_trailing(data):
    with open(TRAILING_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def send_telegram(message):
    subprocess.run([
        'bash', '-c',
        f'''BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot${{BOT_TOKEN}}/sendMessage" \
  -d chat_id="6808823889" \
  --data-urlencode "text={message}"'''
    ], capture_output=True, text=True)

def cancel_stop_order(stop_order_id):
    result = subprocess.run([
        'bash', '-c',
        f'''source ~/.picoclaw/.env.trading212
curl -s -X DELETE \
  -H "Authorization: Basic $T212_AUTH" \
  https://demo.trading212.com/api/v0/equity/orders/{stop_order_id}'''
    ], capture_output=True, text=True)
    return result.returncode == 0

def place_stop_order(ticker, quantity, stop_price):
    neg_qty = float(quantity) * -1
    result = subprocess.run([
        'bash', '-c',
        f'''source ~/.picoclaw/.env.trading212
curl -s -X POST \
  -H "Authorization: Basic $T212_AUTH" \
  -H "Content-Type: application/json" \
  -d '{{"ticker":"{ticker}","quantity":{neg_qty},"stopPrice":{stop_price},"timeValidity":"GOOD_TILL_CANCEL"}}' \
  https://demo.trading212.com/api/v0/equity/orders/stop'''
    ], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        return data.get('id')
    except:
        return None

def get_live_prices():
    result = subprocess.run([
        'bash', '-c',
        '''source ~/.picoclaw/.env.trading212
curl -s -H "Authorization: Basic $T212_AUTH" \
  https://demo.trading212.com/api/v0/equity/portfolio'''
    ], capture_output=True, text=True)
    try:
        portfolio = json.loads(result.stdout)
        return {p['ticker']: float(p.get('currentPrice', 0)) for p in portfolio}
    except:
        return {}

def run():
    positions = load_positions()
    trailing  = load_trailing()
    prices    = get_live_prices()
    now       = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    if not positions:
        print("No open positions")
        return

    updates = []

    for pos in positions:
        ticker    = pos.get('t212_ticker', '')
        name      = pos.get('name', ticker)
        entry     = float(pos.get('entry', 0))
        stop      = float(pos.get('stop', 0))
        target1   = float(pos.get('target1', 0))
        target2   = float(pos.get('target2', 0))
        quantity  = float(pos.get('quantity', 0))
        stop_id   = pos.get('stop_order_id', '')
        t1_hit    = pos.get('t1_hit', False)
        t2_hit    = pos.get('t2_hit', False)

        current = prices.get(ticker, 0)
        if not current:
            continue

        # Calculate R
        risk = entry - stop if entry > stop else 1
        r    = round((current - entry) / risk, 2)

        print(f"{name}: £{current} | Entry £{entry} | Stop £{stop} | T1 £{target1} | R:{r}")

        # Check Target 2 hit
        if not t2_hit and current >= target2:
            print(f"  🎯 TARGET 2 HIT — {name}")
            send_telegram(
                f"🎯 TARGET 2 HIT — {name}\n\n"
                f"Price £{current} reached T2 £{target2}\n"
                f"R achieved: {r}\n\n"
                f"Consider closing full position.\n"
                f"Reply: CLOSE {ticker}"
            )
            pos['t2_hit'] = True
            updates.append(name)

        # Check Target 1 hit — move stop to breakeven
        elif not t1_hit and current >= target1:
            print(f"  🎯 TARGET 1 HIT — moving stop to breakeven for {name}")

            # Cancel existing stop order in T212
            cancelled = False
            if stop_id:
                cancelled = cancel_stop_order(stop_id)
                print(f"  Cancelled old stop order {stop_id}: {cancelled}")

            # Place new stop at entry (breakeven)
            new_stop_id = place_stop_order(ticker, quantity, entry)
            print(f"  New breakeven stop order: {new_stop_id}")

            if new_stop_id:
                pos['stop']          = entry
                pos['stop_order_id'] = str(new_stop_id)
                pos['t1_hit']        = True
                pos['breakeven_set'] = now

                send_telegram(
                    f"🎯 TARGET 1 HIT — {name}\n\n"
                    f"Price £{current} reached T1 £{target1}\n"
                    f"R achieved: {r}\n\n"
                    f"✅ Stop moved to BREAKEVEN £{entry}\n"
                    f"New stop order placed in T212 (ID: {new_stop_id})\n\n"
                    f"This trade cannot lose money now.\n"
                    f"Riding to Target 2: £{target2}"
                )
                updates.append(name)
            else:
                send_telegram(
                    f"🎯 TARGET 1 HIT — {name}\n\n"
                    f"Price £{current} reached T1 £{target1}\n\n"
                    f"⚠️ Could not place breakeven stop automatically.\n"
                    f"Manually move stop to £{entry} in T212 now."
                )

        # Warn if approaching stop
        elif current <= stop * 1.02 and current > stop:
            pct_from_stop = round((current - stop) / stop * 100, 1)
            if pct_from_stop <= 2:
                print(f"  ⚠️ NEAR STOP — {name} only {pct_from_stop}% above stop")
                send_telegram(
                    f"⚠️ STOP APPROACHING — {name}\n\n"
                    f"Price £{current} | Stop £{stop}\n"
                    f"Only {pct_from_stop}% above stop level\n\n"
                    f"Consider: CLOSE {ticker}"
                )

        # Stop hit
        elif current <= stop:
            print(f"  🚨 STOP HIT — {name}")
            send_telegram(
                f"🚨 STOP HIT — {name}\n\n"
                f"Price £{current} hit stop £{stop}\n"
                f"T212 stop order should have triggered.\n\n"
                f"Check T212 and update positions:\n"
                f"CLOSE {ticker}"
            )

    # Save updated positions
    if updates:
        save_positions(positions)
        print(f"Updated positions: {', '.join(updates)}")
    else:
        print("No trailing stop updates needed")

if __name__ == '__main__':
    run()
