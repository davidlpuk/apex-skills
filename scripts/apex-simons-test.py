#!/usr/bin/env python3
"""
Pillar 1: Signal-to-Noise Audit (The Simons Test)
Measures statistical significance of signals across market regimes.

NOW: Collects regime-conditional performance data from every trade.
ACTIVATES: When 20+ trades per regime type accumulated.

Answers:
- Is this signal type profitable in THIS specific regime?
- Is the pattern persistent across regimes or just noise?
- What is the regime-conditional win rate vs overall win rate?
"""
import json
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

SIMONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-simons-test.json'
OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
PARAM_FILE    = '/home/ubuntu/.picoclaw/logs/apex-param-log.json'

# Minimum trades per regime before activating filter
MIN_TRADES_FOR_SIGNIFICANCE = 20

# Regime buckets
REGIME_BUCKETS = {
    'FAVOURABLE':  'Bull regime — VIX<20, breadth>60%',
    'NEUTRAL':     'Normal regime — VIX 20-25, breadth 40-60%',
    'CAUTIOUS':    'Defensive regime — VIX 25-30, breadth 25-40%',
    'HOSTILE':     'Bear regime — VIX>30, breadth<25%',
    'BLOCKED':     'Crisis regime — VIX>35 or breadth<15%',
}

# Signal type buckets
SIGNAL_BUCKETS = ['TREND', 'CONTRARIAN', 'INVERSE', 'EARNINGS_DRIFT', 'DIVIDEND_CAPTURE']

def get_regime_label(vix, breadth):
    """Classify current market into regime bucket."""
    if vix > 35 or breadth < 15:
        return 'BLOCKED'
    elif vix > 30 or breadth < 25:
        return 'HOSTILE'
    elif vix > 25 or breadth < 40:
        return 'CAUTIOUS'
    elif vix > 20 or breadth < 60:
        return 'NEUTRAL'
    else:
        return 'FAVOURABLE'

def calculate_noise_score(win_rate, sample_size, expected_win_rate=0.5):
    """
    Noise score 1-10.
    1 = Pure signal (statistically significant outperformance)
    10 = Pure noise (random, no edge)

    Uses simplified z-score logic:
    z = (observed_wr - expected_wr) / sqrt(expected_wr*(1-expected_wr)/n)
    """
    if sample_size < 5:
        return 8, "Insufficient data — defaulting to high noise"

    import math
    std_err = math.sqrt(expected_win_rate * (1 - expected_win_rate) / sample_size)
    if std_err == 0:
        return 5, "Cannot calculate"

    z_score = (win_rate - expected_win_rate) / std_err

    # Convert z-score to noise score (inverted)
    # z > 2.0 = statistically significant = low noise
    # z < 0.5 = not significant = high noise
    if z_score > 2.5:
        noise = 2
        interpretation = f"Strong signal (z={z_score:.2f}) — statistically significant edge"
    elif z_score > 2.0:
        noise = 3
        interpretation = f"Good signal (z={z_score:.2f}) — likely edge"
    elif z_score > 1.5:
        noise = 5
        interpretation = f"Marginal signal (z={z_score:.2f}) — more data needed"
    elif z_score > 0.5:
        noise = 7
        interpretation = f"Weak signal (z={z_score:.2f}) — possible noise"
    else:
        noise = 9
        interpretation = f"No signal (z={z_score:.2f}) — statistical noise"

    return noise, interpretation

def build_conditional_table():
    """Build regime × signal_type performance table from param log."""
    param_log = safe_read(PARAM_FILE, {'signals': []})
    signals   = param_log.get('signals', [])
    closed    = [s for s in signals if s.get('outcome') in ['WIN', 'LOSS']]

    # Build conditional table: regime → signal_type → {wins, total, pnl}
    table = {}
    for regime in REGIME_BUCKETS:
        table[regime] = {}
        for sig_type in SIGNAL_BUCKETS:
            table[regime][sig_type] = {
                'wins': 0, 'total': 0, 'pnl': 0.0,
                'r_sum': 0.0, 'trades': []
            }

    for trade in closed:
        regime    = trade.get('market_conditions', {}).get('regime_label', 'NEUTRAL')
        sig_type  = trade.get('signal_type', 'TREND')
        outcome   = trade.get('outcome', 'LOSS')
        pnl       = float(trade.get('pnl', 0) or 0)
        r_achieved= float(trade.get('r_achieved', 0) or 0)

        if regime not in table:
            table[regime] = {}
        if sig_type not in table[regime]:
            table[regime][sig_type] = {'wins':0,'total':0,'pnl':0.0,'r_sum':0.0,'trades':[]}

        table[regime][sig_type]['total'] += 1
        table[regime][sig_type]['pnl']   += pnl
        table[regime][sig_type]['r_sum'] += r_achieved
        if outcome == 'WIN':
            table[regime][sig_type]['wins'] += 1

    return table, len(closed)

def get_regime_conditional_win_rate(regime, signal_type):
    """
    Get win rate for specific regime + signal type combination.
    Returns (win_rate, sample_size, is_significant, noise_score)
    """
    try:
        simons_data = safe_read(SIMONS_FILE, {})
        table       = simons_data.get('conditional_table', {})
        cell        = table.get(regime, {}).get(signal_type, {})

        total = cell.get('total', 0)
        wins  = cell.get('wins', 0)

        if total < MIN_TRADES_FOR_SIGNIFICANCE:
            return 0.5, total, False, 8  # Default to 50% prior

        win_rate = wins / total
        noise, _ = calculate_noise_score(win_rate, total)
        return win_rate, total, True, noise

    except Exception as e:
        log_error(f"get_regime_conditional_win_rate failed: {e}")
        return 0.5, 0, False, 8

