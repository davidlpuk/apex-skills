#!/usr/bin/env python3
"""
Advanced Fundamental Signals
1. Earnings revisions (via analyst price target trend)
2. Insider buying (via SEC EDGAR)
3. Dividend safety (payout ratio)
4. Short interest (via yfinance)
5. Earnings quality / accruals ratio (via FMP cash flow)
"""
import json
import urllib.request
import urllib.parse
import yfinance as yf
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


OUTPUT_FILE = '/home/ubuntu/.picoclaw/logs/apex-fundamental-signals.json'

def _get_api_key():
    try:
        from apex_config import get_env
        return get_env('FMP_API_KEY', '')
    except ImportError:
        try:
            with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
                for line in f:
                    if line.strip().startswith('FMP_API_KEY='):
                        return line.strip().split('=', 1)[1]
        except Exception:
            pass
        return ''

API_KEY = _get_api_key()
BASE    = 'https://financialmodelingprep.com/stable'

UNIVERSE = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META",
    "JPM","GS","V","BAC","BLK",
    "JNJ","PFE","UNH","ABBV",
    "XOM","CVX",
    "KO","PEP","PG","WMT",
]

YAHOO_MAP = {
    "AAPL":"AAPL","MSFT":"MSFT","NVDA":"NVDA","GOOGL":"GOOGL",
    "AMZN":"AMZN","META":"META","JPM":"JPM","GS":"GS",
    "V":"V","BAC":"BAC","BLK":"BLK","JNJ":"JNJ","PFE":"PFE",
    "UNH":"UNH","ABBV":"ABBV","XOM":"XOM","CVX":"CVX",
    "KO":"KO","PEP":"PEP","PG":"PG","WMT":"WMT",
}

_fmp_call_count = 0

def fmp_request(endpoint, params=None):
    global _fmp_call_count
    import time

    # Rate limit — max 200 calls per run, 0.5s between calls
    _fmp_call_count += 1
    if _fmp_call_count > 200:
        print(f"  ⚠️ FMP rate limit guard — stopping at {_fmp_call_count} calls")
        return None
    if _fmp_call_count > 1:
        time.sleep(0.5)

    p = params or {}
    p['apikey'] = API_KEY
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(p)
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'ApexBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            # Detect rate limit response
            if isinstance(data, dict) and 'Error Message' in data:
                if 'Limit' in data['Error Message']:
                    print(f"  ⚠️ FMP daily limit reached — {_fmp_call_count} calls used")
                    return None
            return data
    except:
        return None

# ============================================================
# 1. EARNINGS REVISIONS via analyst price target trend
# ============================================================
def get_earnings_revisions(symbol):
    """
    Uses analyst price target trend as earnings revision proxy.
    Rising targets = analysts revising earnings up = bullish.
    Falling targets = analysts revising earnings down = bearish.
    """
    data = fmp_request('price-target-summary', {'symbol': symbol})
    if not data or not isinstance(data, list):
        return None

    d = data[0]
    last_month  = float(d.get('lastMonthAvgPriceTarget', 0) or 0)
    last_qtr    = float(d.get('lastQuarterAvgPriceTarget', 0) or 0)
    last_year   = float(d.get('lastYearAvgPriceTarget', 0) or 0)
    last_month_count = int(d.get('lastMonthCount', 0) or 0)

    if last_year == 0 or last_month == 0:
        return None

    # Revision momentum: where are targets going
    qtr_vs_year   = round((last_qtr - last_year) / last_year * 100, 1) if last_year else 0
    month_vs_qtr  = round((last_month - last_qtr) / last_qtr * 100, 1) if last_qtr else 0
    month_vs_year = round((last_month - last_year) / last_year * 100, 1) if last_year else 0

    # Classify revision trend
    if month_vs_year > 10 and month_vs_qtr > 0:
        trend   = "STRONG_UP"
        signal  = 2
        note    = f"Targets up {month_vs_year}% YoY — analysts revising up aggressively"
    elif month_vs_year > 3:
        trend   = "UP"
        signal  = 1
        note    = f"Targets up {month_vs_year}% YoY — positive revision trend"
    elif month_vs_year < -10 and month_vs_qtr < 0:
        trend   = "STRONG_DOWN"
        signal  = -2
        note    = f"Targets down {month_vs_year}% YoY — analysts cutting estimates aggressively"
    elif month_vs_year < -3:
        trend   = "DOWN"
        signal  = -1
        note    = f"Targets down {month_vs_year}% YoY — negative revision trend"
    else:
        trend   = "FLAT"
        signal  = 0
        note    = f"Targets flat YoY — no clear revision trend"

    return {
        'last_month_target':  round(last_month, 2),
        'last_qtr_target':    round(last_qtr, 2),
        'last_year_target':   round(last_year, 2),
        'analyst_count_month':last_month_count,
        'month_vs_year_pct':  month_vs_year,
        'month_vs_qtr_pct':   month_vs_qtr,
        'trend':              trend,
        'signal':             signal,
        'note':               note,
    }

