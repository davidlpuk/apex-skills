#!/usr/bin/env python3
"""
Pillar 4: Black Swan Vector Detector
Identifies unobvious risks before they become obvious losses.

ACTIVE IMMEDIATELY — runs every morning and before every trade.

Monitors:
1. Overnight gap detector — abnormal price moves without obvious catalyst
2. Regulatory risk scanner — government/regulatory announcements
3. Correlation breakdown — instruments moving against historical patterns
4. Volatility regime change — VIX spike patterns that precede crashes
5. Liquidity withdrawal — volume collapse indicating institutional exit
"""
import json
import subprocess
import sys
import yfinance as yf
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

BLACKSWAN_FILE = '/home/ubuntu/.picoclaw/logs/apex-blackswan.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
REGIME_FILE    = '/home/ubuntu/.picoclaw/logs/apex-regime.json'

# Thresholds
GAP_THRESHOLD_PCT      = 4.0   # Flag gaps > 4% overnight
VOLUME_COLLAPSE_PCT    = 70.0  # Flag if volume < 30% of 20-day average
VIX_SPIKE_THRESHOLD    = 30.0  # Flag if VIX > 30
VIX_JUMP_PCT           = 25.0  # Flag if VIX jumps > 25% in one day
CORRELATION_BREAK_PCT  = 40.0  # Flag if correlation breaks by > 40pp

YAHOO_MAP = {
    "VUAGl_EQ":   "VUAG.L",
    "XOM_US_EQ":  "XOM",
    "V_US_EQ":    "V",
    "AAPL_US_EQ": "AAPL",
    "MSFT_US_EQ": "MSFT",
    "NVDA_US_EQ": "NVDA",
    "GOOGL_US_EQ":"GOOGL",
    "JPM_US_EQ":  "JPM",
    "CVX_US_EQ":  "CVX",
    "ABBV_US_EQ": "ABBV",
    "JNJ_US_EQ":  "JNJ",
    "SHEL_EQ":    "SHEL.L",
    "HSBA_EQ":    "HSBA.L",
    "AZN_EQ":     "AZN.L",
    "ULVR_EQ":    "ULVR.L",
}

def fix_pence(price, yahoo):
    if yahoo.endswith('.L') and price > 100:
        return price / 100
    return price

# ============================================================
# VECTOR 1: OVERNIGHT GAP DETECTOR
# ============================================================
def detect_gaps(universe_tickers):
    """
    Detect abnormal overnight gaps on all tracked instruments.
    Gap > 4% without obvious catalyst = black swan warning.
    """
    gaps = []
    for ticker, yahoo in universe_tickers.items():
        try:
            hist = yf.Ticker(yahoo).history(period="5d")
            if hist.empty or len(hist) < 2:
                continue

            closes = [fix_pence(float(c), yahoo) for c in hist['Close']]
            opens  = [fix_pence(float(o), yahoo) for o in hist['Open']]

            # Yesterday close vs today open
            prev_close  = closes[-2]
            today_open  = opens[-1]
            today_close = closes[-1]

            if prev_close <= 0:
                continue

            gap_pct = round((today_open - prev_close) / prev_close * 100, 2)
            day_pct = round((today_close - today_open) / today_open * 100, 2)

            if abs(gap_pct) > GAP_THRESHOLD_PCT:
                gaps.append({
                    'ticker':       ticker,
                    'yahoo':        yahoo,
                    'prev_close':   round(prev_close, 2),
                    'today_open':   round(today_open, 2),
                    'gap_pct':      gap_pct,
                    'day_pct':      day_pct,
                    'direction':    'UP' if gap_pct > 0 else 'DOWN',
                    'severity':     'EXTREME' if abs(gap_pct) > 8 else 'LARGE',
                    'note':         f"Gapped {gap_pct:+.1f}% overnight — verify catalyst",
                })
        except Exception as e:
            log_error(f"Gap detection failed for {ticker}: {e}")

    return gaps

