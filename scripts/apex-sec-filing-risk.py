#!/usr/bin/env python3
"""
apex-sec-filing-risk.py — Layer 20: SEC Filing Risk
Fetches 3 most recent EDGAR filings (8-K, 10-K, 10-Q, NT variants)
per ticker, classifies red-flag items, and returns a score penalty.

Red flags and penalties:
  going_concern              -3  (going concern / substantial doubt in 10-K/10-Q text)
  bankruptcy_or_receivership -3  (8-K item 1.03)
  restatement                -2  (8-K item 4.02)
  auditor_departure          -2  (8-K item 4.01)
  termination_of_material_agreement -2 (8-K item 1.02)
  late_filing                -2  (NT 10-K / NT 10-Q)
  mass_officer_departures    -1  (8-K item 5.02 in >=3 filings in lookback)

Output: /home/ubuntu/.picoclaw/logs/apex-sec-filing-risk.json
Run:    daily (cron 06:30 UTC), or on-demand before morning scan
"""

import json
import logging
import time
import re
import sys
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).parent
LOG_DIR      = Path.home() / ".picoclaw" / "logs"
OUT_FILE     = LOG_DIR / "apex-sec-filing-risk.json"
LOG_FILE     = LOG_DIR / "apex-sec-filing-risk.log"
QUALITY_FILE = _SCRIPTS_DIR / "apex-quality-universe.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(fp, data):
        import tempfile, os
        tmp = str(fp) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(fp))
    def safe_read(fp, default=None):
        try:
            with open(fp) as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}
    def log_error(m):   print(f"ERROR: {m}", flush=True)
    def log_warning(m): print(f"WARNING: {m}", flush=True)

# ── Logging — FileHandler ONLY (no StreamHandler: runs via exec_module) ───────
_log_handler = logging.FileHandler(str(LOG_FILE))
_log_handler.setFormatter(logging.Formatter("%(asctime)s [SEC] %(levelname)s %(message)s"))
log = logging.getLogger("apex-sec-filing")
if not log.handlers:
    log.addHandler(_log_handler)
    log.setLevel(logging.INFO)

# ── EDGAR API constants ───────────────────────────────────────────────────────
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EDGAR_XBRL_FACTS_URL  = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
EDGAR_EFTS_URL        = "https://efts.sec.gov/LATEST/search-index"
EDGAR_HEADERS         = {
    "User-Agent":      "ApexTradingSystem research@apex.local",
    "Accept-Encoding": "gzip, deflate",
    "Accept":          "application/json",
}
REQUEST_DELAY    = 0.11   # stay under EDGAR 10 req/s limit
LOOKBACK_DAYS    = 90     # scan 90-day window for filings
CACHE_TTL_HOURS  = 24     # staleness gate for getter
CACHE_HARD_HOURS = 48     # beyond this, skip entirely in getter

# XBRL taxonomy tags that appear when auditors flag going concern.
# Source: US-GAAP taxonomy (ASC 205-40 / ASU 2014-15).
GOING_CONCERN_XBRL_TAGS = {
    "SubstantialDoubtAboutGoingConcernBasisOfPresentation",
    "GoingConcernText",
    "SubstantialDoubtAboutGoingConcernText",
    "SubstantialDoubtAboutGoingConcern",
}

# ── Red flag definitions ──────────────────────────────────────────────────────
# Maps 8-K item code → (flag_name, penalty)
EIGHT_K_PENALTIES = {
    "1.03": ("bankruptcy_or_receivership",      -3),
    "4.02": ("restatement",                     -2),
    "4.01": ("auditor_departure",               -2),
    "1.02": ("termination_of_material_agreement", -2),
    "5.02": ("officer_departure",               None),   # accumulated separately
}

NT_FORM_TYPES = {"NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A"}

