#!/usr/bin/env python3
"""
Benchmark Collection
Records VUAG price daily to enable comparison of Apex vs passive index.
After 3 months answers: does active management beat buy-and-hold?

Also tracks:
- Hypothetical passive portfolio (invest same capital in VUAG)
- Risk-adjusted comparison (Sharpe vs passive)
- Alpha generated vs benchmark
"""
import json
import yfinance as yf
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

BENCHMARK_FILE = '/home/ubuntu/.picoclaw/logs/apex-benchmark.json'
OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'

# Benchmark instruments
BENCHMARKS = {
    'VUAG':  'VUAG.L',    # S&P 500 — primary benchmark
    'VWRP':  'VWRP.L',    # Global All-Cap — secondary
    'SPY':   'SPY',        # US proxy
}

# Starting capital — matches Apex portfolio
STARTING_CAPITAL = 5000.0

def get_benchmark_prices():
    """Fetch current prices for all benchmarks."""
    prices = {}
    for name, yahoo in BENCHMARKS.items():
        try:
            hist  = yf.Ticker(yahoo).history(period="2d")
            if hist.empty:
                continue
            price = float(hist['Close'].iloc[-1])
            if yahoo.endswith('.L') and price > 100:
                price /= 100
            prices[name] = round(price, 4)
        except Exception as e:
            log_error(f"Benchmark price fetch failed for {name}: {e}")
    return prices

