#!/usr/bin/env python3
"""
Alpaca real-time data module.
Replaces yfinance for US stocks.
UK/European stocks fall back to yfinance.
"""
import json
import urllib.request
import urllib.parse
import os
from datetime import datetime, timezone, timedelta

# Load credentials
def get_credentials():
    env_file = '/home/ubuntu/.picoclaw/.env.trading212'
    creds = {}
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    creds[k.strip()] = v.strip()
    except:
        pass
    return (
        creds.get('ALPACA_API_KEY', ''),
        creds.get('ALPACA_SECRET', ''),
        'https://data.alpaca.markets/v2'
    )

API_KEY, SECRET, DATA_ENDPOINT = get_credentials()

# US stocks that Alpaca covers
US_TICKERS = {
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
    "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
    "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
    "WMT","PG","XOM","CVX","NVO"
}

def is_us_stock(ticker):
    return ticker.upper() in US_TICKERS

def alpaca_request(path, params=None):
    url = f"{DATA_ENDPOINT}{path}"
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'APCA-API-KEY-ID':     API_KEY,
        'APCA-API-SECRET-KEY': SECRET,
        'Content-Type':        'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return None

def get_live_price(ticker):
    """Get real-time price for US stock."""
    if not is_us_stock(ticker):
        return None

    data = alpaca_request(f"/stocks/{ticker}/quotes/latest")
    if not data:
        return None

    quote = data.get('quote', {})
    bid   = float(quote.get('bp', 0))
    ask   = float(quote.get('ap', 0))

    # Use midpoint if both available, else whichever is non-zero
    if bid > 0 and ask > 0:
        price = round((bid + ask) / 2, 2)
    elif bid > 0:
        price = bid
    elif ask > 0:
        price = ask
    else:
        return None

    return price

def get_bars(ticker, timeframe='1Day', limit=250):
    """Get historical bars for technical analysis."""
    if not is_us_stock(ticker):
        return None

    # Calculate start date
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime('%Y-%m-%d')

    params = {
        'timeframe': timeframe,
        'start':     start,
        'limit':     limit,
        'adjustment':'split'
    }

    data = alpaca_request(f"/stocks/{ticker}/bars", params)
    if not data or 'bars' not in data:
        return None

    return data['bars']

def get_technical_data(ticker):
    """
    Get full technical data for scoring.
    Returns same format as yfinance-based get_live_data.
    """
    if not is_us_stock(ticker):
        return None

    bars = get_bars(ticker)
    if not bars or len(bars) < 50:
        return None

    # Extract price and volume series
    closes  = [float(b['c']) for b in bars]
    volumes = [float(b['v']) for b in bars]
    highs   = [float(b['h']) for b in bars]
    lows    = [float(b['l']) for b in bars]

    price = closes[-1]

    # EMA calculations
    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for val in data[1:]:
            result.append(val * k + result[-1] * (1 - k))
        return result

    ema50_series  = ema(closes, 50)
    ema200_series = ema(closes, 200)
    ema12_series  = ema(closes, 12)
    ema26_series  = ema(closes, 26)

    ema50  = round(ema50_series[-1], 2)
    ema200 = round(ema200_series[-1], 2)

    # MACD
    macd_line    = [e12 - e26 for e12, e26 in zip(ema12_series, ema26_series)]
    signal_line  = ema(macd_line, 9)
    macd_hist    = [m - s for m, s in zip(macd_line, signal_line)]
    macd_current = round(macd_hist[-1], 4)
    macd_rising  = macd_hist[-1] > macd_hist[-2]

    # RSI 14
    gains  = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rs       = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi      = round(100 - (100 / (1 + rs)), 2)

    # Volume
    avg_volume  = sum(volumes[-20:]) / 20
    vol_ratio   = round(volumes[-1] / avg_volume, 2) if avg_volume > 0 else 1.0

    # 52-week range
    year_closes = closes[-252:] if len(closes) >= 252 else closes
    high_52     = round(max(year_closes), 2)
    low_52      = round(min(year_closes), 2)
    discount    = round((high_52 - price) / high_52 * 100, 1)

    # Trend
    trend = "BULLISH" if price > ema50 > ema200 else ("BEARISH" if price < ema50 < ema200 else "NEUTRAL")

    # Live price override — get real-time price
    live_price = get_live_price(ticker)
    if live_price and live_price > 0:
        price = live_price

    # Stop and targets
    stop    = round(price * 0.94, 2)
    risk    = round(price - stop, 2)
    target1 = round(price + risk * 1.5, 2)
    target2 = round(price + risk * 2.5, 2)
    qty     = round(min(50 / risk, 250 / price), 2) if risk > 0 else 1

    return {
        "ticker":      ticker,
        "price":       round(price, 2),
        "currency":    "USD",
        "rsi":         rsi,
        "ema50":       ema50,
        "ema200":      ema200,
        "trend":       trend,
        "macd_hist":   macd_current,
        "macd_rising": macd_rising,
        "vol_ratio":   vol_ratio,
        "high_52":     high_52,
        "low_52":      low_52,
        "discount":    discount,
        "stop":        stop,
        "target1":     target1,
        "target2":     target2,
        "quantity":    qty,
        "risk":        round(qty * risk, 2),
        "data_source": "ALPACA_LIVE"
    }

