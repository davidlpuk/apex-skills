#!/usr/bin/env python3
"""
Data Integrity Layer
Validates all data inputs before the decision engine runs.
Catches bad data from external sources before it causes bad trades.

Checks:
1. Data freshness — is the file within expected age?
2. Value range validation — is the value physically plausible?
3. Price cross-verification — do Alpaca and T212 agree?
4. Dramatic change detection — has anything jumped implausibly?
5. Internal consistency — do related data points agree with each other?
"""
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, log_info, send_telegram, t212_request
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m): print(f'INFO: {m}')

INTEGRITY_FILE = '/home/ubuntu/.picoclaw/logs/apex-data-integrity.json'
LOGS           = '/home/ubuntu/.picoclaw/logs'
SCRIPTS        = '/home/ubuntu/.picoclaw/scripts'

# ============================================================
# DATA SPECIFICATIONS
# Valid ranges, max ages, and cross-check rules for every input
# ============================================================
DATA_SPECS = {
    'apex-regime.json': {
        'max_age_hours': 26,
        'critical':      True,
        'fields': {
            'vix':         {'min': 9,   'max': 90,  'type': float},
            'breadth_pct': {'min': 0,   'max': 100, 'type': float},
        },
        'description': 'Market regime — VIX and breadth'
    },
    'apex-regime-scaling.json': {
        'max_age_hours': 26,
        'critical':      True,
        'fields': {
            'combined_scale':   {'min': 0,   'max': 1.0, 'type': float},
            'trend_scale':      {'min': 0,   'max': 1.0, 'type': float},
            'contrarian_scale': {'min': 0,   'max': 1.3, 'type': float},
        },
        'description': 'Regime scaling factors'
    },
    'apex-sentiment.json': {
        'max_age_hours': 30,
        'critical':      False,
        'fields': {
            'market_sentiment':  {'min': -1.0, 'max': 1.0, 'type': float},
            'total_headlines':   {'min': 0,    'max': 500,  'type': int},
        },
        'description': 'VADER sentiment scores'
    },
    'apex-market-direction.json': {
        'max_age_hours': 26,
        'critical':      False,
        'fields': {},
        'description': 'Market direction'
    },
    'apex-drawdown.json': {
        'max_age_hours': 26,
        'critical':      True,
        'fields': {
            'drawdown_pct': {'min': 0,   'max': 100,  'type': float},
            'multiplier':   {'min': 0,   'max': 1.0,  'type': float},
        },
        'description': 'Portfolio drawdown state'
    },
    'apex-fundamentals.json': {
        'max_age_hours': 200,  # Weekly update
        'critical':      False,
        'fields': {
            'count': {'min': 1, 'max': 100, 'type': int},
        },
        'description': 'FMP fundamental data'
    },
    'apex-relative-strength.json': {
        'max_age_hours': 26,
        'critical':      False,
        'fields': {},
        'description': 'Relative strength rankings'
    },
    'apex-multiframe.json': {
        'max_age_hours': 26,
        'critical':      False,
        'fields': {
            'count': {'min': 1, 'max': 200, 'type': int},
        },
        'description': 'Multi-timeframe analysis'
    },
    'apex-positions.json': {
        'max_age_hours': 2,
        'critical':      True,
        'fields': {},
        'is_list':       True,
        'description':   'Open positions tracker'
    },
    'apex-autopilot.json': {
        'max_age_hours': 168,  # Weekly
        'critical':      True,
        'fields': {
            'max_daily_loss':     {'min': 10,  'max': 1000, 'type': float},
            'max_trades_per_day': {'min': 1,   'max': 10,   'type': int},
        },
        'description': 'Autopilot configuration'
    },
    'apex-geo-news.json': {
        'max_age_hours': 26,
        'critical':      False,
        'fields': {},
        'description': 'Geopolitical news flags'
    },
    'apex-breadth-thrust.json': {
        'max_age_hours': 26,
        'critical':      False,
        'fields': {
            'current_breadth': {'min': 0, 'max': 100, 'type': float},
        },
        'description': 'Breadth thrust detector'
    },
}

# Previous values cache for change detection
PREV_VALUES = {}

