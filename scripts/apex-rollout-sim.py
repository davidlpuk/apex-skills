#!/usr/bin/env python3
"""
Apex Rollout Simulator — Monte Carlo Trade Path Simulation

Before entering a trade, simulates 1000 price paths using recent volatility
and tests whether the stop/target structure survives realistic noise.

Inputs:  signal dict (entry, stop, target1, target2, signal_type)
         optional intel dict (vix)
Outputs: apex-rollout-results.json

Usage (CLI):
  python3 apex-rollout-sim.py TICKER SIGNAL_TYPE ENTRY STOP T1 T2 [VIX]
  python3 apex-rollout-sim.py NVDA TREND 145.20 139.50 151.40 156.10 22
"""

import json
import os
import sys
import math
import random
import statistics
from datetime import datetime, timezone

LOGS = '/home/ubuntu/.picoclaw/logs'
OUTPUT_FILE = os.path.join(LOGS, 'apex-rollout-results.json')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def atomic_write(path, data):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        sys.stderr.write(f"[rollout-sim] atomic_write failed: {e}\n")
        return False


def _now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def _fetch_recent_closes(ticker, days=20):
    """
    Fetch recent closing prices for ticker.
    Returns list of floats (oldest first) or None on failure.
    """
    try:
        import yfinance as yf  # optional dependency
        data = yf.download(ticker, period='1mo', progress=False, auto_adjust=True)
        if data is None or len(data) == 0:
            return None
        closes = data['Close'].dropna().tolist()
        if len(closes) < 5:
            return None
        # Flatten in case yfinance returns a DataFrame column with multi-index
        flat = []
        for c in closes:
            try:
                flat.append(float(c))
            except (TypeError, ValueError):
                pass
        return flat if len(flat) >= 5 else None
    except Exception:
        return None


def _estimate_params_from_closes(closes, vix=None):
    """
    Estimate annualised drift (mu) and volatility (sigma) from close prices.
    Optionally inflate sigma for high VIX regimes.
    Returns (mu, sigma, vix_inflation).
    """
    if closes is None or len(closes) < 2:
        return 0.0, 0.25, 1.0

    try:
        log_returns = []
        for i in range(1, len(closes)):
            p0, p1 = closes[i - 1], closes[i]
            if p0 > 0 and p1 > 0:
                log_returns.append(math.log(p1 / p0))

        if len(log_returns) < 2:
            return 0.0, 0.25, 1.0

        mu_daily = statistics.mean(log_returns)
        sigma_daily = statistics.stdev(log_returns)

        mu = mu_daily * 252
        sigma = sigma_daily * math.sqrt(252)

        # Clamp to sane bounds
        mu = max(-2.0, min(2.0, mu))
        sigma = max(0.05, min(3.0, sigma))

        # VIX inflation
        vix_inflation = 1.0
        if vix is not None and vix > 25:
            vix_inflation = vix / 20.0
            sigma *= vix_inflation
            sigma = max(0.05, min(3.0, sigma))

        return mu, sigma, vix_inflation

    except Exception:
        return 0.0, 0.25, 1.0


def _estimate_params_from_atr(entry, atr, vix=None):
    """
    Fallback: estimate sigma from ATR when no price history available.
    ATR is typically a 14-day average true range.
    sigma_daily = atr / entry / sqrt(14)
    """
    if entry <= 0 or atr is None or atr <= 0:
        return 0.0, 0.25, 1.0
    try:
        sigma_daily = atr / entry / math.sqrt(14)
        sigma = sigma_daily * math.sqrt(252)
        sigma = max(0.05, min(3.0, sigma))
        vix_inflation = 1.0
        if vix is not None and vix > 25:
            vix_inflation = vix / 20.0
            sigma *= vix_inflation
            sigma = max(0.05, min(3.0, sigma))
        return 0.0, sigma, vix_inflation
    except Exception:
        return 0.0, 0.25, 1.0