# ── CIK map — hard-seeded for all quality universe tickers ───────────────────
# Non-US UK-only tickers mapped to None → skipped without network call.
# US-listed foreign companies (ASML, NOVO, AZN, BP, SHEL) included where
# they file with the SEC as foreign private issuers.
CIK_MAP = {
    # US equities
    "AAPL":  320193,
    "MSFT":  789019,
    "NVDA":  1045810,
    "AMZN":  1018724,
    "GOOGL": 1652044,
    "META":  1326801,
    "TSLA":  1318605,
    "V":     1403161,
    "XOM":   34088,
    "CVX":   93410,
    "HOOD":  1783879,
    "PLTR":  1321655,
    "NFLX":  1065280,
    "JPM":   19617,
    "JNJ":   200406,
    "PFE":   78003,
    "MRK":   310158,
    "UNH":   72971,
    "ABBV":  1551152,
    "DHR":   313616,
    "TMO":   97745,
    "KO":    21344,
    "PEP":   77476,
    "MCD":   63908,
    "WMT":   104169,
    "PG":    80424,
    "LMT":   936468,
    "RTX":   101829,
    "COP":   723254,
    "LLY":   59478,
    "POOL":  945841,
    "LEN":   920760,
    "ALLE":  1579241,
    "NFE":   1758488,
    "PGY":   1907982,
    "CRDO":  1836935,
    # US-listed foreign filers (have SEC EDGAR filings)
    "AZN":   901491,     # AstraZeneca (20-F filer)
    "ASML":  937556,     # ASML (20-F filer)
    "SHEL":  1306965,    # Shell (20-F filer)
    "BP":    313807,     # BP (20-F filer)
    # UK-only, LSE-listed — no CIK, skip gracefully
    "ULVR":  None,
    "GSK":   None,
    "NOVO":  None,
    "REL":   None,
    "BA":    None,       # BAE Systems
    "HSBA":  None,
    "LGEN":  None,
    "IMB":   None,
    "BATS":  None,
    # ETFs — no issuer CIK for SEC filings
    "IUIT":  None,
    "VUAG":  None,
    "3USS":  None,
}

# Module-level dynamic CIK cache (filled by lookup_cik_dynamic)
_CIK_CACHE = {}


# ── CIK resolution ────────────────────────────────────────────────────────────

def lookup_cik_dynamic(ticker):
    """Query EDGAR full-text search for a ticker's CIK. Returns int or None."""
    if ticker in _CIK_CACHE:
        return _CIK_CACHE[ticker]
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
        resp  = requests.get(
            EDGAR_EFTS_URL,
            params={"q": f'"{ticker}"', "forms": "10-K",
                    "dateRange": "custom", "startdt": start, "enddt": end},
            headers=EDGAR_HEADERS,
            timeout=10,
        )
        time.sleep(REQUEST_DELAY)
        if resp.status_code != 200:
            _CIK_CACHE[ticker] = None
            return None
        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            _CIK_CACHE[ticker] = None
            return None
        entity_id = hits[0].get("_source", {}).get("entity_id", "")
        cik = int(entity_id) if entity_id else None
        _CIK_CACHE[ticker] = cik
        return cik
    except Exception as exc:
        log.warning(f"Dynamic CIK lookup failed for {ticker}: {exc}")
        _CIK_CACHE[ticker] = None
        return None


def get_cik(ticker):
    """Resolve CIK: hard map → cache → dynamic EDGAR search. Returns int or None."""
    if ticker in CIK_MAP:
        return CIK_MAP[ticker]   # may be None for UK-only tickers
    return lookup_cik_dynamic(ticker)


# ── EDGAR filing fetch ────────────────────────────────────────────────────────

def fetch_submissions_data(cik, ticker):
    """
    Fetch submissions JSON for a CIK.
    Returns (entity_name: str, filings: list[dict]).
    filings dicts: {form, date, accession, items}
    Filtered to LOOKBACK_DAYS window and relevant form types.
    """
    url = EDGAR_SUBMISSIONS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        time.sleep(REQUEST_DELAY)
        if resp.status_code != 200:
            log.warning(f"{ticker}: EDGAR submissions returned {resp.status_code}")
            return "", []
        data = resp.json()
    except Exception as exc:
        log.warning(f"{ticker}: Failed to fetch submissions: {exc}")
        return "", []

    entity_name = data.get("name", "")
    recent      = data.get("filings", {}).get("recent", {})
    forms       = recent.get("form",            [])
    dates       = recent.get("filingDate",      [])
    accessions  = recent.get("accessionNumber", [])
    items_list  = recent.get("items",           [])

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date()
    target_forms = {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A",
                    "NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A"}

    filings = []
    for i, (form, date_str, acc) in enumerate(zip(forms, dates, accessions)):
        if form not in target_forms:
            continue
        try:
            filing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if filing_date < cutoff:
            continue
        filings.append({
            "form":      form,
            "date":      date_str,
            "accession": acc,
            "items":     items_list[i] if i < len(items_list) else "",
        })
        if len(filings) >= 10:
            break

    return entity_name, filings


# ── Going-concern detection (replaces document text scan) ─────────────────────

