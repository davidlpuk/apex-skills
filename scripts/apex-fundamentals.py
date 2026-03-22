#!/usr/bin/env python3
"""
Fundamental Data Module
Fetches P/E, earnings growth, dividend yield, debt/equity from FMP.
Wires into contrarian scoring — cheap quality names score higher.
"""
import json
import urllib.request
import urllib.parse
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


FUNDAMENTALS_FILE = '/home/ubuntu/.picoclaw/logs/apex-fundamentals.json'

def load_env():
    env = {}
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except:
        pass
    return env

API_KEY = load_env().get('FMP_API_KEY', '')
BASE    = 'https://financialmodelingprep.com/stable'

# Universe to fetch fundamentals for
FUNDAMENTAL_UNIVERSE = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META",
    "JPM","GS","V","BAC","BLK",
    "JNJ","PFE","UNH","ABBV",
    "XOM","CVX",
    "KO","PEP","PG","WMT",
]

def fmp_request(endpoint, params=None):
    p = params or {}
    p['apikey'] = API_KEY
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(p)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ApexBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except:
        return None

def get_profile(symbol):
    data = fmp_request('profile', {'symbol': symbol})
    if not data or not isinstance(data, list):
        return None
    p = data[0]
    return {
        'symbol':       symbol,
        'name':         p.get('companyName',''),
        'price':        float(p.get('price', 0)),
        'market_cap':   float(p.get('marketCap', 0)),
        'beta':         float(p.get('beta', 1)),
        'dividend':     float(p.get('lastDividend', 0)),
        'range_52w':    p.get('range',''),
        'industry':     p.get('industry',''),
        'exchange':     p.get('exchange',''),
    }

def get_ratios(symbol):
    # Get margins from ratios endpoint
    ratios_data = fmp_request('ratios', {'symbol': symbol})
    # Get valuation from key-metrics endpoint
    metrics_data = fmp_request('key-metrics', {'symbol': symbol})

    r = ratios_data[0] if ratios_data and isinstance(ratios_data, list) else {}
    m = metrics_data[0] if metrics_data and isinstance(metrics_data, list) else {}

    if not r and not m:
        return None

    # Calculate PE from key metrics
    mktcap = float(m.get('marketCap', 0) or 0)
    ev     = float(m.get('enterpriseValue', 0) or 0)

    # Get profile for price
    prof = fmp_request('profile', {'symbol': symbol})
    price = float(prof[0].get('price', 0)) if prof and isinstance(prof, list) else 0
    div   = float(prof[0].get('lastDividend', 0)) if prof and isinstance(prof, list) else 0
    dy    = round(div / price, 4) if price > 0 else 0

    # Net income for PE
    ev_to_ebitda = float(m.get('evToEBITDA', 0) or 0)
    current_ratio = float(m.get('currentRatio', 0) or 0)
    net_debt_ebitda = float(m.get('netDebtToEBITDA', 0) or 0)

    # Margins from ratios
    net_margin   = float(r.get('netProfitMargin', 0) or 0)
    gross_margin = float(r.get('grossProfitMargin', 0) or 0)
    op_margin    = float(r.get('operatingProfitMargin', 0) or 0)

    # FCF yield
    ev_to_fcf = float(m.get('evToFreeCashFlow', 0) or 0)
    fcf_yield = round(1 / ev_to_fcf, 4) if ev_to_fcf > 0 else 0

    # EV/EBITDA as valuation proxy (lower = cheaper)
    # Typical ranges: <10 cheap, 10-20 fair, >20 expensive
    pe_proxy = ev_to_ebitda  # Use EV/EBITDA as PE proxy

    return {
        'pe':             round(pe_proxy, 1),
        'ev_ebitda':      round(ev_to_ebitda, 1),
        'ev_fcf':         round(ev_to_fcf, 1),
        'debt_equity':    round(net_debt_ebitda, 2),
        'roe':            0,  # Not available in free tier
        'roa':            0,
        'current_ratio':  round(current_ratio, 2),
        'gross_margin':   round(gross_margin, 3),
        'net_margin':     round(net_margin, 3),
        'op_margin':      round(op_margin, 3),
        'dividend_yield': round(dy, 4),
        'fcf_yield':      round(fcf_yield, 4),
        'peg':            0,
    }

