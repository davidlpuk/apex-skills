#!/usr/bin/env python3
import json
import sys

def calculate_position(
    portfolio_value,
    entry_price,
    stop_price,
    signal_score,
    max_score,
    signal_type,
    vix=20,
    breadth=50,
    quality_score=5
):
    """
    Dynamic position sizing based on:
    - Signal conviction (score)
    - Market regime (VIX + breadth)
    - Signal type (trend vs contrarian)
    - Quality of the instrument
    """

    # Base risk % — adjusts by regime
    if vix < 15 and breadth >= 60:
        base_risk_pct = 0.025   # 2.5% — benign market, size up
    elif vix < 20 and breadth >= 50:
        base_risk_pct = 0.02    # 2.0% — normal market
    elif vix < 25 and breadth >= 40:
        base_risk_pct = 0.015   # 1.5% — slightly elevated
    elif vix < 30:
        base_risk_pct = 0.01    # 1.0% — high VIX, standard
    else:
        base_risk_pct = 0.005   # 0.5% — extreme fear, size way down

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

    # Calculate final risk amount
    risk_pct    = base_risk_pct * conviction
    risk_amount = round(portfolio_value * risk_pct, 2)

    # Drawdown adjustment
    try:
        import json as _json
        with open('/home/ubuntu/.picoclaw/logs/apex-drawdown.json') as _f:
            _dd = _json.load(_f)
        dd_multiplier = float(_dd.get('multiplier', 1.0))
        dd_status     = _dd.get('status', 'NORMAL')
        if dd_multiplier < 1.0:
            risk_amount   = round(risk_amount * dd_multiplier, 2)
    except:
        dd_multiplier = 1.0
        dd_status     = 'NORMAL'

    # Hard limits
    risk_amount = min(risk_amount, 100)   # Never more than £100 risk
    risk_amount = max(risk_amount, 10)    # Never less than £10 risk

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
        "sizing_rationale": f"Base {round(base_risk_pct*100,1)}% × conviction {round(conviction,2)} = {round(risk_pct*100,2)}% risk"
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
