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
CACHE_MAX_AGE = 86400  # 24h in seconds

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


def _fetch_price_targets(symbol, fmp_key):
    """
    Fetch analyst price targets from FMP.
    Returns list of {date, priceTarget, analystName} dicts or [].
    """
    if not fmp_key:
        return []
    try:
        import urllib.request
        url = (f"https://financialmodelingprep.com/api/v4/price-target"
               f"?symbol={symbol}&apikey={fmp_key}&limit=50")
        req = urllib.request.Request(url, headers={'User-Agent': 'ApexBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list):
                return data
    except Exception as e:
        log_error(f"FMP price targets fetch failed for {symbol}: {e}")
    return []


def _analyse_revisions(targets, days=30):
    """
    Analyse price target revisions over the last N days.
    Returns (raises, cuts, net_signal, reasons).
    """
    if not targets:
        return 0, 0, 0.0, []

    now        = datetime.now(timezone.utc)
    cutoff     = now - timedelta(days=days)

    raises = 0
    cuts   = 0
    reasons = []

    # Sort by date descending
    sorted_targets = sorted(targets, key=lambda x: x.get('publishedDate', ''), reverse=True)

    # Group by analyst to track changes
    by_analyst = {}
    for t in sorted_targets:
        analyst = t.get('analystName') or t.get('analystCompany', 'Unknown')
        date_str = t.get('publishedDate', '')
        if not date_str:
            continue
        try:
            t_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            if t_date.tzinfo is None:
                t_date = t_date.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        target_price = float(t.get('priceTarget', 0) or 0)
        if target_price <= 0:
            continue

        if analyst not in by_analyst:
            by_analyst[analyst] = []
        by_analyst[analyst].append({'date': t_date, 'price': target_price})

    # Check each analyst's most recent change within the window
    for analyst, history in by_analyst.items():
        if len(history) < 2:
            # Only one data point — check if it's a recent new coverage
            if history[0]['date'] >= cutoff:
                pass  # New coverage, not a revision
            continue

        # Most recent vs previous
        recent   = history[0]
        previous = history[1]

        if recent['date'] < cutoff:
            continue  # Most recent change is outside the window

        change = recent['price'] - previous['price']
        if change > 0:
            raises += 1
            reasons.append(f"{analyst[:20]} raised target ${previous['price']:.0f}→${recent['price']:.0f}")
        elif change < 0:
            cuts += 1
            reasons.append(f"{analyst[:20]} cut target ${previous['price']:.0f}→${recent['price']:.0f}")

    # Net signal
    net = raises - cuts
    if raises >= 3:
        signal = 1.5
    elif raises == 2:
        signal = 1.0
    elif cuts >= 3:
        signal = -1.5
    elif cuts == 2:
        signal = -1.0
    else:
        signal = 0.0

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
    targets = _fetch_price_targets(yahoo, fmp_key)

    raises, cuts, adjustment, reasons = _analyse_revisions(targets)

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
