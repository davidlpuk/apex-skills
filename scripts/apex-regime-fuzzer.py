#!/usr/bin/env python3
"""
Apex Regime Fuzzer
Stress-tests the risk management system against synthetic worst-case scenarios:
- VIX spike to 40 while holding 4 positions
- Breadth collapse 70% → 25% in 3 days
- Simultaneous geo alert + circuit breaker
Runs monthly. Outputs apex-regime-fuzz-results.json.
"""
import json, os, sys, math, random, copy
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read
except ImportError:
    def atomic_write(p, d):
        tmp = p + '.tmp'
        with open(tmp, 'w') as f: json.dump(d, f, indent=2)
        os.replace(tmp, p)
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except: return d if d is not None else {}

LOGS                = '/home/ubuntu/.picoclaw/logs'
POSITIONS_FILE      = f'{LOGS}/apex-positions.json'
REGIME_SCALING_FILE = f'{LOGS}/apex-regime-scaling.json'
CIRCUIT_BREAKER_FILE= f'{LOGS}/apex-circuit-breaker.json'
OUTPUT_FILE         = f'{LOGS}/apex-regime-fuzz-results.json'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_positions():
    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except:
        return []


def load_regime_state():
    try:
        with open(REGIME_SCALING_FILE) as f:
            return json.load(f)
    except:
        return {}


def load_cb_state():
    try:
        with open(CIRCUIT_BREAKER_FILE) as f:
            return json.load(f)
    except:
        return {}


def regime_label(combined_scale):
    if combined_scale == 0:
        return 'BLOCKED'
    elif combined_scale < 0.2:
        return 'HOSTILE'
    elif combined_scale < 0.5:
        return 'CAUTIOUS'
    elif combined_scale < 0.8:
        return 'NEUTRAL'
    else:
        return 'FAVOURABLE'


# ---------------------------------------------------------------------------
# Core simulation engine
# ---------------------------------------------------------------------------

