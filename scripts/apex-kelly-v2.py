#!/usr/bin/env python3
"""
Apex Kelly v2 — Continuous Kelly Criterion + Distributional Modelling

Replaces binary Kelly (f* = (b·p - q)/b) with the continuous/log-optimal
Kelly formula (f* = μ/σ²) which accounts for the full return distribution.

Improvements over apex-thorp-test.py:
  1. Continuous Kelly from R-multiple distribution (not just win/loss binary)
  2. Negative-skew penalty  — reduces sizing for left-fat-tail distributions
  3. Excess-kurtosis penalty — reduces sizing for heavy tails
  4. Volatility-responsive scaling — scale down when VIX > baseline
  5. Same output interface as calculate_optimal_size() for drop-in use

Data sources (priority order):
  1. apex-backtest-results.json  — historical simulation data (more trades)
  2. apex-param-log.json         — live closed trades
  3. Hard-coded priors           — fallback when insufficient data
"""
import json
import math
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, log_info, get_portfolio_value
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m): print(f'INFO: {m}')
    def get_portfolio_value(): return None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KELLY_V2_FILE    = '/home/ubuntu/.picoclaw/logs/apex-kelly-v2.json'
OUTCOMES_FILE    = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
PARAM_FILE       = '/home/ubuntu/.picoclaw/logs/apex-param-log.json'
BACKTEST_FILE    = '/home/ubuntu/.picoclaw/logs/apex-backtest-results.json'
MARKET_DIR_FILE  = '/home/ubuntu/.picoclaw/logs/apex-market-direction.json'
POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
CORR_FILE        = '/home/ubuntu/.picoclaw/logs/apex-portfolio-correlation.json'

MIN_TRADES_CONTINUOUS = 10   # Fewer needed than binary Kelly (uses more info)
HALF_KELLY_FACTOR     = 0.5  # Professional standard
MAX_POSITION_PCT      = 0.08 # Hard cap — never exceed 8% portfolio
BASELINE_VIX          = 20.0 # Long-run average VIX

# Backtest priors — R-multiple distributions (μ, σ) from historical analysis
# μ = mean R, σ = std-dev R — conservative estimates
DISTRIBUTION_PRIORS = {
    'TREND':            {'mu': 0.22, 'sigma': 1.15, 'skew': -0.30, 'kurt_excess': 0.80},
    'CONTRARIAN':       {'mu': 0.34, 'sigma': 1.20, 'skew': -0.20, 'kurt_excess': 0.60},
    'INVERSE':          {'mu': 0.10, 'sigma': 1.40, 'skew': -0.50, 'kurt_excess': 1.20},
    'EARNINGS_DRIFT':   {'mu': 0.28, 'sigma': 1.25, 'skew': -0.15, 'kurt_excess': 0.70},
    'DIVIDEND_CAPTURE': {'mu': 0.25, 'sigma': 0.90, 'skew':  0.10, 'kurt_excess': 0.40},
}


# ---------------------------------------------------------------------------
# Distribution Statistics (no scipy required)
# ---------------------------------------------------------------------------
def _mean(values: list) -> float:
    return sum(values) / len(values)


def _variance(values: list, mean: float = None) -> float:
    """Sample variance (Bessel correction, n-1)."""
    if len(values) < 2:
        return 0.0
    mu = mean if mean is not None else _mean(values)
    return sum((x - mu) ** 2 for x in values) / (len(values) - 1)


def _skewness(values: list, mean: float = None, std: float = None) -> float:
    """
    Sample skewness (Fisher–Pearson standardised moment).
    Negative skew = left tail — dangerous for Kelly sizing.
    """
    n = len(values)
    if n < 3:
        return 0.0
    mu  = mean if mean is not None else _mean(values)
    sd  = std if std is not None else math.sqrt(_variance(values, mu))
    if sd == 0:
        return 0.0
    raw = sum(((x - mu) / sd) ** 3 for x in values) / n
    # Bias correction for sample skewness
    correction = math.sqrt(n * (n - 1)) / (n - 2) if n > 2 else 1.0
    return round(correction * raw, 4)


