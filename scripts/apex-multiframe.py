#!/usr/bin/env python3
"""
Multi-Timeframe Analysis
Checks weekly chart alignment to confirm or reject daily signals.
Weekly trend = institutional money direction.
Daily signal = entry timing.
Both aligned = high conviction. Diverged = lower conviction or block.
"""
import json
import yfinance as yf
from datetime import datetime, timezone
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


OUTPUT_FILE = '/home/ubuntu/.picoclaw/logs/apex-multiframe.json'

YAHOO_MAP = {
    "AAPL":"AAPL","MSFT":"MSFT","NVDA":"NVDA","GOOGL":"GOOGL",
    "AMZN":"AMZN","META":"META","JPM":"JPM","GS":"GS",
    "V":"V","BAC":"BAC","BLK":"BLK","JNJ":"JNJ","PFE":"PFE",
    "UNH":"UNH","ABBV":"ABBV","XOM":"XOM","CVX":"CVX",
    "KO":"KO","PEP":"PEP","PG":"PG","WMT":"WMT",
    "TSLA":"TSLA","HSBA":"HSBA.L","AZN":"AZN.L","GSK":"GSK.L",
    "ULVR":"ULVR.L","SHEL":"SHEL.L","VUAG":"VUAG.L",
    "SQQQ":"SQQQ","QQQS":"QQQS.L","3USS":"3USS.L","SPXU":"SPXU",
}

def fix_pence(price, yahoo):
    if yahoo.endswith('.L') and price > 100:
        return price / 100
    return price

def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k   = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

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
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def get_price_structure(closes):
    """
    Identify higher highs/higher lows (uptrend) or
    lower highs/lower lows (downtrend) from recent swings.
    """
    if len(closes) < 10:
        return "UNKNOWN", 0

    # Use 5-bar swing points
    highs = []
    lows  = []
    for i in range(2, len(closes)-2):
        if closes[i] > closes[i-1] and closes[i] > closes[i-2] and \
           closes[i] > closes[i+1] and closes[i] > closes[i+2]:
            highs.append(closes[i])
        if closes[i] < closes[i-1] and closes[i] < closes[i-2] and \
           closes[i] < closes[i+1] and closes[i] < closes[i+2]:
            lows.append(closes[i])

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]   # Higher high
        hl = lows[-1] > lows[-2]     # Higher low
        lh = highs[-1] < highs[-2]   # Lower high
        ll = lows[-1] < lows[-2]     # Lower low

        if hh and hl:
            return "UPTREND", 2
        elif lh and ll:
            return "DOWNTREND", -2
        elif hh and ll:
            return "VOLATILE", 0
        elif lh and hl:
            return "COMPRESSING", 0

    return "NEUTRAL", 0

def analyse_timeframe(yahoo, period="1y", interval="1wk"):
    """Analyse a single timeframe — weekly or daily."""
    try:
        hist = yf.Ticker(yahoo).history(period=period, interval=interval)
        if hist.empty or len(hist) < 20:
            return None

        closes  = [fix_pence(float(c), yahoo) for c in hist['Close']]
        volumes = [float(v) for v in hist['Volume']]

        price  = closes[-1]
        ema21  = calculate_ema(closes[-21:], 21)
        ema50  = calculate_ema(closes[-50:] if len(closes) >= 50 else closes, 50)
        ema200 = calculate_ema(closes, 200) if len(closes) >= 200 else calculate_ema(closes, len(closes))
        rsi    = calculate_rsi(closes[-28:])

        avg_vol   = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else volumes[-1]
        vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1

        # Trend determination
        above_21  = price > ema21
        above_50  = price > ema50
        above_200 = price > ema200
        ema_aligned = ema21 > ema50 > ema200  # Full bull alignment

        # Price structure
        structure, structure_score = get_price_structure(closes[-20:])

        # Weekly trend score
        trend_score = 0
        trend_notes = []

        if ema_aligned:
            trend_score += 3
            trend_notes.append("Full EMA alignment (21>50>200) — strong uptrend")
        elif above_50 and above_200:
            trend_score += 2
            trend_notes.append("Above 50 and 200 EMA — uptrend")
        elif above_200:
            trend_score += 1
            trend_notes.append("Above 200 EMA — long-term uptrend")
        elif not above_200 and not above_50:
            trend_score -= 2
            trend_notes.append("Below 50 and 200 EMA — downtrend")
        elif not above_200:
            trend_score -= 1
            trend_notes.append("Below 200 EMA — long-term downtrend")

        if structure == "UPTREND":
            trend_score += 2
            trend_notes.append("Higher highs and higher lows — bullish structure")
        elif structure == "DOWNTREND":
            trend_score -= 2
            trend_notes.append("Lower highs and lower lows — bearish structure")

        if rsi > 60:
            trend_score += 1
            trend_notes.append(f"RSI {rsi} — bullish momentum")
        elif rsi < 40:
            trend_score -= 1
            trend_notes.append(f"RSI {rsi} — bearish momentum")

        # Classify
        if trend_score >= 4:
            trend_class = "STRONG_BULL"
        elif trend_score >= 2:
            trend_class = "BULL"
        elif trend_score >= 0:
            trend_class = "NEUTRAL"
        elif trend_score >= -2:
            trend_class = "BEAR"
        else:
            trend_class = "STRONG_BEAR"

        return {
            'price':        round(price, 2),
            'ema21':        round(ema21, 2),
            'ema50':        round(ema50, 2),
            'ema200':       round(ema200, 2),
            'rsi':          rsi,
            'above_200':    above_200,
            'above_50':     above_50,
            'ema_aligned':  ema_aligned,
            'vol_ratio':    vol_ratio,
            'structure':    structure,
            'trend_score':  trend_score,
            'trend_class':  trend_class,
            'notes':        trend_notes,
        }
    except Exception as e:
        return None