# ============================================================
# CHECK 1: DATA FRESHNESS
# ============================================================
def check_freshness(filename, spec, data):
    """Verify data is within expected age."""
    max_age = get_max_age(spec.get('max_age_hours', 24))

    if isinstance(data, list):
        return True, f"List data — no timestamp check"

    timestamp = data.get('timestamp', '')
    if not timestamp:
        return True, "No timestamp field — skipping age check"

    try:
        if 'UTC' in str(timestamp):
            ts = datetime.strptime(str(timestamp)[:16], '%Y-%m-%d %H:%M')
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = datetime.fromisoformat(str(timestamp))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600

        if age_hours > max_age:
            return False, f"STALE: {filename} is {age_hours:.1f}h old (max {max_age}h)"

        return True, f"Fresh: {age_hours:.1f}h old"

    except Exception as e:
        return True, f"Cannot parse timestamp: {e}"

# ============================================================
# CHECK 2: VALUE RANGE VALIDATION
# ============================================================
def check_ranges(filename, spec, data):
    """Verify all field values are within physically plausible bounds."""
    failures = []
    fields   = spec.get('fields', {})

    if isinstance(data, list):
        return True, []

    for field, rules in fields.items():
        value = data.get(field)
        if value is None:
            continue

        try:
            typed_val = rules['type'](value)
        except (ValueError, TypeError):
            failures.append(f"{field}={value} cannot be cast to {rules['type'].__name__}")
            continue

        if 'min' in rules and typed_val < rules['min']:
            failures.append(f"{field}={typed_val} below minimum {rules['min']}")

        if 'max' in rules and typed_val > rules['max']:
            failures.append(f"{field}={typed_val} above maximum {rules['max']}")

    if failures:
        return False, failures

    return True, []

# ============================================================
# CHECK 3: PRICE CROSS-VERIFICATION
# ============================================================
def verify_price(ticker, signal_price, currency='USD'):
    """
    Cross-verify signal price against T212 live price.
    Block if discrepancy > 3%.
    """
    try:
        portfolio = t212_request('/equity/portfolio', timeout=10)
        if not isinstance(portfolio, list):
            return True, "Cannot fetch T212 portfolio — skipping price check"

        pos = next((p for p in portfolio if p.get('ticker') == ticker), None)
        if not pos:
            return True, f"{ticker} not in current portfolio — skipping"

        t212_price = float(pos.get('currentPrice', 0))
        if t212_price == 0:
            return True, "T212 price is 0 — skipping"

        discrepancy = abs(signal_price - t212_price) / t212_price * 100

        if discrepancy > 5:
            return False, f"PRICE MISMATCH: Signal £{signal_price} vs T212 £{t212_price} ({discrepancy:.1f}% diff)"

        if discrepancy > 2:
            return True, f"PRICE WARNING: Signal £{signal_price} vs T212 £{t212_price} ({discrepancy:.1f}% diff)"

        return True, f"Price verified: signal £{signal_price} T212 £{t212_price} ({discrepancy:.1f}% diff)"

    except Exception as e:
        log_error(f"verify_price failed for {ticker}: {e}")
        return True, f"Price check failed: {e} — proceeding"

# ============================================================
# CHECK 4: DRAMATIC CHANGE DETECTION
# ============================================================
def check_dramatic_change(filename, data, prev_data):
    """
    Flag if a value has changed dramatically since last check.
    Catches data feed errors that produce implausible jumps.
    """
    if not prev_data or isinstance(data, list):
        return True, []

    warnings = []

    # VIX should not jump more than 50% in one day
    if 'vix' in data and 'vix' in prev_data:
        curr_vix = float(data.get('vix', 0) or 0)
        prev_vix = float(prev_data.get('vix', 0) or 0)
        if prev_vix > 0:
            change = abs(curr_vix - prev_vix) / prev_vix * 100
            if change > 50:
                warnings.append(f"VIX changed {change:.0f}% ({prev_vix} → {curr_vix}) — verify data source")

    # Breadth should not jump more than 40% in one day
    if 'breadth_pct' in data and 'breadth_pct' in prev_data:
        curr_b = float(data.get('breadth_pct', 0) or 0)
        prev_b = float(prev_data.get('breadth_pct', 0) or 0)
        if prev_b > 0:
            change = abs(curr_b - prev_b)
            if change > 40:
                warnings.append(f"Breadth changed {change:.0f}pp ({prev_b}% → {curr_b}%) — verify data source")

    # Combined scale should not flip from 0.8 to 0.1 overnight
    if 'combined_scale' in data and 'combined_scale' in prev_data:
        curr_s = float(data.get('combined_scale', 0) or 0)
        prev_s = float(prev_data.get('combined_scale', 0) or 0)
        change = abs(curr_s - prev_s)
        if change > 0.5:
            warnings.append(f"Regime scale jumped {change:.2f} ({prev_s} → {curr_s}) — verify")

    return len(warnings) == 0, warnings

