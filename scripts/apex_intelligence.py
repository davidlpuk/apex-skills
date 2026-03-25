#!/usr/bin/env python3
"""
Apex Intelligence Gathering
Loads all intelligence files into a single dict used by the scoring,
filtering, and sizing layers.
"""
import json
import os
import sys
from datetime import datetime, timezone
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, log_error
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m): print(f'ERROR: {m}')

try:
    from apex_config import (CB_MULT_WARNING, CB_MULT_CAUTION,
                              CB_MULT_SUSPEND, CB_MULT_CRITICAL, CB_MULT_UNKNOWN)
except ImportError:
    CB_MULT_WARNING  = 0.75
    CB_MULT_CAUTION  = 0.50
    CB_MULT_SUSPEND  = 0.0
    CB_MULT_CRITICAL = 0.0
    CB_MULT_UNKNOWN  = 0.5

# ── File paths ────────────────────────────────────────────────────────────────
_LOGS = '/home/ubuntu/.picoclaw/logs'
_SCRIPTS = '/home/ubuntu/.picoclaw/scripts'

REGIME_FILE     = f'{_LOGS}/apex-regime.json'
GEO_FILE        = f'{_LOGS}/apex-geo-news.json'
DIRECTION_FILE  = f'{_LOGS}/apex-market-direction.json'
SECTOR_ROT_FILE = f'{_LOGS}/apex-sector-rotation.json'
BREADTH_FILE    = f'{_LOGS}/apex-breadth-drilldown.json'
VIX_CORR_FILE   = f'{_LOGS}/apex-vix-correlation.json'
DRAWDOWN_FILE   = f'{_LOGS}/apex-drawdown.json'
BREAKER_FILE    = f'{_LOGS}/apex-circuit-breaker.json'
EARNINGS_FILE   = f'{_LOGS}/apex-earnings-flags.json'
NEWS_FILE       = f'{_LOGS}/apex-news-flags.json'
DRIFT_FILE      = f'{_LOGS}/apex-earnings-drift.json'
DIVIDEND_FILE   = f'{_LOGS}/apex-dividend-capture.json'
POSITIONS_FILE  = f'{_LOGS}/apex-positions.json'


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default or {}


def _file_age_hours(path):
    """Return hours since a JSON file's internal timestamp field, falling back to mtime."""
    try:
        with open(path) as f:
            ts = json.load(f).get('timestamp', '')
        dt = datetime.strptime(ts, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
    except Exception:
        pass
    try:
        return round((datetime.now(timezone.utc).timestamp() - os.path.getmtime(path)) / 3600, 1)
    except Exception:
        return 99.0


def gather_intelligence():
    intel = {}

    # Regime
    regime = load_json(REGIME_FILE)
    intel['vix']            = float(regime.get('vix', 20))
    intel['breadth']        = float(regime.get('breadth_pct', 50))
    intel['regime_status']  = regime.get('overall', 'CLEAR')
    intel['regime_reasons'] = regime.get('block_reason', [])

    # Geo
    geo = load_json(GEO_FILE)
    intel['geo_status']       = geo.get('overall', 'CLEAR')
    intel['geo_energy_flags'] = geo.get('energy_flags', [])
    intel['geo_flags']        = geo.get('geo_flags', [])

    # Market direction
    direction = load_json(DIRECTION_FILE)
    intel['direction_status'] = direction.get('overall', 'CLEAR')
    intel['direction_blocks'] = direction.get('blocks', [])

    # Sector rotation
    sector_rot = load_json(SECTOR_ROT_FILE)
    sectors    = sector_rot.get('sectors', [])
    intel['leading_sectors'] = sector_rot.get('leaders', [])
    intel['lagging_sectors'] = sector_rot.get('laggards', [])
    intel['sector_scores']   = {s['name']: s['score'] for s in sectors}

    # Sector breadth
    breadth_data = load_json(BREADTH_FILE)
    intel['sector_breadth']   = breadth_data.get('sectors', {})
    intel['strongest_sector'] = breadth_data.get('strongest')
    intel['weakest_sector']   = breadth_data.get('weakest')

    # VIX correlation of current positions
    vix_corr = load_json(VIX_CORR_FILE)
    intel['position_vix_sensitivity'] = {
        p['ticker']: p['vix_corr']
        for p in vix_corr.get('positions', [])
    }

    # Drawdown (peak-to-trough across days/weeks)
    drawdown = load_json(DRAWDOWN_FILE)
    intel['drawdown_pct']    = drawdown.get('drawdown_pct', 0)
    intel['drawdown_status'] = drawdown.get('status', 'NORMAL')
    drawdown_mult = float(drawdown.get('multiplier', 1.0))

    # Circuit breaker (intra-session loss) — apply the more conservative of the two
    # Multipliers sourced from apex_config — edit there, not here
    _CB_MULTS = {
        'CLEAR': 1.0, 'WARNING': CB_MULT_WARNING, 'CAUTION': CB_MULT_CAUTION,
        'SUSPEND': CB_MULT_SUSPEND, 'CRITICAL': CB_MULT_CRITICAL, 'UNKNOWN': CB_MULT_UNKNOWN,
    }
    breaker   = load_json(BREAKER_FILE)
    cb_status = breaker.get('status', 'CLEAR')
    cb_mult   = _CB_MULTS.get(cb_status, 1.0)
    # Honour recovery ramp: 50% sizing for N trades after SUSPEND auto-resume
    if breaker.get('recovery_trades_remaining', 0) > 0 and cb_mult > 0:
        cb_mult = round(cb_mult * 0.5, 2)

    intel['cb_status']       = cb_status
    intel['size_multiplier'] = min(drawdown_mult, cb_mult)

    # Earnings and news flags
    try:
        with open(EARNINGS_FILE) as f:
            earnings_flags = json.load(f)
        intel['earnings_blocked'] = [d['name'] if isinstance(d, dict) else d for d in earnings_flags]
    except Exception:
        intel['earnings_blocked'] = []

    try:
        with open(NEWS_FILE) as f:
            intel['news_blocked'] = json.load(f)
    except Exception:
        intel['news_blocked'] = []

    # Drift signals
    drift = load_json(DRIFT_FILE)
    intel['drift_signals'] = drift.get('signals', [])

    # Dividend signals
    dividend = load_json(DIVIDEND_FILE)
    intel['dividend_signals'] = dividend.get('signals', [])

    # Open positions
    intel['open_positions'] = load_json(POSITIONS_FILE, [])

    # Data provenance — age in hours of each key input file at time of gather
    _PROVENANCE_FILES = {
        'regime':           REGIME_FILE,
        'market_direction': DIRECTION_FILE,
        'geo':              GEO_FILE,
        'sector_rotation':  SECTOR_ROT_FILE,
        'breadth':          BREADTH_FILE,
        'drawdown':         DRAWDOWN_FILE,
        'circuit_breaker':  BREAKER_FILE,
        'macro_signals':    f'{_LOGS}/apex-macro-signals.json',
        'sentiment':        f'{_LOGS}/apex-sentiment.json',
        'backtest_insights':f'{_LOGS}/apex-backtest-v2-insights.json',
    }
    intel['file_ages_hours'] = {k: _file_age_hours(v) for k, v in _PROVENANCE_FILES.items()}

    # Market-direction staleness gate — if data is >12h old, flag it rather than silently use it
    _dir_age = intel['file_ages_hours'].get('market_direction', 99.0)
    if _dir_age > 12:
        intel['direction_stale'] = True
        intel['direction_status'] = f"STALE ({_dir_age:.0f}h old)"
    else:
        intel['direction_stale'] = False

    return intel
