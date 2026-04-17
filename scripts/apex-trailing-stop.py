#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (atomic_write, safe_read, log_error, log_warning,
                            send_telegram, locked_read_modify_write, t212_request,
                            get_fx_rate)
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
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


_TRAJECTORY_INSIGHTS_FILE = '/home/ubuntu/.picoclaw/logs/apex-trajectory-insights.json'


def _sortino_partial_fraction(position=None):
    """
    Dynamic partial close fraction at T1, based on Sortino ratio.
    Sortino >= 2.0 → 33%  (system proven — let winners run)
    Sortino >= 1.0 → 50%  (default)
    Sortino < 0.5  → 66%  (unproven — bank more)
    Cold-start (< 5 trades): 50%

    Trajectory override (when apex-trajectory-insights.json available):
    - If t2_runner is recommended AND position velocity matches profile → 33%
    - Base Sortino calculation otherwise.
    """
    base = 0.5
    try:
        data = safe_read(SHARPE_FILE, {})
        if data.get('total_trades', 0) < 5:
            base = 0.5
        else:
            sortino = float(data.get('sortino_ratio', data.get('sharpe_ratio', 0)))
            if sortino >= 2.0:
                base = 0.33
            elif sortino >= 1.0:
                base = 0.50
            else:
                base = 0.66
    except Exception:
        base = 0.5

    # Trajectory insights override — reduce partial if position looks like a T2 runner
    if position:
        try:
            insights = safe_read(_TRAJECTORY_INSIGHTS_FILE, {})
            t2 = insights.get('t2_runner', {})
            if (insights.get('status') == 'OK'
                    and t2.get('recommended', False)
                    and t2.get('partial_fraction_override')):
                # Check if this position's current velocity exceeds the threshold
                vel_threshold = t2.get('velocity_threshold', 0.2)
                entry  = float(position.get('entry', 0))
                stop   = float(position.get('stop', entry * 0.94))
                curr   = float(position.get('current', entry))
                opened = position.get('opened', '')
                if entry and stop and entry != stop and opened:
                    from datetime import date
                    try:
                        days = max(1, (date.today() - date.fromisoformat(opened)).days)
                    except Exception:
                        days = 1
                    r_current = (curr - entry) / (entry - stop)
                    velocity = r_current / days
                    if velocity >= vel_threshold:
                        override_frac = float(t2['partial_fraction_override'])
                        print(f"  📊 Trajectory override: partial fraction {int(base*100)}% → {int(override_frac*100)}% "
                              f"(T2 runner profile, velocity={velocity:.2f} ≥ threshold={vel_threshold})")
                        return override_frac
        except Exception:
            pass  # Non-critical — fall back to Sortino-based fraction

    return base

def load_positions():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_positions(updated_positions):
    """Write positions atomically under file lock, merging our changes into
    the latest on-disk state so concurrent writers don't lose each other's work.

    Race condition guard: if the on-disk stop_order_id differs from the one we
    loaded (e.g. broker-watchdog placed a new stop between our load and save),
    keep the on-disk version — it is guaranteed to be newer than the stale ID
    we have in memory.  This prevents trailing-stop from overwriting a freshly
    placed watchdog stop with a stale order ID.
    """
    our_map = {p.get('t212_ticker'): p for p in updated_positions}
    def _merge(current):
        current = current or []
        merged = []
        seen = set()
        for p in current:
            t = p.get('t212_ticker')
            if t in our_map:
                updated = our_map[t]
                # If on-disk has a different (newer) stop_order_id, keep it.
                disk_sid = p.get('stop_order_id', '')
                our_sid  = updated.get('stop_order_id', '')
                if disk_sid and disk_sid != our_sid:
                    updated = dict(updated)  # copy to avoid mutating original
                    updated['stop_order_id'] = disk_sid
                merged.append(updated)
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

def _to_t212_price(price, currency):
    """
    Convert a signal price (stored in £/$ pounds) to the T212 API unit.
    GBX instruments (LSE pence-quoted) require ×100 before API submission.
    """
    if currency == 'GBX':
        return round(float(price) * 100, 2)
    return round(float(price), 4)


