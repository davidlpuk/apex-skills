#!/usr/bin/env python3
"""
Parameter Logger
Records every signal with full parameters for future shadow portfolio analysis.
Enables retrospective backtesting of alternative parameters in 8 weeks.

Logs:
- Signal parameters (thresholds, scores, adjustments)
- Market conditions at signal time (VIX, breadth, regime)
- Intelligence layer values (sentiment, RS, MTF, fundamentals)
- Execution outcome (filled, stopped, target hit)
- Counterfactual parameters (what would have happened at different thresholds)
"""
import json
import os
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

PARAM_LOG_FILE  = '/home/ubuntu/.picoclaw/logs/apex-param-log.json'
OUTCOMES_FILE   = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
REGIME_FILE     = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
SENTIMENT_FILE  = '/home/ubuntu/.picoclaw/logs/apex-sentiment.json'
RS_FILE         = '/home/ubuntu/.picoclaw/logs/apex-relative-strength.json'
MTF_FILE        = '/home/ubuntu/.picoclaw/logs/apex-multiframe.json'
DRAWDOWN_FILE   = '/home/ubuntu/.picoclaw/logs/apex-drawdown.json'
FUND_FILE       = '/home/ubuntu/.picoclaw/logs/apex-fundamentals.json'

# Shadow portfolio parameter sets
# These are hypotheses to test against real performance in 8 weeks
SHADOW_CONFIGS = {
    'aggressive': {
        'signal_threshold':    6,    # Lower threshold (vs 7)
        'risk_per_trade':      75,   # Higher risk (vs 50)
        'regime_boost':        0.2,  # Extra regime boost
        'description':         'Lower threshold, higher risk — more trades'
    },
    'conservative': {
        'signal_threshold':    8,    # Higher threshold (vs 7)
        'risk_per_trade':      30,   # Lower risk (vs 50)
        'regime_boost':        0.0,
        'description':         'Higher threshold, lower risk — fewer better trades'
    },
    'trend_only': {
        'signal_threshold':    7,
        'risk_per_trade':      50,
        'allow_contrarian':    False,
        'description':         'Trend signals only — no contrarian'
    },
    'contrarian_only': {
        'signal_threshold':    7,
        'risk_per_trade':      50,
        'allow_trend':         False,
        'description':         'Contrarian signals only — no trend'
    },
    'ignore_regime': {
        'signal_threshold':    7,
        'risk_per_trade':      50,
        'regime_scale_override': 1.0,  # Always full size
        'description':         'Ignore regime scaling — always trade full size'
    },
    'high_confidence_only': {
        'signal_threshold':    7,
        'confidence_threshold': 80,   # Only trade signals with >80% confidence
        'risk_per_trade':      60,
        'description':         'Only high confidence signals (>80%)'
    },
}

