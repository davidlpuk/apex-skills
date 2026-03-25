#!/usr/bin/env python3
"""
Expected Value Calculator
Calculates explicit EV for every signal before execution.
Only signals with positive EV should be traded.
"""
import json
import sys
from datetime import datetime, timezone

OUTCOMES_FILE    = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
EV_LOG_FILE      = '/home/ubuntu/.picoclaw/logs/apex-ev-log.json'
PARAM_FILE       = '/home/ubuntu/.picoclaw/logs/apex-param-log.json'
MAE_MFE_FILE     = '/home/ubuntu/.picoclaw/logs/apex-mae-mfe-calibration.json'

# Transaction cost model — T212 specific
# USD instruments: 0.15% FX conversion each way = 0.30% round trip
# UK instruments:  0.10% spread estimate = 0.20% round trip
# Leveraged ETFs:  0.20% spread estimate = 0.40% round trip
TRANSACTION_COSTS = {
    'USD':      0.0030,  # 0.30% round trip
    'GBP':      0.0020,  # 0.20% round trip
    'INVERSE':  0.0040,  # 0.40% round trip (wider spread on leveraged ETFs)
    'DEFAULT':  0.0025,  # 0.25% default
}

# Slippage model — ATR-based market impact estimate.
# Spread/FX costs above are broker-level; slippage is the additional cost from
# entering/exiting at a price different to the signal calculation price.
# For T212 retail order sizes this is dominated by bid-ask spread movement
# during the order, not volume impact.
#
# k = fraction of ATR used as per-side slippage estimate.
# Empirical basis: for liquid large-caps, bid-ask is ~0.03–0.08% of price, and
# execution rarely moves the market. k=0.04 (4% of ATR) is conservative.
# For illiquid names the true slippage can be 3–5× higher.
#
# Fallback (no ATR data): 0.08% per side = 0.16% round trip.
# This is intentionally higher than the TC spread to avoid underestimating costs.
SLIPPAGE_ATR_FACTOR   = 0.04   # 4% of ATR per side
SLIPPAGE_FALLBACK_PCT = 0.0016 # 0.16% round trip when no ATR available


def estimate_slippage(entry: float, quantity: float = 1,
                      atr: float = None, currency: str = 'USD') -> float:
    """
    Estimate round-trip slippage cost for a trade.

    When ATR is provided:
        slippage = 2 × k × ATR × quantity
        (entry and exit each slip by k × ATR on average)

    When ATR is not available:
        slippage = entry × SLIPPAGE_FALLBACK_PCT × quantity

    Returns total slippage cost in quote currency (same units as EV).
    """
    if atr and atr > 0:
        per_side   = SLIPPAGE_ATR_FACTOR * atr
        total_slip = 2 * per_side * quantity          # entry + exit
    else:
        total_slip = entry * SLIPPAGE_FALLBACK_PCT * quantity
    return round(total_slip, 4)

# Default T1/T2 split — used when no empirical data available
# 60% close at T1, 40% hold to T2 (conservative)
DEFAULT_T1_SPLIT = 0.60

# Prior reward achievement discount — applied when no empirical T1/T2 data exists.
# Empirical observation: early-stage trades achieve ~0.22R vs model expectation of 2.6R
# (12× overestimate). 0.45 is a conservative middle-ground that makes the EV gate
# meaningful without being so harsh it blocks all signals.
# Removed once t_sample >= 5 (empirical data takes over).
PRIOR_REWARD_DISCOUNT = 0.45


def get_win_rate_by_type(signal_type=None):
    """Get win rate from outcomes database, filtered by signal type."""
    try:
        with open(OUTCOMES_FILE) as f:
            db = json.load(f)
        trades = db.get('trades', [])

        if signal_type:
            trades = [t for t in trades if t.get('signal_type') == signal_type]

        if len(trades) < 3:
            # Not enough data — use conservative prior
            return 0.50, len(trades)

        wins     = sum(1 for t in trades if t.get('pnl', 0) > 0)
        win_rate = wins / len(trades)
        return round(win_rate, 3), len(trades)

    except Exception:
        return 0.50, 0


def get_avg_r_by_type(signal_type=None, outcome='win'):
    """Get average R multiple for wins or losses."""
    try:
        with open(OUTCOMES_FILE) as f:
            db = json.load(f)
        trades = db.get('trades', [])

        if signal_type:
            trades = [t for t in trades if t.get('signal_type') == signal_type]

        if outcome == 'win':
            relevant = [t for t in trades if t.get('pnl', 0) > 0]
        else:
            relevant = [t for t in trades if t.get('pnl', 0) <= 0]

        if not relevant:
            return 1.5 if outcome == 'win' else 1.0

        r_values = [abs(t.get('r', 1.5)) for t in relevant]
        return round(sum(r_values) / len(r_values), 2)

    except Exception:
        return 1.5 if outcome == 'win' else 1.0


