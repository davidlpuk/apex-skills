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

# ── Circuit Breaker Sizing Multipliers ───────────────────────────────────────
CB_MULT_WARNING  = 0.75  # Trade at 75% size during WARNING
CB_MULT_CAUTION  = 0.50  # Trade at 50% size during CAUTION
CB_MULT_SUSPEND  = 0.0   # No new trades during SUSPEND
CB_MULT_CRITICAL = 0.0   # No new trades during CRITICAL
CB_MULT_UNKNOWN  = 0.5   # Conservative default when status is unknown

# ── Position Sizing ───────────────────────────────────────────────────────────
BASE_RISK_PCT          = 0.015  # 1.5% of portfolio per trade
MAX_RISK_PCT           = 0.025  # 2.5% hard cap
MIN_POSITION_VALUE     = 50     # £50 minimum position
MAX_OPEN_POSITIONS     = 10     # [PAPER] raised from 6 — more simultaneous experiments
MIN_COUNTED_NOTIONAL   = 150    # Positions below this notional (£) are dust — not counted toward limit
MAX_SECTOR_POSITIONS   = 3      # [PAPER] raised from 2 — allow more sector diversity
MAX_SECTOR_NOTIONAL_PCT = 0.20  # [PAPER] raised from 0.10 — less sector concentration constraint

# ── Signal Quality Gates ──────────────────────────────────────────────────────
MIN_EV_RATIO           = 1.2    # [PAPER] lowered from 1.5 — test marginal EV trades on virtual money
MIN_EV_USD_RATIO       = 1.5    # [PAPER] lowered from 2.0 — less FX drag penalty on paper
MIN_WIN_RATE           = 40     # [PAPER] lowered from 45 — let more signal types earn data
MIN_SIGNAL_SCORE       = 5      # [PAPER] lowered from 6 — wider funnel for learning

# ── Signal Type Enable Flags ─────────────────────────────────────────────────
# Disabled types are paused pending root-cause investigation.
# Do NOT delete entries — flip to True once edge-proof verdict reaches PROVEN.
# See CHANGES.md 2026-04-16 for rationale.
ENABLED_SIGNAL_TYPES = {
    'TREND':            True,
    'CONTRARIAN':       True,
    'INVERSE':          True,
    'MANUAL':           True,     # always-on — human-gated
    'GEO_REVERSAL':     False,    # 6/6 ghost rate as of 2026-04-16, root cause TBD
    'EARNINGS_DRIFT':   False,    # 2/2 ghost rate as of 2026-04-16, root cause TBD
    'TACO_CONTRARIAN':  False,    # 0/1 WR, insufficient real fills
    'DIVIDEND_CAPTURE': False,    # 0 real trades, untested in production
}

# ── Contrarian Signal Gates ───────────────────────────────────────────────────
CONTRARIAN_RSI_MAX     = 38     # RSI must be below this for contrarian entries

# ── Hold Period Caps (calendar days) ─────────────────────────────────────────
MAX_HOLD_TREND         = 10     # [PAPER] reduced from 15 — faster turnover = more closed trades = more data
MAX_HOLD_CONTRARIAN    = 15     # [PAPER] reduced from 20
MAX_HOLD_INVERSE       = 3      # Leveraged inverse ETFs decay fast

# ── T212 API Rate Limiting ────────────────────────────────────────────────────
T212_MIN_INTERVAL      = 0.6    # Min seconds between T212 API calls

# ── Order Fill Polling ────────────────────────────────────────────────────────
# 9 × 20s = 3 min total — same wait window as before, but half the API calls.
# Burst rate (≥18 calls / 3min) was triggering Cloudflare error-1010 IP blocks
# on T212 (geo-throttle), particularly when multiple execution attempts ran
# back-to-back. Reduce to 9 × 20s to stay safely under the per-IP burst ceiling.
# See: scripts/CLAUDE.md "T212 Cloudflare Rate-Limit (Geo-1010)" lesson.
T212_FILL_POLL_COUNT   = 9      # Attempts before deferring (9 × 20s = 3 min)
T212_FILL_POLL_INTERVAL = 20    # Seconds between fill-status polls

