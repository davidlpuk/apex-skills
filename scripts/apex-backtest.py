#!/usr/bin/env python3
"""
Apex Backtesting Engine
Tests the 4-factor scoring system against 2 years of historical data.
Validates signal thresholds, win rates, and strategy edge.
"""
import yfinance as yf
import json
import sys
from datetime import datetime, timezone, timedelta
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


BACKTEST_FILE  = '/home/ubuntu/.picoclaw/logs/apex-backtest-results.json'
QUALITY_FILE   = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'

# Universe to backtest
BACKTEST_UNIVERSE = {
    "AAPL":  "AAPL",    "MSFT":  "MSFT",    "NVDA":  "NVDA",
    "GOOGL": "GOOGL",   "AMZN":  "AMZN",    "META":  "META",
    "JPM":   "JPM",     "XOM":   "XOM",     "CVX":   "CVX",
    "V":     "V",       "JNJ":   "JNJ",     "ABBV":  "ABBV",
    "VUAG":  "VUAG.L",  "SHEL":  "SHEL.L",  "AZN":   "AZN.L",
    "HSBA":  "HSBA.L",  "GSK":   "GSK.L",   "ULVR":  "ULVR.L",
}

def fix_pence(price, ticker):
    if ticker.endswith('.L') and price > 100:
        return price / 100
    return price

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs  = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(closes, period):
    if not closes:
        return 0
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_macd_hist(closes):
    if len(closes) < 26:
        return 0, False
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd  = ema12 - ema26

    # Signal line approximation
    ema12_prev = calculate_ema(closes[:-1], 12)
    ema26_prev = calculate_ema(closes[:-1], 26)
    macd_prev  = ema12_prev - ema26_prev

    signal      = calculate_ema([macd_prev, macd], 9)
    hist        = macd - signal
    hist_prev   = macd_prev - calculate_ema([macd_prev]*2, 9)

    return round(hist, 4), hist > hist_prev

def score_signal(closes, volumes, mode='TREND'):
    """Score using Apex 4-factor system."""
    if len(closes) < 200:
        return 0

    price  = closes[-1]
    ema50  = calculate_ema(closes[-50:], 50)
    ema200 = calculate_ema(closes[-200:], 200)
    rsi    = calculate_rsi(closes[-28:])
    macd_h, macd_rising = calculate_macd_hist(closes[-35:])

    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

    if mode == 'TREND':
        # Trend scoring
        trend_score = 3 if price > ema50 > ema200 else 0
        rsi_score   = 3 if 45 <= rsi <= 70 else (1 if 35 <= rsi < 45 or 70 < rsi <= 80 else 0)
        vol_score   = 2 if vol_ratio >= 1.0 else 1
        macd_score  = 2 if macd_h > 0 and macd_rising else (1 if macd_h > 0 else 0)
        return trend_score + rsi_score + vol_score + macd_score, rsi

    elif mode == 'CONTRARIAN':
        # Contrarian scoring
        score = 0
        if rsi <= 25:   score += 4
        elif rsi <= 32: score += 3
        elif rsi <= 38: score += 2

        high_52  = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        discount = (high_52 - price) / high_52 * 100
        if discount >= 25:   score += 3
        elif discount >= 15: score += 2
        elif discount >= 10: score += 1

        # Quality bonus — hardcoded 2 for backtest universe
        score += 2

        if macd_rising: score += 1

        return score, rsi

def simulate_trade(closes, entry_idx, stop_pct=0.06, t1_r=1.5, t2_r=2.5, max_days=20):
    """
    Simulate a trade from entry_idx.
    Returns: outcome, pnl_r, days_held, exit_reason
    """
    if entry_idx >= len(closes) - 1:
        return 'TIMEOUT', 0, 0, 'insufficient_data'

    entry = closes[entry_idx]
    stop  = entry * (1 - stop_pct)
    t1    = entry + (entry - stop) * t1_r
    t2    = entry + (entry - stop) * t2_r
    risk  = entry - stop

    for day in range(1, min(max_days + 1, len(closes) - entry_idx)):
        price = closes[entry_idx + day]

        if price <= stop:
            return 'LOSS', -1.0, day, 'stop_hit'

        if price >= t2:
            pnl_r = round((price - entry) / risk, 2)
            return 'WIN', pnl_r, day, 'target2_hit'

        if price >= t1:
            # Move stop to breakeven, ride to T2
            stop = entry  # Breakeven

    # Exit at max days — at current price
    final_price = closes[entry_idx + min(max_days, len(closes) - entry_idx - 1)]
    pnl_r = round((final_price - entry) / risk, 2)
    outcome = 'WIN' if pnl_r > 0 else 'LOSS'
    return outcome, pnl_r, max_days, 'timeout'

