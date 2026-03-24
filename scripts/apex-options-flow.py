#!/usr/bin/env python3
"""
apex-options-flow.py — Layer 17: Unusual Options Activity Signal
Detects smart money positioning via options flow analysis.
Uses yfinance (free, no API key) — options chain data.

Signal logic:
  Bullish signals (+1 each, max +3):
    - Unusual call volume (>3x avg open interest ratio)
    - Large OTM call sweep (volume > 5000, OTM > 2%)
    - Call/Put volume ratio > 2.0 (dominant call buying)
    - IV skew bullish (call IV < put IV — market paying for upside)

  Bearish signals (-1 each, max -3):
    - Unusual put volume (>3x avg open interest ratio)
    - Large OTM put sweep (volume > 5000, OTM > 2%)
    - Put/Call volume ratio > 2.0 (dominant put buying)
    - IV skew bearish (put IV < call IV — market hedging downside)

Output: ~/.picoclaw/data/apex-options-signal.json
Cron:   every 2h during market hours weekdays
"""

import json
import time
import logging
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# Paths
DATA_DIR = Path.home() / ".picoclaw" / "data"
LOG_DIR  = Path.home() / ".picoclaw" / "logs"
OUT_FILE = DATA_DIR / "apex-options-signal.json"
LOG_FILE = LOG_DIR  / "apex-options-flow.log"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OPTS] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("apex-options")

# US instruments only — options not available for LSE
INSTRUMENTS = {
    "AAPL":  "AAPL",
    "MSFT":  "MSFT",
    "NVDA":  "NVDA",
    "AMZN":  "AMZN",
    "GOOGL": "GOOGL",
    "META":  "META",
    "TSLA":  "TSLA",
    "V":     "V",
    "XOM":   "XOM",
    "CVX":   "CVX",
    "HOOD":  "HOOD",
    "PLTR":  "PLTR",
    "NFLX":  "NFLX",
}

# Thresholds
MIN_VOLUME        = 500     # ignore low-volume contracts
OTM_SWEEP_VOLUME  = 3000    # min volume for OTM sweep signal
OTM_PCT_THRESHOLD = 0.02    # 2% OTM to qualify as OTM sweep
CP_RATIO_BULL     = 1.8     # call/put ratio above this = bullish
CP_RATIO_BEAR     = 0.55    # call/put ratio below this = bearish
VOL_OI_THRESHOLD  = 2.5     # volume/openInterest ratio = unusual activity
REQUEST_DELAY     = 1.0


def get_options_data(ticker: str) -> dict:
    """
    Fetch options chain for nearest 3 expiries and analyse flow.
    Returns structured signal dict.
    """
    try:
        t       = yf.Ticker(ticker)
        expiries = t.options[:3]  # nearest 3 expiries
        if not expiries:
            return {"error": "no_expiries"}

        # Get current price
        hist = t.history(period="2d")
        if hist.empty:
            return {"error": "no_price"}
        current_price = float(hist["Close"].iloc[-1])

        all_calls = []
        all_puts  = []

        for expiry in expiries:
            try:
                chain = t.option_chain(expiry)
                calls = chain.calls.copy()
                puts  = chain.puts.copy()
                calls["expiry"] = expiry
                puts["expiry"]  = expiry
                # Filter low volume
                calls = calls[calls["volume"].fillna(0) > MIN_VOLUME]
                puts  = puts[puts["volume"].fillna(0) > MIN_VOLUME]
                all_calls.append(calls)
                all_puts.append(puts)
                time.sleep(0.3)
            except Exception as e:
                log.debug(f"{ticker} {expiry}: chain fetch error — {e}")
                continue

        if not all_calls and not all_puts:
            return {"error": "no_chain_data"}

        calls_df = pd.concat(all_calls, ignore_index=True) if all_calls else pd.DataFrame()
        puts_df  = pd.concat(all_puts,  ignore_index=True) if all_puts  else pd.DataFrame()

        return analyse_flow(ticker, current_price, calls_df, puts_df)

    except Exception as e:
        log.warning(f"{ticker}: options fetch failed — {e}")
        return {"error": str(e)}


