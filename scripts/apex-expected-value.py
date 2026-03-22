#!/usr/bin/env python3
"""
Expected Value Calculator
Calculates explicit EV for every signal before execution.
Only signals with positive EV should be traded.
"""
import json
import sys
from datetime import datetime, timezone

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
EV_LOG_FILE   = '/home/ubuntu/.picoclaw/logs/apex-ev-log.json'

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
TRANSACTION_COSTS = {
    'USD':      0.0030,  # 0.30% round trip
    'GBP':      0.0020,  # 0.20% round trip
    'INVERSE':  0.0040,  # 0.40% round trip (wider spread on leveraged ETFs)
    'DEFAULT':  0.0025,  # 0.25% default
}

def get_win_rate_by_type(signal_type=None):
    """Get win rate from outcomes database, filtered by signal type."""
    try:
        with open(OUTCOMES_FILE) as f:
            db = json.load(f)
        trades = db.get('trades', [])

        if signal_type:
            trades = [t for t in trades if t.get('signal_type') == signal_type]

        if len(trades) < 5:
            # Not enough data — use conservative prior
            return 0.50, len(trades)

        wins     = sum(1 for t in trades if t.get('pnl', 0) > 0)
        win_rate = wins / len(trades)
        return round(win_rate, 3), len(trades)

    except:
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
            # Default assumptions
            return 1.5 if outcome == 'win' else 1.0

        r_values = [abs(t.get('r', 1.5)) for t in relevant]
        return round(sum(r_values) / len(r_values), 2)

    except:
        return 1.5 if outcome == 'win' else 1.0

def calculate_ev(entry, stop, target1, target2, signal_type=None, quantity=1, currency='USD'):
    """
    Calculate expected value of a trade including transaction costs.

    EV = (P_win × avg_win_amount) - (P_loss × avg_loss_amount) - transaction_costs

    For a professional system:
    - avg_win considers probability of reaching T1 vs T2
    - avg_loss is the full risk (entry to stop)
    """
    win_rate, sample_size = get_win_rate_by_type(signal_type)
    loss_rate = 1 - win_rate

    # Risk and reward per share
    risk_per_share    = entry - stop
    reward_t1         = target1 - entry
    reward_t2         = target2 - entry

    # Assumption: 40% of wins reach T2, 60% exit at T1
    # (conservative — in practice depends on trailing stop behaviour)
    avg_win_per_share = (reward_t1 * 0.6) + (reward_t2 * 0.4)

    # Total amounts
    total_risk   = risk_per_share * quantity
    total_reward = avg_win_per_share * quantity

    # Expected value
    ev = (win_rate * total_reward) - (loss_rate * total_risk)
    ev = round(ev, 2)

    # Transaction costs — deducted from EV
    if signal_type == 'INVERSE':
        tc_rate = TRANSACTION_COSTS['INVERSE']
    elif currency == 'GBP':
        tc_rate = TRANSACTION_COSTS['GBP']
    elif currency == 'USD':
        tc_rate = TRANSACTION_COSTS['USD']
    else:
        tc_rate = TRANSACTION_COSTS['DEFAULT']

    # Cost = entry_value × round_trip_rate
    entry_value      = entry * quantity
    transaction_cost = round(entry_value * tc_rate, 2)

    # Adjust EV for transaction costs
    ev_gross = ev
    ev       = round(ev - transaction_cost, 2)

    # Transaction costs — deducted from EV
    if signal_type == 'INVERSE':
        tc_rate = TRANSACTION_COSTS['INVERSE']
    elif currency == 'GBP':
        tc_rate = TRANSACTION_COSTS['GBP']
    elif currency == 'USD':
        tc_rate = TRANSACTION_COSTS['USD']
    else:
        tc_rate = TRANSACTION_COSTS['DEFAULT']

    # Cost = entry_value × round_trip_rate
    entry_value      = entry * quantity
    transaction_cost = round(entry_value * tc_rate, 2)

    # Adjust EV for transaction costs
    ev_gross = ev
    ev       = round(ev - transaction_cost, 2)

    # Transaction costs — deducted from EV
    if signal_type == 'INVERSE':
        tc_rate = TRANSACTION_COSTS['INVERSE']
    elif currency == 'GBP':
        tc_rate = TRANSACTION_COSTS['GBP']
    elif currency == 'USD':
        tc_rate = TRANSACTION_COSTS['USD']
    else:
        tc_rate = TRANSACTION_COSTS['DEFAULT']

    # Cost = entry_value × round_trip_rate
    entry_value      = entry * quantity
    transaction_cost = round(entry_value * tc_rate, 2)

    # Adjust EV for transaction costs
    ev_gross = ev
    ev       = round(ev - transaction_cost, 2)

    # EV per £1 risked — normalised metric
    ev_per_risk = round(ev / total_risk, 3) if total_risk > 0 else 0

    # R expectancy — standard trading metric
    r_expectancy = round((win_rate * (avg_win_per_share / risk_per_share)) - loss_rate, 3) if risk_per_share > 0 else 0

    # Minimum win rate needed for this trade to be positive EV
    # EV = 0 when: p × reward = (1-p) × risk
    # p = risk / (risk + reward)
    breakeven_wr = round(risk_per_share / (risk_per_share + avg_win_per_share), 3) if (risk_per_share + avg_win_per_share) > 0 else 0.5

    return {
        "entry":            entry,
        "stop":             stop,
        "target1":          target1,
        "target2":          target2,
        "quantity":         quantity,
        "risk_per_share":   round(risk_per_share, 2),
        "reward_t1":        round(reward_t1, 2),
        "reward_t2":        round(reward_t2, 2),
        "avg_win_per_share":round(avg_win_per_share, 2),
        "total_risk":       round(total_risk, 2),
        "total_reward":     round(total_reward, 2),
        "win_rate":         win_rate,
        "loss_rate":        loss_rate,
        "sample_size":      sample_size,
        "ev":               ev,
        "ev_gross":         ev_gross,
        "transaction_cost": transaction_cost,
        "tc_rate_pct":      round(tc_rate * 100, 2),
        "currency":         currency,
        "ev_per_risk":      ev_per_risk,
        "r_expectancy":     r_expectancy,
        "breakeven_wr":     breakeven_wr,
        "verdict":          "POSITIVE" if ev > 0 else ("MARGINAL" if ev > -2 else "NEGATIVE"),
        "confidence":       "HIGH" if sample_size >= 20 else ("MEDIUM" if sample_size >= 10 else "LOW — using prior"),
        "signal_type":      signal_type or "UNKNOWN"
    }

