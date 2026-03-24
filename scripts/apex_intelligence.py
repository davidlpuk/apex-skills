#!/usr/bin/env python3
"""
Apex Intelligence Gathering
Loads all intelligence files into a single dict used by the scoring,
filtering, and sizing layers.
"""
import json
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, log_error
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m): print(f'ERROR: {m}')

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

    # Drawdown
    drawdown = load_json(DRAWDOWN_FILE)
    intel['drawdown_pct']    = drawdown.get('drawdown_pct', 0)
    intel['drawdown_status'] = drawdown.get('status', 'NORMAL')
    intel['size_multiplier'] = drawdown.get('multiplier', 1.0)

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

    return intel
