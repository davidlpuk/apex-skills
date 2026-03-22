#!/usr/bin/env python3
"""
ATR-Based Stop Loss Calculator
Replaces fixed 6% stops with volatility-adjusted stops.
High volatility instruments get wider stops to avoid noise-based stop-outs.
Low volatility instruments get tighter stops for better risk control.
"""
import json
import sys
from datetime import datetime, timezone

def calculate_atr(highs, lows, closes, period=14):
    """Average True Range calculation."""
    if len(closes) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(closes)):
        hl  = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i-1])
        lpc = abs(lows[i] - closes[i-1])
        tr  = max(hl, hpc, lpc)
        true_ranges.append(tr)

    # Wilder's smoothing
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return round(atr, 4)

def get_atr_data(ticker, yahoo_ticker=None):
    """Fetch price data and calculate ATR."""
    try:
        import yfinance as yf
        t    = yf.Ticker(yahoo_ticker or ticker)
        hist = t.history(period="3mo")

        if hist.empty or len(hist) < 20:
            return None

        highs  = list(hist['High'])
        lows   = list(hist['Low'])
        closes = list(hist['Close'])

        # Fix pence for UK stocks
        if yahoo_ticker and yahoo_ticker.endswith('.L'):
            if closes[-1] > 100:
                highs  = [h/100 for h in highs]
                lows   = [l/100 for l in lows]
                closes = [c/100 for c in closes]

        price = round(closes[-1], 2)
        atr   = calculate_atr(highs, lows, closes)

        if not atr:
            return None

        atr_pct = round(atr / price * 100, 2)

        return {
            "ticker":  ticker,
            "price":   price,
            "atr":     round(atr, 4),
            "atr_pct": atr_pct
        }
    except:
        return None

def calculate_atr_stop(price, atr, atr_multiplier=2.0, signal_type='TREND'):
    """
    Calculate ATR-based stop loss.

    Standard: 2x ATR below entry for trend
    Contrarian: 1.5x ATR (tighter — mean reversion should work quickly)
    """
    if signal_type == 'CONTRARIAN':
        multiplier = 1.5
    elif signal_type == 'EARNINGS_DRIFT':
        multiplier = 1.5
    elif signal_type == 'DIVIDEND_CAPTURE':
        multiplier = 1.0  # Very tight — income trade
    else:
        multiplier = atr_multiplier

    stop         = round(price - (atr * multiplier), 2)
    stop_pct     = round((price - stop) / price * 100, 2)
    risk_share   = round(price - stop, 2)

    return {
        "stop":        stop,
        "stop_pct":    stop_pct,
        "risk_share":  risk_share,
        "multiplier":  multiplier,
        "atr_used":    round(atr, 4)
    }

def calculate_atr_targets(price, atr, signal_type='TREND'):
    """
    Calculate targets based on ATR multiples.
    Target 1: 2x ATR above entry
    Target 2: 3.5x ATR above entry
    """
    if signal_type == 'CONTRARIAN':
        t1_mult = 2.0
        t2_mult = 3.5
    elif signal_type == 'DIVIDEND_CAPTURE':
        t1_mult = 1.0  # Just capture the dividend move
        t2_mult = 1.5
    else:
        t1_mult = 2.0
        t2_mult = 3.5

    target1 = round(price + atr * t1_mult, 2)
    target2 = round(price + atr * t2_mult, 2)

    return target1, target2

