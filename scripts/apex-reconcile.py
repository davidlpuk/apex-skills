#!/usr/bin/env python3
"""
Position Reconciliation
Syncs apex-positions.json with T212 live portfolio.
Runs at start of morning scan and every stop monitor cycle.

Detects and resolves:
1. Positions closed in T212 but still tracked by Apex
2. Positions open in T212 but not tracked by Apex
3. Quantity mismatches between Apex and T212
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (atomic_write, safe_read, log_error, log_warning, log_info,
                            send_telegram, locked_read_modify_write, t212_request)
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m): print(f'INFO: {m}')

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
RECON_FILE     = '/home/ubuntu/.picoclaw/logs/apex-reconciliation.json'

def get_t212_portfolio():
    """Fetch live portfolio from T212 via centralised rate-limited caller."""
    try:
        data = t212_request('/equity/portfolio')
        if isinstance(data, list):
            return {p['ticker']: p for p in data}
        return {}
    except Exception as e:
        log_error(f"get_t212_portfolio failed: {e}")
        return {}

def load_apex_positions():
    """Load Apex position tracking."""
    try:
        return safe_read(POSITIONS_FILE, [])
    except Exception as e:
        log_error(f"load_apex_positions failed: {e}")
        return []

def get_exit_from_history(ticker, opened_date):
    """
    Query T212 order history to find the actual fill price for a closed position.
    Returns (exit_price, close_type) or (None, 'unknown') if not found.
    close_type: 'STOP_HIT' | 'TARGET_HIT' | 'MARKET_CLOSE' | 'unknown'
    """
    try:
        data = t212_request('/equity/history/orders?limit=100')
        if not isinstance(data, dict):
            return None, 'unknown'
        items = data.get('items', [])

        # Find the most recent FILLED SELL for this ticker after it was opened
        candidates = []
        for item in items:
            order = item.get('order', {})
            fill  = item.get('fill', {})
            if (order.get('ticker') == ticker
                    and order.get('status') == 'FILLED'
                    and order.get('side') == 'SELL'
                    and fill.get('price')):
                filled_at = fill.get('filledAt', '')
                if filled_at[:10] >= (opened_date or '2000-01-01'):
                    candidates.append({
                        'price':     float(fill['price']),
                        'type':      order.get('type', 'MARKET'),
                        'filled_at': filled_at,
                    })

        if not candidates:
            return None, 'unknown'

        # Take the most recent fill
        candidates.sort(key=lambda x: x['filled_at'], reverse=True)
        best = candidates[0]

        close_type = {
            'STOP':   'STOP_HIT',
            'LIMIT':  'TARGET_HIT',
            'MARKET': 'MARKET_CLOSE',
        }.get(best['type'], 'MARKET_CLOSE')

        return best['price'], close_type

    except Exception as e:
        log_error(f"get_exit_from_history failed for {ticker}: {e}")
        return None, 'unknown'


def log_closed_position(pos, reason):
    """
    Log a position closure to outcomes.
    Looks up the actual fill price from T212 order history so outcomes
    have real P&L and R data — not zeros.
    """
    try:
        ticker     = pos.get('t212_ticker', '?')
        entry      = float(pos.get('entry', 0))
        stop       = float(pos.get('stop', 0))
        qty        = float(pos.get('quantity', 0))
        target1    = float(pos.get('target1', 0))
        target2    = float(pos.get('target2', 0))
        opened     = pos.get('opened', '')
        rsi        = float(pos.get('rsi', 0))
        signal_type = pos.get('signal_type', 'UNKNOWN')
        sector     = pos.get('sector', 'unknown')

        # Look up actual exit price from T212 order history
        exit_price, close_type = get_exit_from_history(ticker, opened)

        # If history lookup failed, fall back to using current price from position
        if exit_price is None:
            exit_price = float(pos.get('current', pos.get('entry', 0)))
            close_type = reason

        # Override close_type with known reason if it's more specific
        if reason not in ('auto_reconciled_not_in_t212',):
            close_type = reason

        # Calculate P&L and R
        risk = entry - stop if entry > stop else 1.0
        pnl  = round(qty * (exit_price - entry), 2)
        r    = round((exit_price - entry) / risk, 2) if risk else 0

        # Classify result
        if exit_price >= target2 and target2 > 0:
            result = 'TARGET2_HIT'
        elif exit_price >= target1 and target1 > 0:
            result = 'TARGET1_HIT'
        elif exit_price <= stop and stop > 0:
            result = 'STOP_HIT'
        elif pnl > 0:
            result = 'MANUAL_WIN'
        elif pnl < 0:
            result = 'MANUAL_LOSS'
        else:
            result = 'BREAKEVEN'

        # RSI bucket
        rsi_bucket = ('below_35' if rsi < 35 else
                      'below_45' if rsi < 45 else
                      'mid_45_60' if rsi < 60 else
                      'above_60')

        # Day of week
        try:
            day = datetime.strptime(opened, '%Y-%m-%d').strftime('%A')
        except Exception:
            day = 'unknown'

        outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
        if not isinstance(outcomes, dict):
            outcomes = {'trades': []}
        trades = outcomes.get('trades', [])

        trade = {
            'id':           len(trades) + 1,
            'name':         pos.get('name', ticker),
            'ticker':       ticker,
            'entry':        entry,
            'exit':         exit_price,
            'stop':         stop,
            'target1':      target1,
            'target2':      target2,
            'pnl':          pnl,
            'r_achieved':   r,
            'result':       result,
            'outcome_type': close_type,
            'close_reason': reason,
            'score':        pos.get('score', 0),
            'rsi':          rsi,
            'rsi_bucket':   rsi_bucket,
            'macd':         pos.get('macd', 0),
            'signal_type':  signal_type,
            'sector':       sector,
            'opened':       opened,
            'closed':       datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'day_opened':   day,
            'mae_pct':      pos.get('mae_pct', 0),
            'mfe_pct':      pos.get('mfe_pct', 0),
            'auto_reconciled': True,
        }
        trades.append(trade)
        outcomes['trades'] = trades

        # Rebuild summary
        all_pnl  = [t.get('pnl', 0) for t in trades]
        winners  = [t for t in trades if t.get('pnl', 0) > 0]
        outcomes['summary'] = {
            'total_trades': len(trades),
            'winners':      len(winners),
            'losers':       len(trades) - len(winners),
            'win_rate':     round(len(winners) / len(trades) * 100, 1) if trades else 0,
            'total_pnl':    round(sum(all_pnl), 2),
            'avg_r':        round(sum(t.get('r_achieved', 0) for t in trades) / len(trades), 2) if trades else 0,
        }

        atomic_write(OUTCOMES_FILE, outcomes)
        log_info(f"Outcome logged: {pos.get('name')} exit={exit_price} pnl=£{pnl} r={r} result={result}")

    except Exception as e:
        log_error(f"log_closed_position failed: {e}")

def reconcile(silent=False):
    """
    Main reconciliation function.
    Returns: (changes_made, summary)
    """
    now = datetime.now(timezone.utc)

    if not silent:
        print(f"\n=== POSITION RECONCILIATION ===")
        print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    # Fetch both sources
    t212_positions  = get_t212_portfolio()
    apex_positions  = load_apex_positions()

    if not isinstance(apex_positions, list):
        apex_positions = []

    t212_tickers = set(t212_positions.keys())
    apex_tickers = set(p.get('t212_ticker', '') for p in apex_positions)

    changes      = []
    alerts       = []

    # ── 1. Positions in Apex but NOT in T212 ──────────────────────
    # These were closed externally — stop fired, manual close, expiry
    ghost_tickers = apex_tickers - t212_tickers - {''}
    for ticker in ghost_tickers:
        pos = next((p for p in apex_positions if p.get('t212_ticker') == ticker), {})
        name = pos.get('name', ticker)

        if not silent:
            print(f"  ⚠️  Ghost position: {name} ({ticker}) — in Apex but not T212")

        log_warning(f"Ghost position removed: {name} ({ticker})")
        log_closed_position(pos, 'auto_reconciled_not_in_t212')

        # Read back the just-logged outcome to include P&L in the alert
        try:
            _outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
            _last     = _outcomes.get('trades', [{}])[-1]
            _pnl      = _last.get('pnl', '?')
            _result   = _last.get('result', '?')
            _exit     = _last.get('exit', '?')
            alert_detail = f"Result: {_result} | Exit: {_exit} | P&L: £{_pnl}"
        except Exception:
            alert_detail = "P&L lookup failed — check apex-outcomes.json"

        changes.append(f"REMOVED: {name} — closed in T212")
        alerts.append(f"⚠️ {name} closed externally\n{alert_detail}")

    # Remove ghost positions from Apex tracking
    apex_positions = [p for p in apex_positions
                     if p.get('t212_ticker', '') not in ghost_tickers]

    # ── 2. Positions in T212 but NOT in Apex ──────────────────────
    # These were opened manually via T212 app or another system
    orphan_tickers = t212_tickers - apex_tickers
    for ticker in orphan_tickers:
        t212_pos = t212_positions[ticker]
        name     = t212_pos.get('ticker', ticker)
        qty      = float(t212_pos.get('quantity', 0))
        price    = float(t212_pos.get('currentPrice', 0))
        avg      = float(t212_pos.get('averagePrice', 0))

        if not silent:
            print(f"  ⚠️  Orphan position: {name} ({ticker}) — in T212 but not Apex")

        # Add to Apex tracking with minimal data
        new_pos = {
            't212_ticker':  ticker,
            'name':         name,
            'quantity':     qty,
            'entry':        round(avg, 2),
            'current':      round(price, 2),
            'stop':         round(avg * 0.94, 2),  # Default 6% stop
            'target1':      round(avg * 1.08, 2),
            'target2':      round(avg * 1.15, 2),
            'signal_type':  'MANUAL',
            'opened':       now.strftime('%Y-%m-%d'),
            'reconciled':   True,
            'reconcile_note': 'Added by reconciliation — opened outside Apex'
        }
        apex_positions.append(new_pos)
        log_warning(f"Orphan position added to tracking: {name} ({ticker})")
        changes.append(f"ADDED: {name} — found in T212 not tracked by Apex")
        alerts.append(f"⚠️ {name} found in T212 but not tracked — added with default stop")

    # ── 3. Quantity mismatches ──────────────────────────────────────
    for pos in apex_positions:
        ticker   = pos.get('t212_ticker', '')
        if ticker not in t212_positions:
            continue
        t212_qty = float(t212_positions[ticker].get('quantity', 0))
        apex_qty = float(pos.get('quantity', 0))

        if abs(t212_qty - apex_qty) > 0.01:
            name = pos.get('name', ticker)
            if not silent:
                print(f"  ⚠️  Qty mismatch: {name} — Apex:{apex_qty} T212:{t212_qty}")
            pos['quantity'] = t212_qty
            log_warning(f"Qty mismatch fixed: {name} Apex:{apex_qty} → T212:{t212_qty}")
            changes.append(f"QTY FIX: {name} {apex_qty} → {t212_qty}")

    # ── 4. Deduplicate — keep only one entry per ticker ───────────
    seen_tickers = {}
    deduped = []
    for pos in apex_positions:
        ticker = pos.get('t212_ticker', '')
        if ticker not in seen_tickers:
            seen_tickers[ticker] = True
            deduped.append(pos)
        else:
            name = pos.get('name', ticker)
            log_warning(f"Duplicate position removed: {name} ({ticker})")
            changes.append(f"DEDUP: {name} — duplicate entry removed")
    apex_positions = deduped

    # ── 5. Update current prices from T212 ─────────────────────────
    for pos in apex_positions:
        ticker = pos.get('t212_ticker', '')
        if ticker in t212_positions:
            pos['current'] = float(t212_positions[ticker].get('currentPrice', 0))
            pos['ppl']     = float(t212_positions[ticker].get('ppl', 0))

    # Save reconciled positions under file lock — prevents last-writer-wins races
    # with concurrent cron jobs (stop-monitor, watchdog) that also write positions.
    if changes or True:  # Always save to update current prices
        final_positions = apex_positions   # capture for closure
        locked_read_modify_write(POSITIONS_FILE, lambda _: final_positions, default=[])

    # Save reconciliation report
    report = {
        'timestamp':       now.strftime('%Y-%m-%d %H:%M UTC'),
        'apex_count':      len(apex_positions),
        't212_count':      len(t212_positions),
        'changes':         changes,
        'ghost_removed':   list(ghost_tickers),
        'orphans_added':   list(orphan_tickers),
        'status':          'CLEAN' if not changes else 'RECONCILED'
    }
    atomic_write(RECON_FILE, report)

    if not silent:
        if changes:
            print(f"\n  Changes made ({len(changes)}):")
            for c in changes:
                print(f"    → {c}")
        else:
            print(f"\n  ✅ Positions in sync — {len(apex_positions)} tracked / {len(t212_positions)} in T212")

    # Send Telegram alert if significant changes
    if alerts:
        msg = "🔄 POSITION RECONCILIATION\n\n" + "\n".join(alerts)
        msg += f"\n\nApex now tracking {len(apex_positions)} positions."
        send_telegram(msg)
        log_info(f"Reconciliation: {len(changes)} changes — {', '.join(changes[:3])}")

    return bool(changes), report

if __name__ == '__main__':
    changes_made, report = reconcile(silent=False)
    print(f"\n  Status: {report['status']}")
    print(f"  Apex: {report['apex_count']} | T212: {report['t212_count']}")