def run():
    """Build and save the Simons test report."""
    now   = datetime.now(timezone.utc)
    table, total_closed = build_conditional_table()

    print(f"\n=== SIMONS TEST — SIGNAL TO NOISE AUDIT ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Total closed trades: {total_closed}")

    # Build summary
    summary     = {}
    any_active  = False
    pivot_flags = []

    for regime, regime_label in REGIME_BUCKETS.items():
        summary[regime] = {}
        for sig_type in SIGNAL_BUCKETS:
            cell  = table.get(regime, {}).get(sig_type, {})
            total = cell.get('total', 0)
            wins  = cell.get('wins', 0)
            pnl   = cell.get('pnl', 0)

            if total == 0:
                summary[regime][sig_type] = {
                    'status': 'NO_DATA',
                    'trades': 0,
                    'win_rate': None,
                    'noise_score': None,
                    'active': False,
                }
                continue

            win_rate      = round(wins / total, 3)
            noise, interp = calculate_noise_score(win_rate, total)
            is_active     = total >= MIN_TRADES_FOR_SIGNIFICANCE

            if is_active:
                any_active = True

            # Flag if regime-conditional WR differs significantly from overall
            if is_active and win_rate < 0.40:
                pivot_flags.append(
                    f"{sig_type} in {regime}: {round(win_rate*100,1)}% WR — "
                    f"UNDERPERFORMING in this regime"
                )
            elif is_active and win_rate > 0.65:
                pivot_flags.append(
                    f"{sig_type} in {regime}: {round(win_rate*100,1)}% WR — "
                    f"STRONG EDGE in this regime"
                )

            summary[regime][sig_type] = {
                'status':     'ACTIVE' if is_active else 'COLLECTING',
                'trades':     total,
                'wins':       wins,
                'win_rate':   win_rate,
                'pnl':        round(pnl, 2),
                'noise_score':noise,
                'noise_interp':interp,
                'active':     is_active,
                'needed':     max(0, MIN_TRADES_FOR_SIGNIFICANCE - total),
            }

    # Display table
    print(f"\n  Regime-conditional performance (need {MIN_TRADES_FOR_SIGNIFICANCE}+ trades to activate):")
    print(f"  {'Regime':12} {'Signal':18} {'Trades':8} {'WR':8} {'Noise':8} {'Status'}")
    print(f"  {'-'*70}")

    for regime in ['FAVOURABLE', 'NEUTRAL', 'CAUTIOUS', 'HOSTILE']:
        for sig_type in ['TREND', 'CONTRARIAN', 'INVERSE']:
            cell = summary.get(regime, {}).get(sig_type, {})
            if cell.get('trades', 0) == 0:
                status = f"Needs {MIN_TRADES_FOR_SIGNIFICANCE} trades"
                print(f"  {regime:12} {sig_type:18} {'0':8} {'—':8} {'—':8} {status}")
            else:
                wr     = f"{round(cell['win_rate']*100,1)}%" if cell.get('win_rate') else '—'
                noise  = str(cell.get('noise_score','—'))
                active = '✅ ACTIVE' if cell.get('active') else f"⏳ {cell.get('needed',0)} more needed"
                print(f"  {regime:12} {sig_type:18} {cell['trades']:8} {wr:8} {noise:8} {active}")

    if pivot_flags:
        print(f"\n  ⚡ REGIME-SPECIFIC FINDINGS:")
        for flag in pivot_flags:
            print(f"    → {flag}")

    if not any_active:
        print(f"\n  ⏳ All cells in COLLECTING mode")
        print(f"  Filter activates trade by trade as data accumulates")
        print(f"  Currently using global backtest win rate (51-56%) as prior")

    output = {
        'timestamp':        now.strftime('%Y-%m-%d %H:%M UTC'),
        'total_closed':     total_closed,
        'any_active':       any_active,
        'conditional_table':summary,
        'pivot_flags':      pivot_flags,
        'min_trades':       MIN_TRADES_FOR_SIGNIFICANCE,
        'status':           'ACTIVE' if any_active else 'COLLECTING',
    }

    atomic_write(SIMONS_FILE, output)
    print(f"\n✅ Simons test saved — status: {output['status']}")
    return output

def audit_signal(signal, current_regime):
    """
    Run Simons audit on a specific signal.
    Returns: (noise_score, regime_wr, is_significant, recommendation)
    """
    sig_type = signal.get('signal_type', 'TREND')
    wr, sample, is_sig, noise = get_regime_conditional_win_rate(current_regime, sig_type)

    if not is_sig:
        return noise, wr, False, f"COLLECTING — {sample}/{MIN_TRADES_FOR_SIGNIFICANCE} trades in {current_regime}"

    if noise <= 3:
        rec = "APPROVED — statistically significant edge in this regime"
    elif noise <= 5:
        rec = "PROCEED — marginal edge, monitor closely"
    elif noise <= 7:
        rec = "CAUTION — weak signal in this regime, reduce size"
    else:
        rec = "ABORT — no statistical edge in this regime"

    return noise, wr, is_sig, rec

if __name__ == '__main__':
    run()
