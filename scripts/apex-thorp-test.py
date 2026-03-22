#!/usr/bin/env python3
"""
Pillar 2: Ergodicity & Sizing Audit (The Thorp Test)
Kelly Criterion position sizing with ruination risk check.

NOW: Calculates Kelly using backtest priors, logs every sizing decision.
ACTIVATES FULLY: At 50+ real trades with confirmed win rate.

Answers:
- What is the mathematically optimal position size?
- Is there ruination risk at current sizing?
- Does the proposed size survive a 5-trade losing streak?
"""
import json
import math
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

THORP_FILE    = '/home/ubuntu/.picoclaw/logs/apex-thorp-test.json'
OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
PARAM_FILE    = '/home/ubuntu/.picoclaw/logs/apex-param-log.json'

# Thresholds
MIN_TRADES_KELLY     = 50   # Minimum trades before full Kelly activates
HALF_KELLY_FACTOR    = 0.5  # Professional standard — use half Kelly
MAX_POSITION_PCT     = 0.08 # Hard cap — never exceed 8% portfolio
RUINATION_THRESHOLD  = 0.20 # Portfolio drawdown that triggers ruination check

# Backtest priors — used until real data available
BACKTEST_PRIORS = {
    'TREND':            {'win_rate': 0.517, 'avg_win_r': 1.06, 'avg_loss_r': 0.66},
    'CONTRARIAN':       {'win_rate': 0.565, 'avg_win_r': 1.20, 'avg_loss_r': 0.85},
    'INVERSE':          {'win_rate': 0.500, 'avg_win_r': 1.50, 'avg_loss_r': 1.00},
    'EARNINGS_DRIFT':   {'win_rate': 0.550, 'avg_win_r': 1.30, 'avg_loss_r': 0.80},
    'DIVIDEND_CAPTURE': {'win_rate': 0.600, 'avg_win_r': 0.80, 'avg_loss_r': 0.60},
}

def get_real_stats(signal_type):
    """Get real win rate and R stats from outcomes database."""
    try:
        param_log = safe_read(PARAM_FILE, {'signals': []})
        closed    = [s for s in param_log.get('signals', [])
                    if s.get('outcome') in ['WIN','LOSS']
                    and s.get('signal_type') == signal_type]

        if len(closed) < MIN_TRADES_KELLY:
            return None, len(closed)

        wins      = [s for s in closed if s.get('outcome') == 'WIN']
        losses    = [s for s in closed if s.get('outcome') == 'LOSS']
        win_rate  = len(wins) / len(closed)
        avg_win_r = sum(s.get('r_achieved', 1.5) for s in wins) / len(wins) if wins else 1.5
        avg_los_r = abs(sum(s.get('r_achieved', -1.0) for s in losses) / len(losses)) if losses else 1.0

        return {
            'win_rate':   round(win_rate, 3),
            'avg_win_r':  round(avg_win_r, 3),
            'avg_loss_r': round(avg_los_r, 3),
            'sample':     len(closed),
            'source':     'REAL_DATA'
        }, len(closed)

    except Exception as e:
        log_error(f"get_real_stats failed: {e}")
        return None, 0

def calculate_kelly(win_rate, avg_win_r, avg_loss_r):
    """
    Full Kelly Criterion.
    f* = (b*p - q) / b
    where b = reward/risk ratio, p = win rate, q = loss rate
    """
    if avg_loss_r <= 0:
        return 0, 0

    b = avg_win_r / avg_loss_r  # Reward to risk ratio
    p = win_rate
    q = 1 - win_rate

    kelly_full = (b * p - q) / b
    kelly_half = kelly_full * HALF_KELLY_FACTOR

    return round(max(0, kelly_full), 4), round(max(0, kelly_half), 4)

def check_ruination_risk(kelly_fraction, portfolio_value, risk_per_trade, consecutive_losses=5):
    """
    Ruination check — what happens after N consecutive losses?
    Professional standard: portfolio should survive 10 consecutive max losses.
    """
    if risk_per_trade <= 0 or portfolio_value <= 0:
        return False, 0, "Cannot calculate"

    # Simulate N consecutive losses
    remaining = portfolio_value
    for i in range(consecutive_losses):
        remaining -= risk_per_trade
        if remaining <= 0:
            return True, 0, f"RUIN after {i+1} consecutive losses"

    drawdown_pct = round((portfolio_value - remaining) / portfolio_value * 100, 1)
    survival_pct = round(remaining / portfolio_value * 100, 1)

    ruined = drawdown_pct > RUINATION_THRESHOLD * 100

    return ruined, drawdown_pct, (
        f"After {consecutive_losses} losses: -{drawdown_pct}% ({survival_pct}% remaining)"
    )

