#!/usr/bin/env python3
import yfinance as yf
import json
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import send_telegram, atomic_write, safe_read, log_error, t212_request
except ImportError:
    def send_telegram(m):
        print(f'TELEGRAM: {m}')
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None): return d or {}
    def log_error(m): print(f'ERROR: {m}')
    def t212_request(path, method='GET', data=None, timeout=10): return None

GAP_FLAG_FILE = '/home/ubuntu/.picoclaw/logs/apex-gap-emergency.json'

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

YAHOO_MAP = {
    "VUAGl_EQ":   "VUAG.L",
    "XOM_US_EQ":  "XOM",
    "V_US_EQ":    "V",
    "AAPL_US_EQ": "AAPL",
    "MSFT_US_EQ": "MSFT",
    "NVDA_US_EQ": "NVDA",
    "GOOGL_US_EQ":"GOOGL",
    "JPM_US_EQ":  "JPM",
    "GS_US_EQ":   "GS",
    "SHEL_EQ":    "SHEL.L",
    "HSBA_EQ":    "HSBA.L",
    "AZN_EQ":     "AZN.L",
}

def get_premarket_price(yahoo_ticker):
    # Try Alpaca for US stocks first
    ticker = yahoo_ticker.replace('.L','').replace('.PA','').replace('.AS','').replace('.DE','')
    us_tickers = {
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
        "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
        "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
        "WMT","PG","XOM","CVX","NVO"
    }
    if ticker in us_tickers:
        try:
            import sys
            sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
            import apex_alpaca as alpaca
            snap = alpaca.get_snapshot(ticker)
            if snap and snap['prev_close'] > 0:
                return snap['current'], snap['prev_close']
        except Exception as _e:
            log_error(f"Silent failure in apex-gap-protection.py: {_e}")

    # Fall back to yfinance
    try:
        t    = yf.Ticker(yahoo_ticker)
        info = t.info
        pre  = info.get('preMarketPrice') or info.get('regularMarketPrice') or 0
        prev = info.get('previousClose') or info.get('regularMarketPreviousClose') or 0
        return float(pre), float(prev)
    except:
        return None, None

def tighten_stop_order(pos, new_stop):
    """
    Attempt to modify the T212 stop order for a position to a tighter price.
    Deletes old stop and places a new GTC stop at new_stop.
    Returns True if successful, False on any error.
    """
    ticker    = pos.get('t212_ticker', '')
    stop_id   = pos.get('stop_order_id', '')
    quantity  = pos.get('quantity', 0)

    if not stop_id or not quantity:
        log_error(f"tighten_stop: missing stop_order_id or quantity for {ticker}")
        return False

    try:
        # Cancel old stop
        result = t212_request(f'/equity/orders/{stop_id}', method='DELETE', timeout=10)
        if result is None:
            log_error(f"tighten_stop: failed to cancel stop {stop_id} for {ticker}")
            return False

        # Place new tighter stop
        new_order = t212_request(
            '/equity/orders/stop',
            method='POST',
            data={
                'ticker':    ticker,
                'quantity':  quantity,
                'stopPrice': round(new_stop, 4),
                'timeValidity': 'GOOD_TILL_CANCEL',
            },
            timeout=10,
        )
        if new_order and new_order.get('id'):
            log_error(f"tighten_stop: new stop placed for {ticker} at {new_stop} (id {new_order['id']})")
            return new_order['id']
        else:
            log_error(f"tighten_stop: stop placement returned no id for {ticker}: {new_order}")
            return False

    except Exception as e:
        log_error(f"tighten_stop failed for {ticker}: {e}")
        return False


def write_gap_emergency_flags(gap_events):
    """
    Write gap emergency flags to disk so morning-scan.sh can gate trade execution.
    Any 'gap_through_stop' event blocks new entries for that instrument.
    """
    if not gap_events:
        atomic_write(GAP_FLAG_FILE, {'checked_at': datetime.now(timezone.utc).isoformat(),
                                     'events': [], 'block_new_entries': False})
        return

    block = any(e['severity'] == 'GAP_THROUGH_STOP' for e in gap_events)
    atomic_write(GAP_FLAG_FILE, {
        'checked_at':        datetime.now(timezone.utc).isoformat(),
        'events':            gap_events,
        'block_new_entries': block,
        'blocked_tickers':   [e['ticker'] for e in gap_events if e['severity'] == 'GAP_THROUGH_STOP'],
    })