# ── Limit-price slippage premium ─────────────────────────────────────────────
# A passive BUY limit set at the inside ask never crosses the spread for
# illiquid instruments — VAGS bond ETF sat NEW for 9 polls × 20s on 2026-04-16
# without a single fill. Adding a small premium turns it into a "marketable
# limit": still capped (no runaway market fill) but priced through the
# inside ask so it executes immediately under normal spreads.
#
# 0.15% (15 bps) covers spreads on most liquid LSE GBP ETFs and US large-caps.
# For known-illiquid instruments override via T212_LIMIT_PREMIUM_BPS_OVERRIDE.
# Hard cap: premium can never exceed half the entry-to-stop distance, ensuring
# the entry never opens already inside the stop's risk envelope.
T212_LIMIT_PREMIUM_BPS              = 15      # 0.15% premium on BUY limits
T212_LIMIT_PREMIUM_BPS_ILLIQUID     = 35      # 0.35% for low-volume ETFs
T212_LIMIT_PREMIUM_MAX_FRAC_OF_STOP = 0.5     # Cap at 50% of stop distance

# Tickers known to be illiquid (wide spread, low volume) — get the higher
# premium. Add new entries here when fills repeatedly fail at the standard
# premium. Tracked in scripts/CLAUDE.md.
T212_ILLIQUID_TICKERS = {
    'VAGSl_EQ',   # Vanguard Global Aggregate Bond — bond ETF, 15-30 bps spread
    'IBTSl_EQ',   # iShares 0-1y Treasury — short-duration bond
    'IS15l_EQ',   # iShares 1-5y Treasury — short-duration bond
    'VGOVl_EQ',   # Vanguard UK Gilt
    'AIGEl_EQ',   # Bloomberg Energy commodity ETC
    'AIGPl_EQ',   # Bloomberg Precious Metals ETC
    'ICOMl_EQ',   # Bloomberg Commodity ETC
    'COPAl_EQ',   # WisdomTree Copper ETC
}

# ── ATR Stop Multipliers ──────────────────────────────────────────────────────
ATR_STOP_TREND         = 2.0    # ATR multiplier for trend trades
ATR_STOP_CONTRARIAN    = 2.5    # Wider — buying into weakness needs room
ATR_STOP_INVERSE       = 1.5    # Tighter — short-term mean-reversion only
ATR_TARGET_T1          = 2.0    # T1 = entry + 2× ATR
ATR_TARGET_T2          = 3.5    # T2 = entry + 3.5× ATR

# ── Signal Lifecycle ─────────────────────────────────────────────────────────
SIGNAL_MAX_AGE_HOURS = 6   # Signals older than this are expired and deleted

# ── Circuit Breaker Recovery & Rolling Drawdown ───────────────────────────────
CB_RECOVERY_RAMP_TRADES = 5      # trades at 50% sizing after SUSPEND auto-resume
CB_ROLLING_THRESHOLDS   = {
    3:  -8.0,    # 3-day cumulative loss > 8%  → CAUTION
    5:  -10.0,   # 5-day cumulative loss > 10% → SUSPEND
    10: -15.0,   # 10-day cumulative loss > 15% → CRITICAL
}

# ── LLM / AI Integration ─────────────────────────────────────────────────────
# Set GEMINI_API_KEY and ANTHROPIC_API_KEY in .env.trading212.
# All LLM features degrade gracefully to rule-based fallbacks if keys absent.
#
# Provider selection: LLM_PROVIDER controls which model handles thinking-tier
# calls (preflight, tiebreaker, TACO, morning-brief, queue-revalidate).
# Fast-tier calls (sentiment batch scoring) always use Gemini Flash.
# Switch at runtime via: python3 apex_llm_client.py provider gemini|anthropic
# Or Telegram: LLM PROVIDER anthropic|gemini
GEMINI_API_KEY      = ''   # Populated at runtime via get_env() below
ANTHROPIC_API_KEY   = ''   # Populated at runtime via get_env() below
LLM_PROVIDER                 = 'anthropic'       # 'anthropic' | 'gemini'
LLM_SENTIMENT_MODEL          = 'gemini-2.5-flash'  # fast-tier (unchanged)
LLM_THINKING_MODEL_ANTHROPIC = 'claude-sonnet-4-6'
LLM_THINKING_MODEL_GEMINI    = 'gemini-2.5-pro'
LLM_THINKING_BUDGET_TOKENS   = 2048   # max thinking tokens — increase for harder calls
LLM_DAILY_BUDGET_USD         = 2.00   # hard daily spend cap — falls back to fast model
LLM_BUDGET_ALERT_PCT         = 0.80   # send Telegram alert at this fraction of daily budget
LLM_TIMEOUT                  = 30     # seconds per fast API call
LLM_THINKING_TIMEOUT         = 90     # seconds per thinking-tier call (reasoning takes longer)

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

# Populate LLM keys at import time (after get_env is defined)
GEMINI_API_KEY    = get_env('GEMINI_API_KEY', '')
ANTHROPIC_API_KEY = get_env('ANTHROPIC_API_KEY', '')
