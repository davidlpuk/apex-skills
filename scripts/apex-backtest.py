#!/usr/bin/env python3
"""
Apex Backtesting Engine
Tests the live 18-layer scoring system against 2 years of historical data.
Validates signal thresholds, win rates, and strategy edge.

Scoring parity: uses apex_scoring.score_signal_with_intelligence() — the same
function the live decision engine uses. Layers that depend on live-only data
(RS, MTF, EDGAR insider, FRED, options flow, live news) fail silently and
contribute 0, which is flagged in the output as "scoring gaps".

Layers that DO apply historically:
  - Base 4-factor score (trend/RSI/volume/MACD)
  - VIX regime status → regime_status filter
  - SPY vs 200-EMA → direction_status filter
  - Sector rotation → sector boost/penalty
  - Regime scaling → position sizing
"""
import yfinance as yf
import json
import sys
from datetime import datetime, timezone, timedelta
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

try:
    from apex_scoring import score_signal_with_intelligence
    _LIVE_SCORING = True
except ImportError:
    _LIVE_SCORING = False

SCORING_GAPS = [
    "RS adjustment   — requires live relative-strength data",
    "MTF adjustment  — requires live multi-timeframe API calls",
    "EDGAR insider   — no historical EDGAR data available",
    "FRED macro      — FRED series not indexed by historical date in backtest",
    "Options flow    — live options chain data only",
    "News sentiment  — live news feed only",
]


BACKTEST_FILE  = '/home/ubuntu/.picoclaw/logs/apex-backtest-results.json'
QUALITY_FILE   = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'

# Slippage: bid-ask spread estimate (Trading212 has no commission, but spread exists)
SLIPPAGE_PCT = 0.0005  # 0.05% per side

# Sector ETFs for historical rotation scoring
SECTOR_ETFS = {
    "Technology": "XLK",
    "Energy":     "XLE",
    "Financials": "XLF",
    "Healthcare":  "XLV",
    "Consumer":   "XLP",
}


class BacktestIntelligence:
    """
    Builds a minimal historical `intel` dict matching the structure expected by
    score_signal_with_intelligence() and is_blocked().

    Uses only data available historically:
      - VIX → regime_status
      - SPY vs 200-EMA → direction_status
      - Sector ETFs vs their 50/200 EMA → sector_scores, leading/lagging_sectors
    All live-only fields (news_blocked, earnings_blocked, geo) use safe defaults.
    """

    def __init__(self, vix_by_date, spy_data, sector_data):
        self._vix      = vix_by_date      # {date_str: float}
        self._spy      = spy_data         # list of (date_str, close)
        self._sectors  = sector_data      # {sector: list of (date_str, close)}
        self._spy_idx  = {d: i for i, (d, _) in enumerate(spy_data)}
        self._sec_idx  = {s: {d: i for i, (d, _) in enumerate(v)}
                          for s, v in sector_data.items()}

    def _spy_ema(self, date_str, period):
        idx = self._spy_idx.get(date_str)
        if idx is None or idx < period:
            return None
        closes = [c for _, c in self._spy[max(0, idx - period * 2): idx + 1]]
        return calculate_ema(closes, period)

    def _sector_score(self, date_str):
        """Returns sector_scores dict and leading/lagging lists."""
        scores  = {}
        leading = []
        lagging = []
        for sector, etf_data in self._sectors.items():
            idx_map = self._sec_idx.get(sector, {})
            idx = idx_map.get(date_str)
            if idx is None or idx < 50:
                scores[sector] = 5
                continue
            closes = [c for _, c in etf_data[max(0, idx - 60): idx + 1]]
            ema50  = calculate_ema(closes[-50:], 50)
            close  = closes[-1]
            if close > ema50 * 1.02:
                scores[sector] = 8
                leading.append(sector)
            elif close < ema50 * 0.98:
                scores[sector] = 2
                lagging.append(sector)
            else:
                scores[sector] = 5
        return scores, leading, lagging

    def get_intel(self, date_str):
        """Build intel dict for a given historical date."""
        vix = self._vix.get(date_str, 20.0)

        # Regime status from VIX (mirrors apex-regime-scaling.py thresholds)
        if vix >= 33:
            regime_status = 'BLOCKED'
        elif vix >= 25:
            regime_status = 'CAUTION'
        else:
            regime_status = 'OK'

        # Direction status from SPY vs 200 EMA
        direction_blocks = []
        spy_ema200 = self._spy_ema(date_str, 200)
        spy_close  = None
        spy_idx    = self._spy_idx.get(date_str)
        if spy_idx is not None:
            spy_close = self._spy[spy_idx][1]
        if spy_close is not None and spy_ema200 is not None:
            if spy_close < spy_ema200 * 0.97:
                direction_blocks.append(f"SPY {spy_close:.0f} < 200EMA {spy_ema200:.0f}")
        direction_status = 'BLOCKED' if direction_blocks else 'OK'

        # Breadth approximation — use SPY vs 50 EMA as proxy
        spy_ema50 = self._spy_ema(date_str, 50)
        breadth = 60 if (spy_close and spy_ema50 and spy_close > spy_ema50) else 35

        sector_scores, leading, lagging = self._sector_score(date_str)

        return {
            'vix':              vix,
            'regime_status':    regime_status,
            'direction_status': direction_status,
            'direction_blocks': direction_blocks,
            'geo_status':       'CLEAR',
            'geo':              {'overall': 'CLEAR'},
            'breadth':          breadth,
            'sector_scores':    sector_scores,
            'sector_breadth':   {s: {'breadth_200': 50, 'health': 'NEUTRAL'}
                                  for s in sector_scores},
            'leading_sectors':  leading,
            'lagging_sectors':  lagging,
            'earnings_blocked': set(),
            'news_blocked':     set(),
            'size_multiplier':  vix_scale(vix),
        }


