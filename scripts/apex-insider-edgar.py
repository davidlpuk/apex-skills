#!/usr/bin/env python3
"""
apex-insider-edgar.py — Layer 15: EDGAR XML Insider Signal
Parses SEC Form 4 filings via free EDGAR API.
Scores: cluster buying +2, C-suite buying +2, selling -1, cluster selling -2
Outputs: ~/.picoclaw/data/apex-insider-signal.json
Run: every 6 hours via cron, or on-demand before scoring
"""

import json
import time
import logging
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR   = Path.home() / ".picoclaw" / "data"
LOG_DIR    = Path.home() / ".picoclaw" / "logs"
OUT_FILE   = DATA_DIR / "apex-insider-signal.json"
LOG_FILE   = LOG_DIR  / "apex-insider-edgar.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EDGAR] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("apex-insider")

# ── Instrument → CIK map ─────────────────────────────────────────────────────
# CIK = SEC Central Index Key, used to query EDGAR filings
INSTRUMENTS = {
    # US Equities
    "AAPL":  {"cik": "0000320193",  "name": "Apple Inc"},
    "MSFT":  {"cik": "0000789019",  "name": "Microsoft Corp"},
    "NVDA":  {"cik": "0001045810",  "name": "NVIDIA Corp"},
    "AMZN":  {"cik": "0001018724",  "name": "Amazon.com Inc"},
    "GOOGL": {"cik": "0001652044",  "name": "Alphabet Inc"},
    "META":  {"cik": "0001326801",  "name": "Meta Platforms"},
    "TSLA":  {"cik": "0001318605",  "name": "Tesla Inc"},
    "V":     {"cik": "0001403161",  "name": "Visa Inc"},
    "XOM":   {"cik": "0000034088",  "name": "Exxon Mobil Corp"},
    "CVX":   {"cik": "0000093410",  "name": "Chevron Corp"},
    "HOOD":  {"cik": "0001783879",  "name": "Robinhood Markets"},
    "PLTR":  {"cik": "0001321655",  "name": "Palantir Technologies"},
    "NFLX":  {"cik": "0001065280",  "name": "Netflix Inc"},
    # UK ETFs / ETPs — no Form 4 (not SEC-registered), skip gracefully
    "VUAG":  {"cik": None,          "name": "Vanguard S&P500 ETF (LSE)"},
    "3USS":  {"cik": None,          "name": "WisdomTree S&P500 3x (LSE)"},
    "ULVR":  {"cik": None,          "name": "Unilever PLC (LSE)"},
}

# C-suite titles that qualify for the +2 C-suite bonus
CSUITE_TITLES = {
    "ceo", "chief executive", "president", "cfo", "chief financial",
    "coo", "chief operating", "cto", "chief technology",
    "chairman", "director", "executive vice president", "evp"
}

# EDGAR base URLs
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_FILING      = "https://www.sec.gov/Archives/edgar/full-index/"
EDGAR_FORM4_URL   = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=20&search_text=&output=atom"

HEADERS = {
    "User-Agent": "Apex Trading System david@apex.local",  # SEC requires this
    "Accept-Encoding": "gzip, deflate"
}

LOOKBACK_DAYS    = 30     # how far back to scan Form 4s
CLUSTER_THRESHOLD = 2     # min number of insiders buying to trigger cluster signal
REQUEST_DELAY    = 0.5    # seconds between SEC requests (be polite)


# ── SEC EDGAR fetchers ────────────────────────────────────────────────────────

def get_recent_form4s(cik: str, ticker: str) -> list[dict]:
    """
    Fetch recent Form 4 filings for a CIK using EDGAR submissions API.
    Returns list of filing dicts with date, accession, url.
    """
    url = EDGAR_SUBMISSIONS.format(cik=cik)
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"{ticker}: submissions fetch failed — {e}")
        return []

    filings = data.get("filings", {}).get("recent", {})
    forms       = filings.get("form", [])
    dates       = filings.get("filingDate", [])
    accessions  = filings.get("accessionNumber", [])

    cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).date()
    results = []

    for form, date_str, acc in zip(forms, dates, accessions):
        if form not in ("4", "4/A"):
            continue
        try:
            filing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if filing_date < cutoff:
            continue

        acc_clean = acc.replace("-", "")
        # Build primary document URL
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{acc_clean}/{acc}.txt"
        )
        results.append({
            "date":       date_str,
            "accession":  acc,
            "filing_url": filing_url,
            "cik":        cik,
        })

    log.info(f"{ticker}: found {len(results)} Form 4s in last {LOOKBACK_DAYS} days")
    return results


