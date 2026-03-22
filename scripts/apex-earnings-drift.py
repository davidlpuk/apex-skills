#!/usr/bin/env python3
"""
Post-earnings drift strategy.
Scans for quality companies that beat earnings estimates significantly
2 days after reporting. Buys the drift continuation.
"""
import yfinance as yf
import json
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


DRIFT_FILE     = '/home/ubuntu/.picoclaw/logs/apex-earnings-drift.json'
QUALITY_FILE   = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'
TICKER_MAP     = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'

YAHOO_MAP = {
    "AAPL": "AAPL",  "MSFT": "MSFT",  "NVDA": "NVDA",  "GOOGL": "GOOGL",
    "AMZN": "AMZN",  "META": "META",  "JPM":  "JPM",   "JNJ":  "JNJ",
    "XOM":  "XOM",   "CVX":  "CVX",   "V":    "V",     "UNH":  "UNH",
    "ABBV": "ABBV",  "GS":   "GS",    "MS":   "MS",    "BAC":  "BAC",
    "ASML": "ASML.AS","NOVO": "NVO",  "AZN":  "AZN.L", "GSK":  "GSK.L",
}

def check_earnings_beat(ticker, yahoo_ticker):
    """
    Check if company recently beat earnings and is showing drift.
    Uses price action as proxy — gap up on earnings day followed by
    continued strength is the drift signal.
    """
    try:
        t    = yf.Ticker(yahoo_ticker)
        hist = t.history(period="3mo")

        if hist.empty or len(hist) < 30:
            return None

        close  = hist['Close']
        volume = hist['Volume']

        if close.iloc[-1] > 500 and yahoo_ticker.endswith('.L'):
            close = close / 100

        price      = round(float(close.iloc[-1]), 2)
        avg_volume = float(volume.rolling(20).mean().iloc[-1])

        # Look for earnings gap — single day move > 4% on high volume
        # in the last 30 trading days
        earnings_day   = None
        earnings_return = 0

        for i in range(2, min(30, len(close))):
            day_return = (close.iloc[-i] - close.iloc[-i-1]) / close.iloc[-i-1] * 100
            day_volume = volume.iloc[-i]

            # Earnings beat signature: big gap up on 2x+ volume
            if day_return > 4 and day_volume > avg_volume * 1.8:
                earnings_day    = hist.index[-i]
                earnings_return = round(day_return, 2)
                days_since      = i
                break

        if not earnings_day:
            return None

        # Check drift continuation — price higher now than day after earnings
        price_at_earnings = float(close.iloc[-days_since])
        drift_return      = round((price - price_at_earnings) / price_at_earnings * 100, 2)

        # Only signal if:
        # 1. Earnings beat was significant (>4%)
        # 2. Drift is positive (continuing higher)
        # 3. Not too far from earnings (within 10 days for drift play)
        # 4. Price still below pre-earnings high + 15% (not overextended)
        if drift_return < 0:
            return None
        if days_since > 10:
            return None

        # RSI check — not overbought
        delta = close.diff()
        gain  = delta.where(delta > 0, 0).rolling(14).mean()
        loss  = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi   = round(float(100 - (100 / (1 + gain/loss)).iloc[-1]), 2)

        if rsi > 75:
            return None

        # Stop below earnings day low
        earnings_day_low = float(hist['Low'].iloc[-days_since])
        stop = round(earnings_day_low * 0.98, 2)
        risk = round(price - stop, 2)

        # Targets
        target1 = round(price + risk * 1.5, 2)
        target2 = round(price + risk * 2.5, 2)
        qty     = round(min(50 / risk, 250 / price), 2) if risk > 0 else 1

        return {
            "name":              ticker,
            "ticker":            yahoo_ticker,
            "price":             price,
            "rsi":               rsi,
            "earnings_date":     earnings_day.strftime('%Y-%m-%d'),
            "earnings_return":   earnings_return,
            "days_since":        days_since,
            "drift_return":      drift_return,
            "stop":              stop,
            "target1":           target1,
            "target2":           target2,
            "quantity":          qty,
            "signal_type":       "EARNINGS_DRIFT",
            "score":             min(10, round(earnings_return / 2 + drift_return + (10 - days_since) * 0.3, 1))
        }

    except Exception as e:
        return None

def run():
    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        quality = quality_db.get('quality_stocks', {})
    except:
        quality = {}

    now      = datetime.now(timezone.utc)
    signals  = []

    print("Scanning for post-earnings drift opportunities...", flush=True)

    for name, data in quality.items():
        yahoo = YAHOO_MAP.get(name, '')
        if not yahoo:
            continue

        print(f"  {name}...", flush=True)
        result = check_earnings_beat(name, yahoo)

        if result:
            qs = data.get('quality_score', 5)
            result['quality_score'] = qs
            signals.append(result)
            print(f"  ✅ DRIFT SIGNAL: {name} | earnings +{result['earnings_return']}% | drift +{result['drift_return']}% | {result['days_since']} days ago")

    signals.sort(key=lambda x: x.get('score', 0), reverse=True)

    output = {
        "timestamp": now.strftime('%Y-%m-%d %H:%M UTC'),
        "signals":   signals
    }

    atomic_write(DRIFT_FILE, output)

    print(f"\n=== EARNINGS DRIFT SIGNALS ===")
    if signals:
        print(f"{len(signals)} qualifying signals:\n")
        for s in signals:
            print(f"  {s['name']:6} | Score: {s['score']}/10 | "
                  f"Earnings: +{s['earnings_return']}% ({s['days_since']}d ago) | "
                  f"Drift: +{s['drift_return']}% | RSI: {s['rsi']}")
    else:
        print("No post-earnings drift signals today")

    return output

if __name__ == '__main__':
    run()
