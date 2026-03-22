#!/usr/bin/env python3
import json
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


OUTCOMES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
WEIGHTS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-weights.json'

# Default weights
DEFAULT_WEIGHTS = {
    "trend":  3,
    "rsi":    3,
    "volume": 2,
    "macd":   2,
    "total":  10
}

MIN_TRADES_REQUIRED = 20

def load_outcomes():
    try:
        with open(OUTCOMES_FILE) as f:
            return json.load(f)
    except:
        return {"trades": [], "summary": {}}

def load_weights():
    try:
        with open(WEIGHTS_FILE) as f:
            return json.load(f)
    except:
        return DEFAULT_WEIGHTS.copy()

def analyse_factor_performance(trades):
    results = {
        "trend":  {"wins": 0, "losses": 0, "total": 0},
        "rsi":    {"wins": 0, "losses": 0, "total": 0},
        "volume": {"wins": 0, "losses": 0, "total": 0},
        "macd":   {"wins": 0, "losses": 0, "total": 0},
    }

    for t in trades:
        win = t.get('pnl', 0) > 0

        # Trend alignment
        results["trend"]["total"] += 1
        if win:
            results["trend"]["wins"] += 1
        else:
            results["trend"]["losses"] += 1

        # RSI bucket analysis
        rsi = t.get('rsi', 0)
        results["rsi"]["total"] += 1
        if 45 <= rsi <= 70 and win:
            results["rsi"]["wins"] += 1
        elif not (45 <= rsi <= 70) and not win:
            results["rsi"]["wins"] += 1  # RSI correctly excluded bad trade
        else:
            results["rsi"]["losses"] += 1

        # MACD analysis
        macd_positive = t.get('macd_positive', False)
        results["macd"]["total"] += 1
        if macd_positive and win:
            results["macd"]["wins"] += 1
        elif not macd_positive and not win:
            results["macd"]["wins"] += 1
        else:
            results["macd"]["losses"] += 1

        # Volume (proxy — all trades pass volume check so analyse win rate)
        results["volume"]["total"] += 1
        if win:
            results["volume"]["wins"] += 1
        else:
            results["volume"]["losses"] += 1

    # Calculate win rates
    for factor in results:
        total = results[factor]["total"]
        wins  = results[factor]["wins"]
        results[factor]["win_rate"] = round(wins / total * 100, 1) if total > 0 else 50.0

    return results

def calculate_new_weights(factor_performance):
    weights = DEFAULT_WEIGHTS.copy()

    trend_wr  = factor_performance["trend"]["win_rate"]
    rsi_wr    = factor_performance["rsi"]["win_rate"]
    macd_wr   = factor_performance["macd"]["win_rate"]
    volume_wr = factor_performance["volume"]["win_rate"]

    adjustments = []

    # Trend weight
    if trend_wr >= 70:
        weights["trend"] = 4
        adjustments.append(f"Trend win rate {trend_wr}% — increasing weight to 4")
    elif trend_wr <= 45:
        weights["trend"] = 2
        adjustments.append(f"Trend win rate {trend_wr}% — decreasing weight to 2")

    # RSI weight
    if rsi_wr >= 70:
        weights["rsi"] = 4
        adjustments.append(f"RSI win rate {rsi_wr}% — increasing weight to 4")
    elif rsi_wr <= 45:
        weights["rsi"] = 2
        adjustments.append(f"RSI win rate {rsi_wr}% — decreasing weight to 2")

    # MACD weight
    if macd_wr >= 70:
        weights["macd"] = 3
        adjustments.append(f"MACD win rate {macd_wr}% — increasing weight to 3")
    elif macd_wr <= 40:
        weights["macd"] = 1
        adjustments.append(f"MACD win rate {macd_wr}% — decreasing weight to 1")

    # Volume weight
    if volume_wr <= 45:
        weights["volume"] = 1
        adjustments.append(f"Volume win rate {volume_wr}% — decreasing weight to 1")

    weights["total"] = weights["trend"] + weights["rsi"] + weights["volume"] + weights["macd"]

    return weights, adjustments

def run():
    db     = load_outcomes()
    trades = db.get('trades', [])
    today  = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    print(f"🧠 APEX WEIGHT OPTIMIZER — {today}")
    print(f"Trades in database: {len(trades)}\n")

    if len(trades) < MIN_TRADES_REQUIRED:
        remaining = MIN_TRADES_REQUIRED - len(trades)
        print(f"⏳ Not enough data yet.")
        print(f"Need {remaining} more trades before weight optimisation activates.")
        print(f"Current weights (default):")
        weights = load_weights()
        for k, v in weights.items():
            if k != 'total':
                print(f"  {k:8}: {v}/10")
        return

    # Analyse factor performance
    factor_perf = analyse_factor_performance(trades)

    print("📊 FACTOR PERFORMANCE ANALYSIS")
    for factor, stats in factor_perf.items():
        bar = "█" * int(stats['win_rate'] / 10)
        print(f"  {factor:8} | Win rate: {stats['win_rate']:5}% | {bar}")

    # Calculate new weights
    new_weights, adjustments = calculate_new_weights(factor_perf)
    old_weights = load_weights()

    print(f"\n⚙️ WEIGHT ADJUSTMENTS")
    if adjustments:
        for adj in adjustments:
            print(f"  → {adj}")
    else:
        print("  No adjustments needed — all factors performing as expected")

    print(f"\n📐 WEIGHT COMPARISON")
    print(f"  {'Factor':8} | {'Old':5} | {'New':5} | Change")
    for k in ['trend', 'rsi', 'volume', 'macd']:
        old = old_weights.get(k, DEFAULT_WEIGHTS[k])
        new = new_weights.get(k, DEFAULT_WEIGHTS[k])
        change = "↑" if new > old else ("↓" if new < old else "—")
        print(f"  {k:8} | {old:5} | {new:5} | {change}")

    print(f"  {'TOTAL':8} | {old_weights.get('total',10):5} | {new_weights['total']:5}")

    # Save new weights
    new_weights['last_updated']  = today
    new_weights['trades_analysed'] = len(trades)

    atomic_write(WEIGHTS_FILE, new_weights)

    print(f"\n✅ Weights saved to {WEIGHTS_FILE}")
    print("Apex will use these weights in tomorrow's morning scan.")

if __name__ == '__main__':
    run()
