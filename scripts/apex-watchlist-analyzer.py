#!/usr/bin/env python3
"""
Apex Watchlist Analyzer
Reads watchlist from TRADING_STATE.md, fetches live data via yfinance,
scores signal readiness, and saves to apex-watchlist-analysis.json.
Also flags which tickers are NOT in the Apex decision engine scanner.
"""
import yfinance as yf
import json
import sys
import re
from datetime import datetime, timezone

LOGS       = '/home/ubuntu/.picoclaw/logs'
SCRIPTS    = '/home/ubuntu/.picoclaw/scripts'
STATE_FILE = '/home/ubuntu/.picoclaw/workspace/skills/apex-trading/TRADING_STATE.md'
OUTPUT     = f'{LOGS}/apex-watchlist-analysis.json'

# ------------------------------------------------------------------
# Ticker → (yahoo_ticker, display_name, sector, currency)
# ------------------------------------------------------------------
TICKER_META = {
    # ETFs (London)
    "VWRP":  ("VWRP.L",  "Vanguard FTSE All-World (Acc)",  "ETF/World",       "GBP"),
    "VUAG":  ("VUAG.L",  "Vanguard S&P 500 (Acc)",         "ETF/US",          "GBP"),
    "VFEG":  ("VFEG.L",  "Vanguard FTSE Em. Mkt ETF",      "ETF/EM",          "GBP"),
    "IUIT":  ("IUIT.L",  "iShares US IT ETF",              "ETF/Tech",        "GBP"),
    # Small/mid caps
    "NBIS":  ("NBIS",    "Neurobiosys",                    "Biotech",         "USD"),
    "RLKB":  ("RLKB",    "Rock Lake Biotech",              "Biotech",         "USD"),
    "WIX":   ("WIX",     "Wix.com",                        "Tech/SaaS",       "USD"),
    "HROW":  ("HROW",    "Harrow Inc.",                    "Healthcare",      "USD"),
    "DLO":   ("DLO",     "DLocal",                         "FinTech",         "USD"),
    "NRXS":  ("NRXS",    "NRx Pharmaceuticals",            "Biotech",         "USD"),
    "OMDA":  ("OMDA",    "Omada Health",                   "HealthTech",      "USD"),
    "PGY":   ("PGY",     "Pagaya Technologies",            "FinTech/AI",      "USD"),
    "CLPT":  ("CLPT",    "ClearPoint Neuro",               "MedTech",         "USD"),
    # Large caps — growth
    "VRT":   ("VRT",     "Vertiv Holdings",                "Industrials/AI",  "USD"),
    "APP":   ("APP",     "AppLovin",                       "AdTech/AI",       "USD"),
    "ALAB":  ("ALAB",    "Astera Labs",                    "Semiconductors",  "USD"),
    "PLTR":  ("PLTR",    "Palantir",                       "AI/Data",         "USD"),
    "IREN":  ("IREN",    "IREN Ltd",                       "Crypto Mining",   "USD"),
    "CRDO":  ("CRDO",    "Credo Technology",               "Semiconductors",  "USD"),
    # Power / Nuclear / Utilities
    "CEG":   ("CEG",     "Constellation Energy",           "Nuclear/Power",   "USD"),
    "VST":   ("VST",     "Vistra Corp",                    "Power/Nuclear",   "USD"),
    "NEE":   ("NEE",     "NextEra Energy",                 "Utilities",       "USD"),
    "PPL":   ("PPL",     "PPL Corporation",                "Utilities",       "USD"),
    "XEL":   ("XEL",     "Xcel Energy",                    "Utilities",       "USD"),
    "GEV":   ("GEV",     "GE Vernova",                     "Energy/Turbines", "USD"),
    # Energy
    "DVN":   ("DVN",     "Devon Energy",                   "Energy",          "USD"),
    "COP":   ("COP",     "ConocoPhillips",                 "Energy",          "USD"),
    "CCJ":   ("CCJ",     "Cameco Corp",                    "Uranium",         "USD"),
    "ENB":   ("ENB",     "Enbridge",                       "Energy/Pipelines","USD"),
    "NFE":   ("NFE",     "New Fortress Energy",            "LNG",             "USD"),
    # Networking / Cyber
    "ANET":  ("ANET",    "Arista Networks",                "Networking",      "USD"),
    "LITE":  ("LITE",    "Lumentum Holdings",              "Photonics",       "USD"),
    "PANW":  ("PANW",    "Palo Alto Networks",             "Cybersecurity",   "USD"),
    "CRWD":  ("CRWD",    "CrowdStrike",                    "Cybersecurity",   "USD"),
    # Crypto mining
    "CLSK":  ("CLSK",    "Cleanspark",                     "Crypto Mining",   "USD"),
    "RIOT":  ("RIOT",    "Riot Platforms",                 "Crypto Mining",   "USD"),
}

