#!/usr/bin/env python3
"""
apex-veto-evaluator.py
Evaluates whether past agent vetoes were correct decisions by checking
price 10 calendar days after the veto date. Writes veto_correct T/F/None
back to apex-agent-actions.json.

Correct veto (BUY): price 10 days later is LOWER than veto-day price.
Incorrect veto (BUY): price 10 days later is HIGHER by >3%.
"""
import sys
import os
import json
import argparse
from datetime import datetime, timezone, timedelta

LOG_DIR    = '/home/ubuntu/.picoclaw/logs'
SCRIPTS_DIR = '/home/ubuntu/.picoclaw/scripts'
sys.path.insert(0, SCRIPTS_DIR)

from apex_utils import safe_read, log_info, log_warning, locked_read_modify_write

ACTIONS_FILE  = os.path.join(LOG_DIR, 'apex-agent-actions.json')
TICKER_MAP    = os.path.join(SCRIPTS_DIR, 'apex-ticker-map.json')

# LSE suffixes that map to Yahoo .L symbol
_LSE_SUFFIXES = ('l_EQ', 'm_EQ', 's_EQ', 'd_EQ')


def _t212_to_yahoo(t212_ticker: str, ticker_map: dict) -> str | None:
    """Convert a T212 ticker to a Yahoo Finance symbol."""
    # Search ticker map by t212 value
    for key, val in ticker_map.items():
        if val.get('t212') == t212_ticker:
            # Use yahoo_key override if present, else derive from t212
            yahoo_key = val.get('yahoo_key') or key
            currency  = val.get('currency', 'USD')
            if currency in ('GBP', 'GBX') or any(t212_ticker.endswith(s) for s in _LSE_SUFFIXES):
                return f"{yahoo_key}.L"
            return yahoo_key

    # Fallback: derive from raw ticker
    if any(t212_ticker.endswith(s) for s in _LSE_SUFFIXES):
        base = t212_ticker
        for sfx in _LSE_SUFFIXES:
            if base.endswith(sfx):
                base = base[:-len(sfx)]
                break
        return f"{base}.L"
    if t212_ticker.endswith('_US_EQ'):
        return t212_ticker.replace('_US_EQ', '')
    if t212_ticker.endswith('_EQ'):
        return t212_ticker.replace('_EQ', '')
    return t212_ticker


def _get_price_on_date(yahoo_symbol: str, target_date: datetime) -> float | None:
    """Fetch closing price on or near target_date using yfinance."""
    try:
        import yfinance as yf
        start = target_date - timedelta(days=3)
        end   = target_date + timedelta(days=3)
        ticker_obj = yf.Ticker(yahoo_symbol)
        hist = ticker_obj.history(start=start.strftime('%Y-%m-%d'),
                                   end=end.strftime('%Y-%m-%d'))
        if hist.empty:
            return None
        # Pick the row closest to target_date
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo is not None else hist.index
        target_naive = target_date.replace(tzinfo=None)
        closest = min(hist.index, key=lambda d: abs((d - target_naive).days))
        return float(hist.loc[closest, 'Close'])
    except Exception as e:
        log_warning(f"yfinance fetch failed for {yahoo_symbol}: {e}")
        return None