def get_full_atr_levels(ticker, yahoo_ticker, signal_type='TREND', portfolio=5000):
    """
    Get complete ATR-based trade levels.
    Returns stop, targets, quantity — all volatility adjusted.
    """
    data = get_atr_data(ticker, yahoo_ticker)
    if not data:
        # Fall back to fixed 6% stop
        return None

    price = data['price']
    atr   = data['atr']

    stop_data          = calculate_atr_stop(price, atr, signal_type=signal_type)
    target1, target2   = calculate_atr_targets(price, atr, signal_type)

    stop       = stop_data['stop']
    risk_share = stop_data['risk_share']

    # Position sizing — £50 max risk
    qty      = round(50 / risk_share, 2) if risk_share > 0 else 1
    notional = round(qty * price, 2)

    # Cap at 8% portfolio
    max_notional = portfolio * 0.08
    if notional > max_notional:
        qty      = round(max_notional / price, 2)
        notional = round(qty * price, 2)

    return {
        "ticker":       ticker,
        "price":        price,
        "atr":          atr,
        "atr_pct":      data['atr_pct'],
        "stop":         stop,
        "stop_pct":     stop_data['stop_pct'],
        "target1":      target1,
        "target2":      target2,
        "quantity":     qty,
        "notional":     notional,
        "risk":         round(qty * risk_share, 2),
        "signal_type":  signal_type,
        "method":       "ATR"
    }

def compare_fixed_vs_atr(ticker, yahoo_ticker, signal_type='TREND'):
    """Show the difference between fixed 6% and ATR-based stops."""
    data = get_atr_data(ticker, yahoo_ticker)
    if not data:
        print(f"Could not fetch data for {ticker}")
        return

    price = data['price']
    atr   = data['atr']

    # Fixed stop
    fixed_stop     = round(price * 0.94, 2)
    fixed_risk     = round(price - fixed_stop, 2)
    fixed_qty      = round(min(50 / fixed_risk, 250 / price), 2)

    # ATR stop
    atr_stop_data  = calculate_atr_stop(price, atr, signal_type=signal_type)
    atr_stop       = atr_stop_data['stop']
    atr_risk       = atr_stop_data['risk_share']
    atr_qty        = round(min(50 / atr_risk, 250 / price), 2) if atr_risk > 0 else 1
    atr_t1, atr_t2 = calculate_atr_targets(price, atr, signal_type)

    print(f"\n{'='*55}")
    print(f"ATR ANALYSIS — {ticker} @ £{price}")
    print(f"{'='*55}")
    print(f"  ATR (14):     £{atr} ({data['atr_pct']}% of price)")
    print(f"  Volatility:   {'HIGH' if data['atr_pct'] > 3 else ('MEDIUM' if data['atr_pct'] > 1.5 else 'LOW')}")
    print(f"")
    print(f"  {'':20} {'Fixed 6%':12} {'ATR 2x':12}")
    print(f"  {'Stop':20} £{fixed_stop:<11} £{atr_stop:<11}")
    print(f"  {'Stop %':20} {6.0:<11}% {atr_stop_data['stop_pct']:<11}%")
    print(f"  {'Risk/share':20} £{fixed_risk:<11} £{atr_risk:<11}")
    print(f"  {'Quantity':20} {fixed_qty:<11} {atr_qty:<11}")
    print(f"  {'Target 1':20} {'N/A (1.5R)':12} £{atr_t1:<11}")
    print(f"  {'Target 2':20} {'N/A (2.5R)':12} £{atr_t2:<11}")

    if atr_stop_data['stop_pct'] > 6:
        print(f"\n  ⚠️ ATR stop is WIDER than fixed 6%")
        print(f"  This instrument is too volatile for a 6% stop — was being stopped out by noise")
    elif atr_stop_data['stop_pct'] < 6:
        print(f"\n  ✅ ATR stop is TIGHTER than fixed 6%")
        print(f"  This instrument is low-volatility — 6% stop was too generous")
    else:
        print(f"\n  ➡️ ATR stop is similar to fixed 6%")

if __name__ == '__main__':
    print("Testing ATR-based stops on current positions...\n")

    positions = [
        ("VUAG",  "VUAG.L",  "TREND"),
        ("XOM",   "XOM",     "TREND"),
        ("V",     "V",       "CONTRARIAN"),
        ("AAPL",  "AAPL",    "CONTRARIAN"),
        ("NVDA",  "NVDA",    "TREND"),
        ("MSFT",  "MSFT",    "CONTRARIAN"),
    ]

    for ticker, yahoo, sig_type in positions:
        compare_fixed_vs_atr(ticker, yahoo, sig_type)
