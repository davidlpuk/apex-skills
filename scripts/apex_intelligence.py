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
    _vix_raw     = regime.get('vix', 20)
    _breadth_raw = regime.get('breadth_pct', 50)
    intel['vix']     = float(_vix_raw)     if _vix_raw     is not None else 20.0
    intel['breadth'] = float(_breadth_raw) if _breadth_raw is not None else 50.0
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

    # TACO state — geopolitical event classification (used by tiebreaker + scoring)
    taco = load_json(f'{_LOGS}/apex-taco-state.json')
    intel['taco_status']      = taco.get('status', 'NEUTRAL')
    intel['taco_threat_type'] = taco.get('threat_type', 'NONE')
    intel['taco_confidence']  = float(taco.get('confidence', 0))

    # HMM regime state — needed by tiebreaker priority matrix
    scaling = load_json(f'{_LOGS}/apex-regime-scaling.json')
    intel['hmm_state']    = scaling.get('hmm_state', 'UNKNOWN')
    intel['regime_label'] = scaling.get('regime_label', 'NEUTRAL')

    # LLM morning brief — risk posture and sector guidance for today
    # Only consumed if brief was generated today and has not expired
    _brief_path = f'{_LOGS}/apex-llm-morning-brief.json'
    brief = load_json(_brief_path)
    _brief_active = False
    if brief.get('llm_generated'):
        try:
            _exp_str = brief.get('expires_at', '')
            if _exp_str:
                _exp = datetime.fromisoformat(_exp_str.replace('Z', '+00:00'))
                _brief_active = datetime.now(timezone) < _exp
        except Exception:
            pass
    if _brief_active:
        intel['llm_risk_posture']  = brief.get('risk_posture', 'FULL')
        intel['llm_avoid_sectors'] = [s.upper() for s in brief.get('avoid_sectors', [])]
        intel['llm_max_trades']    = brief.get('max_trades_today')
        intel['llm_brief_reason']  = str(brief.get('risk_posture_reason', ''))[:120]
    else:
        intel['llm_risk_posture']  = 'FULL'
        intel['llm_avoid_sectors'] = []
        intel['llm_max_trades']    = None
        intel['llm_brief_reason']  = ''

    # Portfolio agent review — book-level risk from apex-llm-portfolio-agent.py
    # Consumed by decision engine as advisory context (not a hard gate — that's
    # the morning brief's job). HIGH/CRITICAL risk raises the min score threshold.
    _portfolio_review_path = f'{_LOGS}/apex-llm-portfolio-review.json'
    _pr = load_json(_portfolio_review_path)
    if _pr.get('llm_generated') and _pr.get('book_risk_level'):
        try:
            _pr_ts  = _pr.get('timestamp', '')
            _pr_age = (datetime.now(timezone) - datetime.fromisoformat(
                       _pr_ts.replace('Z', '+00:00'))).total_seconds() / 3600 if _pr_ts else 99
            # Only use if less than 4h old (covers pre-market review for full trading day)
            _pr_active = _pr_age < 4
        except Exception:
            _pr_active = False
    else:
        _pr_active = False

    if _pr_active:
        intel['portfolio_book_risk']    = _pr.get('book_risk_level', 'UNKNOWN')
        intel['portfolio_tail_risk']    = str(_pr.get('tail_risk', ''))[:150]
        intel['portfolio_regime_fit']   = str(_pr.get('regime_fit', ''))[:150]
        intel['portfolio_actions']      = _pr.get('position_actions', [])
    else:
        intel['portfolio_book_risk']    = 'UNKNOWN'
        intel['portfolio_tail_risk']    = ''
        intel['portfolio_regime_fit']   = ''
        intel['portfolio_actions']      = []

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