def calculate_optimal_size(signal, portfolio_value=5000):
    """
    Calculate Kelly-optimal position size for a signal.
    Returns full sizing recommendation with ruination check.
    """
    sig_type   = signal.get('signal_type', 'TREND')
    entry      = float(signal.get('entry', 0))
    stop       = float(signal.get('stop', 0))
    target1    = float(signal.get('target1', 0))

    if entry <= 0 or stop <= 0:
        return None

    risk_per_share   = entry - stop
    reward_per_share = target1 - entry if target1 > entry else risk_per_share * 1.5

    if risk_per_share <= 0:
        return None

    r_ratio = round(reward_per_share / risk_per_share, 2)

    # Get stats — real data if available, priors otherwise
    real_stats, sample_count = get_real_stats(sig_type)

    if real_stats:
        stats  = real_stats
        source = f"Real data ({sample_count} trades)"
        using_prior = False
    else:
        stats  = BACKTEST_PRIORS.get(sig_type, BACKTEST_PRIORS['TREND'])
        source = f"Backtest prior (need {MIN_TRADES_KELLY - sample_count} more trades)"
        using_prior = True

    win_rate  = stats['win_rate']
    avg_win_r = stats.get('avg_win_r', r_ratio)
    avg_los_r = stats.get('avg_loss_r', 1.0)

    # Kelly calculation
    kelly_full, kelly_half = calculate_kelly(win_rate, avg_win_r, avg_los_r)

    # Convert Kelly fraction to £ risk
    kelly_full_risk = round(portfolio_value * kelly_full, 2)
    kelly_half_risk = round(portfolio_value * kelly_half, 2)

    # Hard cap
    max_risk = round(portfolio_value * MAX_POSITION_PCT, 2)
    recommended_risk = min(kelly_half_risk, max_risk)

    # Shares and notional
    shares    = round(recommended_risk / risk_per_share, 2) if risk_per_share > 0 else 0
    notional  = round(shares * entry, 2)

    # Ruination check at recommended size
    ruined, dd_pct, ruin_msg = check_ruination_risk(
        kelly_half, portfolio_value, recommended_risk, consecutive_losses=10
    )

    # Devil's advocate counter-thesis
    counter_thesis = generate_counter_thesis(signal, stats, using_prior)

    # Verdict
    if ruined:
        verdict = "REDUCE"
        verdict_reason = f"Ruination risk: {ruin_msg}"
    elif not using_prior and kelly_half < 0:
        verdict = "ABORT"
        verdict_reason = "Negative Kelly — no mathematical edge at current stats"
    elif using_prior and win_rate < 0.45:
        verdict = "REDUCE"
        verdict_reason = "Prior win rate below 45% — reduce to minimum size"
    else:
        verdict = "APPROVED"
        verdict_reason = f"Kelly half = {round(kelly_half*100,1)}% of portfolio"

    return {
        'signal_type':      sig_type,
        'entry':            entry,
        'stop':             stop,
        'risk_per_share':   round(risk_per_share, 2),
        'r_ratio':          r_ratio,
        'stats_source':     source,
        'using_prior':      using_prior,
        'sample_count':     sample_count,
        'win_rate':         win_rate,
        'kelly_full_pct':   round(kelly_full * 100, 2),
        'kelly_half_pct':   round(kelly_half * 100, 2),
        'kelly_full_risk':  kelly_full_risk,
        'kelly_half_risk':  kelly_half_risk,
        'recommended_risk': recommended_risk,
        'recommended_shares':shares,
        'notional':         notional,
        'max_risk_cap':     max_risk,
        'ruination_check':  ruin_msg,
        'ruination_risk':   ruined,
        'drawdown_10loss':  dd_pct,
        'counter_thesis':   counter_thesis,
        'verdict':          verdict,
        'verdict_reason':   verdict_reason,
    }

def generate_counter_thesis(signal, stats, using_prior):
    """
    Generate Devil's Advocate counter-thesis.
    Cold, clinical, mathematical.
    """
    sig_type  = signal.get('signal_type', 'TREND')
    win_rate  = stats.get('win_rate', 0.5)
    name      = signal.get('name', '?')
    rsi       = signal.get('rsi', 50)
    confidence= signal.get('confidence_pct', 0)

    theses = []

    if using_prior:
        theses.append(
            f"Win rate of {round(win_rate*100,1)}% is a backtest prior on historical data — "
            f"live performance may diverge significantly in current market regime."
        )
    else:
        theses.append(
            f"Sample size of {stats.get('sample',0)} trades is statistically insufficient "
            f"to distinguish skill from luck at {round(win_rate*100,1)}% win rate."
        )

    if sig_type == 'CONTRARIAN':
        theses.append(
            f"{name} RSI {rsi} is oversold — but oversold can remain oversold for weeks. "
            f"Mean reversion assumes a mean to revert to which has not been verified."
        )
    elif sig_type == 'TREND':
        theses.append(
            f"{name} trend signal may be late-stage momentum. "
            f"High RSI entries historically show lower win rates when momentum exhausts."
        )
    elif sig_type == 'INVERSE':
        theses.append(
            f"Leveraged inverse ETF subject to volatility decay — "
            f"3x leverage loses value in sideways markets regardless of direction."
        )

    if confidence < 70:
        theses.append(
            f"Confidence {confidence}% indicates intelligence layers are not strongly aligned. "
            f"Low confidence signals have higher variance outcomes."
        )

    return theses[:2]  # Return 2 strongest counter-arguments