def get_multiple_prices(tickers):
    """Get latest prices for multiple US tickers in one call."""
    us_tickers = [t for t in tickers if is_us_stock(t)]
    if not us_tickers:
        return {}

    params = {'symbols': ','.join(us_tickers)}
    data   = alpaca_request("/stocks/quotes/latest", params)

    if not data or 'quotes' not in data:
        return {}

    prices = {}
    for ticker, quote in data['quotes'].items():
        bid = float(quote.get('bp', 0))
        ask = float(quote.get('ap', 0))
        if bid > 0 and ask > 0:
            prices[ticker] = round((bid + ask) / 2, 2)
        elif bid > 0:
            prices[ticker] = bid
        elif ask > 0:
            prices[ticker] = ask

    return prices

def get_snapshot(ticker):
    """Get latest quote, trade and daily bar in one call."""
    data = alpaca_request(f"/stocks/{ticker}/snapshot")
    if not data:
        return None

    prev_daily = data.get('prevDailyBar', {})
    daily      = data.get('dailyBar', {})
    quote      = data.get('latestQuote', {})

    prev_close = float(prev_daily.get('c', 0))
    current_close = float(daily.get('c', 0))

    bid = float(quote.get('bp', 0))
    ask = float(quote.get('ap', 0))
    live_price = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else (bid or ask or current_close)

    return {
        "prev_close":   prev_close,
        "current":      live_price or current_close,
        "daily_open":   float(daily.get('o', 0)),
        "daily_high":   float(daily.get('h', 0)),
        "daily_low":    float(daily.get('l', 0)),
        "daily_volume": float(daily.get('v', 0)),
    }

def get_multiple_snapshots(tickers):
    """Get snapshots for multiple tickers."""
    us_tickers = [t for t in tickers if is_us_stock(t)]
    if not us_tickers:
        return {}

    params = {'symbols': ','.join(us_tickers)}
    data   = alpaca_request("/stocks/snapshots", params)
    if not data:
        return {}

    results = {}
    for ticker, snap in data.items():
        prev_daily = snap.get('prevDailyBar', {})
        daily      = snap.get('dailyBar', {})
        quote      = snap.get('latestQuote', {})

        prev_close = float(prev_daily.get('c', 0))
        bid = float(quote.get('bp', 0))
        ask = float(quote.get('ap', 0))
        live = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else (bid or ask or float(daily.get('c', 0)))

        results[ticker] = {
            "prev_close": prev_close,
            "current":    live,
            "gap_pct":    round((live - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
        }

    return results

if __name__ == '__main__':
    print("Testing Alpaca data module...\n")

    # Test single price
    print("1. Live prices (multiple stocks):")
    prices = get_multiple_prices(["AAPL","MSFT","XOM","V","JPM"])
    for ticker, price in prices.items():
        print(f"   {ticker:6}: ${price}")

    # Test technical data
    print(f"\n2. Technical data for XOM:")
    data = get_technical_data("XOM")
    if data:
        print(f"   Price:    ${data['price']} ({data['data_source']})")
        print(f"   RSI:      {data['rsi']}")
        print(f"   Trend:    {data['trend']}")
        print(f"   MACD:     {data['macd_hist']} ({'rising' if data['macd_rising'] else 'falling'})")
        print(f"   EMA50:    ${data['ema50']}")
        print(f"   52w high: ${data['high_52']}")
        print(f"   52w low:  ${data['low_52']}")
        print(f"   Discount: {data['discount']}% from high")
    else:
        print("   Failed to fetch")
