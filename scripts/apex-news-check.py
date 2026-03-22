#!/usr/bin/env python3
import yfinance as yf
import json
from datetime import datetime, timezone, timedelta

# Top instruments to check for news — focus on individual stocks not ETFs
NEWS_WATCHLIST = {
    "XOM":   "XOM",
    "CVX":   "CVX",
    "SHEL":  "SHEL.L",
    "BP":    "BP.L",
    "AAPL":  "AAPL",
    "MSFT":  "MSFT",
    "NVDA":  "NVDA",
    "GOOGL": "GOOGL",
    "AMZN":  "AMZN",
    "META":  "META",
    "TSLA":  "TSLA",
    "JPM":   "JPM",
    "GS":    "GS",
    "BAC":   "BAC",
    "JNJ":   "JNJ",
    "UNH":   "UNH",
    "AZN":   "AZN.L",
    "GSK":   "GSK.L",
    "BA":    "BA.L",
    "ASML":  "ASML.AS",
}

today     = datetime.now(timezone.utc)
yesterday = today - timedelta(hours=16)

flagged  = []
clean    = []
no_news  = []

for name, ticker in NEWS_WATCHLIST.items():
    try:
        t = yf.Ticker(ticker)
        news = t.news

        if not news:
            no_news.append(name)
            continue

        recent = []
        for item in news[:5]:
            # Check publish time
            pub_time = item.get('providerPublishTime', 0)
            if pub_time:
                pub_dt = datetime.fromtimestamp(pub_time, tz=timezone.utc)
                if pub_dt > yesterday:
                    title = item.get('title', '')
                    # Flag keywords that suggest significant news
                    keywords = [
                        'earnings', 'results', 'profit', 'loss', 'revenue',
                        'downgrade', 'upgrade', 'cut', 'raised', 'beats',
                        'misses', 'warning', 'investigation', 'lawsuit',
                        'acquisition', 'merger', 'buyout', 'dividend',
                        'recall', 'regulation', 'fine', 'ceo', 'resign',
                        'guidance', 'outlook', 'layoff', 'restructure'
                    ]
                    is_significant = any(k in title.lower() for k in keywords)
                    recent.append({
                        "title": title[:100],
                        "significant": is_significant,
                        "age_hours": round((today - pub_dt).seconds / 3600, 1)
                    })

        if recent:
            significant = [r for r in recent if r['significant']]
            if significant:
                flagged.append({
                    "name": name,
                    "news": significant[0]['title'],
                    "age_hours": significant[0]['age_hours']
                })
            else:
                clean.append(name)
        else:
            clean.append(name)

    except Exception as e:
        no_news.append(name)

# Build output
now_str = datetime.now(timezone.utc).strftime('%a %d %b %Y %H:%M UTC')
lines   = [f"📰 APEX PRE-MARKET NEWS — {now_str}\n"]

if flagged:
    lines.append("🚨 SIGNIFICANT OVERNIGHT NEWS:")
    for f in flagged:
        lines.append(f"  ⚠️ {f['name']} ({f['age_hours']}h ago): {f['news']}")
    lines.append("\nConsider avoiding these instruments today until news is assessed.")
else:
    lines.append("✅ No significant overnight news on watchlist instruments.")

lines.append(f"\n✅ Clean: {len(clean)} | 🚨 Flagged: {len(flagged)} | ℹ️ No data: {len(no_news)}")

# Save flags for morning scan to use
news_flags = [f['name'] for f in flagged]
with open('/home/ubuntu/.picoclaw/logs/apex-news-flags.json', 'w') as f:
    json.dump(news_flags, f)

print("\n".join(lines))
