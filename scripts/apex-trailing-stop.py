#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (atomic_write, safe_read, log_error, send_telegram,
                            locked_read_modify_write, t212_request)
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m): print(f'ERROR: {m}')
    def locked_read_modify_write(p, fn, default=None):
        import json as _j
        try:
            with open(p) as f: data = _j.load(f)
        except Exception: data = default
        result = fn(data)
        with open(p, 'w') as f: _j.dump(result, f, indent=2)
        return True
    def t212_request(path, method='GET', payload=None, **kw):
        return None

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
TRAILING_FILE  = '/home/ubuntu/.picoclaw/logs/apex-trailing-stops.json'
SHARPE_FILE    = '/home/ubuntu/.picoclaw/logs/apex-sharpe.json'
LOG            = '/home/ubuntu/.picoclaw/logs/apex-orders.log'


def _sortino_partial_fraction():
    """
    Dynamic partial close fraction at T1, based on Sortino ratio.
    Sortino >= 2.0 → 33%  (system proven — let winners run)
    Sortino >= 1.0 → 50%  (default)
    Sortino < 0.5  → 66%  (unproven — bank more)
    Cold-start (< 5 trades): 50%
    """
    try:
        data = safe_read(SHARPE_FILE, {})
        if data.get('total_trades', 0) < 5:
            return 0.5
        sortino = float(data.get('sortino_ratio', data.get('sharpe_ratio', 0)))
        if sortino >= 2.0:
            return 0.33
        elif sortino >= 1.0:
            return 0.50
        else:
            return 0.66
    except Exception:
        return 0.5

def load_positions():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_positions(updated_positions):
    """Write positions atomically under file lock, merging our changes into
    the latest on-disk state so concurrent writers don't lose each other's work."""
    our_map = {p.get('t212_ticker'): p for p in updated_positions}
    def _merge(current):
        current = current or []
        merged = []
        seen = set()
        for p in current:
            t = p.get('t212_ticker')
            if t in our_map:
                merged.append(our_map[t])   # use our updated version
            else:
                merged.append(p)            # preserve untouched positions
            seen.add(t)
        # Positions we added (shouldn't normally happen in trailing-stop)
        for p in updated_positions:
            if p.get('t212_ticker') not in seen:
                merged.append(p)
        return merged
    locked_read_modify_write(POSITIONS_FILE, _merge, default=[])

