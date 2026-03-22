#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone

SIGNAL_LOG    = '/home/ubuntu/.picoclaw/logs/apex-signal-log.json'
OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'

def log_signal(name, ticker, score, rsi, macd, trend, price,
               currency, sector, action, block_reason=None):

    try:
        with open(SIGNAL_LOG) as f:
            db = json.load(f)
    except:
        db = {"signals": [], "stats": {}}

    now = datetime.now(timezone.utc)

    entry = {
        "id":           len(db['signals']) + 1,
        "timestamp":    now.isoformat(),
        "date":         now.strftime('%Y-%m-%d'),
        "day":          now.strftime('%A'),
        "hour":         now.hour,
        "name":         name,
        "ticker":       ticker,
        "score":        score,
        "rsi":          rsi,
        "macd":         macd,
        "trend":        trend,
        "price":        price,
        "currency":     currency,
        "sector":       sector,
        "action":       action,
        "block_reason": block_reason,
        "outcome":      None,
        "outcome_pnl":  None,
        "resolved":     False
    }

    db['signals'].append(entry)

    # Update stats
    stats = db.get('stats', {})
    stats['total_generated'] = stats.get('total_generated', 0) + 1

    action_map = {
        'CONFIRMED':           'confirmed',
        'REJECTED_BY_USER':    'rejected_by_user',
        'BLOCKED_REGIME':      'blocked_by_regime',
        'BLOCKED_SECTOR':      'blocked_by_sector',
        'BLOCKED_EARNINGS':    'blocked_by_earnings',
        'BLOCKED_NEWS':        'blocked_by_news',
        'BLOCKED_CORRELATION': 'blocked_by_correlation',
        'BLOCKED_STALE':       'blocked_stale',
        'DEFENSIVE_MODE':      'defensive_mode',
    }

    stat_key = action_map.get(action)
    if stat_key:
        stats[stat_key] = stats.get(stat_key, 0) + 1

    db['stats'] = stats

    with open(SIGNAL_LOG, 'w') as f:
        json.dump(db, f, indent=2)

    print(f"✅ Signal logged: {name} | {action} | score:{score} | {block_reason or 'no block'}")

def resolve_signal(signal_id, outcome, pnl):
    try:
        with open(SIGNAL_LOG) as f:
            db = json.load(f)
    except:
        print("No signal log found")
        return

    for s in db['signals']:
        if s['id'] == signal_id:
            s['outcome']     = outcome
            s['outcome_pnl'] = pnl
            s['resolved']    = True
            print(f"✅ Signal {signal_id} resolved: {outcome} | P&L: £{pnl}")
            break

    with open(SIGNAL_LOG, 'w') as f:
        json.dump(db, f, indent=2)

def show_stats():
    try:
        with open(SIGNAL_LOG) as f:
            db = json.load(f)
    except:
        print("No signal log yet")
        return

    stats   = db.get('stats', {})
    signals = db.get('signals', [])
    total   = stats.get('total_generated', 0)

    if total == 0:
        print("No signals logged yet")
        return

    print(f"\n📊 SIGNAL LOG STATS — {total} total signals\n")
    print(f"  Confirmed:           {stats.get('confirmed', 0)} ({round(stats.get('confirmed',0)/total*100,1)}%)")
    print(f"  Rejected by user:    {stats.get('rejected_by_user', 0)}")
    print(f"  Blocked — regime:    {stats.get('blocked_by_regime', 0)}")
    print(f"  Blocked — sector:    {stats.get('blocked_by_sector', 0)}")
    print(f"  Blocked — earnings:  {stats.get('blocked_by_earnings', 0)}")
    print(f"  Blocked — news:      {stats.get('blocked_by_news', 0)}")
    print(f"  Blocked — stale:     {stats.get('blocked_stale', 0)}")
    print(f"  Defensive mode:      {stats.get('defensive_mode', 0)}")

    # Most blocked instrument
    blocked = [s for s in signals if s['action'] != 'CONFIRMED']
    if blocked:
        from collections import Counter
        most_blocked = Counter(s['name'] for s in blocked).most_common(3)
        print(f"\n  Most blocked instruments:")
        for name, count in most_blocked:
            print(f"    {name}: {count} times")

    # Best unconfirmed signals — what we missed
    unresolved = [s for s in signals if not s['resolved'] and s['action'] != 'CONFIRMED']
    high_score = [s for s in unresolved if s.get('score', 0) >= 8]
    if high_score:
        print(f"\n  ⭐ High-score signals that were blocked ({len(high_score)}):")
        for s in high_score[-5:]:
            print(f"    {s['name']} | score:{s['score']} | {s['action']} | {s['date']}")

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'stats'

    if mode == 'stats':
        show_stats()
    elif mode == 'log':
        # Called with: log NAME TICKER SCORE RSI MACD TREND PRICE CCY SECTOR ACTION [REASON]
        args = sys.argv[2:]
        if len(args) >= 10:
            log_signal(
                name=args[0], ticker=args[1], score=float(args[2]),
                rsi=float(args[3]), macd=float(args[4]), trend=args[5],
                price=float(args[6]), currency=args[7], sector=args[8],
                action=args[9], block_reason=args[10] if len(args) > 10 else None
            )
    elif mode == 'resolve':
        resolve_signal(int(sys.argv[2]), sys.argv[3], float(sys.argv[4]))
