#!/usr/bin/env python3
"""
Inverse ETF Scanner
Scans for short opportunities when market regime is CAUTIOUS or HOSTILE.
Uses the same scoring framework as trend scanner but triggered by bearish conditions.
Instruments: SQQQ (3x short NASDAQ), 3USS (3x short S&P500), 3UKS (3x short FTSE), SPXU (3x short S&P500)
"""
import json
import yfinance as yf
from datetime import datetime, timezone
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


INVERSE_FILE   = '/home/ubuntu/.picoclaw/logs/apex-inverse-signals.json'
REGIME_FILE    = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
SENTIMENT_FILE = '/home/ubuntu/.picoclaw/logs/apex-sentiment.json'
DIRECTION_FILE = '/home/ubuntu/.picoclaw/logs/apex-market-direction.json'
SECTOR_FILE    = '/home/ubuntu/.picoclaw/logs/apex-sector-rotation.json'

# Inverse ETF universe
INVERSE_UNIVERSE = {
    "SQQQ": {
        "t212_ticker": "SQQQ_EQ",
        "yahoo":       "SQQQ",
        "name":        "ProShares UltraPro Short QQQ",
        "tracks":      "NASDAQ 3x inverse",
        "leverage":    3,
        "currency":    "USD",
        "trigger":     "NASDAQ",  # Which index weakness triggers this
    },
    "3USS": {
        "t212_ticker": "3USSl_EQ",
        "yahoo":       "3USS.L",
        "name":        "WisdomTree S&P 500 3x Short",
        "tracks":      "S&P 500 3x inverse",
        "leverage":    3,
        "currency":    "GBP",
        "trigger":     "SP500",
    },
    "SPXU": {
        "t212_ticker": "SPXU_EQ",
        "yahoo":       "SPXU",
        "name":        "ProShares UltraPro Short S&P500",
        "tracks":      "S&P 500 3x inverse",
        "leverage":    3,
        "currency":    "USD",
        "trigger":     "SP500",
    },
    "3UKS": {
        "t212_ticker": "3UKSl_EQ",
        "yahoo":       "3UKS.L",
        "name":        "WisdomTree FTSE 100 3x Short",
        "tracks":      "FTSE 100 3x inverse",
        "leverage":    3,
        "currency":    "GBP",
        "trigger":     "FTSE",
    },
    "QQQS": {
        "t212_ticker": "QQQSl_EQ",
        "yahoo":       "QQQS.L",
        "name":        "WisdomTree NASDAQ 100 3x Short",
        "tracks":      "NASDAQ 3x inverse",
        "leverage":    3,
        "currency":    "GBP",
        "trigger":     "NASDAQ",
    },
}

def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default or {}

def fix_pence(price, yahoo):
    if yahoo.endswith('.L') and price > 100:
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
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calculate_ema(closes, period):
    if not closes:
        return 0
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def score_inverse_signal(name, data):
    """
    Score an inverse ETF signal 0-10.
    Higher score = stronger bearish conditions = better inverse ETF entry.
    Inverse of trend scoring — we want:
    - Price ABOVE 50/200 EMA (inverse ETF trending up = market trending down)
    - RSI 45-65 (not overbought — don't chase)
    - Volume above average
    - MACD positive and rising
    """
    yahoo  = data['yahoo']
    try:
        hist = yf.Ticker(yahoo).history(period="6mo")
        if hist.empty or len(hist) < 50:
            return None
    except:
        return None

    closes  = [fix_pence(float(c), yahoo) for c in hist['Close']]
    volumes = [float(v) for v in hist['Volume']]
    price   = closes[-1]

    if price <= 0:
        return None

    ema50  = calculate_ema(closes[-50:], 50)
    ema20  = calculate_ema(closes[-20:], 20)
    rsi    = calculate_rsi(closes[-28:])

    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

    # MACD
    if len(closes) >= 26:
        ema12 = calculate_ema(closes[-12:], 12)
        ema26 = calculate_ema(closes[-26:], 26)
        macd  = ema12 - ema26
        ema12p = calculate_ema(closes[-13:-1], 12)
        ema26p = calculate_ema(closes[-27:-1], 26)
        macd_prev = ema12p - ema26p
        macd_rising = macd > macd_prev
    else:
        macd, macd_rising = 0, False

    score = 0
    reasons = []

    # 1. Trend — inverse ETF above its own EMAs (bearish momentum established)
    if price > ema50:
        score += 3
        reasons.append(f"Above 50 EMA £{round(ema50,2)} — bearish momentum active")
    elif price > ema20:
        score += 1
        reasons.append(f"Above 20 EMA — early bearish signal")

    # 2. RSI — sweet spot 40-65 (not oversold, not overbought)
    if 40 <= rsi <= 65:
        score += 3
        reasons.append(f"RSI {rsi} — ideal entry range")
    elif 35 <= rsi < 40:
        score += 1
        reasons.append(f"RSI {rsi} — acceptable")
    elif rsi > 75:
        score -= 1
        reasons.append(f"RSI {rsi} — overbought, wait for pullback")

    # 3. Volume
    if vol_ratio >= 1.2:
        score += 2
        reasons.append(f"Volume {round(vol_ratio,1)}x above average — conviction")
    elif vol_ratio >= 0.8:
        score += 1

    # 4. MACD
    if macd > 0 and macd_rising:
        score += 2
        reasons.append("MACD positive and rising — momentum confirmed")
    elif macd > 0:
        score += 1

    # ATR for stop calculation
    try:
        highs  = [fix_pence(float(h), yahoo) for h in hist['High']]
        lows   = [fix_pence(float(l), yahoo) for l in hist['Low']]
        true_ranges = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
                      for i in range(1, len(closes))]
        atr = sum(true_ranges[-14:]) / 14
    except:
        atr = price * 0.03

    stop    = round(price - atr * 2, 2)
    target1 = round(price + atr * 2, 2)
    target2 = round(price + atr * 3.5, 2)

    # Position sizing — smaller for leveraged instruments (max 3% portfolio)
    risk_per_share = price - stop
    max_risk = 40  # Lower than normal — leveraged instrument
    qty = round(min(max_risk / risk_per_share, 150 / price), 2) if risk_per_share > 0 else 1

    return {
        "name":        data['name'],
        "ticker":      name,
        "t212_ticker": data['t212_ticker'],
        "yahoo":       yahoo,
        "tracks":      data['tracks'],
        "leverage":    data['leverage'],
        "currency":    data['currency'],
        "trigger":     data['trigger'],
        "price":       round(price, 2),
        "score":       score,
        "rsi":         rsi,
        "ema50":       round(ema50, 2),
        "vol_ratio":   round(vol_ratio, 2),
        "atr":         round(atr, 4),
        "stop":        stop,
        "target1":     target1,
        "target2":     target2,
        "quantity":    qty,
        "signal_type": "INVERSE",
        "reasons":     reasons,
    }

