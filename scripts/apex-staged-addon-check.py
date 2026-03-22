#!/usr/bin/env python3
"""
Staged Add-on Checker
Checks pending Stage 2 add-on orders and executes when conditions are met.
Conditions:
1. 5 days have passed since Stage 1
2. Price is above the trigger level (stabilised)
3. Position is still open
"""
import json
import subprocess
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


ADDON_FILE     = '/home/ubuntu/.picoclaw/logs/apex-staged-addons.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
PENDING_FILE   = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'

def load_env():
    env = {}
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except Exception as _e:
        log_error(f"Silent failure in apex-staged-addon-check.py: {_e}")
    return env

def send_telegram(msg):
    subprocess.run(['bash','-c',
        f'''BOT=$(cat ~/.picoclaw/config.json | grep -A2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot$BOT/sendMessage" \
  -d chat_id=6808823889 --data-urlencode "text={msg}"'''
    ], capture_output=True)

def get_current_price(ticker):
    env  = load_env()
    auth = env.get('T212_AUTH','')
    endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')
    result = subprocess.run([
        'curl','-s','-H',f'Authorization: Basic {auth}',
        f'{endpoint}/equity/portfolio'
    ], capture_output=True, text=True)
    try:
        portfolio = json.loads(result.stdout)
        pos = next((p for p in portfolio if p.get('ticker') == ticker), None)
        return float(pos.get('currentPrice', 0)) if pos else None
    except:
        return None

def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== STAGED ADD-ON CHECKER ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    try:
        with open(ADDON_FILE) as f:
            addons = json.load(f)
    except:
        print("No pending add-ons")
        return

    pending  = [a for a in addons if a.get('status') == 'PENDING']
    updated  = False

    print(f"Pending add-ons: {len(pending)}")

    for addon in pending:
        name     = addon.get('name','?')
        ticker   = addon.get('symbol','')
        qty      = addon.get('qty', 0)
        trigger  = float(addon.get('trigger', 0))
        stop     = float(addon.get('stop', 0))
        execute_after = addon.get('execute_after','')
        days_waited   = addon.get('days_to_wait', 5)

        print(f"\n  Checking {name}:")
        print(f"    Trigger price: £{trigger}")
        print(f"    Execute after: {execute_after[:10]}")

        # Check time condition
        try:
            ea = datetime.fromisoformat(execute_after)
            if now < ea:
                days_left = (ea - now).days
                print(f"    ⏳ {days_left} days remaining before add-on eligible")
                continue
        except Exception as _e:
            log_error(f"Silent failure in apex-staged-addon-check.py: {_e}")

        # Check price condition
        current = get_current_price(ticker)
        if current is None:
            print(f"    ⚠️  Cannot get price for {ticker}")
            continue

        print(f"    Current price: £{current:.2f} | Trigger: £{trigger:.2f}")

        # Price cap — don't chase if move already happened
        entry_signal  = float(addon.get('entry_signal', trigger))
        max_add_price = round(entry_signal * 1.04, 2)  # Max 4% above Stage 1 entry

        if current > max_add_price:
            print(f"    ❌ Price £{current:.2f} too far above entry £{entry_signal:.2f} ({round((current-entry_signal)/entry_signal*100,1)}%) — move already happened, skipping Stage 2")
            addon['status']        = 'SKIPPED'
            addon['skip_reason']   = f"Price {round((current-entry_signal)/entry_signal*100,1)}% above entry — exceeded 4% cap"
            addon['skipped_at']    = now.isoformat()
            updated = True
            send_telegram(
                f"⏭️ STAGE 2 SKIPPED\n\n"
                f"{name}\n"
                f"Price £{current:.2f} is {round((current-entry_signal)/entry_signal*100,1)}% above Stage 1 entry £{entry_signal:.2f}\n"
                f"Move already happened — not chasing.\n"
                f"Stage 1 position remains open."
            )
            continue

        if current >= trigger:
            print(f"    ✅ Price above trigger — executing Stage 2 add-on")

            # Build add-on signal
            signal = {
                'name':        name,
                't212_ticker': ticker,
                'entry':       current,
                'stop':        stop,
                'target1':     addon.get('entry_signal', current) * 1.08,
                'target2':     addon.get('entry_signal', current) * 1.15,
                'quantity':    qty,
                'score':       7,
                'signal_type': 'CONTRARIAN',
                'rsi':         0,
                'sector':      'CONTRARIAN_ADDON',
                'currency':    'USD',
                'generated_at':now.isoformat(),
                'notes':       'Stage 2 add-on — price stabilised above trigger',
            }

            atomic_write(PENDING_FILE, signal)

            result = subprocess.run(
                ['bash', '/home/ubuntu/.picoclaw/scripts/apex-execute-order.sh'],
                capture_output=True, text=True
            )

            if result.returncode == 0:
                addon['status'] = 'EXECUTED'
                addon['executed_at'] = now.isoformat()
                addon['executed_price'] = current
                updated = True

                send_telegram(
                    f"✅ STAGE 2 ADD-ON EXECUTED\n\n"
                    f"{name}\n"
                    f"Added {qty} more shares @ £{current:.2f}\n"
                    f"Stop: £{stop:.2f}\n\n"
                    f"Position now fully sized. Both stages complete."
                )
                print(f"    ✅ Stage 2 executed successfully")
            else:
                print(f"    ❌ Execution failed")

        else:
            pct_away = round((trigger - current) / current * 100, 1)
            print(f"    ⏳ Price {pct_away}% below trigger — not yet stabilised")

            # Check if price is still falling — cancel if stop hit
            entry_signal = float(addon.get('entry_signal', trigger))
            if current < stop:
                addon['status'] = 'CANCELLED'
                addon['cancel_reason'] = f"Price £{current:.2f} below stop £{stop:.2f}"
                updated = True

                send_telegram(
                    f"❌ STAGE 2 CANCELLED\n\n"
                    f"{name}\n"
                    f"Price £{current:.2f} below stop £{stop:.2f}\n"
                    f"Stage 2 add-on will not execute.\n"
                    f"Stage 1 stop loss protecting initial position."
                )
                print(f"    ❌ Cancelled — price below stop level")

    if updated:
        atomic_write(ADDON_FILE, addons)

    executed = sum(1 for a in addons if a.get('status') == 'EXECUTED')
    cancelled= sum(1 for a in addons if a.get('status') == 'CANCELLED')
    print(f"\n✅ Add-on check complete | Pending:{len(pending)} | Executed:{executed} | Cancelled:{cancelled}")

if __name__ == '__main__':
    run()