# ---------------------------------------------------------------------------
# Core simulator
# ---------------------------------------------------------------------------

def simulate_trade_montecarlo(signal, intel=None):
    """
    Run 1000 Monte Carlo price paths for a trade signal.

    Parameters
    ----------
    signal : dict
        Required keys: entry (or price), stop, target1, target2, signal_type
        Optional keys: name, t212_ticker (or ticker), atr
    intel : dict, optional
        Optional key: vix (float)

    Returns
    -------
    dict with simulation summary
    """
    # --- Extract signal fields ---
    ticker = signal.get('t212_ticker') or signal.get('ticker') or signal.get('name', 'UNKNOWN')
    name   = signal.get('name') or ticker
    signal_type = str(signal.get('signal_type', 'TREND')).upper()

    try:
        entry = float(signal.get('entry') or signal.get('price') or 0)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        stop  = float(signal.get('stop') or 0)
    except (TypeError, ValueError):
        stop = 0.0
    try:
        t1    = float(signal.get('target1') or signal.get('t1') or 0)
    except (TypeError, ValueError):
        t1 = 0.0
    try:
        t2    = float(signal.get('target2') or signal.get('t2') or 0)
    except (TypeError, ValueError):
        t2 = 0.0

    atr = signal.get('atr')
    if atr is not None:
        try:
            atr = float(atr)
        except (TypeError, ValueError):
            atr = None

    vix = None
    if intel:
        try:
            vix = float(intel.get('vix') or 0) or None
        except (TypeError, ValueError):
            vix = None

    # --- Validate prices ---
    if entry <= 0:
        return _error_result(ticker, name, signal_type, signal, "Invalid entry price")
    if stop <= 0 or stop >= entry:
        # For short signals stop > entry; handle gracefully by treating as invalid
        return _error_result(ticker, name, signal_type, signal, "Invalid stop price")
    if t1 <= entry:
        t1 = entry * 1.03   # default 3% above entry
    if t2 <= t1:
        t2 = t1 * 1.02      # default 2% above t1

    # --- Determine max days by signal type ---
    max_days_map = {
        'TREND':      15,
        'CONTRARIAN': 20,
        'INVERSE':     3,
        'MOMENTUM':   15,
        'BREAKOUT':   15,
    }
    max_days = max_days_map.get(signal_type, 15)

    # --- Fetch / estimate vol params ---
    closes = _fetch_recent_closes(ticker)
    if closes and len(closes) >= 5:
        mu, sigma, vix_inflation = _estimate_params_from_closes(closes, vix=vix)
        data_source = 'yfinance'
    elif atr is not None and atr > 0:
        mu, sigma, vix_inflation = _estimate_params_from_atr(entry, atr, vix=vix)
        data_source = 'atr_fallback'
    else:
        vix_inflation = 1.0
        if vix is not None and vix > 25:
            vix_inflation = vix / 20.0
        sigma = 0.25 * vix_inflation
        sigma = max(0.05, min(3.0, sigma))
        mu = 0.0
        data_source = 'default'

    # --- Simulation ---
    n_paths = 1000
    dt = 1.0 / 252
    mu_term   = (mu - 0.5 * sigma ** 2) * dt
    sigma_sqrt = sigma * math.sqrt(dt)

    risk   = entry - stop          # positive value; used for R-multiple calc
    r_stop = -1.0
    r_t1   = (t1 - entry) / risk
    r_t2   = (t2 - entry) / risk

    results = []

    for _ in range(n_paths):
        price  = entry
        mae    = 0.0
        t1_hit = False
        outcome_rec = None

        for day in range(1, max_days + 1):
            z = random.gauss(0, 1)
            price = price * math.exp(mu_term + sigma_sqrt * z)

            if price <= 0:
                outcome_rec = {'outcome': 'STOP', 'r': r_stop, 'day': day, 'mae': mae}
                break

            r_current = (price - entry) / risk
            if r_current < mae:
                mae = r_current

            if price <= stop:
                outcome_rec = {'outcome': 'STOP', 'r': r_stop, 'day': day, 'mae': mae}
                break
            elif price >= t2:
                outcome_rec = {'outcome': 'T2', 'r': r_t2, 'day': day, 'mae': mae}
                break
            elif price >= t1 and not t1_hit:
                # 60% chance close at T1, 40% continue toward T2
                if random.random() < 0.6:
                    outcome_rec = {'outcome': 'T1', 'r': r_t1, 'day': day, 'mae': mae}
                    break
                else:
                    t1_hit = True

        if outcome_rec is None:
            # Time exit at current price
            r_final = (price - entry) / risk if risk != 0 else 0.0
            outcome_rec = {'outcome': 'TIMEOUT', 'r': r_final, 'day': max_days, 'mae': mae}

        results.append(outcome_rec)

    # --- Aggregate ---
    r_values   = [rec['r']   for rec in results]
    mae_values = [rec['mae'] for rec in results]
    day_values = [rec['day'] for rec in results]

    n = len(results)
    sim_win_rate = sum(1 for r in r_values if r > 0) / n

    mean_r = statistics.mean(r_values)
    try:
        std_r = statistics.stdev(r_values)
    except statistics.StatisticsError:
        std_r = 0.0

    sim_expected_r   = round(mean_r, 4)
    sim_sharpe       = round(mean_r / std_r, 4) if std_r > 0 else 0.0
    sim_p_day1_stop  = sum(1 for rec in results if rec['day'] == 1 and rec['outcome'] == 'STOP') / n

    # 5th percentile of MAE (most negative — worst adverse excursion in 95% of paths)
    sorted_mae   = sorted(mae_values)
    p5_idx       = max(0, int(0.05 * n) - 1)
    sim_mae_95th = round(sorted_mae[p5_idx], 4)

    # Median exit day
    sorted_days     = sorted(day_values)
    mid             = n // 2
    sim_median_days = (sorted_days[mid - 1] + sorted_days[mid]) / 2 if n % 2 == 0 else sorted_days[mid]

    # Outcome distribution
    outcome_dist = {'T1': 0, 'T2': 0, 'STOP': 0, 'TIMEOUT': 0}
    for rec in results:
        key = rec['outcome']
        if key in outcome_dist:
            outcome_dist[key] += 1
        else:
            outcome_dist[key] = 1

    # --- Verdict ---
    if sim_win_rate < 0.30 or sim_p_day1_stop > 0.30:
        verdict = 'FAIL'
        if sim_win_rate < 0.30:
            reason = f"Win rate too low ({sim_win_rate:.1%} < 30%)"
        else:
            reason = f"Day-1 stop risk too high ({sim_p_day1_stop:.1%} > 30%)"
    elif sim_win_rate < 0.40 or sim_p_day1_stop > 0.20:
        verdict = 'WARN'
        if sim_win_rate < 0.40:
            reason = f"Win rate marginal ({sim_win_rate:.1%} < 40%)"
        else:
            reason = f"Day-1 stop risk elevated ({sim_p_day1_stop:.1%} > 20%)"
    else:
        verdict = 'PASS'
        reason  = 'Simulated edge positive, day-1 stop risk acceptable'

    return {
        'ticker':           ticker,
        'name':             name,
        'signal_type':      signal_type,
        'entry':            round(entry, 4),
        'stop':             round(stop, 4),
        'target1':          round(t1, 4),
        'target2':          round(t2, 4),
        'n_paths':          n_paths,
        'mu':               round(mu, 4),
        'sigma':            round(sigma, 4),
        'vix_inflation':    round(vix_inflation, 4),
        'data_source':      data_source,
        'sim_win_rate':     round(sim_win_rate, 4),
        'sim_expected_r':   sim_expected_r,
        'sim_p_day1_stop':  round(sim_p_day1_stop, 4),
        'sim_mae_95th':     sim_mae_95th,
        'sim_median_days':  sim_median_days,
        'sim_sharpe':       sim_sharpe,
        'outcome_dist':     outcome_dist,
        'verdict':          verdict,
        'reason':           reason,
    }