def _excess_kurtosis(values: list, mean: float = None, std: float = None) -> float:
    """
    Excess kurtosis (kurtosis - 3).
    Positive = fat tails — increases ruin probability vs Gaussian assumption.
    """
    n = len(values)
    if n < 4:
        return 0.0
    mu = mean if mean is not None else _mean(values)
    sd = std if std is not None else math.sqrt(_variance(values, mu))
    if sd == 0:
        return 0.0
    raw = sum(((x - mu) / sd) ** 4 for x in values) / n
    excess = raw - 3.0
    # Bias correction (Fisher)
    if n > 3:
        correction = (n + 1) * n / ((n - 1) * (n - 2) * (n - 3))
        excess = (n - 1) / (n - 2) / (n - 3) * ((n + 1) * raw - 3 * (n - 1))
    return round(excess, 4)


def compute_distribution_stats(r_multiples: list) -> dict:
    """
    Full distributional statistics for an R-multiple series.

    Returns:
        mu, sigma, variance, skewness, excess_kurtosis,
        percentiles (p5, p25, p50, p75, p95),
        n, source
    """
    n = len(r_multiples)
    if n == 0:
        return {}

    sorted_r = sorted(r_multiples)
    mu       = _mean(r_multiples)
    var      = _variance(r_multiples, mu)
    sigma    = math.sqrt(var) if var > 0 else 0.001
    skew     = _skewness(r_multiples, mu, sigma)
    kurt_ex  = _excess_kurtosis(r_multiples, mu, sigma)

    def pct(p):
        idx = max(0, min(n - 1, int(p * n)))
        return round(sorted_r[idx], 3)

    return {
        'n':              n,
        'mu':             round(mu, 4),
        'sigma':          round(sigma, 4),
        'variance':       round(var, 4),
        'skewness':       skew,
        'excess_kurtosis': kurt_ex,
        'p5':             pct(0.05),
        'p25':            pct(0.25),
        'p50':            pct(0.50),
        'p75':            pct(0.75),
        'p95':            pct(0.95),
        'min_r':          round(sorted_r[0], 3),
        'max_r':          round(sorted_r[-1], 3),
        'win_rate':       round(sum(1 for r in r_multiples if r > 0) / n, 4),
    }


# ---------------------------------------------------------------------------
# Continuous Kelly Formula
# ---------------------------------------------------------------------------
def continuous_kelly(mu: float, sigma: float) -> float:
    """
    Log-optimal (continuous) Kelly fraction.

    f* = μ / σ²

    Derivation: maximises E[log(1 + f·R)] where R ~ dist with mean μ, std σ.
    For Gaussian returns this is exact; for non-Gaussian it's the first-order
    approximation (corrections applied separately via skew/kurt factors).

    Automatically negative when μ < 0 (no edge → don't bet).
    """
    if sigma <= 0:
        return 0.0
    return mu / (sigma ** 2)


def skewness_penalty_factor(skewness: float) -> float:
    """
    Multiplicative penalty for negative skewness (left fat tail).

    Rationale: Negative skew means occasional large losses hurt more than
    Gaussian Kelly assumes. We reduce sizing linearly with negative skew,
    flooring at 0.5 (never reduce by more than 50% from skew alone).

    Factor = 1 + 0.5 × min(0, skewness)
    e.g. skew = -1.0 → factor = 0.5
         skew =  0.5 → factor = 1.0 (no penalty for positive skew)
    """
    factor = 1.0 + 0.5 * min(0.0, skewness)
    return round(max(0.50, factor), 4)


def kurtosis_penalty_factor(excess_kurtosis: float) -> float:
    """
    Multiplicative penalty for excess kurtosis (fat tails).

    Rationale: Heavy tails increase variance of outcomes, making Kelly's
    Gaussian approximation optimistic. We reduce sizing as tails get fatter.

    Factor = 1 / sqrt(1 + max(0, excess_kurtosis) × 0.10)
    e.g. excess_kurt = 3.0 → factor = 1/sqrt(1.30) ≈ 0.877
         excess_kurt = 0.0 → factor = 1.0
    """
    factor = 1.0 / math.sqrt(1.0 + max(0.0, excess_kurtosis) * 0.10)
    return round(max(0.60, factor), 4)