# Tickers currently in the Apex decision engine scanner
def _load_scanner_universe():
    try:
        content = open(f'{SCRIPTS}/apex-market-data.py').read()
        keys = re.findall(r'"([A-Z0-9]+)"\s*:\s*\(', content)
        return set(keys)
    except Exception:
        return set()

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)

def _macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    def ema(data, n):
        e = [data[0]]
        k = 2 / (n + 1)
        for v in data[1:]:
            e.append(v * k + e[-1] * (1 - k))
        return e
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast[slow-fast:], ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line[-signal:], signal_line[-signal:])]
    return round(macd_line[-1], 4), round(signal_line[-1], 4), round(hist[-1], 4)

def _atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if not trs:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)

def _signal_readiness(rsi, macd_hist, price, sma20, sma50, sma200,
                       week52_low, week52_high, vol, avg_vol):
    """Score 0–10 how close this stock is to an Apex entry signal."""
    score = 5.0  # neutral baseline

    # RSI component (most important)
    if rsi is not None:
        if rsi < 15:   score += 3.5
        elif rsi < 20: score += 3.0
        elif rsi < 25: score += 2.5
        elif rsi < 30: score += 2.0
        elif rsi < 35: score += 1.5
        elif rsi < 40: score += 1.0
        elif rsi < 45: score += 0.5
        elif rsi > 80: score -= 2.5
        elif rsi > 70: score -= 1.5
        elif rsi > 60: score -= 0.5

    # MACD histogram turning (momentum)
    if macd_hist is not None:
        if macd_hist > 0.5:   score += 0.5
        elif macd_hist > 0:   score += 0.25
        elif macd_hist < -0.5: score -= 0.5
        elif macd_hist < 0:   score -= 0.25

    # Price vs 52-week range
    if week52_low and week52_high and price:
        range_pos = (price - week52_low) / (week52_high - week52_low) if week52_high > week52_low else 0.5
        if range_pos < 0.1:   score += 1.0   # near 52wk low — capitulation zone
        elif range_pos < 0.2: score += 0.5
        elif range_pos > 0.9: score -= 1.0   # near 52wk high — extended
        elif range_pos > 0.8: score -= 0.5

    # Trend (SMA)
    if sma200 and price:
        if price < sma200 * 0.85: score += 0.5  # deeply below 200 SMA
        elif price < sma200:      score += 0.25
        elif price > sma200 * 1.15: score -= 0.5

    # Volume confirmation
    if vol and avg_vol and avg_vol > 0:
        vol_ratio = vol / avg_vol
        if vol_ratio > 2.0: score += 0.5   # volume spike = potential exhaustion
        elif vol_ratio > 1.5: score += 0.25

    return max(0.0, min(10.0, round(score, 1)))

def analyze_ticker(short_name, meta):
    yahoo_ticker, display_name, sector, currency = meta
    result = {
        "ticker":        short_name,
        "yahoo":         yahoo_ticker,
        "name":          display_name,
        "sector":        sector,
        "currency":      currency,
        "price":         None,
        "change_pct":    None,
        "rsi":           None,
        "macd_hist":     None,
        "sma20":         None,
        "sma50":         None,
        "sma200":        None,
        "atr":           None,
        "week52_low":    None,
        "week52_high":   None,
        "volume":        None,
        "avg_volume":    None,
        "signal_readiness": 5.0,
        "tags":          [],
        "error":         None,
    }
    try:
        tk = yf.Ticker(yahoo_ticker)
        # 6 months of daily data — enough for all indicators
        hist = tk.history(period='6mo', interval='1d', auto_adjust=True)
        if hist.empty or len(hist) < 30:
            result['error'] = 'insufficient_data'
            return result

        closes  = list(hist['Close'])
        highs   = list(hist['High'])
        lows    = list(hist['Low'])
        volumes = list(hist['Volume'])

        price       = round(closes[-1], 4)
        prev_close  = closes[-2] if len(closes) > 1 else closes[-1]
        change_pct  = round((price - prev_close) / prev_close * 100, 2)

        rsi         = _rsi(closes)
        macd_l, macd_s, macd_h = _macd(closes)
        atr         = _atr(highs, lows, closes)

        sma20  = round(sum(closes[-20:]) / 20, 4)  if len(closes) >= 20  else None
        sma50  = round(sum(closes[-50:]) / 50, 4)  if len(closes) >= 50  else None
        sma200 = round(sum(closes[-200:]) / 200, 4) if len(closes) >= 200 else None

        week52_low  = round(min(lows[-252:])  if len(lows) >= 252 else min(lows), 4)
        week52_high = round(max(highs[-252:]) if len(highs) >= 252 else max(highs), 4)
        volume      = int(volumes[-1])
        avg_volume  = int(sum(volumes[-20:]) / min(20, len(volumes)))

        readiness = _signal_readiness(
            rsi, macd_h, price, sma20, sma50, sma200,
            week52_low, week52_high, volume, avg_volume
        )

        # Human-readable tags
        tags = []
        if rsi is not None:
            if rsi < 20:   tags.append("DEEPLY OVERSOLD")
            elif rsi < 30: tags.append("OVERSOLD")
            elif rsi > 80: tags.append("OVERBOUGHT")
            elif rsi > 70: tags.append("ELEVATED RSI")
        if sma200 and price < sma200: tags.append("BELOW 200SMA")
        if sma200 and price > sma200: tags.append("ABOVE 200SMA")
        if week52_low and price and price <= week52_low * 1.05: tags.append("NEAR 52WK LOW")
        if week52_high and price and price >= week52_high * 0.97: tags.append("NEAR 52WK HIGH")
        if macd_h and macd_h > 0: tags.append("MACD ↑")
        if macd_h and macd_h < 0: tags.append("MACD ↓")
        if avg_volume and volume > avg_volume * 1.5: tags.append("HIGH VOLUME")

        result.update({
            "price":          price,
            "change_pct":     change_pct,
            "rsi":            rsi,
            "macd_hist":      macd_h,
            "sma20":          sma20,
            "sma50":          sma50,
            "sma200":         sma200,
            "atr":            atr,
            "week52_low":     week52_low,
            "week52_high":    week52_high,
            "volume":         volume,
            "avg_volume":     avg_volume,
            "signal_readiness": readiness,
            "tags":           tags,
        })
    except Exception as e:
        result['error'] = str(e)[:80]

    return result