def simulate_scenario(scenario, positions, regime_state):
    results = {
        'scenario': scenario['name'],
        'description': scenario['description'],
        'days_simulated': 0,
        'trigger_day': None,
        'regime_blocked_day': None,
        'max_portfolio_drawdown_pct': 0.0,
        'positions_at_risk': 0,
        'new_entries_stopped': False,
        'sizing_reduction_pct': 0,
        'verdict': 'PASS',
        'detail': [],
    }

    params = scenario['params']
    portfolio_value = sum(
        abs(p.get('quantity', 0) * p.get('entry', 0)) for p in positions
    )
    if portfolio_value == 0:
        portfolio_value = 5000.0

    vix     = params.get('vix_start', regime_state.get('vix', 20))
    breadth = params.get('breadth_start', regime_state.get('breadth', 50))
    days    = params.get('days', 3)

    for day in range(1, days + 1):
        progress     = day / days
        vix_today    = vix + (params.get('vix_end', vix) - vix) * progress
        breadth_today= breadth + (params.get('breadth_end', breadth) - breadth) * progress

        # Geo / CB override (Scenario 3)
        geo_alert = params.get('geo_alert', False)
        cb_suspend = params.get('cb_suspend', False)

        vix_scale    = max(0.0, 1.0 - (vix_today - 15.0) / 20.0)
        breadth_scale= max(0.0, (breadth_today - 20.0) / 50.0)
        combined_scale = math.sqrt(vix_scale * breadth_scale)

        # Hard overrides: geo alert or CB suspension forces combined_scale to 0
        if geo_alert or cb_suspend:
            combined_scale = 0.0

        label = regime_label(combined_scale)

        # Estimate daily adverse move using VIX as proxy
        daily_adverse_pct = (vix_today / 100.0) * 0.5

        positions_at_stop = 0
        for p in positions:
            entry   = p.get('entry', 0) or 0
            stop    = p.get('stop', entry * 0.94)
            current = p.get('current', entry) or entry
            simulated_price = current * (1.0 - daily_adverse_pct)
            if stop and simulated_price <= stop:
                positions_at_stop += 1

        # Circuit breaker check: session loss > 8%
        session_loss_pct = daily_adverse_pct * 100.0
        cb_triggered = session_loss_pct >= 8.0

        day_detail = {
            'day': day,
            'vix': round(vix_today, 1),
            'breadth': round(breadth_today, 1),
            'combined_scale': round(combined_scale, 2),
            'regime_label': label,
            'positions_at_stop': positions_at_stop,
            'estimated_session_loss_pct': round(session_loss_pct, 1),
            'circuit_breaker_triggered': cb_triggered,
            'geo_alert_active': geo_alert,
            'cb_suspend_active': cb_suspend,
        }
        results['detail'].append(day_detail)

        if combined_scale == 0.0 and results['regime_blocked_day'] is None:
            results['regime_blocked_day'] = day

        if cb_triggered and results['trigger_day'] is None:
            results['trigger_day'] = day

    # Max portfolio drawdown: worst-day adverse move applied to full portfolio
    worst_day = max(results['detail'], key=lambda d: d['vix'])
    adverse_pct = worst_day['vix'] / 100.0 * 0.5
    results['max_portfolio_drawdown_pct'] = round(adverse_pct * 100.0, 1)
    results['positions_at_risk'] = len(positions)

    last = results['detail'][-1]
    if last['combined_scale'] < 0.2:
        results['new_entries_stopped'] = True
        results['sizing_reduction_pct'] = round((1.0 - last['combined_scale']) * 100.0, 0)

    # Verdict
    if results['regime_blocked_day'] is not None:
        results['verdict'] = 'FAIL' if results['regime_blocked_day'] <= 2 else 'WARN'
    elif results['new_entries_stopped']:
        results['verdict'] = 'WARN'
    else:
        results['verdict'] = 'PASS'

    results['days_simulated'] = days
    return results


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def build_scenarios(regime_state):
    current_vix    = regime_state.get('vix', 20.0)
    current_breadth= regime_state.get('breadth', 50.0)

    return [
        {
            'name': 'black_tuesday',
            'description': f'VIX spikes from {current_vix:.0f} to 40 while holding 4 positions',
            'params': {
                'vix_start': current_vix,
                'vix_end': 40.0,
                'breadth_start': current_breadth,
                'breadth_end': current_breadth - 20.0,
                'days': 3,
            },
        },
        {
            'name': 'breadth_collapse',
            'description': 'Market breadth collapses from 70% to 25% over 3 days',
            'params': {
                'vix_start': current_vix,
                'vix_end': current_vix + 5.0,   # mild VIX rise alongside breadth collapse
                'breadth_start': 70.0,
                'breadth_end': 25.0,
                'days': 3,
            },
        },
        {
            'name': 'geo_cb_storm',
            'description': 'Simultaneous geo alert + circuit breaker suspension, VIX=33',
            'params': {
                'vix_start': 33.0,
                'vix_end': 33.0,
                'breadth_start': current_breadth,
                'breadth_end': current_breadth,
                'days': 1,
                'geo_alert': True,
                'cb_suspend': True,
            },
        },
    ]


# ---------------------------------------------------------------------------
# CLI printer
# ---------------------------------------------------------------------------

VERDICT_ICONS = {'PASS': 'PASS', 'WARN': 'WARN', 'FAIL': 'FAIL'}
REGIME_MARKERS = {'BLOCKED': '!!! BLOCKED', 'HOSTILE': '!! HOSTILE', 'CAUTIOUS': '! CAUTIOUS',
                  'NEUTRAL': '  NEUTRAL', 'FAVOURABLE': '  FAVOURABLE'}


def print_scenario(res, idx):
    name = res['scenario'].upper().replace('_', ' ')
    desc = res['description']
    print(f"\n  Scenario {idx}: {name}")
    print(f"    {desc}")

    for d in res['detail']:
        label   = d['regime_label']
        marker  = ''
        if d['regime_blocked_day_flag'] if 'regime_blocked_day_flag' in d else False:
            marker = ' <- regime blocked'
        if res['regime_blocked_day'] == d['day']:
            marker = ' <- regime blocked'
        elif res['trigger_day'] == d['day']:
            marker = ' <- circuit breaker'

        cb_flag = ' [CB!]' if d.get('circuit_breaker_triggered') else ''
        geo_flag= ' [GEO]' if d.get('geo_alert_active') else ''
        sus_flag= ' [SUSPEND]' if d.get('cb_suspend_active') else ''
        extras  = cb_flag + geo_flag + sus_flag

        print(
            f"    Day {d['day']}: VIX={d['vix']} Breadth={d['breadth']}% | "
            f"Scale={d['combined_scale']:.2f} | {label}{extras}{marker}"
        )

    verdict = res['verdict']
    if verdict == 'FAIL':
        detail = f"regime blocked on day {res['regime_blocked_day']}, {res['positions_at_risk']} positions at risk"
        icon   = 'FAIL'
    elif verdict == 'WARN':
        detail = f"new entries stopped, sizing reduced {res['sizing_reduction_pct']:.0f}%"
        icon   = 'WARN'
    else:
        detail = 'system correctly halted entries'
        icon   = 'PASS'

    dd = res['max_portfolio_drawdown_pct']
    print(f"    Max drawdown estimate: {dd:.1f}%  |  Verdict: {icon} — {detail}")