def place_stop_order(ticker, quantity, stop_price, currency='GBP'):
    """
    Place a GTC stop-sell order.
    currency: 'GBX' instruments require price in pence (×100) for T212 API.
    """
    neg_qty    = round(float(quantity) * -1, 8)
    t212_price = _to_t212_price(stop_price, currency)
    data = t212_request('/equity/orders/stop', method='POST', payload={
        "ticker":       ticker,
        "quantity":     neg_qty,
        "stopPrice":    t212_price,
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
        pnl_native = round(qty * (exit_price - entry), 2)
        r     = round((exit_price - entry) / risk, 2) if risk else 0

        # FX attribution — snapshot close rate and compute GBP impact
        currency    = pos.get('currency', '')
        fx_at_entry = float(pos.get('fx_at_entry', 1.0) or 1.0)
        try:
            fx_at_close = get_fx_rate(currency) if currency else 1.0
        except Exception:
            fx_at_close = fx_at_entry
        pnl_gbp        = round(pnl_native * fx_at_close, 2)
        fx_impact_gbp  = round(qty * (exit_price - entry) * (fx_at_close - fx_at_entry), 2)

        outcomes['trades'].append({
            'name':           pos.get('name', ''),
            'ticker':         pos.get('t212_ticker', ''),
            'entry':          entry,
            'exit':           exit_price,
            'pnl':            pnl_native,
            'pnl_gbp':        pnl_gbp,
            'fx_at_entry':    fx_at_entry,
            'fx_at_close':    fx_at_close,
            'fx_impact_gbp':  fx_impact_gbp,
            'currency':       currency,
            'r':              r,
            'qty':            qty,
            'type':           close_type,
            'opened':         pos.get('opened', ''),
            'closed':         datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'signal_type':    pos.get('signal_type', ''),
            'sector':         pos.get('sector', ''),
            'mae_pct':        pos.get('mae_pct', 0.0),
            'mfe_pct':        pos.get('mfe_pct', 0.0),
        })
        atomic_write(OUTCOMES_FILE, outcomes)
    except Exception as e:
        log_error(f"_log_closed_trade failed: {e}")

def get_live_prices():
    """
    Fetch live prices from T212 portfolio.
    Also syncs unrealised_pnl (from T212's ppl field) back into positions.json
    so circuit-breaker auto-close and other consumers always have a current value.
    """
    portfolio = t212_request('/equity/portfolio')
    if not isinstance(portfolio, list):
        return {}
    # Load stored entry prices to detect GBX (pence) vs GBP mismatch.
    # T212 returns currentPrice in pence for UK LSE instruments (e.g. 3UKSl_EQ).
    # If currentPrice is 20x+ the stored entry, divide by 100 to convert to GBP.
    positions = load_positions()
    entry_map = {p.get('t212_ticker'): float(p.get('entry', 0)) for p in positions}

    result  = {}
    pnl_map = {}   # ticker → unrealised_pnl from T212
    for p in portfolio:
        ticker = p['ticker']
        price  = float(p.get('currentPrice', 0))
        entry  = entry_map.get(ticker, 0)
        if entry > 0 and price > entry * 20:
            price = round(price / 100, 4)
        result[ticker] = price
        pnl_val = p.get('ppl')
        if pnl_val is not None:
            try:
                pnl_map[ticker] = round(float(pnl_val), 2)
            except (TypeError, ValueError):
                pass

    # Write unrealised_pnl back to positions.json under file lock.
    # This ensures circuit-breaker, watchdog, and any other consumer that
    # reads positions.json always has a current (not stale zero) P&L figure.
    if pnl_map:
        def _update_pnl(current_positions):
            for pos in (current_positions or []):
                t = pos.get('t212_ticker', '')
                if t in pnl_map:
                    pos['unrealised_pnl'] = pnl_map[t]
            return current_positions
        try:
            locked_read_modify_write(POSITIONS_FILE, _update_pnl, default=[])
        except Exception as _e:
            log_warning(f"get_live_prices: unrealised_pnl sync failed (non-blocking): {_e}")

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
        currency  = pos.get('currency', 'GBP')
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

        # ── Day-1 direction warning ───────────────────────────────────────
        # Trajectory data: 100% of positions showing negative R on end of day 1
        # closed as losers. Alert when this pattern is detected.
        # Only fires once (day1_warned flag), only during market hours.
        try:
            from datetime import date as _date
            _opened = pos.get('opened', '')
            _days_held = max(1, (_date.today() - _date.fromisoformat(_opened)).days) if _opened else 0
            _warned = pos.get('day1_warned', False)
            if _days_held == 1 and r < -0.25 and not _warned:
                pos['day1_warned'] = True
                updates.append(name)
                send_telegram(
                    f"⚠️ DAY-1 WARNING — {name}\n\n"
                    f"Position showing {r:.2f}R on day 1.\n\n"
                    f"📊 Historical insight: 100% of trades negative on day 1 "
                    f"closed as losses (15 trades observed).\n\n"
                    f"Current: £{current} | Entry: £{entry} | Stop: £{stop}\n"
                    f"R: {r:.2f} | Stop distance: {round((current-stop)/(entry-stop)*100,1)}% of risk remaining\n\n"
                    f"Consider: HOLD (stop still valid) or EARLY EXIT\n"
                    f"Reply CLOSE {ticker} to exit at market."
                )
                print(f"  ⚠️ Day-1 warning sent for {name} (R={r:.2f})")
        except Exception as _d1e:
            pass  # Non-critical

        # Check Target 2 hit — auto-close remaining position
        # Guard target2 > 0: an unset or zero target2 would make every price
        # satisfy current >= target2 and close the position immediately after entry.
        if not t2_hit and target2 > 0 and current >= target2:
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
            # Per-position fraction: trajectory insights may override if T2 runner profile
            pos_fraction  = _sortino_partial_fraction(pos)
            # LLM exit timing — may adjust fraction based on news/regime context
            try:
                import importlib.util as _ilu_et
                _spec_et = _ilu_et.spec_from_file_location(
                    'exit_timing', '/home/ubuntu/.picoclaw/scripts/apex-llm-exit-timing.py')
                _et_mod = _ilu_et.module_from_spec(_spec_et)
                _spec_et.loader.exec_module(_et_mod)
                pos_fraction, _et_reason = _et_mod.get_exit_fraction(pos, pos_fraction)
                if _et_reason not in ('flag_disabled', 'not_applicable', 'timing_error'):
                    print(f"  LLM exit timing: {int(pos_fraction*100)}% — {_et_reason}")
            except Exception as _et_e:
                print(f"  LLM exit timing skipped (non-blocking): {_et_e}")
            close_fraction = pos_fraction
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
            new_stop_id   = place_stop_order(ticker, remaining_qty, ratchet_stop, currency=currency)
            print(f"  Partial close: {partial_id} | Ratchet stop @ £{ratchet_stop}: {new_stop_id}")

            # Step 4: Update position record
            pos['quantity']            = remaining_qty
            pos['stop']                = ratchet_stop
            pos['trailing_stop_level'] = ratchet_stop
            pos['stop_order_id']       = str(new_stop_id) if new_stop_id else ''
            pos['t1_hit']              = True
            pos['breakeven_set']       = now
            pos['t1_partial_pnl']      = t1_pnl
            # If ratchet stop placement failed, flag as unprotected so the
            # broker watchdog auto-fix picks it up on its next cycle.
            if not new_stop_id:
                pos['unprotected']     = True
                pos['status']          = 'unprotected'
                log_error(f"T1 ratchet stop failed for {name} — flagged unprotected for watchdog")
            else:
                pos['unprotected']     = False
                pos['status']          = 'protected'
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
            _ratchet_min_move = max(0.25, float(entry) * 0.001)  # ≥25p or ≥0.1% of entry
            if new_ratchet > trailing_level + _ratchet_min_move:  # Only update if meaningfully higher
                # Cancel old stop, place ratcheted one
                if stop_id:
                    cancel_stop_order(stop_id)
                new_stop_id = place_stop_order(ticker, quantity, new_ratchet, currency=currency)
                if new_stop_id:
                    pos['trailing_stop_level'] = new_ratchet
                    pos['stop']                = new_ratchet
                    pos['stop_order_id']       = str(new_stop_id)
                    updates.append(name)
                    print(f"  📈 RATCHET UP — {name}: stop £{trailing_level} → £{new_ratchet} (price £{current})")

        # Time-based exit — don't let capital sit in dead trades.
        # Base hold limits (Trend: 15d, Contrarian: 20d, Inverse: 3d) are
        # adjusted downward by trajectory learner when it detects slow-grind
        # or stop-and-reverse patterns dominating a signal type.
        elif not t1_hit and not t2_hit:
            opened_str  = pos.get('opened', '')
            sig_type    = pos.get('signal_type', 'TREND')
            if opened_str:
                try:
                    opened_dt = datetime.strptime(opened_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    days_held = (datetime.now(timezone.utc) - opened_dt).days

                    if sig_type == 'INVERSE':
                        max_days = 3   # 3× leveraged ETFs decay daily — never hold long
                        # P&L floor: leveraged short ETFs compound decay rapidly.
                        # Close early if down > 8% regardless of days held —
                        # a 3× ETF losing 8% means the underlying moved ~2.7%
                        # against us; holding longer only compounds decay losses.
                        if entry > 0:
                            _inv_pnl_pct = (current - entry) / entry * 100
                            if _inv_pnl_pct < -8.0 and not pos.get('inverse_floor_fired'):
                                print(f"  📉 INVERSE P&L FLOOR — {name}: {_inv_pnl_pct:.1f}% loss exceeds -8% decay floor")
                                pos['inverse_floor_fired'] = True
                                updates.append(name)
                                send_telegram(
                                    f"📉 INVERSE ETF P&L FLOOR — {name}\n\n"
                                    f"Loss {_inv_pnl_pct:.1f}% exceeds -8% decay floor.\n"
                                    f"Leveraged ETF decay accelerates — closing early.\n"
                                    f"Entry £{entry} | Current £{current} | Days held: {days_held}"
                                )
                                order_id = close_position_at_market(ticker, quantity)
                                if order_id:
                                    if stop_id:
                                        cancel_stop_order(stop_id)
                                    pnl = round(quantity * (current - entry), 2)
                                    _log_closed_trade(pos, current, 'INVERSE_FLOOR')
                                    positions = [p for p in positions if p.get('t212_ticker') != ticker]
                                    updates.append(name)
                                    continue
                                else:
                                    send_telegram(f"⚠️ Inverse floor close FAILED for {name} — Reply CLOSE {ticker}")
                    elif sig_type == 'CONTRARIAN':
                        max_days = 20  # Mean reversion needs time to play out
                    else:
                        max_days = 15  # Trend trades — if it hasn't moved in 15 days, exit

                    # ── Trajectory learner override — close the open loop ────
                    # If trajectory insights show avg_days for this signal type
                    # is significantly shorter than the default max, cut earlier.
                    # Also applies early-cut rule once confirmed (r < -0.3R by day 2).
                    try:
                        _ti = safe_read('/home/ubuntu/.picoclaw/logs/apex-trajectory-insights.json', {})
                        if _ti.get('status') == 'OK':
                            _by_type = _ti.get('by_signal_type', {}).get(sig_type, {})
                            _avg_days = _by_type.get('avg_days', 0)
                            # If avg holding time for this type is < 70% of max, tighten hold limit
                            if _avg_days and _avg_days < max_days * 0.7:
                                _new_max = max(3, int(_avg_days * 1.5))
                                if _new_max < max_days:
                                    print(f"  📊 Trajectory override: {sig_type} avg_days={_avg_days:.1f} → max_days {max_days}→{_new_max}")
                                    max_days = _new_max

                            # Early-cut rule — if confirmed and we're in a loss at day 2
                            _ec = _ti.get('early_cut', {})
                            if (_ec.get('recommended') and
                                    days_held >= _ec.get('day', 2) and
                                    r < _ec.get('threshold_r', -0.3) and
                                    not pos.get('early_cut_fired')):
                                print(f"  ✂️ EARLY CUT — {name}: r={r} below {_ec['threshold_r']} on day {days_held} (recovery rate {_ec.get('recovery_rate','?')})")
                                pos['early_cut_fired'] = True
                                updates.append(name)
                                send_telegram(
                                    f"✂️ EARLY CUT — {name}\n\n"
                                    f"R={r:.2f} on day {days_held} (threshold: {_ec['threshold_r']}R)\n"
                                    f"Trajectory data: recovery rate only {_ec.get('recovery_rate',0)*100:.0f}% from this point.\n\n"
                                    f"Exiting at market to preserve capital."
                                )
                                order_id = close_position_at_market(ticker, quantity)
                                if order_id:
                                    if stop_id:
                                        cancel_stop_order(stop_id)
                                    _log_closed_trade(pos, current, 'EARLY_CUT')
                                    positions = [p for p in positions if p.get('t212_ticker') != ticker]
                                    updates.append(name)
                                continue
                    except Exception:
                        pass  # Non-critical — fall back to static max_days
                    # ── end trajectory override ─────────────────────────────

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

        # Stop hit — verify T212 stop order actually fired, force close if not.
        # Gap protection: on overnight gaps, T212 stop may not trigger (instrument
        # suspended, pre-market, order stale) leaving the position exposed to further
        # downside. We verify and force a market sell if the stop did not execute.
        elif current <= stop:
            gap_pct = round((stop - current) / stop * 100, 2) if stop > 0 else 0
            severity = "GAP" if gap_pct > 1.0 else "HIT"
            print(f"  🚨 STOP {severity} — {name} @ £{current} (stop £{stop}, {gap_pct}% through)")

            # Verify the T212 stop order status
            stop_triggered = False
            stop_status    = 'UNKNOWN'
            if stop_id:
                try:
                    order_info = t212_request(f'/equity/orders/{stop_id}')
                    if order_info:
                        stop_status = order_info.get('status', 'UNKNOWN')
                        filled_qty  = float(order_info.get('filledQuantity', 0))
                        stop_triggered = filled_qty > 0 or stop_status in ('FILLED', 'EXECUTED')
                    else:
                        # 404 → order gone, which means either filled or cancelled
                        stop_status = 'GONE'
                        stop_triggered = True  # Assume filled — will be confirmed by reconcile
                except Exception as _e:
                    log_warning(f"Gap protection: could not verify stop {stop_id}: {_e}")
                    stop_status = 'CHECK_FAILED'

            if stop_triggered:
                print(f"  ✅ T212 stop {stop_id} confirmed {stop_status}")
                send_telegram(
                    f"🚨 STOP HIT — {name}\n\n"
                    f"Price £{current} hit stop £{stop} ({gap_pct}% through)\n"
                    f"T212 stop order {stop_status}.\n"
                    f"Reconciler will update positions next cycle."
                )
            else:
                # Stop did NOT fire — force market sell immediately to cap loss
                log_error(f"GAP PROTECTION: T212 stop {stop_id} status={stop_status} but price £{current} ≤ stop £{stop} — forcing market sell")
                print(f"  ⛔ GAP PROTECTION ACTIVATED — T212 stop did not fire, market-selling {quantity} {ticker}")

                # Cancel the stale stop first so it doesn't double-fill
                if stop_id:
                    try:
                        cancel_stop_order(stop_id)
                    except Exception as _e:
                        log_warning(f"Gap protection: cancel_stop_order failed: {_e}")

                order_id = close_position_at_market(ticker, quantity)
                if order_id:
                    pnl = round(quantity * (current - entry), 2)
                    _log_closed_trade(pos, current, 'GAP_PROTECTION')
                    positions = [p for p in positions if p.get('t212_ticker') != ticker]
                    updates.append(name)
                    send_telegram(
                        f"⛔ GAP PROTECTION FIRED — {name}\n\n"
                        f"Price £{current} gapped {gap_pct}% through stop £{stop}\n"
                        f"T212 stop order status: {stop_status} (did not fire)\n\n"
                        f"Forced market sell executed: order {order_id}\n"
                        f"Entry £{entry} → Exit £{current}\n"
                        f"P&L: £{pnl:+.2f} | R: {r}\n\n"
                        f"Position closed to cap loss at current market."
                    )
                else:
                    send_telegram(
                        f"❌ GAP PROTECTION FAILED — {name}\n\n"
                        f"Price £{current} below stop £{stop} ({gap_pct}% gap)\n"
                        f"T212 stop did not fire AND market sell failed.\n\n"
                        f"URGENT: Reply CLOSE {ticker} to exit manually."
                    )

    # Save updated positions
    if updates:
        save_positions(positions)
        print(f"Updated positions: {', '.join(updates)}")
    else:
        print("No trailing stop updates needed")

if __name__ == '__main__':
    run()
