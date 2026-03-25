#!/usr/bin/env python3
"""
Apex Backtest Statistics Library
Pure statistical functions for backtesting validation.

Wilson score intervals, bootstrap CI, permutation tests, binomial significance.
No side effects, no file I/O — just math.
"""
import math
import random
from typing import Callable, Optional

# Try scipy for exact binomial test; fall back to approximation
try:
    from scipy.stats import binomtest as _binomtest
    _HAS_SCIPY = True
except ImportError:
    try:
        from scipy.stats import binom_test as _binom_test_legacy
        _HAS_SCIPY = True
        def _binomtest(k, n, p, alternative='greater'):
            """Wrapper for older scipy that only has binom_test."""
            class _Result:
                def __init__(self, pv):
                    self.pvalue = pv
            return _Result(_binom_test_legacy(k, n, p, alternative=alternative))
    except ImportError:
        _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# Z-values for common confidence levels (avoids scipy dependency)
# ---------------------------------------------------------------------------
_Z_TABLE = {
    0.90: 1.6449,
    0.95: 1.9600,
    0.99: 2.5758,
}


def _z_for_confidence(confidence: float) -> float:
    """Return z-score for a given two-sided confidence level."""
    if confidence in _Z_TABLE:
        return _Z_TABLE[confidence]
    # Rational approximation (Abramowitz & Stegun 26.2.23) for tail probability
    p = (1 - confidence) / 2
    t = math.sqrt(-2 * math.log(p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)


# ---------------------------------------------------------------------------
# Wilson Score Interval — superior to Wald for small n
# ---------------------------------------------------------------------------
def binomial_ci(wins: int, n: int, confidence: float = 0.95) -> tuple:
    """
    Wilson score interval for a binomial proportion.

    Better than Wald (normal approx) for small samples (n < 100) because it
    never produces impossible intervals (< 0 or > 1).

    Formula:
        p_hat = wins / n
        z = z_{1-alpha/2}
        denom = 1 + z^2/n
        centre = (p_hat + z^2/(2n)) / denom
        margin = z * sqrt(p_hat*(1-p_hat)/n + z^2/(4n^2)) / denom

    Returns (lower, upper) as fractions in [0, 1].
    """
    if n == 0:
        return (0.0, 1.0)

    z = _z_for_confidence(confidence)
    p_hat = wins / n
    z2 = z * z

    denom = 1 + z2 / n
    centre = (p_hat + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n)) / denom

    lo = max(0.0, centre - margin)
    hi = min(1.0, centre + margin)
    return (round(lo, 4), round(hi, 4))


def binomial_ci_pct(wins: int, n: int, confidence: float = 0.95) -> tuple:
    """Wilson CI as percentages (0–100)."""
    lo, hi = binomial_ci(wins, n, confidence)
    return (round(lo * 100, 1), round(hi * 100, 1))


