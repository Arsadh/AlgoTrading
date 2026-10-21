"""How the market moves after its own moves.

Conditional studies on daily data: what happened next after streaks, big days,
big weeks, drawdowns, stretched prices, RSI extremes, new highs/lows, gaps,
calendar positions - plus "today's setup": the most similar historical days
(nearest neighbours) and what followed them.

Every bucket reports its sample size and a z-score against the base rate, so
small or insignificant effects are visible as such.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..strategies.indicators import rsi, sma
from . import data


def _fwd(c: pd.Series, n: int) -> pd.Series:
    return c.shift(-n) / c - 1


def _bucket_stats(groups, fwd: dict[str, pd.Series], base_up: float, up_key: str) -> list[dict]:
    rows = []
    for label, idx in groups:
        idx = idx[~fwd[up_key].loc[idx].isna()]
        n = len(idx)
        if n == 0:
            continue
        p = float((fwd[up_key].loc[idx] > 0).mean())
        z = (p - base_up) / np.sqrt(base_up * (1 - base_up) / n) if n else 0.0
        row = {"bucket": str(label), "n": n, "p_up": round(p, 3), "z": round(float(z), 2),
               "significant": bool(abs(z) >= 2 and n >= 30)}
        for k, s in fwd.items():
            row[f"avg_{k}"] = round(float(s.loc[idx].mean()), 5)
        rows.append(row)
    return rows


def _cut(x: pd.Series, bins, labels, fwd, base_up, up_key):
    b = pd.cut(x, bins=bins, labels=labels)
    groups = [(lab, x.index[b == lab]) for lab in labels]
    return _bucket_stats(groups, fwd, base_up, up_key)


def streaks(c: pd.Series) -> pd.Series:
    d = np.sign(c.diff()).fillna(0).to_numpy()
    out = np.zeros(len(d))
    for i in range(1, len(d)):
        if d[i] > 0:
            out[i] = out[i - 1] + 1 if out[i - 1] > 0 else 1
        elif d[i] < 0:
            out[i] = out[i - 1] - 1 if out[i - 1] < 0 else -1
    return pd.Series(out, index=c.index)


def features(df: pd.DataFrame) -> pd.DataFrame:
    c = df.Close
    r = c.pct_change(fill_method=None)
    f = pd.DataFrame(index=df.index)
    f["r1"] = r
    f["r5"] = c / c.shift(5) - 1
    f["r20"] = c / c.shift(20) - 1
    f["dist200"] = c / sma(c, 200) - 1
    f["rsi14"] = rsi(c, 14)
    f["drawdown"] = c / c.cummax() - 1
    f["vol_ratio"] = r.rolling(20).std() / r.rolling(250).std()
    f["streak"] = streaks(c)
    f["new_high"] = c >= c.rolling(252).max()
    f["new_low"] = c <= c.rolling(252).min()
    return f


def study(key: str, since: str | None = None) -> dict:
    """All studies; with `since`, only days from that date count (indicators still use full history)."""
    full = data.load(key)
    f_full = features(full)
    c = full.Close
    fwd = {"1d": _fwd(c, 1), "5d": _fwd(c, 5), "20d": _fwd(c, 20)}
    fwd_long = {"20d": _fwd(c, 20), "60d": _fwd(c, 60), "250d": _fwd(c, 250)}
    today = today_setup(full, f_full, fwd)
    keep = full.index >= since if since else np.ones(len(full), bool)
    df, f = full[keep], f_full[keep]
    r1 = f["r1"]
    fwd = {k: v[keep] for k, v in fwd.items()}
    fwd_long = {k: v[keep] for k, v in fwd_long.items()}
    c = df.Close
    base = {k: round(float(v.mean()), 5) for k, v in {**fwd, "60d": fwd_long["60d"], "250d": fwd_long["250d"]}.items()}
    b1 = float((fwd["1d"].dropna() > 0).mean())
    b5 = float((fwd["5d"].dropna() > 0).mean())
    b20 = float((fwd["20d"].dropna() > 0).mean())
    b250 = float((fwd_long["250d"].dropna() > 0).mean())
    out: dict = {"key": key, "name": data.MARKETS[key].name, "since": since, "from": df.index[0].strftime("%Y-%m-%d"),
                 "to": df.index[-1].strftime("%Y-%m-%d"), "days": len(df),
                 "base": {"p_up_1d": round(b1, 3), "p_up_5d": round(b5, 3), "p_up_20d": round(b20, 3),
                          "p_up_250d": round(b250, 3), **{f"avg_{k}": v for k, v in base.items()}}}

    st = f["streak"].clip(-6, 6)
    out["streaks"] = _bucket_stats([(f"{int(k):+d}" + (" or more" if abs(k) == 6 else ""), st.index[st == k])
                                    for k in range(-6, 7) if k != 0], fwd, b1, "1d")
    out["day_moves"] = _cut(r1, [-1, -.03, -.02, -.01, -.005, 0, .005, .01, .02, .03, 1],
                            ["< -3%", "-3..-2%", "-2..-1%", "-1..-0.5%", "-0.5..0%", "0..+0.5%", "+0.5..1%", "+1..2%", "+2..3%", "> +3%"],
                            fwd, b1, "1d")
    out["week_moves"] = _cut(f["r5"], [-1, -.06, -.04, -.02, 0, .02, .04, .06, 1],
                             ["< -6%", "-6..-4%", "-4..-2%", "-2..0%", "0..+2%", "+2..4%", "+4..6%", "> +6%"], fwd, b5, "5d")
    out["drawdowns"] = _cut(-f["drawdown"], [-0.001, .02, .05, .10, .20, .30, .40, 1],
                            ["0-2% below peak", "2-5%", "5-10%", "10-20%", "20-30%", "30-40%", "> 40%"], fwd_long, b250, "250d")
    out["dist200"] = _cut(f["dist200"], [-1, -.15, -.10, -.05, 0, .05, .10, .15, 1],
                          ["< -15%", "-15..-10%", "-10..-5%", "-5..0%", "0..+5%", "+5..10%", "+10..15%", "> +15%"], fwd_long, b20, "20d")
    out["rsi"] = _cut(f["rsi14"], [0, 20, 30, 40, 50, 60, 70, 80, 100],
                      ["< 20", "20-30", "30-40", "40-50", "50-60", "60-70", "70-80", "> 80"], fwd, b5, "5d")
    out["highs_lows"] = _bucket_stats([("New 52-week high", f.index[f["new_high"]]), ("New 52-week low", f.index[f["new_low"]])],
                                      fwd_long, b20, "20d")

    # Markov: does yesterday's direction predict today's?
    up = (r1 > 0).astype(int)
    prev, prev2 = up.shift(1), up.shift(2)
    nxt = up.shift(-1)
    mk = []
    for lab, mask in (("after an up day", up == 1), ("after a down day", up == 0),
                      ("after up, up", (up == 1) & (prev == 1)), ("after down, down", (up == 0) & (prev == 0)),
                      ("after down, up", (up == 1) & (prev == 0)), ("after up, down", (up == 0) & (prev == 1)),
                      ("after 3 ups", (up == 1) & (prev == 1) & (prev2 == 1)), ("after 3 downs", (up == 0) & (prev == 0) & (prev2 == 0))):
        n = int(mask.sum())
        p = float(nxt[mask].mean())
        mk.append({"state": lab, "n": n, "p_up_next": round(p, 3),
                   "z": round((p - b1) / np.sqrt(b1 * (1 - b1) / n), 2) if n else 0})
    out["markov"] = mk

    # Momentum vs mean reversion over time: lag-1 autocorrelation by decade
    dec = []
    for d0 in range(int(df.index[0].year // 10 * 10), int(df.index[-1].year) + 1, 10):
        s = r1[(r1.index.year >= d0) & (r1.index.year < d0 + 10)].dropna()
        if len(s) > 200:
            dec.append({"decade": f"{d0}s", "lag1_autocorr": round(float(s.autocorr(1)), 3),
                        "lag5_autocorr": round(float(s.autocorr(5)), 3),
                        "abs_lag1_autocorr": round(float(s.abs().autocorr(1)), 3), "n": len(s)})
    out["autocorr_by_decade"] = dec

    # Volatility clustering
    big = r1.abs() > r1.abs().quantile(0.9)
    out["vol_clustering"] = {"p_big_after_big": round(float(big.shift(-1)[big].mean()), 3),
                             "p_big_base": round(float(big.mean()), 3),
                             "p_up_after_big_down": round(float((r1.shift(-1)[big & (r1 < 0)] > 0).mean()), 3),
                             "p_up_after_big_up": round(float((r1.shift(-1)[big & (r1 > 0)] > 0).mean()), 3)}

    # Gaps (days with real opens only)
    ok = data.has_ohlc(df)
    gap = (df.Open / c.shift(1) - 1)[ok]
    intraday = (c / df.Open - 1)[ok]
    filled = pd.Series(np.where(gap > 0, df.Low[ok] <= c.shift(1)[ok], df.High[ok] >= c.shift(1)[ok]), index=gap.index)
    gb = pd.cut(gap, [-1, -.02, -.01, -.005, -.001, .001, .005, .01, .02, 1],
                labels=["< -2%", "-2..-1%", "-1..-0.5%", "-0.5..-0.1%", "flat", "+0.1..0.5%", "+0.5..1%", "+1..2%", "> +2%"])
    out["gaps"] = [{"gap": str(k), "n": int(len(g)), "avg_open_to_close": round(float(g.mean()), 5),
                    "p_close_above_open": round(float((g > 0).mean()), 3), "p_filled": round(float(filled[g.index].mean()), 3)}
                   for k, g in intraday.groupby(gb, observed=True)]

    # Calendar
    out["month"] = [{"month": pd.Timestamp(2000, m, 1).strftime("%b"),
                     "avg_month": round(float((1 + g).groupby(g.index.year).prod().sub(1).mean()), 4),
                     "p_up_month": round(float(((1 + g).groupby(g.index.year).prod() > 1).mean()), 3)}
                    for m, g in r1.dropna().groupby(r1.dropna().index.month)]
    out["weekday"] = [{"day": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d], "avg": round(float(g.mean()), 5),
                       "p_up": round(float((g > 0).mean()), 3), "n": len(g)}
                      for d, g in r1.dropna().groupby(r1.dropna().index.dayofweek) if len(g) > 50]
    month = pd.Series(df.index.to_period("M"), index=df.index)  # partial first month is fine for averages
    pos = month.groupby(month).cumcount()
    from_end = month.groupby(month).cumcount(ascending=False)
    tom = []
    for lab, mask in (("Last day of month", from_end == 0), ("Day 1", pos == 0), ("Day 2", pos == 1), ("Day 3", pos == 2),
                      ("Rest of month", (pos > 2) & (from_end > 0))):
        g = r1[mask].dropna()
        tom.append({"day": lab, "avg": round(float(g.mean()), 5), "p_up": round(float((g > 0).mean()), 3), "n": len(g)})
    out["turn_of_month"] = tom

    out["today"] = today
    out["findings"] = findings(out)
    return out


def today_setup(df: pd.DataFrame, f: pd.DataFrame, fwd: dict) -> dict:
    last = f.iloc[-1]
    cols = ["r1", "r5", "r20", "dist200", "rsi14", "drawdown", "vol_ratio"]
    hist = f[cols].iloc[:-20].dropna().astype(float)
    z = (hist - hist.mean()) / hist.std()
    q = (last[cols].astype(float) - hist.mean()) / hist.std()
    dist = np.sqrt(((z - q) ** 2).sum(axis=1))
    near = dist.nsmallest(60).index
    # avoid 60 copies of one episode: keep analogs at least 5 trading days apart
    picked = []
    for d in near:
        if all(abs(df.index.get_loc(d) - df.index.get_loc(p)) >= 5 for p in picked):
            picked.append(d)
        if len(picked) == 40:
            break
    res = {}
    for k, s in fwd.items():
        v = s.loc[picked].dropna()
        res[k] = {"n": len(v), "p_up": round(float((v > 0).mean()), 3), "avg": round(float(v.mean()), 5),
                  "median": round(float(v.median()), 5), "p10": round(float(v.quantile(0.1)), 4),
                  "p90": round(float(v.quantile(0.9)), 4)}
    return {
        "date": df.index[-1].strftime("%Y-%m-%d"), "close": round(float(df.Close.iloc[-1]), 2),
        "state": {"day_move": round(float(last["r1"]), 4), "week_move": round(float(last["r5"]), 4),
                  "month_move": round(float(last["r20"]), 4), "streak": int(last["streak"]),
                  "dist200": round(float(last["dist200"]), 4), "rsi14": round(float(last["rsi14"]), 1),
                  "drawdown": round(float(last["drawdown"]), 4), "vol_ratio": round(float(last["vol_ratio"]), 2),
                  "new_high": bool(last["new_high"]), "new_low": bool(last["new_low"])},
        "analogs": res,
        "analog_dates": [{"date": d.strftime("%Y-%m-%d"), "next_5d": round(float(fwd["5d"].get(d, np.nan)), 4)
                          if pd.notna(fwd["5d"].get(d, np.nan)) else None} for d in picked[:12]],
    }


def findings(s: dict) -> list[str]:
    out = []
    b = s["base"]
    out.append(f"{s['name']} rose on {b['p_up_1d']:.0%} of days and over {b['p_up_250d']:.0%} of 1-year windows "
               f"({s['from'][:4]}–{s['to'][:4]}, {s['days']:,} days).")
    mk = {m["state"]: m for m in s["markov"]}
    a, d = mk["after an up day"], mk["after a down day"]
    out.append(f"After an up day the next day rose {a['p_up_next']:.0%} of the time; after a down day {d['p_up_next']:.0%}"
               + (" (yesterday's direction tends to continue)" if a["p_up_next"] > d["p_up_next"] + 0.02 else
                  " (yesterday's direction tends to reverse)" if d["p_up_next"] > a["p_up_next"] + 0.02 else " (little memory)") + ".")
    dec = s["autocorr_by_decade"]
    if len(dec) >= 2:
        out.append("Day-to-day momentum (lag-1 autocorrelation) by decade: "
                   + ", ".join(f"{x['decade']} {x['lag1_autocorr']:+.2f}" for x in dec)
                   + ". Positive means trends persisted day to day; near zero means the market became efficient.")
    sig = [r for r in s["day_moves"] + s["streaks"] if r["significant"]]
    if sig:
        best = max(sig, key=lambda r: abs(r["z"]))
        out.append(f"Strongest next-day effect: after '{best['bucket']}' ({best['n']} cases) the next day rose "
                   f"{best['p_up']:.0%} vs {b['p_up_1d']:.0%} normally (z = {best['z']:+.1f}).")
    dd = [r for r in s["drawdowns"] if r["n"] >= 30]
    if dd:
        deep = dd[-1]
        out.append(f"When {s['name']} was {deep['bucket']} below its peak, it was higher a year later {deep['p_up']:.0%} of the time "
                   f"(avg {deep['avg_250d']:+.0%}), vs {b['p_up_250d']:.0%} for any day.")
    vc = s["vol_clustering"]
    out.append(f"Volatility clusters: after a top-10% move, the next day was also a top-10% move {vc['p_big_after_big']:.0%} of the time "
               f"(vs {vc['p_big_base']:.0%} normally). Big moves come in bunches, so size option trades smaller after a shock.")
    hl = {r["bucket"]: r for r in s["highs_lows"]}
    if "New 52-week high" in hl:
        h = hl["New 52-week high"]
        out.append(f"After a new 52-week high, the next 20 days were up {h['p_up']:.0%} of the time "
                   f"(avg {h['avg_20d']:+.1%} vs {b['avg_20d']:+.1%} for any day): highs tend to lead to more highs.")
    return out


def today(key: str) -> dict:
    """Today's setup + nearest historical analogs (cheap enough to run daily)."""
    df = data.load(key)
    c = df.Close
    return today_setup(df, features(df), {"1d": _fwd(c, 1), "5d": _fwd(c, 5), "20d": _fwd(c, 20)})