def _error_result(ticker, name, signal_type, signal, reason):
    """Return a minimal error result dict."""
    return {
        'ticker':          ticker,
        'name':            name,
        'signal_type':     signal_type,
        'entry':           signal.get('entry') or signal.get('price'),
        'stop':            signal.get('stop'),
        'target1':         signal.get('target1'),
        'target2':         signal.get('target2'),
        'n_paths':         0,
        'mu':              None,
        'sigma':           None,
        'vix_inflation':   None,
        'data_source':     'error',
        'sim_win_rate':    None,
        'sim_expected_r':  None,
        'sim_p_day1_stop': None,
        'sim_mae_95th':    None,
        'sim_median_days': None,
        'sim_sharpe':      None,
        'outcome_dist':    {},
        'verdict':         'ERROR',
        'reason':          reason,
    }


# ---------------------------------------------------------------------------
# Batch runner — process pending signal queue
# ---------------------------------------------------------------------------

def run_batch():
    """
    Load pending signals and/or trade queue; run simulation for each.
    Write output to apex-rollout-results.json.
    """
    signals = []

    # Try pending signal
    pending = safe_read(os.path.join(LOGS, 'apex-pending-signal.json'))
    if pending and isinstance(pending, dict) and pending.get('ticker'):
        signals.append(pending)

    # Try trade queue
    queue = safe_read(os.path.join(LOGS, 'apex-trade-queue.json'))
    if isinstance(queue, list):
        for item in queue:
            if isinstance(item, dict) and item.get('status') in (None, 'pending', 'PENDING'):
                signals.append(item)
    elif isinstance(queue, dict):
        for item in queue.get('queue', []):
            if isinstance(item, dict):
                signals.append(item)

    # Load VIX from macro signals
    macro = safe_read(os.path.join(LOGS, 'apex-macro-signals.json'))
    vix = None
    if isinstance(macro, dict):
        vix = macro.get('vix') or macro.get('VIX')
        if vix is None:
            # Try nested
            for v in macro.values():
                if isinstance(v, dict) and 'vix' in v:
                    vix = v['vix']
                    break
    intel = {'vix': vix} if vix else None

    if not signals:
        # Nothing to simulate — write empty result
        output = {
            'timestamp':    _now_str(),
            'simulations':  [],
            'note':         'No pending signals found',
        }
        atomic_write(OUTPUT_FILE, output)
        return output

    sim_results = []
    for sig in signals:
        try:
            result = simulate_trade_montecarlo(sig, intel=intel)
            sim_results.append(result)
        except Exception as e:
            sys.stderr.write(f"[rollout-sim] Error simulating {sig}: {e}\n")

    output = {
        'timestamp':   _now_str(),
        'simulations': sim_results,
    }
    atomic_write(OUTPUT_FILE, output)
    return output


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------

