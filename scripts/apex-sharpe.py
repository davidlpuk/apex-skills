#!/usr/bin/env python3
"""
Sharpe Ratio Tracker
Calculates risk-adjusted return on closed trades.
Target: Sharpe > 1.0. World class: > 2.0
"""
import json
import sys
import math
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
SHARPE_FILE   = '/home/ubuntu/.picoclaw/logs/apex-sharpe.json'

# UK base rate / risk-free rate (annualised)
RISK_FREE_RATE = 0.05  # 5% — approximately current UK base rate

def calculate_sharpe():
    try:
        with open(OUTCOMES_FILE) as f:
            db = json.load(f)
        trades = db.get('trades', [])
    except:
        print("No outcomes data yet")
        return None

    if len(trades) < 3:
        print(f"Need at least 3 trades for Sharpe calculation (have {len(trades)})")
        return None

    # Extract daily returns
    # Each trade return = pnl / capital_at_risk
    returns = []
    for t in trades:
        pnl      = t.get('pnl', 0)
        risk     = t.get('risk_amount', abs(t.get('entry', 0) - t.get('stop', 0)) * t.get('quantity', 1))
        if risk > 0:
            trade_return = pnl / risk  # Return as multiple of risk
            returns.append(trade_return)

    if len(returns) < 3:
        print("Insufficient return data")
        return None

    n        = len(returns)
    avg_ret  = sum(returns) / n
    variance = sum((r - avg_ret) ** 2 for r in returns) / (n - 1)
    std_dev  = math.sqrt(variance)

    # Annualised Sharpe
    # Assume ~2 trades per week = ~100 trades per year
    trades_per_year = 100
    daily_rf        = RISK_FREE_RATE / trades_per_year
    excess_return   = avg_ret - daily_rf

    sharpe = (excess_return / std_dev) * math.sqrt(trades_per_year) if std_dev > 0 else 0
    sharpe = round(sharpe, 3)

    # Sortino ratio — only penalises downside volatility
    downside_returns  = [r for r in returns if r < daily_rf]
    downside_variance = sum((r - daily_rf) ** 2 for r in downside_returns) / max(len(downside_returns), 1)
    downside_std      = math.sqrt(downside_variance)
    sortino = (excess_return / downside_std) * math.sqrt(trades_per_year) if downside_std > 0 else 0
    sortino = round(sortino, 3)

    # Max drawdown on trade sequence
    cumulative = 0
    peak       = 0
    max_dd     = 0
    for r in returns:
        cumulative += r
        peak        = max(peak, cumulative)
        drawdown    = peak - cumulative
        max_dd      = max(max_dd, drawdown)
    max_dd = round(max_dd, 3)

    # Calmar ratio — annualised return / max drawdown
    annualised_return = avg_ret * trades_per_year
    calmar = round(annualised_return / max_dd, 3) if max_dd > 0 else 0

    # Win/loss statistics
    wins   = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate    = round(len(wins) / n * 100, 1)
    avg_win     = round(sum(wins) / len(wins), 3) if wins else 0
    avg_loss    = round(sum(losses) / len(losses), 3) if losses else 0
    profit_factor = round(abs(sum(wins) / sum(losses)), 3) if losses and sum(losses) != 0 else 0

    result = {
        "timestamp":        datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "trades_analysed":  n,
        "sharpe_ratio":     sharpe,
        "sortino_ratio":    sortino,
        "calmar_ratio":     calmar,
        "avg_return_per_trade": round(avg_ret, 3),
        "std_dev":          round(std_dev, 3),
        "max_drawdown_r":   max_dd,
        "win_rate":         win_rate,
        "avg_win_r":        avg_win,
        "avg_loss_r":       avg_loss,
        "profit_factor":    profit_factor,
        "annualised_return_r": round(annualised_return, 2),
        "risk_free_rate":   RISK_FREE_RATE,
        "rating":           rate_sharpe(sharpe)
    }

    atomic_write(SHARPE_FILE, result)

    return result

def rate_sharpe(sharpe):
    if sharpe >= 3.0:  return "EXCEPTIONAL"
    if sharpe >= 2.0:  return "WORLD CLASS"
    if sharpe >= 1.5:  return "EXCELLENT"
    if sharpe >= 1.0:  return "GOOD"
    if sharpe >= 0.5:  return "ACCEPTABLE"
    if sharpe >= 0.0:  return "POOR"
    return "NEGATIVE — losing strategy"

def display_sharpe(result):
    sharpe  = result['sharpe_ratio']
    rating  = result['rating']
    n       = result['trades_analysed']

    # Sharpe rating icon
    if sharpe >= 2.0:   icon = "🏆"
    elif sharpe >= 1.0: icon = "✅"
    elif sharpe >= 0.5: icon = "🟡"
    else:               icon = "🔴"

    print(f"\n{'='*50}")
    print(f"📊 APEX RISK-ADJUSTED PERFORMANCE")
    print(f"Based on {n} closed trades")
    print(f"{'='*50}")
    print(f"\n  {icon} Sharpe Ratio:    {sharpe} — {rating}")
    print(f"  📊 Sortino Ratio:   {result['sortino_ratio']}")
    print(f"  📊 Calmar Ratio:    {result['calmar_ratio']}")
    print(f"\n  Win Rate:          {result['win_rate']}%")
    print(f"  Avg Win:           {result['avg_win_r']}R")
    print(f"  Avg Loss:          {result['avg_loss_r']}R")
    print(f"  Profit Factor:     {result['profit_factor']}")
    print(f"\n  Avg Return/Trade:  {result['avg_return_per_trade']}R")
    print(f"  Annualised Return: {result['annualised_return_r']}R")
    print(f"  Max Drawdown:      {result['max_drawdown_r']}R")
    print(f"\n  Benchmark targets:")
    print(f"    Passive index:   Sharpe ~0.4-0.6")
    print(f"    Good active mgr: Sharpe ~1.0-1.5")
    print(f"    World class:     Sharpe ~2.0+")
    print(f"{'='*50}")

    # Confidence warning
    if n < 20:
        print(f"\n⚠️ LOW CONFIDENCE — only {n} trades")
        print(f"   Need 20+ trades for meaningful Sharpe")
        print(f"   Current figure is statistically unreliable")
    elif n < 50:
        print(f"\n🟡 MEDIUM CONFIDENCE — {n} trades")
        print(f"   50+ trades needed for high confidence")

if __name__ == '__main__':
    result = calculate_sharpe()
    if result:
        display_sharpe(result)
    else:
        print("\nSimulating with example data to show what Sharpe tracking will look like:\n")

        # Simulate 10 trades to demonstrate
        import random
        random.seed(42)
        sim_returns = [random.gauss(0.3, 1.2) for _ in range(10)]

        n        = len(sim_returns)
        avg_ret  = sum(sim_returns) / n
        variance = sum((r - avg_ret) ** 2 for r in sim_returns) / (n - 1)
        std_dev  = math.sqrt(variance)
        sharpe   = round((avg_ret / std_dev) * math.sqrt(100), 2) if std_dev > 0 else 0

        wins     = [r for r in sim_returns if r > 0]
        win_rate = round(len(wins) / n * 100, 1)

        print(f"  Example Sharpe: {sharpe} ({rate_sharpe(sharpe)})")
        print(f"  Example win rate: {win_rate}%")
        print(f"\n  Real calculation will begin after your first 3 closed trades.")