def fetch_form4_xml(cik: str, accession: str) -> str | None:
    """
    Fetch the XML document for a specific Form 4 accession.
    EDGAR indexes list the actual .xml file; we try predictable naming first.
    """
    acc_clean = accession.replace("-", "")
    cik_int   = int(cik)

    # Try index page to find the XML filename
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_clean}/{accession}-index.htm"
    )
    try:
        r = requests.get(index_url, headers=HEADERS, timeout=10)
        time.sleep(REQUEST_DELAY)
        if r.status_code == 200:
            # Extract raw XML href (not the xslF345X05 styled version)
            xml_url = None
            for line in r.text.splitlines():
                if 'href="' in line and ".xml" in line and "xslF345X05" not in line:
                    match = re.search(r'href="(/Archives/edgar/data/[^"]+\.xml)"', line)
                    if match:
                        xml_url = f"https://www.sec.gov{match.group(1)}"
                        break
            if not xml_url:
                # Fallback: try standard naming
                xml_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_int}/{acc_clean}/{accession}.xml"
                )
        else:
            xml_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{acc_clean}/{accession}.xml"
            )

        r2 = requests.get(xml_url, headers=HEADERS, timeout=10)
        time.sleep(REQUEST_DELAY)
        if r2.status_code == 200:
            return r2.text
    except Exception as e:
        log.debug(f"XML fetch failed for {accession}: {e}")

    return None


# ── Form 4 XML parser ─────────────────────────────────────────────────────────

