#!/usr/bin/env python3
import json
from datetime import datetime, timezone

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'

try:
    with open(OUTCOMES_FILE) as f:
        db = json.load(f)
except:
    print("No outcome data yet. Keep trading and the learning report will build over time.")
    exit(0)

trades  = db.get('trades', [])
summary = db.get('summary', {})

if len(trades) < 3:
    print(f"Only {len(trades)} trades logged so far. Need at least 3 to generate meaningful patterns. Keep trading.")
    exit(0)

today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
lines = []
lines.append(f"🧠 APEX LEARNING REPORT — {today}")
lines.append(f"Based on {len(trades)} completed trades\n")

lines.append("📊 OVERALL PERFORMANCE")
lines.append(f"  Win rate:      {summary.get('win_rate', 0)}%")
lines.append(f"  Avg R achieved: {summary.get('avg_r_achieved', 0)}")
lines.append(f"  Total P&L:     £{summary.get('total_pnl', 0)}")
lines.append(f"  Best trade:    {summary.get('best_trade', 'N/A')}")
lines.append(f"  Worst trade:   {summary.get('worst_trade', 'N/A')}\n")

lines.append("📈 WIN RATE BY RSI RANGE")
for bucket, stats in summary.get('by_rsi_bucket', {}).items():
    bar = "█" * int(stats['win_rate'] / 10)
    lines.append(f"  {bucket:12} | {stats['win_rate']:5}% | {bar} ({stats['trades']} trades)")

lines.append("\n📉 WIN RATE BY MACD")
for state, stats in summary.get('by_macd', {}).items():
    lines.append(f"  {state:15} | {stats['win_rate']:5}% ({stats['trades']} trades)")

lines.append("\n🏢 WIN RATE BY SECTOR")
for sector, stats in summary.get('by_sector', {}).items():
    lines.append(f"  {sector:10} | {stats['win_rate']:5}% ({stats['trades']} trades)")

lines.append("\n📅 WIN RATE BY DAY OPENED")
for day, stats in summary.get('by_day', {}).items():
    lines.append(f"  {day:10} | {stats['win_rate']:5}% ({stats['trades']} trades)")

# Insights
lines.append("\n💡 APEX INSIGHTS")
rsi_buckets = summary.get('by_rsi_bucket', {})
if rsi_buckets:
    best_rsi = max(rsi_buckets.items(), key=lambda x: x[1]['win_rate'])
    worst_rsi = min(rsi_buckets.items(), key=lambda x: x[1]['win_rate'])
    if best_rsi[1]['trades'] >= 2:
        lines.append(f"  Best RSI range: {best_rsi[0]} ({best_rsi[1]['win_rate']}% win rate)")
    if worst_rsi[1]['trades'] >= 2:
        lines.append(f"  Avoid RSI range: {worst_rsi[0]} ({worst_rsi[1]['win_rate']}% win rate)")

sectors = summary.get('by_sector', {})
if sectors:
    best_sector = max(sectors.items(), key=lambda x: x[1]['win_rate'])
    if best_sector[1]['trades'] >= 2:
        lines.append(f"  Best sector: {best_sector[0]} ({best_sector[1]['win_rate']}% win rate)")

days = summary.get('by_day', {})
if days:
    worst_day = min(days.items(), key=lambda x: x[1]['win_rate'])
    if worst_day[1]['trades'] >= 2:
        lines.append(f"  Weakest day: {worst_day[0]} ({worst_day[1]['win_rate']}% win rate) — consider avoiding")

lines.append("\n⚙️ SCORING RECOMMENDATIONS")
macd = summary.get('by_macd', {})
macd_pos = macd.get('macd_positive', {}).get('win_rate', 50)
macd_neg = macd.get('macd_negative', {}).get('win_rate', 50)
if macd_pos > macd_neg + 15:
    lines.append("  MACD is proving reliable — current weight appropriate")
elif macd_neg > macd_pos:
    lines.append("  MACD not adding value — consider reducing MACD weight in scoring")

if summary.get('win_rate', 0) > 60:
    lines.append("  Win rate above 60% — system performing well")
elif summary.get('win_rate', 0) < 40:
    lines.append("  Win rate below 40% — consider raising signal threshold to 8/10")

print("\n".join(lines))

# Signal log stats
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-signal-log.json') as f:
        sig_db = json.load(f)
    sig_stats = sig_db.get('stats', {})
    total_sigs = sig_stats.get('total_generated', 0)
    if total_sigs > 0:
        lines.append(f"\n📡 SIGNAL LOG — {total_sigs} total signals")
        lines.append(f"  Confirmed:        {sig_stats.get('confirmed', 0)} ({round(sig_stats.get('confirmed',0)/total_sigs*100,1)}%)")
        lines.append(f"  Blocked regime:   {sig_stats.get('blocked_by_regime', 0)}")
        lines.append(f"  Blocked sector:   {sig_stats.get('blocked_by_sector', 0)}")
        lines.append(f"  Defensive mode:   {sig_stats.get('defensive_mode', 0)}")
        lines.append(f"  Stale/missed:     {sig_stats.get('blocked_stale', 0)}")
except:
    pass

# Slippage summary
try:
    with open('/home/ubuntu/.picoclaw/logs/apex-slippage.json') as f:
        slip_db = json.load(f)
    summary = slip_db.get('summary', {})
    total   = summary.get('total_trades', 0)
    if total > 0:
        lines.append(f"\n💸 SLIPPAGE REPORT — {total} trades")
        lines.append(f"  Avg cost per trade: £{summary.get('avg_slip_per_trade',0)}")
        lines.append(f"  Total slippage:     £{summary.get('total_slip_cost',0)}")
        lines.append(f"  Against you:        {summary.get('trades_against',0)}/{total}")
except:
    pass

# Sharpe ratio
try:
    import importlib.util, math
    spec = importlib.util.spec_from_file_location(
        "sharpe", "/home/ubuntu/.picoclaw/scripts/apex-sharpe.py")
    sharpe_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sharpe_mod)
    result = sharpe_mod.calculate_sharpe()
    if result:
        lines.append(f"\n📊 RISK-ADJUSTED PERFORMANCE")
        lines.append(f"  Sharpe Ratio:   {result['sharpe_ratio']} — {result['rating']}")
        lines.append(f"  Sortino Ratio:  {result['sortino_ratio']}")
        lines.append(f"  Win Rate:       {result['win_rate']}%")
        lines.append(f"  Profit Factor:  {result['profit_factor']}")
        lines.append(f"  Avg Return/Trade: {result['avg_return_per_trade']}R")
except:
    pass