def backtest_instrument(name, yahoo_ticker, mode='TREND', threshold=7):
    """Backtest one instrument over 2 years."""
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="2y")
        if hist.empty or len(hist) < 250:
            return None

        closes  = [fix_pence(float(c), yahoo_ticker) for c in hist['Close']]
        volumes = [float(v) for v in hist['Volume']]
        dates   = [d.strftime('%Y-%m-%d') for d in hist.index]

        trades = []

        # Walk forward — scan each day from day 200 onwards
        for i in range(200, len(closes) - 21):
            score, rsi = score_signal(closes[:i+1], volumes[:i+1], mode)

            if score >= threshold:
                # Check we're not already in a trade (simplistic)
                if trades and trades[-1].get('entry_idx', 0) + trades[-1].get('days_held', 20) > i:
                    continue

                outcome, pnl_r, days, reason = simulate_trade(closes, i)

                trades.append({
                    "date":      dates[i],
                    "entry_idx": i,
                    "score":     score,
                    "rsi":       rsi,
                    "entry":     round(closes[i], 2),
                    "outcome":   outcome,
                    "pnl_r":     pnl_r,
                    "days_held": days,
                    "reason":    reason
                })

        return trades

    except Exception as e:
        print(f"  Error {name}: {e}")
        return None

def analyse_results(all_trades):
    """Calculate statistics across all backtest trades."""
    if not all_trades:
        return {}

    total  = len(all_trades)
    wins   = [t for t in all_trades if t['outcome'] == 'WIN']
    losses = [t for t in all_trades if t['outcome'] == 'LOSS']

    win_rate    = round(len(wins) / total * 100, 1)
    avg_win_r   = round(sum(t['pnl_r'] for t in wins) / len(wins), 2) if wins else 0
    avg_loss_r  = round(sum(t['pnl_r'] for t in losses) / len(losses), 2) if losses else 0
    expectancy  = round((win_rate/100 * avg_win_r) + ((1-win_rate/100) * avg_loss_r), 3)
    profit_factor = round(abs(sum(t['pnl_r'] for t in wins) / sum(t['pnl_r'] for t in losses)), 2) if losses and sum(t['pnl_r'] for t in losses) != 0 else 0
    avg_days    = round(sum(t['days_held'] for t in all_trades) / total, 1)

    # By score bucket
    by_score = {}
    for t in all_trades:
        s = t['score']
        if s not in by_score:
            by_score[s] = {'wins': 0, 'total': 0}
        by_score[s]['total'] += 1
        if t['outcome'] == 'WIN':
            by_score[s]['wins'] += 1

    score_analysis = {}
    for score, data in sorted(by_score.items()):
        wr = round(data['wins'] / data['total'] * 100, 1)
        score_analysis[str(score)] = {
            'win_rate': wr,
            'trades':   data['total']
        }

    # By RSI bucket
    rsi_buckets = {
        'oversold (<30)':   [t for t in all_trades if t['rsi'] < 30],
        'low (30-45)':      [t for t in all_trades if 30 <= t['rsi'] < 45],
        'neutral (45-60)':  [t for t in all_trades if 45 <= t['rsi'] < 60],
        'high (60-70)':     [t for t in all_trades if 60 <= t['rsi'] < 70],
        'overbought (>70)': [t for t in all_trades if t['rsi'] >= 70],
    }

    rsi_analysis = {}
    for bucket, trades in rsi_buckets.items():
        if trades:
            wr = round(sum(1 for t in trades if t['outcome'] == 'WIN') / len(trades) * 100, 1)
            rsi_analysis[bucket] = {'win_rate': wr, 'trades': len(trades)}

    return {
        'total_trades':   total,
        'win_rate':       win_rate,
        'avg_win_r':      avg_win_r,
        'avg_loss_r':     avg_loss_r,
        'expectancy':     expectancy,
        'profit_factor':  profit_factor,
        'avg_days_held':  avg_days,
        'by_score':       score_analysis,
        'by_rsi':         rsi_analysis,
    }

