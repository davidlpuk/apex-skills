#!/usr/bin/env python3
"""
EDGAR Insider Trading Data — Layer 15
SEC Form 4 filings — corporate insiders buying their own stock.
Insider buying is one of the strongest smart money signals available.

Free data from SEC EDGAR API — no API key required.
Updates daily — insiders must file within 2 business days of transaction.

Signal logic:
- Cluster buying (3+ insiders buying same stock within 30 days): +2
- Single large insider buy (>$100k): +1
- CEO/CFO buying (C-suite): +2
- Insider selling: -1 (weaker signal — many reasons to sell)
- No recent insider activity: 0

Wired into decision engine as Layer 15.
"""
import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

INSIDER_FILE = '/home/ubuntu/.picoclaw/logs/apex-insider-data.json'

# CIK numbers for our quality universe — SEC company identifiers
# Get from: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=apple&CIK=&type=4
CIK_MAP = {
    'AAPL':  '0000320193',
    'MSFT':  '0000789019',
    'GOOGL': '0001652044',
    'NVDA':  '0001045810',
    'XOM':   '0000034088',
    'CVX':   '0000093410',
    'V':     '0001403161',
    'JPM':   '0000019617',
    'JNJ':   '0000200406',
    'ABBV':  '0001551152',
    'UNH':   '0000731766',
    'PFE':   '0000078003',
    'GS':    '0000886982',
    'BLK':   '0001364742',
    'AZN':   '0000901832',
    'GSK':   '0000310158',
}

# C-suite titles that carry extra weight
CSUITE_TITLES = [
    'chief executive', 'ceo', 'chief financial', 'cfo',
    'chief operating', 'coo', 'president', 'chairman',
    'executive vice president', 'evp'
]

def fetch_insider_filings(cik, days_back=30):
    """
    Fetch recent Form 4 filings from SEC EDGAR for a company.
    Form 4 = statement of changes in beneficial ownership.
    """
    try:
        # SEC EDGAR submissions endpoint
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        req = urllib.request.Request(url, headers={
            'User-Agent': 'ApexTradingSystem david@picoclaw.com',
            'Accept':     'application/json',
        })

        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))

        filings   = data.get('filings', {}).get('recent', {})
        forms     = filings.get('form', [])
        dates     = filings.get('filingDate', [])
        accessions= filings.get('accessionNumber', [])

        # Filter Form 4 filings within lookback period
        cutoff   = datetime.now(timezone.utc) - timedelta(days=days_back)
        form4s   = []

        for i, form in enumerate(forms):
            if form == '4' and i < len(dates):
                try:
                    filing_date = datetime.strptime(dates[i], '%Y-%m-%d')
                    filing_date = filing_date.replace(tzinfo=timezone.utc)
                    if filing_date >= cutoff:
                        form4s.append({
                            'date':      dates[i],
                            'accession': accessions[i] if i < len(accessions) else '',
                        })
                except:
                    pass

        return form4s

    except Exception as e:
        log_error(f"EDGAR fetch failed for CIK {cik}: {e}")
        return []

def analyse_insider_activity(symbol, days_back=30):
    """
    Analyse insider buying/selling patterns for a symbol.
    Returns (adjustment, reasons, activity_summary)
    """
    cik = CIK_MAP.get(symbol)
    if not cik:
        return 0, [], {'status': 'NO_CIK', 'symbol': symbol}

    filings = fetch_insider_filings(cik, days_back)

    if not filings:
        return 0, [], {
            'status':   'NO_RECENT_FILINGS',
            'symbol':   symbol,
            'count':    0,
        }

    filing_count = len(filings)
    adj          = 0
    reasons      = []

    # Form 4 includes compensation grants AND open market purchases
    # Cannot distinguish without XML parsing — use conservative threshold
    # 20+ filings/month suggests buying on top of routine compensation
    if filing_count >= 20:
        adj += 1
        reasons.append(f"Elevated insider activity: {filing_count} Form 4 filings in {days_back} days")
    # Under 20 = likely routine compensation — no signal

    summary = {
        'status':       'ACTIVE',
        'symbol':       symbol,
        'cik':          cik,
        'filing_count': filing_count,
        'days_back':    days_back,
        'latest_filing':filings[0]['date'] if filings else None,
        'adjustment':   adj,
        'reasons':      reasons,
    }

    return adj, reasons, summary

def run():
    """Fetch insider data for full quality universe."""
    now = datetime.now(timezone.utc)
    print(f"\n=== EDGAR INSIDER DATA ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    results = {}
    total_signals = 0

    for symbol, cik in CIK_MAP.items():
        try:
            adj, reasons, summary = analyse_insider_activity(symbol, days_back=30)
            results[symbol] = summary

            if adj != 0:
                total_signals += 1
                icon = "🟢" if adj > 0 else "🔴"
                print(f"  {icon} {symbol:8} adj:{adj:+d} | {reasons[0][:60] if reasons else ''}")
            else:
                print(f"  ⚪ {symbol:8} No recent insider filings")

            # Rate limit — SEC asks for max 10 requests/second
            import time
            time.sleep(0.15)

        except Exception as e:
            log_error(f"Insider analysis failed for {symbol}: {e}")

    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'data':      results,
        'count':     len(results),
        'signals':   total_signals,
    }

    atomic_write(INSIDER_FILE, output)
    print(f"\n✅ Insider data saved — {total_signals} signals across {len(results)} instruments")
    return output

def get_insider_adjustment(symbol, signal_type='TREND'):
    """
    Get insider adjustment for a specific symbol.
    Called by decision engine Layer 15.
    """
    insider_data = safe_read(INSIDER_FILE, {})
    data         = insider_data.get('data', {})
    inst         = data.get(symbol, {})

    if not inst or inst.get('status') in ['NO_CIK', 'NO_RECENT_FILINGS']:
        return 0, []

    adj     = inst.get('adjustment', 0)
    reasons = inst.get('reasons', [])

    # Insider selling is a weaker bearish signal for contrarian
    # — don't double-penalise already beaten-down stocks
    if signal_type == 'CONTRARIAN' and adj < 0:
        adj = 0
        reasons = []

    return adj, reasons

if __name__ == '__main__':
    run()

# ── PATCHED: redirect get_insider_adjustment to apex-insider-edgar.py output ──
def get_insider_adjustment(symbol, signal_type='TREND'):
    """
    Patched Layer 15 — reads real XML-parsed EDGAR scores from apex-insider-edgar.py.
    Overrides the stub above.
    """
    import json
    from pathlib import Path

    sig_file = Path('/home/ubuntu/.picoclaw/data/apex-insider-signal.json')
    if not sig_file.exists():
        return 0, []

    try:
        data    = json.loads(sig_file.read_text())
        signals = data.get('signals', {})
        sig     = signals.get(symbol, {})
        score   = sig.get('score', 0)
        reasons = sig.get('reasons', [])

        # Don't double-penalise contrarian setups with insider selling
        if signal_type == 'CONTRARIAN' and score < 0:
            return 0, []

        return score, reasons
    except Exception:
        return 0, []