# ---------------------------------------------------------------------------
# Bootstrap Confidence Interval
# ---------------------------------------------------------------------------
def bootstrap_ci(
    values: list,
    stat_fn: Optional[Callable] = None,
    n_boot: int = 5000,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple:
    """
    Percentile bootstrap confidence interval for any statistic.

    Default stat_fn is mean. For profit factor:
        stat_fn = lambda x: abs(sum(v for v in x if v > 0) / (sum(v for v in x if v < 0) or -1))

    Returns (point_estimate, lower, upper).
    """
    if not values:
        return (0.0, 0.0, 0.0)

    if stat_fn is None:
        stat_fn = lambda x: sum(x) / len(x)

    rng = random.Random(seed)
    n = len(values)
    point = stat_fn(values)

    boot_stats = []
    for _ in range(n_boot):
        sample = [values[rng.randint(0, n - 1)] for _ in range(n)]
        try:
            boot_stats.append(stat_fn(sample))
        except (ZeroDivisionError, ValueError):
            continue

    if not boot_stats:
        return (round(point, 4), round(point, 4), round(point, 4))

    boot_stats.sort()
    alpha = (1 - confidence) / 2
    lo_idx = max(0, int(alpha * len(boot_stats)))
    hi_idx = min(len(boot_stats) - 1, int((1 - alpha) * len(boot_stats)))

    return (
        round(point, 4),
        round(boot_stats[lo_idx], 4),
        round(boot_stats[hi_idx], 4),
    )


# ---------------------------------------------------------------------------
# Permutation Test — is win rate significantly above baseline?
# ---------------------------------------------------------------------------
def permutation_test(
    outcomes: list,
    baseline: float = 0.5,
    n_perms: int = 10000,
    seed: int = 42,
) -> float:
    """
    Permutation test: is observed win rate significantly above baseline?

    outcomes: list of bool (True = win, False = loss)
    Returns one-sided p-value.

    For n_trades < 500 this is more appropriate than a z-test because
    it makes no distributional assumptions.
    """
    if not outcomes:
        return 1.0

    n = len(outcomes)
    observed_wins = sum(outcomes)
    observed_wr = observed_wins / n

    if observed_wr <= baseline:
        return 1.0

    rng = random.Random(seed)
    count_extreme = 0

    for _ in range(n_perms):
        # Generate random outcomes with probability = baseline
        sim_wins = sum(1 for _ in range(n) if rng.random() < baseline)
        if sim_wins >= observed_wins:
            count_extreme += 1

    return round(count_extreme / n_perms, 4)


# ---------------------------------------------------------------------------
# Exact Binomial Test (per-instrument significance)
# ---------------------------------------------------------------------------
def instrument_significance(
    wins: int,
    n: int,
    baseline_wr: float = 0.50,
    confidence: float = 0.95,
    significance_level: float = 0.10,
) -> dict:
    """
    Per-instrument significance test.

    Uses exact binomial test when scipy is available, falls back to
    permutation approximation.

    Returns:
        {
            'wins': int, 'n': int, 'win_rate': float,
            'ci': (lo, hi),  # Wilson CI as percentages
            'p_value': float,
            'significant': bool,  # p <= significance_level
            'verdict': 'INCLUDE' | 'MARGINAL' | 'EXCLUDE' | 'INSUFFICIENT'
        }
    """
    if n < 5:
        lo, hi = binomial_ci_pct(wins, n, confidence)
        return {
            'wins': wins, 'n': n,
            'win_rate': round(wins / n * 100, 1) if n > 0 else 0,
            'ci': (lo, hi),
            'p_value': 1.0,
            'significant': False,
            'verdict': 'INSUFFICIENT',
        }

    # p-value
    if _HAS_SCIPY:
        result = _binomtest(wins, n, baseline_wr, alternative='greater')
        p_value = round(result.pvalue, 4)
    else:
        # Fall back to permutation
        outcomes = [True] * wins + [False] * (n - wins)
        p_value = permutation_test(outcomes, baseline_wr, n_perms=10000)

    ci = binomial_ci_pct(wins, n, confidence)

    if p_value <= significance_level:
        verdict = 'INCLUDE'
    elif p_value <= 0.20:
        verdict = 'MARGINAL'
    else:
        verdict = 'EXCLUDE'

    return {
        'wins': wins, 'n': n,
        'win_rate': round(wins / n * 100, 1),
        'ci': ci,
        'p_value': p_value,
        'significant': p_value <= significance_level,
        'verdict': verdict,
    }


# ---------------------------------------------------------------------------
# Sharpe Ratio from R-multiples
# ---------------------------------------------------------------------------
def sharpe_from_r_multiples(r_multiples: list, trades_per_year: float = 100) -> float:
    """
    Annualised Sharpe ratio from R-multiple trade series.

    sharpe = mean(r) / std(r) * sqrt(trades_per_year)

    Assumes risk-free rate contribution is negligible at the per-trade level
    (standard for short-horizon systematic strategies).
    """
    if len(r_multiples) < 2:
        return 0.0

    n = len(r_multiples)
    mean_r = sum(r_multiples) / n
    var_r = sum((r - mean_r) ** 2 for r in r_multiples) / (n - 1)  # Sample variance
    std_r = math.sqrt(var_r) if var_r > 0 else 0.001

    return round(mean_r / std_r * math.sqrt(trades_per_year), 2)


# ---------------------------------------------------------------------------
# Aggregate analysis with CIs
# ---------------------------------------------------------------------------
def analyse_with_confidence(trades: list, confidence: float = 0.95) -> dict:
    """
    Enhanced version of apex-backtest.py:analyse_results() with CIs.

    trades: list of dicts with 'outcome' ('WIN'/'LOSS') and 'pnl_r' (float).
    Returns stats dict with confidence intervals on all key metrics.
    """
    if not trades:
        return {}

    total = len(trades)
    wins = [t for t in trades if t.get('outcome') == 'WIN']
    losses = [t for t in trades if t.get('outcome') == 'LOSS']
    n_wins = len(wins)

    win_rate = round(n_wins / total * 100, 1)
    wr_ci = binomial_ci_pct(n_wins, total, confidence)

    r_multiples = [t.get('pnl_r', 0) for t in trades]
    avg_win_r = round(sum(t['pnl_r'] for t in wins) / n_wins, 2) if wins else 0
    avg_loss_r = round(sum(t['pnl_r'] for t in losses) / len(losses), 2) if losses else 0

    # Expectancy with bootstrap CI
    expectancy_point, exp_lo, exp_hi = bootstrap_ci(
        r_multiples, stat_fn=lambda x: sum(x) / len(x),
        n_boot=5000, confidence=confidence
    )

    # Profit factor
    sum_wins = sum(t['pnl_r'] for t in wins) if wins else 0
    sum_losses = abs(sum(t['pnl_r'] for t in losses)) if losses else 0.001
    profit_factor = round(sum_wins / sum_losses, 2) if sum_losses > 0 else 0

    # Sharpe
    sharpe = sharpe_from_r_multiples(r_multiples)

    # Significance: is this win rate significantly above 50%?
    outcomes = [t.get('outcome') == 'WIN' for t in trades]
    p_value = permutation_test(outcomes, baseline=0.50, n_perms=10000)

    return {
        'n_trades': total,
        'win_rate': win_rate,
        'win_rate_ci': list(wr_ci),
        'avg_win_r': avg_win_r,
        'avg_loss_r': avg_loss_r,
        'expectancy': round(expectancy_point, 3),
        'expectancy_ci': [round(exp_lo, 3), round(exp_hi, 3)],
        'profit_factor': profit_factor,
        'sharpe': sharpe,
        'p_value_vs_random': p_value,
        'significant': p_value <= 0.05,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    print("Apex Backtest Statistics — Self Test")
    print("=" * 50)

    # Wilson CI
    lo, hi = binomial_ci(50, 100)
    print(f"Wilson CI (50/100, 95%): [{lo:.3f}, {hi:.3f}]")
    assert 0.39 < lo < 0.42, f"Expected ~0.40, got {lo}"
    assert 0.58 < hi < 0.61, f"Expected ~0.60, got {hi}"
    print("  PASS")

    # Small sample
    lo, hi = binomial_ci(3, 5)
    print(f"Wilson CI (3/5, 95%):   [{lo:.3f}, {hi:.3f}]")
    assert lo > 0.15 and hi < 0.95
    print("  PASS")

    # Bootstrap CI for mean
    data = [1.5, -1.0, 2.0, -1.0, 1.0, -1.0, 1.5, -1.0, 0.5, -1.0]
    pt, blo, bhi = bootstrap_ci(data)
    print(f"Bootstrap CI (mean of R): {pt:.3f} [{blo:.3f}, {bhi:.3f}]")
    assert blo < pt < bhi
    print("  PASS")

    # Permutation test — 60% win rate on 100 trades
    outcomes = [True] * 60 + [False] * 40
    p = permutation_test(outcomes, baseline=0.50)
    print(f"Permutation test (60/100 vs 50%): p = {p}")
    assert p < 0.05, f"Expected p < 0.05 for 60% on 100 trades, got {p}"
    print("  PASS")

    # Permutation test — 52% on 50 trades (should NOT be significant)
    outcomes2 = [True] * 26 + [False] * 24
    p2 = permutation_test(outcomes2, baseline=0.50)
    print(f"Permutation test (26/50 vs 50%): p = {p2}")
    assert p2 > 0.10, f"Expected p > 0.10 for 52% on 50 trades, got {p2}"
    print("  PASS")

    # Instrument significance
    sig = instrument_significance(8, 12)
    print(f"Significance (8/12): p={sig['p_value']}, verdict={sig['verdict']}")
    print("  PASS")

    # Sharpe
    r_mult = [1.5, -1.0, 2.0, -1.0, 1.0, -1.0, 1.5, -1.0, 0.5, -1.0] * 5
    s = sharpe_from_r_multiples(r_mult)
    print(f"Sharpe (50 trades): {s}")
    assert s != 0
    print("  PASS")

    # Full analysis
    test_trades = [
        {'outcome': 'WIN', 'pnl_r': 1.5},
        {'outcome': 'LOSS', 'pnl_r': -1.0},
        {'outcome': 'WIN', 'pnl_r': 2.0},
        {'outcome': 'LOSS', 'pnl_r': -1.0},
        {'outcome': 'WIN', 'pnl_r': 1.0},
    ] * 10
    analysis = analyse_with_confidence(test_trades)
    print(f"\nFull analysis (50 trades):")
    for k, v in analysis.items():
        print(f"  {k}: {v}")
    assert analysis['significant'] or analysis['p_value_vs_random'] <= 0.10
    print("  PASS")

    print("\n" + "=" * 50)
    print("ALL TESTS PASSED")
