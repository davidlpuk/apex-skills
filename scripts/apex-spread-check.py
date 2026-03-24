#!/usr/bin/env python3
"""
Pre-trade Bid-Ask Spread Measurement
T212's "zero commission" hides spread cost. This measures it before each trade.

Spread verdicts:
  TIGHT  < 0.10%  — proceed normally
  NORMAL 0.10–0.30% — use limit at mid-price
  WIDE   0.30–0.80% — use limit, flag in Telegram
  BLOCK  > 0.80%  — skip trade, spread too wide

Fallback chain: yfinance → Alpaca (US stocks)
Log: apex-spread-log.json (last 500 entries)
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, atomic_write, log_error, log_warning, send_telegram
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
    def send_telegram(m): print(f'TELEGRAM: {m[:80]}')

SPREAD_LOG = '/home/ubuntu/.picoclaw/logs/apex-spread-log.json'

# T212 → Yahoo ticker map
T212_TO_YAHOO = {
    'AAPL_US_EQ': 'AAPL', 'MSFT_US_EQ': 'MSFT', 'NVDA_US_EQ': 'NVDA',
    'AMZN_US_EQ': 'AMZN', 'GOOGL_US_EQ': 'GOOGL', 'META_US_EQ': 'META',
    'TSLA_US_EQ': 'TSLA', 'V_US_EQ': 'V',         'XOM_US_EQ': 'XOM',
    'CVX_US_EQ':  'CVX',  'JPM_US_EQ':  'JPM',    'GS_US_EQ':  'GS',
    'ABBV_US_EQ': 'ABBV', 'JNJ_US_EQ':  'JNJ',    'UNH_US_EQ': 'UNH',
    'NFLX_US_EQ': 'NFLX', 'HOOD_US_EQ': 'HOOD',   'PLTR_US_EQ': 'PLTR',
    'VUAGl_EQ':   'VUAG.L', 'QQQSl_EQ': 'QQQS.L', 'SHEL_EQ': 'SHEL.L',
    'HSBA_EQ':    'HSBA.L', 'AZN_EQ':   'AZN.L',  'ULVR_EQ':  'ULVR.L',
}


def _get_spread_yfinance(yahoo_ticker):
    """Try fetching bid/ask from yfinance .info dict."""
    try:
        import yfinance as yf
        t = yf.Ticker(yahoo_ticker)
        info = t.info
        bid = float(info.get('bid', 0) or 0)
        ask = float(info.get('ask', 0) or 0)
        if bid > 0 and ask > 0 and ask > bid:
            return bid, ask, 'yfinance'
    except Exception as e:
        log_error(f"yfinance spread fetch failed for {yahoo_ticker}: {e}")
    return None, None, None


def _get_spread_alpaca(yahoo_ticker):
    """Try fetching bid/ask from Alpaca snapshot (US stocks only)."""
    try:
        import urllib.request
        import os
        key    = os.environ.get('ALPACA_KEY', '')
        secret = os.environ.get('ALPACA_SECRET', '')
        if not key:
            with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('ALPACA_KEY='): key = line.split('=',1)[1].strip()
                    if line.startswith('ALPACA_SECRET='): secret = line.split('=',1)[1].strip()
        if not key:
            return None, None, None

        url = f"https://data.alpaca.markets/v2/stocks/{yahoo_ticker}/snapshot"
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID': key,
            'APCA-API-SECRET-KEY': secret,
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            quote = data.get('latestQuote', {})
            bid = float(quote.get('bp', 0) or 0)
            ask = float(quote.get('ap', 0) or 0)
            if bid > 0 and ask > 0 and ask > bid:
                return bid, ask, 'alpaca'
    except Exception as e:
        log_error(f"Alpaca spread fetch failed for {yahoo_ticker}: {e}")
    return None, None, None


def check_spread(signal):
    """
    Check bid-ask spread for a signal before trade execution.
    Returns (verdict, spread_pct, mid_price, details_dict).

    Verdicts: TIGHT | NORMAL | WIDE | BLOCK | UNKNOWN
    """
    name    = signal.get('name', '?')
    ticker  = signal.get('t212_ticker', '')
    entry   = float(signal.get('entry', 0))
    now     = datetime.now(timezone.utc)

    # Resolve to Yahoo ticker
    yahoo = T212_TO_YAHOO.get(ticker, '')
    if not yahoo:
        clean = ticker.replace('_US_EQ', '').replace('_EQ', '').replace('l_EQ', '')
        yahoo = clean

    if not yahoo:
        return 'UNKNOWN', 0.0, entry, {'reason': 'Cannot resolve ticker'}

    # Try yfinance first, then Alpaca
    bid, ask, source = _get_spread_yfinance(yahoo)
    if bid is None:
        bid, ask, source = _get_spread_alpaca(yahoo)

    if bid is None or ask is None:
        # No live data — use a conservative estimate based on instrument type
        is_usd = '_US_EQ' in ticker
        is_lev = any(x in ticker for x in ['SQQQ', 'SPXU', '3USS', 'QQQS'])
        if is_lev:
            est_spread = 0.50
        elif is_usd:
            est_spread = 0.08  # US large cap — typically tight
        else:
            est_spread = 0.20  # UK — slightly wider
        return 'UNKNOWN', est_spread, entry, {
            'reason': 'No live bid/ask available — estimated spread used',
            'estimated_spread_pct': est_spread,
        }

    mid_price  = round((bid + ask) / 2, 4)
    spread_pct = round((ask - bid) / mid_price * 100, 4) if mid_price > 0 else 0

    # Classify spread
    if spread_pct < 0.10:
        verdict = 'TIGHT'
    elif spread_pct < 0.30:
        verdict = 'NORMAL'
    elif spread_pct < 0.80:
        verdict = 'WIDE'
    else:
        verdict = 'BLOCK'

    details = {
        'bid':        bid,
        'ask':        ask,
        'mid':        mid_price,
        'spread_pct': spread_pct,
        'source':     source,
        'verdict':    verdict,
    }

    # Log to spread history
    log = safe_read(SPREAD_LOG, {'entries': []})
    entries = log.get('entries', [])
    entries.append({
        'timestamp':  now.isoformat(),
        'name':       name,
        'ticker':     ticker,
        'bid':        bid,
        'ask':        ask,
        'mid':        mid_price,
        'spread_pct': spread_pct,
        'source':     source,
        'verdict':    verdict,
    })
    log['entries'] = entries[-500:]
    log['last_updated'] = now.isoformat()
    atomic_write(SPREAD_LOG, log)

    # Alert on wide spread
    if verdict == 'WIDE':
        send_telegram(
            f"⚠️ WIDE SPREAD — {name}\n\n"
            f"Spread: {spread_pct:.3f}% (bid {bid} / ask {ask})\n"
            f"Using limit at mid-price £{mid_price}\n"
            f"Source: {source}"
        )
        log_warning(f"Wide spread on {name}: {spread_pct:.3f}%")
    elif verdict == 'BLOCK':
        send_telegram(
            f"🔴 SPREAD BLOCK — {name}\n\n"
            f"Spread: {spread_pct:.3f}% exceeds 0.80% threshold\n"
            f"Bid: {bid} | Ask: {ask}\n"
            f"Skipping trade — spread too wide."
        )
        log_warning(f"Spread BLOCK on {name}: {spread_pct:.3f}%")

    return verdict, spread_pct, mid_price, details


def run():
    """Test spread check on a few instruments."""
    print("\n=== SPREAD CHECK ===")
    test_signals = [
        {'name': 'Apple', 't212_ticker': 'AAPL_US_EQ', 'entry': 200.0},
        {'name': 'NVIDIA', 't212_ticker': 'NVDA_US_EQ', 'entry': 120.0},
        {'name': 'Exxon', 't212_ticker': 'XOM_US_EQ', 'entry': 105.0},
    ]
    for sig in test_signals:
        verdict, spread_pct, mid, details = check_spread(sig)
        print(f"  {sig['name']:15} spread={spread_pct:.4f}% → {verdict} (mid={mid})")
    print("\n✅ Spread check done")


if __name__ == '__main__':
    run()
