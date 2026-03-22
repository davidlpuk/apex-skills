#!/usr/bin/env python3
"""
Breadth Thrust Detection
Detects when market breadth moves from deeply oversold to strongly overbought
in a short period — one of the most reliable bull market signals in existence.

Zweig Breadth Thrust: when the 10-day EMA of advancing/declining issues
moves from below 40% to above 61.5% within 10 trading days.

We approximate this using our universe breadth data.
Also detects:
- Breadth washout (extreme oversold — contrarian buy signal)
- Breadth deterioration (breadth failing to confirm price highs — bearish divergence)
"""
import json
import yfinance as yf
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


OUTPUT_FILE  = '/home/ubuntu/.picoclaw/logs/apex-breadth-thrust.json'
BREADTH_FILE = '/home/ubuntu/.picoclaw/logs/apex-breadth-drilldown.json'
REGIME_FILE  = '/home/ubuntu/.picoclaw/logs/apex-regime.json'

# Track breadth history
HISTORY_FILE = '/home/ubuntu/.picoclaw/logs/apex-breadth-history.json'

# Universe for breadth calculation
BREADTH_UNIVERSE = {
    "AAPL":"AAPL","MSFT":"MSFT","NVDA":"NVDA","GOOGL":"GOOGL",
    "AMZN":"AMZN","META":"META","JPM":"JPM","GS":"GS",
    "V":"V","BAC":"BAC","JNJ":"JNJ","PFE":"PFE","ABBV":"ABBV",
    "XOM":"XOM","CVX":"CVX","KO":"KO","PEP":"PEP","WMT":"WMT",
    "HSBA":"HSBA.L","AZN":"AZN.L","ULVR":"ULVR.L","SHEL":"SHEL.L",
}

def fix_pence(price, yahoo):
    if yahoo.endswith('.L') and price > 100:
        return price / 100
    return price

def calculate_ema(values, period):
    if not values or len(values) < 1:
        return 0
    k   = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 3)

def get_daily_breadth(universe):
    """
    Calculate daily breadth — % of stocks above their 200-day EMA.
    Returns series of daily breadth readings.
    """
    print("  Calculating daily breadth...", flush=True)

    all_series = {}
    for name, yahoo in universe.items():
        try:
            hist = yf.Ticker(yahoo).history(period="1y")
            if hist.empty or len(hist) < 200:
                continue
            closes = [fix_pence(float(c), yahoo) for c in hist['Close']]
            dates  = [d.strftime('%Y-%m-%d') for d in hist.index]

            # Calculate 200-day EMA at each point
            k   = 2 / 201
            ema = closes[0]
            daily_above = {}
            for i, (price, date) in enumerate(zip(closes, dates)):
                if i > 0:
                    ema = price * k + ema * (1 - k)
                if i >= 199:
                    daily_above[date] = 1 if price > ema else 0

            all_series[name] = daily_above
        except Exception as _e:
            log_error(f"Silent failure in apex-breadth-thrust.py: {_e}")

    if not all_series:
        return {}

    # Get all dates
    all_dates = sorted(set(
        date for series in all_series.values() for date in series.keys()
    ))

    # Calculate breadth for each date
    breadth_series = {}
    for date in all_dates[-60:]:  # Last 60 trading days
        above = sum(1 for s in all_series.values() if s.get(date, 0) == 1)
        total = sum(1 for s in all_series.values() if date in s)
        if total > 0:
            breadth_series[date] = round(above / total * 100, 1)

    return breadth_series

def detect_breadth_thrust(breadth_series):
    """
    Zweig Breadth Thrust approximation.
    Looks for breadth moving from <40% to >61.5% within 10 trading days.
    """
    if len(breadth_series) < 15:
        return None

    dates  = sorted(breadth_series.keys())
    values = [breadth_series[d] for d in dates]

    # 10-day EMA of breadth
    ema10_series = []
    ema = values[0]
    k   = 2 / 11
    for v in values:
        ema = v * k + ema * (1 - k)
        ema10_series.append(round(ema, 1))

    # Check last 15 days for thrust pattern
    thrust_detected = False
    thrust_date     = None
    thrust_from     = None
    thrust_to       = None

    for i in range(len(ema10_series) - 10, len(ema10_series)):
        if i < 10:
            continue
        window = ema10_series[i-10:i+1]
        min_val = min(window)
        max_val = max(window)

        if min_val < 40 and max_val > 61.5:
            thrust_detected = True
            thrust_date     = dates[i]
            thrust_from     = min_val
            thrust_to       = max_val
            break

    return {
        'thrust_detected': thrust_detected,
        'thrust_date':     thrust_date,
        'thrust_from':     thrust_from,
        'thrust_to':       thrust_to,
        'current_ema10':   ema10_series[-1] if ema10_series else 0,
        'current_raw':     values[-1] if values else 0,
    }

def detect_washout(breadth_series):
    """
    Breadth washout — extreme oversold condition.
    When breadth drops below 20% — selling climax, contrarian buy.
    """
    if not breadth_series:
        return None

    values       = list(breadth_series.values())
    current      = values[-1]
    recent_low   = min(values[-10:]) if len(values) >= 10 else current
    prev_week    = values[-5] if len(values) >= 5 else current

    # Washout if current < 20% and recovering
    washout = current < 20
    recovering = current > prev_week  # Breadth ticking up

    if current < 15:
        severity = "EXTREME"
        signal   = 2
    elif current < 20:
        severity = "SEVERE"
        signal   = 1
    elif current < 30:
        severity = "MODERATE"
        signal   = 0
    else:
        severity = "NORMAL"
        signal   = 0

    return {
        'washout_detected': washout,
        'current_breadth':  current,
        'recent_low':       recent_low,
        'recovering':       recovering,
        'severity':         severity,
        'signal':           signal,
        'note':             f"Breadth {current}% — {severity} washout" if washout else f"Breadth {current}% — normal"
    }