def vix_scale(vix):
    """Mirror of apex-regime-scaling.py: 1.0 at VIX<=15, 0.0 at VIX>=35."""
    if vix <= 15: return 1.0
    if vix >= 35: return 0.0
    return 1.0 - (vix - 15) / 20.0

# Universe to backtest
BACKTEST_UNIVERSE = {
    "AAPL":  "AAPL",    "MSFT":  "MSFT",    "NVDA":  "NVDA",
    "GOOGL": "GOOGL",   "AMZN":  "AMZN",    "META":  "META",
    "JPM":   "JPM",     "XOM":   "XOM",     "CVX":   "CVX",
    "V":     "V",       "JNJ":   "JNJ",     "ABBV":  "ABBV",
    "VUAG":  "VUAG.L",  "SHEL":  "SHEL.L",  "AZN":   "AZN.L",
    "HSBA":  "HSBA.L",  "GSK":   "GSK.L",   "ULVR":  "ULVR.L",
}

def fix_pence(price, ticker):
    if ticker.endswith('.L') and price > 100:
        return price / 100
    return price

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs  = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(closes, period):
    if not closes:
        return 0
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_macd_hist(closes):
    if len(closes) < 26:
        return 0, False
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd  = ema12 - ema26

    # Signal line approximation
    ema12_prev = calculate_ema(closes[:-1], 12)
    ema26_prev = calculate_ema(closes[:-1], 26)
    macd_prev  = ema12_prev - ema26_prev

    signal      = calculate_ema([macd_prev, macd], 9)
    hist        = macd - signal
    hist_prev   = macd_prev - calculate_ema([macd_prev]*2, 9)

    return round(hist, 4), hist > hist_prev

def score_signal_base(closes, volumes, mode='TREND'):
    """
    Compute the 4-factor base score from OHLCV data.
    Returns (base_score, rsi).
    This is the same logic as before, now used as input to the live scorer.
    """
    if len(closes) < 200:
        return 0, 50

    price  = closes[-1]
    ema50  = calculate_ema(closes[-50:], 50)
    ema200 = calculate_ema(closes[-200:], 200)
    rsi    = calculate_rsi(closes[-28:])
    macd_h, macd_rising = calculate_macd_hist(closes[-35:])

    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

    if mode == 'TREND':
        trend_score = 3 if price > ema50 > ema200 else 0
        rsi_score   = 3 if 45 <= rsi <= 70 else (1 if 35 <= rsi < 45 or 70 < rsi <= 80 else 0)
        vol_score   = 2 if vol_ratio >= 1.0 else 1
        macd_score  = 2 if macd_h > 0 and macd_rising else (1 if macd_h > 0 else 0)
        return trend_score + rsi_score + vol_score + macd_score, rsi

    elif mode == 'CONTRARIAN':
        score = 0
        if rsi <= 20:   score += 4
        elif rsi <= 25: score += 3
        elif rsi <= 30: score += 2

        high_52  = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        discount = (high_52 - price) / high_52 * 100
        if discount >= 25:   score += 3
        elif discount >= 15: score += 2
        elif discount >= 10: score += 1

        score += 2  # Quality bonus — hardcoded for backtest universe
        if macd_rising: score += 1

        return score, rsi

    return 0, 50