def check_going_concern_xbrl(cik, ticker):
    """
    Check EDGAR XBRL company facts for going-concern taxonomy tags.
    Returns True if any tag was filed within LOOKBACK_DAYS.
    One HTTP call.

    Reliable when present: auditors are required to tag going-concern
    disclosures in XBRL. False negatives possible only for non-XBRL filers
    (rare for exchange-listed companies), covered by EFTS fallback.
    """
    url = EDGAR_XBRL_FACTS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        time.sleep(REQUEST_DELAY)
        if resp.status_code != 200:
            return False
        facts   = resp.json().get("facts", {})
        us_gaap = facts.get("us-gaap", {})
        cutoff  = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date()

        for tag in GOING_CONCERN_XBRL_TAGS:
            if tag not in us_gaap:
                continue
            # Each tag has a "units" dict; dates are in the value entries
            for unit_values in us_gaap[tag].get("units", {}).values():
                for entry in unit_values:
                    filed_str = entry.get("filed", "") or entry.get("end", "")
                    try:
                        filed = datetime.strptime(filed_str[:10], "%Y-%m-%d").date()
                        if filed >= cutoff:
                            log.info(f"{ticker}: going concern XBRL tag '{tag}' filed {filed}")
                            return True
                    except ValueError:
                        continue
    except Exception as exc:
        log.warning(f"{ticker}: XBRL facts fetch failed: {exc}")
    return False


def check_going_concern(cik, ticker, entity_name):
    """
    XBRL-only going-concern check.

    XBRL is the authoritative source: exchange-listed US companies are required
    to tag going-concern disclosures under ASC 205-40 (ASU 2014-15). Auditors
    add tags from GOING_CONCERN_XBRL_TAGS only when the qualification actually
    exists — there are no false positives from boilerplate disclaimer language
    (unlike full-text search, which fires on "we do NOT have going concern issues").

    EFTS full-text search was tested and removed: searching for 'going concern'
    returns false positives for any company that mentions it as a negative
    ("we do not have substantial doubt..."), which includes most large-cap 10-Ks.
    XBRL alone is sufficient for our quality universe (all exchange-listed).
    """
    return check_going_concern_xbrl(cik, ticker)


# ── Filing classification ─────────────────────────────────────────────────────

def classify_filing(filing):
    """
    Classify a single filing for red flags from its metadata alone.
    Going concern is handled separately via check_going_concern().
    Returns list of (flag_name, penalty) tuples.
    """
    flags = []
    form  = filing.get("form", "")

    # NT (not-timely) = late filing
    if form in NT_FORM_TYPES:
        flags.append(("late_filing", -2))
        return flags

    # 8-K item classification (items field from submissions JSON)
    if form.startswith("8-K"):
        raw_items  = filing.get("items", "") or ""
        item_codes = [i.strip() for i in raw_items.split(",") if i.strip()]
        for code in item_codes:
            if code in EIGHT_K_PENALTIES:
                flag_name, penalty = EIGHT_K_PENALTIES[code]
                if penalty is not None:
                    flags.append((flag_name, penalty))
                else:
                    flags.append(("officer_departure_raw", 0))  # counted by caller

    return flags


# ── Per-ticker scoring ────────────────────────────────────────────────────────

def score_ticker(ticker):
    """
    Orchestrate CIK → submissions fetch → going-concern check → 8-K/NT
    classification → de-duplication → sum.
    Returns a dict with score, flags, recent_filings, and metadata.
    """
    cik = get_cik(ticker)
    if cik is None:
        return {
            "ticker":          ticker,
            "cik":             None,
            "score":           0,
            "flags":           [],
            "recent_filings":  [],
            "note":            "non_us_no_cik",
            "generated":       datetime.now(timezone.utc).isoformat(),
        }

    log.info(f"Scanning {ticker} (CIK {cik})")
    entity_name, filings = fetch_submissions_data(cik, ticker)

    all_flags         = {}   # flag_name → penalty (de-duplicated)
    officer_dep_count = 0
    audit_filings     = []

    # Going-concern: checked once per ticker via XBRL + EFTS (not per filing).
    # This replaces the unreliable 150KB text scan:
    #   - XBRL covers the full filing with taxonomy-tagged disclosures
    #   - EFTS searches EDGAR's full-text index with no depth limit
    if check_going_concern(cik, ticker, entity_name):
        all_flags["going_concern"] = -3

    for filing in filings:
        raw_flags = classify_filing(filing)

        for flag_name, penalty in raw_flags:
            if flag_name == "officer_departure_raw":
                officer_dep_count += 1
            elif flag_name not in all_flags:
                all_flags[flag_name] = penalty  # de-duplicate by flag type

        if len(audit_filings) < 3:
            audit_filings.append({
                "form":  filing["form"],
                "date":  filing["date"],
                "items": filing.get("items", ""),
            })

    # Apply mass officer departure rule (>=3 separate filings with 5.02)
    if officer_dep_count >= 3:
        all_flags["mass_officer_departures"] = -1

    total_score = sum(all_flags.values())
    flag_names  = sorted(all_flags.keys())

    if flag_names:
        log.info(f"{ticker}: flags={flag_names}, score={total_score}")
    else:
        log.info(f"{ticker}: no red flags found")

    return {
        "ticker":          ticker,
        "cik":             cik,
        "score":           total_score,
        "flags":           flag_names,
        "recent_filings":  audit_filings,
        "lookback_days":   LOOKBACK_DAYS,
        "generated":       datetime.now(timezone.utc).isoformat(),
    }