def get_t1_split(signal_type=None) -> tuple:
    """
    Derive empirical T1/T2 split. Priority order:
      1. apex-mae-mfe-calibration.json  — most accurate (full MFE analysis)
      2. apex-param-log.json / apex-outcomes.json — direct r_achieved analysis
      3. DEFAULT_T1_SPLIT (0.60)        — conservative prior

    Returns (t1_fraction, sample_size) where t1_fraction is the proportion of
    wins that closed at T1 rather than running to T2.
    """
    # --- 1. MAE/MFE calibration file (most accurate) ---
    try:
        with open(MAE_MFE_FILE) as f:
            cal = json.load(f)
        # Try signal-type-specific first, then aggregate
        if signal_type:
            t1_splits = cal.get('t1_splits', {})
            if signal_type in t1_splits:
                t1_frac = float(t1_splits[signal_type])
                sig_cal = cal.get('by_signal_type', {}).get(signal_type, {})
                n       = sig_cal.get('n_wins', sig_cal.get('mfe', {}).get('n', 0))
                return round(t1_frac, 3), n
        # Aggregate
        agg_t1 = cal.get('aggregate_t1_fraction')
        if agg_t1 is not None:
            n = cal.get('n_wins_total', 0)
            return round(float(agg_t1), 3), n
    except Exception:
        pass

    # --- 2. Direct r_achieved analysis ---
    try:
        try:
            with open(PARAM_FILE) as f:
                log_data = json.load(f)
            signals = log_data.get('signals', [])
        except Exception:
            signals = []

        if not signals:
            with open(OUTCOMES_FILE) as f:
                db = json.load(f)
            signals = db.get('trades', [])

        wins = [s for s in signals
                if s.get('outcome') == 'WIN' or s.get('pnl', 0) > 0]
        if signal_type:
            wins = [s for s in wins if s.get('signal_type') == signal_type]
        if len(wins) < 5:
            return DEFAULT_T1_SPLIT, 0

        t2_count = 0
        for w in wins:
            r_achieved   = abs(w.get('r_achieved', w.get('r', 0)))
            t2_threshold = abs(w.get('r_target2', 2.5))
            if r_achieved >= t2_threshold:
                t2_count += 1

        t1_frac = round(1.0 - t2_count / len(wins), 3)
        return t1_frac, len(wins)

    except Exception:
        pass

    return DEFAULT_T1_SPLIT, 0


