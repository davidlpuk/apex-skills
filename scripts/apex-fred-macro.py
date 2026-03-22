#!/usr/bin/env python3
"""
apex-fred-macro.py — Layer 16: FRED Economic Data Signal
"""
import json, time, logging, requests
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".picoclaw" / "data"
LOG_DIR  = Path.home() / ".picoclaw" / "logs"
OUT_FILE = DATA_DIR / "apex-fred-signal.json"
LOG_FILE = LOG_DIR  / "apex-fred-macro.log"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FRED] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("apex-fred")

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
SERIES = {
    "FEDFUNDS": {"name": "Fed Funds Rate"},
    "CPIAUCSL": {"name": "CPI Urban"},
    "UNRATE":   {"name": "Unemployment Rate"},
    "T10Y2Y":   {"name": "10Y-2Y Yield Spread"},
    "UMCSENT":  {"name": "Consumer Sentiment"},
    "GDPC1":    {"name": "Real GDP"},
    "ICSA":     {"name": "Initial Jobless Claims"},
}
HEADERS = {"User-Agent": "Apex Trading System david@apex.local"}

def fetch_series(series_id):
    url = FRED_BASE.format(series=series_id)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        rows = []
        for line in r.text.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) != 2: continue
            date_str, val_str = parts
            val_str = val_str.strip()
            if val_str in ("", ".", "NA"): continue
            try:
                rows.append({"date": date_str.strip(), "value": float(val_str)})
            except ValueError:
                continue
        return rows
    except Exception as e:
        log.warning(f"{series_id}: fetch failed - {e}")
        return []

def yoy_change(rows):
    if len(rows) < 13: return None
    current, year_ago = rows[-1]["value"], rows[-13]["value"]
    if year_ago == 0: return None
    return round((current - year_ago) / abs(year_ago) * 100, 2)

def score_fred_data(data):
    score, reasons, detail = 0, [], {}

    # Fed Funds Rate
    ff = data.get("FEDFUNDS", [])
    if len(ff) >= 2:
        ff_cur, ff_prev = ff[-1]["value"], ff[-2]["value"]
        ff_chg = round(ff_cur - ff_prev, 2)
        detail["fed_funds"] = {"current": ff_cur, "change": ff_chg}
        if ff_cur < 3.0:
            score += 1; reasons.append(f"Fed Funds {ff_cur:.2f}% - easy money regime")
        elif ff_cur > 5.0 and ff_chg >= 0:
            score -= 1; reasons.append(f"Fed Funds {ff_cur:.2f}% high and not cutting")
        elif ff_chg < -0.1:
            score += 1; reasons.append(f"Fed cutting ({ff_chg:+.2f}%) - easing cycle")
        log.info(f"Fed Funds: {ff_cur:.2f}% (chg {ff_chg:+.2f}%)")

    # CPI
    cpi = data.get("CPIAUCSL", [])
    cpi_yoy = yoy_change(cpi)
    if cpi_yoy is not None:
        detail["cpi_yoy"] = cpi_yoy
        if cpi_yoy < 2.5:
            score += 1; reasons.append(f"CPI {cpi_yoy:.1f}% YoY - inflation contained")
        elif cpi_yoy > 4.0:
            score -= 1; reasons.append(f"CPI {cpi_yoy:.1f}% YoY - elevated inflation")
        if len(cpi) >= 14:
            prev_yoy = round((cpi[-2]["value"] - cpi[-14]["value"]) / abs(cpi[-14]["value"]) * 100, 2)
            if cpi_yoy > prev_yoy + 0.3:
                score -= 1; reasons.append(f"CPI re-accelerating ({prev_yoy:.1f}% to {cpi_yoy:.1f}%)")
        log.info(f"CPI YoY: {cpi_yoy:.2f}%")

    # Unemployment
    ur = data.get("UNRATE", [])
    if len(ur) >= 3:
        ur_cur, ur_3m = ur[-1]["value"], ur[-3]["value"]
        ur_trend = round(ur_cur - ur_3m, 2)
        detail["unemployment"] = {"current": ur_cur, "trend_3m": ur_trend}
        if ur_cur < 4.5 and ur_trend <= 0.1:
            score += 1; reasons.append(f"Unemployment {ur_cur:.1f}% - tight labour")
        elif ur_trend > 0.3:
            score -= 1; reasons.append(f"Unemployment rising +{ur_trend:.1f}% in 3m")
        log.info(f"Unemployment: {ur_cur:.1f}% (3m trend {ur_trend:+.2f}%)")

    # Yield curve
    yc = data.get("T10Y2Y", [])
    if len(yc) >= 5:
        yc_cur, yc_prev = yc[-1]["value"], yc[-5]["value"]
        detail["yield_curve"] = {"spread": yc_cur, "week_ago": yc_prev}
        if yc_cur > 0.25:
            score += 1; reasons.append(f"Yield curve +{yc_cur:.2f}% - growth supportive")
        elif yc_cur < -0.2:
            score -= 1; reasons.append(f"Yield curve {yc_cur:.2f}% inverted - recession signal")
        log.info(f"Yield curve (10Y-2Y): {yc_cur:.2f}%")

    # Consumer sentiment
    cs = data.get("UMCSENT", [])
    if len(cs) >= 2:
        cs_cur, cs_prev = cs[-1]["value"], cs[-2]["value"]
        cs_chg = round(cs_cur - cs_prev, 1)
        detail["consumer_sentiment"] = {"current": cs_cur, "change": cs_chg}
        if cs_chg > 3.0:
            score += 1; reasons.append(f"Consumer sentiment +{cs_chg}pts - demand positive")
        elif cs_chg < -5.0:
            score -= 1; reasons.append(f"Consumer sentiment {cs_chg}pts - demand concern")
        log.info(f"Consumer sentiment: {cs_cur:.1f} (chg {cs_chg:+.1f})")

    # Jobless claims
    ic = data.get("ICSA", [])
    if len(ic) >= 2:
        ic_cur, ic_prev = ic[-1]["value"], ic[-2]["value"]
        ic_chg_pct = round((ic_cur - ic_prev) / ic_prev * 100, 1) if ic_prev else 0
        detail["jobless_claims"] = {"current": ic_cur, "chg_pct": ic_chg_pct}
        if ic_chg_pct > 10.0:
            score -= 1; reasons.append(f"Jobless claims +{ic_chg_pct:.1f}% WoW - labour weakening")
        elif ic_chg_pct < -5.0:
            score += 1; reasons.append(f"Jobless claims {ic_chg_pct:.1f}% WoW - labour strengthening")
        log.info(f"Initial claims: {ic_cur:,.0f} (chg {ic_chg_pct:+.1f}%)")

    # GDP
    gdp = data.get("GDPC1", [])
    if len(gdp) >= 2:
        gdp_qoq = round((gdp[-1]["value"] - gdp[-2]["value"]) / abs(gdp[-2]["value"]) * 100 * 4, 2)
        detail["gdp_annualised"] = gdp_qoq
        if gdp_qoq > 2.5:
            score += 1; reasons.append(f"GDP {gdp_qoq:.1f}% annualised - strong growth")
        elif gdp_qoq < 0:
            score -= 1; reasons.append(f"GDP {gdp_qoq:.1f}% - contraction territory")
        log.info(f"GDP annualised: {gdp_qoq:.2f}%")

    score = max(-3, min(3, score))
    if score >= 2:   regime = "EXPANSION"
    elif score >= 0: regime = "NEUTRAL"
    elif score >= -1:regime = "CAUTION"
    else:            regime = "CONTRACTION"

    return {"score": score, "regime": regime, "reasons": reasons, "detail": detail}

