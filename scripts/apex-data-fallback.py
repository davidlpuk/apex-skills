#!/usr/bin/env python3
"""
Data Vendor Fallback Chain
yfinance → Alpaca (US stocks) → Alpha Vantage (25 calls/day free tier)

Provides resilient price and history fetching with automatic vendor failover.
Logs which vendor succeeded to track reliability.
"""
import json
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, atomic_write, log_error, log_warning
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

VENDOR_LOG = '/home/ubuntu/.picoclaw/logs/apex-data-vendor.json'


def _get_alpaca_creds():
    """Read Alpaca credentials from env file."""
    key, secret = '', ''
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if line.startswith('ALPACA_KEY='):    key    = line.split('=',1)[1].strip()
                if line.startswith('ALPACA_SECRET='): secret = line.split('=',1)[1].strip()
    except Exception:
        key    = os.environ.get('ALPACA_KEY', '')
        secret = os.environ.get('ALPACA_SECRET', '')
    return key, secret


def _get_alpha_vantage_key():
    """Read Alpha Vantage API key from env file."""
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if line.startswith('ALPHA_VANTAGE_KEY=') or line.startswith('AV_KEY='):
                    return line.split('=',1)[1].strip()
    except Exception:
        pass
    return os.environ.get('ALPHA_VANTAGE_KEY', os.environ.get('AV_KEY', ''))


def _log_vendor_result(symbol, vendor, success, error=None):
    """Track vendor health per instrument per day."""
    now   = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    log   = safe_read(VENDOR_LOG, {'daily': {}, 'summary': {}})

    if today not in log['daily']:
        log['daily'][today] = {}
    if vendor not in log['daily'][today]:
        log['daily'][today][vendor] = {'success': 0, 'fail': 0}

    if success:
        log['daily'][today][vendor]['success'] += 1
    else:
        log['daily'][today][vendor]['fail'] += 1
        if error:
            log['daily'][today][vendor]['last_error'] = str(error)[:100]

    # Keep last 7 days
    days_sorted = sorted(log['daily'].keys(), reverse=True)
    log['daily'] = {d: log['daily'][d] for d in days_sorted[:7]}
    log['last_updated'] = now.isoformat()
    atomic_write(VENDOR_LOG, log)