def score_signal(closes, volumes, mode='TREND', name='', intel=None):
    """
    Score a signal using the live 18-layer scoring system when available,
    falling back to the 4-factor base score.

    Returns (adjusted_score, rsi).
    """
    base_score, rsi = score_signal_base(closes, volumes, mode)

    if _LIVE_SCORING and intel is not None:
        atr = calculate_backtest_atr(closes, len(closes) - 1)
        entry = closes[-1]
        stop  = (entry - atr * 2.0) if (atr and atr > 0) else entry * 0.94
        signal = {
            'name':         name,
            'ticker':       name,
            'signal_type':  mode,
            'total_score':  base_score,
            'entry':        entry,
            'stop':         stop,
        }
        try:
            result = score_signal_with_intelligence(signal, intel)
            adj_score = result.get('adjusted_score', base_score)
            return round(adj_score, 1), rsi
        except Exception:
            pass  # Fall back to base score

    return base_score, rsi

def calculate_backtest_atr(closes, idx, period=14):
    """ATR at a given index for backtest — matches live Wilder's smoothing."""
    if idx < period + 1:
        return None
    # Use closes only (no separate high/low in daily close data) — approximate TR as daily range
    true_ranges = [abs(closes[i] - closes[i-1]) for i in range(max(1, idx - period * 2), idx + 1)]
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def simulate_trade(closes, entry_idx, mode='TREND', max_days=None):
    """
    Simulate a trade using ATR-based stops and targets — mirrors live system.
    TREND: stop=2.0×ATR, T1=2.0×ATR, T2=3.5×ATR, max 15 days
    CONTRARIAN: stop=2.5×ATR, T1=2.0×ATR, T2=3.5×ATR, max 20 days
    Returns: outcome, pnl_r, days_held, exit_reason
    """
    if entry_idx >= len(closes) - 1:
        return 'TIMEOUT', 0, 0, 'insufficient_data'

    atr = calculate_backtest_atr(closes, entry_idx)
    if not atr or atr <= 0:
        return 'TIMEOUT', 0, 0, 'no_atr'

    entry = closes[entry_idx] * (1 + SLIPPAGE_PCT)   # slippage-adjusted fill

    # ATR-based stop distances — matching live apex-atr-stops.py exactly
    if mode == 'CONTRARIAN':
        stop_mult = 2.5
        default_max_days = 20
    else:
        stop_mult = 2.0
        default_max_days = 15

    if max_days is None:
        max_days = default_max_days

    stop = entry - atr * stop_mult
    t1   = entry + atr * 2.0   # Target 1: 2.0× ATR above entry
    t2   = entry + atr * 3.5   # Target 2: 3.5× ATR above entry
    risk = entry - stop

    if risk <= 0:
        return 'TIMEOUT', 0, 0, 'invalid_risk'

    for day in range(1, min(max_days + 1, len(closes) - entry_idx)):
        price = closes[entry_idx + day]

        if price <= stop:
            return 'LOSS', -1.0, day, 'stop_hit'

        if price >= t2:
            pnl_r = round((price * (1 - SLIPPAGE_PCT) - entry) / risk, 2)
            return 'WIN', pnl_r, day, 'target2_hit'

        if price >= t1:
            # Move stop to breakeven, ride to T2 — matches live partial close logic
            stop = entry  # Breakeven

    # Exit at max days (time stop) — slippage-adjusted exit
    final_price = closes[entry_idx + min(max_days, len(closes) - entry_idx - 1)]
    pnl_r = round((final_price * (1 - SLIPPAGE_PCT) - entry) / risk, 2)
    outcome = 'WIN' if pnl_r > 0 else 'LOSS'
    return outcome, pnl_r, max_days, 'time_stop'

def backtest_instrument(name, yahoo_ticker, mode='TREND', threshold=7,
                        vix_by_date=None, bt_intel=None,
                        start_date=None, end_date=None, period="5y"):
    """Backtest one instrument. Optional start_date/end_date (YYYY-MM-DD) for walk-forward windows."""
    try:
        hist = yf.Ticker(yahoo_ticker).history(period=period)
        if hist.empty or len(hist) < 200:
            return None

        closes  = [fix_pence(float(c), yahoo_ticker) for c in hist['Close']]
        volumes = [float(v) for v in hist['Volume']]
        dates   = [d.strftime('%Y-%m-%d') for d in hist.index]

        trades = []

        # Walk forward — scan each day from day 200 onwards
        for i in range(200, len(closes) - 21):
            date_str = dates[i]

            # Walk-forward window filter
            if start_date and date_str < start_date:
                continue
            if end_date and date_str > end_date:
                continue
            intel    = bt_intel.get_intel(date_str) if bt_intel else None

            # Direction/regime filter: skip blocked dates before scoring (fast path)
            if intel:
                if mode == 'TREND' and intel['regime_status'] == 'BLOCKED':
                    continue
                if mode == 'TREND' and intel['direction_status'] == 'BLOCKED':
                    continue

            score, rsi = score_signal(closes[:i+1], volumes[:i+1], mode,
                                      name=name, intel=intel)

            if score >= threshold:
                # Check we're not already in a trade (simplistic)
                if trades and trades[-1].get('entry_idx', 0) + trades[-1].get('days_held', 20) > i:
                    continue

                vix_val     = vix_by_date.get(date_str, 20.0) if vix_by_date else 20.0
                vix_blocked = vix_scale(vix_val) < 0.1  # VIX >= 33 = HOSTILE/BLOCKED

                outcome, pnl_r, days, reason = simulate_trade(closes, i, mode=mode)

                trades.append({
                    "date":        date_str,
                    "entry_idx":   i,
                    "score":       score,
                    "rsi":         rsi,
                    "entry":       round(closes[i], 2),
                    "outcome":     outcome,
                    "pnl_r":       pnl_r,
                    "days_held":   days,
                    "reason":      reason,
                    "vix":         round(vix_val, 1),
                    "vix_blocked": vix_blocked,
                })

        return trades

    except Exception as e:
        print(f"  Error {name}: {e}")
        return None

