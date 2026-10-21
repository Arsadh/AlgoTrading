"""Live market news from public RSS feeds (Indian + global)."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from ..config import IST

FEEDS = {
    "Economic Times": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "LiveMint": "https://www.livemint.com/rss/markets",
    "Business Standard": "https://www.business-standard.com/rss/markets-106.rss",
    "Google News (Nifty/Sensex)": "https://news.google.com/rss/search?q=nifty+OR+sensex+OR+RBI&hl=en-IN&gl=IN&ceid=IN:en",
    "CNBC World": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "OilPrice.com": "https://oilprice.com/rss/main",
    "Google News (crude oil)": "https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+MCX+crude&hl=en-IN&gl=IN&ceid=IN:en",
    "Google News (natural gas)": "https://news.google.com/rss/search?q=%22natural+gas%22+OR+LNG+OR+%22Henry+Hub%22&hl=en-IN&gl=IN&ceid=IN:en",
    "Google News (gold/silver)": "https://news.google.com/rss/search?q=gold+price+OR+silver+price+OR+bullion+OR+MCX+gold&hl=en-IN&gl=IN&ceid=IN:en",
    "Google News (base metals)": "https://news.google.com/rss/search?q=copper+price+OR+zinc+price+OR+LME+metals&hl=en-IN&gl=IN&ceid=IN:en",
}
UA = {"User-Agent": "Mozilla/5.0 (algtrd news reader)"}
log = logging.getLogger(__name__)


def _published(entry) -> str:
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                return parsedate_to_datetime(entry[key]).astimezone(IST).isoformat(timespec="seconds")
            except Exception:
                pass
    return datetime.now(IST).isoformat(timespec="seconds")


def fetch_all(timeout: float = 10.0) -> list[dict]:
    items = []
    with httpx.Client(headers=UA, timeout=timeout, follow_redirects=True) as client:
        for source, url in FEEDS.items():
            try:
                resp = client.get(url)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as e:
                log.warning("feed %s failed: %s", source, e)
                continue
            for e in parsed.entries[:40]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                link = e.get("link", "")
                items.append({
                    "id": hashlib.sha1(title.lower().encode()).hexdigest()[:16],  # same story on 2 feeds -> 1 row
                    "source": source, "title": title, "link": link, "ts": _published(e),
                })
    return items
