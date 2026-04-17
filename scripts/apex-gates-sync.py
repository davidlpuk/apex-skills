#!/usr/bin/env python3
"""apex-gates-sync.py — Publish decision thresholds as queryable JSON.

Reads the live values from apex_config.py and writes apex-decision-gates.json.
Single source of truth stays in Python (imported by every script); this just
exposes the values to the agent as a readable artefact.

Why: per the agent-native guide, behaviour-controlling thresholds should be
inspectable by the agent without grepping Python. The agent reads this file
(via apex-context.md or read_state_file) to judge whether a marginal signal
is near a gate, what sector limit applies, etc.
"""
import json
import os
import sys
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import apex_config as C  # noqa: E402

OUT = os.path.join(os.path.dirname(SCRIPTS_DIR), 'logs', 'apex-decision-gates.json')


def _get(name, default=None):
    return getattr(C, name, default)


def build():
    return {
        '_generated': datetime.now(timezone.utc).isoformat(),
        '_source': 'apex_config.py (single source of truth — do not hand-edit this JSON)',

        'circuit_breaker_pct': {
            'WARNING':  _get('CB_WARNING'),
            'CAUTION':  _get('CB_CAUTION'),
            'SUSPEND':  _get('CB_SUSPEND'),
            'CRITICAL': _get('CB_CRITICAL'),
            'RESUME':   _get('CB_RESUME'),
        },
        'circuit_breaker_size_multiplier': {
            'WARNING':  _get('CB_MULT_WARNING'),
            'CAUTION':  _get('CB_MULT_CAUTION'),
            'SUSPEND':  _get('CB_MULT_SUSPEND'),
            'CRITICAL': _get('CB_MULT_CRITICAL'),
            'UNKNOWN':  _get('CB_MULT_UNKNOWN'),
        },
        'position_sizing': {
            'base_risk_pct':       _get('BASE_RISK_PCT'),
            'max_risk_pct':        _get('MAX_RISK_PCT'),
            'min_position_value':  _get('MIN_POSITION_VALUE'),
            'max_open_positions':  _get('MAX_OPEN_POSITIONS'),
            'min_counted_notional': _get('MIN_COUNTED_NOTIONAL'),
            'max_sector_positions': _get('MAX_SECTOR_POSITIONS'),
            'max_sector_notional_pct': _get('MAX_SECTOR_NOTIONAL_PCT'),
        },
        'signal_quality_gates': {
            'min_ev_ratio_gbp': _get('MIN_EV_RATIO'),
            'min_ev_ratio_usd': _get('MIN_EV_USD_RATIO'),
            'min_win_rate':     _get('MIN_WIN_RATE'),
            'min_signal_score': _get('MIN_SIGNAL_SCORE'),
            'contrarian_rsi_max': _get('CONTRARIAN_RSI_MAX'),
            'signal_max_age_hours': _get('SIGNAL_MAX_AGE_HOURS'),
        },
        'hold_periods_days': {
            'TREND':      _get('MAX_HOLD_TREND'),
            'CONTRARIAN': _get('MAX_HOLD_CONTRARIAN'),
            'INVERSE':    _get('MAX_HOLD_INVERSE'),
        },
        'atr_stop_multipliers': {
            'TREND':      _get('ATR_STOP_TREND'),
            'CONTRARIAN': _get('ATR_STOP_CONTRARIAN'),
            'INVERSE':    _get('ATR_STOP_INVERSE'),
        },
        'recovery': {
            'ramp_trades_after_suspend': _get('CB_RECOVERY_RAMP_TRADES'),
        },
        'llm_budget': {
            'daily_usd':        _get('LLM_DAILY_BUDGET_USD'),
            'alert_pct':        _get('LLM_BUDGET_ALERT_PCT'),
            'thinking_tokens':  _get('LLM_THINKING_BUDGET_TOKENS'),
        },
    }


def main():
    data = build()
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, OUT)
    print(json.dumps({
        'status': 'ok',
        'timestamp': data['_generated'],
        'path': OUT,
        'sections': [k for k in data if not k.startswith('_')],
    }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
