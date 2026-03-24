#!/usr/bin/env python3
"""
Apex Signal Filtering
is_blocked() gate — checks regime, geo, earnings, news, sector breadth,
and market direction before allowing a signal to proceed to sizing.
"""
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

from apex_scoring import get_instrument_sector, get_geo_adjustment


def is_blocked(signal, intel):
    """
    Returns a list of block reasons. Empty list = signal passes.
    Called after scoring, before position sizing.
    """
    name        = signal.get('name', '')
    signal_type = signal.get('signal_type', 'TREND')
    blocks      = []

    # Earnings block
    if name in intel['earnings_blocked']:
        blocks.append(f"Earnings block: {name}")

    # News block
    if name in intel['news_blocked']:
        blocks.append(f"News block: {name}")

    # Sector breadth block — trend signals only
    if signal_type == 'TREND':
        sector = get_instrument_sector(name)
        if sector:
            breadth = intel['sector_breadth'].get(sector, {})
            if breadth.get('breadth_200', 50) <= 20:
                blocks.append(f"Sector breadth too low: {sector} at {breadth.get('breadth_200',0)}%")

    # Regime block — trend signals only
    if signal_type == 'TREND' and intel['regime_status'] == 'BLOCKED':
        blocks.append(f"Regime blocked: VIX {intel['vix']} | Breadth {intel['breadth']}%")

    # Geo block — non-favoured instruments only
    if intel['geo_status'] == 'ALERT':
        geo_boost, _ = get_geo_adjustment(name, intel)
        if geo_boost < 0:
            blocks.append(f"Geo risk: {name} hurt by current conflict")

    # Market direction block — trend signals only
    if signal_type == 'TREND' and intel['direction_status'] == 'BLOCKED':
        blocks.append(f"Market direction: {' | '.join(intel['direction_blocks'])}")

    return blocks
