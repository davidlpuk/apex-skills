#!/usr/bin/env python3
# CRON: */15 8-16 * * 1-5
# Classifies current market event as RHETORIC/ACTION/WALKBACK/EXHAUSTED/NEUTRAL
# and writes apex-taco-state.json with a 24h TTL.
#
# Runs every 15 minutes during market hours. Sends Telegram only when
# classification STATUS changes — not on every run.

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        atomic_write, safe_read, log_error, log_warning, log_info,
        send_telegram, locked_read_modify_write
    )
except ImportError as _e:
    print(f"FATAL: apex_utils import failed: {_e}")
    sys.exit(1)

# ── CONSTANTS ────────────────────────────────────────────────────────────────
LOGS         = '/home/ubuntu/.picoclaw/logs'
CONFIG_FILE  = '/home/ubuntu/.picoclaw/apex-taco-config.json'
GEO_FILE     = f'{LOGS}/apex-geo-news.json'
STATE_FILE   = f'{LOGS}/apex-taco-state.json'
LOG_FILE     = f'{LOGS}/apex-taco-log.json'
OUTCOMES_FILE = f'{LOGS}/apex-taco-outcomes.json'

# Fallback keyword sets if config is unavailable
_DEFAULT_RHETORIC_KW = [
    "threat", "tariff", "warn", "ultimatum", "deadline",
    "consider", "propose", "may impose", "could sanction",
    "warning", "threatens", "threatened", "vow", "vows"
]
_DEFAULT_ACTION_KW = [
    "enacted", "signed", "effective", "implemented", "confirmed",
    "deployed", "launched", "executed", "took effect", "in effect",
    "imposes", "imposed", "activated"
]
_DEFAULT_WALKBACK_KW = [
    "paused", "delayed", "exemption", "negotiating", "softened",
    "reconsidering", "90-day", "extension", "pause", "delay",
    "exempt", "renegotiate", "eased", "backed down", "reversed"
]
_DEFAULT_FUNDAMENTAL_KW = [
    "earnings", "recession", "credit", "bank run", "default",
    "fed rate", "interest rate", "inflation", "jobs report",
    "gdp", "unemployment", "financial crisis", "bank failure"
]
# ─────────────────────────────────────────────────────────────────────────────


def load_config():
    """Load TACO config, returning defaults on failure."""
    return safe_read(CONFIG_FILE, {})


def fetch_vix_data():
    """Fetch VIX from yfinance and compute single-session spike percentage."""
    try:
        import yfinance as yf
        hist = yf.Ticker('^VIX').history(period='5d')
        if hist.empty or len(hist) < 2:
            log_warning("TACO classifier: insufficient VIX history")
            return {"today": None, "yesterday": None, "spike_pct": 0.0, "error": "insufficient_data"}
        today_vix     = float(hist['Close'].iloc[-1])
        yesterday_vix = float(hist['Close'].iloc[-2])
        spike_pct     = (today_vix - yesterday_vix) / yesterday_vix * 100
        return {
            "today":     round(today_vix, 2),
            "yesterday": round(yesterday_vix, 2),
            "spike_pct": round(spike_pct, 2)
        }
    except Exception as e:
        log_error(f"TACO classifier fetch_vix_data: {e}", exc=e)
        return {"today": None, "yesterday": None, "spike_pct": 0.0, "error": str(e)}


def score_headlines(headlines, keywords):
    """Count headlines that match each keyword (1 point per headline, not per hit)."""
    total = 0
    for headline in headlines:
        hl_lower = headline.lower()
        for kw in keywords:
            if re.search(re.escape(kw.lower()), hl_lower):
                total += 1
                break  # Each headline counted once regardless of how many keywords match
    return total


def detect_threat_type(headlines, geo_data):
    """Infer the type of threat from matched keywords to select the right ETF."""
    combined = " ".join(h.lower() for h in headlines)
    energy_flags = [f.get('title', '') for f in geo_data.get('energy_flags', [])]
    energy_text  = " ".join(e.lower() for e in energy_flags)

    if any(kw in combined or kw in energy_text for kw in ['energy', 'oil', 'iran', 'opec', 'saudi']):
        return "GEO_ENERGY"
    if any(kw in combined for kw in ['semiconductor', 'chip', 'nvidia', 'tsmc', 'advanced micro']):
        return "TARIFF_SEMI"
    if any(kw in combined for kw in ['china', 'beijing', 'chinese', 'prc']):
        return "TARIFF_CHINA"
    if any(kw in combined for kw in ['tech', 'technology', 'google', 'apple', 'amazon', 'microsoft']):
        return "TARIFF_TECH"
    if any(kw in combined for kw in ['defense', 'military', 'nato', 'steel', 'aluminum', 'industrial']):
        return "TARIFF_DEFENSE"
    return "TARIFF_BROAD"