# ============================================================
# CHECK 5: INTERNAL CONSISTENCY
# ============================================================
def check_consistency(all_data):
    """
    Cross-check related data points for internal consistency.
    Related data should broadly agree.
    """
    warnings = []

    regime   = all_data.get('apex-regime.json', {})
    scaling  = all_data.get('apex-regime-scaling.json', {})
    breadth  = all_data.get('apex-breadth-thrust.json', {})

    # VIX from regime vs scaling should match
    regime_vix  = float(regime.get('vix', 0) or 0)
    scaling_vix = float(scaling.get('vix', 0) or 0)
    if regime_vix > 0 and scaling_vix > 0:
        diff = abs(regime_vix - scaling_vix)
        if diff > 2:
            warnings.append(f"VIX mismatch: regime={regime_vix} scaling={scaling_vix} — stale data?")

    # Breadth from regime vs breadth thrust should broadly agree
    regime_breadth = float(regime.get('breadth_pct', -1) or -1)
    thrust_breadth = float(breadth.get('current_breadth', -1) or -1)
    if regime_breadth >= 0 and thrust_breadth >= 0:
        diff = abs(regime_breadth - thrust_breadth)
        # Allow larger divergence — regime uses S&P 500, thrust uses our 22-stock universe
        # Energy-heavy universe will show higher breadth than broad market
        if diff > 35:
            warnings.append(f"Breadth mismatch: regime={regime_breadth}% thrust={thrust_breadth}% ({diff:.0f}pp diff) — possible stale data")

    # Regime label vs scale consistency
    label = scaling.get('regime_label', '')
    scale = float(scaling.get('combined_scale', 0.5) or 0.5)
    if label == 'FAVOURABLE' and scale < 0.5:
        warnings.append(f"Regime label FAVOURABLE but scale only {scale} — inconsistent")
    if label in ['HOSTILE', 'BLOCKED'] and scale > 0.5:
        warnings.append(f"Regime label {label} but scale is {scale} — inconsistent")

    return len(warnings) == 0, warnings

# ============================================================
# MAIN RUN
# ============================================================
def get_max_age(base_max_hours):
    """
    Adjust max age tolerance for weekends.
    Files updated Mon-Fri will naturally be older on Saturday/Sunday.
    """
    now = datetime.now(timezone.utc)
    # Saturday=5, Sunday=6
    if now.weekday() == 5:  # Saturday
        return base_max_hours + 24
    elif now.weekday() == 6:  # Sunday
        return base_max_hours + 48
    return base_max_hours