def record_daily_benchmark():
    """Record today's benchmark prices."""
    now    = datetime.now(timezone.utc)
    today  = now.strftime('%Y-%m-%d')
    prices = get_benchmark_prices()

    if not prices:
        log_error("No benchmark prices fetched")
        return None

    benchmark = safe_read(BENCHMARK_FILE, {
        'start_date':     today,
        'start_prices':   prices,
        'starting_capital': STARTING_CAPITAL,
        'history':        [],
        'passive_units':  {},
    })

    # Calculate passive portfolio units on first run
    if not benchmark.get('passive_units'):
        passive_units = {}
        for name, price in prices.items():
            if price > 0:
                passive_units[name] = round(STARTING_CAPITAL / price, 4)
        benchmark['passive_units'] = passive_units
        benchmark['start_prices']  = prices
        benchmark['start_date']    = today
        print(f"  Benchmark initialised — bought {passive_units.get('VUAG',0):.2f} VUAG units")

    # Calculate passive portfolio current value
    passive_units   = benchmark.get('passive_units', {})
    passive_values  = {}
    for name, units in passive_units.items():
        price = prices.get(name, 0)
        passive_values[name] = round(units * price, 2)

    vuag_value   = passive_values.get('VUAG', STARTING_CAPITAL)
    vuag_return  = round((vuag_value - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2)

    # Get Apex portfolio value
    try:
        import subprocess
        env = {}
        with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()

        auth     = env.get('T212_AUTH','')
        endpoint = env.get('T212_ENDPOINT','')
        result   = subprocess.run([
            'curl','-s','--max-time','10',
            '-H',f'Authorization: Basic {auth}',
            f'{endpoint}/equity/account/cash'
        ], capture_output=True, text=True)
        cash         = json.loads(result.stdout)
        apex_value   = round(float(cash.get('total', STARTING_CAPITAL)), 2)
        apex_return  = round((apex_value - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2)
    except Exception as e:
        log_error(f"Apex value fetch failed: {e}")
        apex_value  = STARTING_CAPITAL
        apex_return = 0.0

    # Alpha = Apex return - Benchmark return
    alpha = round(apex_return - vuag_return, 2)

    # Record daily snapshot
    snapshot = {
        'date':          today,
        'prices':        prices,
        'passive_values':passive_values,
        'vuag_value':    vuag_value,
        'vuag_return':   vuag_return,
        'apex_value':    apex_value,
        'apex_return':   apex_return,
        'alpha':         alpha,
        'beating':       apex_return > vuag_return,
    }

    # Append to history (avoid duplicates)
    history = benchmark.get('history', [])
    history = [h for h in history if h.get('date') != today]
    history.append(snapshot)
    benchmark['history'] = history[-252:]  # Keep 1 year

    # Latest summary
    benchmark['latest']  = snapshot
    benchmark['last_updated'] = now.strftime('%Y-%m-%d %H:%M UTC')

    atomic_write(BENCHMARK_FILE, benchmark)
    return snapshot

def _daily_returns(history, key):
    """Compute daily % returns from a list of history snapshots for a given key."""
    import math
    values = [h.get(key, STARTING_CAPITAL) for h in history]
    returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            r = (values[i] - values[i - 1]) / values[i - 1] * 100
            returns.append(r)
    return returns


def _sharpe(daily_returns, risk_free_daily=0.0):
    """Annualised Sharpe ratio from daily % returns."""
    import math
    if len(daily_returns) < 5:
        return None
    n    = len(daily_returns)
    mean = sum(daily_returns) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / (n - 1) if n > 1 else 0
    std  = math.sqrt(variance)
    if std == 0:
        return None
    return round((mean - risk_free_daily) / std * math.sqrt(252), 2)


def _sortino(daily_returns, risk_free_daily=0.0):
    """Annualised Sortino ratio (downside deviation only)."""
    import math
    if len(daily_returns) < 5:
        return None
    downside = [r for r in daily_returns if r < risk_free_daily]
    if not downside:
        # No down days at all — effectively infinite Sortino; cap to avoid absurd numbers
        mean = sum(daily_returns) / len(daily_returns)
        return round(99.0 if mean > 0 else 0.0, 2)
    n        = len(daily_returns)
    mean     = sum(daily_returns) / n
    dd_var   = sum(r ** 2 for r in downside) / n
    dd_std   = math.sqrt(dd_var)
    if dd_std == 0:
        return None
    return round((mean - risk_free_daily) / dd_std * math.sqrt(252), 2)


def _max_drawdown(values):
    """Max peak-to-trough drawdown from a list of portfolio values."""
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def _calmar(annualised_return_pct, max_dd_pct):
    """Calmar ratio = annualised return / max drawdown. Higher is better."""
    if max_dd_pct == 0:
        # No drawdown — cap to avoid division by zero
        return round(99.0 if annualised_return_pct > 0 else 0.0, 2)
    return round(annualised_return_pct / max_dd_pct, 2)


def _information_ratio(apex_returns, bench_returns):
    """Information Ratio = mean(active return) / std(active return), annualised."""
    import math
    if len(apex_returns) != len(bench_returns) or len(apex_returns) < 5:
        return None
    active = [a - b for a, b in zip(apex_returns, bench_returns)]
    n    = len(active)
    mean = sum(active) / n
    variance = sum((r - mean) ** 2 for r in active) / (n - 1) if n > 1 else 0
    std  = math.sqrt(variance)
    if std == 0:
        return None
    return round(mean / std * math.sqrt(252), 2)


def calculate_risk_metrics(history):
    """
    Calculate professional risk-adjusted performance metrics from history.
    Returns a dict of metrics to be stored alongside the daily snapshot.
    """
    if len(history) < 5:
        return {}

    apex_vals  = [h.get('apex_value', STARTING_CAPITAL) for h in history]
    vuag_vals  = [h.get('vuag_value', STARTING_CAPITAL) for h in history]
    days       = len(history)

    # Daily returns
    apex_rets  = _daily_returns(history, 'apex_value')
    vuag_rets  = _daily_returns(history, 'vuag_value')

    # Return (annualised)
    total_apex_ret  = (apex_vals[-1] - STARTING_CAPITAL) / STARTING_CAPITAL * 100
    total_vuag_ret  = (vuag_vals[-1] - STARTING_CAPITAL) / STARTING_CAPITAL * 100
    ann_factor      = 252 / days
    ann_apex_ret    = round(total_apex_ret * ann_factor, 2)
    ann_vuag_ret    = round(total_vuag_ret * ann_factor, 2)

    # Max drawdowns
    apex_mdd   = _max_drawdown(apex_vals)
    vuag_mdd   = _max_drawdown(vuag_vals)

    metrics = {
        'days':                  days,
        'apex_total_return':     round(total_apex_ret, 2),
        'vuag_total_return':     round(total_vuag_ret, 2),
        'apex_annualised_return':ann_apex_ret,
        'vuag_annualised_return':ann_vuag_ret,
        'apex_max_drawdown':     apex_mdd,
        'vuag_max_drawdown':     vuag_mdd,
        'apex_sharpe':           _sharpe(apex_rets),
        'vuag_sharpe':           _sharpe(vuag_rets),
        'apex_sortino':          _sortino(apex_rets),
        'vuag_sortino':          _sortino(vuag_rets),
        'apex_calmar':           _calmar(ann_apex_ret, apex_mdd),
        'vuag_calmar':           _calmar(ann_vuag_ret, vuag_mdd),
        'information_ratio':     _information_ratio(apex_rets, vuag_rets),
    }
    return metrics


def generate_report():
    """
    Generate benchmark comparison report.
    Meaningful after 60+ days of data.
    """
    benchmark = safe_read(BENCHMARK_FILE, {})
    history   = benchmark.get('history', [])

    if len(history) < 5:
        print(f"\n  ⏳ Need 60+ days for meaningful comparison")
        print(f"  Currently: {len(history)} days recorded")
        return

    latest     = benchmark.get('latest', {})
    start_date = benchmark.get('start_date', '')
    days       = len(history)

    print(f"\n=== BENCHMARK COMPARISON ===")
    print(f"  Period: {start_date} → {latest.get('date','')} ({days} days)")
    print(f"  Starting capital: £{STARTING_CAPITAL:,.2f}")
    print(f"")
    print(f"  {'':25} {'Value':10} {'Return':10} {'vs VUAG'}")
    print(f"  {'-'*55}")
    print(f"  {'Apex (Active)':25} £{latest.get('apex_value',0):8.2f}  "
          f"{latest.get('apex_return',0):+.2f}%")
    print(f"  {'VUAG (Passive S&P500)':25} £{latest.get('vuag_value',0):8.2f}  "
          f"{latest.get('vuag_return',0):+.2f}%")
    print(f"")

    alpha = latest.get('alpha', 0)
    icon  = "✅" if alpha > 0 else "🔴"
    print(f"  {icon} Alpha: {alpha:+.2f}% ({'Apex beating passive' if alpha > 0 else 'Passive beating Apex'})")

    # Win rate vs benchmark (days Apex > VUAG)
    beating_days = sum(1 for h in history if h.get('beating', False))
    beat_pct     = round(beating_days / len(history) * 100, 1)
    print(f"  Days beating benchmark: {beating_days}/{days} ({beat_pct}%)")

    # Risk-adjusted metrics
    metrics = calculate_risk_metrics(history)
    if metrics:
        print(f"\n  {'':25} {'Apex':>10} {'VUAG':>10}")
        print(f"  {'-'*47}")

        def _fmt(v):
            return f"{v:>10.2f}" if v is not None else "       N/A"

        print(f"  {'Max Drawdown':25}{_fmt(metrics.get('apex_max_drawdown'))}"
              f"{_fmt(metrics.get('vuag_max_drawdown'))}")
        print(f"  {'Sharpe (ann.)':25}{_fmt(metrics.get('apex_sharpe'))}"
              f"{_fmt(metrics.get('vuag_sharpe'))}")
        print(f"  {'Sortino (ann.)':25}{_fmt(metrics.get('apex_sortino'))}"
              f"{_fmt(metrics.get('vuag_sortino'))}")
        print(f"  {'Calmar':25}{_fmt(metrics.get('apex_calmar'))}"
              f"{_fmt(metrics.get('vuag_calmar'))}")
        ir = metrics.get('information_ratio')
        print(f"  {'Information Ratio':25}{_fmt(ir)}")

        # Store metrics in benchmark file
        benchmark['risk_metrics'] = metrics
        from apex_utils import atomic_write as _aw
        _aw(BENCHMARK_FILE, benchmark)

    if days >= 60:
        verdict = "✅ APEX ADDING VALUE" if alpha > 1.0 else (
                  "🟡 MARGINAL EDGE" if alpha > 0 else
                  "🔴 PASSIVE IS WINNING")
        print(f"\n  VERDICT: {verdict}")

def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== BENCHMARK COLLECTION ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")

    snapshot = record_daily_benchmark()

    if snapshot:
        print(f"  VUAG:  £{snapshot['vuag_value']:.2f} ({snapshot['vuag_return']:+.2f}%)")
        print(f"  Apex:  £{snapshot['apex_value']:.2f} ({snapshot['apex_return']:+.2f}%)")
        print(f"  Alpha: {snapshot['alpha']:+.2f}%")
        print(f"  {'✅ Beating benchmark' if snapshot['beating'] else '🔴 Benchmark winning'}")

    generate_report()
    print(f"\n✅ Benchmark recorded")

if __name__ == '__main__':
    run()
