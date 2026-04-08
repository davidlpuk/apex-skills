#!/usr/bin/env python3
"""apex-query.py — Query current APEX system state.

Agent-native read-only interface to live system data.
Returns structured JSON summaries without needing dashboard auth.

Usage:
    python3 apex-query.py positions
    python3 apex-query.py regime
    python3 apex-query.py signals
    python3 apex-query.py health
    python3 apex-query.py queue
    python3 apex-query.py autopilot
    python3 apex-query.py performance
    python3 apex-query.py learning
    python3 apex-query.py schedule
    python3 apex-query.py all
"""

import json
import os
import sys
from datetime import datetime, timezone

LOGS    = '/home/ubuntu/.picoclaw/logs'
SCRIPTS = '/home/ubuntu/.picoclaw/scripts'


def _read(fname, default=None):
    try:
        with open(os.path.join(LOGS, fname)) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _age_mins(fname):
    try:
        mtime = os.path.getmtime(os.path.join(LOGS, fname))
        return round((datetime.now().timestamp() - mtime) / 60, 1)
    except Exception:
        return None


def query_positions():
    pos  = _read('apex-positions.json', {})
    cache = _read('apex-portfolio-cache.json', {})
    positions = pos if isinstance(pos, list) else pos.get('positions', [])
    total_ppl = sum(float(p.get('ppl', 0) or 0) for p in positions if isinstance(p, dict))
    return {
        'source': 'apex-positions.json',
        'age_mins': _age_mins('apex-positions.json'),
        'count': len(positions),
        'total_value': cache.get('invested'),
        'cash': cache.get('free'),
        'ppl': round(total_ppl, 2),
        'positions': [
            {
                'ticker':    p.get('ticker') or p.get('name'),
                'qty':       p.get('quantity'),
                'entry':     p.get('entry_price') or p.get('averagePrice'),
                'current':   p.get('currentPrice'),
                'pnl':       p.get('ppl') or p.get('unrealised_pnl'),
                'stop':      p.get('stop_price'),
                'signal_type': p.get('signal_type'),
            }
            for p in positions
        ],
    }


def query_regime():
    regime   = _read('apex-regime.json', {})
    scaling  = _read('apex-regime-scaling.json', {})
    breaker  = _read('apex-circuit-breaker.json', {})
    direction = _read('apex-market-direction.json', {})
    return {
        'source': 'apex-regime.json',
        'age_mins': _age_mins('apex-regime.json'),
        'overall':       regime.get('overall'),
        'vix':           regime.get('vix'),
        'breadth_pct':   regime.get('breadth_pct'),
        'block_reason':  regime.get('block_reason', []),
        'size_multiplier': scaling.get('multiplier', 1.0),
        'scaling_reason': scaling.get('reason'),
        'circuit_breaker': breaker.get('status'),
        'market_direction': direction.get('overall'),
    }


def query_signals():
    pending  = _read('apex-pending-signal.json', None)
    ev_log   = _read('apex-ev-log.json', [])
    queue    = _read('apex-trade-queue.json', {})
    # ev-log is a list of records; take the most recent
    last_ev = ev_log[-1] if isinstance(ev_log, list) and ev_log else (ev_log if isinstance(ev_log, dict) else {})
    return {
        'pending_signal': pending,
        'pending_age_mins': _age_mins('apex-pending-signal.json') if pending else None,
        'ev_summary': {
            'last_ev':    last_ev.get('ev'),
            'signal_type': last_ev.get('signal_type'),
            'verdict':    last_ev.get('verdict'),
        } if last_ev else None,
        'queue_count': len(queue) if isinstance(queue, list) else len(queue.get('queue', [])),
        'queue_items': [
            {'ticker': q.get('name') or q.get('ticker'), 'signal_type': q.get('signal_type'), 'queued_at': q.get('queued_at'), 'status': q.get('status')}
            for q in (queue if isinstance(queue, list) else queue.get('queue', []))
        ],
    }


def query_health():
    breaker = _read('apex-circuit-breaker.json', {})
    drawdown = _read('apex-drawdown.json', {})
    staleness = _read('apex-staleness-check.json', {})
    integrity = _read('apex-data-integrity.json', {})
    try:
        with open(os.path.join(LOGS, 'apex-health.log')) as f:
            health_lines = f.readlines()[-10:]
    except Exception:
        health_lines = []
    return {
        'circuit_breaker': {
            'status':  breaker.get('status'),
            'blocks':  breaker.get('blocks', []),
            'age_mins': _age_mins('apex-circuit-breaker.json'),
        },
        'drawdown': {
            'status':       drawdown.get('status'),
            'drawdown_pct': drawdown.get('drawdown_pct'),
            'multiplier':   drawdown.get('multiplier'),
        },
        'data_integrity': {
            'checks_passed': integrity.get('checks_passed'),
            'checks_failed': integrity.get('checks_failed'),
            'issues':        integrity.get('issues', []),
        },
        'recent_health_log': [l.strip() for l in health_lines if l.strip()],
    }