def volatility_factor(current_vix: float = None) -> float:
    """
    Scale Kelly by current volatility vs baseline.

    When VIX is elevated, trade outcomes have more variance than the
    distributional estimate, so we reduce sizing proportionally.

    Factor = min(1.0, BASELINE_VIX / current_vix)
    Floored at 0.5 (never halve again below VIX=40) and capped at 1.2.
    """
    if current_vix is None or current_vix <= 0:
        return 1.0
    factor = BASELINE_VIX / current_vix
    return round(max(0.50, min(1.20, factor)), 4)


def parameter_uncertainty_factor(n: int, target_n: int = 50) -> float:
    """
    Bayesian shrinkage factor that reduces Kelly sizing proportionally to
    how uncertain we are about the true μ estimate.

    Motivation: at n=2 closed trades the 95% CI on win rate spans ~[0.03, 0.97].
    Sizing at Kelly(prior_μ) as if that point estimate were certain is epistemically
    dishonest. We should size near-minimum until empirical data supports the prior.

    Formula: factor = max(MIN_FACTOR, n / target_n)
      n=0  → 0.10  (10% of Kelly — minimum bet)
      n=10 → 0.20
      n=25 → 0.50
      n=50 → 1.00  (full Kelly adjustments restored)
      n>50 → 1.00  (capped — more data doesn't unlock beyond 100%)

    MIN_FACTOR = 0.10: never size at zero on a prior-based signal — the floor
    ensures the system still takes calibration trades to build the empirical base.
    Only applies when using_prior=True; when empirical data is sufficient this
    factor is 1.0 (already accounted for via real distribution stats).
    """
    MIN_FACTOR = 0.10
    factor = n / target_n
    return round(max(MIN_FACTOR, min(1.0, factor)), 4)


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
# Portfolio Correlation Discount
# ---------------------------------------------------------------------------
def portfolio_correlation_factor(ticker: str, portfolio_value: float = None) -> float:
    """
    Markowitz-consistent reduction factor when new position correlates with
    existing portfolio holdings.

    Formula (Markowitz marginal variance):
        Each existing position i contributes a correlation drag proportional to:
          drag_i = max(0, ρ_i) × (notional_i / portfolio_value)
        Only positive correlations matter (negative = diversifying, no penalty).

        discount = 1 / (1 + sum(drag_i))

    Bounded: [0.40, 1.0]
      - 1.0 = no correlated positions or no data
      - 0.4  = maximum reduction (never cut by more than 60% on correlation alone)

    Data comes from pre-computed apex-portfolio-correlation.json.
    Correlation with NEW (not yet held) instruments defaults to sector-based proxy
    if not in the matrix, rather than making live API calls.

    Also handles the case where the new signal IS an existing position
    (adding to a position) — no additional penalty beyond what's already sized.
    """
    if portfolio_value is None or portfolio_value <= 0:
        portfolio_value = get_portfolio_value() or 5000

    ticker_upper = (ticker or '').upper()

    try:
        positions  = safe_read(POSITIONS_FILE, [])
        corr_data  = safe_read(CORR_FILE, {})

        if not positions:
            return 1.0  # No existing positions — no correlation concern

        # Build name → correlation lookup from pre-computed matrix
        # corr_data['pairs'] = [{'pair': 'A / B', 'correlation': 0.72}, ...]
        corr_lookup = {}
        for pair in corr_data.get('pairs', []):
            parts = [n.strip() for n in pair.get('pair', '').split(' / ')]
            if len(parts) != 2:
                continue
            a, b = parts
            c    = pair.get('correlation', 0)
            corr_lookup[(a, b)] = c
            corr_lookup[(b, a)] = c

        total_drag = 0.0
        for pos in positions:
            pos_name     = pos.get('name', pos.get('t212_ticker', ''))
            pos_notional = float(pos.get('value', pos.get('notional', 0)))

            if pos_notional <= 0:
                continue

            weight = pos_notional / portfolio_value

            # Look up correlation between new ticker and this position
            rho = corr_lookup.get((ticker_upper, pos_name.upper()),
                  corr_lookup.get((pos_name.upper(), ticker_upper),
                  None))

            if rho is None:
                # No pre-computed data — use sector-based proxy
                # Same instrument re-entry: assume high correlation
                if ticker_upper in pos_name.upper() or pos_name.upper() in ticker_upper:
                    rho = 0.95
                else:
                    # Conservative assumption: mild positive correlation for equities
                    rho = 0.30

            # Only penalise positive correlation (negative is diversifying)
            drag = max(0.0, float(rho)) * weight
            total_drag += drag

        if total_drag == 0:
            return 1.0

        # discount = 1 / (1 + total_drag)
        discount = 1.0 / (1.0 + total_drag)
        return round(max(0.40, min(1.0, discount)), 4)

    except Exception as e:
        log_warning(f"portfolio_correlation_factor failed (non-fatal): {e}")
        return 1.0  # Safe default — no discount on error