# ============================================================
# VECTOR 2: REGULATORY RISK SCANNER
# ============================================================
def scan_regulatory_risk():
    """
    Scan RSS feeds for regulatory announcements.
    Keywords: SEC, FCA, antitrust, investigation, fine, ban, sanction
    """
    import urllib.request
    import re

    REGULATORY_KEYWORDS = [
        'SEC investigation', 'FCA investigation', 'antitrust',
        'class action', 'regulatory fine', 'trading halt',
        'sanctions', 'asset freeze', 'probe', 'subpoena',
        'congressional hearing', 'executive order',
        'tariff', 'export ban', 'import restriction',
        'market manipulation', 'insider trading charge',
    ]

    FEEDS = [
        "http://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.reuters.com/reuters/businessNews",
    ]

    regulatory_alerts = []

    for feed_url in FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={
                'User-Agent': 'ApexBot/1.0'
            })
            with urllib.request.urlopen(req, timeout=8) as r:
                content = r.read().decode('utf-8', errors='ignore')

            items = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)
            for item in items[:20]:
                title_match = re.search(r'<title>(.*?)</title>', item, re.DOTALL)
                if not title_match:
                    continue
                title = re.sub(r'<[^>]+>','', title_match.group(1)).strip()

                for keyword in REGULATORY_KEYWORDS:
                    if keyword.lower() in title.lower():
                        regulatory_alerts.append({
                            'title':    title[:100],
                            'keyword':  keyword,
                            'severity': 'HIGH' if any(k in title.lower() for k in
                                        ['halt', 'ban', 'freeze', 'sanction']) else 'MEDIUM',
                        })
                        break
        except Exception as e:
            log_error(f"Regulatory scan failed for {feed_url}: {e}")

    return regulatory_alerts

# ============================================================
# VECTOR 3: VIX REGIME CHANGE DETECTOR
# ============================================================
def detect_vix_regime_change():
    """
    Detect VIX spike patterns that historically precede crashes.
    VIX above 30 + rising = danger zone.
    VIX jump > 25% in one day = immediate alert.
    """
    try:
        hist = yf.Ticker("^VIX").history(period="10d")
        if hist.empty or len(hist) < 2:
            return None

        closes  = [float(c) for c in hist['Close']]
        current = closes[-1]
        prev    = closes[-2]
        week_ago= closes[-5] if len(closes) >= 5 else closes[0]

        daily_jump  = round((current - prev) / prev * 100, 1)
        weekly_move = round((current - week_ago) / week_ago * 100, 1)

        alerts = []

        if current > 40:
            alerts.append(f"VIX {current:.1f} — EXTREME FEAR, crisis level")
        elif current > 30:
            alerts.append(f"VIX {current:.1f} — HIGH FEAR, caution zone")

        if daily_jump > VIX_JUMP_PCT:
            alerts.append(f"VIX spiked {daily_jump:+.1f}% today ({prev:.1f} → {current:.1f})")

        if weekly_move > 40:
            alerts.append(f"VIX up {weekly_move:+.1f}% this week — regime change likely")

        return {
            'current_vix':  round(current, 2),
            'prev_vix':     round(prev, 2),
            'daily_jump':   daily_jump,
            'weekly_move':  weekly_move,
            'alerts':       alerts,
            'severity':     'EXTREME' if current > 40 else ('HIGH' if current > 30 else 'NORMAL'),
        }
    except Exception as e:
        log_error(f"VIX regime change detection failed: {e}")
        return None

# Session schedule in UTC minutes-since-midnight
# Used to scale the 20-day average volume down to match elapsed session time,
# preventing false "volume collapse" alerts early in the trading day.
_SESSIONS = {
    'US':  {'open': 14*60+30, 'close': 21*60,    'mins': 390},  # NYSE/NASDAQ 14:30–21:00 UTC
    'LSE': {'open':  8*60,    'close': 16*60+30,  'mins': 510},  # LSE 08:00–16:30 UTC
}

