#!/usr/bin/env python3
"""
Apex Signal Filtering
is_blocked() gate — checks regime, geo, earnings, news, sector breadth,
and market direction before allowing a signal to proceed to sizing.
"""
import json, sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

from apex_scoring import get_instrument_sector, get_geo_adjustment

_LOGS = '/home/ubuntu/.picoclaw/logs'
_ADVERSARIAL_RESULTS = f'{_LOGS}/apex-adversarial-results.json'

# Cached adversarial anti-rules (loaded once per process)
_ADV_CACHE = None

def _load_adversarial_rules():
    global _ADV_CACHE
    if _ADV_CACHE is not None:
        return _ADV_CACHE
    try:
        with open(_ADVERSARIAL_RESULTS) as f:
            data = json.load(f)
        _ADV_CACHE = data.get('anti_rules', [])
    except Exception:
        _ADV_CACHE = []
    return _ADV_CACHE


def is_adversarial_blocked(signal, intel):
    """
    Check adversarial anti-rules from apex-adversarial-results.json.
    Returns list of block reasons (empty = pass).
    Only blocks when action='block' AND confidence >= 0.85 AND win_rate <= 0.30.
    """
    blocks = []
    try:
        rules = _load_adversarial_rules()
        signal_type = signal.get('signal_type', '')
        rsi = signal.get('rsi', 50)
        vix = intel.get('vix', 20)
        breadth = intel.get('breadth', 50)

        for rule in rules:
            if not rule.get('active', False):
                continue
            if rule.get('action') != 'block':
                continue
            if rule.get('confidence', 0) < 0.85:
                continue
            if rule.get('win_rate', 0.5) > 0.30:
                continue

            dims = rule.get('dimensions', {})
            matched = True
            for key, val in dims.items():
                if key == 'signal_type' and signal_type != val:
                    matched = False; break
                elif key == 'vix_bucket':
                    vix_b = ('>33' if vix > 33 else '28-33' if vix > 28 else
                             '22-28' if vix > 22 else '18-22' if vix > 18 else '<18')
                    if vix_b != val:
                        matched = False; break
                elif key == 'breadth_bucket':
                    br_b = ('>60%' if breadth > 60 else '40-60%' if breadth > 40 else '<40%')
                    if br_b != val:
                        matched = False; break
                elif key == 'rsi_bucket':
                    rsi_b = ('>60' if rsi > 60 else '45-60' if rsi > 45 else
                             '30-45' if rsi > 30 else '<30')
                    if rsi_b != val:
                        matched = False; break
            if matched:
                blocks.append(
                    f"Adversarial block: {rule.get('condition_key','?')} "
                    f"(WR={rule.get('win_rate',0):.0%}, CI confidence={rule.get('confidence',0):.0%})"
                )
    except Exception:
        pass  # Adversarial filter is non-critical — silent failure
    return blocks


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

    # Adversarial anti-rules (data-driven, statistically validated)
    adv_blocks = is_adversarial_blocked(signal, intel)
    blocks.extend(adv_blocks)

    return blocks