def should_scan_inverse():
    """
    Only scan for inverse ETFs when conditions are bearish.
    Returns True if inverse scan is warranted.
    """
    regime    = load(REGIME_FILE)
    direction = load(DIRECTION_FILE)
    sentiment = load(SENTIMENT_FILE)

    label     = regime.get('regime_label', 'NEUTRAL')
    breadth   = float(regime.get('breadth', 50))
    vix       = float(regime.get('vix', 20))
    mkt_dir   = direction.get('overall', 'CLEAR')
    mkt_sent  = float(sentiment.get('market_sentiment', 0))

    reasons = []
    score   = 0

    # Regime conditions
    if label in ['HOSTILE', 'BLOCKED']:
        score += 3
        reasons.append(f"Regime: {label}")
    elif label == 'CAUTIOUS':
        score += 2
        reasons.append(f"Regime: CAUTIOUS")

    # Breadth
    if breadth < 25:
        score += 2
        reasons.append(f"Breadth {breadth}% — extreme weakness")
    elif breadth < 40:
        score += 1
        reasons.append(f"Breadth {breadth}% — weak")

    # VIX
    if vix > 30:
        score += 2
        reasons.append(f"VIX {vix} — fear elevated")
    elif vix > 22:
        score += 1
        reasons.append(f"VIX {vix} — elevated")

    # Market direction
    if mkt_dir == 'BLOCKED':
        score += 2
        reasons.append("Market direction: BLOCKED")

    # Sentiment
    if mkt_sent < -0.2:
        score += 1
        reasons.append(f"Sentiment: {mkt_sent} — negative")

    should_scan = score >= 3
    return should_scan, score, reasons

def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== APEX INVERSE ETF SCANNER ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Check if conditions warrant inverse scan
    should_scan, cond_score, cond_reasons = should_scan_inverse()

    print(f"Market conditions score: {cond_score}/10")
    for r in cond_reasons:
        print(f"  → {r}")

    if not should_scan:
        print(f"\n✅ Market conditions NOT bearish enough for inverse ETFs (score {cond_score}/10)")
        print(f"   Inverse scan only activates when score ≥ 3")
        output = {
            'timestamp':      now.isoformat(),
            'scan_triggered': False,
            'condition_score':cond_score,
            'signals':        [],
            'message':        'Conditions not bearish enough'
        }
        atomic_write(INVERSE_FILE, output)
        return output

    print(f"\n🔴 BEARISH CONDITIONS DETECTED — scanning inverse ETFs...\n")

    signals = []
    for name, data in INVERSE_UNIVERSE.items():
        print(f"  Scanning {name} ({data['tracks']})...", flush=True)
        result = score_inverse_signal(name, data)
        if result:
            qualifying = result['score'] >= 6
            flag = "✅" if qualifying else "  "
            print(f"  {flag} {name}: score {result['score']}/10 | RSI {result['rsi']} | Price £{result['price']}")
            if qualifying:
                signals.append(result)
        else:
            print(f"     {name}: no data")

    # Sort by score
    signals.sort(key=lambda x: x['score'], reverse=True)

    print(f"\n=== INVERSE ETF SIGNALS ===")
    if signals:
        for s in signals:
            print(f"\n  🔴 {s['name']}")
            print(f"     Ticker:  {s['t212_ticker']}")
            print(f"     Price:   £{s['price']} | Score: {s['score']}/10 | RSI: {s['rsi']}")
            print(f"     Stop:    £{s['stop']} | T1: £{s['target1']} | T2: £{s['target2']}")
            print(f"     Qty:     {s['quantity']} | ATR: £{s['atr']}")
            for r in s['reasons']:
                print(f"     → {r}")
    else:
        print(f"  No inverse ETF signals at threshold 6/10")
        print(f"  Market is bearish but inverse ETFs not yet in optimal entry zone")

    output = {
        'timestamp':       now.isoformat(),
        'scan_triggered':  True,
        'condition_score': cond_score,
        'condition_reasons': cond_reasons,
        'signals':         signals,
        'best':            signals[0] if signals else None,
    }

    atomic_write(INVERSE_FILE, output)

    print(f"\n✅ Inverse scan complete — {len(signals)} signals found")
    return output

if __name__ == '__main__':
    run()