# ============================================================
# 2. INSIDER BUYING via SEC EDGAR
# ============================================================
def get_insider_signal(symbol):
    """
    Fetches SEC Form 4 insider transactions from EDGAR.
    Cluster of buys in last 90 days = strong bullish signal.
    """
    try:
        # EDGAR full-text search for Form 4
        # Use FMP insider trading with transaction type filter
        url = f"https://financialmodelingprep.com/stable/insider-trading?symbol={symbol}&limit=50&apikey={API_KEY}"
        req = urllib.request.Request(url, headers={
            'User-Agent': 'ApexBot research@apex.com',
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        # Filter to actual purchases (P) in last 90 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        purchases = []
        sales     = []

        if isinstance(data, list):
            for tx in data:
                tx_type = tx.get('transactionType', '')
                tx_date = tx.get('transactionDate', '')
                value   = float(tx.get('securitiesTransacted', 0) or 0)
                price   = float(tx.get('price', 0) or 0)

                try:
                    dt = datetime.fromisoformat(tx_date).replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except:
                    continue

                if tx_type == 'P-Purchase':
                    purchases.append({'value': value * price, 'name': tx.get('reportingName','')})
                elif tx_type == 'S-Sale':
                    sales.append({'value': value * price})

        buy_value  = sum(p['value'] for p in purchases)
        sell_value = sum(s['value'] for s in sales)
        net_value  = buy_value - sell_value

        if len(purchases) >= 3 or buy_value > 1000000:
            signal = 2
            trend  = "STRONG_BUY"
            note   = f"{len(purchases)} insider purchases (${round(buy_value/1000,0)}k) in 90 days — cluster buy signal"
        elif len(purchases) >= 1:
            signal = 1
            trend  = "BUY"
            note   = f"{len(purchases)} insider purchase(s) in 90 days — positive signal"
        elif len(sales) > len(purchases) * 3:
            signal = -1
            trend  = "SELLING"
            note   = f"{len(sales)} insider sales vs {len(purchases)} purchases — net selling"
        else:
            signal = 0
            trend  = "NEUTRAL"
            note   = f"No significant insider buying in 90 days"

        return {
            'purchases_90d':  len(purchases),
            'sales_90d':      len(sales),
            'buy_value':      round(buy_value, 0),
            'sell_value':     round(sell_value, 0),
            'trend':          trend,
            'signal':         signal,
            'note':           note,
            'source':         'FMP insider'
        }

    except Exception as e:
        # Fallback — use yfinance institutional ownership change as proxy
        try:
            t    = yf.Ticker(YAHOO_MAP.get(symbol, symbol))
            info = t.info
            inst_pct = float(info.get('heldPercentInstitutions', 0) or 0) * 100
            return {
                'form4_count_90d': 0,
                'inst_ownership':  round(inst_pct, 1),
                'trend':           'UNKNOWN',
                'signal':          0,
                'note':            f"Institutional ownership: {round(inst_pct,1)}%",
                'source':          'yfinance fallback'
            }
        except:
            return None

# ============================================================
# 3. DIVIDEND SAFETY via payout ratio
# ============================================================
def get_dividend_safety(symbol):
    """
    Checks dividend safety using payout ratio and coverage.
    Payout ratio > 80% = danger. > 100% = cut likely.
    """
    # Get dividend data
    div_data = fmp_request('dividends', {'symbol': symbol, 'limit': 4})
    # Get income statement for EPS
    income   = fmp_request('income-statement', {'symbol': symbol, 'limit': 1})

    if not div_data or not isinstance(div_data, list):
        return {'has_dividend': False, 'signal': 0, 'note': 'No dividend'}

    latest_div = div_data[0] if div_data else {}
    # Sum last 4 dividends for annual total
    annual_div = sum(float(d.get('adjDividend', 0) or 0) for d in div_data[:4])

    if annual_div == 0:
        return {'has_dividend': False, 'signal': 0, 'note': 'No dividend paid'}

    # Get EPS from income statement
    eps = 0
    fcf_per_share = 0
    if income and isinstance(income, list):
        inc = income[0]
        eps = float(inc.get('eps', 0) or 0)
        shares = float(inc.get('weightedAverageShsOut', 1) or 1)
        fcf = float(inc.get('operatingCashFlow', 0) or 0) - float(inc.get('capitalExpenditure', 0) or 0)
        fcf_per_share = round(fcf / shares, 2) if shares > 0 else 0

    # Payout ratio
    payout_ratio = round(annual_div / eps * 100, 1) if eps > 0 else 999
    fcf_payout   = round(annual_div / fcf_per_share * 100, 1) if fcf_per_share > 0 else 999

    # Dividend yield
    div_yield = float(latest_div.get('yield', 0) or 0)

    # Fix div_yield — should be percentage not decimal*100
    div_yield_pct_display = round(div_yield * 100, 2) if div_yield < 1 else round(div_yield, 2)

    # Safety classification — use payout_ratio as primary, fcf_payout as secondary
    # fcf_payout 999 means no FCF data — don't use it as danger signal
    fcf_danger = fcf_payout > 100 and fcf_payout != 999

    if payout_ratio > 100:
        safety  = "DANGER"
        signal  = -2
        note    = f"Payout ratio {payout_ratio}% — dividend cut likely"
    elif payout_ratio > 80 or fcf_danger:
        safety  = "STRETCHED"
        signal  = -1
        note    = f"Payout ratio {payout_ratio}% — stretched, monitor closely"
    elif payout_ratio > 50:
        safety  = "MODERATE"
        signal  = 0
        note    = f"Payout ratio {payout_ratio}% — sustainable"
    elif payout_ratio > 0:
        safety  = "SAFE"
        signal  = 1
        note    = f"Payout ratio {payout_ratio}% — well covered"
    else:
        safety  = "UNKNOWN"
        signal  = 0
        note    = "Cannot calculate payout ratio"

    return {
        'has_dividend':   True,
        'annual_div':     round(annual_div, 2),
        'div_yield_pct':  div_yield_pct_display,
        'eps':            round(eps, 2),
        'payout_ratio':   payout_ratio,
        'fcf_payout':     fcf_payout,
        'safety':         safety,
        'signal':         signal,
        'note':           note,
    }

# ============================================================
# 4. SHORT INTEREST via yfinance
# ============================================================
def get_short_interest(symbol):
    """
    Short interest as % of float.
    High short interest on recovering stock = squeeze potential.
    High short interest on deteriorating stock = confirms bear case.
    """
    try:
        t    = yf.Ticker(YAHOO_MAP.get(symbol, symbol))
        info = t.info

        short_pct   = float(info.get('shortPercentOfFloat', 0) or 0) * 100
        short_ratio = float(info.get('shortRatio', 0) or 0)  # Days to cover
        shares_short= int(info.get('sharesShort', 0) or 0)
        float_shares= int(info.get('floatShares', 1) or 1)

        # Classification
        if short_pct > 20:
            level   = "VERY_HIGH"
            note    = f"{round(short_pct,1)}% short — extreme short interest, squeeze potential on recovery"
            # High short interest is bullish for contrarian (squeeze) but bearish signal overall
            squeeze_potential = True
        elif short_pct > 10:
            level   = "HIGH"
            note    = f"{round(short_pct,1)}% short — elevated, smart money bearish"
            squeeze_potential = True
        elif short_pct > 5:
            level   = "MODERATE"
            note    = f"{round(short_pct,1)}% short — moderate short interest"
            squeeze_potential = False
        elif short_pct > 0:
            level   = "LOW"
            note    = f"{round(short_pct,1)}% short — low short interest, not a crowded short"
            squeeze_potential = False
        else:
            level   = "UNKNOWN"
            note    = "Short interest data unavailable"
            squeeze_potential = False

        # Signal depends on context
        # For CONTRARIAN signals: high short = squeeze potential = boost
        # For TREND signals: high short = smart money bearish = penalty
        contrarian_signal = 1 if short_pct > 10 else 0
        trend_signal      = -1 if short_pct > 15 else 0

        return {
            'short_pct_float':    round(short_pct, 2),
            'days_to_cover':      round(short_ratio, 1),
            'shares_short':       shares_short,
            'level':              level,
            'squeeze_potential':  squeeze_potential,
            'contrarian_signal':  contrarian_signal,
            'trend_signal':       trend_signal,
            'note':               note,
        }
    except Exception as e:
        return {'short_pct_float': 0, 'level': 'UNKNOWN', 'signal': 0, 'note': str(e)}

# ============================================================
# 5. EARNINGS QUALITY / ACCRUALS RATIO
# ============================================================
def get_earnings_quality(symbol):
    """
    Accruals ratio = (Net Income - Operating Cash Flow) / Total Assets
    Low/negative accruals = earnings backed by cash = HIGH QUALITY
    High accruals = earnings not backed by cash = RED FLAG
    """
    cf_data     = fmp_request('cash-flow-statement', {'symbol': symbol, 'limit': 2})
    bal_data    = fmp_request('balance-sheet-statement', {'symbol': symbol, 'limit': 1})
    income_data = fmp_request('income-statement', {'symbol': symbol, 'limit': 1})

    if not cf_data or not isinstance(cf_data, list):
        return None

    cf   = cf_data[0]
    bal  = bal_data[0] if bal_data and isinstance(bal_data, list) else {}
    inc  = income_data[0] if income_data and isinstance(income_data, list) else {}

    net_income = float(cf.get('netIncome', 0) or 0)
    op_cf      = float(cf.get('operatingCashFlow', 0) or 0)
    capex      = float(cf.get('capitalExpenditure', 0) or 0)
    total_assets = float(bal.get('totalAssets', 1) or 1)
    fcf        = op_cf - abs(capex)

    # Accruals ratio
    accruals       = net_income - op_cf
    accruals_ratio = round(accruals / total_assets * 100, 2) if total_assets > 0 else 0

    # FCF vs Net Income ratio
    fcf_to_ni = round(fcf / net_income, 2) if net_income > 0 else 0

    # Cash conversion cycle proxy
    # High FCF/NI = earnings converting to cash = high quality
    # Low FCF/NI = earnings not converting = low quality

    # Classification
    if accruals_ratio < -5:
        quality  = "EXCEPTIONAL"
        signal   = 2
        note     = f"Accruals {accruals_ratio}% — cash earnings exceed reported earnings, exceptional quality"
    elif accruals_ratio < 0:
        quality  = "HIGH"
        signal   = 1
        note     = f"Accruals {accruals_ratio}% — earnings well backed by cash flow"
    elif accruals_ratio < 3:
        quality  = "MODERATE"
        signal   = 0
        note     = f"Accruals {accruals_ratio}% — acceptable earnings quality"
    elif accruals_ratio < 8:
        quality  = "LOW"
        signal   = -1
        note     = f"Accruals {accruals_ratio}% — earnings not fully backed by cash, caution"
    else:
        quality  = "POOR"
        signal   = -2
        note     = f"Accruals {accruals_ratio}% — significant gap between earnings and cash, red flag"

    return {
        'net_income':      round(net_income/1e9, 2),
        'operating_cf':    round(op_cf/1e9, 2),
        'fcf':             round(fcf/1e9, 2),
        'accruals_ratio':  accruals_ratio,
        'fcf_to_ni':       fcf_to_ni,
        'quality':         quality,
        'signal':          signal,
        'note':            note,
    }

# ============================================================
# COMPOSITE FUNDAMENTAL SCORE
# ============================================================
def calculate_composite_score(revisions, insider, dividend, short, accruals):
    """
    Combine all 5 signals into a composite fundamental score.
    +/- adjustments applied to decision engine signal scoring.
    """
    score   = 0
    reasons = []

    if revisions:
        score += revisions['signal']
        if revisions['signal'] != 0:
            reasons.append(f"Revisions: {revisions['trend']} ({revisions['note']})")

    if insider and insider['signal'] != 0:
        score += insider['signal']
        reasons.append(f"Insider: {insider['note']}")

    if dividend and dividend.get('has_dividend'):
        score += dividend['signal']
        if dividend['signal'] != 0:
            reasons.append(f"Dividend: {dividend['safety']} ({dividend['note']})")

    # Short interest — context dependent, store for decision engine
    if short:
        reasons.append(f"Short interest: {short['level']} ({short['note'][:60]})")

    if accruals:
        score += accruals['signal']
        if accruals['signal'] != 0:
            reasons.append(f"Earnings quality: {accruals['quality']} ({accruals['note'][:60]})")

    # Classify composite
    if score >= 4:
        composite = "STRONG_BUY_SIGNAL"
    elif score >= 2:
        composite = "BUY_SIGNAL"
    elif score >= 0:
        composite = "NEUTRAL"
    elif score >= -2:
        composite = "CAUTION"
    else:
        composite = "AVOID"

    return score, composite, reasons

# ============================================================
# MAIN RUN
# ============================================================
def run():
    now     = datetime.now(timezone.utc)
    results = {}

    print(f"\n=== APEX FUNDAMENTAL SIGNALS ===")
    print(f"Running 5-factor analysis on {len(UNIVERSE)} instruments...\n")

    for symbol in UNIVERSE:
        print(f"  {symbol}...", flush=True)

        revisions = get_earnings_revisions(symbol)
        insider   = get_insider_signal(symbol)
        dividend  = get_dividend_safety(symbol)
        short     = get_short_interest(symbol)
        accruals  = get_earnings_quality(symbol)

        comp_score, composite, reasons = calculate_composite_score(
            revisions, insider, dividend, short, accruals
        )

        results[symbol] = {
            'composite_score': comp_score,
            'composite':       composite,
            'reasons':         reasons,
            'revisions':       revisions,
            'insider':         insider,
            'dividend':        dividend,
            'short_interest':  short,
            'accruals':        accruals,
        }

        rev_trend = revisions['trend'] if revisions else 'N/A'
        div_safe  = dividend['safety'] if dividend and dividend.get('has_dividend') else 'NO DIV'
        short_lvl = short['level'] if short else 'N/A'
        qual      = accruals['quality'] if accruals else 'N/A'
        icon      = "✅" if comp_score >= 2 else ("🟡" if comp_score >= 0 else "🔴")

        print(f"  {icon} {symbol:6} | composite:{comp_score:+d} | rev:{rev_trend:10} | div:{div_safe:10} | short:{short_lvl:8} | quality:{qual}")

    # Save
    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'count':     len(results),
        'data':      results,
    }

    atomic_write(OUTPUT_FILE, output)

    # Summary — best and worst
    sorted_results = sorted(results.items(), key=lambda x: x[1]['composite_score'], reverse=True)

    print(f"\n=== COMPOSITE RANKINGS ===")
    print(f"{'Symbol':8} {'Score':8} {'Composite':20} {'Key reason'}")
    print("-" * 75)
    for sym, data in sorted_results:
        icon = "✅" if data['composite_score'] >= 2 else ("🟡" if data['composite_score'] >= 0 else "🔴")
        reason = data['reasons'][0][:45] if data['reasons'] else '—'
        print(f"{icon} {sym:6} {data['composite_score']:+6}     {data['composite']:20} {reason}")

    print(f"\n✅ Fundamental signals saved for {len(results)} instruments")
    return output

if __name__ == '__main__':
    run()
