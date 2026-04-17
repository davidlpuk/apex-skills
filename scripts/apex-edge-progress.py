#!/usr/bin/env python3
"""
Apex Edge Progress — daily "are we getting better?" tracker.

Reads:
  apex-outcomes.json         — all closed trades
  apex-edge-proof.json       — current verdicts + per-strategy stats
  apex-edge-proof-history.json — historical snapshots for trend lines

Writes:
  apex-edge-progress.json    — dashboard-ready progress summary

The output answers four user-facing questions:
  1. How close is each strategy to having statistically meaningful data?
     (n_real / 20 — the threshold above which edge-proof drops backtest)
  2. How much new evidence did this week add?
  3. Is uncertainty actually shrinking? (Wilson CI width over time)
  4. At the current trade rate, how many days until each strategy could
     plausibly reach CONFIRMED?

This is the headline "improvement signal" — it changes daily even when
verdicts don't, because n_real and CI width move long before verdicts flip.

Runs:  10 minutes after apex-edge-proof.py (Mon 07:18 UTC), and after EOD.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_LOGS = '/home/ubuntu/.picoclaw/logs'
_OUTCOMES_FILE   = f'{_LOGS}/apex-outcomes.json'
_EDGE_PROOF_FILE = f'{_LOGS}/apex-edge-proof.json'
_HISTORY_FILE    = f'{_LOGS}/apex-edge-proof-history.json'
_OUTPUT_FILE     = f'{_LOGS}/apex-edge-progress.json'

# Same threshold used by apex-edge-proof._REAL_ONLY_THRESHOLD — above this,
# real trades stand alone (no backtest supplement needed).
_TARGET_N = 20

# Strategies to track (must match _TYPE_ALIASES in apex-edge-proof.py)
_STRATEGIES = ['TREND', 'CONTRARIAN', 'INVERSE',
               'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE']

# Same alias mapping as edge-proof
_TYPE_ALIASES = {
    'TREND':            ['TREND'],
    'CONTRARIAN':       ['CONTRARIAN'],
    'INVERSE':          ['INVERSE'],
    'EARNINGS_DRIFT':   ['EARNINGS_DRIFT', 'EARNINGS'],
    'DIVIDEND_CAPTURE': ['DIVIDEND_CAPTURE', 'DIVIDEND'],
}


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _classify(trade):
    raw = (trade.get('signal_type') or trade.get('outcome_type') or '').upper()
    for stype, aliases in _TYPE_ALIASES.items():
        if any(a in raw for a in aliases):
            return stype
    if 'MANUAL' not in raw:
        return 'TREND'
    return None


def _parse_iso(s):
    """Parse an ISO date or datetime to a tz-aware UTC datetime."""
    if not s:
        return None
    s = s.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        # Date-only string (e.g. "2026-04-15")
        try:
            return datetime.fromisoformat(s + 'T00:00:00+00:00')
        except ValueError:
            return None


def _trade_close_dt(trade):
    return _parse_iso(trade.get('closed_iso') or trade.get('closed'))


def _per_strategy_progress(trades, edge_proof_data):
    """
    For each strategy compute:
      n_real, target_n, pct_to_target,
      trades_last_7d, trades_last_30d,
      trade_rate_per_week (rolling 30d),
      days_to_target (at current rate, capped/floor at sensible limits),
      verdict, dsr_probability.
    """
    now = datetime.now(timezone.utc)
    cutoff_7d  = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    by_type = defaultdict(list)
    for t in trades:
        stype = _classify(t)
        if stype in _STRATEGIES:
            by_type[stype].append(t)

    out = {}
    ep = edge_proof_data.get('by_signal_type', {})

    for stype in _STRATEGIES:
        ts = by_type.get(stype, [])
        n_real = len(ts)

        # Date-bucket counts
        n_7d  = sum(1 for t in ts
                    if (dt := _trade_close_dt(t)) and dt >= cutoff_7d)
        n_30d = sum(1 for t in ts
                    if (dt := _trade_close_dt(t)) and dt >= cutoff_30d)

        # Trade rate per week from 30-day window (more stable than 7-day)
        rate_per_week = round(n_30d / 30 * 7, 2) if n_30d else 0.0

        # Days to reach _TARGET_N at current rate
        if n_real >= _TARGET_N:
            days_to_target = 0
        elif rate_per_week > 0:
            remaining = _TARGET_N - n_real
            days_to_target = int(round(remaining / (rate_per_week / 7)))
        else:
            days_to_target = None  # no recent activity → can't project

        ep_entry = ep.get(stype, {})
        out[stype] = {
            'n_real':            n_real,
            'target_n':          _TARGET_N,
            'pct_to_target':     round(min(100, n_real / _TARGET_N * 100), 1),
            'trades_last_7d':    n_7d,
            'trades_last_30d':   n_30d,
            'trade_rate_per_wk': rate_per_week,
            'days_to_target':    days_to_target,
            'verdict':           ep_entry.get('verdict', 'INSUFFICIENT_DATA'),
            'dsr_probability':   ep_entry.get('dsr_probability', 0.0),
            'win_rate_pct':      ep_entry.get('win_rate_pct'),
            'ci_width':          (ep_entry.get('ci_95', [0, 100])[1]
                                  - ep_entry.get('ci_95', [0, 100])[0]),
        }
    return out


def _ci_tightening_series(history, lookback_days=30):
    """
    Build per-strategy time series of (timestamp, n_real, ci_width) over
    the last `lookback_days`, plus a 7-day delta on ci_width.

    The CI width is the most informative scalar of "how uncertain are we?":
      - At n=2 with 100% WR, width ≈ 66pp
      - At n=20 with 60% WR, width ≈ 40pp
      - At n=100 with 60% WR, width ≈ 20pp
    A shrinking CI width means real progress, even if verdict hasn't moved.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    series = defaultdict(list)
    for snap in history:
        dt = _parse_iso(snap.get('timestamp'))
        if not dt or dt < cutoff:
            continue
        for stype, v in snap.get('by_signal_type', {}).items():
            if stype in _STRATEGIES:
                series[stype].append({
                    'ts':       snap['timestamp'],
                    'n_real':   v.get('n_real', 0),
                    'ci_width': v.get('ci_width', 100.0),
                })

    # Compute 7d delta in CI width (negative = tightening, good)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    deltas = {}
    for stype, points in series.items():
        # Most recent snapshot
        latest = points[-1] if points else None
        # Snapshot closest to (now - 7d)
        prior = None
        for p in points:
            dt = _parse_iso(p['ts'])
            if dt and dt <= cutoff_7d:
                prior = p
        if latest and prior:
            deltas[stype] = {
                'ci_width_now':   latest['ci_width'],
                'ci_width_7d_ago': prior['ci_width'],
                'ci_width_delta': round(latest['ci_width'] - prior['ci_width'], 2),
                'n_real_now':      latest['n_real'],
                'n_real_7d_ago':   prior['n_real'],
                'n_real_delta':    latest['n_real'] - prior['n_real'],
            }
        else:
            deltas[stype] = None

    return dict(series), deltas


