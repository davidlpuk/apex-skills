#!/usr/bin/env python3
import yfinance as yf
import json
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


POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
VIX_CORR_FILE    = '/home/ubuntu/.picoclaw/logs/apex-vix-correlation.json'

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
}

def run():
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        print("No positions")
        return

    if not positions:
        print("No open positions")
        return

    print("Fetching VIX data...", flush=True)

    # Get VIX returns
    try:
        vix_hist = yf.Ticker("^VIX").history(period="3mo")
        if vix_hist.empty:
            print("Could not fetch VIX data")
            return
        vix_returns = vix_hist['Close'].pct_change().dropna()
        vix_returns.index = vix_returns.index.tz_localize(None) if vix_returns.index.tz else vix_returns.index
        vix_returns.index = vix_returns.index.normalize()
    except Exception as e:
        print(f"VIX error: {e}")
        return

    results  = []
    warnings = []

    print("Calculating VIX correlations...\n", flush=True)

    for pos in positions:
        ticker = pos.get('t212_ticker', '')
        name   = pos.get('name', ticker)
        yahoo  = YAHOO_MAP.get(ticker, '')

        if not yahoo:
            continue

        try:
            hist = yf.Ticker(yahoo).history(period="3mo")
            if hist.empty:
                continue

            close = hist['Close']
            if close.iloc[-1] > 500 and yahoo.endswith('.L'):
                close = close / 100

            returns = close.pct_change().dropna()
            returns.index = returns.index.tz_localize(None) if returns.index.tz else returns.index
            returns.index = returns.index.normalize()

            # Align with VIX
            import pandas as pd
            aligned = pd.DataFrame({
                'stock': returns,
                'vix':   vix_returns
            }).dropna()

            if len(aligned) < 20:
                continue

            corr = round(float(aligned['stock'].corr(aligned['vix'])), 2)

            # Classify sensitivity
            if corr <= -0.6:
                sensitivity = "HIGH"
                note = "Drops sharply when VIX spikes — most vulnerable"
                icon = "🔴"
            elif corr <= -0.3:
                sensitivity = "MEDIUM"
                note = "Moderately sensitive to fear spikes"
                icon = "🟡"
            elif corr <= 0.1:
                sensitivity = "LOW"
                note = "Relatively immune to VIX moves"
                icon = "✅"
            else:
                sensitivity = "HEDGE"
                note = "Rises when VIX spikes — natural hedge"
                icon = "🛡️"

            results.append({
                "name":        name,
                "ticker":      ticker,
                "vix_corr":    corr,
                "sensitivity": sensitivity,
                "note":        note,
                "icon":        icon
            })

            if sensitivity == "HIGH":
                warnings.append(f"{name} highly correlated to VIX — most at risk during fear spikes")

        except Exception as e:
            print(f"  {name}: error — {e}")
            continue

    # Sort by correlation (most negative first = most vulnerable)
    results.sort(key=lambda x: x['vix_corr'])

    now = datetime.now(timezone.utc)

    print(f"=== VIX SENSITIVITY REPORT ===\n")
    print(f"{'Position':25} | {'VIX Corr':8} | {'Sensitivity':10} | Note")
    print("-" * 80)

    for r in results:
        print(f"{r['icon']} {r['name']:23} | {r['vix_corr']:+8.2f} | {r['sensitivity']:10} | {r['note']}")

    # Get current VIX
    try:
        current_vix = round(float(yf.Ticker("^VIX").history(period="1d")['Close'].iloc[-1]), 2)
        print(f"\n📊 Current VIX: {current_vix}")
        if current_vix >= 30:
            print(f"🚨 VIX ELEVATED — high sensitivity positions at significant risk")
        elif current_vix >= 20:
            print(f"⚠️ VIX ELEVATED — monitor high sensitivity positions")
        else:
            print(f"✅ VIX normal — low near-term fear")
    except:
        pass

    if warnings:
        print(f"\n⚠️ VULNERABILITY WARNINGS:")
        for w in warnings:
            print(f"  {w}")

    # Save
    output = {
        "timestamp": now.strftime('%Y-%m-%d %H:%M UTC'),
        "positions": results,
        "warnings":  warnings
    }

    atomic_write(VIX_CORR_FILE, output)

    return output

if __name__ == '__main__':
    run()
