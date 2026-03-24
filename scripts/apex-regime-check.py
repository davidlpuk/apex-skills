#!/usr/bin/env python3
import yfinance as yf
import json
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


SAMPLE_BREADTH = [
    'AAPL','MSFT','NVDA','GOOGL','AMZN','META','JPM','JNJ','V','XOM',
    'CVX','PG','KO','MCD','WMT','GS','BAC','UNH','ABBV','DHR',
    'TSLA','ORCL','CRM','AMD','QCOM','BLK','AXP','TMO','DHR','PFE'
]

def check_regime():
    result = {
        "vix":              None,
        "vix_regime":       None,
        "breadth_pct":      None,
        "breadth_regime":   None,
        "overall":          None,
        "block_reason":     [],
        "timestamp":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    }

    # VIX check
    try:
        vix_hist = yf.Ticker('^VIX').history(period='1mo')
        vix = round(float(vix_hist['Close'].iloc[-1]), 2)
        result['vix'] = vix

        if vix < 15:
            result['vix_regime'] = "LOW_FEAR"
        elif vix < 20:
            result['vix_regime'] = "NORMAL"
        elif vix < 25:
            result['vix_regime'] = "ELEVATED"
        elif vix < 30:
            result['vix_regime'] = "HIGH"
        else:
            result['vix_regime'] = "EXTREME"

        if vix >= 35:
            result['block_reason'].append(f"VIX {vix} — extreme fear, no new longs")
        elif vix >= 28:
            result['block_reason'].append(f"VIX {vix} — high fear, reduce position sizes by 50%")

    except Exception as e:
        result['vix_regime'] = "UNKNOWN"

    # Market breadth check
    try:
        above = 0
        checked = 0
        for ticker in SAMPLE_BREADTH:
            try:
                h = yf.Ticker(ticker).history(period='1y')
                if h.empty:
                    continue
                price  = float(h['Close'].iloc[-1])
                ema200 = float(h['Close'].ewm(span=200).mean().iloc[-1])
                if price > ema200:
                    above += 1
                checked += 1
            except:
                pass

        breadth = round(above / checked * 100, 1) if checked > 0 else 0
        result['breadth_pct']  = breadth
        result['breadth_regime'] = "BULLISH" if breadth >= 60 else ("NEUTRAL" if breadth >= 40 else "BEARISH")

        if breadth < 30:
            result['block_reason'].append(f"Breadth {breadth}% — fewer than 30% of stocks healthy, avoid new longs")
        elif breadth < 60:
            result['block_reason'].append(f"Breadth {breadth}% — neutral, be selective")

    except Exception as e:
        result['breadth_regime'] = "UNKNOWN"

    # Overall regime
    blocked = len([r for r in result['block_reason'] if 'no new longs' in r or 'avoid new longs' in r]) > 0
    result['overall'] = "BLOCKED" if blocked else "CLEAR"

    return result

if __name__ == '__main__':
    print("Checking market regime...", flush=True)
    r = check_regime()

    print(f"\n=== MARKET REGIME ===")
    print(f"VIX:      {r['vix']} ({r['vix_regime']})")
    print(f"Breadth:  {r['breadth_pct']}% above 200 EMA ({r['breadth_regime']})")
    print(f"Overall:  {r['overall']}")
    if r['block_reason']:
        for reason in r['block_reason']:
            print(f"⚠️  {reason}")
    else:
        print("✅ Market conditions clear for trading")

    # Save for morning scan to use
    atomic_write('/home/ubuntu/.picoclaw/logs/apex-regime.json', r)

    print(f"\n=== JSON ===")
    print(json.dumps(r, indent=2))
