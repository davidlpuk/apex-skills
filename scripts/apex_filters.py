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
_ECON_CALENDAR_FILE  = f'{_LOGS}/apex-econ-calendar.json'

# Cached adversarial anti-rules (loaded once per process)
_ADV_CACHE = None


def _load_econ_calendar_status():
    """
    Read the current blackout status from apex-econ-calendar.json.
    If the file is older than 6 hours, recompute inline (stale guard).
    Returns dict with at least 'status' key, or None on failure.
    """
    import os, time
    try:
        mtime = os.path.getmtime(_ECON_CALENDAR_FILE)
        if time.time() - mtime > 6 * 3600:
            # Stale — recompute inline
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "econ_cal", "/home/ubuntu/.picoclaw/scripts/apex-econ-calendar.py")
            _ec = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_ec)
            return _ec.run()
        with open(_ECON_CALENDAR_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        # First run — compute inline
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "econ_cal", "/home/ubuntu/.picoclaw/scripts/apex-econ-calendar.py")
            _ec = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_ec)
            return _ec.run()
        except Exception:
            return None
    except Exception:
        return None

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

    # Economic calendar blackout — high-impact macro events (FOMC, CPI, NFP)
    # routinely produce 2-3σ moves. Block ALL new entries in ±2h window.
    # Reads pre-computed status from apex-econ-calendar.json (updated by the
    # cron-scheduled script). Falls back to running the check inline if file
    # is missing or stale, since this is a hard risk control.
    try:
        econ = _load_econ_calendar_status()
        if econ and econ.get('status') == 'BLACKOUT':
            blocks.append(econ.get('reason', 'Econ calendar blackout'))
    except Exception as _e:
        pass  # Fail-open: don't block trading if calendar check errors

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

    # VIX-level gate for TREND signals — regime.overall is binary (BLOCKED/CLEAR).
    # VIX 28–35 adds a warning but does NOT set BLOCKED, so TREND would pass through
    # at HIGH fear levels without this check. Catch that gap explicitly.
    # VIX ≥ 35 (EXTREME): TREND blocked entirely — trade CONTRARIAN/INVERSE instead.
    # VIX 28–35 (HIGH): TREND requires score ≥ 9.0 or is blocked.
    if signal_type == 'TREND':
        vix = float(intel.get('vix', 20))
        sig_score = float(signal.get('adjusted_score', signal.get('total_score', 7.0)))
        if vix >= 35:
            blocks.append(
                f"VIX EXTREME ({vix:.1f}): TREND blocked — switch to CONTRARIAN/INVERSE"
            )
        elif vix >= 28:
            if sig_score < 9.0:
                blocks.append(
                    f"VIX HIGH ({vix:.1f}): TREND requires score ≥9.0, signal scored {sig_score:.1f}"
                )

    # Regime block — trend signals only
    if signal_type == 'TREND' and intel['regime_status'] == 'BLOCKED':
        blocks.append(f"Regime blocked: VIX {intel['vix']} | Breadth {intel['breadth']}%")

    # Portfolio heat check — block new longs when existing positions are heavily
    # correlated to VIX moves (all fall together on fear spikes).
    # Uses position_vix_sensitivity: {ticker: corr_value}.
    # Negative correlation means position falls when VIX rises (typical long equity).
    # If ≥3 open positions have VIX correlation < -0.5, portfolio is already saturated
    # with correlated long exposure — adding more amplifies drawdown during VIX spikes.
    if signal_type in ('TREND', 'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE'):
        vix_sensitivity = intel.get('position_vix_sensitivity', {})
        high_vix_corr_count = sum(
            1 for corr in vix_sensitivity.values()
            if isinstance(corr, (int, float)) and corr < -0.5
        )
        if high_vix_corr_count >= 3:
            blocks.append(
                f"Portfolio heat: {high_vix_corr_count} open positions with VIX correlation < -0.5 "
                f"— adding more correlated longs amplifies drawdown on VIX spike"
            )

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