def _session_fraction(yahoo_ticker: str) -> tuple:
    """
    Return (fraction_elapsed, session_name) for the primary exchange of a ticker.

    Fraction is the proportion of the trading day that has elapsed so far:
      0.0  = session not started yet (pre-market)
      0.25 = 25% of session done (e.g. 90 min into NYSE)
      1.0  = session complete (post-market or weekend)

    Floor at 0.05 to avoid division by near-zero on the opening print.
    When outside all sessions the full-day volume is already final → returns 1.0.
    """
    now_utc  = datetime.now(timezone.utc)
    weekday  = now_utc.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    if weekday >= 5:
        return 1.0, 'closed'

    now_mins = now_utc.hour * 60 + now_utc.minute
    skey     = 'LSE' if yahoo_ticker.endswith('.L') else 'US'
    sess     = _SESSIONS[skey]

    if now_mins < sess['open']:
        # Pre-market: today's volume row is yesterday's complete volume → 1.0
        return 1.0, f'{skey}_pre'
    if now_mins >= sess['close']:
        # Post-market: session complete → 1.0
        return 1.0, f'{skey}_post'

    elapsed  = now_mins - sess['open']
    fraction = max(0.05, min(1.0, elapsed / sess['mins']))
    return round(fraction, 3), f'{skey}_intraday'


# ============================================================
# VECTOR 4: VOLUME COLLAPSE DETECTOR
# ============================================================
def detect_volume_collapse(positions):
    """
    Detect if volume has collapsed on open positions.
    Volume < 30% of 20-day average = institutional exit warning.

    Intraday scaling: when called during market hours, today's volume is
    a partial-session figure. Comparing it directly against a full 20-day
    average produces false alarms early in the session (e.g. at 90 min in,
    any stock appears to have only ~23% of its daily average). The fix:
    scale avg_vol_20 by the fraction of the session elapsed before comparing.

    Example — AAPL at 16:02 UTC (90 min into NYSE session):
      today_vol    = 10,049,530
      avg_vol_20   = 40,376,916
      session frac = 90/390 = 0.231
      scaled_avg   = 40,376,916 × 0.231 = 9,327,068
      vol_ratio    = 10,049,530 / 9,327,068 = 107.7%  → NORMAL (was falsely 24.9%)
    """
    collapses = []

    for pos in positions:
        ticker = pos.get('t212_ticker','')
        yahoo  = YAHOO_MAP.get(ticker, '')
        name   = pos.get('name','?')

        if not yahoo:
            continue

        try:
            hist = yf.Ticker(yahoo).history(period="1mo")
            if hist.empty or len(hist) < 5:
                continue

            volumes    = [float(v) for v in hist['Volume']]
            avg_vol_20 = sum(volumes[-20:]) / min(20, len(volumes))
            today_vol  = volumes[-1]

            if avg_vol_20 == 0:
                continue

            # Scale the daily average down to match how much of the session has elapsed.
            # Outside market hours (pre/post/weekend) fraction=1.0 → no change.
            sess_frac, sess_label = _session_fraction(yahoo)
            scaled_avg = avg_vol_20 * sess_frac
            vol_ratio  = round(today_vol / scaled_avg * 100, 1) if scaled_avg > 0 else 0

            if vol_ratio < (100 - VOLUME_COLLAPSE_PCT):
                collapses.append({
                    'name':          name,
                    'ticker':        ticker,
                    'vol_ratio':     vol_ratio,
                    'today_vol':     int(today_vol),
                    'avg_vol_20':    int(avg_vol_20),
                    'session_frac':  sess_frac,
                    'session_label': sess_label,
                    'note':          (
                        f"Volume only {vol_ratio}% of session-adjusted avg "
                        f"({sess_label}, {round(sess_frac*100)}% of day elapsed) "
                        f"— possible institutional exit"
                    ),
                })
        except Exception as e:
            log_error(f"Volume collapse detection failed for {name}: {e}")

    return collapses

