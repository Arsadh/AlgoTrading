"""Test every rule on every market, split into train / test / holdout periods,
and against random strategies with the same exposure (to see what pure luck gives).

Cost: COST_BPS per unit of position change (index futures + slippage). Option
buyers pay more (spread + time decay), so treat these as best-case numbers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data
from .rules import RULES, Rule

COST_BPS = 5
DAYS = 252
N_RANDOM = 200
BLOCK = 20


def positions(rule: Rule, df: pd.DataFrame) -> pd.Series:
    pos = rule.fn(df).reindex(df.index).fillna(0.0).clip(-1, 1)
    if rule.needs_ohlc:
        ok = data.has_ohlc(df).rolling(5, min_periods=1).min().astype(bool)
        pos = pos.where(ok, 0.0)
    return pos


def strat_returns(pos: pd.Series, close: pd.Series, cost_bps: float = COST_BPS) -> pd.Series:
    r = close.pct_change(fill_method=None).fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    cost = pos.diff().abs().fillna(pos.abs()) * cost_bps / 1e4
    return held * r - cost.shift(1).fillna(0.0)


def metrics(ret: pd.Series, pos: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    if len(ret) < 60:
        return {}
    eq = (1 + ret).cumprod()
    yrs = len(ret) / DAYS
    sd = ret.std()
    out = {
        "cagr": float(eq.iloc[-1] ** (1 / yrs) - 1) if eq.iloc[-1] > 0 else -1.0,
        "sharpe": float(ret.mean() / sd * np.sqrt(DAYS)) if sd > 0 else 0.0,
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "vol": float(sd * np.sqrt(DAYS)),
    }
    if pos is not None:
        p = pos.reindex(ret.index)
        out["exposure"] = float(p.abs().mean())
        out["net_long"] = float(p.mean())
        out["trades_per_year"] = float((p.diff().abs() > 0).sum() / yrs)
        active = ret[p.shift(1).fillna(0) != 0]
        out["hit_rate"] = float((active > 0).mean()) if len(active) else None
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def periods(m: data.Market, idx: pd.DatetimeIndex) -> dict[str, pd.Series]:
    s = pd.Series(True, index=idx)
    return {"train": s[idx < m.test_from].index, "test": s[(idx >= m.test_from) & (idx < m.holdout_from)].index,
            "holdout": s[idx >= m.holdout_from].index, "full": idx}


def random_sharpes(pos: pd.Series, close: pd.Series, n: int = N_RANDOM, seed: int = 7) -> np.ndarray:
    """Block-shuffle the position series: same exposure and turnover profile, random timing."""
    rng = np.random.default_rng(seed)
    p = pos.to_numpy()
    blocks = [p[i:i + BLOCK] for i in range(0, len(p), BLOCK)]
    out = np.empty(n)
    for k in range(n):
        order = rng.permutation(len(blocks))
        shuffled = pd.Series(np.concatenate([blocks[j] for j in order]), index=pos.index)
        r = strat_returns(shuffled, close)
        out[k] = r.mean() / r.std() * np.sqrt(DAYS) if r.std() > 0 else 0.0
    return out


def evaluate_all(markets: list[str] | None = None, with_random: str = "NIFTY") -> dict:
    markets = markets or list(data.MARKETS)
    frames = {k: data.load(k) for k in markets}
    rows, pos_cache = [], {}
    for rule in RULES:
        row = {"key": rule.key, "name": rule.name, "category": rule.category, "needs_ohlc": rule.needs_ohlc, "markets": {}}
        for mk, df in frames.items():
            m = data.MARKETS[mk]
            pos = positions(rule, df)
            pos_cache[(rule.key, mk)] = pos
            ret = strat_returns(pos, df.Close)
            lo = strat_returns(pos.clip(lower=0), df.Close)
            per = periods(m, df.index)
            row["markets"][mk] = {
                **{p: metrics(ret.loc[ix], pos) for p, ix in per.items()},
                "long_only": metrics(lo, pos.clip(lower=0)),
            }
        if with_random in frames and rule.key != "buy_hold":
            df = frames[with_random]
            pos = pos_cache[(rule.key, with_random)]
            actual = row["markets"][with_random]["full"].get("sharpe", 0)
            rs = random_sharpes(pos, df.Close)
            row["random"] = {"p_value": round(float((rs >= actual).mean()), 3),
                             "random_p95": round(float(np.percentile(rs, 95)), 3),
                             "random_median": round(float(np.median(rs)), 3)}
        rows.append(row)
    for row in rows:
        row["verdict"], row["why"] = verdict(row)
    return {"rows": rows, "positions": pos_cache, "frames": frames}


def verdict(row: dict) -> tuple[str, str]:
    if row["key"] == "buy_hold":
        return "baseline", "Reference"
    n = row["markets"].get("NIFTY", {})
    sh = [n.get(p, {}).get("sharpe") for p in ("train", "test", "holdout")]
    all_pos = all(s is not None and s > 0 for s in sh)
    p = row.get("random", {}).get("p_value", 1)
    others = [row["markets"][k]["full"].get("sharpe", 0) for k in ("SP500", "SENSEX", "BANKNIFTY", "NASDAQ") if k in row["markets"]]
    abroad = sum(1 for s in others if s > 0)
    why = (f"Nifty Sharpe train/test/holdout {', '.join('–' if s is None else f'{s:.2f}' for s in sh)}; "
           f"beats {100 * (1 - p):.0f}% of random timings; positive in {abroad}/{len(others)} other markets")
    if all_pos and p <= 0.05 and abroad >= len(others) - 1:
        return "robust", why
    if all_pos and (p <= 0.2 or abroad >= len(others) - 1):
        return "promising", why
    return "fails", why
