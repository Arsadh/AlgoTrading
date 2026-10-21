"""Replay the signal rules over ~60 days of 5-min candles and grade every call.

Simulated on the underlying index (option P&L is roughly delta x points, minus
time decay, which this ignores, so treat results as an upper bound for option buyers):
- signal on a completed bar -> enter at the next bar's open, same day only
- exit at stop-loss, at target 2, or at 15:20; after target 1 the stop moves to entry
- if stop and target fall in the same bar we assume the stop hit first (conservative)
- only fresh setups (the bar before had no signal in that direction), max 3 per day
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import engine as E


def run(df5: pd.DataFrame, max_per_day: int = E.MAX_SIGNALS_PER_DAY, daily_bias: pd.Series | None = None,
        hours: dict | None = None) -> dict:
    """hours: the instrument's session times (Instrument.hours()); defaults to NSE."""
    eod = hours["eod"] if hours else E.EOD_EXIT
    f = E.frame(df5, hours["or_end"]) if hours else E.frame(df5)
    v = E.votes(f)
    weights = dict(E.TECH_WEIGHTS)
    if daily_bias is not None and len(daily_bias):
        v["daily_algo"] = E.daily_vote(f.index, daily_bias)
        weights.update(E.DAILY_WEIGHT)
    sc = E.score(v, weights)
    sig = E.fresh(E.decide(f, sc, (hours["sig_from"], hours["sig_to"]) if hours else None))
    trades = []
    per_day: dict = {}
    i, n = 0, len(f)
    idx = f.index
    while i < n - 1:
        side = sig.iat[i]
        if (not side or idx[i + 1].date() != idx[i].date() or f["tod"].iat[i + 1] >= eod
                or per_day.get(idx[i].date(), 0) >= max_per_day):
            i += 1
            continue
        per_day[idx[i].date()] = per_day.get(idx[i].date(), 0) + 1
        lv = E.levels(side, f["Close"].iat[i], f["st_line"].iat[i], f["atr"].iat[i])
        entry = f["Open"].iat[i + 1]
        shift = entry - lv["entry"]  # keep the same distances from the actual fill
        sl, t1, t2, risk = lv["sl"] + shift, lv["t1"] + shift, lv["t2"] + shift, lv["risk"]
        if not risk > 0:  # no valid stop distance (indicators not warmed up / bad bars)
            i += 1
            continue
        t1_hit, exit_px, reason, j = False, None, "eod", i + 1
        while j < n and idx[j].date() == idx[i].date():
            hi, lo = f["High"].iat[j], f["Low"].iat[j]
            stop_hit = lo <= sl if side == "CALL" else hi >= sl
            tgt_hit = hi >= t2 if side == "CALL" else lo <= t2
            if stop_hit:
                exit_px, reason = sl, "breakeven" if t1_hit else "stop"
                break
            if tgt_hit:
                exit_px, reason = t2, "target2"
                break
            if not t1_hit and (hi >= t1 if side == "CALL" else lo <= t1):
                t1_hit, sl = True, entry
            if f["tod"].iat[j] >= eod:
                exit_px = f["Close"].iat[j]
                break
            j += 1
        if exit_px is None:
            j = min(j, n - 1)
            exit_px = f["Close"].iat[j]
        pts = (exit_px - entry) if side == "CALL" else (entry - exit_px)
        trades.append({"time": idx[i + 1].isoformat(), "side": side, "entry": round(entry, 2),
                       "exit": round(exit_px, 2), "exit_time": idx[min(j, n - 1)].isoformat(),
                       "points": round(pts, 2), "r": round(pts / risk, 2) if risk else 0.0,
                       "reason": reason, "t1_hit": t1_hit, "score": round(float(sc.iat[i]), 1)})
        i = j + 1
    return {"from": idx[0].isoformat() if n else None, "to": idx[-1].isoformat() if n else None,
            "days": int(len(set(idx.date))), "stats": stats(trades), "trades": trades}


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t["r"] for t in trades])
    pts = np.array([t["points"] for t in trades])
    wins, losses = r[r > 0], r[r < 0]
    streak = worst = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    out = {
        "trades": len(trades), "win_rate": round(float((r > 0).mean()), 3),
        "t1_hit_rate": round(float(np.mean([t["t1_hit"] for t in trades])), 3),
        "avg_r": round(float(r.mean()), 3), "total_r": round(float(r.sum()), 2),
        "avg_points": round(float(pts.mean()), 1), "total_points": round(float(pts.sum()), 1),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() else None,
        "max_losing_streak": int(worst),
    }
    for side in ("CALL", "PUT"):
        rs = np.array([t["r"] for t in trades if t["side"] == side])
        out[side.lower()] = {"trades": int(len(rs)), "win_rate": round(float((rs > 0).mean()), 3) if len(rs) else None,
                             "avg_r": round(float(rs.mean()), 3) if len(rs) else None}
    return out
