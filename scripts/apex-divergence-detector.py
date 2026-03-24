#!/usr/bin/env python3
"""
Cross-Asset Divergence Detector (Layer 14.5)
Identifies when macro drivers diverge from equity price action.

Examples of tradeable divergences:
  Crude oil +3% in 5 days BUT energy stock flat → bullish divergence (+1)
  10Y yields UP 20bps in 5 days BUT tech stock UP → bearish divergence (-1)
  Financial stock flat BUT yield curve steepening → bullish divergence (+1)

Logic:
  Each instrument's sector determines which macro driver to compare.
  If driver direction > threshold AND equity is NOT moving in sympathy → divergence.
  Capped at ±2 total adjustment.
"""
import json
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, atomic_write, log_error
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')

MACRO_FILE     = '/home/ubuntu/.picoclaw/logs/apex-macro-signals.json'
DIVERGENCE_FILE = '/home/ubuntu/.picoclaw/logs/apex-divergence.json'

# Sector → macro driver mapping
# driver: key in macro_data dict | direction: 'up' or 'down' is bullish for sector
SECTOR_DRIVERS = {
    'Energy':      {'driver': 'crude_oil',   'equity_direction': 'up',   'threshold_pct': 3.0},
    'Technology':  {'driver': 'us_10y_yield','equity_direction': 'down', 'threshold_pct': 0.15},  # 15bps
    'Financials':  {'driver': 'yield_curve', 'equity_direction': 'up',   'threshold_pct': 0.10},   # 10bps steepening
    'Healthcare':  {'driver': 'vix',         'equity_direction': 'down', 'threshold_pct': 10.0},   # VIX -10%
    'Consumer':    {'driver': 'dxy',         'equity_direction': 'down', 'threshold_pct': 1.0},    # DXY +1% bearish for consumer
    'Materials':   {'driver': 'gold',        'equity_direction': 'up',   'threshold_pct': 2.0},
    'Industrials': {'driver': 'copper',      'equity_direction': 'up',   'threshold_pct': 2.0},
}

# T212 ticker → sector mapping (supplement apex-quality-universe.json)
TICKER_SECTORS = {
    'XOM_US_EQ': 'Energy',   'CVX_US_EQ': 'Energy',
    'AAPL_US_EQ':'Technology','MSFT_US_EQ':'Technology','NVDA_US_EQ':'Technology',
    'GOOGL_US_EQ':'Technology','META_US_EQ':'Technology',
    'JPM_US_EQ': 'Financials','GS_US_EQ':  'Financials',
    'ABBV_US_EQ':'Healthcare','JNJ_US_EQ': 'Healthcare','UNH_US_EQ': 'Healthcare',
    'AMZN_US_EQ':'Consumer',
    'SHEL_EQ':   'Energy',   'AZN_EQ':    'Healthcare',
}


def _get_instrument_5d_return(ticker, name=''):
    """Fetch 5-day return for an equity instrument."""
    try:
        import yfinance as yf

        # Resolve to Yahoo ticker
        yahoo_map = {
            'XOM_US_EQ': 'XOM',    'CVX_US_EQ': 'CVX',
            'AAPL_US_EQ':'AAPL',   'MSFT_US_EQ':'MSFT',   'NVDA_US_EQ':'NVDA',
            'GOOGL_US_EQ':'GOOGL', 'META_US_EQ':'META',   'TSLA_US_EQ':'TSLA',
            'V_US_EQ': 'V',        'JPM_US_EQ': 'JPM',    'GS_US_EQ':  'GS',
            'ABBV_US_EQ':'ABBV',   'JNJ_US_EQ': 'JNJ',    'UNH_US_EQ': 'UNH',
            'AMZN_US_EQ':'AMZN',   'NFLX_US_EQ':'NFLX',
            'SHEL_EQ':  'SHEL.L',  'AZN_EQ':   'AZN.L',
            'HSBA_EQ':  'HSBA.L',  'ULVR_EQ':  'ULVR.L',
            'VUAGl_EQ': 'VUAG.L',
        }
        yahoo = yahoo_map.get(ticker, ticker.replace('_US_EQ','').replace('_EQ',''))
        hist  = yf.Ticker(yahoo).history(period='10d')
        if hist.empty or len(hist) < 5:
            return None
        closes   = list(hist['Close'])
        ret_5d   = (closes[-1] - closes[-5]) / closes[-5] * 100
        return round(ret_5d, 2)
    except Exception:
        return None


