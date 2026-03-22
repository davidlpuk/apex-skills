#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
import yfinance as yf
try:
    import apex_price_feed as price_feed
    USE_PRICE_FEED = True
except:
    USE_PRICE_FEED = False
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


QUALITY_FILE   = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'
TICKER_MAP     = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'
GEO_FILE       = '/home/ubuntu/.picoclaw/logs/apex-geo-news.json'
WEIGHTS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-weights.json'

YAHOO_MAP = {
    "AAPL": "AAPL",   "MSFT": "MSFT",   "GOOGL": "GOOGL",
    "AMZN": "AMZN",   "NVDA": "NVDA",   "META": "META",
    "JPM":  "JPM",    "JNJ":  "JNJ",    "AZN":  "AZN.L",
    "ASML": "ASML.AS","NOVO": "NVO",    "XOM":  "XOM",
    "CVX":  "CVX",    "SHEL": "SHEL.L", "V":    "V",
    "UNH":  "UNH",    "ABBV": "ABBV",   "GSK":  "GSK.L",
    "ULVR": "ULVR.L", "REL":  "REL.L",  "BA":   "BA.L",
    "HSBA": "HSBA.L", "LGEN": "LGEN.L", "IMB":  "IMB.L",
    "BATS": "BATS.L",
}

CURRENCY_MAP = {
    "AZN": "GBX", "SHEL": "GBX", "GSK": "GBX", "ULVR": "GBX",
    "REL": "GBX", "BA":   "GBX", "HSBA":"GBX", "LGEN": "GBX",
    "IMB": "GBX", "BATS": "GBX",
}

def fix_pence(price, currency):
    if currency == "GBX" and price > 100:
        return round(price / 100, 2)
    return price

def load_geo_favourites():
    try:
        with open(GEO_FILE) as f:
            geo = json.load(f)
        with open(QUALITY_FILE) as f:
            quality = json.load(f)

        if geo.get('overall') == 'ALERT':
            energy_flags = geo.get('energy_flags', [])
            if energy_flags:
                # Iran/Middle East conflict detected
                return quality['geo_event_map']['iran_war']['favour']
        return []
    except:
        return []