def evaluate_vetoes(dry_run: bool = False) -> dict:
    """Evaluate all unevaluated vetoes in apex-agent-actions.json."""
    ticker_map = safe_read(TICKER_MAP, {})
    data = safe_read(ACTIONS_FILE, {})
    if not isinstance(data, dict):
        data = {'actions': data if isinstance(data, list) else []}
    actions = data.get('actions', [])

    vetoes = [a for a in actions
              if a.get('action_type') == 'signal_vetoed'
              and a.get('veto_correct') is None
              and 'veto_correct' not in a]

    log_info(f"apex-veto-evaluator: found {len(vetoes)} unevaluated vetoes")

    results = {'evaluated': 0, 'correct': 0, 'incorrect': 0, 'insufficient_data': 0}

    for action in vetoes:
        raw_ticker = action.get('ticker', '')
        ts_str     = action.get('timestamp', '')

        # Parse veto timestamp
        try:
            veto_dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except Exception:
            log_warning(f"Cannot parse timestamp for veto: {ts_str}")
            continue

        # Only evaluate vetoes where 10 days have elapsed
        now_utc = datetime.now(timezone.utc)
        eval_dt = veto_dt + timedelta(days=10)
        if eval_dt > now_utc:
            log_info(f"Veto for {raw_ticker} not yet 10 days old — skipping")
            continue

        # Resolve Yahoo symbol — try the ticker field first (may be bare or T212)
        yahoo_sym = None
        # Check if raw_ticker looks like a T212 ticker
        if '_EQ' in raw_ticker:
            yahoo_sym = _t212_to_yahoo(raw_ticker, ticker_map)
        else:
            # Try as bare symbol — look up in map by key
            entry = ticker_map.get(raw_ticker)
            if entry:
                t212_tk = entry.get('t212', '')
                yahoo_sym = _t212_to_yahoo(t212_tk, ticker_map)
            else:
                # Try appending _US_EQ fallback
                yahoo_sym = raw_ticker

        if not yahoo_sym:
            log_warning(f"Cannot resolve Yahoo symbol for veto ticker: {raw_ticker}")
            action['veto_correct'] = None
            action['veto_eval_note'] = 'no_yahoo_symbol'
            continue

        log_info(f"Evaluating veto: {raw_ticker} → {yahoo_sym}, veto_date={veto_dt.date()}")

        price_on_veto_day = _get_price_on_date(yahoo_sym, veto_dt)
        price_10_days_later = _get_price_on_date(yahoo_sym, eval_dt)

        if price_on_veto_day is None or price_10_days_later is None:
            action['veto_correct'] = None
            action['veto_eval_note'] = 'insufficient_price_data'
            results['insufficient_data'] += 1
            log_info(f"  Insufficient data for {yahoo_sym}")
            continue

        pct_change = (price_10_days_later - price_on_veto_day) / price_on_veto_day

        # BUY veto evaluation
        if pct_change < 0:
            # Price fell → veto was correct (avoided a loser)
            correct = True
            note = f'price_fell_{pct_change:.1%}'
        elif pct_change > 0.03:
            # Price rose >3% → veto was wrong (missed a winner)
            correct = False
            note = f'price_rose_{pct_change:.1%}'
        else:
            # Small move — inconclusive
            correct = None
            note = f'inconclusive_move_{pct_change:.1%}'

        action['veto_correct']   = correct
        action['veto_eval_note'] = note
        action['veto_price_on_day']  = round(price_on_veto_day, 4)
        action['veto_price_10d']     = round(price_10_days_later, 4)
        action['veto_pct_change']    = round(pct_change * 100, 2)

        results['evaluated'] += 1
        if correct is True:
            results['correct'] += 1
        elif correct is False:
            results['incorrect'] += 1
        else:
            results['insufficient_data'] += 1

        log_info(f"  {raw_ticker}: veto_correct={correct} ({note})")

    # Write back
    if not dry_run and results['evaluated'] > 0:
        def _update(d):
            if not isinstance(d, dict):
                d = {'actions': d if isinstance(d, list) else []}
            d['actions'] = actions
            return d
        locked_read_modify_write(ACTIONS_FILE, _update, default={'actions': []})
        log_info(f"apex-veto-evaluator: wrote {results['evaluated']} evaluations to {ACTIONS_FILE}")

    # Summary
    n_eval = results['evaluated']
    n_all  = len(vetoes)
    acc    = round(results['correct'] / n_eval * 100, 1) if n_eval > 0 else None
    print(f"Veto evaluator summary:")
    print(f"  Vetoes pending evaluation: {n_all}")
    print(f"  Evaluated this run:        {n_eval}")
    print(f"  Correct (avoided losers):  {results['correct']}")
    print(f"  Incorrect (missed winners): {results['incorrect']}")
    print(f"  Accuracy (correct/evaluated): {acc}%")
    if dry_run:
        print("  [DRY RUN — no changes written]")

    return results


def main():
    parser = argparse.ArgumentParser(description='Apex Veto Evaluator')
    parser.add_argument('--dry-run', action='store_true', help='Do not write results')
    args = parser.parse_args()
    evaluate_vetoes(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