def _get_macro_5d_change(driver_key, macro_data):
    """
    Get 5-day change for a macro driver from the macro signals data.
    Returns change value or None if not available.
    """
    driver_data = macro_data.get(driver_key, {})
    if not driver_data:
        return None

    # Try 'change_5d', 'pct_change_5d', 'week_change', 'change_pct', etc.
    for key in ('change_5d', 'pct_change_5d', 'week_change', 'pct_change', 'change'):
        val = driver_data.get(key)
        if val is not None:
            try:
                return float(val)
            except Exception:
                pass

    # Fall back to computing from price if 'price' and 'prev_price' available
    price      = float(driver_data.get('price', 0) or 0)
    prev_price = float(driver_data.get('prev_price', driver_data.get('price_5d_ago', 0)) or 0)
    if price > 0 and prev_price > 0:
        return round((price - prev_price) / prev_price * 100, 2)

    return None


def get_divergence_adjustment(name, t212_ticker, signal_type='TREND'):
    """
    Main entry point — called by scoring layer.
    Returns (adjustment, reasons_list).
    """
    # Determine sector
    sector = TICKER_SECTORS.get(t212_ticker, '')
    if not sector:
        return 0.0, []

    driver_config = SECTOR_DRIVERS.get(sector)
    if not driver_config:
        return 0.0, []

    # Load macro data
    macro_file_data = safe_read(MACRO_FILE, {})
    macro_data      = macro_file_data.get('macro_data', macro_file_data)
    if not macro_data:
        return 0.0, []

    driver_key    = driver_config['driver']
    threshold     = driver_config['threshold_pct']
    macro_bullish = driver_config['equity_direction']  # 'up' = driver moving up is good

    # Get macro driver 5d change
    macro_change = _get_macro_5d_change(driver_key, macro_data)
    if macro_change is None:
        return 0.0, []

    # Get equity 5d return
    equity_return = _get_instrument_5d_return(t212_ticker, name)
    if equity_return is None:
        return 0.0, []

    adjustment = 0.0
    reasons    = []

    # Driver moving bullishly (e.g. crude oil up > 3%)
    driver_bullish_move = (macro_bullish == 'up' and macro_change > threshold) or \
                          (macro_bullish == 'down' and macro_change < -threshold)
    # Driver moving bearishly (e.g. crude oil down > 3%)
    driver_bearish_move = (macro_bullish == 'up' and macro_change < -threshold) or \
                          (macro_bullish == 'down' and macro_change > threshold)

    FLAT_THRESHOLD = 1.5  # Equity is "flat" if 5d return < 1.5%

    if driver_bullish_move and abs(equity_return) < FLAT_THRESHOLD:
        # Macro driver bullish but equity hasn't moved → bullish divergence
        adjustment = 1.0
        reasons.append(
            f"Bullish divergence: {driver_key} {macro_change:+.1f}% but {name} flat "
            f"({equity_return:+.1f}%) — catch-up potential"
        )
    elif driver_bearish_move and equity_return > FLAT_THRESHOLD:
        # Macro driver bearish but equity still rising → negative divergence
        adjustment = -1.0
        reasons.append(
            f"Bearish divergence: {driver_key} {macro_change:+.1f}% but {name} up "
            f"({equity_return:+.1f}%) — risk of reversal"
        )
    elif driver_bullish_move and equity_return > FLAT_THRESHOLD:
        # Both moving in same direction → confirmation
        adjustment = 0.5
        reasons.append(
            f"Cross-asset confirmation: {driver_key} {macro_change:+.1f}% and "
            f"{name} {equity_return:+.1f}%"
        )

    # For CONTRARIAN signals, invert the divergence logic
    if signal_type == 'CONTRARIAN':
        adjustment = -adjustment

    adjustment = max(-2.0, min(2.0, adjustment))

    # Cache result
    cache = safe_read(DIVERGENCE_FILE, {'instruments': {}})
    now   = datetime.now(timezone.utc)
    cache.setdefault('instruments', {})[t212_ticker] = {
        'updated_at':    now.isoformat(),
        'driver':        driver_key,
        'macro_change':  macro_change,
        'equity_return': equity_return,
        'adjustment':    adjustment,
        'reasons':       reasons,
    }
    cache['last_updated'] = now.isoformat()
    atomic_write(DIVERGENCE_FILE, cache)

    return adjustment, reasons


def run():
    """Test divergence detection."""
    print("\n=== CROSS-ASSET DIVERGENCE DETECTOR ===")
    tests = [
        ('Exxon Mobil', 'XOM_US_EQ'),
        ('Apple', 'AAPL_US_EQ'),
        ('JPMorgan', 'JPM_US_EQ'),
    ]
    for name, ticker in tests:
        adj, reasons = get_divergence_adjustment(name, ticker)
        print(f"  {name:20} {adj:+.1f}")
        for r in reasons:
            print(f"    • {r[:80]}")
    print("\n✅ Divergence detection done")


if __name__ == '__main__':
    run()