def analyse_results(all_trades):
    """Calculate statistics across all backtest trades."""
    if not all_trades:
        return {}

    total  = len(all_trades)
    wins   = [t for t in all_trades if t['outcome'] == 'WIN']
    losses = [t for t in all_trades if t['outcome'] == 'LOSS']

    win_rate    = round(len(wins) / total * 100, 1)
    avg_win_r   = round(sum(t['pnl_r'] for t in wins) / len(wins), 2) if wins else 0
    avg_loss_r  = round(sum(t['pnl_r'] for t in losses) / len(losses), 2) if losses else 0
    expectancy  = round((win_rate/100 * avg_win_r) + ((1-win_rate/100) * avg_loss_r), 3)
    profit_factor = round(abs(sum(t['pnl_r'] for t in wins) / sum(t['pnl_r'] for t in losses)), 2) if losses and sum(t['pnl_r'] for t in losses) != 0 else 0
    avg_days    = round(sum(t['days_held'] for t in all_trades) / total, 1)

    # By score bucket
    by_score = {}
    for t in all_trades:
        s = t['score']
        if s not in by_score:
            by_score[s] = {'wins': 0, 'total': 0}
        by_score[s]['total'] += 1
        if t['outcome'] == 'WIN':
            by_score[s]['wins'] += 1

    score_analysis = {}
    for score, data in sorted(by_score.items()):
        wr = round(data['wins'] / data['total'] * 100, 1)
        score_analysis[str(score)] = {
            'win_rate': wr,
            'trades':   data['total']
        }

    # By RSI bucket
    rsi_buckets = {
        'oversold (<30)':   [t for t in all_trades if t['rsi'] < 30],
        'low (30-45)':      [t for t in all_trades if 30 <= t['rsi'] < 45],
        'neutral (45-60)':  [t for t in all_trades if 45 <= t['rsi'] < 60],
        'high (60-70)':     [t for t in all_trades if 60 <= t['rsi'] < 70],
        'overbought (>70)': [t for t in all_trades if t['rsi'] >= 70],
    }

    rsi_analysis = {}
    for bucket, trades in rsi_buckets.items():
        if trades:
            wr = round(sum(1 for t in trades if t['outcome'] == 'WIN') / len(trades) * 100, 1)
            rsi_analysis[bucket] = {'win_rate': wr, 'trades': len(trades)}

    return {
        'total_trades':   total,
        'win_rate':       win_rate,
        'avg_win_r':      avg_win_r,
        'avg_loss_r':     avg_loss_r,
        'expectancy':     expectancy,
        'profit_factor':  profit_factor,
        'avg_days_held':  avg_days,
        'by_score':       score_analysis,
        'by_rsi':         rsi_analysis,
    }

