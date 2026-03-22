#!/usr/bin/env python3
"""
News Sentiment Scoring
Replaces binary keyword blocking with -1 to +1 sentiment scores.
Uses VADER (Valence Aware Dictionary and sEntiment Reasoner) — free, local, no API.
"""
import json
import urllib.request
import re
from datetime import datetime, timezone
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


SENTIMENT_FILE = '/home/ubuntu/.picoclaw/logs/apex-sentiment.json'

RSS_FEEDS = {
    "BBC Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters":      "https://feeds.reuters.com/reuters/businessNews",
}

# Instrument-specific keywords for targeted sentiment
INSTRUMENT_KEYWORDS = {
    "AAPL":  ["apple","iphone","tim cook","app store","ios","mac"],
    "MSFT":  ["microsoft","azure","satya nadella","windows","teams","copilot"],
    "NVDA":  ["nvidia","jensen huang","gpu","ai chips","cuda","blackwell"],
    "GOOGL": ["google","alphabet","sundar pichai","search","youtube","gemini"],
    "AMZN":  ["amazon","aws","andy jassy","prime","alexa"],
    "META":  ["meta","facebook","mark zuckerberg","instagram","whatsapp","threads"],
    "XOM":   ["exxon","oil","crude","energy","opec","petroleum"],
    "CVX":   ["chevron","oil","crude","energy","opec"],
    "SHEL":  ["shell","oil","crude","energy","lng"],
    "BP":    ["bp","oil","crude","energy","british petroleum"],
    "V":     ["visa","payment","card network","mastercard"],
    "JPM":   ["jpmorgan","jamie dimon","banking","federal reserve","interest rates"],
    "HSBA":  ["hsbc","hong kong","banking","china"],
    "AZN":   ["astrazeneca","pharma","drug","fda","clinical trial"],
    "GSK":   ["gsk","glaxo","pharma","drug","vaccine"],
    "ULVR":  ["unilever","consumer goods","dove","lipton"],
    "LGEN":  ["legal general","insurance","pension","lgim"],
    "VUAG":  ["s&p 500","sp500","us market","wall street","federal reserve"],
    "VWRP":  ["global market","world stocks","msci world"],
}

# Market-wide negative events that should reduce all position sizes
MARKET_CRISIS_KEYWORDS = [
    "recession", "crash", "crisis", "bank run", "systemic risk",
    "lehman", "black monday", "circuit breaker", "market halt",
    "emergency rate", "fed emergency", "contagion"
]

def clean_cdata(text):
    text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;','&').replace('&lt;','<').replace('&gt;','>')
    text = text.replace('&quot;','"').replace('&#39;',"'")
    return text.strip()

def fetch_headlines():
    headlines = []
    for source, url in RSS_FEEDS.items():
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; ApexBot/1.0)'
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                content = r.read().decode('utf-8', errors='ignore')
            items = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)
            for block in items[:15]:
                title = re.search(r'<title>(.*?)</title>', block, re.DOTALL)
                desc  = re.search(r'<description>(.*?)</description>', block, re.DOTALL)
                if title:
                    t = clean_cdata(title.group(1))
                    d = clean_cdata(desc.group(1)) if desc else ''
                    if t and len(t) > 5:
                        headlines.append({
                            'source': source,
                            'title':  t,
                            'desc':   d[:200]
                        })
        except Exception as _e:
            log_error(f"Silent failure in apex-sentiment.py: {_e}")
    return headlines

def score_headlines(headlines, analyzer):
    scored = []
    for h in headlines:
        text  = h['title'] + '. ' + h.get('desc','')
        score = analyzer.polarity_scores(text)
        scored.append({
            'source':   h['source'],
            'title':    h['title'][:120],
            'compound': round(score['compound'], 3),
            'pos':      round(score['pos'], 3),
            'neg':      round(score['neg'], 3),
            'neu':      round(score['neu'], 3),
        })
    return scored

def get_instrument_sentiment(instrument, headlines, analyzer):
    keywords = INSTRUMENT_KEYWORDS.get(instrument, [instrument.lower()])
    relevant = []

    for h in headlines:
        text_lower = (h['title'] + ' ' + h.get('desc','')).lower()
        if any(kw in text_lower for kw in keywords):
            text  = h['title'] + '. ' + h.get('desc','')
            score = analyzer.polarity_scores(text)
            relevant.append({
                'title':    h['title'][:80],
                'compound': round(score['compound'], 3),
            })

    if not relevant:
        return 0.0, []

    avg_sentiment = round(sum(r['compound'] for r in relevant) / len(relevant), 3)
    return avg_sentiment, relevant