# ---------------------------------------------------------------------------
def get_current_vix() -> float:
    """Load current VIX from market direction file."""
    try:
        data = safe_read(MARKET_DIR_FILE, {})
        vix  = data.get('vix', data.get('vix_current', None))
        if vix and float(vix) > 0:
            return float(vix)
    except Exception:
        pass
    return BASELINE_VIX


def get_r_multiples(signal_type: str = None) -> tuple:
    """
    Load R-multiple series. Priority:
      1. apex-backtest-results.json (most data, simulation)
      2. apex-param-log.json (live closed trades)
    Returns (r_multiples, source_label).
    """
    r_multiples = []
    source      = 'prior'

    # ---- Try backtest results ----
    try:
        bt = safe_read(BACKTEST_FILE, {})
        trades = bt.get('trades', [])
        if signal_type:
            trades = [t for t in trades if t.get('signal_type') == signal_type]
        r_vals = [t['pnl_r'] for t in trades if 'pnl_r' in t]
        if len(r_vals) >= MIN_TRADES_CONTINUOUS:
            r_multiples = r_vals
            source      = f'backtest ({len(r_vals)} trades)'
    except Exception as e:
        log_warning(f"Kelly v2: backtest load failed: {e}")

    # ---- Try live param log ----
    if not r_multiples:
        try:
            log   = safe_read(PARAM_FILE, {'signals': []})
            sigs  = [s for s in log.get('signals', [])
                     if s.get('outcome') in ['WIN', 'LOSS']]
            if signal_type:
                sigs = [s for s in sigs if s.get('signal_type') == signal_type]
            r_vals = [s.get('r_achieved', 0) for s in sigs if 'r_achieved' in s]
            if len(r_vals) >= MIN_TRADES_CONTINUOUS:
                r_multiples = r_vals
                source      = f'live ({len(r_vals)} trades)'
        except Exception as e:
            log_warning(f"Kelly v2: live data load failed: {e}")

    return r_multiples, source


