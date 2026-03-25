#!/usr/bin/env python3
"""
Apex VWAP Entry Gate

Checks whether current price is at/below VWAP (fair value) before entry.
Used as an advisory gate — POOR VWAP applies a score penalty; does not block.

Usage (standalone test):
    python3 apex-vwap-gate.py AAPL AAPL TREND
    python3 apex-vwap-gate.py VUAG VUKE.L CONTRARIAN

Callable API:
    from apex-vwap-gate import check_vwap_entry
    result = check_vwap_entry('AAPL', 'AAPL', 'TREND')
"""
import sys
import json
from datetime import datetime, timezone


def check_vwap_entry(ticker: str, yahoo_ticker: str, signal_type: str = 'TREND') -> dict:
    """
    Fetch today's intraday bars and calculate VWAP.

    Returns:
        {
            'vwap':          float | None,
            'price':         float | None,
            'deviation_pct': float | None,   # (price - vwap) / vwap × 100
            'verdict':       'IDEAL' | 'OK' | 'POOR' | 'SKIP',
            'score_adj':     float,           # +0.3 IDEAL, 0 OK, -0.5 POOR
            'reason':        str,
        }
    """
    _skip = {'vwap': None, 'price': None, 'deviation_pct': None,
             'verdict': 'SKIP', 'score_adj': 0.0, 'reason': 'No intraday data'}

    try:
        import yfinance as yf
    except ImportError:
        return {**_skip, 'reason': 'yfinance not available'}

    try:
        hist = yf.Ticker(yahoo_ticker).history(period='1d', interval='5m')
    except Exception as e:
        return {**_skip, 'reason': f'yfinance error: {e}'}

    if hist is None or hist.empty or len(hist) < 3:
        return {**_skip, 'reason': 'Insufficient intraday bars (pre-market or holiday)'}

    try:
        typical_price = (hist['High'] + hist['Low'] + hist['Close']) / 3
        cumvol = hist['Volume'].cumsum()
        if cumvol.iloc[-1] == 0:
            return {**_skip, 'reason': 'Zero volume — market likely closed'}

        vwap  = (typical_price * hist['Volume']).cumsum() / cumvol
        current_vwap  = float(vwap.iloc[-1])
        current_price = float(hist['Close'].iloc[-1])
    except Exception as e:
        return {**_skip, 'reason': f'VWAP calculation error: {e}'}

    if current_vwap <= 0:
        return {**_skip, 'reason': 'VWAP is zero or negative'}

    deviation_pct = (current_price - current_vwap) / current_vwap * 100

    # Verdict thresholds depend on signal type:
    # CONTRARIAN — already oversold, want to enter below VWAP
    # INVERSE    — short at premium, want price above VWAP
    # TREND      — slight above VWAP acceptable (momentum confirms)
    signal_type = (signal_type or 'TREND').upper()

    if signal_type == 'CONTRARIAN':
        if deviation_pct < -0.5:
            verdict, score_adj = 'IDEAL', +0.3
        elif deviation_pct < 0.5:
            verdict, score_adj = 'OK',    0.0
        else:
            verdict, score_adj = 'POOR',  -0.5
    elif signal_type == 'INVERSE':
        if deviation_pct > 0.5:
            verdict, score_adj = 'IDEAL', +0.3
        elif deviation_pct > -0.5:
            verdict, score_adj = 'OK',    0.0
        else:
            verdict, score_adj = 'POOR',  -0.5
    else:  # TREND, EARNINGS_DRIFT, DIVIDEND_CAPTURE, etc.
        if -1.0 < deviation_pct < 1.5:
            verdict, score_adj = 'IDEAL', +0.3
        elif deviation_pct < 2.0:
            verdict, score_adj = 'OK',    0.0
        else:
            verdict, score_adj = 'POOR',  -0.5

    reason_map = {
        'IDEAL': f"Price {deviation_pct:+.1f}% vs VWAP — ideal entry zone",
        'OK':    f"Price {deviation_pct:+.1f}% vs VWAP — acceptable",
        'POOR':  f"Price {deviation_pct:+.1f}% vs VWAP — above fair value, consider deferring",
    }

    return {
        'vwap':          round(current_vwap, 4),
        'price':         round(current_price, 4),
        'deviation_pct': round(deviation_pct, 2),
        'verdict':       verdict,
        'score_adj':     score_adj,
        'reason':        reason_map[verdict],
        'signal_type':   signal_type,
        'n_bars':        len(hist),
        'timestamp':     datetime.now(timezone.utc).isoformat(),
    }


if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: apex-vwap-gate.py <ticker> <yahoo_ticker> [signal_type]")
        sys.exit(1)

    ticker      = args[0]
    yahoo       = args[1]
    stype       = args[2] if len(args) > 2 else 'TREND'

    print(f"\nVWAP Entry Gate — {ticker} ({yahoo}) [{stype}]")
    result = check_vwap_entry(ticker, yahoo, stype)
    print(f"  VWAP:      {result['vwap']}")
    print(f"  Price:     {result['price']}")
    print(f"  Deviation: {result['deviation_pct']}%")
    print(f"  Verdict:   {result['verdict']} (score adj: {result['score_adj']:+.1f})")
    print(f"  Reason:    {result['reason']}")
    print(f"  Bars:      {result.get('n_bars', 'N/A')}")
