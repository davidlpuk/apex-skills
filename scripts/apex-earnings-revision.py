#!/usr/bin/env python3
"""
Earnings Revision Momentum (Layer 15.5)
Tracks analyst price target revisions over a 30-day rolling window.

Signal logic:
  +1.5  3+ analysts raised targets in 30 days
  +1.0  2 analysts raised targets
  -1.0  2 analysts cut targets
  -1.5  3+ analysts cut targets

Data source: FMP free tier (250 calls/day)
Cache: 24h per instrument to conserve quota.
"""
import json
import sys
import os
from datetime import datetime, timezone, timedelta

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

REVISION_FILE = '/home/ubuntu/.picoclaw/logs/apex-earnings-revision.json'
QUOTA_FILE    = '/home/ubuntu/.picoclaw/logs/apex-fmp-quota.json'
CACHE_MAX_AGE = 86400  # 24h in seconds
DAILY_LIMIT   = 230


def _quota_check_and_record():
    """Return True if quota allows another call, and record it."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        q = json.load(open(QUOTA_FILE))
        if q.get('date') != today:
            q = {'date': today, 'calls': 0, 'by_script': {}}
    except Exception:
        q = {'date': today, 'calls': 0, 'by_script': {}}
    if int(q.get('calls', 0)) >= DAILY_LIMIT:
        return False
    q['calls'] = int(q.get('calls', 0)) + 1
    q.setdefault('by_script', {})
    q['by_script']['earnings-revision'] = q['by_script'].get('earnings-revision', 0) + 1
    try:
        with open(QUOTA_FILE, 'w') as f:
            json.dump(q, f, indent=2)
    except Exception:
        pass
    return True

# T212 ticker → Yahoo ticker mapping
T212_TO_YAHOO = {
    'AAPL_US_EQ': 'AAPL', 'MSFT_US_EQ': 'MSFT', 'NVDA_US_EQ': 'NVDA',
    'AMZN_US_EQ': 'AMZN', 'GOOGL_US_EQ': 'GOOGL', 'META_US_EQ': 'META',
    'TSLA_US_EQ': 'TSLA', 'V_US_EQ': 'V',         'XOM_US_EQ': 'XOM',
    'CVX_US_EQ':  'CVX',  'JPM_US_EQ':  'JPM',    'GS_US_EQ':  'GS',
    'ABBV_US_EQ': 'ABBV', 'JNJ_US_EQ':  'JNJ',    'UNH_US_EQ': 'UNH',
    'NFLX_US_EQ': 'NFLX', 'HOOD_US_EQ': 'HOOD',   'PLTR_US_EQ': 'PLTR',
}


def _get_fmp_key():
    """Read FMP API key from env file."""
    try:
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if line.startswith('FMP_KEY=') or line.startswith('FMP_API_KEY='):
                    return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return os.environ.get('FMP_KEY', '')


def _fetch_target_summary(symbol, fmp_key):
    """
    Fetch price target summary from FMP stable endpoint.
    Returns summary dict or None.
    Uses stable/price-target-summary (replaces legacy v4/price-target).
    """
    if not fmp_key:
        return None
    if not _quota_check_and_record():
        log_warning("FMP daily quota reached — skipping earnings revision fetch")
        return None
    try:
        import urllib.request, urllib.error
        url = (f"https://financialmodelingprep.com/stable/price-target-summary"
               f"?symbol={symbol}&apikey={fmp_key}")
        req = urllib.request.Request(url, headers={'User-Agent': 'ApexBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list) and data:
                return data[0]
    except Exception as e:
        log_error(f"FMP target summary fetch failed for {symbol}: {e}")
    return None


def _analyse_revisions(summary):
    """
    Derive revision momentum from price-target-summary.
    Compares lastMonthAvgPriceTarget vs lastQuarterAvgPriceTarget to infer
    whether analysts have been raising or cutting targets recently.
    Returns (raises, cuts, net_signal, reasons).
    """
    if not summary:
        return 0, 0, 0.0, []

    month_avg   = float(summary.get('lastMonthAvgPriceTarget') or 0)
    quarter_avg = float(summary.get('lastQuarterAvgPriceTarget') or 0)
    month_count = int(summary.get('lastMonthCount') or 0)

    if month_avg <= 0 or quarter_avg <= 0 or month_count == 0:
        return 0, 0, 0.0, []

    pct_change = (month_avg - quarter_avg) / quarter_avg * 100

    raises = 0
    cuts   = 0
    signal = 0.0
    reasons = []

    if pct_change >= 6 and month_count >= 3:
        raises = 3
        signal = 1.5
        reasons.append(f"{month_count} analysts raised targets +{pct_change:.1f}% vs quarterly avg (${quarter_avg:.0f}→${month_avg:.0f})")
    elif pct_change >= 3 and month_count >= 2:
        raises = 2
        signal = 1.0
        reasons.append(f"{month_count} analysts raised targets +{pct_change:.1f}% vs quarterly avg (${quarter_avg:.0f}→${month_avg:.0f})")
    elif pct_change <= -6 and month_count >= 3:
        cuts = 3
        signal = -1.5
        reasons.append(f"{month_count} analysts cut targets {pct_change:.1f}% vs quarterly avg (${quarter_avg:.0f}→${month_avg:.0f})")
    elif pct_change <= -3 and month_count >= 2:
        cuts = 2
        signal = -1.0
        reasons.append(f"{month_count} analysts cut targets {pct_change:.1f}% vs quarterly avg (${quarter_avg:.0f}→${month_avg:.0f})")

    return raises, cuts, signal, reasons


def get_revision_momentum(instrument_name, t212_ticker='', signal_type='TREND'):
    """
    Main entry point — called by scoring layer.
    Returns (adjustment, reasons_list).
    """
    # Resolve to Yahoo symbol
    yahoo = T212_TO_YAHOO.get(t212_ticker, '')
    if not yahoo:
        # Try stripping suffix
        clean = t212_ticker.replace('_US_EQ', '').replace('_EQ', '').replace('l_EQ', '')
        yahoo = clean if clean and not clean.endswith('.L') else ''

    if not yahoo:
        return 0.0, []

    # Check cache
    cache = safe_read(REVISION_FILE, {'instruments': {}})
    cached = cache.get('instruments', {}).get(yahoo, {})
    now = datetime.now(timezone.utc)

    if cached.get('fetched_at'):
        try:
            age = (now - datetime.fromisoformat(cached['fetched_at'])).total_seconds()
            if age < CACHE_MAX_AGE:
                adj = cached.get('adjustment', 0.0)
                rsns = cached.get('reasons', [])
                return adj, rsns
        except Exception:
            pass

    # Fetch fresh data
    fmp_key = _get_fmp_key()
    summary = _fetch_target_summary(yahoo, fmp_key)

    raises, cuts, adjustment, reasons = _analyse_revisions(summary)

    # Store in cache
    if 'instruments' not in cache:
        cache['instruments'] = {}
    cache['instruments'][yahoo] = {
        'fetched_at':  now.isoformat(),
        'raises':      raises,
        'cuts':        cuts,
        'adjustment':  adjustment,
        'reasons':     reasons[:5],
    }
    cache['last_updated'] = now.isoformat()
    atomic_write(REVISION_FILE, cache)

    return adjustment, reasons[:3]


def run():
    """Test run for a few instruments."""
    print("\n=== EARNINGS REVISION MOMENTUM ===")
    test_stocks = [
        ('Apple', 'AAPL_US_EQ'),
        ('NVIDIA', 'NVDA_US_EQ'),
        ('Exxon', 'XOM_US_EQ'),
    ]
    for name, ticker in test_stocks:
        adj, reasons = get_revision_momentum(name, ticker)
        print(f"  {name}: {adj:+.1f}")
        for r in reasons[:2]:
            print(f"    • {r}")
    print("\n✅ Earnings revision momentum done")


if __name__ == '__main__':
    run()