def calculate_ev(entry, stop, target1, target2, signal_type=None, quantity=1,
                 currency='USD', atr=None):
    """
    Calculate expected value of a trade including transaction costs and slippage.

    EV = (P_win × avg_win_amount) - (P_loss × avg_loss_amount)
         - transaction_costs - slippage_estimate

    The T1/T2 split (what fraction of winners exit at T1 vs T2) is derived
    empirically from closed trade history when ≥10 winning trades are available,
    otherwise falls back to the DEFAULT_T1_SPLIT (60/40).

    atr: Average True Range of the instrument (optional). When provided, slippage
    is estimated as 2 × SLIPPAGE_ATR_FACTOR × ATR × quantity. Without it, a
    conservative fixed percentage fallback is used.
    """
    win_rate, sample_size = get_win_rate_by_type(signal_type)
    loss_rate = 1 - win_rate

    # Risk and reward per share
    risk_per_share = entry - stop
    reward_t1      = target1 - entry
    reward_t2      = target2 - entry

    # Empirical T1/T2 split
    t1_frac, t_sample = get_t1_split(signal_type)
    t2_frac           = 1.0 - t1_frac

    avg_win_per_share = (reward_t1 * t1_frac) + (reward_t2 * t2_frac)

    # Apply prior reward discount when no empirical T1/T2 data.
    # Without this, model expects ~2.6R/win which guarantees positive EV on every
    # signal regardless of quality. Discount brings expectation in line with observed
    # outcomes (0.22R empirical vs 2.6R model). Removed once empirical data available.
    if t_sample < 5:
        avg_win_per_share *= PRIOR_REWARD_DISCOUNT

    # Total amounts
    total_risk   = risk_per_share * quantity
    total_reward = avg_win_per_share * quantity

    # Expected value
    ev_gross = round((win_rate * total_reward) - (loss_rate * total_risk), 2)

    # Transaction costs — deducted from EV (single application, not triplicated)
    if signal_type == 'INVERSE':
        tc_rate = TRANSACTION_COSTS['INVERSE']
    elif currency == 'GBP':
        tc_rate = TRANSACTION_COSTS['GBP']
    elif currency == 'USD':
        tc_rate = TRANSACTION_COSTS['USD']
    else:
        tc_rate = TRANSACTION_COSTS['DEFAULT']

    entry_value      = entry * quantity
    transaction_cost = round(entry_value * tc_rate, 2)
    slippage_cost    = estimate_slippage(entry, quantity, atr=atr, currency=currency)
    total_costs      = round(transaction_cost + slippage_cost, 4)
    ev               = round(ev_gross - total_costs, 2)

    # EV per £1 risked — normalised metric
    ev_per_risk = round(ev / total_risk, 3) if total_risk > 0 else 0

    # R expectancy — standard trading metric
    r_expectancy = round(
        (win_rate * (avg_win_per_share / risk_per_share)) - loss_rate, 3
    ) if risk_per_share > 0 else 0

    # Minimum win rate for positive EV
    breakeven_wr = round(
        risk_per_share / (risk_per_share + avg_win_per_share), 3
    ) if (risk_per_share + avg_win_per_share) > 0 else 0.5

    using_discount = t_sample < 5
    t_split_label = (f"empirical {round(t1_frac*100)}%/{round(t2_frac*100)}% T1/T2 (n={t_sample})"
                     if not using_discount
                     else f"prior {round(DEFAULT_T1_SPLIT*100)}%/{round((1-DEFAULT_T1_SPLIT)*100)}% T1/T2 (×{PRIOR_REWARD_DISCOUNT} discount, n={t_sample})")

    return {
        "entry":             entry,
        "stop":              stop,
        "target1":           target1,
        "target2":           target2,
        "quantity":          quantity,
        "risk_per_share":    round(risk_per_share, 2),
        "reward_t1":         round(reward_t1, 2),
        "reward_t2":         round(reward_t2, 2),
        "avg_win_per_share": round(avg_win_per_share, 2),
        "t1_fraction":       t1_frac,
        "t2_fraction":       t2_frac,
        "t_split_source":    t_split_label,
        "total_risk":        round(total_risk, 2),
        "total_reward":      round(total_reward, 2),
        "win_rate":          win_rate,
        "loss_rate":         loss_rate,
        "sample_size":       sample_size,
        "ev":                ev,
        "ev_gross":          ev_gross,
        "transaction_cost":  transaction_cost,
        "slippage_cost":     slippage_cost,
        "total_costs":       round(total_costs, 4),
        "tc_rate_pct":       round(tc_rate * 100, 2),
        "slippage_atr_used": atr is not None and atr > 0,
        "currency":          currency,
        "ev_per_risk":       ev_per_risk,
        "r_expectancy":      r_expectancy,
        "breakeven_wr":      breakeven_wr,
        "verdict":           "POSITIVE" if ev > 0 else ("MARGINAL" if ev > -2 else "NEGATIVE"),
        "confidence":        "HIGH" if sample_size >= 20 else ("MEDIUM" if sample_size >= 10 else "LOW — using prior"),
        "signal_type":       signal_type or "UNKNOWN",
        "fx_degraded":       currency == 'USD',
        "fx_drag_pct":       round(tc_rate * 100, 2) if currency == 'USD' else 0,
        "effective_min_ev_ratio": 2.0 if currency == 'USD' else 1.5,
    }


def log_ev(signal_name, ev_data):
    """Log EV calculation for every signal."""
    try:
        with open(EV_LOG_FILE) as f:
            log = json.load(f)
    except Exception:
        log = []

    log.append({
        "date":        datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "name":        signal_name,
        "ev":          ev_data['ev'],
        "ev_per_risk": ev_data['ev_per_risk'],
        "r_expect":    ev_data['r_expectancy'],
        "win_rate":    ev_data['win_rate'],
        "t1_fraction": ev_data.get('t1_fraction', DEFAULT_T1_SPLIT),
        "verdict":     ev_data['verdict'],
        "confidence":  ev_data['confidence'],
        "signal_type": ev_data['signal_type']
    })

    with open(EV_LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)