def _print_summary(result):
    ticker      = result.get('ticker', 'UNKNOWN')
    signal_type = result.get('signal_type', '')
    entry       = result.get('entry')
    stop        = result.get('stop')
    t1          = result.get('target1')
    t2          = result.get('target2')
    mu          = result.get('mu')
    sigma       = result.get('sigma')
    vix_inf     = result.get('vix_inflation', 1.0)
    win_rate    = result.get('sim_win_rate')
    exp_r       = result.get('sim_expected_r')
    p_d1        = result.get('sim_p_day1_stop')
    mae_95      = result.get('sim_mae_95th')
    med_days    = result.get('sim_median_days')
    sharpe      = result.get('sim_sharpe')
    od          = result.get('outcome_dist', {})
    verdict     = result.get('verdict', 'ERROR')
    reason      = result.get('reason', '')
    n_paths     = result.get('n_paths', 0)

    def fmt(v, fmt_str='.4f'):
        return format(v, fmt_str) if v is not None else 'N/A'

    verdict_sym = {'PASS': '✓', 'WARN': '⚠', 'FAIL': '✗', 'ERROR': '!'}.get(verdict, '?')

    print()
    print(f"Monte Carlo Rollout — {ticker} ({signal_type})")
    print(f"  Entry: {fmt(entry)} | Stop: {fmt(stop)} | T1: {fmt(t1)} | T2: {fmt(t2)}")
    if mu is not None and sigma is not None:
        print(f"  Params: mu={fmt(mu, '.2f')} sigma={fmt(sigma, '.2f')} (VIX inflation: {fmt(vix_inf, '.1f')}x)")
    print()
    print(f"  Results ({n_paths} paths):")
    print(f"    Win rate:     {format(win_rate * 100, '.1f') + '%' if win_rate is not None else 'N/A'}")
    print(f"    Expected R:   {('+' if exp_r and exp_r >= 0 else '') + fmt(exp_r, '.2f') if exp_r is not None else 'N/A'}")
    print(f"    Day-1 stop:   {format(p_d1 * 100, '.1f') + '%' if p_d1 is not None else 'N/A'}")
    print(f"    MAE 95th:     {fmt(mae_95, '.1f') + 'R' if mae_95 is not None else 'N/A'}")
    print(f"    Median days:  {med_days if med_days is not None else 'N/A'}")
    print(f"    Sharpe:       {fmt(sharpe, '.2f') if sharpe is not None else 'N/A'}")
    print()
    print(f"  Outcomes: T1={od.get('T1', 0)} T2={od.get('T2', 0)} STOP={od.get('STOP', 0)} TIMEOUT={od.get('TIMEOUT', 0)}")
    print()
    print(f"  Verdict: {verdict} {verdict_sym}")
    if reason:
        print(f"  Reason:  {reason}")
    print()