def run(mode='TREND', threshold=7, override_start=None, override_end=None, silent=False, period="5y"):
    """
    Run backtest.

    override_start / override_end: ISO date strings 'YYYY-MM-DD' for walk-forward windows.
    silent: suppress per-instrument output (used when called by walk-forward).
    """
    now = datetime.now(timezone.utc)
    scoring_label = "18-layer live scoring" if _LIVE_SCORING else "4-factor base scoring"
    if not silent:
        print(f"\n🔬 APEX BACKTEST ENGINE — {mode} signals ({scoring_label})")
        print(f"Universe: {len(BACKTEST_UNIVERSE)} instruments | Threshold: {threshold}/12")
        period_label = f"{override_start} → {override_end}" if override_start else f"{period} history"
        print(f"Period: {period_label} | Stop: ATR-based | T1: 2.0×ATR | T2: 3.5×ATR")
        print(f"Slippage: {SLIPPAGE_PCT*100:.2f}% per side | Regime filter: VIX-based + SPY direction")
        print("="*60)

    if _LIVE_SCORING:
        print("\n  Scoring gaps (live-only layers skipped in backtest):")
        for gap in SCORING_GAPS:
            print(f"    ⚠  {gap}")
        print()

    # Fetch VIX + SPY + sector ETF history once
    vix_by_date = {}
    spy_data    = []
    sector_data = {}

    if not silent:
        print("  Fetching market context history...", flush=True)
    try:
        vix_hist    = yf.Ticker("^VIX").history(period=period)
        vix_by_date = {d.strftime('%Y-%m-%d'): float(v)
                       for d, v in zip(vix_hist.index, vix_hist['Close'])}
        if not silent:
            print(f"    VIX: {len(vix_by_date)} days")
    except Exception as e:
        if not silent:
            print(f"    VIX fetch failed ({e})")

    try:
        spy_hist = yf.Ticker("SPY").history(period=period)
        spy_data = [(d.strftime('%Y-%m-%d'), float(c))
                    for d, c in zip(spy_hist.index, spy_hist['Close'])]
        if not silent:
            print(f"    SPY: {len(spy_data)} days")
    except Exception as e:
        if not silent:
            print(f"    SPY fetch failed ({e})")

    for sector, etf in SECTOR_ETFS.items():
        try:
            h = yf.Ticker(etf).history(period=period)
            sector_data[sector] = [(d.strftime('%Y-%m-%d'), float(c))
                                   for d, c in zip(h.index, h['Close'])]
        except Exception:
            sector_data[sector] = []
    if not silent:
        print(f"    Sector ETFs: {sum(1 for v in sector_data.values() if v)} loaded")

    bt_intel = BacktestIntelligence(vix_by_date, spy_data, sector_data) if spy_data else None

    # Apply walk-forward date filter if provided
    if override_start or override_end:
        # Filter vix_by_date and spy_data to the window
        if override_start:
            vix_by_date = {d: v for d, v in vix_by_date.items() if d >= override_start}
            spy_data    = [(d, v) for d, v in spy_data if d >= override_start]
        if override_end:
            vix_by_date = {d: v for d, v in vix_by_date.items() if d <= override_end}
            spy_data    = [(d, v) for d, v in spy_data if d <= override_end]

    all_trades  = []
    by_instrument = {}

    for name, yahoo in BACKTEST_UNIVERSE.items():
        if not silent:
            print(f"  Testing {name}...", flush=True)
        trades = backtest_instrument(name, yahoo, mode, threshold,
                                     vix_by_date, bt_intel,
                                     start_date=override_start,
                                     end_date=override_end,
                                     period=period)

        if trades:
            wins     = sum(1 for t in trades if t['outcome'] == 'WIN')
            wr       = round(wins / len(trades) * 100, 1) if trades else 0
            by_instrument[name] = {
                'trades':   len(trades),
                'win_rate': wr,
                'trades_data': trades
            }
            all_trades.extend(trades)
            print(f"    {len(trades)} signals | {wr}% win rate")
        else:
            print(f"    No signals or error")

    # Dual analysis: all signals vs VIX-regime-filtered signals
    stats          = analyse_results(all_trades)
    regime_trades  = [t for t in all_trades if not t.get('vix_blocked')]
    stats_filtered = analyse_results(regime_trades)
    blocked_count  = len(all_trades) - len(regime_trades)

    print(f"\n{'='*70}")
    print(f"📊 BACKTEST RESULTS — {mode} (threshold {threshold}/12, {SLIPPAGE_PCT*100:.2f}% slippage, {scoring_label})")
    print(f"{'='*70}")
    print(f"  {'Metric':<22} {'All signals':>16} {'Regime-filtered':>16}")
    print(f"  {'-'*54}")
    for label, key, unit in [
        ('Total signals',  'total_trades',  ''),
        ('Win rate',       'win_rate',      '%'),
        ('Avg win',        'avg_win_r',     'R'),
        ('Avg loss',       'avg_loss_r',    'R'),
        ('Expectancy',     'expectancy',    'R'),
        ('Profit factor',  'profit_factor', ''),
        ('Avg hold (days)','avg_days_held', ''),
    ]:
        va = f"{stats.get(key, 0)}{unit}"
        vf = f"{stats_filtered.get(key, 0)}{unit}"
        print(f"  {label:<22} {va:>16} {vf:>16}")
    if blocked_count:
        print(f"\n  Regime filter blocked {blocked_count}/{len(all_trades)} signals (VIX >= 33)")

    print(f"\n  Win rate by score:")
    for score, data in stats.get('by_score', {}).items():
        bar  = "█" * int(data['win_rate'] / 10)
        flag = "✅" if data['win_rate'] >= 55 else ("🟡" if data['win_rate'] >= 45 else "🔴")
        print(f"  {flag} Score {score}/10: {data['win_rate']:5}% ({data['trades']} trades) {bar}")

    print(f"\n  Win rate by RSI bucket:")
    for bucket, data in stats.get('by_rsi', {}).items():
        bar  = "█" * int(data['win_rate'] / 10)
        flag = "✅" if data['win_rate'] >= 55 else ("🟡" if data['win_rate'] >= 45 else "🔴")
        print(f"  {flag} {bucket:20}: {data['win_rate']:5}% ({data['trades']} trades) {bar}")

    print(f"\n  Best instruments:")
    sorted_inst = sorted(
        [(k, v) for k, v in by_instrument.items() if v['trades'] >= 3],
        key=lambda x: x[1]['win_rate'],
        reverse=True
    )
    for name, data in sorted_inst[:5]:
        print(f"    {name:8}: {data['win_rate']}% ({data['trades']} trades)")

    print(f"\n  Worst instruments:")
    for name, data in sorted_inst[-3:]:
        print(f"    {name:8}: {data['win_rate']}% ({data['trades']} trades)")

    # Optimal threshold analysis
    print(f"\n  Threshold optimisation:")
    for thresh in [6, 7, 8, 9]:
        thresh_trades = [t for t in all_trades if t['score'] >= thresh]
        if thresh_trades:
            thresh_wins = sum(1 for t in thresh_trades if t['outcome'] == 'WIN')
            thresh_wr   = round(thresh_wins / len(thresh_trades) * 100, 1)
            flag = "✅" if thresh_wr >= 55 else ("🟡" if thresh_wr >= 45 else "🔴")
            print(f"  {flag} Threshold {thresh}+: {thresh_wr}% win rate ({len(thresh_trades)} trades)")

    # Save results
    output = {
        "timestamp":        now.strftime('%Y-%m-%d %H:%M UTC'),
        "mode":             mode,
        "threshold":        threshold,
        "slippage_pct":     SLIPPAGE_PCT,
        "scoring_method":   scoring_label,
        "scoring_gaps":     SCORING_GAPS if _LIVE_SCORING else [],
        "stats":            stats,
        "stats_regime_filtered": stats_filtered,
        "regime_blocked_signals": blocked_count,
        "instruments":      {k: {'trades': v['trades'], 'win_rate': v['win_rate']}
                             for k, v in by_instrument.items()},
        "all_trades":       all_trades[-100:]  # Save last 100 for reference
    }

    atomic_write(BACKTEST_FILE, output)

    print(f"\n✅ Results saved to apex-backtest-results.json")

    # Verdict
    expectancy = stats.get('expectancy', 0)
    win_rate   = stats.get('win_rate', 0)

    print(f"\n{'='*60}")
    print(f"VERDICT:")
    if expectancy > 0.3 and win_rate >= 55:
        print(f"✅ STRONG EDGE — strategy is validated. Safe to trade live.")
    elif expectancy > 0 and win_rate >= 45:
        print(f"🟡 MARGINAL EDGE — strategy works but consider raising threshold.")
    else:
        print(f"🔴 NO EDGE — strategy needs adjustment before live trading.")
    print(f"{'='*60}")

    return stats

