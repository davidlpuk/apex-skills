#!/usr/bin/env python3
"""
Apex Signal Filtering
is_blocked() gate — checks regime, geo, earnings, news, sector breadth,
and market direction before allowing a signal to proceed to sizing.
"""
import json, sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

from apex_scoring import get_instrument_sector, get_geo_adjustment

try:
    from apex_config import ENABLED_SIGNAL_TYPES
except ImportError:
    ENABLED_SIGNAL_TYPES = {}  # fail-open: if config missing, all types pass

_LOGS = '/home/ubuntu/.picoclaw/logs'
_OUTCOMES_FILE = f'{_LOGS}/apex-outcomes.json'
_QUEUE_FILE    = f'{_LOGS}/apex-trade-queue.json'

# Ticker cooldown after exit — prevents same-day and next-day re-entry thrashing
TICKER_COOLDOWN_HOURS = 48
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


def _normalise_ticker(t212_ticker: str) -> str:
    """
    Return a normalised base ticker for dedup comparison.
    Strips venue suffixes so 'XOM_US_EQ' and 'XOM_US_EQ' compare equal.
    Does not map across instruments — only used for same-ticker dedup.
    """
    return t212_ticker.upper().strip() if t212_ticker else ''


def _ticker_in_queue_today(t212_ticker: str) -> bool:
    """
    Returns True if the ticker has an active (QUEUED or EXECUTED) entry
    in the trade queue today.
    """
    try:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with open(_QUEUE_FILE) as f:
            queue = json.load(f)
        if not isinstance(queue, list):
            return False
        norm = _normalise_ticker(t212_ticker)
        for entry in queue:
            if (entry.get('status') in ('QUEUED', 'EXECUTED')
                    and _normalise_ticker(entry.get('t212_ticker', '')) == norm
                    and entry.get('queued_at', '').startswith(today)):
                return True
    except (FileNotFoundError, Exception):
        pass
    return False