def main_cli(args):
    """
    CLI entry point.
    Usage: apex-rollout-sim.py TICKER SIGNAL_TYPE ENTRY STOP T1 T2 [VIX]
    """
    if len(args) < 6:
        print("Usage: apex-rollout-sim.py TICKER SIGNAL_TYPE ENTRY STOP T1 T2 [VIX]")
        print("  e.g: apex-rollout-sim.py NVDA TREND 145.20 139.50 151.40 156.10 22")
        sys.exit(1)

    try:
        ticker      = args[0]
        signal_type = args[1].upper()
        entry       = float(args[2])
        stop        = float(args[3])
        t1          = float(args[4])
        t2          = float(args[5])
        vix         = float(args[6]) if len(args) > 6 else None
    except (IndexError, ValueError) as e:
        print(f"Error parsing arguments: {e}")
        sys.exit(1)

    signal = {
        'ticker':      ticker,
        't212_ticker': ticker,
        'name':        ticker,
        'signal_type': signal_type,
        'entry':       entry,
        'stop':        stop,
        'target1':     t1,
        'target2':     t2,
    }
    intel = {'vix': vix} if vix is not None else None

    result = simulate_trade_montecarlo(signal, intel=intel)
    _print_summary(result)

    # Also persist to log file
    output = {
        'timestamp':   _now_str(),
        'simulations': [result],
    }
    atomic_write(OUTPUT_FILE, output)
    print(f"  [Saved to {OUTPUT_FILE}]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    args = sys.argv[1:]
    if args:
        main_cli(args)
    else:
        output = run_batch()
        n = len(output.get('simulations', []))
        print(f"[rollout-sim] Completed {n} simulation(s) → {OUTPUT_FILE}")
        if n:
            for sim in output['simulations']:
                v = sim.get('verdict', 'ERROR')
                t = sim.get('ticker', '?')
                r = sim.get('sim_expected_r')
                wr = sim.get('sim_win_rate')
                print(f"  {t}: {v}  E[R]={r}  win={wr}")