def log_signal_params(signal, execution_result=None):
    """
    Log full signal parameters at the moment of execution.
    Called by autopilot after every trade decision.
    """
    now = datetime.now(timezone.utc)

    # Load current market conditions
    regime    = safe_read(REGIME_FILE, {})
    sentiment = safe_read(SENTIMENT_FILE, {})
    drawdown  = safe_read(DRAWDOWN_FILE, {})

    # Get RS for this instrument
    rs_data = safe_read(RS_FILE, {})
    inst_rs = rs_data.get('data', {}).get(signal.get('name',''), {})

    # Get MTF for this instrument
    mtf_data = safe_read(MTF_FILE, {})
    inst_mtf = mtf_data.get('data', {}).get(signal.get('name',''), {})
    weekly_trend = inst_mtf.get('weekly', {}).get('trend_class', 'UNKNOWN') if inst_mtf else 'UNKNOWN'

    # Get fundamentals
    fund_data = safe_read(FUND_FILE, {})
    inst_fund = fund_data.get('data', {}).get(signal.get('name',''), {})

    # Build parameter record
    record = {
        'id':               f"{now.strftime('%Y%m%d_%H%M%S')}_{signal.get('name','?')}",
        'timestamp':        now.isoformat(),
        'date':             now.strftime('%Y-%m-%d'),
        'time':             now.strftime('%H:%M UTC'),

        # Signal identity
        'name':             signal.get('name', '?'),
        'ticker':           signal.get('t212_ticker', '?'),
        'signal_type':      signal.get('signal_type', 'UNKNOWN'),

        # Core signal parameters
        'base_score':       signal.get('score', 0),
        'adjusted_score':   signal.get('adjusted_score', 0),
        'raw_score':        signal.get('raw_score', signal.get('adjusted_score', 0)),
        'confidence_pct':   signal.get('confidence_pct', 0),
        'rsi':              signal.get('rsi', 0),
        'adjustments':      signal.get('adjustments', []),

        # Execution parameters
        'entry':            signal.get('entry', 0),
        'stop':             signal.get('stop', 0),
        'target1':          signal.get('target1', 0),
        'target2':          signal.get('target2', 0),
        'quantity':         signal.get('quantity', 0),
        'risk_amount':      round(
            float(signal.get('quantity', 0)) *
            (float(signal.get('entry', 0)) - float(signal.get('stop', 0))), 2
        ),
        'atr':              signal.get('atr', 0),
        'stop_method':      signal.get('stop_method', 'ATR'),

        # Market conditions at signal time
        'market_conditions': {
            'vix':              regime.get('vix', 0),
            'breadth':          regime.get('breadth', 0),
            'regime_label':     regime.get('regime_label', 'UNKNOWN'),
            'combined_scale':   regime.get('combined_scale', 0),
            'trend_scale':      regime.get('trend_scale', 0),
            'contrarian_scale': regime.get('contrarian_scale', 0),
            'geo_status':       sentiment.get('geo_status', 'CLEAR'),
            'market_sentiment': sentiment.get('market_sentiment', 0),
            'drawdown_pct':     drawdown.get('drawdown_pct', 0),
            'drawdown_status':  drawdown.get('status', 'NORMAL'),
        },

        # Intelligence layer snapshot
        'intelligence': {
            'rs_class':         inst_rs.get('rs_class', 'UNKNOWN'),
            'vs_market_1m':     inst_rs.get('vs_market_1m', 0),
            'weekly_trend':     weekly_trend,
            'fund_score':       inst_fund.get('fund_score', 5) if inst_fund else 5,
            'sentiment_score':  sentiment.get('instrument_scores', {}).get(
                signal.get('name',''), {}).get('sentiment', 0),
        },

        # EV data
        'ev':               signal.get('ev', 0),
        'ev_gross':         signal.get('ev_gross', 0),
        'transaction_cost': signal.get('transaction_cost', 0),
        'breakeven_wr':     signal.get('breakeven_wr', 0),

        # Staged entry
        'staged':           bool(signal.get('staged_entry')),
        'stage':            1 if not signal.get('is_addon') else 2,

        # Counterfactual — would shadow configs have taken this trade?
        'shadow_would_trade': {},

        # Outcome — filled in later when trade closes
        'outcome':          execution_result or 'PENDING',
        'pnl':              None,
        'r_achieved':       None,
        'days_held':        None,
        'exit_reason':      None,
    }

    # Calculate counterfactuals for each shadow config
    for config_name, config in SHADOW_CONFIGS.items():
        would_trade = True
        reasons     = []

        threshold = config.get('signal_threshold', 7)
        if record['adjusted_score'] < threshold:
            would_trade = False
            reasons.append(f"Score {record['adjusted_score']} < threshold {threshold}")

        conf_threshold = config.get('confidence_threshold', 0)
        if conf_threshold and record['confidence_pct'] < conf_threshold:
            would_trade = False
            reasons.append(f"Confidence {record['confidence_pct']}% < {conf_threshold}%")

        if not config.get('allow_contrarian', True) and record['signal_type'] == 'CONTRARIAN':
            would_trade = False
            reasons.append("Contrarian disabled in this config")

        if not config.get('allow_trend', True) and record['signal_type'] == 'TREND':
            would_trade = False
            reasons.append("Trend disabled in this config")

        # Calculate shadow position size
        regime_scale = config.get('regime_scale_override', record['market_conditions']['combined_scale'])
        shadow_risk  = round(config.get('risk_per_trade', 50) * regime_scale, 2)

        record['shadow_would_trade'][config_name] = {
            'would_trade': would_trade,
            'shadow_risk': shadow_risk if would_trade else 0,
            'reasons':     reasons,
        }

    # Append to parameter log
    try:
        param_log = safe_read(PARAM_LOG_FILE, {'signals': [], 'count': 0})
        if not isinstance(param_log, dict):
            param_log = {'signals': [], 'count': 0}

        param_log['signals'].append(record)
        param_log['count']     = len(param_log['signals'])
        param_log['last_updated'] = now.strftime('%Y-%m-%d %H:%M UTC')

        # Keep last 500 signals
        if len(param_log['signals']) > 500:
            param_log['signals'] = param_log['signals'][-500:]

        atomic_write(PARAM_LOG_FILE, param_log)
        log_warning(f"Param logged: {record['name']} | score:{record['adjusted_score']} | raw:{record['raw_score']}")
        return record

    except Exception as e:
        log_error(f"param_logger failed for {signal.get('name','?')}: {e}")
        return None

