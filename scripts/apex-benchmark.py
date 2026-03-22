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
        endpoint = env.get('T212_ENDPOINT','https://demo.trading212.com/api/v0')
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
