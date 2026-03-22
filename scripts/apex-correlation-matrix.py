#!/usr/bin/env python3
import yfinance as yf
import json
import sys
from datetime import datetime, timezone

POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
CORRELATION_FILE = '/home/ubuntu/.picoclaw/logs/apex-portfolio-correlation.json'

YAHOO_MAP = {
    "VUAGl_EQ":  "VUAG.L",
    "XOM_US_EQ": "XOM",
    "V_US_EQ":   "V",
    "AAPL_US_EQ":"AAPL",
    "MSFT_US_EQ":"MSFT",
    "NVDA_US_EQ":"NVDA",
    "GOOGL_US_EQ":"GOOGL",
    "JPM_US_EQ": "JPM",
    "GS_US_EQ":  "GS",
}

def get_returns(ticker, yahoo_ticker):
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="3mo")
        if hist.empty:
            return None
        close = hist['Close']
        if close.iloc[-1] > 500 and yahoo_ticker.endswith('.L'):
            close = close / 100
        return close.pct_change().dropna()
    except:
        return None

def run():
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        print("No positions file")
        return

    if len(positions) < 2:
        print("Need at least 2 positions for correlation")
        return

    print("Calculating portfolio correlation matrix...", flush=True)

    returns_data = {}
    for pos in positions:
        ticker = pos.get('t212_ticker', '')
        name   = pos.get('name', ticker)
        yahoo  = YAHOO_MAP.get(ticker, '')
        if not yahoo:
            continue
        print(f"  {name}...", flush=True)
        ret = get_returns(ticker, yahoo)
        if ret is not None:
            returns_data[name] = ret

    if len(returns_data) < 2:
        print("Not enough data for correlation")
        return

    # Align all series to same dates
    import pandas as pd
    # Resample all to daily, forward fill gaps
    aligned = {}
    for name, ret in returns_data.items():
        ret.index = ret.index.tz_localize(None) if ret.index.tz else ret.index
        ret.index = ret.index.normalize()
        aligned[name] = ret
    df = pd.DataFrame(aligned)
    df = df.ffill().dropna()

    # Calculate correlation matrix
    corr = df.corr()

    now     = datetime.now(timezone.utc)
    results = []
    warnings = []

    print(f"\n=== PORTFOLIO CORRELATION MATRIX ===\n")

    names = list(returns_data.keys())

    # Print header
    header = f"{'':20}"
    for n in names:
        header += f"{n[:8]:>10}"
    print(header)

    for i, n1 in enumerate(names):
        row = f"{n1[:20]:20}"
        for j, n2 in enumerate(names):
            if i == j:
                row += f"{'1.00':>10}"
            elif j > i:
                c = round(float(corr.loc[n1, n2]), 2)
                row += f"{c:>10.2f}"
                results.append({
                    "pair":        f"{n1} / {n2}",
                    "correlation": c,
                    "risk":        "HIGH" if abs(c) > 0.7 else ("MEDIUM" if abs(c) > 0.5 else "LOW")
                })
                if abs(c) > 0.7:
                    warnings.append(f"⚠️ {n1} & {n2}: correlation {c} — highly correlated, limited diversification")
            else:
                row += f"{'':>10}"
        print(row)

    # Summary
    print(f"\n=== CORRELATION PAIRS ===")
    results.sort(key=lambda x: abs(x['correlation']), reverse=True)
    for r in results:
        icon = "🔴" if r['risk'] == 'HIGH' else ("🟡" if r['risk'] == 'MEDIUM' else "✅")
        print(f"  {icon} {r['pair']:35} | {r['correlation']:+.2f} | {r['risk']}")

    if warnings:
        print(f"\n⚠️ DIVERSIFICATION WARNINGS:")
        for w in warnings:
            print(f"  {w}")
    else:
        print(f"\n✅ Portfolio is well diversified — no high correlations detected")

    # Save
    output = {
        "timestamp": now.strftime('%Y-%m-%d %H:%M UTC'),
        "pairs":     results,
        "warnings":  warnings,
        "overall":   "HIGH_RISK" if warnings else "DIVERSIFIED"
    }

    with open(CORRELATION_FILE, 'w') as f:
        json.dump(output, f, indent=2)

    return output

if __name__ == '__main__':
    run()