def analyse_flow(ticker: str, price: float, calls: pd.DataFrame, puts: pd.DataFrame) -> dict:
    """Core analysis — detects unusual activity and scores it."""
    score   = 0
    reasons = []
    detail  = {}

    # Total volumes
    total_call_vol = float(calls["volume"].sum()) if not calls.empty else 0
    total_put_vol  = float(puts["volume"].sum())  if not puts.empty  else 0
    total_vol      = total_call_vol + total_put_vol

    detail["total_call_volume"] = int(total_call_vol)
    detail["total_put_volume"]  = int(total_put_vol)
    detail["current_price"]     = round(price, 2)

    if total_vol == 0:
        return {"score": 0, "reasons": ["no_volume"], "detail": detail}

    # ── Call/Put ratio ────────────────────────────────────────────────────────
    cp_ratio = round(total_call_vol / total_put_vol, 2) if total_put_vol > 0 else 99.0
    detail["cp_ratio"] = cp_ratio

    if cp_ratio >= CP_RATIO_BULL:
        score += 1
        reasons.append(f"C/P ratio {cp_ratio:.1f}x — dominant call buying")
    elif cp_ratio <= CP_RATIO_BEAR:
        score -= 1
        reasons.append(f"C/P ratio {cp_ratio:.1f}x — dominant put buying")

    # ── Unusual volume/OI ratio ───────────────────────────────────────────────
    if not calls.empty and "openInterest" in calls.columns:
        calls_valid = calls[calls["openInterest"].fillna(0) > 100].copy()
        if not calls_valid.empty:
            calls_valid["vol_oi"] = calls_valid["volume"] / calls_valid["openInterest"].replace(0, 1)
            unusual_calls = calls_valid[calls_valid["vol_oi"] > VOL_OI_THRESHOLD]
            if len(unusual_calls) >= 2:
                top = unusual_calls.nlargest(1, "volume").iloc[0]
                score += 1
                reasons.append(
                    f"Unusual call activity: {len(unusual_calls)} contracts "
                    f"vol/OI>{VOL_OI_THRESHOLD}x (top: ${top['strike']:.0f} "
                    f"vol:{int(top['volume']):,})"
                )
                detail["unusual_calls"] = len(unusual_calls)

    if not puts.empty and "openInterest" in puts.columns:
        puts_valid = puts[puts["openInterest"].fillna(0) > 100].copy()
        if not puts_valid.empty:
            puts_valid["vol_oi"] = puts_valid["volume"] / puts_valid["openInterest"].replace(0, 1)
            unusual_puts = puts_valid[puts_valid["vol_oi"] > VOL_OI_THRESHOLD]
            if len(unusual_puts) >= 2:
                top = unusual_puts.nlargest(1, "volume").iloc[0]
                score -= 1
                reasons.append(
                    f"Unusual put activity: {len(unusual_puts)} contracts "
                    f"vol/OI>{VOL_OI_THRESHOLD}x (top: ${top['strike']:.0f} "
                    f"vol:{int(top['volume']):,})"
                )
                detail["unusual_puts"] = len(unusual_puts)

    # ── OTM sweep detection ───────────────────────────────────────────────────
    if not calls.empty:
        otm_calls = calls[calls["strike"] > price * (1 + OTM_PCT_THRESHOLD)]
        big_otm_calls = otm_calls[otm_calls["volume"].fillna(0) >= OTM_SWEEP_VOLUME]
        if not big_otm_calls.empty:
            top = big_otm_calls.nlargest(1, "volume").iloc[0]
            pct_otm = round((top["strike"] - price) / price * 100, 1)
            score += 1
            reasons.append(
                f"OTM call sweep: ${top['strike']:.0f} "
                f"({pct_otm:.1f}% OTM) vol:{int(top['volume']):,}"
            )
            detail["otm_call_sweep"] = {
                "strike": float(top["strike"]),
                "volume": int(top["volume"]),
                "pct_otm": pct_otm
            }

    if not puts.empty:
        otm_puts = puts[puts["strike"] < price * (1 - OTM_PCT_THRESHOLD)]
        big_otm_puts = otm_puts[otm_puts["volume"].fillna(0) >= OTM_SWEEP_VOLUME]
        if not big_otm_puts.empty:
            top = big_otm_puts.nlargest(1, "volume").iloc[0]
            pct_otm = round((price - top["strike"]) / price * 100, 1)
            score -= 1
            reasons.append(
                f"OTM put sweep: ${top['strike']:.0f} "
                f"({pct_otm:.1f}% OTM) vol:{int(top['volume']):,}"
            )
            detail["otm_put_sweep"] = {
                "strike": float(top["strike"]),
                "volume": int(top["volume"]),
                "pct_otm": pct_otm
            }

    # ── IV skew ───────────────────────────────────────────────────────────────
    if not calls.empty and not puts.empty and "impliedVolatility" in calls.columns:
        # ATM options: within 2% of current price
        atm_calls = calls[abs(calls["strike"] - price) / price < 0.02]
        atm_puts  = puts[abs(puts["strike"]  - price) / price < 0.02]
        if not atm_calls.empty and not atm_puts.empty:
            avg_call_iv = float(atm_calls["impliedVolatility"].mean())
            avg_put_iv  = float(atm_puts["impliedVolatility"].mean())
            iv_skew     = round(avg_put_iv - avg_call_iv, 4)
            detail["iv_skew"] = iv_skew
            detail["avg_call_iv"] = round(avg_call_iv, 4)
            detail["avg_put_iv"]  = round(avg_put_iv, 4)

            if iv_skew > 0.05:
                score -= 1
                reasons.append(
                    f"IV skew bearish: put IV {avg_put_iv:.1%} > "
                    f"call IV {avg_call_iv:.1%} — market hedging downside"
                )
            elif iv_skew < -0.02:
                score += 1
                reasons.append(
                    f"IV skew bullish: call IV {avg_call_iv:.1%} > "
                    f"put IV {avg_put_iv:.1%} — market pricing upside"
                )

    # Cap
    score = max(-3, min(3, score))

    return {
        "ticker":  ticker,
        "score":   score,
        "reasons": reasons,
        "detail":  detail,
    }