def run(mode='TREND', threshold=7):
    now = datetime.now(timezone.utc)
    print(f"\n🔬 APEX BACKTEST ENGINE — {mode} signals")
    print(f"Universe: {len(BACKTEST_UNIVERSE)} instruments | Threshold: {threshold}/10")
    print(f"Period: 2 years | Stop: 6% | T1: 1.5R | T2: 2.5R | Max hold: 20 days")
    print("="*60)

    all_trades  = []
    by_instrument = {}

    for name, yahoo in BACKTEST_UNIVERSE.items():
        print(f"  Testing {name}...", flush=True)
        trades = backtest_instrument(name, yahoo, mode, threshold)

        if trades:
            wins     = sum(1 for t in trades if t['outcome'] == 'WIN')
            wr       = round(wins / len(trades) * 100, 1) if trades else 0
            by_instrument[name] = {
                'trades':   len(trades),
                'win_rate': wr,
                'trades_data': trades
            }
            all_trades.extend(trades)
            print(f"    {len(trades)} signals | {wr}% win rate")
        else:
            print(f"    No signals or error")

    # Analyse results
    stats = analyse_results(all_trades)

    print(f"\n{'='*60}")
    print(f"📊 BACKTEST RESULTS — {mode} (threshold {threshold}/10)")
    print(f"{'='*60}")
    print(f"  Total signals:    {stats.get('total_trades', 0)}")
    print(f"  Win rate:         {stats.get('win_rate', 0)}%")
    print(f"  Avg win:          {stats.get('avg_win_r', 0)}R")
    print(f"  Avg loss:         {stats.get('avg_loss_r', 0)}R")
    print(f"  Expectancy:       {stats.get('expectancy', 0)}R per trade")
    print(f"  Profit factor:    {stats.get('profit_factor', 0)}")
    print(f"  Avg hold:         {stats.get('avg_days_held', 0)} days")

    print(f"\n  Win rate by score:")
    for score, data in stats.get('by_score', {}).items():
        bar  = "█" * int(data['win_rate'] / 10)
        flag = "✅" if data['win_rate'] >= 55 else ("🟡" if data['win_rate'] >= 45 else "🔴")
        print(f"  {flag} Score {score}/10: {data['win_rate']:5}% ({data['trades']} trades) {bar}")

    print(f"\n  Win rate by RSI bucket:")
    for bucket, data in stats.get('by_rsi', {}).items():
        bar  = "█" * int(data['win_rate'] / 10)
        flag = "✅" if data['win_rate'] >= 55 else ("🟡" if data['win_rate'] >= 45 else "🔴")
        print(f"  {flag} {bucket:20}: {data['win_rate']:5}% ({data['trades']} trades) {bar}")

    print(f"\n  Best instruments:")
    sorted_inst = sorted(
        [(k, v) for k, v in by_instrument.items() if v['trades'] >= 3],
        key=lambda x: x[1]['win_rate'],
        reverse=True
    )
    for name, data in sorted_inst[:5]:
        print(f"    {name:8}: {data['win_rate']}% ({data['trades']} trades)")

    print(f"\n  Worst instruments:")
    for name, data in sorted_inst[-3:]:
        print(f"    {name:8}: {data['win_rate']}% ({data['trades']} trades)")

    # Optimal threshold analysis
    print(f"\n  Threshold optimisation:")
    for thresh in [6, 7, 8, 9]:
        thresh_trades = [t for t in all_trades if t['score'] >= thresh]
        if thresh_trades:
            thresh_wins = sum(1 for t in thresh_trades if t['outcome'] == 'WIN')
            thresh_wr   = round(thresh_wins / len(thresh_trades) * 100, 1)
            flag = "✅" if thresh_wr >= 55 else ("🟡" if thresh_wr >= 45 else "🔴")
            print(f"  {flag} Threshold {thresh}+: {thresh_wr}% win rate ({len(thresh_trades)} trades)")

    # Save results
    output = {
        "timestamp":   now.strftime('%Y-%m-%d %H:%M UTC'),
        "mode":        mode,
        "threshold":   threshold,
        "stats":       stats,
        "instruments": {k: {'trades': v['trades'], 'win_rate': v['win_rate']}
                       for k, v in by_instrument.items()},
        "all_trades":  all_trades[-100:]  # Save last 100 for reference
    }

    atomic_write(BACKTEST_FILE, output)

    print(f"\n✅ Results saved to apex-backtest-results.json")

    # Verdict
    expectancy = stats.get('expectancy', 0)
    win_rate   = stats.get('win_rate', 0)

    print(f"\n{'='*60}")
    print(f"VERDICT:")
    if expectancy > 0.3 and win_rate >= 55:
        print(f"✅ STRONG EDGE — strategy is validated. Safe to trade live.")
    elif expectancy > 0 and win_rate >= 45:
        print(f"🟡 MARGINAL EDGE — strategy works but consider raising threshold.")
    else:
        print(f"🔴 NO EDGE — strategy needs adjustment before live trading.")
    print(f"{'='*60}")

    return stats

if __name__ == '__main__':
    mode      = sys.argv[1] if len(sys.argv) > 1 else 'TREND'
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    run(mode, threshold)
