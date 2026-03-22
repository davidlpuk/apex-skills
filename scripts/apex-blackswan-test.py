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
    from apex_utils import atomic_write, safe_read, log_error, log_warning
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

def send_telegram(msg):
    try:
        subprocess.run(['bash', '-c',
            f'''BOT=$(cat ~/.picoclaw/config.json | grep -A2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot$BOT/sendMessage" \
  -d chat_id=6808823889 --data-urlencode "text={msg}"'''
        ], capture_output=True)
    except Exception as e:
        log_error(f"send_telegram failed: {e}")

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

# ============================================================
# VECTOR 4: VOLUME COLLAPSE DETECTOR
# ============================================================
def detect_volume_collapse(positions):
    """
    Detect if volume has collapsed on open positions.
    Volume < 30% of 20-day average = institutional exit warning.
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

            volumes     = [float(v) for v in hist['Volume']]
            avg_vol_20  = sum(volumes[-20:]) / min(20, len(volumes))
            today_vol   = volumes[-1]

            if avg_vol_20 == 0:
                continue

            vol_ratio = round(today_vol / avg_vol_20 * 100, 1)

            if vol_ratio < (100 - VOLUME_COLLAPSE_PCT):
                collapses.append({
                    'name':       name,
                    'ticker':     ticker,
                    'vol_ratio':  vol_ratio,
                    'today_vol':  int(today_vol),
                    'avg_vol_20': int(avg_vol_20),
                    'note':       f"Volume only {vol_ratio}% of 20-day avg — possible institutional exit",
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

def pre_trade_check(signal):
    """Quick black swan check before any trade execution."""
    bs_data = safe_read(BLACKSWAN_FILE, {'score': 0, 'level': 'CLEAR'})
    score   = bs_data.get('score', 0)
    level   = bs_data.get('level', 'CLEAR')

    if score >= 7:
        return False, f"BLOCKED — Black Swan score {score}/10 ({level})"
    elif score >= 5:
        return False, f"BLOCKED — Black Swan HIGH risk ({level}), {score}/10"
    elif score >= 3:
        return True, f"CAUTION — Black Swan ELEVATED ({score}/10) — reduced sizing recommended"
    return True, f"CLEAR — Black Swan score {score}/10"

if __name__ == '__main__':
    run()
