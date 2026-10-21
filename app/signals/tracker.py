"""Records every live CALL/PUT signal and grades it against later prices,
so the app keeps an honest live scorecard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd

from .. import db
from ..config import IST
from . import engine as E


def open_signal(key: str) -> dict | None:
    rows = db.query("SELECT * FROM signals WHERE symbol = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (key,))
    return rows[0] if rows else None


def _close(sig: dict, status: str, ts: str, spot: float):
    pts = (spot - sig["entry"]) if sig["side"] == "CALL" else (sig["entry"] - spot)
    r = pts / sig["risk"] if sig["risk"] else 0.0
    db.execute("UPDATE signals SET status=?, exit_ts=?, exit_spot=?, points=?, r=? WHERE id=?",
               (status, ts, round(spot, 2), round(pts, 2), round(r, 2), sig["id"]))


def update(key: str, df1m: pd.DataFrame, eod=None) -> dict | None:
    """Walk 1-min bars since the open signal's last check; close it on stop/target/EOD."""
    eod = eod or E.EOD_EXIT
    sig = open_signal(key)
    if not sig:
        return None
    since = pd.Timestamp(sig["checked_ts"] or sig["ts"])
    bars = df1m[df1m.index > since]
    long = sig["side"] == "CALL"
    for t, b in bars.iterrows():
        if t.date() != since.date():
            _close(sig, "eod", t.isoformat(), float(bars[bars.index < t]["Close"].iloc[-1]) if (bars.index < t).any() else sig["entry"])
            return None
        if (b.Low <= sig["sl"]) if long else (b.High >= sig["sl"]):
            _close(sig, "breakeven" if sig["t1_hit"] else "stop", t.isoformat(), sig["sl"])
            return None
        if (b.High >= sig["t2"]) if long else (b.Low <= sig["t2"]):
            _close(sig, "target2", t.isoformat(), sig["t2"])
            return None
        if not sig["t1_hit"] and ((b.High >= sig["t1"]) if long else (b.Low <= sig["t1"])):
            sig["t1_hit"], sig["sl"] = 1, sig["entry"]
            db.execute("UPDATE signals SET t1_hit=1, sl=? WHERE id=?", (sig["entry"], sig["id"]))
        if t.time() >= eod:
            _close(sig, "eod", t.isoformat(), float(b.Close))
            return None
    if len(bars):
        db.execute("UPDATE signals SET checked_ts=? WHERE id=?", (bars.index[-1].isoformat(), sig["id"]))
    return open_signal(key)


def maybe_open(a: dict) -> dict | None:
    """Store a new signal if the analysis says CALL/PUT and rules allow another one."""
    side, key = a["signal"], a["key"]
    if not side:
        return None
    cur = open_signal(key)
    now = datetime.now(IST)
    if cur:
        if cur["side"] == side:
            return None
        _close(cur, "reversed", now.isoformat(timespec="seconds"), a["spot"])
    today = now.date().isoformat()
    n_today = db.query("SELECT COUNT(*) n FROM signals WHERE symbol=? AND substr(ts,1,10)=?", (key, today))[0]["n"]
    if n_today >= E.MAX_SIGNALS_PER_DAY:
        return None
    recent = db.query("SELECT exit_ts FROM signals WHERE symbol=? AND side=? AND exit_ts IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1", (key, side))
    if recent and recent[0]["exit_ts"] and pd.Timestamp(recent[0]["exit_ts"]) > pd.Timestamp(now - timedelta(minutes=15)):
        return None  # don't jump straight back in after being stopped out
    lv, opt = a["levels"], a["option"]
    db.execute(
        "INSERT INTO signals (ts, symbol, side, score, entry, sl, t1, t2, risk, strike, opt_type, expiry, premium, "
        "premium_sl, premium_t1, premium_t2, reasons, status, t1_hit, checked_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',0,?)",
        (now.isoformat(timespec="seconds"), key, side, a["score"], lv["entry"], lv["sl"], lv["t1"], lv["t2"], lv["risk"],
         opt.get("strike"), opt.get("type"), opt.get("expiry"), opt.get("premium"), opt.get("premium_sl"),
         opt.get("premium_t1"), opt.get("premium_t2"),
         json.dumps([r for r in a["reasons"] if r["vote"] * (1 if side == "CALL" else -1) > 0]), a["last_tick"]))
    return open_signal(key)


def history(key: str | None = None, limit: int = 200) -> list[dict]:
    if key:
        rows = db.query("SELECT * FROM signals WHERE symbol=? ORDER BY id DESC LIMIT ?", (key, limit))
    else:
        rows = db.query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["reasons"] = json.loads(r["reasons"] or "[]")
    return rows


def scorecard() -> dict:
    out = {}
    for key in [r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM signals")]:
        closed = db.query("SELECT side, r, points, status, t1_hit FROM signals WHERE symbol=? AND status!='open'", (key,))
        if not closed:
            out[key] = {"trades": 0}
            continue
        rs = [c["r"] for c in closed]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x < 0]
        out[key] = {"trades": len(rs), "win_rate": round(len(wins) / len(rs), 3),
                    "t1_hit_rate": round(sum(c["t1_hit"] for c in closed) / len(rs), 3),
                    "avg_r": round(sum(rs) / len(rs), 3), "total_points": round(sum(c["points"] for c in closed), 1),
                    "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None}
    return out


def check_price(key: str, price: float, ts: str) -> dict | None:
    """Instant stop/target check on a live tick. Returns an event dict when something happened."""
    sig = open_signal(key)
    if not sig:
        return None
    long = sig["side"] == "CALL"
    if (price <= sig["sl"]) if long else (price >= sig["sl"]):
        status = "breakeven" if sig["t1_hit"] else "stop"
        _close(sig, status, ts, sig["sl"])
        return {"event": status, "signal": {**sig, "status": status}}
    if (price >= sig["t2"]) if long else (price <= sig["t2"]):
        _close(sig, "target2", ts, sig["t2"])
        return {"event": "target2", "signal": {**sig, "status": "target2"}}
    if not sig["t1_hit"] and ((price >= sig["t1"]) if long else (price <= sig["t1"])):
        db.execute("UPDATE signals SET t1_hit=1, sl=? WHERE id=?", (sig["entry"], sig["id"]))
        return {"event": "target1", "signal": {**sig, "t1_hit": 1, "sl": sig["entry"]}}
    return None