def run():
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        print("No positions")
        return

    if not positions:
        print("No open positions")
        atomic_write(GAP_FLAG_FILE, {'checked_at': datetime.now(timezone.utc).isoformat(),
                                     'events': [], 'block_new_entries': False})
        return

    now        = datetime.now(timezone.utc)
    alerts     = []
    warnings   = []
    gap_events = []

    print(f"Gap protection check — {now.strftime('%H:%M UTC')}", flush=True)

    for pos in positions:
        ticker  = pos.get('t212_ticker', '')
        name    = pos.get('name', ticker)
        entry   = float(pos.get('entry', 0))
        stop    = float(pos.get('stop', 0))
        qty     = pos.get('quantity', 0)
        yahoo   = YAHOO_MAP.get(ticker, '')

        if not yahoo:
            continue

        pre_price, prev_close = get_premarket_price(yahoo)

        if not pre_price or not prev_close:
            continue

        # Gap calculation
        gap_pct = round((pre_price - prev_close) / prev_close * 100, 2)
        print(f"  {name}: prev close £{prev_close} → pre-market £{pre_price} ({gap_pct:+.1f}%)")

        # ── CRITICAL: Gap THROUGH stop ─────────────────────────────────
        # Pre-market price is already below the stop. The actual fill at open
        # will be far worse than the stop price (no guarantee of stop execution
        # at stop level when market opens below it). Flag for emergency action.
        if pre_price < stop:
            gap_through   = round(stop - pre_price, 2)
            est_loss_pct  = round((pre_price - entry) / entry * 100, 2)
            gap_events.append({'ticker': ticker, 'name': name,
                                'severity': 'GAP_THROUGH_STOP',
                                'gap_pct': gap_pct, 'pre_price': pre_price,
                                'stop': stop, 'estimated_loss_pct': est_loss_pct})
            alerts.append(
                f"🚨 GAP THROUGH STOP — {name}\n"
                f"Pre-mkt: £{pre_price} | Stop: £{stop}\n"
                f"Will open £{gap_through} BELOW stop\n"
                f"Estimated loss: {est_loss_pct:+.1f}% vs planned stop\n"
                f"ACTION: Market sell at open, or accept larger-than-planned loss\n"
                f"Reply: CLOSE {ticker}"
            )

        # ── SEVERE: Gap > 5% down but still above stop ─────────────────
        # High risk of stop being hit at open. Tighten stop to pre-market
        # price minus a small buffer (0.5%) to protect against continuation.
        elif gap_pct <= -5.0 and pre_price > stop:
            # Tighten stop to pre_market price - 0.5% buffer (floor = original stop)
            tighter_stop  = round(max(pre_price * 0.995, stop), 4)
            new_stop_id   = None

            if tighter_stop > stop and pos.get('stop_order_id'):
                new_stop_id = tighten_stop_order(pos, tighter_stop)
                if new_stop_id:
                    # Update positions file with new stop/stop_order_id
                    pos['stop']          = tighter_stop
                    pos['stop_order_id'] = new_stop_id
                    pos['stop_tightened_at'] = now.isoformat()
                    pos['stop_tightened_reason'] = f"Gap {gap_pct:+.1f}% pre-market"

            pct_above_stop = round((pre_price - tighter_stop) / tighter_stop * 100, 1)
            gap_events.append({'ticker': ticker, 'name': name,
                                'severity': 'LARGE_GAP_DOWN',
                                'gap_pct': gap_pct, 'pre_price': pre_price,
                                'original_stop': stop, 'new_stop': tighter_stop,
                                'stop_tightened': bool(new_stop_id)})
            stop_note = (f"✅ Stop auto-tightened to £{tighter_stop}"
                         if new_stop_id else f"⚠️ Stop tighten FAILED — manual check needed")
            alerts.append(
                f"🔴 LARGE GAP DOWN — {name} ({gap_pct:+.1f}%)\n"
                f"Pre-mkt: £{pre_price} | Original stop: £{stop}\n"
                f"{stop_note}\n"
                f"Still {pct_above_stop}% above new stop"
            )

        # ── WARNING: Gap 2–5% down, approaching stop ──────────────────
        elif pre_price < entry and gap_pct < -2:
            pct_above_stop = round((pre_price - stop) / stop * 100, 1)
            gap_events.append({'ticker': ticker, 'name': name,
                                'severity': 'GAP_DOWN_WARNING',
                                'gap_pct': gap_pct, 'pre_price': pre_price, 'stop': stop})
            warnings.append(
                f"⚠️ GAP DOWN — {name} ({gap_pct:+.1f}%)\n"
                f"Pre-mkt: £{pre_price} | Stop: £{stop}\n"
                f"Still {pct_above_stop}% above stop — monitor at open"
            )

        # ── GAP UP — opportunity to raise stop ────────────────────────
        elif gap_pct > 2:
            t1 = float(pos.get('target1', 0))
            pct_to_t1 = round((t1 - pre_price) / pre_price * 100, 1) if t1 > 0 else 0
            gap_events.append({'ticker': ticker, 'name': name,
                                'severity': 'GAP_UP',
                                'gap_pct': gap_pct, 'pre_price': pre_price})
            warnings.append(
                f"📈 GAP UP — {name} ({gap_pct:+.1f}%)\n"
                f"Pre-mkt: £{pre_price}"
                + (f" | {pct_to_t1:+.1f}% from T1" if t1 > 0 else "")
            )

    # Save updated positions if any stops were tightened
    if any(e.get('stop_tightened') for e in gap_events):
        try:
            atomic_write(POSITIONS_FILE, positions)
            print("  Positions file updated with tightened stops")
        except Exception as e:
            log_error(f"Failed to save tightened positions: {e}")

    write_gap_emergency_flags(gap_events)

    if alerts:
        for alert in alerts:
            send_telegram(alert)
            print(f"ALERT: {alert[:80]}")
    elif warnings:
        combined = f"🌅 PRE-MARKET GAP CHECK — {now.strftime('%d %b %H:%M UTC')}\n\n" + "\n\n".join(warnings)
        send_telegram(combined)
        print(f"WARNINGS sent: {len(warnings)}")
    else:
        print("No significant gaps — all positions safe at open")

if __name__ == '__main__':
    run()
