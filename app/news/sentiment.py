"""Headline sentiment.

Two analysers:
- VADER with a finance word list: free, local, instant. Always available.
- Claude (if ANTHROPIC_API_KEY is set): reads a batch of headlines and returns
  sentiment, market impact, scope, category and affected NSE symbols.
"""
from __future__ import annotations

import json
import logging
import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from ..config import settings
from ..universe import STOCKS

log = logging.getLogger(__name__)

FIN_LEXICON = {
    "surge": 2.5, "surges": 2.5, "soar": 2.8, "soars": 2.8, "rally": 2.3, "rallies": 2.3, "jumps": 2.0,
    "gains": 1.5, "record high": 2.5, "beats": 2.0, "upgrade": 2.0, "upgraded": 2.0, "outperform": 1.8,
    "bullish": 2.5, "rebound": 1.8, "rebounds": 1.8, "inflows": 1.5, "rate cut": 1.5, "buyback": 1.5,
    "profit rises": 2.0, "order win": 1.8, "dividend": 1.0, "approval": 1.2,
    "plunge": -3.0, "plunges": -3.0, "crash": -3.2, "crashes": -3.2, "tumble": -2.6, "tumbles": -2.6,
    "slump": -2.5, "slumps": -2.5, "sinks": -2.3, "falls": -1.5, "drops": -1.5, "slides": -1.8,
    "bearish": -2.5, "downgrade": -2.2, "downgraded": -2.2, "misses": -2.0, "selloff": -2.6,
    "sell-off": -2.6, "outflows": -1.6, "rate hike": -1.5, "war": -2.5, "sanctions": -1.8, "tariff": -1.5,
    "tariffs": -1.5, "recession": -2.6, "default": -2.5, "output cut": 1.5, "supply cut": 1.5, "inventories rise": -1.5,
    "inventory build": -1.5, "inventory draw": 1.5, "glut": -2.2, "oversupply": -2.2, "fraud": -3.0, "probe": -1.8, "raid": -2.0,
    "loss widens": -2.2, "profit falls": -2.0, "inflation": -0.8, "volatility": -0.8, "fii selling": -1.8,
}
_vader = SentimentIntensityAnalyzer()
_vader.lexicon.update({k: v for k, v in FIN_LEXICON.items() if " " not in k})
_PHRASES = {k: v for k, v in FIN_LEXICON.items() if " " in k}

MARKET_WORDS = re.compile(r"\b(nifty|sensex|market|markets|rbi|fed|fii|dii|gdp|inflation|budget|rupee|"
                          r"crude|oil|gas|gold|silver|copper|metals?|war|tariff|repo|sebi|election|global)\b", re.I)
HIGH_IMPACT = re.compile(r"\b(rbi|repo|rate (cut|hike)|fed|budget|war|crash|plunge|record|election|"
                         r"recession|sanction|tariff|ban|default)\b", re.I)
_SYMBOL_RE = {s: re.compile(rf"\b{re.escape(s)}\b", re.I) for s in STOCKS}
_ALIASES = {"RELIANCE": "reliance", "HDFCBANK": "hdfc bank", "ICICIBANK": "icici bank", "SBIN": "sbi",
            "BHARTIARTL": "airtel", "KOTAKBANK": "kotak", "AXISBANK": "axis bank", "LT": "larsen",
            "BAJFINANCE": "bajaj finance", "MARUTI": "maruti", "INFY": "infosys", "HINDUNILVR": "hul",
            "SUNPHARMA": "sun pharma", "ULTRACEMCO": "ultratech", "M&M": "mahindra",
            "TATASTEEL": "tata steel", "ADANIPORTS": "adani ports", "COALINDIA": "coal india"}


def vader_score(title: str) -> dict:
    s = _vader.polarity_scores(title)["compound"]
    low = title.lower()
    for phrase, w in _PHRASES.items():
        if phrase in low:
            s = max(-1.0, min(1.0, s + w / 6))
    symbols = [sym for sym, rx in _SYMBOL_RE.items() if rx.search(title)]
    symbols += [sym for sym, a in _ALIASES.items() if a in low and sym not in symbols]
    market = bool(MARKET_WORDS.search(title))
    return {
        "sentiment": round(s, 3),
        "impact": "high" if HIGH_IMPACT.search(title) else ("medium" if market or symbols else "low"),
        "scope": "market" if market and not symbols else ("stock" if symbols else "sector"),
        "category": "general",
        "symbols": symbols,
        "analyzer": "vader",
    }


# ------------------------------------------------------------------- Claude

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "sentiment": {"type": "number"},
                    "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                    "scope": {"type": "string", "enum": ["market", "sector", "stock"]},
                    "category": {"type": "string", "enum": ["monetary_policy", "fiscal_policy", "earnings",
                                                             "macro_data", "geopolitics", "global_markets",
                                                             "flows", "regulation", "corporate_action",
                                                             "commodities", "other"]},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["i", "sentiment", "impact", "scope", "category", "symbols"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are an equity analyst for the Indian stock market (NSE). For each headline, judge the likely "
    "short-term effect on Indian equity prices, not the tone of the writing.\n"
    "- sentiment: -1 (strongly bearish) to +1 (strongly bullish) for the affected stocks or the market.\n"
    "- impact: 'high' only for news likely to move Nifty or a stock by more than ~1% today "
    "(RBI/Fed decisions, budget, wars, big earnings surprises); 'low' for routine or stale news.\n"
    "- symbols: NSE trading symbols of directly affected companies, chosen from this list only: "
    + ", ".join(STOCKS) + ". Empty if none."
)

_client = None


def _claude():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=settings.anthropic_key)
    return _client


def claude_scores(titles: list[str]) -> list[dict] | None:
    """Score a batch of headlines with Claude. Returns None on any failure so
    the caller falls back to VADER."""
    if not settings.anthropic_key or not titles:
        return None
    import anthropic

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    try:
        resp = _claude().beta.messages.create(
            model=settings.claude_model,
            max_tokens=16000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Headlines:\n{numbered}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError:
        log.warning("Claude rate limited; using VADER for this batch")
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s: %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Claude unreachable; using VADER for this batch")
        return None
    if resp.stop_reason != "end_turn":
        log.warning("Claude stopped with %s; using VADER", resp.stop_reason)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)["items"]
    except (ValueError, KeyError):
        return None
    out: list[dict | None] = [None] * len(titles)
    for it in parsed:
        i = it.get("i")
        if isinstance(i, int) and 0 <= i < len(titles):
            it["sentiment"] = max(-1.0, min(1.0, float(it["sentiment"])))
            it["symbols"] = [s for s in it["symbols"] if s in STOCKS]
            it["analyzer"] = "claude"
            it.pop("i", None)
            out[i] = it
    return out


def score_batch(titles: list[str]) -> list[dict]:
    smart = claude_scores(titles) or [None] * len(titles)
    return [s or vader_score(t) for s, t in zip(smart, titles)]
