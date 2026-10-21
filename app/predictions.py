"""Hourly prediction log, graded later, saved as JSON for the public site in docs/ (GitHub Pages).

Every hour while an instrument's market is open, its current call is recorded:
score, bias (bullish / bearish / neutral), signal, strike, stop-loss and targets.
Each call is graded twice, so the published accuracy is measured, not claimed:
  - 1 hour later: did the price move the way the bias said?
  - at that day's close: same question, from the call to the close.
Neutral calls make no prediction and are not graded.

Files (docs/data/):
  predictions/YYYY-MM-DD.json  every call of the day + that day's live CALL/PUT signals and results
  latest.json                  the current call for every instrument
  summary.json                 accuracy so far, per instrument and per score strength
After every hourly write and at each exchange's close the files are committed and
pushed to GitHub when PUBLISH_GIT=1 (see publish.py).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import IST, ROOT
from .market.instruments import INSTRUMENTS

DOCS = ROOT / "docs"
DATA = DOCS / "data"
DAYS = DATA / "predictions"
log = logging.getLogger(__name__)
_lock = threading.Lock()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    tmp.replace(path)


def _grade(bias: str, start: float, end: float) -> dict:
    change = end - start
    want = {"bullish": 1, "bearish": -1}.get(bias)
    correct = None if not want or change == 0 else (change > 0) == (want > 0)
    return {"spot": round(end, 2), "change": round(change, 2),
            "change_pct": round(change / start * 100, 3) if start else None, "correct": correct}


def _call(a: dict, now: datetime, kind: str) -> dict:
    opt, lv = a.get("option") or {}, a.get("levels") or {}
    return {
        "id": f"{now:%Y-%m-%dT%H}:{a['key']}", "time": now.isoformat(timespec="seconds"), "kind": kind,
        "key": a["key"], "name": a["name"], "exchange": a.get("exchange"), "source": a.get("source"),
        "spot": a["spot"], "score": a["score"], "bias": a["bias"], "signal": a.get("signal") or "",
        "suggested": {"type": opt.get("type"), "strike": opt.get("strike"), "expiry": opt.get("expiry"),
                      "premium": opt.get("premium"), "entry": lv.get("entry"), "sl": lv.get("sl"),
                      "t1": lv.get("t1"), "t2": lv.get("t2")},
        "blockers": a.get("blockers") or [],
        "outcome_1h": None, "outcome_close": None,
    }


class Recorder:
    def __init__(self, on_written=None):
        self.on_written = on_written      # callback(message) after files change (git publish)
        self.closed: dict[str, str] = {}  # key -> date already graded at the close

    def tick(self, analysis: dict[str, dict]):
        """Called after every refresh: record on the first refresh of each hour, grade at the close."""
        now = datetime.now(IST)
        today = now.date().isoformat()
        wrote = []
        with _lock:
            day = _read(DAYS / f"{today}.json", {"date": today, "predictions": [], "signals": []})
            ids = {p["id"] for p in day["predictions"]}
            for key, a in analysis.items():
                inst = INSTRUMENTS[key]
                last = datetime.fromisoformat(a["last_tick"])
                fresh = now - last < timedelta(minutes=10)
                if inst.is_open(now) and fresh and f"{now:%Y-%m-%dT%H}:{key}" not in ids:
                    for p in day["predictions"]:  # grade this instrument's previous hourly call
                        if p["key"] == key and p["outcome_1h"] is None:
                            p["outcome_1h"] = _grade(p["bias"], p["spot"], a["spot"]) | {"at": now.isoformat(timespec="seconds")}
                    day["predictions"].append(_call(a, now, "hourly"))
                    wrote.append(key)
                elif (not inst.is_open(now) and now.time() > inst.hours(now.date())["close"]
                      and last.date().isoformat() == today and self.closed.get(key) != today):
                    for p in day["predictions"]:
                        if p["key"] == key and p["outcome_close"] is None:
                            p["outcome_close"] = _grade(p["bias"], p["spot"], a["spot"]) | {"at": now.isoformat(timespec="seconds")}
                    self.closed[key] = today
                    wrote.append(f"{key} close")
            if wrote:
                day["signals"] = db.query("SELECT ts, symbol, side, score, entry, sl, t1, t2, strike, opt_type, expiry, premium, "
                                          "status, exit_ts, exit_spot, points, r FROM signals WHERE substr(ts,1,10)=? ORDER BY id",
                                          (today,))
                day["updated"] = now.isoformat(timespec="seconds")
                _write(DAYS / f"{today}.json", day)
                _write(DATA / "latest.json", latest(analysis, now))
                _write(DATA / "summary.json", summary(now))
        if wrote and self.on_written:
            closes = [w for w in wrote if w.endswith("close")]
            self.on_written(f"Predictions {now:%Y-%m-%d %H:%M} IST: "
                            + (f"close graded for {', '.join(w[:-6] for w in closes)}" if closes else f"{len(wrote)} instruments"))


def latest(analysis: dict[str, dict], now: datetime) -> dict:
    keep = ("key", "name", "exchange", "open", "spot", "change_pct", "score", "bias", "signal", "source", "blockers",
            "levels", "computed_at", "last_tick")
    rows = []
    for a in analysis.values():
        r = {k: a.get(k) for k in keep}
        opt = a.get("option") or {}
        r["option"] = {k: opt.get(k) for k in ("type", "strike", "expiry", "premium", "premium_sl", "premium_t1",
                                                "premium_t2", "lot_desc", "premium_source")}
        rows.append(r)
    return {"updated": now.isoformat(timespec="seconds"), "instruments": rows}


def summary(now: datetime) -> dict:
    """Accuracy of every graded call so far (all daily files) + the live signal scorecard."""
    from .signals import tracker

    def bucket():
        return {"calls": 0, "graded": 0, "correct": 0}

    def add(b, outcome):
        b["calls"] += 1
        if outcome and outcome.get("correct") is not None:
            b["graded"] += 1
            b["correct"] += int(outcome["correct"])

    total = {"1h": bucket(), "close": bucket()}
    per: dict[str, dict] = {}
    strength = {name: {"1h": bucket(), "close": bucket()} for name in ("|score| 20-44", "|score| 45-69", "|score| 70+")}
    days = []
    for f in sorted(DAYS.glob("*.json")):
        d = _read(f, {})
        preds = [p for p in d.get("predictions", []) if p["bias"] != "neutral"]
        days.append({"date": d.get("date", f.stem), "calls": len(d.get("predictions", [])), "directional": len(preds),
                     "signals": len(d.get("signals", []))})
        for p in preds:
            s = abs(p["score"])
            sb = strength["|score| 70+" if s >= 70 else "|score| 45-69" if s >= 45 else "|score| 20-44"]
            pk = per.setdefault(p["key"], {"name": p["name"], "1h": bucket(), "close": bucket()})
            for h, o in (("1h", p.get("outcome_1h")), ("close", p.get("outcome_close"))):
                add(total[h], o)
                add(pk[h], o)
                add(sb[h], o)
    for b in [total, *per.values(), *strength.values()]:
        for h in ("1h", "close"):
            g = b[h]
            g["accuracy"] = round(g["correct"] / g["graded"], 3) if g["graded"] else None
    sigs = db.query("SELECT r FROM signals WHERE status != 'open'")
    wins = sum(1 for s in sigs if (s["r"] or 0) > 0)
    return {
        "updated": now.isoformat(timespec="seconds"),
        "note": "A bullish/bearish call is correct when the price moved that way 1 hour later (or by the close). "
                "50% is a coin flip. Neutral calls are not graded.",
        "total": total, "by_strength": strength, "by_instrument": per, "days": days[::-1],
        "signals": {"closed": len(sigs), "wins": wins, "win_rate": round(wins / len(sigs), 3) if sigs else None,
                    "avg_r": round(sum(s["r"] or 0 for s in sigs) / len(sigs), 3) if sigs else None,
                    "by_instrument": tracker.scorecard()},
    }
