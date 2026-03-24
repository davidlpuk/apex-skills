#!/usr/bin/env python3
import urllib.request
import json
import re
from datetime import datetime, timezone, timedelta
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def send_telegram(m): print(f'TELEGRAM: {m[:80]}')

VELOCITY_FILE = '/home/ubuntu/.picoclaw/logs/apex-geo-velocity.json'


def _update_velocity(geo_count, energy_count):
    """
    Track hourly geo headline counts and detect 2× acceleration.
    Keeps rolling 12-hour window. Returns (is_accelerating, velocity_ratio).
    """
    now   = datetime.now(timezone.utc)
    data  = safe_read(VELOCITY_FILE, {'hours': []})
    hours = data.get('hours', [])

    # Append current hour
    hours.append({
        'ts':           now.isoformat(),
        'geo_count':    geo_count,
        'energy_count': energy_count,
        'total':        geo_count + energy_count,
    })

    # Keep only last 12 hours
    cutoff = now - timedelta(hours=12)
    hours  = [h for h in hours if datetime.fromisoformat(h['ts']) > cutoff]
    data['hours'] = hours

    # Velocity: compare last 1h to previous 1h average
    is_accelerating = False
    velocity_ratio  = 1.0

    if len(hours) >= 2:
        recent_total = hours[-1]['total']
        prev_totals  = [h['total'] for h in hours[-4:-1]]  # up to 3 prior hours
        if prev_totals:
            avg_prev = sum(prev_totals) / len(prev_totals)
            if avg_prev > 0:
                velocity_ratio = round(recent_total / avg_prev, 2)
                is_accelerating = velocity_ratio >= 2.0 and recent_total >= 3

    data['velocity_ratio']   = velocity_ratio
    data['is_accelerating']  = is_accelerating
    data['last_updated']     = now.isoformat()
    atomic_write(VELOCITY_FILE, data)

    return is_accelerating, velocity_ratio


RSS_FEEDS = {
    "BBC Business":     "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC World":        "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters Business": "https://feeds.reuters.com/reuters/businessNews",
}

GEO_KEYWORDS = [
    "war", "strike", "attack", "missile", "airstrike", "invasion",
    "troops", "military", "conflict", "explosion", "bomb", "sanctions",
    "embargo", "tariff", "trade war", "blockade", "iran", "russia",
    "china", "north korea", "ukraine", "middle east", "opec", "nato",
    "oil field", "pipeline", "refinery", "energy supply", "crude",
    "brent", "opec cut", "oil production", "bank run", "credit crisis",
    "default", "recession", "fed emergency", "oman", "gulf", "strait",
]

ENERGY_KEYWORDS = [
    "oil", "gas", "energy", "crude", "brent", "opec", "pipeline",
    "refinery", "iran", "saudi", "oman", "middle east", "gulf", "strait"
]

def clean_cdata(text):
    text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'")
    return text.strip()

def parse_rss(url):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; ApexBot/1.0)'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read().decode('utf-8', errors='ignore')

        items = []
        item_blocks = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)

        for block in item_blocks[:15]:
            title = re.search(r'<title>(.*?)</title>', block, re.DOTALL)
            if title:
                clean = clean_cdata(title.group(1))
                if clean and len(clean) > 5:
                    items.append(clean)

        return items
    except Exception as e:
        return []

def scan_geo_news():
    now     = datetime.now(timezone.utc)
    flagged = []
    energy_flags = []
    all_headlines = []

    for source, url in RSS_FEEDS.items():
        headlines = parse_rss(url)
        for h in headlines:
            all_headlines.append({"source": source, "title": h})
            h_lower = h.lower()
            is_geo    = any(k in h_lower for k in GEO_KEYWORDS)
            is_energy = any(k in h_lower for k in ENERGY_KEYWORDS)

            if is_geo and is_energy:
                energy_flags.append({"source": source, "title": h[:120]})
            elif is_geo:
                flagged.append({"source": source, "title": h[:120]})

    output = {
        "timestamp":     now.strftime("%Y-%m-%d %H:%M UTC"),
        "geo_flags":     flagged,
        "energy_flags":  energy_flags,
        "total_scanned": len(all_headlines),
        "overall":       "ALERT" if energy_flags else ("WARN" if flagged else "CLEAR")
    }

    atomic_write('/home/ubuntu/.picoclaw/logs/apex-geo-news.json', output)

    print(f"\n🌍 GEO-POLITICAL NEWS SCAN — {now.strftime('%H:%M UTC')}")
    print(f"Scanned {len(all_headlines)} headlines from {len(RSS_FEEDS)} sources\n")

    if energy_flags:
        print("🚨 ENERGY/GEO ALERTS — may affect energy stocks:")
        for f in energy_flags:
            print(f"  [{f['source']}] {f['title']}")
    elif flagged:
        print("⚠️ GEOPOLITICAL FLAGS — monitor for market impact:")
        for f in flagged[:5]:
            print(f"  [{f['source']}] {f['title']}")
    else:
        print("✅ No significant geopolitical news detected")

    if all_headlines:
        print(f"\n📰 Sample headlines scanned:")
        for h in all_headlines[:5]:
            print(f"  [{h['source']}] {h['title'][:80]}")

    print(f"\nOverall: {output['overall']}")

    # Sentiment velocity — detect 2× acceleration in geo headline volume
    try:
        is_accelerating, vel_ratio = _update_velocity(len(flagged), len(energy_flags))
        if is_accelerating:
            print(f"⚡ VELOCITY ALERT — headline rate {vel_ratio:.1f}× normal")
            send_telegram(
                f"⚡ GEO NEWS VELOCITY ALERT\n\n"
                f"Geo headline rate is {vel_ratio:.1f}× above average in last hour.\n"
                f"This early signal precedes market moves — monitor closely.\n\n"
                f"Geo flags: {len(flagged)} | Energy flags: {len(energy_flags)}\n"
                f"Overall: {output['overall']}"
            )
    except Exception:
        pass

    # Weekend auto-pause: if ALERT fires on Saturday or Sunday, write pause flag
    # so no trades execute at Monday open before a human reviews
    if output['overall'] == 'ALERT' and now.weekday() in (5, 6):  # 5=Sat, 6=Sun
        pause_flag = '/home/ubuntu/.picoclaw/logs/apex-paused.flag'
        try:
            with open(pause_flag, 'w') as f:
                f.write(f"GEO_ALERT_WEEKEND_{now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
            try:
                from apex_utils import send_telegram
                flag_headlines = (energy_flags or flagged)[:3]
                headline_text = '\n'.join(f"  • {h['title'][:80]}" for h in flag_headlines)
                send_telegram(
                    f"🚨 WEEKEND GEO ALERT — Trading paused for Monday open\n\n"
                    f"Geopolitical risk detected:\n{headline_text}\n\n"
                    f"System will NOT trade Monday until you send RESUME.\n"
                    f"Review geo news before enabling."
                )
            except Exception:
                pass
            print(f"⚠️ Weekend geo ALERT — pause flag written. Send RESUME to enable Monday trading.")
        except Exception as e:
            print(f"WARNING: Could not write pause flag: {e}")

    return output

if __name__ == '__main__':
    scan_geo_news()
