#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

def log_outcome(ticker, exit_price, outcome_type):
    # Load outcomes
    try:
        with open(OUTCOMES_FILE) as f:
            db = json.load(f)
    except:
        db = {"trades": [], "summary": {}}

    # Load position details
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        positions = []

    pos = next((p for p in positions if p.get('t212_ticker', '').upper() == ticker.upper()), None)
    if not pos:
        print(f"Position {ticker} not found in positions file")
        return

    entry       = float(pos.get('entry', 0))
    stop        = float(pos.get('stop', 0))
    target1     = float(pos.get('target1', 0))
    target2     = float(pos.get('target2', 0))
    qty         = float(pos.get('quantity', 0))
    opened      = pos.get('opened', 'unknown')
    name        = pos.get('name', ticker)
    score       = pos.get('score', 0)
    rsi         = pos.get('rsi', 0)
    macd        = pos.get('macd', 0)
    sector      = pos.get('sector', 'unknown')

    exit_price  = float(exit_price)
    risk        = entry - stop if entry > stop else 1
    r_achieved  = round((exit_price - entry) / risk, 2)
    pnl         = round((exit_price - entry) * qty, 2)

    # Determine outcome
    if exit_price >= target2:
        result = "TARGET2_HIT"
    elif exit_price >= target1:
        result = "TARGET1_HIT"
    elif exit_price <= stop:
        result = "STOP_HIT"
    elif pnl > 0:
        result = "MANUAL_WIN"
    elif pnl < 0:
        result = "MANUAL_LOSS"
    else:
        result = "BREAKEVEN"

    # RSI range bucket
    if rsi < 45:
        rsi_bucket = "below_45"
    elif rsi < 55:
        rsi_bucket = "45_to_55"
    elif rsi < 65:
        rsi_bucket = "55_to_65"
    else:
        rsi_bucket = "65_to_70"

    # Day of week opened
    try:
        day = datetime.strptime(opened, '%Y-%m-%d').strftime('%A')
    except:
        day = 'unknown'

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    trade = {
        "id":           len(db['trades']) + 1,
        "name":         name,
        "ticker":       ticker,
        "opened":       opened,
        "closed":       now[:10],
        "entry":        entry,
        "exit":         exit_price,
        "stop":         stop,
        "target1":      target1,
        "target2":      target2,
        "quantity":     qty,
        "pnl":          pnl,
        "r_achieved":   r_achieved,
        "result":       result,
        "outcome_type": outcome_type,
        "score":        score,
        "rsi":          rsi,
        "rsi_bucket":   rsi_bucket,
        "macd":         macd,
        "macd_positive": macd > 0,
        "sector":       sector,
        "day_opened":   day,
        "days_held":    (datetime.strptime(now[:10], '%Y-%m-%d') - datetime.strptime(opened, '%Y-%m-%d')).days if opened != 'unknown' else 0
    }

    db['trades'].append(trade)

    # Update summary
    trades = db['trades']
    winners   = [t for t in trades if t['pnl'] > 0]
    losers    = [t for t in trades if t['pnl'] < 0]
    breakeven = [t for t in trades if t['pnl'] == 0]

    db['summary'] = {
        "total_trades":  len(trades),
        "winners":       len(winners),
        "losers":        len(losers),
        "breakeven":     len(breakeven),
        "win_rate":      round(len(winners) / len(trades) * 100, 1) if trades else 0,
        "avg_r_achieved": round(sum(t['r_achieved'] for t in trades) / len(trades), 2) if trades else 0,
        "total_pnl":     round(sum(t['pnl'] for t in trades), 2),
        "best_trade":    max(trades, key=lambda x: x['pnl'])['name'] if trades else None,
        "worst_trade":   min(trades, key=lambda x: x['pnl'])['name'] if trades else None,
        "by_sector":     {},
        "by_day":        {},
        "by_rsi_bucket": {},
        "by_macd":       {}
    }

    # Sector breakdown
    sectors = set(t['sector'] for t in trades)
    for s in sectors:
        s_trades = [t for t in trades if t['sector'] == s]
        s_wins   = [t for t in s_trades if t['pnl'] > 0]
        db['summary']['by_sector'][s] = {
            "trades": len(s_trades),
            "win_rate": round(len(s_wins) / len(s_trades) * 100, 1)
        }

    # Day breakdown
    days = set(t['day_opened'] for t in trades)
    for d in days:
        d_trades = [t for t in trades if t['day_opened'] == d]
        d_wins   = [t for t in d_trades if t['pnl'] > 0]
        db['summary']['by_day'][d] = {
            "trades": len(d_trades),
            "win_rate": round(len(d_wins) / len(d_trades) * 100, 1)
        }

    # RSI bucket breakdown
    buckets = set(t['rsi_bucket'] for t in trades)
    for b in buckets:
        b_trades = [t for t in trades if t['rsi_bucket'] == b]
        b_wins   = [t for t in b_trades if t['pnl'] > 0]
        db['summary']['by_rsi_bucket'][b] = {
            "trades": len(b_trades),
            "win_rate": round(len(b_wins) / len(b_trades) * 100, 1)
        }

    # MACD breakdown
    for macd_state in [True, False]:
        m_trades = [t for t in trades if t['macd_positive'] == macd_state]
        m_wins   = [t for t in m_trades if t['pnl'] > 0]
        key = "macd_positive" if macd_state else "macd_negative"
        db['summary']['by_macd'][key] = {
            "trades": len(m_trades),
            "win_rate": round(len(m_wins) / len(m_trades) * 100, 1) if m_trades else 0
        }

    with open(OUTCOMES_FILE, 'w') as f:
        json.dump(db, f, indent=2)

    print(f"✅ Trade logged: {name} | {result} | P&L: £{pnl} | R: {r_achieved}")
    print(f"📊 Running stats: {len(trades)} trades | Win rate: {db['summary']['win_rate']}% | Total P&L: £{db['summary']['total_pnl']}")

def update_param_log(name, pnl, r_achieved, days_held, exit_reason):
    """Update shadow portfolio parameter log with trade outcome."""
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "pl", "/home/ubuntu/.picoclaw/scripts/apex-param-logger.py")
        _pl = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_pl)
        _pl.update_outcome(name, pnl, r_achieved, days_held, exit_reason)
    except Exception as e:
        log_error(f"param log update failed: {e}")

if __name__ == '__main__':
    if len(sys.argv) < 4:
        print("Usage: apex-log-outcome.py TICKER EXIT_PRICE OUTCOME_TYPE")
        print("Example: apex-log-outcome.py XOM_US_EQ 165.50 TRIM")
        sys.exit(1)
    log_outcome(sys.argv[1], sys.argv[2], sys.argv[3])