def get_price_with_fallback(ticker, yahoo_ticker=None):
    """
    Fetch latest price with vendor fallback chain.
    Returns (price, vendor_used) or (None, None).

    Chain: yfinance → Alpaca (US stocks) → Alpha Vantage
    """
    yahoo = yahoo_ticker or ticker
    is_us = not yahoo.endswith('.L') and '.' not in yahoo

    # 1. yfinance
    try:
        import yfinance as yf
        hist = yf.Ticker(yahoo).history(period='2d')
        if not hist.empty:
            price = float(hist['Close'].iloc[-1])
            if yahoo.endswith('.L') and price > 100:
                price /= 100
            _log_vendor_result(ticker, 'yfinance', True)
            return round(price, 4), 'yfinance'
    except Exception as e:
        _log_vendor_result(ticker, 'yfinance', False, e)
        log_warning(f"yfinance price fetch failed for {ticker}: {e}")

    # 2. Alpaca (US stocks only)
    if is_us:
        try:
            import urllib.request
            key, secret = _get_alpaca_creds()
            if key:
                url = f"https://data.alpaca.markets/v2/stocks/{yahoo}/snapshot"
                req = urllib.request.Request(url, headers={
                    'APCA-API-KEY-ID': key,
                    'APCA-API-SECRET-KEY': secret,
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                    price = float(data.get('latestTrade', {}).get('p', 0)
                                  or data.get('minuteBar', {}).get('c', 0))
                    if price > 0:
                        _log_vendor_result(ticker, 'alpaca', True)
                        return round(price, 4), 'alpaca'
        except Exception as e:
            _log_vendor_result(ticker, 'alpaca', False, e)
            log_warning(f"Alpaca price fetch failed for {ticker}: {e}")

    # 3. Alpha Vantage (25 free calls/day)
    try:
        import urllib.request
        av_key = _get_alpha_vantage_key()
        if av_key:
            url = (f"https://www.alphavantage.co/query"
                   f"?function=GLOBAL_QUOTE&symbol={yahoo}&apikey={av_key}")
            req = urllib.request.Request(url, headers={'User-Agent': 'ApexBot/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                quote = data.get('Global Quote', {})
                price_str = quote.get('05. price', '0')
                price = float(price_str) if price_str else 0
                if price > 0:
                    _log_vendor_result(ticker, 'alpha_vantage', True)
                    return round(price, 4), 'alpha_vantage'
    except Exception as e:
        _log_vendor_result(ticker, 'alpha_vantage', False, e)
        log_error(f"Alpha Vantage price fetch failed for {ticker}: {e}")

    log_error(f"All vendors failed for {ticker}")
    return None, None


def get_history_with_fallback(ticker, yahoo_ticker=None, period="5y"):
    """
    Fetch price history with vendor fallback.
    Returns (DataFrame or list of (date, close) tuples, vendor_used).
    """
    yahoo  = yahoo_ticker or ticker
    is_us  = not yahoo.endswith('.L') and '.' not in yahoo

    # 1. yfinance (preferred — returns DataFrame)
    try:
        import yfinance as yf
        hist = yf.Ticker(yahoo).history(period=period)
        if not hist.empty and len(hist) >= 50:
            _log_vendor_result(ticker, 'yfinance', True)
            return hist, 'yfinance'
    except Exception as e:
        _log_vendor_result(ticker, 'yfinance', False, e)
        log_warning(f"yfinance history fetch failed for {ticker}: {e}")

    # 2. Alpha Vantage full daily (free, no period limit but 25 calls/day)
    try:
        import urllib.request
        av_key = _get_alpha_vantage_key()
        if av_key:
            url = (f"https://www.alphavantage.co/query"
                   f"?function=TIME_SERIES_DAILY&symbol={yahoo}"
                   f"&outputsize=full&apikey={av_key}")
            req = urllib.request.Request(url, headers={'User-Agent': 'ApexBot/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            ts = data.get('Time Series (Daily)', {})
            if ts:
                # Convert to list of (date, close) tuples sorted oldest→newest
                records = sorted([
                    (d, float(v.get('4. close', 0)))
                    for d, v in ts.items()
                    if float(v.get('4. close', 0)) > 0
                ])
                if records:
                    _log_vendor_result(ticker, 'alpha_vantage', True)
                    return records, 'alpha_vantage'
    except Exception as e:
        _log_vendor_result(ticker, 'alpha_vantage', False, e)
        log_error(f"Alpha Vantage history fetch failed for {ticker}: {e}")

    log_error(f"All vendors failed for history of {ticker}")
    return None, None


def get_vendor_health():
    """Return a summary of vendor reliability."""
    log = safe_read(VENDOR_LOG, {'daily': {}, 'summary': {}})
    daily = log.get('daily', {})
    if not daily:
        return {}

    totals = {}
    for day_data in daily.values():
        for vendor, counts in day_data.items():
            if vendor not in totals:
                totals[vendor] = {'success': 0, 'fail': 0}
            totals[vendor]['success'] += counts.get('success', 0)
            totals[vendor]['fail']    += counts.get('fail', 0)

    health = {}
    for vendor, counts in totals.items():
        total = counts['success'] + counts['fail']
        health[vendor] = {
            'success_rate': round(counts['success'] / total * 100, 1) if total else 0,
            'total_calls':  total,
        }
    return health


def run():
    """Test the fallback chain."""
    print("\n=== DATA VENDOR FALLBACK CHAIN ===")
    tests = [('AAPL', 'AAPL'), ('XOM', 'XOM'), ('VUAG', 'VUAG.L')]
    for ticker, yahoo in tests:
        price, vendor = get_price_with_fallback(ticker, yahoo)
        status = f"£{price}" if price else "FAILED"
        print(f"  {ticker:10} {status:15} (via {vendor or 'none'})")

    health = get_vendor_health()
    if health:
        print("\n  Vendor health:")
        for v, h in health.items():
            print(f"    {v:15}: {h['success_rate']}% success ({h['total_calls']} calls)")
    print("\n✅ Data fallback done")


if __name__ == '__main__':
    run()