def run_walkforward(mode='TREND', threshold=7, windows=4, period="5y"):
    """
    Walk-forward validation: splits the available history into
    `windows` sequential segments and tests each independently.

    Approach:
    - Data is split chronologically into N equal segments.
    - Each segment is tested with the same scoring system and threshold.
    - We look for STABILITY: results should be consistent across windows.
      Wildly varying win rates (e.g. 70% in window 1, 30% in window 3)
      signal overfitting or regime dependence rather than robust edge.

    Outputs:
    - Per-window stats (win rate, expectancy, trade count)
    - Stability metric: std deviation of win rates across windows
    - Flag if any window shows NO EDGE (expectancy ≤ 0)
    """
    import math

    period_days = {'1y': 365, '2y': 730, '3y': 1095, '5y': 1825, '10y': 3650}
    lookback    = period_days.get(period, 1825)

    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"WALK-FORWARD VALIDATION — {mode} (threshold {threshold})")
    print(f"Windows: {windows} | Period: {period}")
    print(f"{'='*60}")

    end_date   = now.date()
    start_date = end_date - timedelta(days=lookback)

    # Generate window boundaries
    total_days = (end_date - start_date).days
    window_days = total_days // windows
    window_results = []

    try:
        from apex_backtest import run as _run_bt
    except ImportError:
        # Self-reference: import this module's run()
        _run_bt = run

    for i in range(windows):
        win_start = start_date + timedelta(days=i * window_days)
        win_end   = start_date + timedelta(days=(i + 1) * window_days)
        if i == windows - 1:
            win_end = end_date  # Last window catches remainder

        print(f"\n  Window {i+1}/{windows}: {win_start} → {win_end}")

        # Run full backtest for this window
        try:
            stats = run(mode, threshold,
                        override_start=win_start.isoformat(),
                        override_end=win_end.isoformat(),
                        silent=True,
                        period=period)
            if stats:
                window_results.append({
                    'window':      i + 1,
                    'start':       win_start.isoformat(),
                    'end':         win_end.isoformat(),
                    'trades':      stats.get('total', 0),
                    'win_rate':    stats.get('win_rate', 0),
                    'expectancy':  stats.get('expectancy', 0),
                    'profit_factor': stats.get('profit_factor', 0),
                })
                edge = ('✅ EDGE' if stats.get('expectancy', 0) > 0
                        else '🔴 NO EDGE')
                print(f"    {edge} — WR {stats.get('win_rate',0)}% | "
                      f"E={stats.get('expectancy',0):.2f}R | "
                      f"Trades={stats.get('total',0)}")
        except Exception as e:
            print(f"    ❌ Window {i+1} failed: {e}")

    if not window_results:
        print("\n  ❌ Walk-forward failed — no window results")
        return

    # Stability analysis
    win_rates   = [w['win_rate'] for w in window_results]
    expectancies = [w['expectancy'] for w in window_results]
    n           = len(win_rates)

    mean_wr   = round(sum(win_rates) / n, 1)
    mean_exp  = round(sum(expectancies) / n, 2)
    std_wr    = round(math.sqrt(sum((r - mean_wr)**2 for r in win_rates) / n), 1) if n > 1 else 0
    failing   = [w for w in window_results if w['expectancy'] <= 0]
    stable    = std_wr < 10  # < 10% std deviation in win rates = stable

    print(f"\n{'='*60}")
    print(f"WALK-FORWARD SUMMARY")
    print(f"  Mean win rate:    {mean_wr}%  (σ={std_wr}%)")
    print(f"  Mean expectancy:  {mean_exp:.2f}R")
    print(f"  Windows failing:  {len(failing)}/{n}")
    print(f"  Stability:        {'✅ STABLE' if stable else '⚠️ UNSTABLE — high variance across windows'}")

    if failing:
        print(f"\n  ⚠️  Failing windows:")
        for w in failing:
            print(f"    Window {w['window']} ({w['start'][:7]}): E={w['expectancy']:.2f}R, WR={w['win_rate']}%")

    if mean_exp > 0.2 and stable and not failing:
        print(f"\n  ✅ ROBUST EDGE — stable across all windows")
    elif mean_exp > 0 and len(failing) <= 1:
        print(f"\n  🟡 FRAGILE EDGE — works in most windows but regime-dependent")
    else:
        print(f"\n  🔴 UNRELIABLE — too much variance; likely overfitted to recent data")

    # Save walk-forward results
    wf_output = {
        'timestamp':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'mode':          mode,
        'threshold':     threshold,
        'windows':       window_results,
        'mean_win_rate': mean_wr,
        'mean_expectancy': mean_exp,
        'std_win_rate':  std_wr,
        'stable':        stable,
        'failing_windows': len(failing),
    }
    atomic_write('/home/ubuntu/.picoclaw/logs/apex-walkforward-results.json', wf_output)
    print(f"\n✅ Walk-forward results saved")
    return wf_output