def update_outcome(signal_id, pnl, r_achieved, days_held, exit_reason):
    """
    Update a logged signal with its outcome when the trade closes.
    Called by log_outcome script.
    """
    try:
        param_log = safe_read(PARAM_LOG_FILE, {'signals': []})
        signals   = param_log.get('signals', [])

        for sig in signals:
            if sig.get('id') == signal_id or (
                sig.get('name') in signal_id and
                sig.get('outcome') == 'PENDING'
            ):
                sig['outcome']    = 'WIN' if pnl > 0 else 'LOSS'
                sig['pnl']        = round(pnl, 2)
                sig['r_achieved'] = round(r_achieved, 2)
                sig['days_held']  = days_held
                sig['exit_reason']= exit_reason
                sig['closed_at']  = datetime.now(timezone.utc).isoformat()
                break

        param_log['signals'] = signals
        atomic_write(PARAM_LOG_FILE, param_log)
        return True

    except Exception as e:
        log_error(f"update_outcome failed: {e}")
        return False

def generate_shadow_report():
    """
    Compare real portfolio vs shadow configs.
    Only meaningful after 20+ trades.
    Run this in 8 weeks.
    """
    param_log = safe_read(PARAM_LOG_FILE, {'signals': []})
    signals   = param_log.get('signals', [])
    closed    = [s for s in signals if s.get('outcome') in ['WIN', 'LOSS']]

    if len(closed) < 20:
        return {
            'status':  'INSUFFICIENT_DATA',
            'message': f"Need 20+ closed trades for meaningful comparison. Currently: {len(closed)}",
            'closed':  len(closed),
        }

    # Real portfolio stats
    real_wins  = sum(1 for s in closed if s.get('outcome') == 'WIN')
    real_wr    = round(real_wins / len(closed) * 100, 1)
    real_pnl   = round(sum(s.get('pnl', 0) for s in closed), 2)
    real_avg_r = round(sum(s.get('r_achieved', 0) for s in closed) / len(closed), 2)

    # Shadow portfolio stats
    shadow_results = {}
    for config_name, config in SHADOW_CONFIGS.items():
        shadow_trades = [
            s for s in closed
            if s.get('shadow_would_trade', {}).get(config_name, {}).get('would_trade', False)
        ]

        if not shadow_trades:
            shadow_results[config_name] = {'trades': 0, 'note': 'No trades would have been taken'}
            continue

        s_wins  = sum(1 for s in shadow_trades if s.get('outcome') == 'WIN')
        s_wr    = round(s_wins / len(shadow_trades) * 100, 1)
        s_pnl   = round(sum(s.get('pnl', 0) for s in shadow_trades), 2)
        s_avg_r = round(sum(s.get('r_achieved', 0) for s in shadow_trades) / len(shadow_trades), 2)

        # Risk-adjusted comparison
        avg_shadow_risk = round(
            sum(s.get('shadow_would_trade', {}).get(config_name, {}).get('shadow_risk', 0)
                for s in shadow_trades) / len(shadow_trades), 2
        )

        shadow_results[config_name] = {
            'trades':       len(shadow_trades),
            'win_rate':     s_wr,
            'total_pnl':    s_pnl,
            'avg_r':        s_avg_r,
            'avg_risk':     avg_shadow_risk,
            'description':  config.get('description', ''),
            'vs_real_wr':   round(s_wr - real_wr, 1),
            'vs_real_pnl':  round(s_pnl - real_pnl, 2),
            'pivot_signal': s_wr > real_wr + 5 and len(shadow_trades) >= 15,
        }

    # Find best shadow config
    best_shadow = max(
        [(k, v) for k, v in shadow_results.items() if v.get('trades', 0) >= 15],
        key=lambda x: x[1].get('win_rate', 0),
        default=(None, {})
    )

    report = {
        'timestamp':     datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'status':        'READY',
        'closed_trades': len(closed),
        'real': {
            'trades':   len(closed),
            'win_rate': real_wr,
            'total_pnl':real_pnl,
            'avg_r':    real_avg_r,
        },
        'shadows':       shadow_results,
        'best_shadow':   best_shadow[0],
        'pivot_recommended': best_shadow[1].get('pivot_signal', False),
        'pivot_config':  best_shadow[1] if best_shadow[1].get('pivot_signal') else None,
    }

    print(f"\n=== SHADOW PORTFOLIO REPORT ===")
    print(f"Closed trades: {len(closed)}")
    print(f"\nReal portfolio:")
    print(f"  Win rate: {real_wr}% | P&L: £{real_pnl} | Avg R: {real_avg_r}")
    print(f"\nShadow portfolios:")
    for name, data in sorted(shadow_results.items(),
                              key=lambda x: x[1].get('win_rate', 0), reverse=True):
        if data.get('trades', 0) == 0:
            continue
        pivot_flag = "⚡ PIVOT SIGNAL" if data.get('pivot_signal') else ""
        wr_diff    = data.get('vs_real_wr', 0)
        icon       = "✅" if wr_diff > 0 else ("🔴" if wr_diff < -2 else "🟡")
        print(f"  {icon} {name:20} | WR: {data.get('win_rate',0)}% ({wr_diff:+.1f}%) | "
              f"P&L: £{data.get('total_pnl',0)} | Trades: {data.get('trades',0)} {pivot_flag}")

    if report['pivot_recommended']:
        print(f"\n⚡ STRATEGY PIVOT RECOMMENDED: {best_shadow[0]}")
        print(f"   {best_shadow[1].get('description','')}")
        print(f"   Win rate {best_shadow[1].get('win_rate',0)}% vs real {real_wr}%")

    atomic_write('/home/ubuntu/.picoclaw/logs/apex-shadow-report.json', report)
    return report

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'report':
        generate_shadow_report()
    else:
        # Test with a sample signal
        test_signal = {
            'name':          'XOM',
            't212_ticker':   'XOM_US_EQ',
            'signal_type':   'CONTRARIAN',
            'score':         5,
            'adjusted_score':10.0,
            'raw_score':     11.0,
            'confidence_pct':73.3,
            'rsi':           61.9,
            'entry':         159.67,
            'stop':          153.94,
            'target1':       171.02,
            'target2':       179.03,
            'quantity':      2.51,
            'atr':           3.8198,
            'adjustments':   ['Geo: +2', 'RS: +2', 'MTF: +2'],
            'ev':            5.27,
            'currency':      'USD',
        }
        record = log_signal_params(test_signal, 'EXECUTED')
        if record:
            print(f"✅ Parameter logged: {record['id']}")
            print(f"   Score: {record['adjusted_score']}/10 (raw:{record['raw_score']})")
            print(f"   Confidence: {record['confidence_pct']}%")
            print(f"\n   Shadow config counterfactuals:")
            for cfg, data in record['shadow_would_trade'].items():
                icon = "✅" if data['would_trade'] else "❌"
                print(f"   {icon} {cfg:20} | would_trade:{data['would_trade']} | risk:£{data['shadow_risk']}")