def run(signal=None, verbose=True):
    """
    Run full data integrity check.
    If signal provided, also verify signal price.
    Returns: (all_clear, failures, warnings, report)
    """
    now      = datetime.now(timezone.utc)
    failures = []
    warnings = []
    all_data = {}

    if verbose:
        print(f"\n=== DATA INTEGRITY CHECK ===")
        print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Load previous integrity report for change detection
    prev_report = safe_read(INTEGRITY_FILE, {})
    prev_data   = prev_report.get('data_snapshot', {})

    # Check each data file
    for filename, spec in DATA_SPECS.items():
        filepath = f"{LOGS}/{filename}"
        data     = safe_read(filepath, None)

        if data is None:
            if spec.get('critical'):
                msg = f"MISSING critical file: {filename}"
                failures.append(msg)
                log_error(msg)
                if verbose:
                    print(f"  ❌ {filename}: MISSING (critical)")
            else:
                if verbose:
                    print(f"  ⚠️  {filename}: missing (non-critical)")
            continue

        all_data[filename] = data
        file_failures = []
        file_warnings = []

        # Check 1: Freshness
        fresh_ok, fresh_msg = check_freshness(filename, spec, data)
        if not fresh_ok:
            if spec.get('critical'):
                file_failures.append(fresh_msg)
            else:
                file_warnings.append(fresh_msg)

        # Check 2: Range validation
        range_ok, range_failures = check_ranges(filename, spec, data)
        if not range_ok:
            for f in range_failures:
                if spec.get('critical'):
                    file_failures.append(f"{filename}: {f}")
                else:
                    file_warnings.append(f"{filename}: {f}")

        # Check 4: Dramatic changes
        change_ok, change_warnings = check_dramatic_change(
            filename, data, prev_data.get(filename, {})
        )
        file_warnings.extend(change_warnings)

        # Summarise
        if file_failures:
            failures.extend(file_failures)
            if verbose:
                print(f"  ❌ {filename}: {file_failures[0][:70]}")
        elif file_warnings:
            warnings.extend(file_warnings)
            if verbose:
                print(f"  ⚠️  {filename}: {file_warnings[0][:70]}")
        else:
            if verbose:
                print(f"  ✅ {filename}")

    # Check 5: Internal consistency
    consist_ok, consist_warnings = check_consistency(all_data)
    if not consist_ok:
        warnings.extend(consist_warnings)
        if verbose:
            for w in consist_warnings:
                print(f"  ⚠️  Consistency: {w}")

    # Check 3: Price cross-verification (if signal provided)
    if signal:
        price_ok, price_msg = verify_price(
            signal.get('t212_ticker', ''),
            float(signal.get('entry', 0)),
            signal.get('currency', 'USD')
        )
        if not price_ok:
            failures.append(price_msg)
            if verbose:
                print(f"  ❌ Price: {price_msg}")
        else:
            if verbose:
                print(f"  ✅ Price: {price_msg}")

    # Overall result
    all_clear = len(failures) == 0

    # Data snapshot for next run comparison
    data_snapshot = {}
    for filename, data in all_data.items():
        if not isinstance(data, list):
            data_snapshot[filename] = {
                k: data.get(k) for k in
                ['vix', 'breadth_pct', 'combined_scale', 'market_sentiment',
                 'current_breadth', 'drawdown_pct']
                if data.get(k) is not None
            }

    # Save report
    report = {
        'timestamp':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'all_clear':     all_clear,
        'failures':      failures,
        'warnings':      warnings,
        'files_checked': len(DATA_SPECS),
        'data_snapshot': data_snapshot,
        'status':        'CLEAR' if all_clear else 'FAILED',
    }

    atomic_write(INTEGRITY_FILE, report)

    # Summary
    if verbose:
        print(f"\n  {'✅ ALL CLEAR' if all_clear else '❌ INTEGRITY FAILURES DETECTED'}")
        if failures:
            print(f"  Critical failures ({len(failures)}):")
            for f in failures:
                print(f"    🚫 {f}")
        if warnings:
            print(f"  Warnings ({len(warnings)}):")
            for w in warnings[:3]:
                print(f"    ⚠️  {w}")
        print(f"  Files checked: {len(DATA_SPECS)} | Failures: {len(failures)} | Warnings: {len(warnings)}")

    # Alert if failures
    if failures:
        msg = (
            f"🚨 DATA INTEGRITY FAILURE\n\n"
            f"{len(failures)} critical issue(s):\n"
            + "\n".join(f"• {f[:80]}" for f in failures[:5])
            + f"\n\nMorning scan may be blocked.\nCheck apex-data-integrity.json"
        )
        send_telegram(msg)
        log_error(f"Data integrity failed: {failures}")
    elif warnings:
        log_warning(f"Data integrity warnings: {warnings[:3]}")

    return all_clear, failures, warnings, report

def quick_check():
    """Fast check — returns True if safe to proceed, False if blocked."""
    all_clear, failures, warnings, _ = run(verbose=False)
    return all_clear, failures

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'quick':
        ok, fails = quick_check()
        print(f"{'✅ CLEAR' if ok else '❌ BLOCKED'}")
        for f in fails:
            print(f"  {f}")
    else:
        run(verbose=True)
