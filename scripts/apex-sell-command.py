#!/usr/bin/env python3
"""
apex-sell-command.py — Natural-language sell command processor for Telegram.

Usage:
  python3 apex-sell-command.py --text "Confirm Apple sell" [--confirmed]
  python3 apex-sell-command.py --text "Sell AAPL" [--confirmed]

If --confirmed is passed (or "confirm" appears in the text), executes immediately.
Otherwise prints a confirmation prompt and exits 2 (caller should send the prompt).

Exit codes:
  0 — success (order placed, outcomes updated, tax import triggered)
  1 — error (API failure, position not found, etc.)
  2 — needs confirmation (outputs prompt for user to send back)

Output: single JSON line on stdout with {status, message, ticker, name, qty}
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_utils import (
    t212_request, send_telegram, locked_read_modify_write,
    safe_read, atomic_write, log_error, LOG_DIR,
)

POSITIONS_FILE = os.path.join(LOG_DIR, 'apex-positions.json')
OUTCOMES_FILE  = os.path.join(LOG_DIR, 'apex-outcomes.json')


# ── Fuzzy position resolver ────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def resolve_position(query: str) -> list[dict]:
    """
    Find open position(s) matching query (ticker or company name, fuzzy).
    Returns list of matching position dicts from apex-positions.json.
    """
    positions = safe_read(POSITIONS_FILE, [])
    if not positions:
        return []

    q = _normalise(query)
    matches = []

    for pos in positions:
        ticker = _normalise(pos.get('t212_ticker', ''))
        name   = _normalise(pos.get('name', ''))
        # Exact ticker match (e.g. AAPL_US_EQ or AAPL)
        if q == ticker or q == ticker.replace('_us_eq', '').replace('_eq', ''):
            matches.append(pos)
        # Substring name match (e.g. "apple" matches "Apple Inc")
        elif q and q in name:
            matches.append(pos)

    return matches


def extract_query(text: str) -> str:
    """
    Strip command keywords (SELL, CONFIRM, CLOSE, EXIT, OF, THE, A)
    and return the stock identifier portion.
    """
    text = text.strip()
    # Remove leading/trailing punctuation
    text = re.sub(r'[^\w\s/]', ' ', text)
    # Remove known command words (case-insensitive)
    stop_words = r'\b(confirm|sell|close|exit|of|the|a|please|now)\b'
    text = re.sub(stop_words, ' ', text, flags=re.IGNORECASE)
    # Collapse whitespace
    return re.sub(r'\s+', ' ', text).strip()


# ── Outcome recorder ──────────────────────────────────────────────────────────

def _record_outcome(pos: dict, exit_price: float, order_id: str) -> None:
    """Append a TELEGRAM_SELL entry to apex-outcomes.json and update summary."""
    today = date.today().isoformat()
    entry_price = float(pos.get('entry', 0))
    qty         = float(pos.get('quantity', 0))
    pnl         = round((exit_price - entry_price) * qty, 4)
    r_risk      = float(pos.get('entry', 0)) - float(pos.get('stop', pos.get('entry', 0)))
    r_achieved  = round(pnl / (r_risk * qty), 2) if r_risk and qty else 0.0

    new_trade = {
        'ticker':       pos.get('t212_ticker'),
        'name':         pos.get('name', pos.get('t212_ticker')),
        'opened':       pos.get('opened', today),
        'closed':       today,
        'entry':        entry_price,
        'exit':         exit_price,
        'stop':         float(pos.get('stop', 0)),
        'target1':      float(pos.get('target1', 0)),
        'target2':      float(pos.get('target2', 0)),
        'quantity':     qty,
        'pnl':          pnl,
        'r_achieved':   r_achieved,
        'result':       'WIN' if pnl > 0 else ('LOSS' if pnl < 0 else 'BREAKEVEN'),
        'outcome_type': 'TELEGRAM_SELL',
        'close_reason': 'telegram_command',
        'score':        float(pos.get('score', 0)),
        'rsi':          float(pos.get('rsi', 0)),
        'macd':         float(pos.get('macd', 0)),
        'sector':       pos.get('sector', 'unknown'),
        'day_opened':   pos.get('day_opened', ''),
        'order_id':     order_id,
        'currency':     pos.get('currency', 'GBP'),
    }

    def _update(data):
        if not isinstance(data, dict):
            data = {}
        trades = data.get('trades', [])
        # Assign sequential id
        max_id = max((t.get('id', 0) for t in trades), default=0)
        new_trade['id'] = max_id + 1
        trades.append(new_trade)
        data['trades'] = trades
        # Recompute summary
        wins   = [t for t in trades if t.get('pnl', 0) > 0]
        losses = [t for t in trades if t.get('pnl', 0) < 0]
        total_pnl = sum(t.get('pnl', 0) for t in trades)
        data['summary'] = {
            'total_trades': len(trades),
            'winners':      len(wins),
            'losers':       len(losses),
            'win_rate':     round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'total_pnl':    round(total_pnl, 4),
            'avg_r':        round(sum(t.get('r_achieved', 0) for t in trades) / len(trades), 2) if trades else 0,
        }
        return data

    locked_read_modify_write(OUTCOMES_FILE, _update, {})


def _remove_from_positions(ticker: str) -> None:
    """Remove closed position from apex-positions.json."""
    def _update(data):
        if not isinstance(data, list):
            return data
        return [p for p in data if p.get('t212_ticker', '').upper() != ticker.upper()]
    locked_read_modify_write(POSITIONS_FILE, _update, [])


def _trigger_tax_import() -> bool:
    """POST to local tax import endpoint to sync the new trade into the tax DB."""
    try:
        import urllib.request
        req = urllib.request.Request(
            'http://localhost:7777/tax/import/apex',
            data=b'',
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 302)
    except Exception as e:
        log_error(f"apex-sell-command: tax import trigger failed: {e}")
        return False


# ── Main execution ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--text',      required=True, help='Raw message text from Telegram')
    parser.add_argument('--confirmed', type=int, default=0, help='1 = execute immediately')
    args = parser.parse_args()

    raw_text   = args.text
    confirmed  = bool(args.confirmed) or bool(re.search(r'\bconfirm\b', raw_text, re.IGNORECASE))

    query = extract_query(raw_text)
    if not query:
        print(json.dumps({'status': 'error', 'message': 'No stock identified in message.'}))
        sys.exit(1)

    matches = resolve_position(query)

    if not matches:
        positions = safe_read(POSITIONS_FILE, [])
        open_list = ', '.join(
            f"{p.get('name','?')} ({p.get('t212_ticker','?')})"
            for p in positions[:8]
        ) or 'none'
        print(json.dumps({
            'status':  'not_found',
            'message': f"No open position found for '{query}'.\nOpen positions: {open_list}",
        }))
        sys.exit(1)

    if len(matches) > 1:
        options = '\n'.join(
            f"  • {p.get('name')} ({p.get('t212_ticker')})"
            for p in matches
        )
        print(json.dumps({
            'status':  'ambiguous',
            'message': f"Multiple positions match '{query}':\n{options}\n\nReply: CONFIRM SELL <exact_ticker>",
        }))
        sys.exit(1)

    pos    = matches[0]
    ticker = pos['t212_ticker']
    name   = pos.get('name', ticker)
    qty    = float(pos.get('quantity', 0))

    if not confirmed:
        # Return a confirmation prompt — caller sends this to Telegram
        print(json.dumps({
            'status':  'needs_confirm',
            'message': (
                f"⚠️ About to sell {qty:g}x {name} ({ticker}) at market.\n"
                f"Entry: {pos.get('entry', '?')} | Stop: {pos.get('stop', '?')}\n\n"
                f"Reply: CONFIRM SELL {ticker}"
            ),
            'ticker': ticker,
            'name':   name,
            'qty':    qty,
        }))
        sys.exit(2)

    # ── Execute the sell ──
    neg_qty = -abs(qty)
    result = t212_request(
        '/equity/orders/market',
        method='POST',
        payload={'ticker': ticker, 'quantity': neg_qty},
    )

    if result is None:
        msg = f"❌ Market sell failed for {name} ({ticker}) — T212 API returned no response."
        print(json.dumps({'status': 'error', 'message': msg}))
        sys.exit(1)

    order_id  = str(result.get('id', result.get('orderId', 'UNKNOWN')))
    # T212 market orders fill immediately; filledPrice may not be in response
    # Use fillPrice if available, else fall back to last known price or entry
    fill_price = (
        result.get('filledPrice')
        or result.get('fillPrice')
        or pos.get('current')
        or pos.get('entry', 0)
    )
    fill_price = float(fill_price)

    entry_price = float(pos.get('entry', 0))
    pnl         = round((fill_price - entry_price) * qty, 2)
    pnl_str     = f"+£{pnl:.2f}" if pnl >= 0 else f"-£{abs(pnl):.2f}"

    # Record in outcomes.json
    _record_outcome(pos, fill_price, order_id)

    # Remove from positions
    _remove_from_positions(ticker)

    # Trigger tax import
    tax_ok = _trigger_tax_import()
    tax_note = ' | Tax DB updated' if tax_ok else ' | ⚠️ Tax sync pending'

    msg = (
        f"✅ Sold {qty:g}x {name}\n"
        f"Order: {order_id}\n"
        f"Fill: ~{fill_price:.4g} | Entry: {entry_price:.4g}\n"
        f"P&L: {pnl_str}{tax_note}"
    )

    print(json.dumps({
        'status':      'executed',
        'message':     msg,
        'ticker':      ticker,
        'name':        name,
        'qty':         qty,
        'order_id':    order_id,
        'fill_price':  fill_price,
        'pnl':         pnl,
    }))
    sys.exit(0)


if __name__ == '__main__':
    main()