def query_queue():
    queue   = _read('apex-trade-queue.json', [])
    ap      = _read('apex-autopilot.json', {})
    breaker = _read('apex-circuit-breaker.json', {})
    queue_list = queue if isinstance(queue, list) else queue.get('queue', [])
    return {
        'queue': queue_list,
        'queue_count': len(queue_list),
        'autopilot_enabled': ap.get('enabled'),
        'trades_today': ap.get('trades_today'),
        'max_trades': ap.get('max_trades_per_day'),
        'circuit_status': breaker.get('status'),
        'age_mins': _age_mins('apex-trade-queue.json'),
    }


def query_autopilot():
    ap = _read('apex-autopilot.json', {})
    paused = os.path.exists(os.path.join(LOGS, 'apex-paused.flag'))
    try:
        with open(os.path.join(LOGS, 'apex-autopilot.json')) as f:
            raw = json.load(f)
        log = raw.get('log', [])[-5:]
    except Exception:
        log = []
    return {
        'enabled':          ap.get('enabled'),
        'paused':           paused,
        'trades_today':     ap.get('trades_today'),
        'max_trades_per_day': ap.get('max_trades_per_day'),
        'daily_loss_today': ap.get('daily_loss_today'),
        'max_daily_loss':   ap.get('max_daily_loss'),
        'total_autonomous_trades': ap.get('total_autonomous_trades'),
        'activated_at':     ap.get('activated_at'),
        'recent_log':       log,
        'age_mins':         _age_mins('apex-autopilot.json'),
    }


def query_performance():
    sharpe   = _read('apex-sharpe.json', {})
    drawdown = _read('apex-drawdown.json', {})
    bench    = _read('apex-benchmark.json', {})
    outcomes = _read('apex-outcomes.json', {})
    trades   = outcomes.get('trades', [])
    closed   = [t for t in trades if t.get('pnl') is not None]
    wins     = [t for t in closed if float(t.get('pnl', 0)) > 0]
    return {
        'sharpe_ratio':    sharpe.get('sharpe_ratio'),
        'sortino_ratio':   sharpe.get('sortino_ratio'),
        'trades_analysed': sharpe.get('trades_analysed'),
        'win_rate':        round(len(wins) / len(closed), 3) if closed else None,
        'closed_trades':   len(closed),
        'drawdown_pct':    drawdown.get('drawdown_pct'),
        'benchmark_vs_spy': bench.get('vs_spy'),
        'total_pnl':       sum(float(t.get('pnl', 0)) for t in closed),
        'age_mins':        _age_mins('apex-sharpe.json'),
    }


def query_learning():
    weights  = _read('apex-learned-weights.json', {})
    edge     = _read('apex-edge-proof.json', {})
    traj     = _read('apex-trajectory-insights.json', {})
    kelly    = _read('apex-kelly-v2.json', {})
    return {
        'learned_weights': {
            k: round(v, 3) for k, v in weights.items()
            if isinstance(v, (int, float))
        } if isinstance(weights, dict) else weights,
        'edge_proof': {
            sig: {
                'verdict': e.get('verdict'),
                'win_rate': e.get('win_rate'),
                'n': e.get('n'),
                'p_value': e.get('p_value'),
            }
            for sig, e in edge.items()
            if isinstance(e, dict)
        } if isinstance(edge, dict) else {},
        'trajectory': {
            sig: {
                'avg_days': t.get('avg_days'),
                'early_cut': t.get('early_cut', {}).get('recommended'),
            }
            for sig, t in traj.items()
            if isinstance(t, dict)
        } if isinstance(traj, dict) else {},
        'kelly_fraction': kelly.get('kelly_fraction'),
        'age_mins': _age_mins('apex-learned-weights.json'),
    }


def query_schedule():
    try:
        with open(os.path.join(SCRIPTS, 'apex-schedule.json')) as f:
            sched = json.load(f)
    except Exception as e:
        return {'error': str(e)}
    now = datetime.now(timezone.utc)
    upcoming = []
    for entry in sched.get('schedule', []):
        t = entry.get('time_utc', '')
        if ':' in t and ',' not in t and '*' not in t:
            try:
                h, m = map(int, t.split(':'))
                from datetime import timedelta
                candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate < now:
                    candidate += timedelta(days=1)
                mins = round((candidate - now).total_seconds() / 60)
                if mins < 120:
                    upcoming.append({
                        'name': entry['name'],
                        'time_utc': t,
                        'mins_until': mins,
                        'chain': entry.get('chain'),
                        'category': entry.get('category'),
                    })
            except Exception:
                pass
    return {
        'total_entries': len(sched.get('schedule', [])),
        'upcoming_2h': sorted(upcoming, key=lambda x: x['mins_until']),
    }


QUERIES = {
    'positions':   query_positions,
    'regime':      query_regime,
    'signals':     query_signals,
    'health':      query_health,
    'queue':       query_queue,
    'autopilot':   query_autopilot,
    'performance': query_performance,
    'learning':    query_learning,
    'schedule':    query_schedule,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print('Usage: apex-query.py <source>')
        print('Sources:', ' | '.join(list(QUERIES.keys()) + ['all']))
        return 1

    source = sys.argv[1].lower()

    if source == 'all':
        result = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **{k: fn() for k, fn in QUERIES.items()}
        }
    elif source in QUERIES:
        result = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'source': source,
            **QUERIES[source]()
        }
    else:
        result = {'error': f'Unknown source: {source}', 'available': list(QUERIES.keys())}
        print(json.dumps(result, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