def print_summary(summary, portfolio_value, n_positions, ts):
    date_str = ts[:10]
    print(f"\n{'='*60}")
    print(f"APEX REGIME FUZZER — {date_str}")
    print(f"  Portfolio: £{portfolio_value:,.0f} | Positions: {n_positions}")

    n_pass = summary['scenarios_passed']
    n_warn = summary['scenarios_warned']
    n_fail = summary['scenarios_failed']
    assess = summary['system_assessment']

    print(f"\n  Summary: {n_pass} PASS | {n_warn} WARN | {n_fail} FAIL")
    print(f"  System assessment: {assess}")
    print(f"  Results saved to apex-regime-fuzz-results.json")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    ts  = now.strftime('%Y-%m-%d %H:%M UTC')

    positions    = load_positions()
    regime_state = load_regime_state()
    cb_state     = load_cb_state()

    portfolio_value = sum(
        abs(p.get('quantity', 0) * p.get('entry', 0)) for p in positions
    )
    if portfolio_value == 0:
        portfolio_value = 5000.0

    n_positions = len(positions)

    scenarios     = build_scenarios(regime_state)
    scenario_results = []

    for scenario in scenarios:
        res = simulate_scenario(scenario, positions, regime_state)
        scenario_results.append(res)

    n_pass = sum(1 for r in scenario_results if r['verdict'] == 'PASS')
    n_warn = sum(1 for r in scenario_results if r['verdict'] == 'WARN')
    n_fail = sum(1 for r in scenario_results if r['verdict'] == 'FAIL')

    if n_fail > 0:
        assessment = f'CAUTION — {n_fail} failure scenario{"s" if n_fail > 1 else ""} found'
    elif n_warn > 0:
        assessment = f'MONITOR — {n_warn} warning scenario{"s" if n_warn > 1 else ""} found'
    else:
        assessment = 'ALL CLEAR — all scenarios within acceptable bounds'

    summary = {
        'scenarios_passed': n_pass,
        'scenarios_warned': n_warn,
        'scenarios_failed': n_fail,
        'system_assessment': assessment,
    }

    output = {
        'timestamp': ts,
        'portfolio_value': round(portfolio_value, 2),
        'n_positions': n_positions,
        'regime_at_run': {
            'vix': regime_state.get('vix'),
            'breadth': regime_state.get('breadth'),
            'combined_scale': regime_state.get('combined_scale'),
            'regime_label': regime_state.get('regime_label'),
        },
        'cb_at_run': {
            'status': cb_state.get('status'),
            'triggered': cb_state.get('triggered'),
            'session_pnl_pct': cb_state.get('session_pnl_pct'),
        },
        'scenarios': scenario_results,
        'summary': summary,
    }

    atomic_write(OUTPUT_FILE, output)

    # CLI output
    print(f"\nAPEX REGIME FUZZER — {ts}")
    print(f"  Portfolio: £{portfolio_value:,.0f} | Positions: {n_positions}")
    print(f"  Regime at run: VIX={regime_state.get('vix', 'N/A')} | "
          f"Breadth={regime_state.get('breadth', 'N/A')}% | "
          f"Label={regime_state.get('regime_label', 'N/A')}")

    for i, res in enumerate(scenario_results, 1):
        print_scenario(res, i)

    print(f"\n  Summary: {n_pass} PASS | {n_warn} WARN | {n_fail} FAIL")
    if n_fail > 0:
        print(f"  System assessment: CAUTION — {n_fail} failure scenario{'s' if n_fail > 1 else ''} found")
    elif n_warn > 0:
        print(f"  System assessment: MONITOR — {n_warn} warning scenario{'s' if n_warn > 1 else ''} found")
    else:
        print(f"  System assessment: ALL CLEAR — all scenarios within acceptable bounds")
    print(f"  Results saved to apex-regime-fuzz-results.json\n")

    return 0


if __name__ == '__main__':
    sys.exit(main())
