#!/usr/bin/env python3
import yfinance as yf
import json
from datetime import datetime, timezone

WATCHLIST = {
    # Core Global ETFs
    "VWRP":  ("VWRP.L",    "GBP"),
    "VUAG":  ("VUAG.L",    "GBP"),
    "VFEA":  ("VFEA.L",    "GBP"),
    "IGWD":  ("IGWD.L",    "GBX"),
    "HMWO":  ("HMWO.L",    "GBX"),
    "CSPX":  ("CSPX.L",    "USD"),
    # Sector ETFs
    "IITU":  ("IITU.L",    "GBX"),
    "IUFS":  ("IUFS.L",    "GBP"),
    "IUHC":  ("IUHC.L",    "GBP"),
    "IUES":  ("IUES.L",    "GBP"),
    "IUCD":  ("IUCD.L",    "GBP"),
    "EQQQ":  ("EQQQ.L",    "GBX"),
    "ISF":   ("ISF.L",     "GBX"),
    # Thematic ETFs
    "HEAL":  ("HEAL.L",    "EUR"),
    "INRG":  ("INRG.L",    "GBX"),
    "WCLD":  ("WCLD.L",    "USD"),
    # Regional ETFs
    "VAPX":  ("VAPX.L",    "CHF"),
    "VJPN":  ("VJPN.L",    "EUR"),
    # Bonds + Commodities
    "VGOV":  ("VGOV.L",    "GBP"),
    "VAGS":  ("VAGS.L",    "GBP"),
    "SGLN":  ("SGLN.L",    "GBX"),
    "SSLN":  ("SSLN.L",    "GBX"),
    # UK FTSE 100 — Financials
    "HSBA":  ("HSBA.L",    "GBX"),
    "LLOY":  ("LLOY.L",    "GBX"),
    "BARC":  ("BARC.L",    "GBX"),
    "NWG":   ("NWG.L",     "GBX"),
    "PRU":   ("PRU.L",     "GBX"),
    "LGEN":  ("LGEN.L",    "GBX"),
    "AVIVA": ("AV.L",      "GBX"),
    # UK FTSE 100 — Energy
    "SHEL":  ("SHEL.L",    "GBX"),
    "BP":    ("BP.L",      "GBX"),
    "NG":    ("NG.L",      "GBX"),
    "SSE":   ("SSE.L",     "GBX"),
    # UK FTSE 100 — Healthcare/Consumer
    "AZN":   ("AZN.L",     "GBX"),
    "GSK":   ("GSK.L",     "GBX"),
    "ULVR":  ("ULVR.L",    "GBX"),
    "DGE":   ("DGE.L",     "GBX"),
    "IMB":   ("IMB.L",     "GBX"),
    "BATS":  ("BATS.L",    "GBX"),
    # UK FTSE 100 — Industrials/Other
    "BA":    ("BA.L",      "GBX"),
    "REL":   ("REL.L",     "GBX"),
    "RIO":   ("RIO.L",     "GBX"),
    "EXPN":  ("EXPN.L",    "GBX"),
    "CPG":   ("CPG.L",     "GBX"),
    "WPP":   ("WPP.L",     "GBX"),
    "VOD":   ("VOD.L",     "GBX"),
    "BT":    ("BT-A.L",    "GBX"),
    # European
    "AIR":   ("AIR.PA",    "EUR"),
    "LVMH":  ("MC.PA",     "EUR"),
    "SAN":   ("SAN.MC",    "EUR"),
    "NOVN":  ("NOVN.SW",   "CHF"),
    "ROG":   ("ROG.SW",    "CHF"),
    "TTE":   ("TTE.PA",    "EUR"),
    "ASML":  ("ASML.AS",   "EUR"),
    "SIE":   ("SIE.DE",    "EUR"),
    "NOVO":  ("NVO",       "USD"),
    # US Tech
    "AAPL":  ("AAPL",      "USD"),
    "MSFT":  ("MSFT",      "USD"),
    "NVDA":  ("NVDA",      "USD"),
    "GOOGL": ("GOOGL",     "USD"),
    "AMZN":  ("AMZN",      "USD"),
    "META":  ("META",      "USD"),
    "TSLA":  ("TSLA",      "USD"),
    "CRM":   ("CRM",       "USD"),
    "ORCL":  ("ORCL",      "USD"),
    "AMD":   ("AMD",       "USD"),
    "INTC":  ("INTC",      "USD"),
    "QCOM":  ("QCOM",      "USD"),
    # US Financials
    "JPM":   ("JPM",       "USD"),
    "GS":    ("GS",        "USD"),
    "MS":    ("MS",        "USD"),
    "BAC":   ("BAC",       "USD"),
    "BLK":   ("BLK",       "USD"),
    "AXP":   ("AXP",       "USD"),
    "C":     ("C",         "USD"),
    "V":     ("V",         "USD"),
    # US Healthcare
    "JNJ":   ("JNJ",       "USD"),
    "PFE":   ("PFE",       "USD"),
    "MRK":   ("MRK",       "USD"),
    "UNH":   ("UNH",       "USD"),
    "ABBV":  ("ABBV",      "USD"),
    "TMO":   ("TMO",       "USD"),
    "DHR":   ("DHR",       "USD"),
    # US Defensive/Dividend
    "KO":    ("KO",        "USD"),
    "PEP":   ("PEP",       "USD"),
    "MCD":   ("MCD",       "USD"),
    "WMT":   ("WMT",       "USD"),
    "PG":    ("PG",        "USD"),
    "XOM":   ("XOM",       "USD"),
    "CVX":   ("CVX",       "USD"),
}

