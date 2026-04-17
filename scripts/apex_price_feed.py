#!/usr/bin/env python3
"""
Unified price feed — Alpaca for US stocks, yfinance for everything else.
Drop-in replacement for yfinance calls throughout Apex.
"""
import sys
import os

# Yahoo-symbol resolution map.  Mirrors WATCHLIST_YAHOO in
# `apex-staleness-check.py` — keep the two in sync when adding instruments.
# Used by the yfinance fallback to convert a clean equity name (e.g. "INRG")
# or T212 ticker (e.g. "INRGl_EQ") to a Yahoo-recognised symbol (e.g. "INRG.L").
# Without this resolution the fallback would call yf.Ticker("INRG") or
# yf.Ticker("INRGL_EQ") — both 404 on Yahoo for LSE/EU instruments.
_YAHOO_MAP = {
    "VWRP": "VWRP.L", "VUAG": "VUAG.L", "VFEA": "VFEA.L",
    "IGWD": "IGWD.L", "HMWO": "HMWO.L", "IITU": "IITU.L",
    "IUFS": "IUFS.L", "IUHC": "IUHC.L", "IUES": "IUES.L",
    "IUCD": "IUCD.L", "SGLN": "SGLN.L", "SSLN": "SSLN.L",
    "ISF":  "ISF.L",  "CSPX": "CSPX.L", "EQQQ": "EQQQ.L",
    "HEAL": "HEAL.L", "INRG": "INRG.L", "WCLD": "WCLD.L",
    "VAPX": "VAPX.L", "VJPN": "VJPN.L", "VGOV": "VGOV.L",
    "VAGS": "VAGS.L", "HSBA": "HSBA.L", "SHEL": "SHEL.L",
    "AZN":  "AZN.L",  "ULVR": "ULVR.L", "GSK":  "GSK.L",
    "LLOY": "LLOY.L", "BP":   "BP.L",   "RIO":  "RIO.L",
    "BA":   "BA.L",   "REL":  "REL.L",  "BARC": "BARC.L",
    "NWG":  "NWG.L",  "PRU":  "PRU.L",  "NG":   "NG.L",
    "SSE":  "SSE.L",  "DGE":  "DGE.L",  "IMB":  "IMB.L",
    "BATS": "BATS.L", "EXPN": "EXPN.L", "CPG":  "CPG.L",
    "WPP":  "WPP.L",  "VOD":  "VOD.L",  "BT":   "BT-A.L",
    "AVIVA":"AV.L",   "AIR":  "AIR.PA", "LVMH": "MC.PA",
    "SAN":  "SAN.MC", "NOVN": "NOVN.SW","ROG":  "ROG.SW",
    "TTE":  "TTE.PA", "ASML": "ASML.AS","SIE":  "SIE.DE",
    "AAPL": "AAPL",   "MSFT": "MSFT",   "NVDA": "NVDA",
    "GOOGL":"GOOGL",  "AMZN": "AMZN",   "META": "META",
    "TSLA": "TSLA",   "CRM":  "CRM",    "ORCL": "ORCL",
    "AMD":  "AMD",    "INTC": "INTC",   "QCOM": "QCOM",
    "JPM":  "JPM",    "GS":   "GS",     "MS":   "MS",
    "BAC":  "BAC",    "BLK":  "BLK",    "AXP":  "AXP",
    "C":    "C",      "V":    "V",      "JNJ":  "JNJ",
    "PFE":  "PFE",    "MRK":  "MRK",    "UNH":  "UNH",
    "ABBV": "ABBV",   "TMO":  "TMO",    "DHR":  "DHR",
    "KO":   "KO",     "PEP":  "PEP",    "MCD":  "MCD",
    "WMT":  "WMT",    "PG":   "PG",     "XOM":  "XOM",
    "CVX":  "CVX",    "NOVO": "NVO",
}


def _resolve_yahoo(clean, ticker):
    """
    Best-effort resolution of a Yahoo symbol given:
      clean  — equity name with T212 suffix already stripped (e.g. "INRG", "AAPL")
      ticker — original raw ticker, possibly a T212 form (e.g. "INRGl_EQ", "AAPL_US_EQ")

    Returns a Yahoo-recognised string, or None if no mapping is known. Callers
    should NOT pass the unresolved ticker to yfinance when this returns None —
    that produces guaranteed-404 noise in the logs (e.g. "INRGL_EQ" lookups).
    """
    if not clean and not ticker:
        return None
    # Try direct map hit on the cleaned name first.
    if clean and clean in _YAHOO_MAP:
        return _YAHOO_MAP[clean]
    # If raw ticker is already in Yahoo form (contains a dot, e.g. "INRG.L"),
    # accept it as-is.
    if ticker and "." in ticker and "_" not in ticker:
        return ticker
    return None


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

    clean_ticker = ticker.upper().replace('_US_EQ','').replace('L_EQ','').replace('_EQ','')

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
        resolved = yahoo_ticker or _resolve_yahoo(clean_ticker, ticker) or clean_ticker
        return get_yfinance_data(resolved, "USD")
    else:
        # Use yfinance for non-US instruments — resolve via map so a raw T212
        # ticker like "INRGl_EQ" becomes "INRG.L" instead of triggering a 404.
        resolved = yahoo_ticker or _resolve_yahoo(clean_ticker, ticker)
        if not resolved:
            return None
        return get_yfinance_data(resolved, detect_currency(resolved))

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
    if currency == "GBX":
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
    clean = ticker.upper().replace('_US_EQ','').replace('L_EQ','').replace('_EQ','')

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

    # yfinance fallback — resolve to a Yahoo-recognised symbol first.  Without
    # this, a raw T212 ticker like "INRGl_EQ" would be passed straight to
    # yfinance and produce a guaranteed 404 ("Quote not found for symbol:
    # INRGL_EQ"), polluting the cron log and silently returning None.
    resolved = yahoo_ticker or _resolve_yahoo(clean, ticker)
    if not resolved:
        return None, "USD", "NO_YAHOO_MAP"
    try:
        import yfinance as yf
        hist = yf.Ticker(resolved).history(period="1d")
        if not hist.empty:
            price = float(hist['Close'].iloc[-1])
            currency = detect_currency(resolved)
            if currency == "GBX":
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