def _summary_headline(progress):
    """Single-sentence headline for the dashboard card."""
    proven   = sum(1 for p in progress.values() if p['verdict'] == 'CONFIRMED')
    marginal = sum(1 for p in progress.values() if p['verdict'] == 'MARGINAL')
    near     = sum(1 for p in progress.values()
                   if p['verdict'] != 'CONFIRMED'
                   and p['n_real'] >= _TARGET_N // 2)
    total_trades = sum(p['n_real'] for p in progress.values())

    if proven > 0:
        return f"{proven} strategy/strategies CONFIRMED — capital allocation can scale."
    if marginal > 0:
        return f"{marginal} marginal, {near} approaching threshold ({total_trades} trades total)."
    if near > 0:
        return f"{near} strategy/strategies past 50% of target sample — early evidence forming."
    return f"Early data collection ({total_trades} trades). No verdicts yet — keep gathering data."


def main():
    trades  = _load_json(_OUTCOMES_FILE, {}).get('trades', [])
    ep      = _load_json(_EDGE_PROOF_FILE, {})
    history = _load_json(_HISTORY_FILE, [])

    progress = _per_strategy_progress(trades, ep)
    series, deltas = _ci_tightening_series(history, lookback_days=30)

    output = {
        'timestamp':       datetime.now(timezone.utc).isoformat(),
        'target_n':        _TARGET_N,
        'headline':        _summary_headline(progress),
        'by_signal_type':  progress,
        'ci_series_30d':   series,
        'deltas_7d':       deltas,
        'total_real_trades': sum(p['n_real'] for p in progress.values()),
        'history_snapshots': len(history),
    }

    with open(_OUTPUT_FILE, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"Apex Edge Progress — written to {_OUTPUT_FILE}")
    print(f"  {output['headline']}")
    print()
    for stype, p in progress.items():
        bar_len = int(p['pct_to_target'] / 5)  # 20 chars max
        bar = '█' * bar_len + '░' * (20 - bar_len)
        d7 = deltas.get(stype) or {}
        ci_delta = d7.get('ci_width_delta')
        ci_arrow = '↓' if ci_delta and ci_delta < 0 else ('↑' if ci_delta and ci_delta > 0 else '–')
        ci_str = f"  CI {ci_arrow}{abs(ci_delta):.1f}pp/7d" if ci_delta is not None else ""
        days = p['days_to_target']
        days_str = f"~{days}d" if days else "—"
        print(f"  {stype:18s} [{bar}] {p['n_real']:2d}/{_TARGET_N}"
              f"  +{p['trades_last_7d']}/7d  →{days_str}{ci_str}  [{p['verdict']}]")


if __name__ == '__main__':
    main()