# ---------------------------------------------------------------------------
# Main Sizing Function
# ---------------------------------------------------------------------------
def calculate_optimal_size_v2(signal: dict, portfolio_value: float = None) -> dict:
    """
    Continuous Kelly position sizing with distributional adjustments.

    Compatible output interface with apex-thorp-test.calculate_optimal_size().
    Extra fields: dist_stats, kelly_continuous, adjustment_factors.
    """
    if portfolio_value is None:
        portfolio_value = get_portfolio_value() or 5000

    sig_type = signal.get('signal_type', 'TREND')
    entry    = float(signal.get('entry', 0))
    stop     = float(signal.get('stop', 0))
    target1  = float(signal.get('target1', 0))

    if entry <= 0 or stop <= 0:
        return None

    risk_per_share   = entry - stop
    reward_per_share = target1 - entry if target1 > entry else risk_per_share * 1.5
    if risk_per_share <= 0:
        return None

    r_ratio = round(reward_per_share / risk_per_share, 2)

    # --- Load R-multiple distribution ---
    r_multiples, data_source = get_r_multiples(sig_type)
    using_prior              = len(r_multiples) < MIN_TRADES_CONTINUOUS

    if using_prior:
        prior          = DISTRIBUTION_PRIORS.get(sig_type, DISTRIBUTION_PRIORS['TREND'])
        mu             = prior['mu']
        sigma          = prior['sigma']
        skew           = prior['skew']
        kurt_excess    = prior['kurt_excess']
        dist_stats     = {
            'n': 0, 'mu': mu, 'sigma': sigma,
            'skewness': skew, 'excess_kurtosis': kurt_excess,
        }
        data_source    = f'prior (need {MIN_TRADES_CONTINUOUS} trades)'
    else:
        dist_stats  = compute_distribution_stats(r_multiples)
        mu          = dist_stats['mu']
        sigma       = dist_stats['sigma']
        skew        = dist_stats['skewness']
        kurt_excess = dist_stats['excess_kurtosis']

    # --- Continuous Kelly ---
    f_star     = continuous_kelly(mu, sigma)

    # --- Distributional adjustments ---
    skew_f      = skewness_penalty_factor(skew)
    kurt_f      = kurtosis_penalty_factor(kurt_excess)
    current_vix = get_current_vix()
    vol_f       = volatility_factor(current_vix)
    port_f      = portfolio_correlation_factor(
        signal.get('name', signal.get('ticker', '')), portfolio_value
    )
    # Uncertainty factor: shrinks sizing when sample is too small to trust the prior.
    # Uses live sample_count if empirical data was loaded, otherwise 0 (full shrinkage).
    live_n  = dist_stats.get('n', 0) if not using_prior else 0
    uncert_f = parameter_uncertainty_factor(live_n) if using_prior else 1.0

    # Half-Kelly with all adjustments applied
    f_adjusted = (f_star * HALF_KELLY_FACTOR) * skew_f * kurt_f * vol_f * port_f * uncert_f
    f_adjusted = max(0.0, min(MAX_POSITION_PCT, f_adjusted))

    # --- Currency amounts ---
    kelly_full_pct  = round(f_star * 100, 2)
    kelly_half_pct  = round(f_star * HALF_KELLY_FACTOR * 100, 2)
    kelly_adj_pct   = round(f_adjusted * 100, 2)

    kelly_full_risk = round(portfolio_value * max(0, f_star), 2)
    kelly_half_risk = round(portfolio_value * max(0, f_star * HALF_KELLY_FACTOR), 2)
    recommended_risk = round(portfolio_value * f_adjusted, 2)
    max_risk         = round(portfolio_value * MAX_POSITION_PCT, 2)
    recommended_risk = min(recommended_risk, max_risk)
    recommended_risk = max(5.0, recommended_risk) if f_adjusted > 0 else 5.0

    shares   = round(recommended_risk / risk_per_share, 2) if risk_per_share > 0 else 0
    notional = round(shares * entry, 2)

    # --- Ruination check (10 consecutive losses) ---
    remaining   = portfolio_value
    ruin_after  = None
    for i in range(10):
        remaining -= recommended_risk
        if remaining <= 0:
            ruin_after = i + 1
            break
    dd_pct     = round((portfolio_value - max(0, remaining)) / portfolio_value * 100, 1)
    survival   = round(max(0, remaining) / portfolio_value * 100, 1)
    ruined     = ruin_after is not None or dd_pct > 20
    ruin_msg   = (f"RUIN after {ruin_after} losses" if ruin_after else
                  f"After 10 losses: -{dd_pct}% ({survival}% remaining)")

    # --- Verdict ---
    if mu < 0 and not using_prior:
        verdict        = 'ABORT'
        verdict_reason = f"Negative μ ({mu:.3f}R) — no distributional edge"
    elif ruined:
        verdict        = 'REDUCE'
        verdict_reason = f"Ruination risk: {ruin_msg}"
    elif using_prior and mu < 0.05:
        verdict        = 'REDUCE'
        verdict_reason = "Prior μ below minimum edge — reduce to minimum size"
    else:
        verdict        = 'APPROVED'
        uncert_note    = f" uncert×{uncert_f} (n={live_n}/50)" if using_prior else ""
        verdict_reason = (
            f"Continuous Kelly {kelly_adj_pct}% "
            f"[skew×{skew_f} kurt×{kurt_f} vol×{vol_f} port×{port_f}{uncert_note}]"
        )

    return {
        # Compatible with apex-thorp-test output
        'signal_type':        sig_type,
        'entry':              entry,
        'stop':               stop,
        'risk_per_share':     round(risk_per_share, 2),
        'r_ratio':            r_ratio,
        'stats_source':       data_source,
        'using_prior':        using_prior,
        'sample_count':       dist_stats.get('n', 0),
        'win_rate':           dist_stats.get('win_rate', 0.5),
        'kelly_full_pct':     kelly_full_pct,
        'kelly_half_pct':     kelly_half_pct,
        'kelly_full_risk':    kelly_full_risk,
        'kelly_half_risk':    kelly_half_risk,
        'recommended_risk':   recommended_risk,
        'recommended_shares': shares,
        'notional':           notional,
        'max_risk_cap':       max_risk,
        'ruination_check':    ruin_msg,
        'ruination_risk':     ruined,
        'drawdown_10loss':    dd_pct,
        'verdict':            verdict,
        'verdict_reason':     verdict_reason,
        # v2-specific fields
        'kelly_continuous':   round(f_star, 4),
        'kelly_adjusted_pct': kelly_adj_pct,
        'dist_stats':         dist_stats,
        'adjustment_factors': {
            'half_kelly':           HALF_KELLY_FACTOR,
            'skewness':             skew_f,
            'kurtosis':             kurt_f,
            'volatility':           vol_f,
            'portfolio':            port_f,
            'uncertainty':          uncert_f,
            'uncertainty_n':        live_n,
            'uncertainty_target_n': 50,
            'current_vix':          round(current_vix, 1),
        },
    }


