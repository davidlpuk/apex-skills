#!/usr/bin/env python3
"""
Sector Concentration Limit
Prevents over-concentration in any single sector.
Max 40% of portfolio in any one sector.
Max 2 positions in the same sector simultaneously.

Called by autopilot before every new position.
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, get_portfolio_value
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

# Sector mapping
INSTRUMENT_SECTOR = {
    # Energy
    "XOM_US_EQ":   "Energy", "CVX_US_EQ":  "Energy",
    "SHEL_EQ":     "Energy", "BP_EQ":      "Energy",
    "TTE_EQ":      "Energy",
    # Technology
    "AAPL_US_EQ":  "Technology", "MSFT_US_EQ": "Technology",
    "NVDA_US_EQ":  "Technology", "GOOGL_US_EQ":"Technology",
    "AMZN_US_EQ":  "Technology", "META_US_EQ": "Technology",
    "CRM_US_EQ":   "Technology", "ORCL_US_EQ": "Technology",
    # Financials
    "JPM_US_EQ":   "Financials", "GS_US_EQ":   "Financials",
    "V_US_EQ":     "Financials", "BAC_US_EQ":  "Financials",
    "BLK_US_EQ":   "Financials", "HSBA_EQ":    "Financials",
    # Healthcare
    "JNJ_US_EQ":   "Healthcare", "PFE_US_EQ":  "Healthcare",
    "UNH_US_EQ":   "Healthcare", "ABBV_US_EQ": "Healthcare",
    "AZN_EQ":      "Healthcare", "GSK_EQ":     "Healthcare",
    # Consumer
    "KO_US_EQ":    "Consumer",   "PEP_US_EQ":  "Consumer",
    "PG_US_EQ":    "Consumer",   "WMT_US_EQ":  "Consumer",
    "ULVR_EQ":     "Consumer",
    # Broad Market
    "VUAGl_EQ":    "Broad",      "VWRP_EQ":   "Broad",
    # Inverse ETFs
    "SQQQ_EQ":     "Inverse",    "QQQSl_EQ":  "Inverse",
    "3USSl_EQ":    "Inverse",    "SPXU_EQ":   "Inverse",
    "3UKSl_EQ":    "Inverse",
}

# Concentration limits
MAX_SECTOR_POSITIONS = 2      # Max positions in same sector
MAX_SECTOR_PCT       = 40.0   # Max % of portfolio in one sector
MAX_INVERSE_PCT      = 20.0   # Max % in inverse ETFs (leveraged)

def get_sector(ticker):
    """Get sector for a ticker."""
    return INSTRUMENT_SECTOR.get(ticker, 'Other')

def analyse_concentration(positions, portfolio_value=None):
    """Analyse current sector concentration."""
    if portfolio_value is None:
        portfolio_value = get_portfolio_value() or 5000
    sector_data = {}

    for pos in positions:
        ticker   = pos.get('t212_ticker', '')
        sector   = get_sector(ticker)
        notional = float(pos.get('quantity', 0)) * float(pos.get('current', pos.get('entry', 0)))

        if sector not in sector_data:
            sector_data[sector] = {'positions': [], 'notional': 0, 'pct': 0}

        sector_data[sector]['positions'].append(pos.get('name', ticker))
        sector_data[sector]['notional'] += notional

    # Calculate percentages
    for sector in sector_data:
        sector_data[sector]['pct'] = round(
            sector_data[sector]['notional'] / portfolio_value * 100, 1
        )

    return sector_data

def check_concentration(new_signal, positions, portfolio_value=None):
    """
    Check if adding new signal would breach concentration limits.
    Returns (allowed, reason)
    """
    if portfolio_value is None:
        portfolio_value = get_portfolio_value() or 5000
    new_ticker = new_signal.get('t212_ticker', '')
    new_sector = get_sector(new_ticker)
    new_notional = float(new_signal.get('quantity', 0)) * float(new_signal.get('entry', 0))

    sector_data = analyse_concentration(positions, portfolio_value)

    # Check position count in sector
    existing = sector_data.get(new_sector, {})
    existing_count    = len(existing.get('positions', []))
    existing_notional = existing.get('notional', 0)
    existing_pct      = existing.get('pct', 0)

    if existing_count >= MAX_SECTOR_POSITIONS:
        return False, (
            f"Sector concentration: already {existing_count} positions in {new_sector} "
            f"({', '.join(existing['positions'])}) — max {MAX_SECTOR_POSITIONS}"
        )

    # Check notional percentage
    new_total_pct = round((existing_notional + new_notional) / portfolio_value * 100, 1)
    limit = MAX_INVERSE_PCT if new_sector == 'Inverse' else MAX_SECTOR_PCT

    if new_total_pct > limit:
        return False, (
            f"Sector concentration: {new_sector} would be {new_total_pct}% of portfolio "
            f"(max {limit}%) — reduce size or skip"
        )

    # Warning if getting close
    warning_threshold = limit * 0.8
    if new_total_pct > warning_threshold:
        return True, (
            f"Sector concentration WARNING: {new_sector} at {new_total_pct}% "
            f"(limit {limit}%) — approaching maximum"
        )

    return True, f"Sector {new_sector}: {new_total_pct}% of portfolio — within limits"

def run():
    """Show current sector concentration."""
    now       = datetime.now(timezone.utc)
    positions = safe_read(POSITIONS_FILE, [])

    print(f"\n=== SECTOR CONCENTRATION ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    if not positions:
        print("  No open positions")
        return

    sector_data = analyse_concentration(positions)

    print(f"  {'Sector':15} {'Positions':30} {'Notional':10} {'% Port':8} {'Status'}")
    print(f"  {'-'*75}")

    for sector, data in sorted(sector_data.items(),
                               key=lambda x: x[1]['pct'], reverse=True):
        limit  = MAX_INVERSE_PCT if sector == 'Inverse' else MAX_SECTOR_PCT
        pct    = data['pct']
        status = "✅ OK"
        if pct > limit:
            status = f"❌ OVER LIMIT ({limit}%)"
        elif pct > limit * 0.8:
            status = f"⚠️  Near limit ({limit}%)"

        names = ', '.join(data['positions'][:3])
        print(f"  {sector:15} {names:30} £{data['notional']:7.0f}  {pct:6.1f}%  {status}")

    return sector_data

def get_correlation_size_multiplier(new_ticker, positions):
    """
    Reduce position size if adding a highly correlated instrument.
    If correlation > 0.85 with existing position — halve the size.
    If correlation > 0.70 — reduce to 75%.

    Uses known correlation pairs rather than calculated correlation
    (calculated correlation needs price history — known pairs are instant).
    """
    # Known high-correlation pairs
    HIGH_CORRELATION = {
        # Energy pairs
        ('XOM_US_EQ', 'CVX_US_EQ'): 0.92,
        ('XOM_US_EQ', 'SHEL_EQ'):   0.88,
        ('CVX_US_EQ', 'SHEL_EQ'):   0.87,
        ('XOM_US_EQ', 'BP_EQ'):     0.85,
        # Tech pairs
        ('AAPL_US_EQ', 'MSFT_US_EQ'): 0.85,
        ('AAPL_US_EQ', 'NVDA_US_EQ'): 0.80,
        ('MSFT_US_EQ', 'GOOGL_US_EQ'):0.82,
        # Financial pairs
        ('JPM_US_EQ',  'GS_US_EQ'):  0.88,
        ('JPM_US_EQ',  'BAC_US_EQ'): 0.90,
        ('V_US_EQ',    'JPM_US_EQ'): 0.75,
        # Healthcare pairs
        ('JNJ_US_EQ',  'ABBV_US_EQ'):0.72,
        ('AZN_EQ',     'GSK_EQ'):    0.78,
        # Inverse pairs (negatively correlated with longs)
        ('SQQQ_EQ',    'AAPL_US_EQ'):-0.95,
        ('QQQSl_EQ',   'MSFT_US_EQ'):-0.95,
        ('3USSl_EQ',   'VUAGl_EQ'):  -0.98,
    }

    open_tickers = [p.get('t212_ticker','') for p in positions]
    max_corr     = 0.0
    corr_partner = None

    for open_ticker in open_tickers:
        # Check both orderings
        pair1 = (new_ticker, open_ticker)
        pair2 = (open_ticker, new_ticker)
        corr  = HIGH_CORRELATION.get(pair1, HIGH_CORRELATION.get(pair2, 0.0))
        corr  = abs(corr)  # Use absolute correlation

        if corr > max_corr:
            max_corr     = corr
            corr_partner = open_ticker

    # Determine size multiplier
    if max_corr >= 0.85:
        multiplier = 0.50
        reason = f"High correlation {max_corr:.2f} with {corr_partner} — 50% size"
    elif max_corr >= 0.70:
        multiplier = 0.75
        reason = f"Moderate correlation {max_corr:.2f} with {corr_partner} — 75% size"
    else:
        multiplier = 1.0
        reason = f"No significant correlation (max {max_corr:.2f}) — full size"

    return multiplier, reason, max_corr

if __name__ == '__main__':
    run()

    # Test with a new signal
    positions = safe_read(POSITIONS_FILE, [])
    test_signal = {
        'name': 'CVX', 't212_ticker': 'CVX_US_EQ',
        'entry': 165.0, 'quantity': 2.0,
        'signal_type': 'CONTRARIAN'
    }
    allowed, reason = check_concentration(test_signal, positions)
    print(f"\n  Test CVX signal: {'✅ ALLOWED' if allowed else '❌ BLOCKED'}")
    print(f"  Reason: {reason}")
