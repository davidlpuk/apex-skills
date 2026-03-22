#!/usr/bin/env python3
import urllib.request
import csv
import json
import io
from datetime import datetime, timezone

RATES_CACHE = '/home/ubuntu/.picoclaw/logs/apex-hmrc-rates.json'

def fetch_hmrc_rate(year, month, currency_code="USD"):
    url = f"https://www.trade-tariff.service.gov.uk/exchange_rates/view/files/monthly_csv_{year}-{month}.csv"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            if currency_code.upper() in row.get('Currency', '').upper() or \
               currency_code.upper() in str(row).upper():
                for key, val in row.items():
                    if 'rate' in key.lower() or 'sterling' in key.lower():
                        try:
                            return float(val)
                        except:
                            continue
    except Exception as e:
        pass
    return None

def get_rate_for_date(trade_date, currency="USD"):
    try:
        dt = datetime.strptime(trade_date[:10], '%Y-%m-%d')
    except:
        return None

    year  = dt.year
    month = dt.month

    # Check cache first
    try:
        with open(RATES_CACHE) as f:
            cache = json.load(f)
    except:
        cache = {}

    cache_key = f"{currency}_{year}_{month:02d}"
    if cache_key in cache:
        return cache[cache_key]

    # Fetch from HMRC
    rate = fetch_hmrc_rate(year, month, currency)

    if rate:
        cache[cache_key] = rate
        with open(RATES_CACHE, 'w') as f:
            json.dump(cache, f, indent=2)
        return rate

    return None

def convert_to_gbp(amount_usd, trade_date):
    rate = get_rate_for_date(trade_date)
    if rate:
        return round(amount_usd / rate, 2), rate
    return None, None

if __name__ == '__main__':
    # Test with today's date
    today = datetime.now().strftime('%Y-%m-%d')
    amount = 159.01
    gbp, rate = convert_to_gbp(amount, today)
    if gbp:
        print(f"${amount} USD = £{gbp} GBP (HMRC rate: {rate})")
    else:
        print("Could not fetch HMRC rate — use manual rate from hmrc.gov.uk")
        print(f"URL to check: https://www.trade-tariff.service.gov.uk/exchange_rates/monthly")
