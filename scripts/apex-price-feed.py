#!/usr/bin/env python3
"""
Unified price feed — Alpaca for US stocks, yfinance for everything else.
Drop-in replacement for yfinance calls throughout Apex.
"""
import sys
import os

def get_technical_data(ticker, yahoo_ticker=None):
    """
    Get technical data for any instrument.
    Routes to Alpaca for US stocks, yfinance for UK/European.
    """
    # Determine if US stock
    us_tickers = {
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
        "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
        "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
        "WMT","PG","XOM","CVX","NVO"
    }

    clean_ticker = ticker.upper().replace('_US_EQ','').replace('_EQ','')

    if clean_ticker in us_tickers:
        # Use Alpaca for real-time US data
        try:
            sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
            import apex_alpaca as alpaca
            data = alpaca.get_technical_data(clean_ticker)
            if data:
                return data
        except Exception as e:
            pass
        # Fall back to yfinance if Alpaca fails
        return get_yfinance_data(yahoo_ticker or clean_ticker, "USD")
    else:
        # Use yfinance for non-US instruments
        return get_yfinance_data(yahoo_ticker or ticker, detect_currency(yahoo_ticker or ticker))

def detect_currency(yahoo_ticker):
    if not yahoo_ticker:
        return "USD"
    if yahoo_ticker.endswith('.L'):
        return "GBX"
    if yahoo_ticker.endswith('.SW') or yahoo_ticker.endswith('.SW'):
        return "CHF"
    if yahoo_ticker.endswith('.PA') or yahoo_ticker.endswith('.AS') or yahoo_ticker.endswith('.DE') or yahoo_ticker.endswith('.MC'):
        return "EUR"
    return "USD"

def fix_pence(price, currency):
    if currency == "GBX" and price > 100:
        return round(price / 100, 2)
    return price

def get_yfinance_data(yahoo_ticker, currency="USD"):
    """yfinance fallback for non-US instruments."""
    try:
        import yfinance as yf

        t    = yf.Ticker(yahoo_ticker)
        hist = t.history(period="1y")
        if hist.empty or len(hist) < 50:
            return None

        close  = hist['Close'].apply(lambda x: fix_pence(x, currency))
        volume = hist['Volume']

        price    = round(float(close.iloc[-1]), 2)
        ema50    = round(float(close.ewm(span=50).mean().iloc[-1]), 2)
        ema200   = round(float(close.ewm(span=200).mean().iloc[-1]), 2)
        ema12    = close.ewm(span=12).mean()
        ema26    = close.ewm(span=26).mean()
        macd_h   = (ema12 - ema26) - (ema12 - ema26).ewm(span=9).mean()
        macd_current = round(float(macd_h.iloc[-1]), 4)
        macd_rising  = float(macd_h.iloc[-1]) > float(macd_h.iloc[-2])

        delta    = close.diff()
        gain     = delta.where(delta > 0, 0).rolling(14).mean()
        loss     = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi      = round(float(100 - (100 / (1 + gain/loss)).iloc[-1]), 2)

        avg_vol  = float(volume.rolling(20).mean().iloc[-1])
        vol_ratio = round(float(volume.iloc[-1]) / avg_vol, 2) if avg_vol > 0 else 1.0

        high_52  = round(float(close.max()), 2)
        low_52   = round(float(close.min()), 2)
        discount = round((high_52 - price) / high_52 * 100, 1)

        trend = "BULLISH" if price > ema50 > ema200 else ("BEARISH" if price < ema50 < ema200 else "NEUTRAL")

        stop    = round(price * 0.94, 2)
        risk    = round(price - stop, 2)
        target1 = round(price + risk * 1.5, 2)
        target2 = round(price + risk * 2.5, 2)
        qty     = round(min(50 / risk, 250 / price), 2) if risk > 0 else 1

        return {
            "ticker":      yahoo_ticker,
            "price":       price,
            "currency":    currency,
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
            "data_source": "YFINANCE"
        }
    except:
        return None

def get_live_price(ticker, yahoo_ticker=None):
    """Get just the current price — fastest call."""
    clean = ticker.upper().replace('_US_EQ','').replace('_EQ','')

    us_tickers = {
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
        "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
        "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
        "WMT","PG","XOM","CVX","NVO"
    }

    if clean in us_tickers:
        try:
            import apex_alpaca as alpaca
            price = alpaca.get_live_price(clean)
            if price:
                return price, "USD", "ALPACA"
        except:
            pass

    # yfinance fallback
    try:
        import yfinance as yf
        hist = yf.Ticker(yahoo_ticker or ticker).history(period="1d")
        if not hist.empty:
            price = float(hist['Close'].iloc[-1])
            currency = detect_currency(yahoo_ticker or ticker)
            if currency == "GBX" and price > 100:
                price = price / 100
            return round(price, 2), currency, "YFINANCE"
    except:
        pass

    return None, "USD", "ERROR"

if __name__ == '__main__':
    print("Testing unified price feed...\n")

    test_cases = [
        ("XOM",    "XOM",     None),
        ("AAPL",   "AAPL",    None),
        ("VUAG",   "VUAG.L",  None),
        ("SHEL",   "SHEL.L",  None),
        ("ASML",   "ASML.AS", None),
    ]

    print(f"{'Ticker':8} | {'Price':10} | {'Currency':8} | Source")
    print("-" * 50)
    for ticker, yahoo, _ in test_cases:
        price, currency, source = get_live_price(ticker, yahoo)
        print(f"{ticker:8} | £{price:8} | {currency:8} | {source}")