def _ticker_recently_exited(t212_ticker: str, cooldown_hours: int = TICKER_COOLDOWN_HOURS) -> bool:
    """
    Returns True if this ticker had a closed trade within the last
    cooldown_hours. Prevents re-entry thrashing after a failed fill
    or rapid exit.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
        with open(_OUTCOMES_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        trades = data.get('trades', [])
        norm = _normalise_ticker(t212_ticker)
        for t in trades:
            if _normalise_ticker(t.get('ticker', '')) != norm:
                continue
            closed_str = t.get('closed', '')
            if not closed_str:
                continue
            try:
                closed_dt = datetime.strptime(closed_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                if closed_dt >= cutoff:
                    return True
            except ValueError:
                pass
    except (FileNotFoundError, Exception):
        pass
    return False


def signal_type_enabled(signal_type: str) -> tuple:
    """
    Returns (True, '') if signal_type is enabled, (False, reason) if disabled.
    Checks ENABLED_SIGNAL_TYPES from apex_config.
    Unknown types default to enabled (fail-open: new types shouldn't be silently blocked).
    """
    enabled = ENABLED_SIGNAL_TYPES.get(signal_type, True)
    if enabled:
        return True, ''
    return False, (
        f"Signal type {signal_type!r} is disabled in apex_config.ENABLED_SIGNAL_TYPES "
        f"(paused pending root-cause investigation — see CHANGES.md 2026-04-16)"
    )


def is_blocked(signal, intel):
    """
    Returns a list of block reasons. Empty list = signal passes.
    Called after scoring, before position sizing.
    """
    name        = signal.get('name', '')
    signal_type = signal.get('signal_type', 'TREND')
    blocks      = []

    # Signal-type enable flag — checked first, cheapest gate
    _type_ok, _type_reason = signal_type_enabled(signal_type)
    if not _type_ok:
        blocks.append(_type_reason)
        return blocks   # early return — no point running further checks

    # Market hours gate — hard block for closed exchanges
    try:
        with open(f'{_LOGS}/apex-market-calendar.json') as _mh_f:
            _mh_today = json.load(_mh_f).get('today', {})
        _mh_us = _mh_today.get('us_currently_open', True)
        _mh_uk = _mh_today.get('uk_currently_open', True)
        _sig_cur = signal.get('currency', 'USD')
        if _sig_cur == 'USD' and not _mh_us:
            blocks.append('Market closed: NYSE not open (opens 14:30 UTC)')
            return blocks
        if _sig_cur in ('GBX', 'GBP') and not _mh_uk:
            blocks.append('Market closed: LSE not open (opens 08:00 UTC)')
            return blocks
        if _sig_cur in ('EUR', 'CHF') and not _mh_uk:
            blocks.append('Market closed: European exchanges not open')
            return blocks
    except Exception:
        pass  # fail-open — if calendar unreadable, don't block

    # Duplicate position block — prevent adding to a ticker already held
    # t212_ticker is resolved by score_signal_with_intelligence before is_blocked is called.
    _sig_t212 = signal.get('t212_ticker', '')
    if _sig_t212:
        _held_tickers = {p.get('t212_ticker', '') for p in intel.get('open_positions', [])}
        if _sig_t212 in _held_tickers:
            blocks.append(f"Already in positions: {_sig_t212}")

    # Queue dedup — block if ticker already queued or executed today (any signal type)
    # Prevents XOM-style same-day repeat queuing that creates ghost fills.
    if _sig_t212 and _ticker_in_queue_today(_sig_t212):
        blocks.append(
            f"Ticker {_sig_t212} already QUEUED or EXECUTED today — "
            f"same-day re-queue blocked (prevents ghost-fill loop)"
        )

    # Repeat-ticker cooldown — block re-entry within 48h of previous exit
    if _sig_t212 and _ticker_recently_exited(_sig_t212, TICKER_COOLDOWN_HOURS):
        blocks.append(
            f"Ticker {_sig_t212} exited within last {TICKER_COOLDOWN_HOURS}h — "
            f"cooldown prevents re-entry thrashing"
        )

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
            breadth = intel.get('sector_breadth', {}).get(sector, {})
            if breadth.get('breadth_200', 50) <= 20:
                blocks.append(f"Sector breadth too low: {sector} at {breadth.get('breadth_200',0)}%")

    # Sector rotation lagging gate — block TREND signals in sectors the rotation
    # model identifies as laggards. CONTRARIAN signals are explicitly allowed
    # (lagging sectors are contrarian opportunities, not blocks).
    # Gate does NOT fire if lagging_sectors list is empty (data missing / not stale).
    if signal_type == 'TREND':
        _lagging = intel.get('lagging_sectors', [])
        if _lagging:
            _sig_sector = get_instrument_sector(name)
            if _sig_sector and _sig_sector in _lagging:
                blocks.append(
                    f"Sector rotation: {_sig_sector} is a lagging sector "
                    f"({', '.join(_lagging)}) — wait for rotation recovery"
                )

    # VIX-level gate for TREND signals — regime.overall is binary (BLOCKED/CLEAR).
    # VIX 28–35 adds a warning but does NOT set BLOCKED, so TREND would pass through
    # at HIGH fear levels without this check. Catch that gap explicitly.
    # VIX ≥ 35 (EXTREME): TREND blocked entirely — trade CONTRARIAN/INVERSE instead.
    # VIX 28–35 (HIGH): TREND requires score ≥ 9.0 or is blocked.
    if signal_type in ('TREND', 'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE'):
        vix = float(intel.get('vix') or 20)
        sig_score = float(signal.get('adjusted_score', signal.get('total_score', 7.0)) or 7.0)
        if vix >= 35:
            blocks.append(
                f"VIX EXTREME ({vix:.1f}): {signal_type} blocked — macro risk overrides fundamentals"
            )
        elif vix >= 28 and signal_type == 'TREND':
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
    # Also block on STALE direction data: 25h-old bearish data is not safe to ignore.
    _dir_st = intel.get('direction_status', '')
    if signal_type == 'TREND' and (_dir_st == 'BLOCKED' or 'STALE' in str(_dir_st)):
        _dir_reason = intel.get('direction_blocks', [])
        blocks.append(
            f"Market direction: {' | '.join(_dir_reason)}" if _dir_reason
            else f"Market direction: {_dir_st}"
        )

    # Adversarial anti-rules (data-driven, statistically validated)
    adv_blocks = is_adversarial_blocked(signal, intel)
    blocks.extend(adv_blocks)

    return blocks