def atomic_write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def run():
    log.info("=== Options Flow Layer 17 run starting ===")
    start_ts = datetime.utcnow().isoformat()
    results  = {}

    for ticker in INSTRUMENTS:
        log.info(f"Processing {ticker}")
        sig = get_options_data(ticker)
        results[ticker] = sig
        score   = sig.get("score", 0)
        reasons = sig.get("reasons", [])
        bar     = "▲" * max(0, score) + "▼" * max(0, -score)
        log.info(f"  {ticker:<6} {score:+d} {bar:<4} {reasons[0][:60] if reasons else sig.get('error','—')}")
        time.sleep(REQUEST_DELAY)

    output = {
        "layer":     17,
        "source":    "yfinance options chain",
        "generated": start_ts,
        "signals":   results,
        "summary": {
            t: {"score": v.get("score", 0), "reasons": v.get("reasons", [])}
            for t, v in results.items()
        }
    }

    atomic_write(OUT_FILE, output)
    log.info(f"Written -> {OUT_FILE}")

    log.info("── Options flow summary ────────────────────────")
    for t, v in sorted(results.items(), key=lambda x: -x[1].get("score", 0)):
        s = v.get("score", 0)
        r = v.get("reasons", [v.get("error", "—")])
        log.info(f"  {t:<6} {s:+d}  {r[0][:65] if r else '—'}")
    log.info("=== Options Flow Layer 17 run complete ===")


def get_options_adjustment(ticker: str, signal_type: str = "TREND") -> tuple:
    """Called by decision engine Layer 17."""
    if not OUT_FILE.exists():
        return 0, []
    try:
        data    = json.loads(OUT_FILE.read_text())
        # Staleness — options flow stale after 4h (cron fires every 2h, 2h margin)
        generated = datetime.fromisoformat(data.get("generated", ""))
        age_h     = (datetime.utcnow() - generated).total_seconds() / 3600
        if age_h > 4:
            return 0, []

        sig     = data.get("signals", {}).get(ticker, {})
        score   = sig.get("score", 0)
        reasons = sig.get("reasons", [])

        if signal_type == "CONTRARIAN" and score < 0:
            # Unusual put buying = more oversold = better contrarian entry
            return abs(score), [f"Options: heavy put buying confirms oversold ({reasons[0][:50] if reasons else ''})"]

        return score, reasons
    except Exception:
        return 0, []


if __name__ == "__main__":
    run()