def score_fundamentals(profile, ratios):
    """
    Score fundamental quality 0-10.
    Used to boost/penalise contrarian signals.
    """
    if not profile or not ratios:
        return 5, []  # Neutral if no data

    score   = 5  # Start neutral
    reasons = []

    ev_ebitda = ratios.get('ev_ebitda', 0)
    de  = ratios.get('debt_equity', 0)
    nm  = ratios.get('net_margin', 0)
    gm  = ratios.get('gross_margin', 0)
    dy  = ratios.get('dividend_yield', 0)
    fcf = ratios.get('fcf_yield', 0)
    cr  = ratios.get('current_ratio', 0)
    beta = profile.get('beta', 1)

    # EV/EBITDA valuation (proxy for PE)
    if 0 < ev_ebitda < 10:
        score += 2
        reasons.append(f"EV/EBITDA {ev_ebitda:.1f} — cheap (below 10)")
    elif 0 < ev_ebitda < 18:
        score += 1
        reasons.append(f"EV/EBITDA {ev_ebitda:.1f} — reasonable")
    elif ev_ebitda > 30:
        score -= 1
        reasons.append(f"EV/EBITDA {ev_ebitda:.1f} — expensive")

    # Gross margin — business quality
    if gm > 0.50:
        score += 2
        reasons.append(f"Gross margin {round(gm*100,1)}% — exceptional")
    elif gm > 0.35:
        score += 1
        reasons.append(f"Gross margin {round(gm*100,1)}% — strong")

    # Debt
    if de < 0.3:
        score += 1
        reasons.append(f"D/E {de:.2f} — low debt")
    elif de > 2.0:
        score -= 1
        reasons.append(f"D/E {de:.2f} — high debt")

    # Net margin
    if nm > 0.20:
        score += 1
        reasons.append(f"Net margin {round(nm*100,1)}% — excellent")
    elif nm < 0:
        score -= 1
        reasons.append(f"Net margin {round(nm*100,1)}% — unprofitable")

    # Dividend yield bonus for contrarian
    if dy > 0.04:
        score += 1
        reasons.append(f"Dividend yield {round(dy*100,1)}% — income support")

    # FCF yield
    if fcf > 0.05:
        score += 1
        reasons.append(f"FCF yield {round(fcf*100,1)}% — strong cash generation")

    # Beta penalty for contrarian
    if beta > 1.5:
        score -= 1
        reasons.append(f"Beta {beta:.2f} — high volatility, careful with contrarian")

    score = max(0, min(10, score))
    return score, reasons

def classify_fundamental_score(score):
    if score >= 8: return "EXCEPTIONAL"
    if score >= 6: return "STRONG"
    if score >= 4: return "NEUTRAL"
    if score >= 2: return "WEAK"
    return "POOR"

def run():
    now     = datetime.now(timezone.utc)
    results = {}

    print(f"Fetching fundamentals for {len(FUNDAMENTAL_UNIVERSE)} instruments...", flush=True)

    for symbol in FUNDAMENTAL_UNIVERSE:
        print(f"  {symbol}...", flush=True)

        profile = get_profile(symbol)
        ratios  = get_ratios(symbol)

        if not profile and not ratios:
            print(f"    No data")
            continue

        fund_score, reasons = score_fundamentals(profile, ratios)
        fund_class = classify_fundamental_score(fund_score)

        results[symbol] = {
            'profile':    profile,
            'ratios':     ratios,
            'fund_score': fund_score,
            'fund_class': fund_class,
            'reasons':    reasons,
        }

        ev  = ratios.get('ev_ebitda', 0) if ratios else 0
        nm  = ratios.get('net_margin', 0) if ratios else 0
        print(f"    Score: {fund_score}/10 ({fund_class}) | EV/EBITDA: {ev:.1f} | Margin: {round(nm*100,1)}%")

    # Save
    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'count':     len(results),
        'data':      results,
    }

    atomic_write(FUNDAMENTALS_FILE, output)

    # Summary
    print(f"\n=== FUNDAMENTAL SCORES ===")
    print(f"{'Symbol':8} {'Score':8} {'Class':12} {'PE':8} {'ROE':8} {'D/E':8}")
    print("-" * 55)

    print(f"{'Symbol':8} {'Score':8} {'Class':12} {'EV/EBITDA':10} {'Margin':8} {'FCF Yield':10}")
    print("-" * 60)
    for sym, data in sorted(results.items(), key=lambda x: x[1]['fund_score'], reverse=True):
        r  = data.get('ratios', {}) or {}
        ev = r.get('ev_ebitda', 0)
        nm = r.get('net_margin', 0)
        fcf = r.get('fcf_yield', 0)
        icon = "✅" if data['fund_score'] >= 7 else ("🟡" if data['fund_score'] >= 5 else "🔴")
        print(f"{icon} {sym:6} {data['fund_score']:6}/10  {data['fund_class']:12} {ev:8.1f}   {round(nm*100,1):5}%   {round(fcf*100,2):5}%")

    print(f"\n✅ Fundamentals saved for {len(results)} instruments")
    return output

if __name__ == '__main__':
    run()
