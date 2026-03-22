#!/usr/bin/env python3
"""
Real-time correlation check.
Runs before every new position to check correlation with existing positions.
More frequent than weekly matrix — catches intra-week correlation spikes.
"""
import yfinance as yf
import json
import sys
from datetime import datetime, timezone

POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
CORRELATION_FILE = '/home/ubuntu/.picoclaw/logs/apex-portfolio-correlation.json'

YAHOO_MAP = {
    "VUAGl_EQ":   "VUAG.L",
    "XOM_US_EQ":  "XOM",
    "V_US_EQ":    "V",
    "AAPL_US_EQ": "AAPL",
    "MSFT_US_EQ": "MSFT",
    "NVDA_US_EQ": "NVDA",
    "GOOGL_US_EQ":"GOOGL",
    "JPM_US_EQ":  "JPM",
    "GS_US_EQ":   "GS",
    "SHEL_EQ":    "SHEL.L",
    "HSBA_EQ":    "HSBA.L",
    "AZN_EQ":     "AZN.L",
    "ABBV_US_EQ": "ABBV",
    "JNJ_US_EQ":  "JNJ",
    "CVX_US_EQ":  "CVX",
}

# Auto-detect yahoo ticker from instrument name
def get_yahoo(ticker, name=""):
    if ticker in YAHOO_MAP:
        return YAHOO_MAP[ticker]
    # Try to construct from ticker
    clean = ticker.replace('_US_EQ','').replace('l_EQ','').replace('_EQ','')
    # UK stocks
    if any(uk in name for uk in ['Vanguard','Shell','HSBC','AstraZeneca','GSK','Unilever','Barclays']):
        return clean + '.L'
    return clean

def get_returns(yahoo_ticker, period="3mo"):
    try:
        hist = yf.Ticker(yahoo_ticker).history(period=period)
        if hist.empty:
            return None
        close = hist['Close']
        if close.iloc[-1] > 500 and yahoo_ticker.endswith('.L'):
            close = close / 100
        ret = close.pct_change().dropna()
        ret.index = ret.index.tz_localize(None) if ret.index.tz else ret.index
        ret.index = ret.index.normalize()
        return ret
    except:
        return None

def check_new_position_correlation(new_ticker, new_yahoo, threshold=0.65):
    """
    Check if a new instrument is too correlated with existing positions.
    Returns: is_blocked, max_correlation, correlated_with
    """
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        return False, 0, []

    if not positions:
        return False, 0, []

    import pandas as pd

    new_returns = get_returns(new_yahoo)
    if new_returns is None:
        return False, 0, []

    high_correlations = []
    max_corr = 0

    for pos in positions:
        ticker = pos.get('t212_ticker','')
        name   = pos.get('name','')
        yahoo  = get_yahoo(ticker, name)

        pos_returns = get_returns(yahoo)
        if pos_returns is None:
            continue

        # Align
        aligned = pd.DataFrame({
            'new': new_returns,
            'pos': pos_returns
        }).dropna()

        if len(aligned) < 20:
            continue

        corr = round(float(aligned['new'].corr(aligned['pos'])), 2)

        if abs(corr) > max_corr:
            max_corr = abs(corr)

        if abs(corr) >= threshold:
            high_correlations.append({
                'position': name,
                'ticker':   ticker,
                'corr':     corr
            })

    is_blocked = len(high_correlations) > 0
    return is_blocked, max_corr, high_correlations

def run_portfolio_correlation():
    """Run full portfolio correlation matrix and save."""
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        return

    if len(positions) < 2:
        return

    import pandas as pd

    returns_data = {}
    for pos in positions:
        ticker = pos.get('t212_ticker','')
        name   = pos.get('name', ticker)
        yahoo  = get_yahoo(ticker, name)
        ret    = get_returns(yahoo)
        if ret is not None:
            returns_data[name] = ret

    if len(returns_data) < 2:
        return

    df   = pd.DataFrame(returns_data).ffill().dropna()
    corr = df.corr()

    now     = datetime.now(timezone.utc)
    pairs   = []
    warnings = []

    names = list(returns_data.keys())
    for i, n1 in enumerate(names):
        for j, n2 in enumerate(names):
            if j <= i:
                continue
            try:
                c = round(float(corr.loc[n1, n2]), 2)
                risk = "HIGH" if abs(c) > 0.7 else ("MEDIUM" if abs(c) > 0.5 else "LOW")
                pairs.append({'pair': f"{n1}/{n2}", 'correlation': c, 'risk': risk})
                if abs(c) > 0.7:
                    warnings.append(f"{n1} & {n2}: {c:+.2f} — highly correlated")
            except:
                pass

    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'pairs':     pairs,
        'warnings':  warnings,
        'overall':   'HIGH_RISK' if warnings else 'DIVERSIFIED'
    }

    with open(CORRELATION_FILE, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n=== PORTFOLIO CORRELATION ===")
    for p in sorted(pairs, key=lambda x: abs(x['correlation']), reverse=True):
        icon = "🔴" if p['risk'] == 'HIGH' else ("🟡" if p['risk'] == 'MEDIUM' else "✅")
        print(f"  {icon} {p['pair']:35} | {p['correlation']:+.2f} | {p['risk']}")

    if warnings:
        print(f"\n⚠️ Warnings:")
        for w in warnings:
            print(f"  {w}")
    else:
        print(f"\n✅ Portfolio well diversified")

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'portfolio'

    if mode == 'portfolio':
        run_portfolio_correlation()
    elif mode == 'check' and len(sys.argv) >= 4:
        ticker = sys.argv[2]
        yahoo  = sys.argv[3]
        blocked, max_corr, high = check_new_position_correlation(ticker, yahoo)
        print(f"Ticker: {ticker} | Max correlation: {max_corr:.2f}")
        if blocked:
            print(f"BLOCKED — too correlated:")
            for h in high:
                print(f"  {h['position']}: {h['corr']:+.2f}")
        else:
            print(f"CLEAR — acceptable correlation")
