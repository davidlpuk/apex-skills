#!/usr/bin/env python3
"""
Cross-Asset Macro Signal Confirmation
Checks underlying macro drivers before confirming equity signals.
Institutional approach: never trade an equity in isolation from its macro context.

Signal confirmation rules:
- Energy equities: crude oil direction must align
- US equities: 10-year Treasury yield must be stable or falling (risk-on)
- UK equities: GBP/USD must be stable (currency risk)
- Tech equities: USD/CNY must not be spiking (China demand signal)
- All equities: credit spreads (HYG vs LQD) must not be widening sharply

Adds +2 / -2 adjustment to decision engine scoring.
"""
import json
import sys
import yfinance as yf
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

MACRO_FILE = '/home/ubuntu/.picoclaw/logs/apex-macro-signals.json'

# Macro instruments
MACRO_TICKERS = {
    'crude_oil':    'CL=F',      # WTI Crude Oil Futures
    'brent':        'BZ=F',      # Brent Crude Futures
    'treasury_10y': '^TNX',      # 10-Year US Treasury Yield
    'treasury_2y':  '^IRX',      # 2-Year US Treasury Yield
    'gbp_usd':      'GBPUSD=X',  # GBP/USD
    'usd_cny':      'CNY=X',     # USD/CNY
    'dollar_index': 'DX-Y.NYB',  # US Dollar Index
    'hyg':          'HYG',       # High Yield Bond ETF (credit risk)
    'lqd':          'LQD',       # Investment Grade Bond ETF
    'vix':          '^VIX',      # Volatility Index
    'gold':         'GC=F',      # Gold Futures (safe haven)
    'copper':       'HG=F',      # Copper Futures (growth signal)
}

# Sector to macro driver mapping
SECTOR_MACRO = {
    'Energy':      ['crude_oil', 'dollar_index'],
    'Technology':  ['treasury_10y', 'usd_cny', 'dollar_index'],
    'Financials':  ['treasury_10y', 'treasury_2y', 'hyg'],
    'Healthcare':  ['treasury_10y', 'hyg'],
    'Consumer':    ['copper', 'gbp_usd'],
    'Broad':       ['treasury_10y', 'vix', 'hyg'],
    'Inverse':     ['vix', 'treasury_10y'],
    'Other':       ['treasury_10y', 'dollar_index'],
}

# Instrument to sector mapping
INSTRUMENT_SECTOR = {
    'XOM_US_EQ':  'Energy',   'CVX_US_EQ':  'Energy',
    'SHEL_EQ':    'Energy',   'BP_EQ':      'Energy',
    'AAPL_US_EQ': 'Technology','MSFT_US_EQ': 'Technology',
    'NVDA_US_EQ': 'Technology','GOOGL_US_EQ':'Technology',
    'V_US_EQ':    'Financials','JPM_US_EQ':  'Financials',
    'GS_US_EQ':   'Financials','HSBA_EQ':    'Financials',
    'JNJ_US_EQ':  'Healthcare','ABBV_US_EQ': 'Healthcare',
    'AZN_EQ':     'Healthcare','GSK_EQ':     'Healthcare',
    'UNH_US_EQ':  'Healthcare',
    'ULVR_EQ':    'Consumer',  'PG_US_EQ':   'Consumer',
    'VUAGl_EQ':   'Broad',
    'QQQSl_EQ':   'Inverse',  'SQQQ_EQ':    'Inverse',
    '3USSl_EQ':   'Inverse',  'SPXU_EQ':    'Inverse',
}

def get_macro_data():
    """Fetch all macro instruments and calculate direction."""
    macro_data = {}

    for name, ticker in MACRO_TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="10d")
            if hist.empty or len(hist) < 3:
                continue

            closes   = [float(c) for c in hist['Close']]
            current  = closes[-1]
            prev     = closes[-2]
            week_ago = closes[-5] if len(closes) >= 5 else closes[0]

            daily_chg  = round((current - prev) / prev * 100, 2)
            weekly_chg = round((current - week_ago) / week_ago * 100, 2)

            # Direction classification
            if weekly_chg > 2.0:
                direction = 'RISING'
            elif weekly_chg < -2.0:
                direction = 'FALLING'
            else:
                direction = 'STABLE'

            macro_data[name] = {
                'ticker':     ticker,
                'current':    round(current, 4),
                'daily_chg':  daily_chg,
                'weekly_chg': weekly_chg,
                'direction':  direction,
            }
        except Exception as e:
            log_error(f"Macro fetch failed for {name} ({ticker}): {e}")

    return macro_data