def check_fundamental_vix(geo_data, config):
    """Return True if VIX spike looks fundamental (earnings/credit) rather than rhetorical."""
    fundamental_kw = (config.get('classifier', {})
                      .get('fundamental_vix_keywords', _DEFAULT_FUNDAMENTAL_KW))
    all_headlines = (
        [f.get('title', '') for f in geo_data.get('geo_flags', [])] +
        [f.get('title', '') for f in geo_data.get('energy_flags', [])]
    )
    combined = " ".join(h.lower() for h in all_headlines)
    matched = sum(1 for kw in fundamental_kw if kw.lower() in combined)
    return matched >= 2  # Two or more fundamental keywords → treat as macro event, not TACO


def load_taco_log_for_escalation():
    """Count RHETORIC events in the last 7 days from the audit log."""
    try:
        log_data = safe_read(LOG_FILE, [])
        if not isinstance(log_data, list):
            return 0, None
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        escalations = []
        for entry in log_data:
            if entry.get('event') != 'CLASSIFIED':
                continue
            if entry.get('status') != 'RHETORIC':
                continue
            ts_str = entry.get('classified_at', '')
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    escalations.append(entry)
            except Exception:
                pass
        latest_id = escalations[-1].get('event_id') if escalations else None
        return len(escalations), latest_id
    except Exception as e:
        log_error(f"TACO classifier load_taco_log_for_escalation: {e}", exc=e)
        return 0, None


def load_exhausted_flag():
    """Read the exhausted flag written by apex-taco-outcomes-tracker.py."""
    return safe_read(OUTCOMES_FILE, {}).get('exhausted', False)


def compute_confidence(rhetoric_score, action_score, walkback_score, escalation_count, config):
    """Compute [0.0–1.0] confidence score with escalation ladder penalty."""
    clf_cfg     = config.get('classifier', {})
    penalty_per = clf_cfg.get('escalation_confidence_penalty', 0.15)
    esc_thresh  = clf_cfg.get('escalation_count_threshold', 3)

    total = rhetoric_score + action_score
    if total == 0:
        return 0.0

    # Normalise rhetoric dominance to [0, 1]
    base_raw    = (rhetoric_score - action_score) / total
    base_normed = (base_raw + 1.0) / 2.0

    # Walkback boost: small boost if walkback also detected (double confirmation)
    if walkback_score >= 2:
        base_normed = min(1.0, base_normed + 0.05)

    # Escalation penalty — each extra escalation beyond threshold reduces confidence
    extra_escalations = max(0, escalation_count - esc_thresh)
    penalty = extra_escalations * penalty_per

    confidence = max(0.0, min(1.0, base_normed - penalty))
    return round(confidence, 2)


def is_state_stale(state):
    """Return True if the taco-state.json TTL has expired or is missing."""
    expires_at = state.get('expires_at', '')
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp
    except Exception:
        return True


def classify(vix_data, geo_data, config):
    """Run the full TACO classification pipeline and return state dict."""
    clf_cfg       = config.get('classifier', {})
    kw_cfg        = config.get('keywords', {})
    spike_pct     = vix_data.get('spike_pct', 0.0)
    threshold     = clf_cfg.get('vix_spike_threshold_pct', 15.0)
    r_min         = clf_cfg.get('rhetoric_score_min', 2)
    a_min         = clf_cfg.get('action_score_min', 2)
    w_min         = clf_cfg.get('walkback_score_min', 2)
    ttl_hours     = clf_cfg.get('state_ttl_hours', 24)

    rhetoric_kw = kw_cfg.get('rhetoric', _DEFAULT_RHETORIC_KW)
    action_kw   = kw_cfg.get('action',   _DEFAULT_ACTION_KW)
    walkback_kw = kw_cfg.get('walkback', _DEFAULT_WALKBACK_KW)

    all_headlines = (
        [f.get('title', '') for f in geo_data.get('geo_flags', [])] +
        [f.get('title', '') for f in geo_data.get('energy_flags', [])]
    )

    rhetoric_score = score_headlines(all_headlines, rhetoric_kw)
    action_score   = score_headlines(all_headlines, action_kw)
    walkback_score = score_headlines(all_headlines, walkback_kw)

    escalation_count, latest_event_id = load_taco_log_for_escalation()
    exhausted = load_exhausted_flag()

    # ── Classification priority order ────────────────────────────────────────
    if (spike_pct >= threshold
            and action_score >= a_min
            and action_score > rhetoric_score):
        status = "ACTION"

    elif spike_pct >= threshold and walkback_score >= w_min:
        status = "WALKBACK"

    elif exhausted:
        status = "EXHAUSTED"

    elif (spike_pct >= threshold
          and rhetoric_score >= r_min
          and rhetoric_score > action_score
          and not check_fundamental_vix(geo_data, config)):
        status = "RHETORIC"

    else:
        status = "NEUTRAL"

    confidence   = compute_confidence(rhetoric_score, action_score, walkback_score,
                                      escalation_count, config)
    threat_type  = detect_threat_type(all_headlines, geo_data)
    now          = datetime.now(timezone.utc)
    expires_at   = (now + timedelta(hours=ttl_hours)).isoformat()

    return {
        "status":           status,
        "confidence":       confidence,
        "vix_today":        vix_data.get("today"),
        "vix_spike_pct":    round(spike_pct, 2),
        "rhetoric_score":   rhetoric_score,
        "action_score":     action_score,
        "walkback_score":   walkback_score,
        "keyword_score":    rhetoric_score - action_score,
        "escalation_count": escalation_count,
        "threat_type":      threat_type,
        "trigger_headlines": all_headlines[:5],
        "classified_at":    now.isoformat(),
        "expires_at":       expires_at,
        "exhausted":        exhausted,
    }


