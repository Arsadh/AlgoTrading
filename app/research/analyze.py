"""Full research run: backtest every strategy over all available years and study
how the Indian market behaves (regimes, overnight gaps, shocks, events, news).

    python -m app.research.analyze            # writes data/research/report.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .. import db
from ..backtest import engine
from ..config import DATA_DIR, IST, ROOT
from ..data import history
from ..strategies import all_strategies, describe
from ..strategies.indicators import sma

OUT_DIR = DATA_DIR / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT = OUT_DIR / "report.json"
SPLIT = "2019-01-01"  # in-sample before, out-of-sample from here on
log = logging.getLogger(__name__)


def _r(x, n=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


# ------------------------------------------------------------ strategy tests

def backtest_all(close: pd.DataFrame, ctx: dict) -> tuple[list[dict], dict]:
    results, curves = [], {}
    descs = {d["name"]: d["description"] for d in describe()}

    bench = ctx["index"].pct_change(fill_method=None).reindex(close.index).fillna(0.0)
    ew = close.pct_change(fill_method=None).mean(axis=1).fillna(0.0)
    for name, ret, desc in [("NIFTY 50 (buy & hold)", bench, "Benchmark: hold the index."),
                            ("Equal-weight universe", ew, "Benchmark: hold all universe stocks equally.")]:
        results.append(_summarise(name, desc, ret, None, None, benchmark=True))
        curves[name] = engine.equity_curve(ret)

    for name, fn in all_strategies().items():
        w = fn(close, ctx)
        bt = engine.run(w, close)
        results.append(_summarise(name, descs.get(name, ""), bt["net"], bt["exposure"], bt["turnover"]))
        curves[name] = engine.equity_curve(bt["net"])
    return results, curves


def _summarise(name, desc, ret, exposure, turnover, benchmark=False) -> dict:
    ret = ret[ret.index >= ret.ne(0).idxmax()]  # skip warm-up period with no positions
    return {
        "name": name, "description": desc, "benchmark": benchmark,
        "start": ret.index[0].strftime("%Y-%m-%d"), "end": ret.index[-1].strftime("%Y-%m-%d"),
        "full": engine.metrics(ret, exposure, turnover),
        "in_sample": engine.metrics(ret[ret.index < SPLIT]),
        "out_of_sample": engine.metrics(ret[ret.index >= SPLIT]),
        "yearly": engine.yearly_returns(ret),
    }


def recommend(results: list[dict]) -> list[dict]:
    """Rank strategies on out-of-sample risk-adjusted return, require that they
    also held up in-sample (guards against a lucky recent run)."""
    bench = next(r for r in results if r["name"].startswith("NIFTY"))
    b_oos = bench["out_of_sample"]
    picks = []
    for r in results:
        if r["benchmark"]:
            continue
        o, i = r["out_of_sample"], r["in_sample"]
        if not o or not i:
            continue
        score = o["sharpe"] * 0.6 + o["calmar"] * 0.4
        reasons = []
        if o["sharpe"] > b_oos["sharpe"]:
            reasons.append(f"OOS Sharpe {o['sharpe']:.2f} vs Nifty {b_oos['sharpe']:.2f}")
        if o["max_drawdown"] > b_oos["max_drawdown"]:
            reasons.append(f"shallower OOS drawdown ({o['max_drawdown']:.0%} vs {b_oos['max_drawdown']:.0%})")
        b_is = bench["in_sample"]
        robust = i["sharpe"] > b_is["sharpe"] and o["sharpe"] > b_oos["sharpe"]
        picks.append({"name": r["name"], "score": _r(score), "robust": robust, "reasons": reasons,
                      "oos_cagr": o["cagr"], "oos_sharpe": o["sharpe"], "oos_max_dd": o["max_drawdown"]})
    picks.sort(key=lambda p: (p["robust"], p["score"] or -9), reverse=True)
    return picks


# ---------------------------------------------------------- market behaviour

def market_behaviour(idx: pd.DataFrame, vix: pd.Series) -> dict:
    c = idx["Close"]
    ret = c.pct_change(fill_method=None)
    fwd20 = c.shift(-20) / c - 1
    out: dict = {}

    yr = ret.groupby(ret.index.year)
    out["yearly"] = [{"year": int(y), "return": _r((1 + g).prod() - 1),
                      "max_drawdown": _r(((1 + g).cumprod() / (1 + g).cumprod().cummax() - 1).min()),
                      "volatility": _r(g.std() * np.sqrt(252))} for y, g in yr]

    # Fear gauge: what happened over the next 20 days for each VIX level
    v = vix.reindex(c.index).ffill()
    bins = [0, 12, 16, 20, 25, 35, 200]
    labels = ["<12", "12-16", "16-20", "20-25", "25-35", ">35"]
    vb = pd.cut(v, bins=bins, labels=labels)
    out["vix_regimes"] = [{"vix": str(k), "days": int(g.count()), "avg_fwd_20d": _r(g.mean()),
                           "pct_positive": _r((g > 0).mean())}
                          for k, g in fwd20.groupby(vb, observed=True)]

    above = c > sma(c, 200)
    out["trend_regimes"] = [{"regime": "Nifty above 200-day SMA" if k else "Nifty below 200-day SMA",
                             "days": int(g.count()), "avg_daily": _r(g.mean(), 5),
                             "ann_vol": _r(g.std() * np.sqrt(252)), "pct_positive": _r((g > 0).mean())}
                            for k, g in ret.groupby(above.shift(1, fill_value=False))]

    # Overnight gaps are mostly the market digesting news that broke after hours
    prev = c.shift(1)
    o = idx["Open"]
    valid = (o - prev).abs() > 1e-6  # older Yahoo data sometimes sets open == prev close
    gap_all = o / prev - 1
    filled = pd.Series(np.where(gap_all > 0, idx["Low"] <= prev, idx["High"] >= prev), index=c.index)[valid]
    gap = gap_all[valid]
    intraday = (c / o - 1)[valid]
    gb = pd.cut(gap, [-1, -0.02, -0.01, -0.003, 0.003, 0.01, 0.02, 1],
                labels=["< -2%", "-2..-1%", "-1..-0.3%", "flat", "+0.3..1%", "+1..2%", "> +2%"])
    out["gaps"] = [{"gap": str(k), "days": int(g.count()), "avg_intraday": _r(g.mean(), 5),
                    "pct_continued": _r((np.sign(g) == np.sign(gap[g.index])).mean()),
                    "pct_gap_filled": _r(filled[g.index].mean())}
                   for k, g in intraday.groupby(gb, observed=True)]

    # Reaction after shock days
    shocks = []
    for label, mask in [("Down > 3%", ret < -0.03), ("Down 2-3%", (ret < -0.02) & (ret >= -0.03)),
                        ("Up > 2%", ret > 0.02)]:
        f = {h: (c.shift(-h) / c - 1)[mask] for h in (1, 5, 20)}
        shocks.append({"day": label, "count": int(mask.sum()),
                       **{f"fwd_{h}d": _r(s.mean()) for h, s in f.items()},
                       "pct_up_20d": _r((f[20] > 0).mean())})
    out["shocks"] = shocks

    dow = ret.groupby(ret.index.dayofweek)
    out["weekday"] = [{"day": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d], "avg": _r(g.mean(), 5),
                       "pct_positive": _r((g > 0).mean())} for d, g in dow]
    mon = ret.groupby(ret.index.month)
    out["month"] = [{"month": pd.Timestamp(2000, m, 1).strftime("%b"), "avg_daily": _r(g.mean(), 5),
                     "avg_month": _r((1 + g).groupby(g.index.year).prod().sub(1).mean())} for m, g in mon]
    return out


def event_study(idx: pd.DataFrame) -> list[dict]:
    path = ROOT / "events" / "events.csv"
    if not path.exists():
        return []
    ev = pd.read_csv(path, parse_dates=["date"])
    c = idx["Close"]
    rows = []
    for _, e in ev.iterrows():
        pos = c.index.searchsorted(e["date"])
        if pos >= len(c) or pos < 6:
            continue
        p0 = c.iloc[pos - 1]
        def at(k):
            j = pos + k
            return _r(c.iloc[j] / p0 - 1) if j < len(c) else None
        rows.append({"date": c.index[pos].strftime("%Y-%m-%d"), "type": e["type"], "description": e["description"],
                     "pre_5d": _r(p0 / c.iloc[pos - 6] - 1), "day0": at(0), "day1": at(1), "day5": at(5), "day20": at(20)})
    return rows


def news_reaction() -> dict:
    """How Nifty moved after news we ingested live (grows as the app runs)."""
    news = db.query("SELECT seen_ts, sentiment, impact, nifty_at_seen FROM news WHERE nifty_at_seen IS NOT NULL")
    ticks = db.query("SELECT ts, price FROM ticks WHERE symbol = 'NIFTY' ORDER BY ts")
    if not news or not ticks:
        return {"samples": 0, "buckets": []}
    t = pd.Series([r["price"] for r in ticks], index=pd.to_datetime([r["ts"] for r in ticks])).sort_index()
    rows = []
    for n in news:
        ts = pd.Timestamp(n["seen_ts"])
        r = {"sentiment": n["sentiment"], "impact": n["impact"]}
        for m in (15, 60):
            after = t[t.index >= ts + timedelta(minutes=m)]
            if len(after) and (after.index[0] - ts) < timedelta(minutes=m + 30):
                r[f"r{m}"] = after.iloc[0] / n["nifty_at_seen"] - 1
        rows.append(r)
    df = pd.DataFrame(rows)
    if "r15" not in df:
        return {"samples": len(df), "buckets": []}
    df["bucket"] = pd.cut(df["sentiment"], [-1.01, -0.5, -0.15, 0.15, 0.5, 1.01],
                          labels=["very negative", "negative", "neutral", "positive", "very positive"])
    out = []
    for (b, imp), g in df.groupby(["bucket", "impact"], observed=True):
        out.append({"sentiment": str(b), "impact": imp, "count": int(len(g)),
                    "avg_15m": _r(g["r15"].mean(), 5), "avg_60m": _r(g.get("r60", pd.Series()).mean(), 5)})
    return {"samples": int(len(df)), "buckets": out}


def findings(results, beh, picks) -> list[str]:
    """Plain-language takeaways generated from the numbers."""
    f = []
    b = next(r for r in results if r["name"].startswith("NIFTY"))["full"]
    f.append(f"Nifty buy-and-hold: {b['cagr']:.1%} a year with a worst drawdown of {b['max_drawdown']:.0%}.")
    tr = {x["regime"]: x for x in beh["trend_regimes"]}
    a, z = tr.get("Nifty above 200-day SMA"), tr.get("Nifty below 200-day SMA")
    if a and z:
        f.append(f"Volatility is {z['ann_vol'] / a['ann_vol']:.1f}x higher when Nifty is below its 200-day SMA "
                 f"({z['ann_vol']:.0%} vs {a['ann_vol']:.0%} annualised); this is why the '+regime' variants exist.")
    hi = [v for v in beh["vix_regimes"] if v["vix"] in ("25-35", ">35")]
    if hi:
        f.append("When India VIX is above 25, the following 20 days averaged "
                 + ", ".join(f"{v['avg_fwd_20d']:+.1%} (VIX {v['vix']})" for v in hi)
                 + ": panic has often been a buying opportunity for patient money, but with very wide swings.")
    sh = next((s for s in beh["shocks"] if s["day"] == "Down > 3%"), None)
    if sh and sh["count"]:
        f.append(f"After {sh['count']} days where Nifty fell more than 3%, the next 20 days averaged "
                 f"{sh['fwd_20d']:+.1%} and were positive {sh['pct_up_20d']:.0%} of the time.")
    big = [g for g in beh["gaps"] if g["gap"] in ("< -2%", "> +2%")]
    for g in big:
        f.append(f"Overnight gaps {g['gap']} ({g['days']} days): the move continued intraday {g['pct_continued']:.0%} "
                 f"of the time and the gap filled {g['pct_gap_filled']:.0%} of the time.")
    ew = next(r for r in results if r["name"].startswith("Equal-weight"))["full"]
    f.append(f"Caution: simply holding all universe stocks equally returned {ew['cagr']:.1%} a year, more than the index. "
             "Much of that is survivorship bias (the list is today's large caps), so strategy returns above the "
             "index are inflated too. Compare strategies with each other and with the equal-weight line, not only with Nifty.")
    churn = [r for r in results if not r["benchmark"] and (r["full"].get("turnover_per_year") or 0) > 25]
    if churn:
        f.append("High-turnover strategies (" + ", ".join(r["name"] for r in churn) + ") lose most of their edge to "
                 "costs at 0.15% per side. Short-term mean reversion needs very low costs to work on daily data.")
    robust = [p for p in picks if p["robust"]][:3]
    if robust:
        f.append("Strategies with a better Sharpe ratio than Nifty both before and after 2019: "
                 + "; ".join(f"{p['name']} (OOS CAGR {p['oos_cagr']:.1%}, Sharpe {p['oos_sharpe']:.2f}, max DD {p['oos_max_dd']:.0%})"
                             for p in robust) + ".")
    return f


def run() -> dict:
    close = history.load_panel()
    idx = history.load_index()
    vix = history.load_vix()
    close = close[close.index >= idx.index[0]]
    ctx = {"index": idx["Close"], "vix": vix}

    results, curves = backtest_all(close, ctx)
    beh = market_behaviour(idx, vix)
    picks = recommend(results)
    report = {
        "generated": datetime.now(IST).isoformat(timespec="seconds"),
        "universe": list(close.columns), "split": SPLIT,
        "data_range": [close.index[0].strftime("%Y-%m-%d"), close.index[-1].strftime("%Y-%m-%d")],
        "assumptions": {"cost_bps_per_side": engine.COST_BPS, "risk_free": engine.RISK_FREE,
                        "fill": "close of signal day", "survivorship_bias": True},
        "strategies": results, "curves": curves, "recommendations": picks,
        "market": beh, "events": event_study(idx), "news_reaction": news_reaction(),
    }
    report["findings"] = findings(results, beh, picks)
    REPORT.write_text(json.dumps(report, default=_r))
    return report


def load_report() -> dict | None:
    return json.loads(REPORT.read_text()) if REPORT.exists() else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rep = run()
    print("\n".join(rep["findings"]))
    print("\nTop strategies:")
    for p in rep["recommendations"][:6]:
        print(f"  {p['name']:32s} robust={p['robust']!s:5s} OOS CAGR={p['oos_cagr']:.1%} "
              f"Sharpe={p['oos_sharpe']:.2f} MaxDD={p['oos_max_dd']:.0%}")