def get_macro_adjustment(ticker, signal_type, macro_data):
    """
    Calculate macro signal adjustment for a specific instrument.
    Returns (adjustment, reasons) where adjustment is -2 to +2.
    """
    sector   = INSTRUMENT_SECTOR.get(ticker, 'Other')
    drivers  = SECTOR_MACRO.get(sector, SECTOR_MACRO['Other'])
    adj      = 0
    reasons  = []

    for driver in drivers:
        data = macro_data.get(driver)
        if not data:
            continue

        direction = data['direction']
        chg       = data['weekly_chg']

        # Energy — crude oil alignment
        if driver == 'crude_oil':
            if signal_type in ['TREND', 'CONTRARIAN'] and direction == 'RISING':
                adj += 1
                reasons.append(f"Crude RISING {chg:+.1f}% — energy tailwind")
            elif signal_type in ['TREND', 'CONTRARIAN'] and direction == 'FALLING':
                adj -= 1
                reasons.append(f"Crude FALLING {chg:+.1f}% — energy headwind")
            elif signal_type == 'INVERSE' and direction == 'FALLING':
                adj += 1
                reasons.append(f"Crude FALLING — inverse energy confirmed")

        # 10-year Treasury — risk on/off
        elif driver == 'treasury_10y':
            current = data['current']
            if current > 4.5 and direction == 'RISING':
                adj -= 1
                reasons.append(f"10Y yield {current:.2f}% rising — risk-off pressure")
            elif current < 4.0 or direction == 'FALLING':
                adj += 1
                reasons.append(f"10Y yield {current:.2f}% stable/falling — risk-on")
            if signal_type == 'INVERSE' and current > 4.5 and direction == 'RISING':
                adj += 1
                reasons.append(f"High rising yields confirm inverse signal")

        # Yield curve — financials
        elif driver == 'treasury_2y':
            t10  = macro_data.get('treasury_10y', {}).get('current', 4.0)
            t2   = data['current']
            spread = round(t10 - t2, 2)
            if sector == 'Financials':
                if spread > 0.5:
                    adj += 1
                    reasons.append(f"Yield curve steep ({spread:.2f}%) — financials positive")
                elif spread < 0:
                    adj -= 1
                    reasons.append(f"Yield curve inverted ({spread:.2f}%) — financials headwind")

        # GBP/USD — UK instruments
        elif driver == 'gbp_usd':
            if direction == 'FALLING' and abs(chg) > 1.5:
                adj -= 1
                reasons.append(f"GBP falling {chg:+.1f}% — UK currency risk")
            elif direction == 'STABLE':
                reasons.append(f"GBP stable — no currency risk")

        # USD/CNY — tech demand
        elif driver == 'usd_cny':
            if direction == 'RISING' and chg > 1.0:
                adj -= 1
                reasons.append(f"USD/CNY rising {chg:+.1f}% — China demand pressure on tech")
            elif direction == 'STABLE' or direction == 'FALLING':
                adj += 1
                reasons.append(f"USD/CNY stable — no China demand headwind")

        # High yield spreads — credit stress
        elif driver == 'hyg':
            if direction == 'FALLING' and chg < -1.5:
                adj -= 1
                reasons.append(f"HYG falling {chg:+.1f}% — credit stress, risk-off")
            elif direction == 'RISING':
                adj += 1
                reasons.append(f"HYG stable/rising — credit markets calm")

        # Copper — growth signal
        elif driver == 'copper':
            if direction == 'RISING':
                adj += 1
                reasons.append(f"Copper RISING {chg:+.1f}% — growth signal positive")
            elif direction == 'FALLING' and chg < -3.0:
                adj -= 1
                reasons.append(f"Copper FALLING {chg:+.1f}% — growth concern")

        # VIX for inverse signals
        elif driver == 'vix':
            current = data['current']
            if signal_type == 'INVERSE':
                if current > 25 and direction == 'RISING':
                    adj += 1
                    reasons.append(f"VIX {current:.1f} rising — inverse signal strengthened")
                elif current < 18:
                    adj -= 1
                    reasons.append(f"VIX {current:.1f} low — inverse signal weakened")

    # Cap adjustment at -2/+2
    adj = max(-2, min(2, adj))
    return adj, reasons

def run():
    """Fetch all macro data and save."""
    now = datetime.now(timezone.utc)
    print(f"\n=== MACRO SIGNAL ANALYSIS ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    macro_data = get_macro_data()

    if not macro_data:
        print("  ❌ No macro data fetched")
        return {}

    print(f"  {'Instrument':15} {'Current':10} {'Daily':8} {'Weekly':8} {'Direction'}")
    print(f"  {'-'*55}")

    for name, data in macro_data.items():
        icon = "📈" if data['direction'] == 'RISING' else (
               "📉" if data['direction'] == 'FALLING' else "➡️")
        print(f"  {name:15} {data['current']:10.4f} "
              f"{data['daily_chg']:+7.2f}% {data['weekly_chg']:+7.2f}%  "
              f"{icon} {data['direction']}")

    # Test adjustments on current positions
    positions = safe_read('/home/ubuntu/.picoclaw/logs/apex-positions.json', [])
    if positions:
        print(f"\n  Macro adjustments for current positions:")
        for pos in positions:
            ticker   = pos.get('t212_ticker','')
            name     = pos.get('name','?')
            sig_type = pos.get('signal_type','TREND')
            adj, reasons = get_macro_adjustment(ticker, sig_type, macro_data)
            icon = "✅" if adj >= 0 else "⚠️"
            print(f"\n  {icon} {name}: macro adj {adj:+d}")
            for r in reasons[:2]:
                print(f"     → {r}")

    output = {
        'timestamp':  now.strftime('%Y-%m-%d %H:%M UTC'),
        'macro_data': macro_data,
        'count':      len(macro_data),
    }
    atomic_write(MACRO_FILE, output)
    print(f"\n✅ Macro signals saved — {len(macro_data)} instruments")
    return macro_data

if __name__ == '__main__':
    run()