def append_to_log(entry):
    """Append one entry to the append-only taco audit log."""
    try:
        def _modifier(data):
            if not isinstance(data, list):
                data = []
            data.append(entry)
            return data
        locked_read_modify_write(LOG_FILE, _modifier, default=[])
    except Exception as e:
        log_error(f"TACO classifier append_to_log: {e}", exc=e)


def main():
    """Classify current market event and write apex-taco-state.json."""
    try:
        if not safe_read(CONFIG_FILE, {}).get('enabled', True):
            log_info("TACO module disabled in config — skipping classifier")
            return

        config   = load_config()
        geo_data = safe_read(GEO_FILE, {})
        vix_data = fetch_vix_data()

        # Graceful degradation: if VIX fetch failed, write NEUTRAL and exit
        if vix_data.get('error') and vix_data.get('today') is None:
            log_warning(f"TACO classifier: VIX unavailable ({vix_data.get('error')}) — writing NEUTRAL")
            now = datetime.now(timezone.utc)
            ttl = config.get('classifier', {}).get('state_ttl_hours', 24)
            neutral_state = {
                "status": "NEUTRAL", "confidence": 0.0, "vix_today": None,
                "vix_spike_pct": 0.0, "rhetoric_score": 0, "action_score": 0,
                "walkback_score": 0, "keyword_score": 0, "escalation_count": 0,
                "threat_type": "TARIFF_BROAD", "trigger_headlines": [],
                "classified_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=ttl)).isoformat(),
                "exhausted": False, "vix_error": True
            }
            atomic_write(STATE_FILE, neutral_state)
            return

        # Read previous state to detect status changes
        prev_state  = safe_read(STATE_FILE, {})
        prev_status = prev_state.get('status', 'NEUTRAL')

        state = classify(vix_data, geo_data, config)
        atomic_write(STATE_FILE, state)

        # Log every classification for audit trail
        log_entry = {
            "event":         "CLASSIFIED",
            "status":        state["status"],
            "confidence":    state["confidence"],
            "vix_spike_pct": state["vix_spike_pct"],
            "rhetoric_score": state["rhetoric_score"],
            "action_score":   state["action_score"],
            "walkback_score": state["walkback_score"],
            "threat_type":    state["threat_type"],
            "classified_at":  state["classified_at"],
        }
        append_to_log(log_entry)

        # Only Telegram on status transition — not every 15 min
        if state["status"] != prev_status:
            status_emoji = {
                "RHETORIC":  "📢",
                "ACTION":    "🔴",
                "WALKBACK":  "🔄",
                "EXHAUSTED": "⚠️",
                "NEUTRAL":   "✅"
            }.get(state["status"], "🌮")
            msg = (
                f"{status_emoji} TACO CLASSIFIER: {prev_status} → {state['status']}\n\n"
                f"Confidence: {state['confidence']:.0%}\n"
                f"VIX spike: {state['vix_spike_pct']:+.1f}%\n"
                f"Rhetoric score: {state['rhetoric_score']} | "
                f"Action score: {state['action_score']} | "
                f"Walkback score: {state['walkback_score']}\n"
                f"Threat type: {state['threat_type']}\n"
                f"Expires: {state['expires_at'][:16]} UTC"
            )
            send_telegram(msg)

        log_info(f"TACO classifier: {state['status']} (conf={state['confidence']:.2f}, "
                 f"VIX spike={state['vix_spike_pct']:+.1f}%)")

    except Exception as e:
        log_error(f"TACO classifier fatal: {e}", exc=e)
        sys.exit(0)  # Always exit 0 for cron health


if __name__ == "__main__":
    main()
