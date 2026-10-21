"""Ingest news, score it, store it, and summarise the current market mood."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from .. import db
from ..config import IST
from . import feeds, sentiment

IMPACT_W = {"high": 3.0, "medium": 1.5, "low": 0.5}
SCOPE_W = {"market": 1.0, "sector": 0.6, "stock": 0.4}
HALF_LIFE_MIN = 45  # a headline's weight halves every 45 minutes


def ingest(nifty_price: float | None = None) -> list[dict]:
    """Fetch feeds, store unseen headlines with scores. Returns the new rows."""
    items = feeds.fetch_all()
    known = {r["id"] for r in db.query("SELECT id FROM news WHERE seen_ts > ?",
                                       ((datetime.now(IST) - timedelta(days=3)).isoformat(),))}
    fresh, seen_ids = [], set()
    for it in items:
        if it["id"] in known or it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        fresh.append(it)
    if not fresh:
        return []
    scores = []
    for i in range(0, len(fresh), 40):  # Claude batch size
        scores += sentiment.score_batch([f["title"] for f in fresh[i:i + 40]])
    seen = db.now_ist()
    rows = []
    for it, sc in zip(fresh, scores):
        row = {**it, **sc, "seen_ts": seen, "nifty_at_seen": nifty_price}
        db.execute(
            "INSERT OR IGNORE INTO news (id, ts, seen_ts, source, title, link, sentiment, impact, scope, "
            "category, symbols, analyzer, nifty_at_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["ts"], seen, row["source"], row["title"], row["link"], row["sentiment"],
             row["impact"], row["scope"], row["category"], json.dumps(row["symbols"]), row["analyzer"],
             nifty_price))
        rows.append(row)
    return rows


def recent(limit: int = 100) -> list[dict]:
    rows = db.query("SELECT * FROM news ORDER BY ts DESC LIMIT ?", (limit,))
    for r in rows:
        r["symbols"] = json.loads(r["symbols"] or "[]")
    return rows


def mood(window_minutes: int = 180, pattern: str | None = None) -> dict:
    """Time-decayed, impact-weighted average sentiment for the market and per stock.
    pattern: only headlines matching this regex count (e.g. crude-oil news for MCX crude)."""
    now = datetime.now(IST)
    since = (now - timedelta(minutes=window_minutes)).isoformat()
    rows = db.query("SELECT ts, title, sentiment, impact, scope, symbols FROM news WHERE ts > ?", (since,))
    if pattern:
        import re
        rx = re.compile(pattern, re.I)
        rows = [r for r in rows if rx.search(r["title"] or "")]
    num = den = 0.0
    per: dict[str, list[float]] = {}
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    for r in rows:
        age = max(0.0, (now - datetime.fromisoformat(r["ts"])).total_seconds() / 60)
        w = IMPACT_W.get(r["impact"], 1) * SCOPE_W.get(r["scope"], 0.5) * math.pow(0.5, age / HALF_LIFE_MIN)
        num += w * r["sentiment"]
        den += w
        s = r["sentiment"]
        counts["positive" if s > 0.15 else "negative" if s < -0.15 else "neutral"] += 1
        for sym in json.loads(r["symbols"] or "[]"):
            per.setdefault(sym, [0.0, 0.0])
            per[sym][0] += w * s
            per[sym][1] += w
    score = num / den if den else 0.0
    label = ("very bearish" if score < -0.5 else "bearish" if score < -0.15 else
             "very bullish" if score > 0.5 else "bullish" if score > 0.15 else "neutral")
    return {
        "score": round(score, 3), "label": label, "headlines": len(rows), "counts": counts,
        "window_minutes": window_minutes,
        "stocks": {k: round(v[0] / v[1], 3) for k, v in per.items() if v[1] > 0},
    }
