#!/usr/bin/env python3
"""
Apex Trading System — Centralised Configuration
Single source of truth for all paths, thresholds, and constant settings.
Import from here instead of redefining locally in each script.
"""
import sys
import os

# ── Directory Layout ──────────────────────────────────────────────────────────
BASE_DIR    = '/home/ubuntu/.picoclaw'
SCRIPTS_DIR = f'{BASE_DIR}/scripts'
LOGS_DIR    = f'{BASE_DIR}/logs'
ENV_FILE    = f'{BASE_DIR}/.env.trading212'

# ── State / Log Files ─────────────────────────────────────────────────────────
POSITIONS_FILE       = f'{LOGS_DIR}/apex-positions.json'
OUTCOMES_FILE        = f'{LOGS_DIR}/apex-outcomes.json'
PENDING_SIGNAL_FILE  = f'{LOGS_DIR}/apex-pending-signal.json'
TRADE_QUEUE_FILE     = f'{LOGS_DIR}/apex-trade-queue.json'
CIRCUIT_BREAKER_FILE = f'{LOGS_DIR}/apex-circuit-breaker.json'
PAUSE_FLAG           = f'{LOGS_DIR}/apex-paused.flag'
SCORING_WEIGHTS_FILE = f'{LOGS_DIR}/apex-scoring-weights.json'
WATCHDOG_FILE        = f'{LOGS_DIR}/apex-broker-watchdog.json'
RECON_FILE           = f'{LOGS_DIR}/apex-reconciliation.json'
DRAWDOWN_FILE        = f'{LOGS_DIR}/apex-drawdown.json'
DECISION_LOG_FILE    = f'{LOGS_DIR}/apex-decision-log.json'

# ── Circuit Breaker Thresholds (% of session open value) ─────────────────────
CB_WARNING  = -3.0    # Alert only — continue trading
CB_CAUTION  = -5.0    # Reduce sizing to 50%
CB_SUSPEND  = -8.0    # Halt all new entries
CB_CRITICAL = -12.0   # Close all positions — manual resume required
CB_RESUME   = -4.0    # Auto-resume threshold after SUSPEND

# ── Position Sizing ───────────────────────────────────────────────────────────
BASE_RISK_PCT          = 0.01   # 1% of portfolio per trade
MAX_RISK_PCT           = 0.025  # 2.5% hard cap
MIN_POSITION_VALUE     = 50     # £50 minimum position
MAX_OPEN_POSITIONS     = 6      # Maximum concurrent positions
MAX_SECTOR_POSITIONS   = 2      # Max positions in one sector

# ── Signal Quality Gates ──────────────────────────────────────────────────────
MIN_EV_RATIO           = 1.5    # Minimum expected value ratio
MIN_WIN_RATE           = 45     # Minimum historical win rate %
MIN_SIGNAL_SCORE       = 6      # Minimum score to qualify for entry

# ── Contrarian Signal Gates ───────────────────────────────────────────────────
CONTRARIAN_RSI_MAX     = 30     # RSI must be below this for contrarian entries

# ── Hold Period Caps (calendar days) ─────────────────────────────────────────
MAX_HOLD_TREND         = 15
MAX_HOLD_CONTRARIAN    = 20
MAX_HOLD_INVERSE       = 3      # Leveraged inverse ETFs decay fast

# ── T212 API Rate Limiting ────────────────────────────────────────────────────
T212_MIN_INTERVAL      = 0.6    # Min seconds between T212 API calls

# ── ATR Stop Multipliers ──────────────────────────────────────────────────────
ATR_STOP_TREND         = 2.0    # ATR multiplier for trend trades
ATR_STOP_CONTRARIAN    = 2.5    # Wider — buying into weakness needs room
ATR_STOP_INVERSE       = 1.5    # Tighter — short-term mean-reversion only
ATR_TARGET_T1          = 2.0    # T1 = entry + 2× ATR
ATR_TARGET_T2          = 3.5    # T2 = entry + 3.5× ATR

# ── Environment / Credentials ────────────────────────────────────────────────
def get_env(key: str, default: str = '') -> str:
    """Return a value from .env.trading212, delegating to apex_utils cache."""
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    try:
        from apex_utils import _load_env
        return _load_env().get(key, default)
    except ImportError:
        # Fallback: parse env file directly (no apex_utils available)
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        if k.strip() == key:
                            return v.strip()
        except Exception:
            pass
        return default