def score_contrarian(name, yahoo_ticker, currency, quality_score):
    try:
        t    = yf.Ticker(yahoo_ticker)
        hist = t.history(period="1y")
        if hist.empty or len(hist) < 50:
            return None

        close  = hist['Close'].apply(lambda x: fix_pence(x, currency))
        volume = hist['Volume']

        price    = round(float(close.iloc[-1]), 2)
        high_52  = fix_pence(round(float(close.max()), 2), currency)
        low_52   = fix_pence(round(float(close.min()), 2), currency)
        ema200   = round(float(close.ewm(span=200).mean().iloc[-1]), 2)
        ema50    = round(float(close.ewm(span=50).mean().iloc[-1]), 2)

        # RSI 14
        delta  = close.diff()
        gain   = delta.where(delta > 0, 0).rolling(14).mean()
        loss   = -delta.where(delta < 0, 0).rolling(14).mean()
        rs     = gain / loss
        rsi    = round(float(100 - (100 / (1 + rs.iloc[-1]))), 2)

        # MACD
        ema12       = close.ewm(span=12).mean()
        ema26       = close.ewm(span=26).mean()
        macd_hist   = float((ema12 - ema26 - (ema12 - ema26).ewm(span=9).mean()).iloc[-1])
        macd_rising = macd_hist > float((ema12 - ema26 - (ema12 - ema26).ewm(span=9).mean()).iloc[-2])

        # Discount from 52-week high
        discount_pct = round((high_52 - price) / high_52 * 100, 1)

        # Distance from 52-week low
        above_low_pct = round((price - low_52) / low_52 * 100, 1)

        # Contrarian scoring — different logic to trend following
        score = 0
        reasons = []

        # RSI deeply oversold on quality name = strong buy signal
        if rsi <= 25:
            score += 4
            reasons.append(f"RSI {rsi} — deeply oversold")
        elif rsi <= 32:
            score += 3
            reasons.append(f"RSI {rsi} — oversold")
        elif rsi <= 38:
            score += 2
            reasons.append(f"RSI {rsi} — approaching oversold")

        # Significant discount from 52-week high
        if discount_pct >= 25:
            score += 3
            reasons.append(f"Down {discount_pct}% from 52w high")
        elif discount_pct >= 15:
            score += 2
            reasons.append(f"Down {discount_pct}% from 52w high")
        elif discount_pct >= 10:
            score += 1
            reasons.append(f"Down {discount_pct}% from 52w high")

        # Quality score bonus
        if quality_score >= 9:
            score += 2
            reasons.append(f"Tier-1 quality (score {quality_score}/10)")
        elif quality_score >= 7:
            score += 1
            reasons.append(f"High quality (score {quality_score}/10)")

        # MACD turning — early reversal signal
        if macd_rising and macd_hist > -0.5:
            score += 1
            reasons.append("MACD turning — early reversal signal")

        # Price near 52-week low but not breaking down
        if above_low_pct <= 5:
            score += 1
            reasons.append(f"Near 52w low — potential support")

        return {
            "name":          name,
            "ticker":        yahoo_ticker,
            "currency":      currency,
            "price":         price,
            "rsi":           rsi,
            "ema50":         ema50,
            "ema200":        ema200,
            "high_52":       high_52,
            "low_52":        low_52,
            "discount_pct":  discount_pct,
            "above_low_pct": above_low_pct,
            "macd_hist":     round(macd_hist, 4),
            "macd_rising":   macd_rising,
            "quality_score": quality_score,
            "contrarian_score": score,
            "max_score":     10,
            "reasons":       reasons,
            "signal_type":   "CONTRARIAN",
            "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

    except Exception as e:
        return None

def run():
    with open(QUALITY_FILE) as f:
        quality_db = json.load(f)

    geo_favs = load_geo_favourites()
    quality  = quality_db['quality_stocks']

    results  = []
    geo_opps = []

    print(f"Scanning {len(quality)} quality instruments for contrarian opportunities...", flush=True)

    for name, data in quality.items():
        # Skip value traps identified in backtest
        if data.get('contrarian_skip', False):
            print(f"  {name}... SKIPPED (backtest: {data.get('contrarian_note','')})", flush=True)
            continue

        yahoo   = YAHOO_MAP.get(name, name)
        currency = CURRENCY_MAP.get(name, "USD")
        qs      = data.get('quality_score', 5)

        # Boost preferred contrarian instruments
        if data.get('contrarian_preferred', False):
            qs = min(10, qs + 1)

        print(f"  {name}...", flush=True)
        result = score_contrarian(name, yahoo, currency, qs)

        if result:
            # Geo-reversal boost
            if name in geo_favs:
                result['contrarian_score'] += 2
                result['reasons'].append("Geo-reversal: energy beneficiary during Middle East conflict")
                result['geo_favoured'] = True
                geo_opps.append(name)
            else:
                result['geo_favoured'] = False

            results.append(result)

    # Sort by contrarian score
    results.sort(key=lambda x: x.get('contrarian_score', 0), reverse=True)

    # Save
    atomic_write('/home/ubuntu/.picoclaw/logs/apex-contrarian-signals.json', results)

    # Print top opportunities
    print(f"\n=== TOP CONTRARIAN OPPORTUNITIES ===")
    qualifying = [r for r in results if r['contrarian_score'] >= 6]

    if not qualifying:
        print("No contrarian opportunities meet threshold (6+/10)")
        # Show top 5 anyway
        for r in results[:5]:
            print(f"{r['name']:6} | C-Score: {r['contrarian_score']}/10 | RSI: {r['rsi']:5} | Discount: {r['discount_pct']}% | {'⭐ GEO FAV' if r.get('geo_favoured') else ''}")
    else:
        print(f"{len(qualifying)} opportunities qualifying (score 6+/10):")
        for r in qualifying[:8]:
            geo_tag = "⭐ GEO" if r.get('geo_favoured') else ""
            print(f"{r['name']:6} | C-Score: {r['contrarian_score']}/10 | RSI: {r['rsi']:5} | Discount: {r['discount_pct']}% | {geo_tag}")
            for reason in r['reasons'][:2]:
                print(f"       → {reason}")

    if geo_opps:
        print(f"\n⭐ Geo-reversal opportunities: {', '.join(geo_opps)}")
        print(f"   These instruments BENEFIT from current geopolitical situation")

    print(f"\n=== FULL DATA ===")
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    run()
