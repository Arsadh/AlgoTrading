"""The combined daily algorithm ("AlgTrd Composite").

Selection uses ONLY the training periods (Nifty before 2006 and S&P 500 before
1990), so the test and holdout results are genuinely out-of-sample:
  a rule joins if its long/short Sharpe was > TRAIN_MIN_NIFTY on Nifty-train
  AND > 0 on S&P-500-train (it had to work in two unrelated markets).
The composite position is the plain average of the member rules (-1..+1);
no weights are fitted, which keeps over-fitting to a minimum.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data, evaluate
from .rules import BY_KEY

TRAIN_MIN_NIFTY = 0.3


def select(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        if r["key"] == "buy_hold" or r["category"] == "calendar" and r["key"] not in ("turn_of_month", "halloween"):
            continue  # single weekdays are noise by construction
        n = r["markets"]["NIFTY"]["train"].get("sharpe", 0)
        s = r["markets"]["SP500"]["train"].get("sharpe", 0)
        if n > TRAIN_MIN_NIFTY and s > 0:
            out.append(r["key"])
    return out


def composite(members: list[str], df: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    votes = pd.DataFrame({k: evaluate.positions(BY_KEY[k], df) for k in members})
    return votes.mean(axis=1), votes


def build(res: dict | None = None) -> dict:
    res = res or evaluate.evaluate_all()
    members = select(res["rows"])
    out = {"members": [{"key": k, "name": BY_KEY[k].name, "category": BY_KEY[k].category} for k in members],
           "rule": f"Nifty-train Sharpe > {TRAIN_MIN_NIFTY} and S&P-500-train Sharpe > 0", "markets": {}}
    for mk, df in res["frames"].items():
        m = data.MARKETS[mk]
        pos, votes = composite(members, df)
        ret = evaluate.strat_returns(pos, df.Close)
        lo = evaluate.strat_returns(pos.clip(lower=0), df.Close)
        bh = df.Close.pct_change(fill_method=None).fillna(0.0)
        per = evaluate.periods(m, df.index)
        eq = lambda r: (1 + r).cumprod()  # noqa: E731
        weekly = lambda s: [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in s.resample("W-FRI").last().dropna().items()]  # noqa: E731
        out["markets"][mk] = {
            "name": m.name, "test_from": m.test_from, "holdout_from": m.holdout_from,
            "composite": {p: evaluate.metrics(ret.loc[ix], pos) for p, ix in per.items()},
            "long_only": {p: evaluate.metrics(lo.loc[ix], pos.clip(lower=0)) for p, ix in per.items()},
            "buy_hold": {p: evaluate.metrics(bh.loc[ix]) for p, ix in per.items()},
            "curve": {"composite": weekly(eq(ret)), "long_only": weekly(eq(lo)), "buy_hold": weekly(eq(bh))},
            "today": {"date": df.index[-1].strftime("%Y-%m-%d"), "bias": round(float(pos.iloc[-1]), 3),
                      "votes": {k: float(v) for k, v in votes.iloc[-1].items()}},
            "bias_history": [[d.strftime("%Y-%m-%d"), round(float(v), 3)] for d, v in pos.iloc[-260:].items()],
        }
        # How well does the composite's sign call the next day / next 5 days?
        fwd1 = df.Close.pct_change(fill_method=None).shift(-1)
        fwd5 = df.Close.shift(-5) / df.Close - 1
        hold_ix = per["holdout"]
        out["markets"][mk]["direction_hit"] = _hits(pos.loc[hold_ix], fwd1.loc[hold_ix], fwd5.loc[hold_ix])
    return out


def _hits(pos: pd.Series, f1: pd.Series, f5: pd.Series) -> dict:
    out = {}
    for name, mask in (("strong_bull", pos >= 0.5), ("bull", (pos > 0) & (pos < 0.5)), ("neutral", pos == 0),
                       ("bear", (pos < 0) & (pos > -0.5)), ("strong_bear", pos <= -0.5)):
        n = int(mask.sum())
        out[name] = {"days": n,
                     "next_day_up": round(float((f1[mask] > 0).mean()), 3) if n else None,
                     "next_day_avg": round(float(f1[mask].mean()), 5) if n else None,
                     "next_5d_avg": round(float(f5[mask].mean()), 4) if n else None}
    return out


def daily_bias(market: str, members: list[str]) -> tuple[pd.Series, dict]:
    """Composite bias history + today's per-rule votes (used by the live engine)."""
    df = data.load(market)
    pos, votes = composite(members, df)
    today = {"date": df.index[-1].strftime("%Y-%m-%d"), "bias": round(float(pos.iloc[-1]), 3),
             "bullish": int((votes.iloc[-1] > 0).sum()), "bearish": int((votes.iloc[-1] < 0).sum()),
             "members": len(members), "votes": {k: float(v) for k, v in votes.iloc[-1].items()}}
    return pos, today