# ---------------------------------------------------------------------------
# Batch Report
# ---------------------------------------------------------------------------
def run():
    """Generate Kelly v2 report for all signal types."""
    now = datetime.now(timezone.utc)
    print(f"\n=== KELLY v2 — CONTINUOUS KELLY + DISTRIBUTIONAL MODELLING ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    portfolio   = get_portfolio_value() or 5000
    current_vix = get_current_vix()
    results     = {}

    print(f"  Portfolio:   £{portfolio}")
    print(f"  Current VIX: {current_vix}  (baseline {BASELINE_VIX})")
    print(f"  Vol factor:  {volatility_factor(current_vix)}\n")
    print(f"  {'Signal Type':20} {'μ(R)':8} {'σ(R)':8} {'Skew':8} {'Kurt':8} "
          f"{'f*':8} {'Adj%':8} {'£Risk':8}")
    print(f"  {'-'*78}")

    for sig_type, prior in DISTRIBUTION_PRIORS.items():
        r_mults, src = get_r_multiples(sig_type)
        if len(r_mults) >= MIN_TRADES_CONTINUOUS:
            ds   = compute_distribution_stats(r_mults)
            mu   = ds['mu'];   sigma = ds['sigma']
            skew = ds['skewness'];  kurt = ds['excess_kurtosis']
        else:
            mu   = prior['mu'];   sigma = prior['sigma']
            skew = prior['skew']; kurt  = prior['kurt_excess']

        f_star = continuous_kelly(mu, sigma)
        sf     = skewness_penalty_factor(skew)
        kf     = kurtosis_penalty_factor(kurt)
        vf     = volatility_factor(current_vix)
        f_adj  = max(0.0, min(MAX_POSITION_PCT, f_star * HALF_KELLY_FACTOR * sf * kf * vf))
        risk   = round(min(portfolio * f_adj, portfolio * MAX_POSITION_PCT), 2)

        results[sig_type] = {
            'mu': mu, 'sigma': sigma, 'skewness': skew, 'excess_kurtosis': kurt,
            'kelly_continuous': round(f_star, 4),
            'kelly_adjusted_pct': round(f_adj * 100, 2),
            'risk_gbp': risk,
        }

        print(f"  {sig_type:20} {mu:6.3f}   {sigma:6.3f}   {skew:+6.2f}   {kurt:+6.2f}   "
              f"{round(f_star*100,1):6.1f}%  {round(f_adj*100,1):6.1f}%  £{risk}")

    output = {
        'timestamp':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'portfolio':     portfolio,
        'current_vix':   current_vix,
        'vol_factor':    volatility_factor(current_vix),
        'baseline_vix':  BASELINE_VIX,
        'half_kelly':    HALF_KELLY_FACTOR,
        'max_pct':       MAX_POSITION_PCT,
        'kelly_table':   results,
    }
    atomic_write(KELLY_V2_FILE, output)
    print(f"\n  Saved to {KELLY_V2_FILE}")
    return output


# ---------------------------------------------------------------------------
# Self-Test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys

    if '--test' in sys.argv:
        print("Kelly v2 — Self Tests")
        print("=" * 50)

        # 1. continuous_kelly
        f = continuous_kelly(0.30, 1.20)
        print(f"continuous_kelly(μ=0.30, σ=1.20) = {f:.4f}")
        assert abs(f - 0.30 / 1.44) < 0.001, f"Expected {0.30/1.44:.4f}, got {f:.4f}"
        print("  PASS")

        # 2. Negative mu → negative f*
        f_neg = continuous_kelly(-0.10, 1.20)
        print(f"continuous_kelly(μ=-0.10, σ=1.20) = {f_neg:.4f}")
        assert f_neg < 0
        print("  PASS")

        # 3. Parameter uncertainty factor
        assert parameter_uncertainty_factor(0)  == 0.10   # floor at n=0
        assert parameter_uncertainty_factor(25) == 0.50   # halfway
        assert parameter_uncertainty_factor(50) == 1.00   # full restored
        assert parameter_uncertainty_factor(99) == 1.00   # capped at 1.0
        assert parameter_uncertainty_factor(10) == 0.20
        print("parameter_uncertainty_factor: PASS")

        # 4. Skewness penalty
        assert skewness_penalty_factor(0.0)  == 1.0
        assert skewness_penalty_factor(-1.0) == 0.5
        assert skewness_penalty_factor(-2.0) == 0.5  # floored
        assert skewness_penalty_factor(0.5)  == 1.0  # no penalty for positive skew
        print("skewness_penalty_factor: PASS")

        # 4. Kurtosis penalty
        assert kurtosis_penalty_factor(0.0) == 1.0
        k3 = kurtosis_penalty_factor(3.0)
        assert 0.85 < k3 < 0.95, f"Expected ~0.88, got {k3}"
        print(f"kurtosis_penalty_factor(3.0) = {k3}: PASS")

        # 5. Volatility factor
        assert volatility_factor(20.0) == 1.0
        assert volatility_factor(40.0) == 0.5
        assert volatility_factor(10.0) == 1.2   # capped at 1.2
        assert volatility_factor(None) == 1.0
        print("volatility_factor: PASS")

        # 6. Distribution stats
        r_mults = [1.5, -1.0, 2.0, -1.0, 1.0, -1.0, 1.5, -1.0, 0.5, -1.0] * 5
        stats   = compute_distribution_stats(r_mults)
        assert stats['n']   == 50
        assert stats['mu']  > 0
        assert stats['sigma'] > 0
        assert stats['win_rate'] == 0.5   # 5 positives out of 10 per repeat
        print(f"compute_distribution_stats: n={stats['n']}, μ={stats['mu']:.3f}, "
              f"σ={stats['sigma']:.3f}, skew={stats['skewness']:.3f}: PASS")

        # 7. Full sizing calc
        test_sig = {
            'signal_type': 'CONTRARIAN',
            'entry': 100.0, 'stop': 96.0, 'target1': 108.0,
        }
        result = calculate_optimal_size_v2(test_sig, portfolio_value=5000)
        assert result is not None
        assert result['verdict'] in ('APPROVED', 'REDUCE', 'ABORT')
        assert 0 <= result['recommended_risk'] <= 5000 * MAX_POSITION_PCT
        assert 'dist_stats' in result
        assert 'adjustment_factors' in result
        print(f"calculate_optimal_size_v2: verdict={result['verdict']}, "
              f"risk=£{result['recommended_risk']}, kelly_adj={result['kelly_adjusted_pct']}%: PASS")

        print("\n" + "=" * 50)
        print("ALL TESTS PASSED")

    else:
        run()