def load_trailing():
    try:
        with open(TRAILING_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_trailing(data):
    atomic_write(TRAILING_FILE, data)

def cancel_stop_order(stop_order_id):
    result = t212_request(f'/equity/orders/{stop_order_id}', method='DELETE')
    return result is not None

def place_stop_order(ticker, quantity, stop_price):
    neg_qty = round(float(quantity) * -1, 8)
    data = t212_request('/equity/orders/stop', method='POST', payload={
        "ticker":       ticker,
        "quantity":     neg_qty,
        "stopPrice":    round(float(stop_price), 4),
        "timeValidity": "GOOD_TILL_CANCEL",
    })
    if data is None:
        return None
    return data.get('id')

def _market_sell(ticker, quantity):
    """Place a market sell order for the given quantity (positive number)."""
    neg_qty = round(float(quantity) * -1, 8)
    data = t212_request('/equity/orders/market', method='POST', payload={
        "ticker":   ticker,
        "quantity": neg_qty,
    })
    if data is None:
        return None
    return data.get('id')

def partial_close_at_market(ticker, quantity, fraction=0.5):
    """Market sell a fraction of a position (default 50% at T1)."""
    sell_qty = round(float(quantity) * fraction, 8)
    return _market_sell(ticker, sell_qty)

def close_position_at_market(ticker, quantity):
    """Market sell the full remaining position at T2."""
    return _market_sell(ticker, float(quantity))

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'

def _log_closed_trade(pos, exit_price, close_type):
    """Append a closed trade record to apex-outcomes.json."""
    try:
        outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
        if not isinstance(outcomes, dict):
            outcomes = {'trades': []}
        entry = float(pos.get('entry', 0))
        stop  = float(pos.get('stop', 0))
        qty   = float(pos.get('quantity', 0))
        risk  = entry - stop if entry > stop else 1
        pnl   = round(qty * (exit_price - entry), 2)
        r     = round((exit_price - entry) / risk, 2) if risk else 0
        outcomes['trades'].append({
            'name':        pos.get('name', ''),
            'ticker':      pos.get('t212_ticker', ''),
            'entry':       entry,
            'exit':        exit_price,
            'pnl':         pnl,
            'r':           r,
            'qty':         qty,
            'type':        close_type,
            'opened':      pos.get('opened', ''),
            'closed':      datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'signal_type': pos.get('signal_type', ''),
            'sector':      pos.get('sector', ''),
            'mae_pct':     pos.get('mae_pct', 0.0),
            'mfe_pct':     pos.get('mfe_pct', 0.0),
        })
        atomic_write(OUTCOMES_FILE, outcomes)
    except Exception as e:
        log_error(f"_log_closed_trade failed: {e}")

def get_live_prices():
    portfolio = t212_request('/equity/portfolio')
    if not isinstance(portfolio, list):
        return {}
    # Load stored entry prices to detect GBX (pence) vs GBP mismatch.
    # T212 returns currentPrice in pence for UK LSE instruments (e.g. 3UKSl_EQ).
    # If currentPrice is 20x+ the stored entry, divide by 100 to convert to GBP.
    positions = load_positions()
    entry_map = {p.get('t212_ticker'): float(p.get('entry', 0)) for p in positions}
    result = {}
    for p in portfolio:
        ticker = p['ticker']
        price  = float(p.get('currentPrice', 0))
        entry  = entry_map.get(ticker, 0)
        if entry > 0 and price > entry * 20:
            price = round(price / 100, 4)
        result[ticker] = price
    return result

def run():
    positions       = load_positions()
    trailing        = load_trailing()
    prices          = get_live_prices()
    now             = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    close_fraction  = _sortino_partial_fraction()

    print(f"Sortino partial-close fraction: {int(close_fraction*100)}%")

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

        # Track MAE (max adverse excursion) and MFE (max favourable excursion).
        # Updated every 30-min cycle so after 50+ trades we can tell if stops/targets are right.
        if entry > 0:
            excursion_pct = round((current - entry) / entry * 100, 2)
            prev_mae = pos.get('mae_pct', 0.0)  # most negative = worst drawdown
            prev_mfe = pos.get('mfe_pct', 0.0)  # most positive = peak unrealised gain
            if excursion_pct < prev_mae:
                pos['mae_pct'] = excursion_pct
                updates.append(name)
            if excursion_pct > prev_mfe:
                pos['mfe_pct'] = excursion_pct
                updates.append(name)

        print(f"{name}: £{current} | Entry £{entry} | Stop £{stop} | T1 £{target1} | R:{r} | MAE:{pos.get('mae_pct',0)}% MFE:{pos.get('mfe_pct',0)}%")

        # Check Target 2 hit — auto-close remaining position
        if not t2_hit and current >= target2:
            print(f"  🎯 TARGET 2 HIT — AUTO-CLOSING {name}")
            order_id = close_position_at_market(ticker, quantity)
            if order_id:
                pos['t2_hit']    = True
                pos['t2_closed'] = True
                updates.append(name)
                # Cancel stop — position is now closed
                if stop_id:
                    cancel_stop_order(stop_id)
                # Log to outcomes
                _log_closed_trade(pos, current, 'T2_AUTO')
                # Remove position from tracking
                positions = [p for p in positions if p.get('t212_ticker') != ticker]
                send_telegram(
                    f"✅ TARGET 2 HIT — AUTO-CLOSED\n\n"
                    f"{name}\n"
                    f"Closed £{current} | T2 was £{target2}\n"
                    f"R achieved: {r}\n"
                    f"Order ID: {order_id}"
                )
            else:
                pos['t2_hit'] = True
                updates.append(name)
                send_telegram(
                    f"⚠️ T2 HIT but auto-close FAILED\n\n"
                    f"{name} @ £{current}\n"
                    f"Reply: CLOSE {ticker}"
                )

        # Check Target 1 hit — adaptive partial close + ratchet stop on remainder
        elif not t1_hit and current >= target1:
            sell_qty      = round(quantity * close_fraction, 8)
            remaining_qty = round(quantity - sell_qty, 8)
            t1_pnl        = round(sell_qty * (current - entry), 2)
            pct_sold      = int(close_fraction * 100)

            print(f"  🎯 TARGET 1 HIT — selling {pct_sold}% ({sell_qty} shares), ratchet stop for {name}")

            # Step 1: Sell adaptive fraction at market
            partial_id = partial_close_at_market(ticker, quantity, fraction=close_fraction)

            # Step 2: Cancel existing stop
            if stop_id:
                cancel_stop_order(stop_id)

            # Step 3: Ratchet stop — lock in 50% of unrealised gain above entry
            # e.g. entry=100, current=112 → ratchet_stop = 100 + 0.5*(112-100) = 106
            # Minimum: entry (breakeven), in case current is only just above T1
            ratchet_stop  = round(entry + 0.5 * (current - entry), 4)
            ratchet_stop  = max(ratchet_stop, entry)  # Never below breakeven
            new_stop_id   = place_stop_order(ticker, remaining_qty, ratchet_stop)
            print(f"  Partial close: {partial_id} | Ratchet stop @ £{ratchet_stop}: {new_stop_id}")

            # Step 4: Update position record
            pos['quantity']            = remaining_qty
            pos['stop']                = ratchet_stop
            pos['trailing_stop_level'] = ratchet_stop
            pos['stop_order_id']       = str(new_stop_id) if new_stop_id else ''
            pos['t1_hit']              = True
            pos['breakeven_set']       = now
            pos['t1_partial_pnl']      = t1_pnl
            updates.append(name)

            _log_closed_trade(
                {**pos, 'quantity': sell_qty},
                current,
                'T1_PARTIAL'
            )

            if partial_id and new_stop_id:
                send_telegram(
                    f"🎯 TARGET 1 HIT — {name}\n\n"
                    f"Price £{current} | T1 was £{target1}\n"
                    f"R achieved: {r}\n\n"
                    f"✅ Sold {pct_sold}% ({sell_qty} shares) — banked £{t1_pnl:+.2f}\n"
                    f"✅ Ratchet stop: £{ratchet_stop} (locks in 50% of gain)\n\n"
                    f"Remaining {remaining_qty} shares riding to T2: £{target2}"
                )
            else:
                send_telegram(
                    f"🎯 TARGET 1 HIT — {name}\n\n"
                    f"Price £{current} | T1 was £{target1}\n\n"
                    f"{'✅ Partial sell placed' if partial_id else '⚠️ Partial sell FAILED'}\n"
                    f"{'✅ Ratchet stop @ £' + str(ratchet_stop) if new_stop_id else '⚠️ Ratchet stop FAILED — set manually'}\n\n"
                    f"Remaining qty: {remaining_qty} shares"
                )

        # Trailing ratchet for positions already past T1 — ratchet up as price rises
        elif t1_hit and not t2_hit and current > entry:
            trailing_level = float(pos.get('trailing_stop_level', pos.get('stop', entry)))
            new_ratchet    = round(entry + 0.5 * (current - entry), 4)
            if new_ratchet > trailing_level + 0.01:  # Only update if meaningfully higher
                # Cancel old stop, place ratcheted one
                if stop_id:
                    cancel_stop_order(stop_id)
                new_stop_id = place_stop_order(ticker, quantity, new_ratchet)
                if new_stop_id:
                    pos['trailing_stop_level'] = new_ratchet
                    pos['stop']                = new_ratchet
                    pos['stop_order_id']       = str(new_stop_id)
                    updates.append(name)
                    print(f"  📈 RATCHET UP — {name}: stop £{trailing_level} → £{new_ratchet} (price £{current})")

        # Time-based exit — don't let capital sit in dead trades.
        # Trend: 15 trading days max. Contrarian: 20 days. Inverse ETFs: 3 days (leveraged decay).
        elif not t1_hit and not t2_hit:
            opened_str  = pos.get('opened', '')
            sig_type    = pos.get('signal_type', 'TREND')
            if opened_str:
                try:
                    opened_dt = datetime.strptime(opened_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    days_held = (datetime.now(timezone.utc) - opened_dt).days

                    if sig_type == 'INVERSE':
                        max_days = 3   # 3× leveraged ETFs decay daily — never hold long
                    elif sig_type == 'CONTRARIAN':
                        max_days = 20  # Mean reversion needs time to play out
                    else:
                        max_days = 15  # Trend trades — if it hasn't moved in 15 days, exit

                    if days_held >= max_days:
                        print(f"  ⏰ TIME STOP — {name} held {days_held} days (max {max_days} for {sig_type})")
                        order_id = close_position_at_market(ticker, quantity)
                        if order_id:
                            if stop_id:
                                cancel_stop_order(stop_id)
                            _log_closed_trade(pos, current, f'TIME_STOP_{days_held}d')
                            positions = [p for p in positions if p.get('t212_ticker') != ticker]
                            updates.append(name)
                            pnl = round(quantity * (current - entry), 2)
                            send_telegram(
                                f"⏰ TIME STOP — {name}\n\n"
                                f"Held {days_held} days (max {max_days} for {sig_type})\n"
                                f"Entry £{entry} → Exit £{current}\n"
                                f"P&L: £{pnl:+.2f} | R: {r}\n\n"
                                f"Capital freed for better setups."
                            )
                            continue  # Position removed, skip remaining checks
                        else:
                            send_telegram(
                                f"⚠️ TIME STOP failed for {name} — held {days_held}d\n"
                                f"Reply: CLOSE {ticker}"
                            )
                except Exception as _e:
                    log_error(f"Time stop check failed for {name}: {_e}")

        # Warn if approaching stop
        if current <= stop * 1.02 and current > stop:
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
