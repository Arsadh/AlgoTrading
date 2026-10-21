"""Call/put signal rules on 5-minute candles.

The same functions drive the live signal and the historical backtest, so the
backtest numbers describe exactly the rules you see live (minus the option-chain
and news votes, which have no history).

Score: weighted vote of independent indicators, -100 (strong bearish) .. +100
(strong bullish). A BUY CALL needs score >= SIGNAL_THRESHOLD, Supertrend up,
ADX >= MIN_ADX (a trending, not choppy, market) and RSI not already overbought.
BUY PUT is the mirror image.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from ..config import settings
from .indicators import adx, atr, ema, macd_hist, rsi, supertrend

TECH_WEIGHTS = {"trend_15m": 20, "supertrend": 20, "ema_9_21": 10, "macd": 10, "rsi": 10,
                "prev_day": 10, "opening_range": 10}
DAILY_WEIGHT = {"daily_algo": 10}  # multi-decade daily composite (app/quant), known from yesterday's close
LIVE_WEIGHTS = {"option_chain": 10, "news": 5}
LABELS = {
    "trend_15m": "15-min trend (EMA 20/50)", "supertrend": "Supertrend (10,3)", "ema_9_21": "EMA 9 vs 21",
    "macd": "MACD momentum", "rsi": "RSI (14)", "prev_day": "Previous day high/low/close",
    "opening_range": "Opening range (first 15 min)", "option_chain": "Option chain OI (PCR)", "news": "News mood",
    "daily_algo": "Daily algorithm (multi-decade tested rules)",
}
EOD_EXIT = dtime(15, 20)
MAX_SIGNALS_PER_DAY = 3


def _t(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def frame(df5: pd.DataFrame, or_end: dtime = dtime(9, 30)) -> pd.DataFrame:
    """Indicators for every completed 5-min bar. or_end = end of the opening range (session open + 15 min)."""
    f = df5[["Open", "High", "Low", "Close"]].copy()
    c = f["Close"]
    f["ema9"], f["ema21"] = ema(c, 9), ema(c, 21)
    f["st_line"], f["st_dir"] = supertrend(f)
    f["rsi"] = rsi(c, 14)
    f["macd_h"] = macd_hist(c)
    f["adx"] = adx(f)
    f["atr"] = atr(f)

    # 15-min trend, using only 15-min bars that have closed (shift avoids look-ahead)
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    f15 = f[["Open", "High", "Low", "Close"]].resample("15min", origin="start_day", offset="15min").agg(agg).dropna()
    t15 = pd.DataFrame({"e20": ema(f15["Close"], 20), "e50": ema(f15["Close"], 50)})
    t15.index = t15.index + pd.Timedelta(minutes=15)  # value known at the bar's end
    t15 = t15.reindex(f.index.union(t15.index)).ffill().reindex(f.index)
    f["ema20_15"], f["ema50_15"] = t15["e20"], t15["e50"]

    # Previous session levels and today's opening range
    day = f.index.date
    daily = f.groupby(day).agg(H=("High", "max"), L=("Low", "min"), C=("Close", "last"))
    prev = daily.shift(1)
    f["pdh"] = [prev.at[d, "H"] for d in day]
    f["pdl"] = [prev.at[d, "L"] for d in day]
    f["pdc"] = [prev.at[d, "C"] for d in day]
    tod = pd.Series([t.time() for t in f.index], index=f.index)
    in_or = tod < or_end
    orh = f["High"].where(in_or).groupby(day).transform("max")
    orl = f["Low"].where(in_or).groupby(day).transform("min")
    f["orh"] = orh.where(~in_or)  # only known once the opening range has finished
    f["orl"] = orl.where(~in_or)
    f["tod"] = tod
    return f


def votes(f: pd.DataFrame) -> pd.DataFrame:
    c = f["Close"]
    v = pd.DataFrame(index=f.index)
    up15 = (c > f["ema20_15"]) & (f["ema20_15"] > f["ema50_15"])
    dn15 = (c < f["ema20_15"]) & (f["ema20_15"] < f["ema50_15"])
    v["trend_15m"] = np.where(up15, 1.0, np.where(dn15, -1.0, 0.5 * np.sign(f["ema20_15"] - f["ema50_15"])))
    v["supertrend"] = f["st_dir"]
    v["ema_9_21"] = np.sign(f["ema9"] - f["ema21"])
    h, hp = f["macd_h"], f["macd_h"].shift(1)
    v["macd"] = np.where(h > 0, np.where(h > hp, 1.0, 0.5), np.where(h < hp, -1.0, -0.5))
    r = f["rsi"]
    v["rsi"] = np.select([r > 60, r > 55, r < 40, r < 45], [1.0, 0.5, -1.0, -0.5], 0.0)
    v["prev_day"] = np.where(c > f["pdh"], 1.0, np.where(c < f["pdl"], -1.0, 0.5 * np.sign(c - f["pdc"])))
    v["opening_range"] = np.where(c > f["orh"], 1.0, np.where(c < f["orl"], -1.0, 0.0))
    return v.fillna(0.0)


def score(v: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    cols = [k for k in weights if k in v]
    w = pd.Series({k: weights[k] for k in cols})
    return (v[cols] * w).sum(axis=1) / w.sum() * 100


def decide(f: pd.DataFrame, sc: pd.Series, window: tuple[dtime, dtime] | None = None) -> pd.Series:
    """'CALL', 'PUT' or '' per bar. window = (first, last) time a signal may fire."""
    thr = settings.signal_threshold
    lo, hi = window or (_t(settings.no_signal_before), _t(settings.no_signal_after))
    window = (f["tod"] >= lo) & (f["tod"] <= hi)
    trending = f["adx"] >= settings.min_adx
    call = window & trending & (sc >= thr) & (f["st_dir"] == 1) & (f["rsi"] < 75)
    put = window & trending & (sc <= -thr) & (f["st_dir"] == -1) & (f["rsi"] > 25)
    return pd.Series(np.where(call, "CALL", np.where(put, "PUT", "")), index=f.index)


def levels(side: str, close: float, st_line: float, a: float) -> dict:
    """Stop-loss and targets on the underlying. Risk is 0.5-1.5 ATR, targets 1R and 2R."""
    if side == "CALL":
        sl = min(max(st_line, close - 1.5 * a), close - 0.5 * a)
        risk = close - sl
        return {"entry": close, "sl": sl, "t1": close + risk, "t2": close + 2 * risk, "risk": risk}
    sl = max(min(st_line, close + 1.5 * a), close + 0.5 * a)
    risk = sl - close
    return {"entry": close, "sl": sl, "t1": close - risk, "t2": close - 2 * risk, "risk": risk}


def fresh(sig: pd.Series) -> pd.Series:
    """Keep a signal only on the bar where it first appears (not while it persists)."""
    same_as_prev = sig.eq(sig.shift(1)) & pd.Series(sig.index.date, index=sig.index).eq(
        pd.Series(sig.index.date, index=sig.index).shift(1))
    return sig.where(~same_as_prev, "")


def daily_vote(index: pd.DatetimeIndex, daily_bias: pd.Series) -> pd.Series:
    """For each intraday bar, the composite bias as of the previous session's close."""
    b = daily_bias.copy()
    b.index = pd.to_datetime(b.index).date
    days = pd.Series(sorted(set(index.date)))
    prev = {d: b[b.index < d].iloc[-1] if (b.index < d).any() else 0.0 for d in days}
    return pd.Series([prev[d] for d in index.date], index=index)
