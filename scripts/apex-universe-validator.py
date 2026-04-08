#!/usr/bin/env python3
"""
Universe Validator
==================
Aggregates all ticker references from every universe file and validates each
against Yahoo Finance. Alerts on delisted/missing tickers so they can be
removed before they cause silent errors in scans.

Does NOT auto-remove tickers — human reviews the report and removes manually.
Output: ../logs/apex-universe-validation.json

Run daily at 06:30 UTC (before any scan).
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, log_error, log_warning, send_telegram, safe_read
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m):    print(f'ERROR: {m}')
    def log_warning(m):  print(f'WARNING: {m}')
    def send_telegram(m): print(f'TELEGRAM: {m}')
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d

OUTPUT_FILE   = '/home/ubuntu/.picoclaw/logs/apex-universe-validation.json'
SCRIPTS_DIR   = '/home/ubuntu/.picoclaw/scripts'
LOGS_DIR      = '/home/ubuntu/.picoclaw/logs'
QUALITY_FILE  = f'{SCRIPTS_DIR}/apex-quality-universe.json'


# ── Source extraction helpers ────────────────────────────────────────────────

def _tickers_from_staleness_check():
    """56 tickers from WATCHLIST_YAHOO in apex-staleness-check.py."""
    return {
        "VWRP.L","VUAG.L","VFEA.L","IGWD.L","HMWO.L","IITU.L",
        "IUFS.L","IUHC.L","IUES.L","IUCD.L","SGLN.L","SSLN.L",
        "ISF.L","CSPX.L","EQQQ.L","HEAL.L","INRG.L","WCLD.L",
        "VAPX.L","VJPN.L","VGOV.L","VAGS.L","HSBA.L","SHEL.L",
        "AZN.L","ULVR.L","GSK.L","LLOY.L","BP.L","RIO.L",
        "BA.L","REL.L","BARC.L","NWG.L","PRU.L","NG.L",
        "SSE.L","DGE.L","IMB.L","BATS.L","EXPN.L","CPG.L",
        "WPP.L","VOD.L","BT-A.L","AV.L","AIR.PA","MC.PA",
        "SAN.MC","NOVN.SW","ROG.SW","TTE.PA","ASML.AS","SIE.DE",
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA",
        "CRM","ORCL","AMD","INTC","QCOM","JPM","GS","MS",
        "BAC","BLK","AXP","C","V","JNJ","PFE","MRK","UNH",
        "ABBV","TMO","DHR","KO","PEP","MCD","WMT","PG",
        "XOM","CVX","NVO",
    }, "apex-staleness-check.py"


def _tickers_from_inverse_scanner():
    """4 tickers from INVERSE_UNIVERSE in apex-inverse-scanner.py."""
    return {"SQQQ","SPXU","3UKS.L","QQQS.L"}, "apex-inverse-scanner.py"


def _tickers_from_watchlist_analyzer():
    """66 tickers from TICKER_META in apex-watchlist-analyzer.py."""
    return {
        "VWRP.L","VUAG.L","NBIS","CLSK","AAPL","MSFT","NVDA","GOOGL",
        "AMZN","META","JPM","GS","BAC","BLK","AXP","V","JNJ","PFE",
        "MRK","UNH","ABBV","TMO","XOM","CVX","KO","PEP","PG","WMT",
        "MCD","TSLA","CRM","ORCL","AMD","INTC","QCOM","MS","C","DHR",
        "NVO","HSBA.L","AZN.L","GSK.L","SHEL.L","ULVR.L","BP.L",
        "RIO.L","LLOY.L","BARC.L","AV.L","DGE.L","VOD.L","SSE.L",
        "NG.L","IMB.L","BATS.L","REL.L","NWG.L","PRU.L","CPG.L",
        "WPP.L","EXPN.L","BA.L","BT-A.L","VUAG.L",
    }, "apex-watchlist-analyzer.py"


def _tickers_from_quality_universe():
    """Tickers from apex-quality-universe.json (stock tickers, not Yahoo symbols)."""
    data = safe_read(QUALITY_FILE, {})
    stocks = data.get('quality_stocks', data) if isinstance(data, dict) else {}
    # Quality universe uses plain tickers (AAPL, MSFT, etc.) — no .L suffix
    # Map to Yahoo by appending .L for obvious UK names; leave others as-is.
    # Simple heuristic: if ticker is 3-4 chars and all alpha, likely US
    yahoo_tickers = set()
    for t in stocks.keys():
        yahoo_tickers.add(t)  # these are already Yahoo-compatible
    return yahoo_tickers, "apex-quality-universe.json"


def _tickers_from_multiframe():
    """Tickers from YAHOO_MAP in apex-multiframe.py."""
    return {
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","JPM","GS",
        "V","BAC","BLK","JNJ","PFE","UNH","ABBV","XOM","CVX",
        "KO","PEP","PG","WMT","TSLA","HSBA.L","AZN.L","GSK.L",
        "ULVR.L","SHEL.L","VUAG.L","SQQQ","QQQS.L","SPXU",
    }, "apex-multiframe.py"


def _build_universe():
    """
    Aggregate all tickers from all sources.
    Returns:
        universe: dict of {yahoo_ticker: [source_file, ...]}
    """
    universe = {}

    sources = [
        _tickers_from_staleness_check(),
        _tickers_from_inverse_scanner(),
        _tickers_from_watchlist_analyzer(),
        _tickers_from_quality_universe(),
        _tickers_from_multiframe(),
    ]

    for tickers, source_file in sources:
        for t in tickers:
            if t not in universe:
                universe[t] = []
            if source_file not in universe[t]:
                universe[t].append(source_file)

    return universe


# ── Validation ───────────────────────────────────────────────────────────────

def _validate_ticker(yahoo_ticker: str) -> str:
    """
    Check if a ticker is alive in Yahoo Finance.
    Returns: 'VALID' | 'DELISTED_OR_MISSING' | 'NETWORK_ERROR'
    """
    try:
        import yfinance as yf
        hist = yf.download(yahoo_ticker, period='2d', progress=False, auto_adjust=True)
        if hist is None or hist.empty:
            return 'DELISTED_OR_MISSING'
        return 'VALID'
    except Exception as e:
        err_str = str(e).lower()
        # Distinguish network errors from "ticker not found" errors
        if any(kw in err_str for kw in ('no data found', 'delisted', 'no timezone', 'exception')):
            return 'DELISTED_OR_MISSING'
        return 'NETWORK_ERROR'


def run(verbose: bool = True):
    """Run full universe validation and write report."""
    now = datetime.now(timezone.utc)
    print(f"\n=== UNIVERSE VALIDATOR ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    universe = _build_universe()
    total = len(universe)
    print(f"Aggregated {total} unique tickers across all universe sources")
    print("Validating against Yahoo Finance...\n")

    valid   = []
    delisted = []
    errors  = []

    for i, (ticker, sources) in enumerate(sorted(universe.items())):
        result = _validate_ticker(ticker)
        if verbose:
            icon = "✅" if result == 'VALID' else ("❌" if result == 'DELISTED_OR_MISSING' else "⚠️ ")
            print(f"  [{i+1:3d}/{total}] {icon} {ticker:15s} → {result}")
        if result == 'VALID':
            valid.append(ticker)
        elif result == 'DELISTED_OR_MISSING':
            delisted.append(ticker)
        else:
            errors.append(ticker)

    # Build report
    report = {
        'timestamp':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'total_checked': total,
        'valid_count':   len(valid),
        'delisted_count': len(delisted),
        'error_count':   len(errors),
        'valid':         sorted(valid),
        'delisted':      sorted(delisted),
        'errors':        sorted(errors),
        'sources':       {t: universe[t] for t in delisted + errors},
    }

    atomic_write(OUTPUT_FILE, report)
    print(f"\nReport saved to {OUTPUT_FILE}")
    print(f"Valid: {len(valid)} | Delisted/Missing: {len(delisted)} | Network errors: {len(errors)}")

    # Alert if any delisted tickers found
    if delisted:
        source_lines = []
        for t in delisted:
            src = ', '.join(universe.get(t, ['unknown']))
            source_lines.append(f"• {t} — appears in: {src}")
        msg = (
            f"⚠️ UNIVERSE VALIDATOR — Delisted Tickers\n\n"
            f"{len(delisted)} ticker(s) returned no data from Yahoo Finance:\n\n"
            + "\n".join(source_lines)
            + "\n\nAction: manually remove from each listed file.\n"
            f"Check apex-universe-validation.json for full report."
        )
        send_telegram(msg)
        log_error(f"Delisted tickers detected: {delisted}")

    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Apex Universe Validator')
    parser.add_argument('--quiet', action='store_true', help='Suppress per-ticker output')
    args = parser.parse_args()
    run(verbose=not args.quiet)