def parse_watchlist_from_state():
    """Extract tickers from TRADING_STATE.md — lines that look like plain ticker symbols."""
    tickers = []
    try:
        with open(STATE_FILE) as f:
            content = f.read()
        # Find everything after the last ## heading that contains tickers
        ticker_pattern = re.compile(r'^([A-Z][A-Z0-9]{1,5})$', re.MULTILINE)
        tickers = ticker_pattern.findall(content)
        # Filter out markdown headings words / common English
        exclude = {'IMPORTANT', 'ALWAYS', 'NEVER', 'ACCOUNT', 'RISK', 'RULES', 'SCRIPTS',
                   'DO', 'NOT', 'ALL', 'UK', 'CGT', 'GBP', 'USD', 'ETF'}
        tickers = [t for t in tickers if t not in exclude]
    except Exception:
        pass
    return list(dict.fromkeys(tickers))  # deduplicate, preserve order

def run():
    print(f"=== APEX WATCHLIST ANALYZER ===")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    scanner_universe = _load_scanner_universe()
    watchlist_tickers = parse_watchlist_from_state()
    print(f"Watchlist: {len(watchlist_tickers)} tickers")

    results = []
    for ticker in watchlist_tickers:
        meta = TICKER_META.get(ticker)
        if not meta:
            # Unknown ticker — try as-is on Yahoo
            meta = (ticker, ticker, "Unknown", "USD")

        print(f"  Analyzing {ticker}...", end=' ', flush=True)
        analysis = analyze_ticker(ticker, meta)
        analysis['in_apex_scanner'] = ticker in scanner_universe

        if analysis.get('error'):
            print(f"⚠ {analysis['error']}")
        else:
            rsi_str = f"RSI {analysis['rsi']}" if analysis['rsi'] else "—"
            print(f"✅ {rsi_str} | readiness={analysis['signal_readiness']}")

        results.append(analysis)

    # Sort: highest readiness first
    results.sort(key=lambda x: x['signal_readiness'], reverse=True)

    output = {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "total":         len(results),
        "top_signal":    results[0]['ticker'] if results else None,
        "watchlist":     results,
    }

    with open(OUTPUT, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ Analysis saved — {len(results)} tickers")
    print(f"Top signal readiness: {results[0]['ticker']} ({results[0]['signal_readiness']}/10)" if results else "")

    # Highlight high-readiness (≥7.5) for Telegram notification
    hot = [r for r in results if r['signal_readiness'] >= 7.5 and not r.get('error')]
    if hot:
        try:
            sys.path.insert(0, SCRIPTS)
            from apex_utils import send_telegram
            lines = ["👀 *WATCHLIST ALERT — Stocks Near Entry*\n"]
            for r in hot[:5]:
                rsi_str = f"RSI {r['rsi']:.0f}" if r['rsi'] else "—"
                lines.append(f"• *{r['ticker']}* ({r['name']}) — readiness {r['signal_readiness']}/10 | {rsi_str}")
                if r.get('tags'):
                    lines.append(f"  _{', '.join(r['tags'][:3])}_")
            lines.append("\nReply WATCHLIST to see full analysis on dashboard.")
            send_telegram('\n'.join(lines))
            print(f"Telegram alert sent — {len(hot)} hot ticker(s)")
        except Exception as e:
            print(f"Telegram alert failed: {e}")

if __name__ == '__main__':
    run()