def log_ev(signal_name, ev_data):
    """Log EV calculation for every signal."""
    try:
        with open(EV_LOG_FILE) as f:
            log = json.load(f)
    except:
        log = []

    log.append({
        "date":        datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "name":        signal_name,
        "ev":          ev_data['ev'],
        "ev_per_risk": ev_data['ev_per_risk'],
        "r_expect":    ev_data['r_expectancy'],
        "win_rate":    ev_data['win_rate'],
        "verdict":     ev_data['verdict'],
        "confidence":  ev_data['confidence'],
        "signal_type": ev_data['signal_type']
    })

    with open(EV_LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)

def display_ev(name, ev_data):
    verdict_icon = "✅" if ev_data['verdict'] == 'POSITIVE' else "❌"
    conf_icon    = "🟢" if ev_data['confidence'] == 'HIGH' else ("🟡" if ev_data['confidence'] == 'MEDIUM' else "⚪")

    print(f"\n{'='*50}")
    print(f"📊 EXPECTED VALUE — {name}")
    print(f"{'='*50}")
    print(f"  Entry:          £{ev_data['entry']}")
    print(f"  Stop:           £{ev_data['stop']} (risk: £{ev_data['risk_per_share']}/share)")
    print(f"  Target 1:       £{ev_data['target1']} (reward: £{ev_data['reward_t1']}/share)")
    print(f"  Target 2:       £{ev_data['target2']} (reward: £{ev_data['reward_t2']}/share)")
    print(f"  Quantity:       {ev_data['quantity']} shares")
    print(f"")
    print(f"  Win rate used:  {round(ev_data['win_rate']*100,1)}% {conf_icon} ({ev_data['confidence']})")
    print(f"  Sample size:    {ev_data['sample_size']} trades")
    print(f"  Breakeven WR:   {round(ev_data['breakeven_wr']*100,1)}% needed")
    print(f"")
    print(f"  Total risk:     £{ev_data['total_risk']}")
    print(f"  Expected win:   £{ev_data['total_reward']}")
    print(f"  Trans costs:    £{ev_data.get('transaction_cost', 0)} ({ev_data.get('tc_rate_pct', 0)}% round trip)")
    print(f"  EV (gross):     £{ev_data.get('ev_gross', ev_data['ev'])}")
    print(f"  EV (net):       £{ev_data['ev']} {verdict_icon}")
    print(f"  EV per £1 risk: £{ev_data['ev_per_risk']}")
    print(f"  R expectancy:   {ev_data['r_expectancy']}R")
    print(f"")
    print(f"  VERDICT: {ev_data['verdict']} EV — {ev_data['confidence']}")
    print(f"{'='*50}")

if __name__ == '__main__':
    if len(sys.argv) >= 6:
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