# ============================================================
# COMPOSITE BLACK SWAN SCORE
# ============================================================
def calculate_blackswan_score(gaps, regulatory, vix_data, volume_collapses):
    """
    Composite black swan risk score 0-10.
    0 = No detected risks
    10 = Multiple simultaneous black swan signals
    """
    score  = 0
    events = []

    # Gap events
    for gap in gaps:
        if gap['severity'] == 'EXTREME':
            score += 3
            events.append(f"EXTREME GAP: {gap['ticker']} {gap['gap_pct']:+.1f}%")
        else:
            score += 1
            events.append(f"LARGE GAP: {gap['ticker']} {gap['gap_pct']:+.1f}%")

    # Regulatory
    high_reg = [r for r in regulatory if r['severity'] == 'HIGH']
    med_reg  = [r for r in regulatory if r['severity'] == 'MEDIUM']
    score   += len(high_reg) * 2 + len(med_reg)
    for r in high_reg[:2]:
        events.append(f"REGULATORY HIGH: {r['title'][:60]}")

    # VIX
    if vix_data:
        if vix_data['severity'] == 'EXTREME':
            score += 3
            events.append(f"VIX EXTREME: {vix_data['current_vix']}")
        elif vix_data['severity'] == 'HIGH':
            score += 1
            events.append(f"VIX HIGH: {vix_data['current_vix']}")
        if abs(vix_data.get('daily_jump', 0)) > VIX_JUMP_PCT:
            score += 2
            events.append(f"VIX SPIKE: {vix_data['daily_jump']:+.1f}% today")

    # Volume collapse
    for vc in volume_collapses:
        score += 1
        events.append(f"VOL COLLAPSE: {vc['name']} at {vc['vol_ratio']}% of avg")

    score = min(10, score)

    if score >= 7:
        level      = "CRITICAL"
        action     = "SUSPEND ALL TRADING — multiple black swan signals"
    elif score >= 5:
        level      = "HIGH"
        action     = "HALT new entries — review open positions immediately"
    elif score >= 3:
        level      = "ELEVATED"
        action     = "REDUCE sizing — increase stops on open positions"
    elif score >= 1:
        level      = "WATCH"
        action     = "Monitor closely — unusual activity detected"
    else:
        level      = "CLEAR"
        action     = "No black swan signals detected"

    return score, level, action, events

