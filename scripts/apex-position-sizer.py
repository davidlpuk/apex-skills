#!/usr/bin/env python3
import json
import sys

LOGS = '/home/ubuntu/.picoclaw/logs'


def _performance_multiplier():
    """
    Scale sizing based on actual strategy performance (Sharpe + benchmark alpha).
    Returns 1.0 when insufficient data (cold-start safe).
    """
    try:
        with open(f'{LOGS}/apex-sharpe.json') as f:
            sharpe_data = json.load(f)
        total_trades = sharpe_data.get('total_trades', 0)
        if total_trades < 5:
            return 1.0  # Cold-start — no adjustment until 5 trades closed

        sharpe = float(sharpe_data.get('sharpe_ratio', 0))
        if sharpe >= 2.0:
            mult = 1.30
        elif sharpe >= 1.0:
            mult = 1.15
        elif sharpe >= 0.5:
            mult = 1.00
        elif sharpe >= 0.0:
            mult = 0.85
        else:
            mult = 0.70
    except Exception:
        return 1.0

    # Benchmark alpha bonus/penalty
    try:
        with open(f'{LOGS}/apex-benchmark.json') as f:
            bench = json.load(f)
        alpha = float(bench.get('alpha_pct', 0))
        if alpha > 2.0:
            mult += 0.10
        elif alpha < -2.0:
            mult -= 0.10
    except Exception:
        pass

    return max(0.5, min(1.5, round(mult, 2)))


def _momentum_multiplier():
    """
    Scale sizing based on recent trade streak (wins/losses in a row).
    Returns 1.0 when insufficient data (cold-start safe).
    """
    try:
        with open(f'{LOGS}/apex-outcomes.json') as f:
            outcomes = json.load(f)
        trades = outcomes.get('trades', [])
        if len(trades) < 3:
            return 1.0  # Cold-start — need at least 3 trades

        recent = trades[-5:]  # Look at last 5
        results = [1 if t.get('pnl', 0) > 0 else -1 for t in recent]

        # Winning streaks
        if len(results) >= 5 and all(r > 0 for r in results[-5:]):
            return 1.25
        if len(results) >= 3 and all(r > 0 for r in results[-3:]):
            return 1.15
        if len(results) >= 2 and all(r > 0 for r in results[-2:]):
            return 1.05

        # Losing streaks
        if len(results) >= 3 and all(r < 0 for r in results[-3:]):
            return 0.75
        if len(results) >= 2 and all(r < 0 for r in results[-2:]):
            return 0.90

        return 1.0
    except Exception:
        return 1.0