def get_signal_adjustment(daily, weekly, signal_type):
    """
    Calculate score adjustment based on timeframe alignment.
    """
    if not weekly or not daily:
        return 0, "No multi-timeframe data"

    w_class = weekly.get('trend_class', 'NEUTRAL')
    d_class = daily.get('trend_class', 'NEUTRAL')

    adjustment = 0
    reason     = ""

    if signal_type == 'TREND':
        # Trend signals need weekly confirmation
        if w_class in ['STRONG_BULL', 'BULL']:
            adjustment = 2
            reason     = f"Weekly {w_class} confirms trend signal — high conviction"
        elif w_class == 'NEUTRAL':
            adjustment = 0
            reason     = f"Weekly NEUTRAL — trend signal marginal"
        elif w_class == 'BEAR':
            adjustment = -2
            reason     = f"Weekly BEAR contradicts trend signal — reduce conviction"
        elif w_class == 'STRONG_BEAR':
            adjustment = -3
            reason     = f"Weekly STRONG_BEAR — trend signal against institutional flow"

    elif signal_type in ['CONTRARIAN', 'DIVIDEND_CAPTURE']:
        # Contrarian signals work AGAINST the daily trend
        # Weekly context matters differently — we want weekly support
        if w_class in ['STRONG_BULL', 'BULL']:
            # Contrarian in a weekly uptrend = pullback to buy = ideal
            adjustment = 2
            reason     = f"Weekly {w_class} — contrarian is pullback in uptrend, ideal"
        elif w_class == 'NEUTRAL':
            adjustment = 0
            reason     = f"Weekly NEUTRAL — contrarian viable"
        elif w_class == 'BEAR':
            adjustment = -1
            reason     = f"Weekly BEAR — contrarian is catching falling knife, caution"
        elif w_class == 'STRONG_BEAR':
            adjustment = -2
            reason     = f"Weekly STRONG_BEAR — contrarian high risk, may keep falling"

    elif signal_type == 'INVERSE':
        # Inverse ETF — want weekly bear confirmation
        if w_class in ['STRONG_BEAR', 'BEAR']:
            adjustment = 2
            reason     = f"Weekly {w_class} confirms inverse signal — strong conviction"
        elif w_class == 'NEUTRAL':
            adjustment = 0
            reason     = f"Weekly NEUTRAL — inverse signal marginal"
        elif w_class in ['BULL', 'STRONG_BULL']:
            adjustment = -2
            reason     = f"Weekly {w_class} contradicts inverse — dangerous trade"

    return adjustment, reason

def analyse_instrument(name, yahoo):
    """Full multi-timeframe analysis for one instrument."""
    weekly = analyse_timeframe(yahoo, period="2y", interval="1wk")
    daily  = analyse_timeframe(yahoo, period="6mo", interval="1d")

    if not weekly and not daily:
        return None

    return {
        'name':   name,
        'yahoo':  yahoo,
        'weekly': weekly,
        'daily':  daily,
    }

def run(universe=None):
    """Run multi-timeframe analysis on full universe."""
    now = datetime.now(timezone.utc)

    if universe is None:
        universe = YAHOO_MAP

    print(f"\n=== MULTI-TIMEFRAME ANALYSIS ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Instruments: {len(universe)}\n")

    results = {}

    for name, yahoo in universe.items():
        print(f"  {name}...", flush=True)
        data = analyse_instrument(name, yahoo)

        if data and data.get('weekly'):
            w = data['weekly']
            d = data['daily'] or {}
            w_class = w.get('trend_class','?')
            d_class = d.get('trend_class','?')
            d_rsi   = d.get('rsi', '?')
            icon    = "✅" if w_class in ['STRONG_BULL','BULL'] else \
                      ("🔴" if w_class in ['STRONG_BEAR','BEAR'] else "🟡")
            print(f"  {icon} {name:6} | Weekly:{w_class:12} | Daily:{d_class:12} | D-RSI:{d_rsi}")
            results[name] = data
        else:
            print(f"     {name}: no data")

    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'count':     len(results),
        'data':      results,
    }

    atomic_write(OUTPUT_FILE, output)

    print(f"\n✅ Multi-timeframe analysis complete — {len(results)} instruments")
    return output

def get_adjustment_for_signal(instrument_name, signal_type):
    """Called by decision engine for each signal."""
    try:
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        inst = data.get('data', {}).get(instrument_name, {})
        if not inst:
            return 0, "No MTF data"
        adj, reason = get_signal_adjustment(
            inst.get('daily'),
            inst.get('weekly'),
            signal_type
        )
        return adj, reason
    except:
        return 0, "MTF data unavailable"

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        # Single instrument mode
        name  = sys.argv[1].upper()
        yahoo = YAHOO_MAP.get(name, name)
        data  = analyse_instrument(name, yahoo)
        if data:
            print(f"\n{name} — MULTI-TIMEFRAME ANALYSIS")
            for tf in ['weekly','daily']:
                tf_data = data.get(tf,{})
                if tf_data:
                    print(f"\n  {tf.upper()}:")
                    print(f"    Trend:     {tf_data.get('trend_class')}")
                    print(f"    Structure: {tf_data.get('structure')}")
                    print(f"    RSI:       {tf_data.get('rsi')}")
                    print(f"    EMA21:     £{tf_data.get('ema21')}")
                    print(f"    EMA50:     £{tf_data.get('ema50')}")
                    print(f"    EMA200:    £{tf_data.get('ema200')}")
                    for note in tf_data.get('notes',[]):
                        print(f"    → {note}")
    else:
        run()