def detect_divergence(breadth_series, price_series=None):
    """
    Breadth divergence — price making new highs but breadth failing.
    Classic distribution signal.
    """
    if len(breadth_series) < 20:
        return None

    values = list(breadth_series.values())

    # Compare first half vs second half breadth
    mid     = len(values) // 2
    first_h = sum(values[:mid]) / mid
    second_h= sum(values[mid:]) / len(values[mid:])

    breadth_trend = round(second_h - first_h, 1)

    if breadth_trend < -10:
        divergence = "STRONG_DETERIORATION"
        signal     = -2
        note       = f"Breadth deteriorating significantly ({breadth_trend:+.1f}% trend) — distribution"
    elif breadth_trend < -5:
        divergence = "DETERIORATION"
        signal     = -1
        note       = f"Breadth weakening ({breadth_trend:+.1f}% trend) — watch for top"
    elif breadth_trend > 10:
        divergence = "IMPROVEMENT"
        signal     = 1
        note       = f"Breadth improving ({breadth_trend:+.1f}% trend) — accumulation"
    else:
        divergence = "STABLE"
        signal     = 0
        note       = f"Breadth stable ({breadth_trend:+.1f}% trend)"

    return {
        'divergence':     divergence,
        'breadth_trend':  breadth_trend,
        'first_half_avg': round(first_h, 1),
        'second_half_avg':round(second_h, 1),
        'signal':         signal,
        'note':           note,
    }

def save_breadth_history(breadth_series):
    """Append today's breadth to rolling history."""
    try:
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    except:
        history = []

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if not history or history[-1]['date'] != today:
        current = list(breadth_series.values())[-1] if breadth_series else 0
        history.append({'date': today, 'breadth': current})
        history = history[-60:]  # Keep 60 days

    atomic_write(HISTORY_FILE, history)

def run():
    now = datetime.now(timezone.utc)
    print(f"\n=== BREADTH THRUST DETECTOR ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Get breadth series
    breadth_series = get_daily_breadth(BREADTH_UNIVERSE)

    if not breadth_series:
        print("No breadth data available")
        return {}

    print(f"  Breadth series: {len(breadth_series)} days")
    print(f"  Current breadth: {list(breadth_series.values())[-1]}%")

    # Save to history
    save_breadth_history(breadth_series)

    # Detect patterns
    thrust     = detect_breadth_thrust(breadth_series)
    washout    = detect_washout(breadth_series)
    divergence = detect_divergence(breadth_series)

    # Composite signal
    composite_signal = 0
    signals = []

    if thrust and thrust['thrust_detected']:
        composite_signal += 3
        signals.append(f"🚀 BREADTH THRUST DETECTED — {thrust['thrust_from']}% → {thrust['thrust_to']}% in 10 days")
        signals.append(f"   This is one of the most reliable bull signals in market history")

    if washout and washout['washout_detected']:
        composite_signal += washout['signal']
        signals.append(f"🧨 WASHOUT: {washout['severity']} ({washout['current_breadth']}%)")
        if washout['recovering']:
            composite_signal += 1
            signals.append(f"   Breadth recovering — potential selling climax")

    if divergence:
        composite_signal += divergence['signal']
        signals.append(f"📊 BREADTH TREND: {divergence['divergence']} ({divergence['breadth_trend']:+.1f}%)")

    # Overall assessment
    if composite_signal >= 3:
        assessment = "VERY_BULLISH"
        icon       = "🟢"
    elif composite_signal >= 1:
        assessment = "BULLISH"
        icon       = "✅"
    elif composite_signal >= -1:
        assessment = "NEUTRAL"
        icon       = "🟡"
    elif composite_signal >= -2:
        assessment = "BEARISH"
        icon       = "🔴"
    else:
        assessment = "VERY_BEARISH"
        icon       = "⛔"

    print(f"\n  {icon} Breadth Assessment: {assessment}")
    print(f"  Composite signal: {composite_signal:+d}")
    for sig in signals:
        print(f"  {sig}")

    # Regime override check
    if thrust and thrust['thrust_detected']:
        print(f"\n  ⚡ REGIME OVERRIDE RECOMMENDED")
        print(f"  Breadth thrust should override CAUTIOUS/BLOCKED regime")
        print(f"  Consider increasing trend signal sizing to 100%")

    output = {
        'timestamp':          now.strftime('%Y-%m-%d %H:%M UTC'),
        'current_breadth':    list(breadth_series.values())[-1] if breadth_series else 0,
        'thrust':             thrust,
        'washout':            washout,
        'divergence':         divergence,
        'composite_signal':   composite_signal,
        'assessment':         assessment,
        'signals':            signals,
        'breadth_series':     dict(list(breadth_series.items())[-10:]),
    }

    atomic_write(OUTPUT_FILE, output)

    print(f"\n✅ Breadth thrust analysis complete")
    return output

def get_regime_override(current_regime_scale):
    """
    Check if breadth thrust should override current regime.
    Returns new scale if override warranted.
    """
    try:
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        thrust = data.get('thrust', {})
        if thrust and thrust.get('thrust_detected'):
            # Breadth thrust overrides regime — force to 80% minimum
            new_scale = max(current_regime_scale, 0.8)
            return new_scale, "BREADTH_THRUST_OVERRIDE"
        washout = data.get('washout', {})
        if washout and washout.get('washout_detected') and washout.get('recovering'):
            new_scale = max(current_regime_scale, 0.5)
            return new_scale, "WASHOUT_RECOVERY"
        return current_regime_scale, "NO_OVERRIDE"
    except:
        return current_regime_scale, "NO_DATA"

if __name__ == '__main__':
    run()