def classify_sentiment(score):
    if score >= 0.3:   return "VERY_POSITIVE", "+2 signal boost"
    if score >= 0.1:   return "POSITIVE",      "+1 signal boost"
    if score >= -0.1:  return "NEUTRAL",       "no adjustment"
    if score >= -0.3:  return "NEGATIVE",      "-1 signal penalty"
    return "VERY_NEGATIVE", "block signal"

def run():
    now      = datetime.now(timezone.utc)
    analyzer = SentimentIntensityAnalyzer()

    print("Fetching headlines...", flush=True)
    headlines = fetch_headlines()
    print(f"  {len(headlines)} headlines fetched", flush=True)

    # Score all headlines
    all_scored = score_headlines(headlines, analyzer)

    # Overall market sentiment
    if all_scored:
        market_sentiment = round(sum(h['compound'] for h in all_scored) / len(all_scored), 3)
    else:
        market_sentiment = 0.0

    market_class, market_note = classify_sentiment(market_sentiment)

    # Check for market crisis language
    crisis_detected = False
    crisis_headlines = []
    for h in headlines:
        text_lower = h['title'].lower()
        if any(kw in text_lower for kw in MARKET_CRISIS_KEYWORDS):
            crisis_detected = True
            crisis_headlines.append(h['title'][:80])

    # Score each instrument
    instrument_scores = {}
    for instrument in INSTRUMENT_KEYWORDS.keys():
        score, relevant = get_instrument_sentiment(instrument, headlines, analyzer)
        if relevant:
            label, note = classify_sentiment(score)
            instrument_scores[instrument] = {
                'sentiment':  score,
                'label':      label,
                'note':       note,
                'headlines':  relevant[:2],
                'count':      len(relevant)
            }

    # Most positive and negative
    if instrument_scores:
        most_positive = max(instrument_scores.items(), key=lambda x: x[1]['sentiment'])
        most_negative = min(instrument_scores.items(), key=lambda x: x[1]['sentiment'])
    else:
        most_positive = most_negative = None

    output = {
        "timestamp":         now.strftime('%Y-%m-%d %H:%M UTC'),
        "total_headlines":   len(headlines),
        "market_sentiment":  market_sentiment,
        "market_class":      market_class,
        "market_note":       market_note,
        "crisis_detected":   crisis_detected,
        "crisis_headlines":  crisis_headlines,
        "instrument_scores": instrument_scores,
        "most_positive":     most_positive[0] if most_positive else None,
        "most_negative":     most_negative[0] if most_negative else None,
        "top_headlines":     sorted(all_scored, key=lambda x: x['compound'], reverse=True)[:5],
        "worst_headlines":   sorted(all_scored, key=lambda x: x['compound'])[:5],
    }

    atomic_write(SENTIMENT_FILE, output)

    # Print summary
    print(f"\n=== SENTIMENT ANALYSIS ===")
    print(f"  Headlines scanned: {len(headlines)}")
    print(f"  Market sentiment:  {market_sentiment} → {market_class}")
    if crisis_detected:
        print(f"  🚨 CRISIS LANGUAGE DETECTED:")
        for h in crisis_headlines[:3]:
            print(f"    → {h}")

    print(f"\n  Instrument sentiment (where relevant news found):")
    for inst, data in sorted(instrument_scores.items(), key=lambda x: x[1]['sentiment'], reverse=True):
        bar   = "█" * int(abs(data['sentiment']) * 10)
        color = "+" if data['sentiment'] > 0 else ""
        print(f"  {inst:6} {color}{data['sentiment']:+.3f} {bar} [{data['label']}] ({data['count']} headlines)")

    if most_positive:
        print(f"\n  Most positive: {most_positive[0]} ({most_positive[1]['sentiment']:+.3f})")
    if most_negative:
        print(f"  Most negative: {most_negative[0]} ({most_negative[1]['sentiment']:+.3f})")

    return output

if __name__ == '__main__':
    run()
