#!/usr/bin/env python3
"""
Apex Score Adapter — Outcomes → Scoring Feedback Loop

Reads apex-outcomes.json and calculates performance-based score
adjustments from real trade history. The decision engine calls
get_learned_adjustment() to apply these weights to live signals.

How it works:
  - Calculates expectancy (edge) per signal_type, sector, RSI bucket
  - Normalises so "average" performance = 0 adjustment
  - Outperforming categories get +0.5 to +2.0 score boost
  - Underperforming categories get -0.5 to -2.0 score reduction
  - Requires MIN_TRADES_PER_CATEGORY before any adjustment fires
  - Caps total learned adjustment at ±2 to avoid dominating other layers

Runs: after apex-eod-review.sh daily, and on demand.
Saved to: apex-scoring-weights.json
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, log_info
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m):   print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m):    print(f'INFO: {m}')

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
WEIGHTS_FILE  = '/home/ubuntu/.picoclaw/logs/apex-scoring-weights.json'

# Minimum trades in a category before bucketed adjustments activate.
# 15 trades gives a statistically meaningful sample before weights influence scoring.
# With 4 it was too easy for 1-2 lucky/unlucky trades to skew the learned weights.
MIN_TRADES_PER_CATEGORY = 15

# Tier 1: global learning fires much earlier (5 total trades).
# Applies a single directional adjustment to ALL signals until bucketed data is ready.
MIN_TRADES_GLOBAL = 5

# Backtest priors fade to zero as real trades accumulate (fully faded at 30 trades).
PRIOR_FADE_TRADES = 30

BACKTEST_FILE = '/home/ubuntu/.picoclaw/logs/apex-backtest-insights.json'

# Maximum learned adjustment in either direction
MAX_ADJUSTMENT = 2.0


def _expectancy(trades):
    """
    Calculate trading expectancy for a list of trade dicts.
    Expectancy = (win_rate * avg_win_r) - (loss_rate * avg_loss_r)
    Returns (expectancy, win_rate, sample_size)
    """
    if not trades:
        return 0.0, 0.0, 0

    winners = [t for t in trades if t.get('pnl', 0) > 0]
    losers  = [t for t in trades if t.get('pnl', 0) <= 0]
    n       = len(trades)

    win_rate  = len(winners) / n if n else 0
    loss_rate = 1 - win_rate

    avg_win_r = (sum(t.get('r_achieved', 0) for t in winners) / len(winners)
                 if winners else 0)
    avg_los_r = abs(sum(t.get('r_achieved', 0) for t in losers) / len(losers)
                    if losers else 1.0)

    expectancy = (win_rate * avg_win_r) - (loss_rate * avg_los_r)
    return round(expectancy, 4), round(win_rate, 4), n


def _expectancy_to_adjustment(expectancy, baseline_expectancy):
    """
    Convert expectancy vs baseline into a score adjustment.
    delta > 0 → positive edge above average → boost score
    delta < 0 → negative edge below average → reduce score

    Mapping (empirically reasonable):
      delta ≥ +0.5  → +2
      delta ≥ +0.25 → +1
      delta ≥ +0.10 → +0.5
      |delta| < 0.10 → 0
      delta ≤ -0.10 → -0.5
      delta ≤ -0.25 → -1
      delta ≤ -0.50 → -2
    """
    delta = expectancy - baseline_expectancy
    if   delta >=  0.50: return  2.0
    elif delta >=  0.25: return  1.0
    elif delta >=  0.10: return  0.5
    elif delta <= -0.50: return -2.0
    elif delta <= -0.25: return -1.0
    elif delta <= -0.10: return -0.5
    else:                return  0.0


def build_weights(trades):
    """
    Compute score adjustment weights from a list of closed trades.
    Returns a dict of category → adjustment value.

    Tier 1 (MIN_TRADES_GLOBAL=5): global_adjustment applies to ALL signals.
    Tier 2 (MIN_TRADES_PER_CATEGORY=15): per-signal-type/sector/RSI/day buckets.
    """
    n = len(trades)
    if n < MIN_TRADES_GLOBAL:
        return {}

    # Overall baseline
    baseline_exp, baseline_wr, _ = _expectancy(trades)

    weights = {
        '_meta': {
            'total_trades':        n,
            'baseline_expectancy': baseline_exp,
            'baseline_win_rate':   baseline_wr,
            'generated':           datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        },
        # ── Regime-locked score thresholds ────────────────────────────────
        # Decision engine reads these dynamically instead of the hardcoded 6.0
        # from apex_config.py.  Tighter gates in adverse regimes prevent
        # marginal signals from qualifying when the environment is hostile.
        'min_score_by_regime': {
            'FAVOURABLE': 6.0,   # VIX < 15, breadth > 70% — normal gate
            'NEUTRAL':    6.0,   # default
            'CAUTIOUS':   7.0,   # VIX 20–28 or breadth 35–50% — raise bar
            'HOSTILE':    8.0,   # VIX 28–35 or breadth 20–35% — only high conviction
            'BLOCKED':    99.0,  # VIX ≥ 35 or breadth ≤ 20% — no TREND entries
        },
    }

    # ── Tier 1: global adjustment (5+ trades) ──────────────────────────
    # A single directional signal applied to all signals when real edge is
    # emerging but bucket data is insufficient.
    global_adj = _expectancy_to_adjustment(baseline_exp, 0.0)
    weights['global_adjustment'] = {
        'adjustment':  global_adj,
        'expectancy':  baseline_exp,
        'win_rate':    baseline_wr,
        'trades':      n,
        'tier':        'global',
        'active':      True,
    }

    if n < MIN_TRADES_PER_CATEGORY:
        # Tier 2 not yet ready — return with just global + meta
        weights['_meta']['status'] = 'GLOBAL_ACTIVE'
        return weights

    # ── By signal family (per-family Bayesian priors, fades from backtest) ─
    # Each family has its own expectancy prior seeded from OOS backtest results
    # and the hardcoded Beta priors in apex-expected-value.py.  Blends with live
    # data using the same PRIOR_FADE_TRADES=30 mechanism as the backtest prior.
    # When any family has ≥1 live trade, the global_adjustment is deactivated —
    # the family-level priors are more informative and correctly signal-specific.
    FAMILY_BACKTEST_PRIORS = {
        # TREND: OOS aggregate from apex-backtest-v2-results.json (n=1107 trades)
        'TREND':            {'expectancy': 0.635, 'win_rate': 0.388},
        # Others: Beta priors from get_win_rate_by_type() in apex-expected-value.py
        'CONTRARIAN':       {'expectancy': 0.450, 'win_rate': 0.556},
        'INVERSE':          {'expectancy': 0.145, 'win_rate': 0.500},
        'EARNINGS_DRIFT':   {'expectancy': 0.350, 'win_rate': 0.571},
        'DIVIDEND_CAPTURE': {'expectancy': 0.350, 'win_rate': 0.571},
    }

    weights['by_signal_family'] = {}
    any_live_family_trades = False

    for family, prior in FAMILY_BACKTEST_PRIORS.items():
        family_bucket = [
            t for t in trades
            if t.get('signal_type', t.get('result', '')).upper() == family
            or family in str(t.get('outcome_type', '')).upper()
        ]
        n_live = len(family_bucket)
        if n_live > 0:
            any_live_family_trades = True

        prior_weight = round(max(0.0, 1.0 - n_live / PRIOR_FADE_TRADES), 3)

        if n_live >= 1:
            live_exp, live_wr, _ = _expectancy(family_bucket)
            blended_exp = round(prior_weight * prior['expectancy'] + (1 - prior_weight) * live_exp, 4)
            blended_wr  = round(prior_weight * prior['win_rate']   + (1 - prior_weight) * live_wr,  4)
            source = 'blended' if prior_weight > 0 else 'live'
        else:
            blended_exp = prior['expectancy']
            blended_wr  = prior['win_rate']
            source = 'backtest_prior'

        # Compare against 0.0 (break-even), not the live baseline.
        # Family priors validate absolute edge ("is this family profitable?"),
        # not relative performance vs a noisy small-sample live average.
        adj = _expectancy_to_adjustment(blended_exp, 0.0)
        weights['by_signal_family'][family] = {
            'adjustment':   adj,
            'expectancy':   blended_exp,
            'win_rate':     blended_wr,
            'trades':       n_live,
            'prior_weight': prior_weight,
            'source':       source,
            'active':       True,
        }

    # Deactivate global once any family has real trades — family priors supersede it
    if any_live_family_trades and 'global_adjustment' in weights:
        weights['global_adjustment']['active'] = False

    # ── By signal type ─────────────────────────────────────────────────
    weights['by_signal_type'] = {}
    for sig_type in ('TREND', 'CONTRARIAN', 'EARNINGS_DRIFT',
                     'DIVIDEND_CAPTURE', 'INVERSE', 'MANUAL'):
        bucket = [t for t in trades
                  if t.get('signal_type', t.get('result', '')).upper() == sig_type
                  or sig_type in str(t.get('outcome_type', '')).upper()]
        if len(bucket) >= MIN_TRADES_PER_CATEGORY:
            exp, wr, n = _expectancy(bucket)
            adj = _expectancy_to_adjustment(exp, baseline_exp)
            weights['by_signal_type'][sig_type] = {
                'adjustment': adj,
                'expectancy': exp,
                'win_rate':   wr,
                'trades':     n,
            }

    # ── By sector ──────────────────────────────────────────────────────
    weights['by_sector'] = {}
    sectors = set(t.get('sector', 'unknown') for t in trades)
    for sector in sectors:
        bucket = [t for t in trades if t.get('sector', 'unknown') == sector]
        if len(bucket) >= MIN_TRADES_PER_CATEGORY:
            exp, wr, n = _expectancy(bucket)
            adj = _expectancy_to_adjustment(exp, baseline_exp)
            weights['by_sector'][sector] = {
                'adjustment': adj,
                'expectancy': exp,
                'win_rate':   wr,
                'trades':     n,
            }

    # ── By RSI bucket ──────────────────────────────────────────────────
    weights['by_rsi_bucket'] = {}
    rsi_buckets = set(t.get('rsi_bucket', '') for t in trades if t.get('rsi_bucket'))
    for bucket_name in rsi_buckets:
        bucket = [t for t in trades if t.get('rsi_bucket') == bucket_name]
        if len(bucket) >= MIN_TRADES_PER_CATEGORY:
            exp, wr, n = _expectancy(bucket)
            adj = _expectancy_to_adjustment(exp, baseline_exp)
            weights['by_rsi_bucket'][bucket_name] = {
                'adjustment': adj,
                'expectancy': exp,
                'win_rate':   wr,
                'trades':     n,
            }

    # ── By day of week ─────────────────────────────────────────────────
    weights['by_day'] = {}
    days = set(t.get('day_opened', '') for t in trades if t.get('day_opened'))
    for day in days:
        bucket = [t for t in trades if t.get('day_opened') == day]
        if len(bucket) >= MIN_TRADES_PER_CATEGORY:
            exp, wr, n = _expectancy(bucket)
            adj = _expectancy_to_adjustment(exp, baseline_exp)
            weights['by_day'][day] = {
                'adjustment': adj,
                'expectancy': exp,
                'win_rate':   wr,
                'trades':     n,
            }

    return weights


def _backtest_prior_adjustment(signal, num_real_trades):
    """
    Bayesian prior from backtest insights. Fades to zero as real trades accumulate.
    prior_weight = max(0, 1 - num_real_trades / PRIOR_FADE_TRADES)
    Returns (adjustment, reason_str) or (0, None).
    """
    if num_real_trades >= PRIOR_FADE_TRADES:
        return 0, None

    prior_weight = round(1.0 - num_real_trades / PRIOR_FADE_TRADES, 3)
    if prior_weight <= 0:
        return 0, None

    try:
        backtest = safe_read(BACKTEST_FILE, {})
    except Exception:
        return 0, None

    sig_type = signal.get('signal_type', 'TREND').upper()
    ticker   = signal.get('ticker', '')

    # Map signal type to backtest strategy section
    strategy_key = None
    if sig_type == 'CONTRARIAN':
        strategy_key = 'contrarian_strategy'
    elif sig_type in ('TREND', 'EARNINGS_DRIFT', 'GEO_REVERSAL'):
        strategy_key = 'trend_strategy'

    if not strategy_key or strategy_key not in backtest:
        return 0, None

    strat = backtest[strategy_key]
    best_instr  = strat.get('best_instruments', [])
    worst_instr = strat.get('worst_instruments', [])

    raw_adj = 0.0
    if ticker in best_instr:
        raw_adj = +1.0
    elif ticker in worst_instr:
        raw_adj = -1.0

    if raw_adj == 0:
        return 0, None

    adj = round(raw_adj * prior_weight, 2)
    reason = (f"Backtest prior [{ticker}/{sig_type}]: {adj:+.2f} "
              f"(weight={prior_weight:.2f}, fades at {PRIOR_FADE_TRADES} trades)")
    return adj, reason


def get_learned_adjustment(signal):
    """
    Called by the decision engine for every signal.
    Returns (total_adjustment, reasons_list).
    Total adjustment is capped at ±MAX_ADJUSTMENT.

    Usage in decision engine:
        adj, reasons = get_learned_adjustment(signal)
        total_score += adj
    """
    weights = safe_read(WEIGHTS_FILE, {})
    if not weights or '_meta' not in weights:
        return 0, []  # No weights yet — silent pass-through

    total_trades = weights['_meta'].get('total_trades', 0)
    if total_trades < MIN_TRADES_GLOBAL:
        return 0, []

    adjustments = []
    reasons     = []

    # ── Backtest prior (fades over first 30 trades) ─────────────────────
    prior_adj, prior_reason = _backtest_prior_adjustment(signal, total_trades)
    if prior_adj != 0 and prior_reason:
        adjustments.append(prior_adj)
        reasons.append(prior_reason)

    # ── Tier 1a: per-family prior (supersedes global when present) ─────────
    # Uses blended backtest+live expectancy per signal family so that a TREND
    # signal and a CONTRARIAN signal are evaluated against their own baselines,
    # not a single pooled average.  Active as soon as any family has ≥1 trade.
    sig_type    = signal.get('signal_type', 'TREND')
    family_data = weights.get('by_signal_family', {}).get(sig_type.upper())
    use_global  = True

    if family_data and family_data.get('active'):
        family_adj = family_data.get('adjustment', 0)
        if family_adj != 0:
            adjustments.append(family_adj)
            reasons.append(
                f"Family prior [{sig_type}]: {family_adj:+.1f} "
                f"(WR={family_data['win_rate']*100:.0f}%, "
                f"exp={family_data['expectancy']:.3f}, "
                f"src={family_data['source']}, n={family_data['trades']})"
            )
        use_global = False  # family priors supersede global

    # ── Tier 1b: global adjustment (5+ trades, fallback only) ───────────────
    global_data = weights.get('global_adjustment', {})
    global_adj  = global_data.get('adjustment', 0) if global_data.get('active') else 0

    # ── Tier 2: bucketed adjustments (15+ trades per category) ──────────
    bucket_adjustments = []

    st_data  = weights.get('by_signal_type', {}).get(sig_type)
    if st_data:
        adj = st_data['adjustment']
        if adj != 0:
            bucket_adjustments.append(adj)
            reasons.append(
                f"Learned [{sig_type}]: {adj:+.1f} "
                f"({st_data['win_rate']*100:.0f}% WR, {st_data['trades']}T)"
            )

    sector   = signal.get('sector', '')
    sec_data = weights.get('by_sector', {}).get(sector)
    if sec_data and sector:
        adj = sec_data['adjustment']
        if adj != 0:
            bucket_adjustments.append(adj)
            reasons.append(
                f"Learned [{sector}]: {adj:+.1f} "
                f"({sec_data['win_rate']*100:.0f}% WR, {sec_data['trades']}T)"
            )

    rsi      = float(signal.get('rsi', 50))
    bucket   = ('below_35' if rsi < 35 else
                'below_45' if rsi < 45 else
                'mid_45_60' if rsi < 60 else
                'above_60')
    rsi_data = weights.get('by_rsi_bucket', {}).get(bucket)
    if rsi_data:
        adj = rsi_data['adjustment']
        if adj != 0:
            bucket_adjustments.append(adj)
            reasons.append(
                f"Learned [RSI {bucket}]: {adj:+.1f} "
                f"({rsi_data['win_rate']*100:.0f}% WR, {rsi_data['trades']}T)"
            )

    day      = datetime.now(timezone.utc).strftime('%A')
    day_data = weights.get('by_day', {}).get(day)
    if day_data:
        adj = round(day_data['adjustment'] * 0.5, 1)
        if adj != 0:
            bucket_adjustments.append(adj)
            reasons.append(
                f"Learned [{day}]: {adj:+.1f} "
                f"({day_data['win_rate']*100:.0f}% WR, {day_data['trades']}T)"
            )

    if bucket_adjustments:
        # Tier 2 active — bucketed data takes over from family and global
        adjustments.extend(bucket_adjustments)
    elif use_global and global_adj != 0:
        # No family prior and no bucket data — Tier 1b global fallback
        adjustments.append(global_adj)
        reasons.append(
            f"Learned [global]: {global_adj:+.1f} "
            f"(baseline edge {global_data.get('expectancy', 0):+.3f}, "
            f"{global_data.get('trades', 0)}T)"
        )

    # Cap total
    total = round(sum(adjustments), 2)
    total = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, total))

    return total, reasons


def run():
    """Build weights from outcomes and save. Called by EOD cron."""
    now = datetime.now(timezone.utc)
    print(f"\n=== SCORE ADAPTER ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    outcomes = safe_read(OUTCOMES_FILE, {'trades': []})
    trades   = outcomes.get('trades', [])

    print(f"  Trades in outcomes db: {len(trades)}")

    if len(trades) < MIN_TRADES_GLOBAL:
        needed_global   = MIN_TRADES_GLOBAL - len(trades)
        needed_bucketed = MIN_TRADES_PER_CATEGORY - len(trades)
        print(f"  ⏳ Collecting — need {needed_global} more trades before global tier activates "
              f"({needed_bucketed} for bucketed learning)")
        atomic_write(WEIGHTS_FILE, {
            '_meta': {
                'total_trades':        len(trades),
                'baseline_expectancy': 0,
                'baseline_win_rate':   0,
                'status':              'COLLECTING',
                'needed_global':       needed_global,
                'needed_bucketed':     needed_bucketed,
                'generated':           now.strftime('%Y-%m-%d %H:%M UTC'),
            }
        })
        return

    if len(trades) < MIN_TRADES_PER_CATEGORY:
        print(f"  🟡 Global tier ACTIVE ({len(trades)} trades) — "
              f"need {MIN_TRADES_PER_CATEGORY - len(trades)} more for bucketed learning")

    weights = build_weights(trades)
    atomic_write(WEIGHTS_FILE, weights)

    meta = weights['_meta']
    print(f"  Baseline expectancy: {meta['baseline_expectancy']:.4f}")
    print(f"  Baseline win rate:   {meta['baseline_win_rate']*100:.1f}%")

    # Global adjustment summary
    gd = weights.get('global_adjustment', {})
    if gd.get('active'):
        print(f"\n  global_adjustment:   adj={gd['adjustment']:+.1f}  "
              f"wr={gd['win_rate']*100:.0f}%  n={gd['trades']}")
    else:
        print(f"\n  global_adjustment:   INACTIVE (superseded by family priors)")

    # Per-family prior summary
    family_weights = weights.get('by_signal_family', {})
    if family_weights:
        print(f"\n  by_signal_family:")
        for fam, fv in family_weights.items():
            print(f"    {fam:20} adj={fv['adjustment']:+.1f}  "
                  f"wr={fv['win_rate']*100:.0f}%  "
                  f"exp={fv['expectancy']:.3f}  "
                  f"n={fv['trades']}  "
                  f"src={fv['source']}")

    for category, category_data in weights.items():
        if category in ('_meta', 'global_adjustment', 'by_signal_family'):
            continue  # by_signal_family already printed above
        if not isinstance(category_data, dict):
            continue
        active = {k: v for k, v in category_data.items()
                  if isinstance(v, dict) and v.get('adjustment', 0) != 0}
        if active:
            print(f"\n  {category}:")
            for k, v in active.items():
                print(f"    {k:20} adj={v['adjustment']:+.1f}  "
                      f"wr={v['win_rate']*100:.0f}%  n={v['trades']}")

    print(f"\n  ✅ Weights saved to apex-scoring-weights.json")
    return weights


if __name__ == '__main__':
    run()
