"""Strategy library.

Every strategy is a function (close, ctx) -> target weights, a DataFrame of
dates x symbols. A weight decided at the close of day t is held during day t+1.
ctx carries market-wide series: ctx["index"] (Nifty close), ctx["vix"].
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from .indicators import rolling_high, rolling_low, rsi, sma, bollinger

MIN_SLOTS = 5  # with fewer than 5 qualifying stocks, the rest stays in cash


def _positions(entry: pd.DataFrame, exit_: pd.DataFrame) -> pd.DataFrame:
    """Turn entry/exit signals into a 0/1 holding state (exit wins on ties)."""
    state = pd.DataFrame(float("nan"), index=entry.index, columns=entry.columns)
    state[entry.fillna(False)] = 1.0
    state[exit_.fillna(False)] = 0.0
    return state.ffill().fillna(0.0)


def _equal_weight(pos: pd.DataFrame, slots: int = MIN_SLOTS) -> pd.DataFrame:
    n = pos.sum(axis=1).clip(lower=slots)
    return pos.div(n, axis=0).fillna(0.0)


def _month_ends(idx: pd.DatetimeIndex) -> pd.Series:
    s = pd.Series(idx, index=idx)
    return s.groupby([idx.year, idx.month]).transform("max") == s


# ---------------------------------------------------------------- strategies

def sma_trend(close, ctx):
    """Trend following: hold a stock while its 50-day SMA is above its 200-day SMA."""
    pos = (sma(close, 50) > sma(close, 200)).astype(float)
    return _equal_weight(pos, slots=10)


def donchian_breakout(close, ctx):
    """Turtle-style: buy a 55-day closing high, exit on a 20-day closing low."""
    entry = close > rolling_high(close, 55).shift(1)
    exit_ = close < rolling_low(close, 20).shift(1)
    return _equal_weight(_positions(entry, exit_), slots=10)


def rsi2_pullback(close, ctx):
    """Buy short, sharp dips (RSI(2) < 10) in stocks above their 200-day SMA; exit on a bounce above the 5-day SMA."""
    up = close > sma(close, 200)
    entry = up & (rsi(close, 2) < 10)
    exit_ = (close > sma(close, 5)) | ~up
    return _equal_weight(_positions(entry, exit_), slots=MIN_SLOTS)


def bollinger_reversion(close, ctx):
    """Buy closes below the lower Bollinger band (uptrend only); exit at the middle band."""
    lo, mid, _ = bollinger(close, 20, 2.0)
    up = close > sma(close, 200)
    entry = up & (close < lo)
    exit_ = (close > mid) | ~up
    return _equal_weight(_positions(entry, exit_), slots=MIN_SLOTS)


def momentum_rotation(close, ctx, top: int = 10):
    """Monthly: hold the 10 stocks with the best 6-month return (skipping the latest month), if positive."""
    mom = close.shift(21) / close.shift(147) - 1
    rank = mom.rank(axis=1, ascending=False)
    pick = ((rank <= top) & (mom > 0)).astype(float)
    w = pick.div(top)
    me = _month_ends(close.index)
    w[~me.values] = float("nan")
    return w.ffill().fillna(0.0)


def low_volatility(close, ctx, top: int = 10):
    """Monthly: hold the 10 least volatile stocks of the last year (defensive factor)."""
    vol = close.pct_change(fill_method=None).rolling(252, min_periods=200).std()
    rank = vol.rank(axis=1, ascending=True)
    w = (rank <= top).astype(float).div(top)
    w[vol.isna()] = 0.0
    me = _month_ends(close.index)
    w[~me.values] = float("nan")
    return w.ffill().fillna(0.0)


# ------------------------------------------------------------------ overlays

def regime_filter(weights: pd.DataFrame, ctx, vix_max: float = 25.0) -> pd.DataFrame:
    """Go to cash when Nifty is below its 200-day SMA or India VIX is elevated."""
    idx = ctx["index"].reindex(weights.index).ffill()
    vix = ctx["vix"].reindex(weights.index).ffill()
    ok = (idx > sma(idx, 200)) & ((vix < vix_max) | vix.isna())
    return weights.mul(ok.astype(float), axis=0)


BASE: dict[str, Callable] = {
    "sma_trend": sma_trend,
    "donchian_breakout": donchian_breakout,
    "rsi2_pullback": rsi2_pullback,
    "bollinger_reversion": bollinger_reversion,
    "momentum_rotation": momentum_rotation,
    "low_volatility": low_volatility,
}


def all_strategies() -> dict[str, Callable]:
    """Each base strategy plus a '+regime' variant that respects the market filter."""
    out: dict[str, Callable] = {}
    for name, fn in BASE.items():
        out[name] = fn
        out[f"{name}+regime"] = (lambda f: lambda c, x: regime_filter(f(c, x), x))(fn)
    return out


def describe() -> list[dict]:
    rows = []
    for name, fn in BASE.items():
        rows.append({"name": name, "description": (fn.__doc__ or "").strip()})
        rows.append({"name": f"{name}+regime",
                     "description": "Same, but in cash when Nifty < 200-day SMA or India VIX > 25."})
    return rows