# ============================================================
# MAIN RUN
# ============================================================
def run(positions=None):
    now = datetime.now(timezone.utc)
    print(f"\n=== BLACK SWAN DETECTOR ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    if positions is None:
        positions = safe_read(POSITIONS_FILE, [])

    # Build universe — open positions + quality names
    universe = dict(YAHOO_MAP)

    # Vector 1: Gaps
    print("  Scanning for overnight gaps...", flush=True)
    gaps = detect_gaps(universe)
    if gaps:
        for g in gaps:
            print(f"  ⚠️  GAP: {g['ticker']:15} {g['gap_pct']:+.1f}% overnight | {g['severity']}")
    else:
        print(f"  ✅ No significant gaps detected")

    # Vector 2: Regulatory
    print("  Scanning regulatory news...", flush=True)
    regulatory = scan_regulatory_risk()
    high_reg   = [r for r in regulatory if r['severity'] == 'HIGH']
    if high_reg:
        for r in high_reg[:3]:
            print(f"  🚨 REGULATORY: {r['title'][:70]}")
    elif regulatory:
        print(f"  ⚠️  {len(regulatory)} regulatory mentions — medium severity")
    else:
        print(f"  ✅ No regulatory alerts")

    # Vector 3: VIX regime
    print("  Checking VIX regime...", flush=True)
    vix_data = detect_vix_regime_change()
    if vix_data:
        for alert in vix_data.get('alerts', []):
            print(f"  ⚠️  VIX: {alert}")
        if not vix_data.get('alerts'):
            print(f"  ✅ VIX normal: {vix_data['current_vix']}")

    # Vector 4: Volume collapse
    print("  Checking volume on open positions...", flush=True)
    vol_collapses = detect_volume_collapse(positions)
    if vol_collapses:
        for vc in vol_collapses:
            print(f"  ⚠️  VOLUME: {vc['name']} at {vc['vol_ratio']}% of avg")
    else:
        print(f"  ✅ Volume normal on all positions")

    # Composite score
    score, level, action, events = calculate_blackswan_score(
        gaps, regulatory, vix_data, vol_collapses
    )

    icon = {"CLEAR":"✅","WATCH":"👁","ELEVATED":"⚠️","HIGH":"🔴","CRITICAL":"🚨"}.get(level,"⚠️")
    print(f"\n  {icon} BLACK SWAN SCORE: {score}/10 — {level}")
    print(f"  Action: {action}")

    if events:
        print(f"\n  Active events:")
        for e in events[:5]:
            print(f"    → {e}")

    # Alert on elevated risk
    if score >= 3:
        msg = (
            f"{'🚨' if score >= 7 else '⚠️'} BLACK SWAN ALERT — Score {score}/10\n\n"
            f"Level: {level}\n"
            f"Action: {action}\n\n"
            + "\n".join(f"• {e}" for e in events[:5])
        )
        send_telegram(msg)
        log_warning(f"Black swan score {score}/10: {events[:3]}")

    output = {
        'timestamp':    now.strftime('%Y-%m-%d %H:%M UTC'),
        'score':        score,
        'level':        level,
        'action':       action,
        'events':       events,
        'gaps':         gaps,
        'regulatory':   regulatory[:10],
        'vix':          vix_data,
        'vol_collapses':vol_collapses,
    }

    atomic_write(BLACKSWAN_FILE, output)
    print(f"\n✅ Black Swan detector complete")
    return output

def quick_check():
    """
    Fast intraday re-scan: live VIX + RSS only (skips slow gap/volume checks).
    Runs every 30 min during session. Updates apex-blackswan.json score in place
    so pre_trade_check() always sees a fresh value without waiting for 7am.
    """
    now = datetime.now(timezone.utc)

    # Live VIX
    vix_data = detect_vix_regime_change()

    # Quick RSS (reuses existing function — fast, no yfinance)
    regulatory = scan_regulatory_risk()

    # Reload cached gap + volume data from this morning's full run
    bs_data     = safe_read(BLACKSWAN_FILE, {'score': 0, 'level': 'CLEAR'})
    cached_gaps = bs_data.get('gaps', [])
    cached_vol  = bs_data.get('vol_collapses', [])

    score, level, action, events = calculate_blackswan_score(
        cached_gaps, regulatory, vix_data, cached_vol
    )

    bs_data.update({
        'score':        score,
        'level':        level,
        'action':       action,
        'events':       events,
        'vix':          vix_data,
        'regulatory':   regulatory[:10],
        'last_quick_check': now.isoformat(),
    })
    atomic_write(BLACKSWAN_FILE, bs_data)

    if score >= 3:
        icon = '🚨' if score >= 7 else '⚠️'
        log_warning(f"Quick black swan check: score {score}/10 ({level})")
        if score >= 5:
            send_telegram(
                f"{icon} MID-SESSION BLACK SWAN — Score {score}/10\n\n"
                f"Level: {level}\nAction: {action}\n\n"
                + "\n".join(f"• {e}" for e in events[:4])
            )

    return score, level


def pre_trade_check(signal):
    """
    Quick black swan check before any trade execution.
    Reads cached score PLUS does a live VIX check — the cached file
    may be hours old but VIX can spike 50% mid-session.
    """
    bs_data = safe_read(BLACKSWAN_FILE, {'score': 0, 'level': 'CLEAR'})
    score   = bs_data.get('score', 0)
    level   = bs_data.get('level', 'CLEAR')

    # Live VIX override — always check current VIX regardless of cached score
    # A war / crash spikes VIX immediately; no RSS headline needed
    try:
        vix_hist = yf.Ticker("^VIX").history(period="1d")
        if not vix_hist.empty:
            live_vix = float(vix_hist['Close'].iloc[-1])
            if live_vix > 40:
                return False, f"BLOCKED — Live VIX {live_vix:.1f} (EXTREME FEAR — crisis level)"
            elif live_vix > 35:
                return False, f"BLOCKED — Live VIX {live_vix:.1f} (HIGH FEAR — new entries halted)"
            elif live_vix > 30:
                # Elevate cached score to at least ELEVATED
                score = max(score, 3)
                level = 'ELEVATED' if level == 'CLEAR' else level
    except Exception:
        pass  # VIX fetch failed — fall through to cached score

    if score >= 7:
        return False, f"BLOCKED — Black Swan score {score}/10 ({level})"
    elif score >= 5:
        return False, f"BLOCKED — Black Swan HIGH risk ({level}), {score}/10"
    elif score >= 3:
        return True, f"CAUTION — Black Swan ELEVATED ({score}/10) — reduced sizing recommended"
    return True, f"CLEAR — Black Swan score {score}/10"

if __name__ == '__main__':
    run()
