#!/usr/bin/env python3
"""
Dividend capture calendar.
Scans high-yield FTSE 100 and US dividend stocks for upcoming
ex-dividend dates. Flags opportunities to buy before ex-date
and capture the dividend.
"""
import yfinance as yf
import json
from datetime import datetime, timezone, timedelta
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


DIVIDEND_FILE = '/home/ubuntu/.picoclaw/logs/apex-dividend-capture.json'

# High yield dividend candidates
DIVIDEND_UNIVERSE = {
    # UK FTSE — high yielders
    "LGEN":  {"yahoo": "LGEN.L",  "name": "Legal & General",  "est_yield": 8.1},
    "IMB":   {"yahoo": "IMB.L",   "name": "Imperial Brands",  "est_yield": 10.2},
    "BATS":  {"yahoo": "BATS.L",  "name": "BAT",              "est_yield": 9.8},
    "AVIVA": {"yahoo": "AV.L",    "name": "Aviva",            "est_yield": 7.2},
    "LLOY":  {"yahoo": "LLOY.L",  "name": "Lloyds",           "est_yield": 5.8},
    "HSBA":  {"yahoo": "HSBA.L",  "name": "HSBC",             "est_yield": 7.2},
    "NG":    {"yahoo": "NG.L",    "name": "National Grid",    "est_yield": 5.9},
    "SSE":   {"yahoo": "SSE.L",   "name": "SSE",              "est_yield": 5.4},
    "VOD":   {"yahoo": "VOD.L",   "name": "Vodafone",         "est_yield": 11.2},
    "BP":    {"yahoo": "BP.L",    "name": "BP",               "est_yield": 5.6},
    # US dividend payers
    "XOM":   {"yahoo": "XOM",     "name": "Exxon Mobil",      "est_yield": 3.8},
    "CVX":   {"yahoo": "CVX",     "name": "Chevron",          "est_yield": 4.5},
    "KO":    {"yahoo": "KO",      "name": "Coca Cola",        "est_yield": 3.1},
    "PEP":   {"yahoo": "PEP",     "name": "PepsiCo",          "est_yield": 3.4},
    "JNJ":   {"yahoo": "JNJ",     "name": "Johnson & Johnson","est_yield": 3.2},
    "ABBV":  {"yahoo": "ABBV",    "name": "AbbVie",           "est_yield": 3.8},
    "PG":    {"yahoo": "PG",      "name": "Procter & Gamble", "est_yield": 2.4},
}

def check_dividend(code, data):
    try:
        t     = yf.Ticker(data['yahoo'])
        info  = t.info
        hist  = t.history(period="1y", actions=True)

        if hist.empty:
            return None

        # Get dividend history
        divs = hist[hist['Dividends'] > 0]['Dividends']

        if divs.empty:
            return None

        # Most recent dividend
        last_div_date  = divs.index[-1]
        last_div_amt   = float(divs.iloc[-1])

        # Estimate next ex-dividend date
        # Most UK stocks pay semi-annually, US quarterly
        if data['yahoo'].endswith('.L'):
            freq_days = 180  # semi-annual
        else:
            freq_days = 90   # quarterly

        last_div_date_naive = last_div_date.replace(tzinfo=None) if last_div_date.tzinfo else last_div_date
        next_ex_date = last_div_date_naive + timedelta(days=freq_days)
        now          = datetime.now()
        days_to_ex   = (next_ex_date - now).days

        # Current price
        close = hist['Close']
        if close.iloc[-1] > 500 and data['yahoo'].endswith('.L'):
            close = close / 100
        price = round(float(close.iloc[-1]), 2)

        # Adjust dividend for pence conversion
        if data['yahoo'].endswith('.L') and last_div_amt > 1:
            last_div_amt = last_div_amt / 100

        # Yield calculation
        annual_divs = last_div_amt * (365 / freq_days)
        yield_pct   = round(annual_divs / price * 100, 2) if price else 0

        # RSI
        delta = close.diff()
        gain  = delta.where(delta > 0, 0).rolling(14).mean()
        loss  = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi   = round(float(100 - (100 / (1 + gain/loss)).iloc[-1]), 2)

        # Only flag if ex-date is 3-10 days away — sweet spot
        if not (3 <= days_to_ex <= 10):
            return None

        # Stop and targets
        stop    = round(price * 0.97, 2)  # Tight 3% stop for income trade
        target  = round(price * 1.03, 2)  # Target: recover drop + small gain
        qty     = round(min(50 / (price * 0.03), 250 / price), 2)

        return {
            "code":          code,
            "name":          data['name'],
            "yahoo":         data['yahoo'],
            "price":         price,
            "rsi":           rsi,
            "last_div":      round(last_div_amt, 4),
            "next_ex_date":  next_ex_date.strftime('%Y-%m-%d'),
            "days_to_ex":    days_to_ex,
            "yield_pct":     yield_pct,
            "est_yield":     data['est_yield'],
            "stop":          stop,
            "target":        target,
            "quantity":      qty,
            "signal_type":   "DIVIDEND_CAPTURE",
            "rationale":     f"Ex-div in {days_to_ex} days | yield {yield_pct}% | buy now, collect dividend"
        }

    except Exception as e:
        return None

def run():
    now     = datetime.now(timezone.utc)
    signals = []

    print("Scanning dividend capture opportunities...", flush=True)

    for code, data in DIVIDEND_UNIVERSE.items():
        print(f"  {code}...", flush=True)
        result = check_dividend(code, data)
        if result:
            signals.append(result)
            print(f"  💰 DIVIDEND: {result['name']} | ex-date {result['next_ex_date']} ({result['days_to_ex']} days) | yield {result['yield_pct']}%")

    signals.sort(key=lambda x: x['yield_pct'], reverse=True)

    output = {
        "timestamp": now.strftime('%Y-%m-%d %H:%M UTC'),
        "signals":   signals
    }

    atomic_write(DIVIDEND_FILE, output)

    print(f"\n=== DIVIDEND CAPTURE OPPORTUNITIES ===")
    if signals:
        print(f"{len(signals)} ex-dividend dates in next 3-10 days:\n")
        for s in signals:
            print(f"  💰 {s['name']:20} | Ex: {s['next_ex_date']} ({s['days_to_ex']}d) | "
                  f"Yield: {s['yield_pct']}% | Price: £{s['price']} | RSI: {s['rsi']}")
            print(f"     → {s['rationale']}")
    else:
        print("No dividend capture opportunities in next 3-10 days")

    # Also show upcoming dividends beyond the window
    print(f"\n📅 HIGH YIELD WATCHLIST (est. annual yield):")
    for code, data in sorted(DIVIDEND_UNIVERSE.items(), key=lambda x: x[1]['est_yield'], reverse=True)[:8]:
        print(f"  {data['name']:20} | ~{data['est_yield']}% yield")

    return output

if __name__ == '__main__':
    run()
