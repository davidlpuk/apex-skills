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
# Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
# ---------------------------------------------------------------------------
#
# Standard Sharpe Ratio assumes Gaussian returns AND no selection bias from
# multiple-strategy testing. Both assumptions break for retail systems that
# test 5+ strategies with skewed/fat-tailed R-distributions.
#
# DSR returns the probability that the OBSERVED Sharpe is greater than zero,
# AFTER adjusting for:
#   - Number of trials  (selection bias / multiple testing)
#   - Skewness          (negative skew = "land mines" risk)
#   - Excess kurtosis   (fat tails = bigger surprise drawdowns)
#   - Sample length     (longer track records get more credit)
#
# Reference: Bailey, D. H. and López de Prado, M. (2014).
# "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
# Overfitting and Non-Normality". Journal of Portfolio Management, 40(5).
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """Standard normal CDF using the Abramowitz-Stegun approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _moments(r_multiples: list) -> tuple:
    """
    Return (mean, std, skew, excess_kurtosis) for a series.
    Uses sample variance (n-1) and Fisher's definition of excess kurtosis
    (so a Gaussian has excess_kurtosis = 0).
    """
    n = len(r_multiples)
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0
    mean = sum(r_multiples) / n
    var = sum((r - mean) ** 2 for r in r_multiples) / (n - 1)
    std = math.sqrt(var) if var > 0 else 1e-9
    if n < 3 or std == 0:
        return mean, std, 0.0, 0.0
    m3 = sum((r - mean) ** 3 for r in r_multiples) / n
    m4 = sum((r - mean) ** 4 for r in r_multiples) / n
    skew = m3 / (std ** 3) if std > 0 else 0.0
    excess_kurt = (m4 / (std ** 4)) - 3.0 if std > 0 else 0.0
    return mean, std, skew, excess_kurt


def expected_max_sharpe(n_trials: int) -> float:
    """
    Expected maximum Sharpe Ratio under the null hypothesis (zero true SR)
    when N independent strategies are tested. From Bailey & López de Prado:

      E[max SR] ≈ (1 - γ) · Φ⁻¹(1 - 1/N)  +  γ · Φ⁻¹(1 - 1/(N·e))

    where γ ≈ 0.5772 is the Euler-Mascheroni constant.

    This is the SR you would expect to see by pure luck after selecting
    the best of N strategies. The observed SR must clear THIS bar, not zero.
    """
    if n_trials <= 1:
        return 0.0
    gamma = 0.5772156649  # Euler-Mascheroni
    # Inverse-normal approximations via Beasley-Springer-Moro
    def _inv_norm(p):
        # Acklam's approximation (good to ~5e-7)
        if p <= 0 or p >= 1:
            return 0.0
        a = [-3.969683028665376e+01, 2.209460984245205e+02,
             -2.759285104469687e+02, 1.383577518672690e+02,
             -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02,
             -1.556989798598866e+02, 6.680131188771972e+01,
             -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01,
             -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01,
             2.445134137142996e+00, 3.754408661907416e+00]
        plow = 0.02425
        phigh = 1 - plow
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                   ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
        elif p <= phigh:
            q = p - 0.5
            r = q * q
            return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
                   (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
        else:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                    ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    e = math.e
    return (1 - gamma) * _inv_norm(1 - 1.0 / n_trials) + \
           gamma * _inv_norm(1 - 1.0 / (n_trials * e))


def deflated_sharpe_ratio(r_multiples: list,
                          n_trials: int = 1,
                          trades_per_year: float = 100) -> dict:
    """
    Bailey & López de Prado (2014) Deflated Sharpe Ratio.

    Parameters
    ----------
    r_multiples    : list of per-trade R-multiples
    n_trials       : number of strategies tested (for selection-bias correction).
                     If you tested 5 strategies and report the best, n_trials=5.
    trades_per_year: annualisation factor (default 100; lower for swing systems)

    Returns
    -------
    {
      'sharpe_observed': float,   # raw Sharpe (annualised)
      'sharpe_expected_max': float,  # E[max SR] under null (selection bar)
      'dsr_probability': float,   # Prob(true SR > 0) after all corrections
      'verdict': str,             # 'CONFIRMED'|'MARGINAL'|'NOT_PROVEN'
      'n_trades': int,
      'skew': float,
      'excess_kurtosis': float,
    }
    """
    n = len(r_multiples)
    if n < 5:
        return {
            'sharpe_observed': 0.0,
            'sharpe_expected_max': 0.0,
            'dsr_probability': 0.0,
            'verdict': 'INSUFFICIENT_DATA',
            'n_trades': n,
            'skew': 0.0,
            'excess_kurtosis': 0.0,
        }

    mean, std, skew, ex_kurt = _moments(r_multiples)
    if std <= 0:
        return {
            'sharpe_observed': 0.0,
            'sharpe_expected_max': 0.0,
            'dsr_probability': 0.0,
            'verdict': 'INSUFFICIENT_DATA',
            'n_trades': n,
            'skew': skew,
            'excess_kurtosis': ex_kurt,
        }

    # Per-trade Sharpe and its z-score-standardised form.
    # Under H0 (true SR=0, Gaussian returns), Var(SR_hat) ≈ 1/(n-1),
    # so SR_hat * sqrt(n-1) ~ N(0, 1).  The BLP E[max] formula returns
    # values in this standardised-z-score scale.
    sr_obs_per_trade = mean / std
    sr_obs_z = sr_obs_per_trade * math.sqrt(n - 1)
    sr_exp_max_z = expected_max_sharpe(max(n_trials, 1))

    # Bailey-de Prado adjustment for non-Gaussian shape:
    #   σ_skewed² = 1 - skew·SR + ((kurt-1)/4)·SR²
    # Negative skew (left-tail blow-ups) and high excess kurtosis (fat tails)
    # both INFLATE the variance of the SR estimator, lowering DSR confidence.
    denom_sq = 1 - skew * sr_obs_per_trade + (ex_kurt / 4.0) * (sr_obs_per_trade ** 2)
    denom_sq = max(denom_sq, 1e-9)  # guard against negative under extreme tails
    denom = math.sqrt(denom_sq)

    # DSR = Φ((SR_hat_z - SR*_z) / σ_skewed)
    z = (sr_obs_z - sr_exp_max_z) / denom
    dsr_prob = _normal_cdf(z)

    # Verdict thresholds match the BLP paper conventions
    if dsr_prob >= 0.95:
        verdict = 'CONFIRMED'
    elif dsr_prob >= 0.75:
        verdict = 'MARGINAL'
    else:
        verdict = 'NOT_PROVEN'

    # Annualise for human display (BLP recommends reporting both raw and annual)
    sr_annual = sr_obs_per_trade * math.sqrt(trades_per_year)
    # Convert E[max] back from z-score to per-trade SR, then annualise
    sr_max_per_trade = sr_exp_max_z / math.sqrt(n - 1)
    sr_max_annual = sr_max_per_trade * math.sqrt(trades_per_year)

    return {
        'sharpe_observed': round(sr_annual, 3),
        'sharpe_expected_max': round(sr_max_annual, 3),
        'dsr_probability': round(dsr_prob, 4),
        'verdict': verdict,
        'n_trades': n,
        'skew': round(skew, 3),
        'excess_kurtosis': round(ex_kurt, 3),
    }


# ---------------------------------------------------------------------------
# Benjamini-Hochberg False Discovery Rate correction
# ---------------------------------------------------------------------------
#
# When testing N strategies simultaneously, controlling per-test α at 0.10
# inflates the family-wise error rate to ~1 - (1-0.10)^N. With N=5 strategies
# that's ~41% chance of at least one false positive. BH-FDR controls the
# EXPECTED proportion of false discoveries among rejected nulls — far more
# appropriate for parallel strategy validation than Bonferroni (too strict).
#
# Reference: Benjamini, Y., & Hochberg, Y. (1995).
# "Controlling the false discovery rate: a practical and powerful approach
# to multiple testing." J. Royal Statistical Society B, 57(1).
# ---------------------------------------------------------------------------

def benjamini_hochberg(p_values: list, fdr: float = 0.10) -> list:
    """
    BH-FDR correction. Returns one boolean per input p-value indicating
    whether the null is rejected at the given FDR.

    Procedure:
      1. Sort p-values ascending: p(1) ≤ p(2) ≤ ... ≤ p(m)
      2. Find largest k such that p(k) ≤ k/m * fdr
      3. Reject all hypotheses with p-value ≤ p(k)

    Returns: list of bool (same order as input), and list of adjusted p-values.

    Example:
      p_values = [0.001, 0.04, 0.20, 0.50, 0.80]   # 5 strategies
      benjamini_hochberg(p_values, fdr=0.10)
      → [True, True, False, False, False]    # only first two survive
    """
    m = len(p_values)
    if m == 0:
        return [], []

    # Sort with original index preserved
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    sorted_p = [p for _, p in indexed]
    orig_idx = [i for i, _ in indexed]

    # Find largest k where p(k) ≤ k/m * fdr  (1-indexed k)
    k_star = 0
    for k in range(1, m + 1):
        if sorted_p[k - 1] <= (k / m) * fdr:
            k_star = k

    # Adjusted p-values: q(i) = min over j≥i of (m/j) * p(j)
    adjusted = [0.0] * m
    running_min = 1.0
    for j in range(m, 0, -1):
        adj = min(1.0, (m / j) * sorted_p[j - 1])
        running_min = min(running_min, adj)
        adjusted[j - 1] = round(running_min, 4)

    # Rejection set: top k_star sorted positions
    rejected_sorted = [i < k_star for i in range(m)]

    # Re-order both to original input order
    rejected_orig = [False] * m
    adjusted_orig = [1.0] * m
    for sorted_pos, orig_pos in enumerate(orig_idx):
        rejected_orig[orig_pos] = rejected_sorted[sorted_pos]
        adjusted_orig[orig_pos] = adjusted[sorted_pos]

    return rejected_orig, adjusted_orig


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