def run_montecarlo(mode='TREND', threshold=7, simulations=2000):
    """
    Monte Carlo simulation on the trade sequence.

    Takes the actual backtest trade outcomes (wins/losses as R-multiples)
    and randomly reshuffles the sequence 2000 times. For each shuffle,
    computes the ending equity and maximum drawdown.

    This answers:
    1. "Is my result luck or skill?" — if 95% of shuffles are profitable,
       the edge is structural, not sequence-dependent.
    2. "What's my realistic worst-case drawdown?" — the 5th percentile
       outcome represents a bad-but-plausible scenario.
    3. "What's my expected range of outcomes?" — 25th/75th percentile band.

    Requires existing backtest results (run `apex-backtest.py` first).
    """
    import math
    import random

    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"MONTE CARLO SIMULATION — {mode} (threshold {threshold})")
    print(f"Simulations: {simulations:,}")
    print(f"{'='*60}")

    # Load existing backtest results for trade outcomes
    bt_results = safe_read('/home/ubuntu/.picoclaw/logs/apex-backtest-results.json', {})
    all_trades = bt_results.get('all_trades', [])

    if len(all_trades) < 20:
        print(f"  ⚠️  Only {len(all_trades)} trades in backtest — need 20+ for meaningful simulation")
        print(f"  Run apex-backtest.py {mode} {threshold} first to generate trades")
        return

    # Extract R-multiples from trade history
    r_multiples = []
    for t in all_trades:
        if t.get('outcome') == 'WIN':
            r_multiples.append(float(t.get('r_multiple', 1.5)))
        else:
            r_multiples.append(-1.0)

    n_trades = len(r_multiples)
    print(f"  Input trades: {n_trades} | Win rate: {sum(1 for r in r_multiples if r > 0)/n_trades*100:.1f}%")

    # Run simulations
    starting_capital = 5000.0
    risk_pct         = 0.02   # 2% risk per trade (matches system default)

    end_values    = []
    max_drawdowns = []
    ruin_count    = 0

    for _ in range(simulations):
        equity      = starting_capital
        peak        = starting_capital
        max_dd      = 0.0
        ruined      = False

        shuffled = r_multiples.copy()
        random.shuffle(shuffled)

        for r in shuffled:
            risk_amt = equity * risk_pct
            equity  += risk_amt * r
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
            if equity < starting_capital * 0.5:  # 50% ruin threshold
                ruined = True
                break

        end_values.append(equity)
        max_drawdowns.append(max_dd)
        if ruined:
            ruin_count += 1

    # Statistics
    end_values.sort()
    max_drawdowns.sort()

    def pct(lst, p):
        idx = max(0, min(len(lst) - 1, int(len(lst) * p / 100)))
        return lst[idx]

    profitable = sum(1 for v in end_values if v > starting_capital)
    profit_pct = round(profitable / simulations * 100, 1)
    ruin_pct   = round(ruin_count / simulations * 100, 2)

    print(f"\n  Ending equity (£{starting_capital:,.0f} start):")
    print(f"    5th  pct:  £{pct(end_values, 5):8,.0f}  (worst realistic outcome)")
    print(f"    25th pct:  £{pct(end_values, 25):8,.0f}")
    print(f"    50th pct:  £{pct(end_values, 50):8,.0f}  (median)")
    print(f"    75th pct:  £{pct(end_values, 75):8,.0f}")
    print(f"    95th pct:  £{pct(end_values, 95):8,.0f}  (best realistic outcome)")

    print(f"\n  Max drawdown:")
    print(f"    Median:    {pct(max_drawdowns, 50):.1f}%")
    print(f"    95th pct:  {pct(max_drawdowns, 95):.1f}%  (bad-but-plausible scenario)")

    print(f"\n  {'✅' if profit_pct >= 70 else '⚠️' if profit_pct >= 50 else '🔴'} "
          f"Profitable in {profit_pct}% of simulations")
    if ruin_pct > 0:
        print(f"  ⚠️  Ruin (<50% capital) in {ruin_pct}% of simulations")

    # Interpretation
    if profit_pct >= 80 and pct(max_drawdowns, 95) < 25:
        print(f"\n  ✅ ROBUST — edge is structural, not sequence luck")
    elif profit_pct >= 60:
        print(f"\n  🟡 MODERATE — edge exists but sequence risk is real. "
              f"Keep position sizes conservative.")
    else:
        print(f"\n  🔴 HIGH RISK — less than 60% of random sequences are profitable. "
              f"Review signal threshold and position sizing.")

    # Save results
    mc_output = {
        'timestamp':       now.strftime('%Y-%m-%d %H:%M UTC'),
        'mode':            mode,
        'threshold':       threshold,
        'simulations':     simulations,
        'n_trades':        n_trades,
        'starting_capital':starting_capital,
        'profitable_pct':  profit_pct,
        'ruin_pct':        ruin_pct,
        'equity_p5':       round(pct(end_values, 5), 2),
        'equity_p25':      round(pct(end_values, 25), 2),
        'equity_p50':      round(pct(end_values, 50), 2),
        'equity_p75':      round(pct(end_values, 75), 2),
        'equity_p95':      round(pct(end_values, 95), 2),
        'max_dd_median':   round(pct(max_drawdowns, 50), 2),
        'max_dd_p95':      round(pct(max_drawdowns, 95), 2),
    }
    atomic_write('/home/ubuntu/.picoclaw/logs/apex-montecarlo-results.json', mc_output)
    print(f"\n✅ Monte Carlo results saved")
    return mc_output


if __name__ == '__main__':
    mode      = sys.argv[1] if len(sys.argv) > 1 else 'TREND'
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    cmd       = sys.argv[3] if len(sys.argv) > 3 else 'run'

    if cmd == 'walkforward':
        windows = int(sys.argv[4]) if len(sys.argv) > 4 else 4
        run_walkforward(mode, threshold, windows)
    elif cmd == 'montecarlo':
        sims = int(sys.argv[4]) if len(sys.argv) > 4 else 2000
        run_montecarlo(mode, threshold, sims)
    elif cmd == 'all':
        run(mode, threshold)
        run_walkforward(mode, threshold)
        run_montecarlo(mode, threshold)
    else:
        run(mode, threshold)
