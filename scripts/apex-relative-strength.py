#!/usr/bin/env python3
"""
Relative Strength Analysis
Measures each stock's performance relative to its sector and the broad market.
Strong relative strength = institutions accumulating regardless of market.
Weak relative strength = distribution, avoid.
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


OUTPUT_FILE = '/home/ubuntu/.picoclaw/logs/apex-relative-strength.json'

# Sector ETFs as benchmarks
SECTOR_BENCHMARKS = {
    "Technology":   "XLK",
    "Financials":   "XLF",
    "Healthcare":   "XLV",
    "Energy":       "XLE",
    "Consumer":     "XLP",
    "Industrial":   "XLI",
    "UK":           "EWU",
    "Broad":        "SPY",
}

# Instrument to sector mapping
INSTRUMENT_SECTOR = {
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology",
    "GOOGL":"Technology","AMZN":"Technology","META":"Technology",
    "JPM":"Financials","GS":"Financials","V":"Financials",
    "BAC":"Financials","BLK":"Financials",
    "JNJ":"Healthcare","PFE":"Healthcare","UNH":"Healthcare",
    "ABBV":"Healthcare",
    "XOM":"Energy","CVX":"Energy","SHEL":"Energy",
    "KO":"Consumer","PEP":"Consumer","PG":"Consumer","WMT":"Consumer",
    "HSBA":"UK","AZN":"UK","GSK":"UK","ULVR":"UK",
    "VUAG":"Broad",
}

YAHOO_MAP = {
    "AAPL":"AAPL","MSFT":"MSFT","NVDA":"NVDA","GOOGL":"GOOGL",
    "AMZN":"AMZN","META":"META","JPM":"JPM","GS":"GS",
    "V":"V","BAC":"BAC","BLK":"BLK","JNJ":"JNJ","PFE":"PFE",
    "UNH":"UNH","ABBV":"ABBV","XOM":"XOM","CVX":"CVX",
    "KO":"KO","PEP":"PEP","PG":"PG","WMT":"WMT",
    "HSBA":"HSBA.L","AZN":"AZN.L","GSK":"GSK.L",
    "ULVR":"ULVR.L","SHEL":"SHEL.L","VUAG":"VUAG.L",
}

def fix_pence(price, yahoo):
    if yahoo.endswith('.L') and price > 100:
        return price / 100
    return price

def get_returns(yahoo, period="3mo"):
    try:
        hist = yf.Ticker(yahoo).history(period=period)
        if hist.empty or len(hist) < 20:
            return None
        closes = [fix_pence(float(c), yahoo) for c in hist['Close']]
        ret_1m  = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2) if len(closes) >= 21 else 0
        ret_3m  = round((closes[-1] - closes[0])  / closes[0]  * 100, 2)
        ret_1w  = round((closes[-1] - closes[-5])  / closes[-5]  * 100, 2) if len(closes) >= 5 else 0
        return {'1w': ret_1w, '1m': ret_1m, '3m': ret_3m, 'price': closes[-1]}
    except:
        return None

def calculate_rs_score(stock_ret, sector_ret, market_ret):
    """
    RS Score vs sector and market.
    Positive = outperforming. Negative = underperforming.
    """
    if not stock_ret or not sector_ret or not market_ret:
        return 0, 0, "UNKNOWN"

    vs_sector = round(stock_ret['1m'] - sector_ret['1m'], 2)
    vs_market = round(stock_ret['1m'] - market_ret['1m'], 2)

    # Multi-period confirmation
    vs_sector_3m = round(stock_ret['3m'] - sector_ret['3m'], 2)
    vs_market_3m = round(stock_ret['3m'] - market_ret['3m'], 2)

    # Composite RS score
    rs_score = 0
    if vs_market > 3:
        rs_score += 2
    elif vs_market > 0:
        rs_score += 1
    elif vs_market < -3:
        rs_score -= 2
    elif vs_market < 0:
        rs_score -= 1

    if vs_market_3m > 5:
        rs_score += 1
    elif vs_market_3m < -5:
        rs_score -= 1

    # Classify
    if rs_score >= 3:
        rs_class = "STRONG_LEADER"
    elif rs_score >= 1:
        rs_class = "LEADER"
    elif rs_score >= -1:
        rs_class = "IN_LINE"
    elif rs_score >= -2:
        rs_class = "LAGGARD"
    else:
        rs_class = "STRONG_LAGGARD"

    return vs_sector, vs_market, rs_class

def get_signal_adjustment(rs_class, signal_type):
    """Score adjustment based on relative strength."""
    if signal_type == 'TREND':
        # Trend signals — want leaders, not laggards
        adj_map = {
            'STRONG_LEADER': 2,
            'LEADER':        1,
            'IN_LINE':       0,
            'LAGGARD':      -1,
            'STRONG_LAGGARD':-2,
        }
        return adj_map.get(rs_class, 0)

    elif signal_type == 'CONTRARIAN':
        # Contrarian — laggards that are recovering can squeeze hard
        # But strong laggards in downtrends are dangerous
        adj_map = {
            'STRONG_LEADER': 1,   # Pullback in strong stock = buy the dip
            'LEADER':        1,
            'IN_LINE':       0,
            'LAGGARD':       0,   # Could recover but risky
            'STRONG_LAGGARD':-1,  # Avoid — institutional selling
        }
        return adj_map.get(rs_class, 0)

    elif signal_type == 'INVERSE':
        # Inverse ETFs — want market laggards (confirms bearish thesis)
        adj_map = {
            'STRONG_LAGGARD': 1,
            'LAGGARD':        1,
            'IN_LINE':        0,
            'LEADER':        -1,
            'STRONG_LEADER': -1,
        }
        return adj_map.get(rs_class, 0)

    return 0

def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== RELATIVE STRENGTH ANALYSIS ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Fetch benchmark returns first
    print("Fetching benchmarks...", flush=True)
    benchmarks = {}
    for sector, etf in SECTOR_BENCHMARKS.items():
        ret = get_returns(etf)
        if ret:
            benchmarks[sector] = ret
            print(f"  {sector}: 1M {ret['1m']:+.1f}% | 3M {ret['3m']:+.1f}%")

    market_ret = benchmarks.get('Broad')

    print(f"\nAnalysing instruments...\n")
    results = {}

    for name, yahoo in YAHOO_MAP.items():
        sector    = INSTRUMENT_SECTOR.get(name, 'Broad')
        sector_ret = benchmarks.get(sector, market_ret)
        stock_ret  = get_returns(yahoo)

        if not stock_ret:
            continue

        vs_sector, vs_market, rs_class = calculate_rs_score(
            stock_ret, sector_ret, market_ret
        )

        adj = get_signal_adjustment(rs_class, 'TREND')

        icon = "✅" if rs_class in ['STRONG_LEADER','LEADER'] else \
               ("🔴" if rs_class in ['STRONG_LAGGARD','LAGGARD'] else "🟡")

        print(f"  {icon} {name:6} | {rs_class:15} | vs market: {vs_market:+.1f}% | vs sector: {vs_sector:+.1f}% | 1M:{stock_ret['1m']:+.1f}%")

        results[name] = {
            'sector':       sector,
            'rs_class':     rs_class,
            'vs_sector_1m': vs_sector,
            'vs_market_1m': vs_market,
            'ret_1w':       stock_ret['1w'],
            'ret_1m':       stock_ret['1m'],
            'ret_3m':       stock_ret['3m'],
            'trend_adj':    get_signal_adjustment(rs_class, 'TREND'),
            'contra_adj':   get_signal_adjustment(rs_class, 'CONTRARIAN'),
            'inverse_adj':  get_signal_adjustment(rs_class, 'INVERSE'),
        }

    # Sort by vs_market
    sorted_results = sorted(results.items(),
                           key=lambda x: x[1]['vs_market_1m'], reverse=True)

    print(f"\n=== RELATIVE STRENGTH RANKINGS ===")
    print(f"{'Symbol':8} {'RS Class':16} {'vs Market':10} {'1M Return':10}")
    print("-" * 50)
    for sym, data in sorted_results:
        icon = "✅" if data['rs_class'] in ['STRONG_LEADER','LEADER'] else \
               ("🔴" if data['rs_class'] in ['STRONG_LAGGARD','LAGGARD'] else "🟡")
        print(f"{icon} {sym:6} {data['rs_class']:16} {data['vs_market_1m']:+8.1f}%  {data['ret_1m']:+8.1f}%")

    output = {
        'timestamp':  now.strftime('%Y-%m-%d %H:%M UTC'),
        'benchmarks': {k: v for k, v in benchmarks.items()},
        'data':       results,
    }

    atomic_write(OUTPUT_FILE, output)

    print(f"\n✅ Relative strength complete — {len(results)} instruments")
    return output

def get_rs_adjustment(instrument_name, signal_type):
    """Called by decision engine."""
    try:
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        inst = data.get('data', {}).get(instrument_name, {})
        if not inst:
            return 0, "No RS data"
        rs_class = inst.get('rs_class', 'IN_LINE')
        adj      = get_signal_adjustment(rs_class, signal_type)
        vs_mkt   = inst.get('vs_market_1m', 0)
        reason   = f"RS {rs_class} ({vs_mkt:+.1f}% vs market)"
        return adj, reason
    except:
        return 0, "RS data unavailable"

if __name__ == '__main__':
    run()