def atomic_write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)

def run():
    log.info("=== FRED Layer 16 run starting ===")
    start_ts = datetime.utcnow().isoformat()
    raw_data = {}
    for series_id in SERIES:
        log.info(f"Fetching {series_id}")
        rows = fetch_series(series_id)
        if rows:
            raw_data[series_id] = rows
            log.info(f"  {series_id}: latest {rows[-1]['date']} = {rows[-1]['value']}")
        time.sleep(0.3)

    result = score_fred_data(raw_data)
    log.info(f"FRED score: {result['score']:+d} | Regime: {result['regime']}")
    for r in result["reasons"]:
        log.info(f"  -> {r}")

    output = {
        "layer": 16, "source": "FRED", "generated": start_ts,
        "score": result["score"], "regime": result["regime"],
        "reasons": result["reasons"], "detail": result["detail"],
        "series_fetched": list(raw_data.keys()),
    }
    atomic_write(OUT_FILE, output)
    log.info(f"Written -> {OUT_FILE}")
    log.info("=== FRED Layer 16 run complete ===")

def get_fred_adjustment(signal_type="TREND"):
    if not OUT_FILE.exists(): return 0, []
    try:
        data    = json.loads(OUT_FILE.read_text())
        score   = data.get("score", 0)
        regime  = data.get("regime", "NEUTRAL")
        reasons = data.get("reasons", [])
        if signal_type == "CONTRARIAN":
            if score <= -2: return 1, [f"FRED {regime} - macro stress = contrarian opportunity"]
            elif score >= 2: return -1, [f"FRED {regime} - strong macro reduces contrarian edge"]
            return 0, []
        else:
            if score >= 2:  return 1, [f"FRED {regime} - macro tailwind ({reasons[0][:50] if reasons else ""})"]
            elif score <= -2: return -1, [f"FRED {regime} - macro headwind ({reasons[0][:50] if reasons else ""})"]
            return 0, []
    except Exception:
        return 0, []

if __name__ == "__main__":
    run()