def display_ev(name, ev_data):
    verdict_icon = "+" if ev_data['verdict'] == 'POSITIVE' else "-"
    conf_icon    = "HI" if ev_data['confidence'] == 'HIGH' else ("MID" if ev_data['confidence'] == 'MEDIUM' else "LOW")

    print(f"\n{'='*50}")
    print(f"EXPECTED VALUE — {name}")
    print(f"{'='*50}")
    print(f"  Entry:          {ev_data['entry']}")
    print(f"  Stop:           {ev_data['stop']} (risk: {ev_data['risk_per_share']}/share)")
    print(f"  Target 1:       {ev_data['target1']} (reward: {ev_data['reward_t1']}/share)")
    print(f"  Target 2:       {ev_data['target2']} (reward: {ev_data['reward_t2']}/share)")
    print(f"  T1/T2 split:    {ev_data.get('t_split_source','?')}")
    print(f"  Quantity:       {ev_data['quantity']} shares")
    print(f"")
    print(f"  Win rate used:  {round(ev_data['win_rate']*100,1)}% [{conf_icon}] ({ev_data['confidence']})")
    print(f"  Sample size:    {ev_data['sample_size']} trades")
    print(f"  Breakeven WR:   {round(ev_data['breakeven_wr']*100,1)}% needed")
    print(f"")
    print(f"  Total risk:     {ev_data['total_risk']}")
    print(f"  Expected win:   {ev_data['total_reward']}")
    slip_src = "ATR-based" if ev_data.get('slippage_atr_used') else "fixed fallback"
    print(f"  Trans costs:    {ev_data.get('transaction_cost', 0)} ({ev_data.get('tc_rate_pct', 0)}% round trip)")
    print(f"  Slippage:       {ev_data.get('slippage_cost', 0)} ({slip_src})")
    print(f"  Total costs:    {ev_data.get('total_costs', ev_data.get('transaction_cost', 0))}")
    print(f"  EV (gross):     {ev_data.get('ev_gross', ev_data['ev'])}")
    print(f"  EV (net):       {ev_data['ev']} [{verdict_icon}]")
    print(f"  EV per unit risk: {ev_data['ev_per_risk']}")
    print(f"  R expectancy:   {ev_data['r_expectancy']}R")
    print(f"")
    print(f"  VERDICT: {ev_data['verdict']} EV — {ev_data['confidence']}")
    print(f"{'='*50}")


if __name__ == '__main__':
    if '--test' in sys.argv:
        print("EV Calculator — Self Tests")
        print("=" * 40)

        # T1/T2 split defaults when no data
        t1, n = get_t1_split('TREND')
        assert t1 == DEFAULT_T1_SPLIT, f"Expected {DEFAULT_T1_SPLIT}, got {t1}"
        print(f"get_t1_split (no data): t1={t1}, n={n}: PASS")

        # calculate_ev basic
        result = calculate_ev(
            entry=100.0, stop=96.0, target1=108.0, target2=114.0,
            signal_type='CONTRARIAN', quantity=10, currency='GBP'
        )
        assert result['risk_per_share'] == 4.0
        assert result['reward_t1']      == 8.0
        assert result['reward_t2']      == 14.0
        assert result['verdict'] in ('POSITIVE', 'NEGATIVE', 'MARGINAL')
        assert result['transaction_cost'] > 0
        assert result['slippage_cost']    > 0
        assert result['total_costs'] == round(result['transaction_cost'] + result['slippage_cost'], 4)
        # Verify ev = ev_gross - total_costs (slippage + TC applied exactly once)
        assert abs(result['ev'] - (result['ev_gross'] - result['total_costs'])) < 0.01, \
            f"Cost deduction error: ev={result['ev']}, gross={result['ev_gross']}, total_costs={result['total_costs']}"
        print(f"calculate_ev (no ATR): ev={result['ev']}, tc={result['transaction_cost']}, "
              f"slip={result['slippage_cost']}, split={result['t_split_source']}: PASS")

        # Test with ATR — slippage should be ATR-based (larger on volatile stock)
        result_atr = calculate_ev(
            entry=100.0, stop=96.0, target1=108.0, target2=114.0,
            signal_type='CONTRARIAN', quantity=10, currency='GBP', atr=3.0
        )
        assert result_atr['slippage_atr_used'] is True
        expected_slip = round(2 * SLIPPAGE_ATR_FACTOR * 3.0 * 10, 4)
        assert abs(result_atr['slippage_cost'] - expected_slip) < 0.001, \
            f"ATR slippage: expected {expected_slip}, got {result_atr['slippage_cost']}"
        print(f"calculate_ev (ATR=3.0): slip={result_atr['slippage_cost']} (expected {expected_slip}): PASS")

        print("\n" + "=" * 40)
        print("ALL TESTS PASSED")

    elif len(sys.argv) >= 6:
        name      = sys.argv[1]
        entry     = float(sys.argv[2])
        stop      = float(sys.argv[3])
        t1        = float(sys.argv[4])
        t2        = float(sys.argv[5])
        qty       = float(sys.argv[6]) if len(sys.argv) > 6 else 1
        sig_type  = sys.argv[7] if len(sys.argv) > 7 else None

        result = calculate_ev(entry, stop, t1, t2, sig_type, qty)
        display_ev(name, result)
        log_ev(name, result)
    else:
        # Test with Apple example
        result = calculate_ev(
            entry=252.00, stop=236.88,
            target1=274.68, target2=289.80,
            signal_type='CONTRARIAN', quantity=1.59
        )
        display_ev("Apple (test)", result)
