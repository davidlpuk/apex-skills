#!/usr/bin/env python3
"""
Apex Pairwise Correlation Update
Computes 90-day rolling pairwise correlation between all instruments in the
quality universe and writes apex-pairwise-corr.json for the position sizer.

Runs nightly (07:05 UTC) so fresh data is available before the morning scan.
The sizer reads this file; if absent or >48h stale, it falls back to a
sector-proxy correlation of 0.72 for same-sector instruments.
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f:
            json.dump(d, f, indent=2)
        def log_warning(m): print(f'WARNING: {m}')
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}

QUALITY_FILE = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'
OUTPUT_FILE  = '/home/ubuntu/.picoclaw/logs/apex-pairwise-corr.json'
LOOKBACK     = '6mo'   # 6-month rolling window
MAX_TICKERS  = 40      # cap API calls — pick most-active universe members


def fetch_correlations(tickers):
    """
    Download 6-month closing prices for all tickers and compute
    pairwise Pearson correlation. Returns dict of 'T1:T2' → float.
    """
    try:
        import yfinance as yf
        import math
    except ImportError:
        log_warning("yfinance not available — skipping correlation update")
        return {}

    print(f"  Fetching {len(tickers)} tickers ({LOOKBACK} history)...")
    try:
        raw = yf.download(tickers, period=LOOKBACK, auto_adjust=True,
                          progress=False, threads=True)
        if hasattr(raw, 'columns') and isinstance(raw.columns, type(raw.columns)):
            # Multi-ticker download
            try:
                prices = raw['Close']
            except Exception:
                prices = raw
        else:
            prices = raw
    except Exception as e:
        log_warning(f"yfinance download failed: {e}")
        return {}

    try:
        returns = prices.pct_change().dropna()
        corr_matrix = returns.corr()
    except Exception as e:
        log_warning(f"Correlation computation failed: {e}")
        return {}

    correlations = {}
    cols = list(corr_matrix.columns)
    for i, t1 in enumerate(cols):
        for t2 in cols[i + 1:]:
            try:
                val = corr_matrix.loc[t1, t2]
                if val is not None and not (isinstance(val, float) and math.isnan(val)):
                    correlations[f"{t1}:{t2}"] = round(float(val), 4)
            except Exception:
                pass

    return correlations


def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== CORRELATION UPDATE ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    # Load quality universe — extract Yahoo-compatible tickers
    universe = safe_read(QUALITY_FILE, {})
    tickers_raw = universe.get('tickers', universe.get('universe', []))
    if isinstance(tickers_raw, list):
        raw_list = tickers_raw
    elif isinstance(tickers_raw, dict):
        raw_list = list(tickers_raw.keys())
    else:
        raw_list = []

    # Normalise to Yahoo format (T212 uses _EQ suffix — strip it, map LSE tickers)
    yahoo_tickers = []
    for t in raw_list[:MAX_TICKERS]:
        t = str(t)
        if t.endswith('_EQ'):
            t = t[:-3]
        if t.endswith('l'):        # LSE l suffix → .L
            t = t[:-1] + '.L'
        yahoo_tickers.append(t)

    yahoo_tickers = list(dict.fromkeys(yahoo_tickers))  # deduplicate, preserve order

    if len(yahoo_tickers) < 2:
        print("  Insufficient tickers — skipping")
        return

    print(f"  Universe: {len(yahoo_tickers)} tickers")
    correlations = fetch_correlations(yahoo_tickers)

    result = {
        'generated':    now.strftime('%Y-%m-%d %H:%M UTC'),
        'n_tickers':    len(yahoo_tickers),
        'n_pairs':      len(correlations),
        'lookback':     LOOKBACK,
        'correlations': correlations,
    }

    atomic_write(OUTPUT_FILE, result)

    # Report highly correlated pairs
    high_corr = [(k, v) for k, v in correlations.items() if abs(v) >= 0.70]
    high_corr.sort(key=lambda x: -abs(x[1]))
    print(f"  Pairs computed: {len(correlations)} | High-corr (≥0.70): {len(high_corr)}")
    if high_corr[:5]:
        print(f"  Top correlations:")
        for k, v in high_corr[:5]:
            print(f"    {k}: {v:+.3f}")
    print(f"  ✅ Saved to apex-pairwise-corr.json")


if __name__ == '__main__':
    run()