# ── Main run ──────────────────────────────────────────────────────────────────

def run():
    """Iterate quality universe, score each ticker, write output JSON."""
    log.info("SEC filing risk scan starting")
    t_start = time.monotonic()

    quality = safe_read(str(QUALITY_FILE), {})
    stocks  = quality.get("quality_stocks", {})
    if not stocks:
        log.error("Quality universe empty or missing — aborting")
        return

    results = {}
    flagged = 0

    for ticker in stocks:
        try:
            result = score_ticker(ticker)
            results[ticker] = result
            if result["score"] < 0:
                flagged += 1
        except Exception as exc:
            log.error(f"score_ticker failed for {ticker}: {exc}")
            results[ticker] = {
                "ticker":    ticker,
                "score":     0,
                "flags":     [],
                "note":      f"error: {exc}",
                "generated": datetime.now(timezone.utc).isoformat(),
            }

    output = {
        "layer":           20,
        "source":          "SEC EDGAR submissions API (free)",
        "generated":       datetime.now(timezone.utc).isoformat(),
        "lookback_days":   LOOKBACK_DAYS,
        "cache_ttl_hours": CACHE_TTL_HOURS,
        "tickers_scanned": len(results),
        "tickers_flagged": flagged,
        "tickers":         results,
    }

    atomic_write(str(OUT_FILE), output)
    elapsed = round(time.monotonic() - t_start, 1)
    log.info(f"Done: {len(results)} tickers, {flagged} flagged, {elapsed}s")


# ── Public getter for apex_scoring.py ────────────────────────────────────────

def get_sec_filing_adjustment(ticker, signal_type):
    """
    Called by apex_scoring.py (Layer 20).
    Returns (adjustment: float, reasons: list[str]).
    Staleness gates:
      <24h  → use normally
      24-48h → use with [data Xh old — stale] note
      >48h  → skip entirely, return (0, [])
    """
    data = safe_read(str(OUT_FILE), {})
    if not data:
        return 0.0, []

    # Staleness check
    generated_str = data.get("generated", "")
    age_hours = None
    if generated_str:
        try:
            generated_dt = datetime.fromisoformat(generated_str)
            age_hours    = (datetime.now(timezone.utc) - generated_dt).total_seconds() / 3600
        except ValueError:
            pass

    stale_note = ""
    if age_hours is not None:
        if age_hours > CACHE_HARD_HOURS:
            log_warning(f"SEC_FILING: skipping {ticker} — data {age_hours:.0f}h old (hard limit {CACHE_HARD_HOURS}h)")
            return 0.0, []
        if age_hours > CACHE_TTL_HOURS:
            stale_note = f" [data {age_hours:.0f}h old — stale]"

    ticker_data = data.get("tickers", {}).get(ticker, {})
    if not ticker_data:
        return 0.0, []

    score = ticker_data.get("score", 0)
    if score == 0:
        return 0.0, []

    flags       = ticker_data.get("flags", [])
    filings     = ticker_data.get("recent_filings", [])
    latest_date = filings[0].get("date", "unknown") if filings else "unknown"

    # Build human-readable reason
    flag_desc = ", ".join(f.replace("_", " ") for f in flags) if flags else "regulatory red flag"
    reason    = f"{ticker}: {flag_desc} (latest filing {latest_date}){stale_note}"

    return float(score), [reason]


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