def run():
    """Generate full Thorp test report."""
    now   = datetime.now(timezone.utc)
    print(f"\n=== THORP TEST — ERGODICITY & SIZING AUDIT ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    print(f"  Kelly Criterion sizing table (backtest priors):")
    print(f"  {'Signal Type':20} {'WR':8} {'Full Kelly':12} {'Half Kelly':12} {'£ Risk*':10}")
    print(f"  {'-'*65}")

    portfolio = 5000
    results   = {}

    for sig_type, stats in BACKTEST_PRIORS.items():
        wr        = stats['win_rate']
        avg_win_r = stats['avg_win_r']
        avg_los_r = stats['avg_loss_r']
        kf, kh    = calculate_kelly(wr, avg_win_r, avg_los_r)
        risk_gbp  = round(min(portfolio * kh, portfolio * MAX_POSITION_PCT), 2)

        results[sig_type] = {
            'win_rate':    wr,
            'kelly_full':  kf,
            'kelly_half':  kh,
            'risk_gbp':    risk_gbp,
        }

        print(f"  {sig_type:20} {round(wr*100,1):6}%  "
              f"{round(kf*100,1):10}%  {round(kh*100,1):10}%  £{risk_gbp:8}")

    print(f"\n  * Based on £{portfolio} portfolio, capped at {MAX_POSITION_PCT*100}%")
    print(f"\n  Current sizing: £50 flat risk")
    print(f"  Kelly half (CONTRARIAN): £{results['CONTRARIAN']['risk_gbp']}")
    print(f"  Kelly half (TREND):      £{results['TREND']['risk_gbp']}")
    print(f"\n  Status: COLLECTING — activates at {MIN_TRADES_KELLY} real trades")
    print(f"  Using backtest priors until sufficient live data accumulated")

    # Ruination check at current £50 sizing
    ruin, dd, msg = check_ruination_risk(0.01, portfolio, 50, consecutive_losses=10)
    print(f"\n  Ruination check (current £50 sizing, 10 consecutive losses):")
    print(f"  {msg}")
    print(f"  {'⚠️ RUINATION RISK' if ruin else '✅ Survives 10 consecutive losses'}")

    output = {
        'timestamp':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'status':        'COLLECTING',
        'min_trades':    MIN_TRADES_KELLY,
        'portfolio':     portfolio,
        'kelly_table':   results,
        'current_risk':  50,
        'half_kelly_factor': HALF_KELLY_FACTOR,
        'max_position_pct':  MAX_POSITION_PCT,
    }

    atomic_write(THORP_FILE, output)
    print(f"\n✅ Thorp test saved")
    return output

def audit_signal(signal, portfolio_value=5000):
    """Called by decision engine for every signal."""
    result = calculate_optimal_size(signal, portfolio_value)
    if not result:
        return None

    # Save to log
    try:
        log = safe_read(THORP_FILE, {})
        audits = log.get('signal_audits', [])
        audits.append({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'signal':    signal.get('name','?'),
            'verdict':   result['verdict'],
            'kelly_half_risk': result['kelly_half_risk'],
            'recommended_risk': result['recommended_risk'],
        })
        log['signal_audits'] = audits[-100:]
        atomic_write(THORP_FILE, log)
    except Exception as e:
        log_error(f"Thorp audit save failed: {e}")

    return result

if __name__ == '__main__':
    run()

    # Test with current XOM signal
    print("\n" + "="*60)
    print("SAMPLE AUDIT — XOM CONTRARIAN")
    print("="*60)
    test_signal = {
        'name': 'XOM', 'signal_type': 'CONTRARIAN',
        'entry': 159.67, 'stop': 153.94,
        'target1': 171.02, 'rsi': 61.9,
        'confidence_pct': 73.3,
    }
    result = calculate_optimal_size(test_signal, 5000)
    if result:
        print(f"\n  Win rate used:      {round(result['win_rate']*100,1)}% ({result['stats_source']})")
        print(f"  Full Kelly:         {result['kelly_full_pct']}% = £{result['kelly_full_risk']}")
        print(f"  Half Kelly:         {result['kelly_half_pct']}% = £{result['kelly_half_risk']}")
        print(f"  Recommended risk:   £{result['recommended_risk']}")
        print(f"  Recommended shares: {result['recommended_shares']}")
        print(f"  Ruination check:    {result['ruination_check']}")
        print(f"\n  Devil's Advocate:")
        for i, thesis in enumerate(result['counter_thesis'], 1):
            print(f"  {i}. {thesis}")
        print(f"\n  VERDICT: {result['verdict']} — {result['verdict_reason']}")