SECTOR_MAP = {
    # US Tech
    "AAPL": "IITU", "MSFT": "IITU", "NVDA": "IITU",
    "GOOGL":"IITU", "AMZN": "IITU", "META": "IITU",
    "TSLA": "IITU", "CRM":  "IITU", "ORCL": "IITU",
    "AMD":  "IITU", "INTC": "IITU", "QCOM": "IITU",
    "ASML": "IITU",
    # US Financials
    "JPM":  "IUFS", "GS":   "IUFS", "MS":   "IUFS",
    "BAC":  "IUFS", "BLK":  "IUFS", "AXP":  "IUFS",
    "C":    "IUFS", "V":    "IUFS",
    # US Healthcare
    "JNJ":  "IUHC", "PFE":  "IUHC", "MRK":  "IUHC",
    "UNH":  "IUHC", "ABBV": "IUHC", "TMO":  "IUHC",
    "DHR":  "IUHC", "NOVO": "IUHC",
    # UK/EU Healthcare
    "AZN":  "IUHC", "GSK":  "IUHC", "NOVN": "IUHC",
    "ROG":  "IUHC",
    # Energy
    "SHEL": "IUES", "BP":   "IUES", "XOM":  "IUES",
    "CVX":  "IUES", "TTE":  "IUES", "NG":   "IUES",
    "SSE":  "IUES",
    # UK/EU via HMWO
    "HSBA": "HMWO", "LLOY": "HMWO", "BARC": "HMWO",
    "NWG":  "HMWO", "PRU":  "HMWO", "LGEN": "HMWO",
    "AVIVA":"HMWO", "ULVR": "HMWO", "DGE":  "HMWO",
    "IMB":  "HMWO", "BATS": "HMWO", "BA":   "HMWO",
    "REL":  "HMWO", "RIO":  "HMWO", "EXPN": "HMWO",
    "CPG":  "HMWO", "WPP":  "HMWO", "VOD":  "HMWO",
    "BT":   "HMWO", "AIR":  "HMWO", "LVMH": "HMWO",
    "SAN":  "HMWO", "SIE":  "HMWO",
    # US Consumer/Defensive via IUCD
    "MCD":  "IUCD", "WMT":  "IUCD", "PG":   "IUCD",
    "KO":   "IUCD", "PEP":  "IUCD",
}

def fix_pence(price, currency):
    if currency == "GBX" and price > 100:
        return round(price / 100, 2)
    return price