def parse_form4_xml(xml_text: str) -> dict | None:
    """
    Parse Form 4 XML. Returns structured dict with:
    - reporter_name, reporter_title, is_director, is_officer
    - transactions: list of {date, type, shares, price_per_share, acquired_disposed}
    - is_csuite: bool
    - net_shares: positive = bought, negative = sold
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.debug(f"XML parse error: {e}")
        return None

    ns = ""  # Form 4 XML is usually namespace-free

    # Reporter identity
    reporter_name  = ""
    reporter_title = ""
    is_director    = False
    is_officer     = False

    rn = root.find(".//reportingOwner/reportingOwnerRelationship")
    if rn is not None:
        is_director = (rn.findtext("isDirector", "0").strip() == "1")
        is_officer  = (rn.findtext("isOfficer",  "0").strip() == "1")
        reporter_title = rn.findtext("officerTitle", "").strip().lower()

    rname = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
    if rname is not None:
        reporter_name = rname.text.strip() if rname.text else ""

    # Detect C-suite
    is_csuite = is_officer and any(
        kw in reporter_title for kw in CSUITE_TITLES
    )

    # Transactions — non-derivative (shares)
    transactions = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        try:
            txn_type = txn.findtext(
                "transactionCoding/transactionCode", ""
            ).strip().upper()
            shares_text = txn.findtext(
                "transactionAmounts/transactionShares/value", "0"
            )
            price_text = txn.findtext(
                "transactionAmounts/transactionPricePerShare/value", "0"
            )
            ad_code = txn.findtext(
                "transactionAmounts/transactionAcquiredDisposedCode/value", ""
            ).strip().upper()
            txn_date = txn.findtext("transactionDate/value", "")

            shares = float(shares_text) if shares_text else 0.0
            price  = float(price_text)  if price_text  else 0.0

            transactions.append({
                "date":               txn_date,
                "type":               txn_type,  # P=purchase, S=sale, etc.
                "shares":             shares,
                "price_per_share":    price,
                "acquired_disposed":  ad_code,   # A=acquired, D=disposed
            })
        except (ValueError, AttributeError):
            continue

    # Net shares: A = bought (+), D = sold (-)
    net_shares = sum(
        t["shares"] if t["acquired_disposed"] == "A" else -t["shares"]
        for t in transactions
    )

    return {
        "reporter_name":  reporter_name,
        "reporter_title": reporter_title,
        "is_director":    is_director,
        "is_officer":     is_officer,
        "is_csuite":      is_csuite,
        "transactions":   transactions,
        "net_shares":     net_shares,
    }


# ── Signal scorer ─────────────────────────────────────────────────────────────

def score_insider_activity(ticker: str, parsed_filings: list[dict]) -> dict:
    """
    Apply Apex scoring rules to parsed Form 4 data.

    Rules:
      +2  cluster buying  (≥2 distinct insiders bought in lookback window)
      +2  C-suite buying  (CEO/CFO/COO/CTO individually bought)
      -1  single insider selling
      -2  cluster selling (≥2 distinct insiders sold)
       0  mixed / neutral / no data

    Scores are capped: max +4, min -2
    Returns score dict with explanation.
    """
    buyers  = []  # reporter names who net-bought
    sellers = []  # reporter names who net-sold
    csuite_bought = False
    csuite_sold   = False

    for f in parsed_filings:
        if f["net_shares"] > 0:
            buyers.append(f["reporter_name"])
            if f["is_csuite"]:
                csuite_bought = True
        elif f["net_shares"] < 0:
            sellers.append(f["reporter_name"])
            if f["is_csuite"]:
                csuite_sold = True

    score    = 0
    reasons  = []
    evidence = []

    # Cluster buy
    unique_buyers = list(set(buyers))
    if len(unique_buyers) >= CLUSTER_THRESHOLD:
        score += 2
        reasons.append(f"cluster_buy({len(unique_buyers)} insiders)")
        evidence.extend(unique_buyers[:3])  # show up to 3 names

    # C-suite buy
    if csuite_bought:
        score += 2
        reasons.append("csuite_buy")

    # Cluster sell
    unique_sellers = list(set(sellers))
    if len(unique_sellers) >= CLUSTER_THRESHOLD:
        score -= 2
        reasons.append(f"cluster_sell({len(unique_sellers)} insiders)")
    elif unique_sellers:
        score -= 1
        reasons.append("insider_sell")

    # C-suite sell overrides (bearish signal)
    if csuite_sold and not csuite_bought:
        score -= 1
        reasons.append("csuite_sell")

    # Cap
    score = max(-2, min(4, score))

    return {
        "ticker":          ticker,
        "score":           score,
        "reasons":         reasons,
        "buyers":          unique_buyers,
        "sellers":         unique_sellers,
        "csuite_bought":   csuite_bought,
        "csuite_sold":     csuite_sold,
        "filings_parsed":  len(parsed_filings),
        "lookback_days":   LOOKBACK_DAYS,
    }


# ── Atomic write (consistent with apex_utils pattern) ─────────────────────────

def atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run() -> None:
    log.info("=== EDGAR Layer 15 run starting ===")
    start_ts = datetime.utcnow().isoformat()

    results    = {}
    errors     = {}
    skipped    = []

    for ticker, meta in INSTRUMENTS.items():
        cik = meta["cik"]

        # Skip non-SEC instruments (LSE ETFs, UK stocks)
        if cik is None:
            log.info(f"{ticker}: skipping — no SEC CIK (non-US instrument)")
            skipped.append(ticker)
            results[ticker] = {
                "ticker":  ticker,
                "score":   0,
                "reasons": ["no_sec_filing"],
                "note":    "LSE/non-US — Form 4 not applicable",
            }
            continue

        log.info(f"Processing {ticker} (CIK {cik})")

        # Step 1: get filing list
        filings = get_recent_form4s(cik, ticker)
        time.sleep(REQUEST_DELAY)

        if not filings:
            results[ticker] = {
                "ticker":  ticker,
                "score":   0,
                "reasons": ["no_recent_filings"],
                "filings_parsed": 0,
                "lookback_days": LOOKBACK_DAYS,
            }
            continue

        # Step 2: fetch + parse each filing XML
        parsed = []
        for f in filings[:10]:  # cap at 10 most recent to respect SEC rate limits
            xml_text = fetch_form4_xml(cik, f["accession"])
            if xml_text:
                p = parse_form4_xml(xml_text)
                if p:
                    p["filing_date"] = f["date"]
                    parsed.append(p)
            time.sleep(REQUEST_DELAY)

        log.info(f"{ticker}: parsed {len(parsed)}/{len(filings)} filings")

        # Step 3: score
        signal = score_insider_activity(ticker, parsed)
        signal["last_filing_date"] = filings[0]["date"] if filings else None
        results[ticker] = signal

        log.info(
            f"{ticker}: score={signal['score']:+d} "
            f"reasons={signal['reasons']} "
            f"buyers={signal['buyers'][:2]}"
        )

    # Compile output
    output = {
        "layer":       15,
        "source":      "SEC EDGAR Form 4",
        "generated":   start_ts,
        "lookback_days": LOOKBACK_DAYS,
        "skipped":     skipped,
        "signals":     results,
        "summary": {
            t: {"score": v["score"], "reasons": v.get("reasons", [])}
            for t, v in results.items()
        }
    }

    atomic_write(OUT_FILE, output)
    log.info(f"Written → {OUT_FILE}")

    # Print summary table
    log.info("── Signal summary ──────────────────────────────")
    for ticker, sig in sorted(results.items(), key=lambda x: -x[1].get("score", 0)):
        score   = sig.get("score", 0)
        reasons = ", ".join(sig.get("reasons", ["—"]))
        bar     = "▲" * max(0, score) + "▼" * max(0, -score)
        log.info(f"  {ticker:<6} {score:+d} {bar:<6} {reasons}")
    log.info("─" * 50)
    log.info("=== EDGAR Layer 15 run complete ===")


# ── Apex integration helper ───────────────────────────────────────────────────

def get_insider_score(ticker: str) -> int:
    """
    Called by apex-score.py (or equivalent) to retrieve Layer 15 score.
    Returns integer score, 0 if not found or stale.
    """
    if not OUT_FILE.exists():
        return 0

    try:
        data = json.loads(OUT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    # Staleness check — warn if > 8 hours old
    try:
        generated = datetime.fromisoformat(data.get("generated", ""))
        age_hours = (datetime.utcnow() - generated).total_seconds() / 3600
        if age_hours > 8:
            log.warning(f"Insider data is {age_hours:.1f}h old for {ticker}")
    except ValueError:
        pass

    signals = data.get("signals", {})
    return signals.get(ticker, {}).get("score", 0)


def get_all_insider_scores() -> dict[str, int]:
    """Returns {ticker: score} dict for all instruments."""
    if not OUT_FILE.exists():
        return {}
    try:
        data = json.loads(OUT_FILE.read_text())
        return {
            t: v.get("score", 0)
            for t, v in data.get("signals", {}).items()
        }
    except (json.JSONDecodeError, OSError):
        return {}


if __name__ == "__main__":
    run()