def calculate_position(
    portfolio_value,
    entry_price,
    stop_price,
    signal_score,
    max_score,
    signal_type,
    vix=20,
    breadth=50,
    quality_score=5,
    currency='GBP'
):
    """
    Dynamic position sizing based on:
    - Signal conviction (score)
    - Market regime (VIX + breadth)
    - Signal type (trend vs contrarian)
    - Quality of the instrument
    """

    # Fix 9: Regime scale from continuous apex-regime-scaling.json
    # (replaces VIX/breadth tier table with pre-computed per-signal-type scale)
    try:
        import json as _j
        _rs = _j.load(open('/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'))
        _scale_map = {
            'TREND':            _rs.get('trend_scale',      1.0),
            'CONTRARIAN':       _rs.get('contrarian_scale', 1.0),
            'INVERSE':          _rs.get('inverse_scale',    1.0),
            'DIVIDEND_CAPTURE': _rs.get('dividend_scale',   1.0),
            'EARNINGS_DRIFT':   _rs.get('drift_scale',      1.0),
            'GEO_REVERSAL':     _rs.get('combined_scale',   1.0),
        }
        regime_scale = _scale_map.get(signal_type, _rs.get('combined_scale', 0.5))
    except Exception:
        # Fallback to VIX/breadth tier table
        if vix < 15 and breadth >= 60:   regime_scale = 1.0
        elif vix < 20 and breadth >= 50: regime_scale = 0.8
        elif vix < 25 and breadth >= 40: regime_scale = 0.6
        elif vix < 30:                   regime_scale = 0.4
        else:                            regime_scale = 0.2

    # Conviction multiplier based on score
    score_pct = signal_score / max_score
    if score_pct >= 0.9:
        conviction = 1.5    # Top score — size up 50%
    elif score_pct >= 0.8:
        conviction = 1.25   # High score — size up 25%
    elif score_pct >= 0.7:
        conviction = 1.0    # Good score — standard size
    else:
        conviction = 0.75   # Marginal — size down

    # Signal type adjustment
    if signal_type == "CONTRARIAN":
        conviction *= 0.8   # Contrarian trades are higher risk — size down slightly
    elif signal_type == "GEO_REVERSAL":
        conviction *= 1.2   # Geo reversal with clear fundamental = size up

    # Quality adjustment
    if quality_score >= 9:
        conviction *= 1.1
    elif quality_score <= 6:
        conviction *= 0.9

    # Fix 8: Kelly sizing — use Thorp half-Kelly risk_gbp once we have 50+ real trades
    # Until then (status=COLLECTING), fall back to percentage-based sizing
    _kelly_risk = None
    try:
        import json as _jk
        _thorp = _jk.load(open('/home/ubuntu/.picoclaw/logs/apex-thorp-test.json'))
        if _thorp.get('status') != 'COLLECTING':
            _kelly_risk = _thorp.get('kelly_table', {}).get(signal_type, {}).get('risk_gbp')
    except Exception:
        pass

    base_risk_pct = 0.02  # Fixed 2% reference base

    if _kelly_risk:
        # Kelly half risk × conviction × regime scale
        risk_amount = round(float(_kelly_risk) * conviction * regime_scale, 2)
        risk_pct    = risk_amount / portfolio_value if portfolio_value else base_risk_pct
    else:
        # Percentage-based fallback using continuous regime scale
        risk_pct    = base_risk_pct * conviction * regime_scale
        risk_amount = round(portfolio_value * risk_pct, 2)

    # Performance feedback — scale based on actual Sharpe + benchmark alpha
    perf_mult = _performance_multiplier()
    if perf_mult != 1.0:
        risk_amount = round(risk_amount * perf_mult, 2)

    # Momentum feedback — scale based on recent win/loss streak
    mom_mult = _momentum_multiplier()
    if mom_mult != 1.0:
        risk_amount = round(risk_amount * mom_mult, 2)

    # Combined performance+momentum cap: never exceed 1.5x or drop below 0.5x
    _combined_perf = perf_mult * mom_mult
    if _combined_perf > 1.5:
        risk_amount = round(risk_amount / _combined_perf * 1.5, 2)
        _combined_perf = 1.5
    elif _combined_perf < 0.5:
        risk_amount = round(risk_amount / _combined_perf * 0.5, 2)
        _combined_perf = 0.5

    # Drawdown adjustment
    try:
        with open(f'{LOGS}/apex-drawdown.json') as _f:
            _dd = json.load(_f)
        dd_multiplier = float(_dd.get('multiplier', 1.0))
        dd_status     = _dd.get('status', 'NORMAL')
        if dd_multiplier < 1.0:
            risk_amount   = round(risk_amount * dd_multiplier, 2)
    except:
        dd_multiplier = 1.0
        dd_status     = 'NORMAL'

    # FX cost deduction — USD instruments incur 0.15% each way (0.30% round trip) on T212
    if currency == 'USD':
        fx_cost = round(risk_amount * 0.003, 2)
        risk_amount = round(risk_amount - fx_cost, 2)

    # Crash mode correlation check — if all positions suddenly correlated, cut to 25%
    try:
        import importlib.util as _ilu_cm
        _spec_cm = _ilu_cm.spec_from_file_location(
            "rtc_cm", "/home/ubuntu/.picoclaw/scripts/apex-realtime-correlation.py")
        _rtc_cm = _ilu_cm.module_from_spec(_spec_cm)
        _spec_cm.loader.exec_module(_rtc_cm)
        _crash_mult, _crash_status = _rtc_cm.get_crash_mode_multiplier()
        if _crash_mult < 1.0:
            risk_amount = round(risk_amount * _crash_mult, 2)
    except Exception:
        pass

    # Hard limits
    risk_amount = min(risk_amount, 100)   # Never more than £100 risk
    risk_amount = max(risk_amount, 10)    # Never less than £10 risk

    # Multiplier floor guard — warn when stacked multipliers compress sizing unexpectedly.
    _effective_mult = conviction * regime_scale * _combined_perf * dd_multiplier
    if _effective_mult < 0.15:
        print(
            f"[SIZER WARN] Effective multiplier {_effective_mult:.3f}x is very low "
            f"(conviction={conviction:.2f} × regime={regime_scale:.2f} × "
            f"perf={perf_mult:.2f} × mom={mom_mult:.2f} × drawdown={dd_multiplier:.2f}). "
            f"risk_amount clamped to £{risk_amount}.",
            file=sys.stderr
        )

    # Position sizing
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return None

    quantity = round(risk_amount / risk_per_share, 2)
    notional = round(quantity * entry_price, 2)

    # Position size limit
    max_notional = portfolio_value * 0.08   # Max 8% of portfolio in one position
    if notional > max_notional:
        quantity = round(max_notional / entry_price, 2)
        notional = round(quantity * entry_price, 2)
        risk_amount = round(quantity * risk_per_share, 2)

    return {
        "quantity":      quantity,
        "notional":      notional,
        "risk_amount":   risk_amount,
        "risk_pct":      round(risk_pct * 100, 2),
        "conviction":    round(conviction, 2),
        "base_risk_pct": round(base_risk_pct * 100, 2),
        "perf_multiplier": perf_mult,
        "momentum_multiplier": mom_mult,
        "sizing_rationale": (
            f"Base {round(base_risk_pct*100,1)}% × conviction {round(conviction,2)} × "
            f"regime {round(regime_scale,2)} × perf {round(perf_mult,2)} × "
            f"mom {round(mom_mult,2)} × drawdown {round(dd_multiplier,2)} "
            f"= {round(risk_pct*100,2)}% risk (effective {round(_effective_mult,3)}x)"
        )
    }

if __name__ == '__main__':
    # Test
    result = calculate_position(
        portfolio_value=5000,
        entry_price=159.01,
        stop_price=151.00,
        signal_score=9,
        max_score=10,
        signal_type="GEO_REVERSAL",
        vix=24,
        breadth=30,
        quality_score=8
    )
    print(json.dumps(result, indent=2))