def get_technicals(name, yahoo_ticker, currency):
    try:
        t = yf.Ticker(yahoo_ticker)
        hist = t.history(period="6mo")
        if hist.empty:
            return {"name": name, "error": "No data"}

        close  = hist["Close"].apply(lambda x: fix_pence(x, currency))
        volume = hist["Volume"]

        price  = round(float(close.iloc[-1]), 2)
        ema50  = round(float(close.ewm(span=50).mean().iloc[-1]), 2)

        delta  = close.diff()
        gain   = delta.where(delta > 0, 0).rolling(14).mean()
        loss   = -delta.where(delta < 0, 0).rolling(14).mean()
        rs     = gain / loss
        rsi    = round(float(100 - (100 / (1 + rs.iloc[-1]))), 2)

        ema12       = close.ewm(span=12).mean()
        ema26       = close.ewm(span=26).mean()
        macd_line   = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        histogram   = macd_line - signal_line
        macd_now    = float(histogram.iloc[-1])
        macd_prev   = float(histogram.iloc[-2])
        macd_rising = macd_now > macd_prev

        avg_vol   = float(volume.rolling(20).mean().iloc[-1])
        vol_ratio = round(float(volume.iloc[-1]) / avg_vol, 2)

        hist_1y = t.history(period="1y")
        high_52 = fix_pence(round(float(hist_1y["Close"].max()), 2), currency)
        low_52  = fix_pence(round(float(hist_1y["Close"].min()), 2), currency)

        trend       = "BULLISH" if price > ema50 else "BEARISH"
        trend_score = WEIGHT_TREND if trend == "BULLISH" else 0
        rsi_score   = WEIGHT_RSI if 45 <= rsi <= 70 else (1 if 35 <= rsi < 45 or 70 < rsi <= 80 else 0)
        vol_score   = WEIGHT_VOLUME if vol_ratio >= 1.0 else 1
        
        macd_score  = WEIGHT_MACD if macd_now > 0 and macd_rising else (round(WEIGHT_MACD/2) if macd_now > 0 else 0)
        total       = trend_score + rsi_score + vol_score + macd_score

        return {
            "name":           name,
            "ticker":         yahoo_ticker,
            "currency":       currency,
            "price":          price,
            "ema50":          ema50,
            "rsi":            rsi,
            "macd_hist":      round(macd_now, 4),
            "macd_rising":    macd_rising,
            "trend":          trend,
            "volume_ratio":   vol_ratio,
            "week52_high":    high_52,
            "week52_low":     low_52,
            "trend_score":    trend_score,
            "rsi_score":      rsi_score,
            "vol_score":      vol_score,
            "macd_score":     macd_score,
            "total_score":    total,
            "max_score":      10,
            "sector_etf":     SECTOR_MAP.get(name, "none"),
            "sector_blocked": False,
            "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }
    except Exception as e:
        return {"name": name, "ticker": yahoo_ticker, "error": str(e)}

print(f"Scanning {len(WATCHLIST)} instruments...", flush=True)
results = []
for name, (ticker, currency) in WATCHLIST.items():
    print(f"  {name}...", flush=True)
    results.append(get_technicals(name, ticker, currency))

print("Checking sector health...", flush=True)
sector_trends = {}
for r in results:
    etf_name = r.get("name")
    if etf_name in ["IITU","IUFS","IUHC","IUES","IUCD","HMWO","VUAG","VWRP","CSPX","ISF"]:
        sector_trends[etf_name] = r.get("trend", "BEARISH")

sector_blocks = []
for r in results:
    if "error" in r:
        continue
    sector = r.get("sector_etf", "none")
    if sector != "none":
        sector_trend = sector_trends.get(sector, "BEARISH")
        if sector_trend == "BEARISH":
            r["sector_blocked"] = True
            r["sector_block_reason"] = f"{sector} is BEARISH"
            sector_blocks.append(r["name"])

results.sort(key=lambda x: x.get("total_score", 0), reverse=True)

print(f"\n=== SECTOR HEALTH ===")
for etf, trend in sorted(sector_trends.items()):
    icon = "✅" if trend == "BULLISH" else "❌"
    print(f"  {icon} {etf}: {trend}")

print(f"\n=== TOP 15 INSTRUMENTS (unblocked) ===")
unblocked = [r for r in results if not r.get("sector_blocked") and "error" not in r]
for r in unblocked[:15]:
    macd_arrow = "↑" if r.get("macd_rising") else "↓"
    print(f"{r['name']:6} | {r['total_score']}/10 | RSI:{r['rsi']:5} | MACD:{r['macd_hist']:+.4f}{macd_arrow} | {r['trend']:7} | {r['price']} {r['currency']}")

print(f"\nTotal: {len(results)} | Unblocked: {len(unblocked)} | Blocked: {len(sector_blocks)}")
print("\n=== FULL DATA ===")
print(json.dumps(results, indent=2))

# Dynamic weight loading — reads from weights file if available
import os as _os
_WEIGHTS_FILE = '/home/ubuntu/.picoclaw/logs/apex-weights.json'
try:
    with open(_WEIGHTS_FILE) as _f:
        _w = json.load(_f)
    WEIGHT_TREND  = _w.get('trend',  3)
    WEIGHT_RSI    = _w.get('rsi',    3)
    WEIGHT_VOLUME = _w.get('volume', 2)
    WEIGHT_MACD   = _w.get('macd',   2)
    MAX_SCORE     = WEIGHT_TREND + WEIGHT_RSI + WEIGHT_VOLUME + WEIGHT_MACD
except:
    WEIGHT_TREND  = 3
    WEIGHT_RSI    = 3
    WEIGHT_VOLUME = 2
    WEIGHT_MACD   = 2
    MAX_SCORE     = 10
