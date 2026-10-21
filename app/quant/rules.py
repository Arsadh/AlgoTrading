"""Library of classic technical / calendar / pattern rules on daily bars.

Every rule maps an OHLC frame to a position series in {-1, 0, +1} decided at
the close of day t and held during day t+1 (+1 = long / buy calls,
-1 = short / buy puts, 0 = flat). Rules only look at data up to day t.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from ..signals.indicators import adx, atr, macd_hist, supertrend
from ..strategies.indicators import bollinger, ema, rolling_high, rolling_low, rsi, sma


@dataclass(frozen=True)
class Rule:
    key: str
    name: str
    category: str
    fn: Callable[[pd.DataFrame], pd.Series]
    needs_ohlc: bool = False
    description: str = ""


# ------------------------------------------------------------------ helpers
def _state(index, long_in=None, long_out=None, short_in=None, short_out=None) -> pd.Series:
    """Event-driven position: enter on *_in, leave on *_out, otherwise keep."""
    n = len(index)
    li = np.zeros(n, bool) if long_in is None else long_in.fillna(False).to_numpy()
    lo = np.zeros(n, bool) if long_out is None else long_out.fillna(False).to_numpy()
    si = np.zeros(n, bool) if short_in is None else short_in.fillna(False).to_numpy()
    so = np.zeros(n, bool) if short_out is None else short_out.fillna(False).to_numpy()
    pos = np.zeros(n)
    cur = 0.0
    for i in range(n):
        if cur == 1 and lo[i]:
            cur = 0.0
        elif cur == -1 and so[i]:
            cur = 0.0
        if li[i]:
            cur = 1.0
        elif si[i]:
            cur = -1.0
        pos[i] = cur
    return pd.Series(pos, index=index)


def _hold(event_long: pd.Series, event_short: pd.Series | None = None, days: int = 5) -> pd.Series:
    """Hold for N days after an event (most recent event wins)."""
    idx = event_long.index
    out = np.zeros(len(idx))
    el = event_long.fillna(False).to_numpy()
    es = np.zeros(len(idx), bool) if event_short is None else event_short.fillna(False).to_numpy()
    left, cur = 0, 0.0
    for i in range(len(idx)):
        if el[i]:
            cur, left = 1.0, days
        elif es[i]:
            cur, left = -1.0, days
        out[i] = cur if left > 0 else 0.0
        left -= 1
    return pd.Series(out, index=idx)


def _sign(s: pd.Series) -> pd.Series:
    return np.sign(s).fillna(0.0)


def _next_day(df: pd.DataFrame) -> pd.Series:
    """Date of the next trading day (known calendar)."""
    nd = pd.Series(df.index, index=df.index).shift(-1)
    return nd.fillna(df.index[-1] + pd.tseries.offsets.BDay(1))


# ------------------------------------------------------------------- trend
def sma_cross(f, s):
    return lambda df: _sign(sma(df.Close, f) - sma(df.Close, s))


def ema_cross(f, s):
    return lambda df: _sign(ema(df.Close, f) - ema(df.Close, s))


def price_vs_sma(n):
    return lambda df: _sign(df.Close - sma(df.Close, n))


def macd_signal(df):
    return _sign(macd_hist(df.Close))


def macd_zero(df):
    return _sign(ema(df.Close, 12) - ema(df.Close, 26))


def donchian(entry, exit_):
    def f(df):
        c = df.Close
        return _state(df.index, c > rolling_high(c, entry).shift(1), c < rolling_low(c, exit_).shift(1),
                      c < rolling_low(c, entry).shift(1), c > rolling_high(c, exit_).shift(1))
    return f


def supertrend_daily(df):
    return supertrend(df, 10, 3.0)[1].replace(0, np.nan).ffill().fillna(0.0)


def adx_trend(df):
    h, l = df.High, df.Low
    upm, dnm = h.diff(), -l.diff()
    plus = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0), index=df.index).ewm(alpha=1 / 14, adjust=False).mean()
    minus = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0), index=df.index).ewm(alpha=1 / 14, adjust=False).mean()
    return _sign(plus - minus).where(adx(df) > 25, 0.0)


def ichimoku(df):
    hh = lambda n: df.High.rolling(n).max()  # noqa: E731
    ll = lambda n: df.Low.rolling(n).min()  # noqa: E731
    tenkan, kijun = (hh(9) + ll(9)) / 2, (hh(26) + ll(26)) / 2
    span_a, span_b = ((tenkan + kijun) / 2).shift(26), ((hh(52) + ll(52)) / 2).shift(26)
    top, bot = np.maximum(span_a, span_b), np.minimum(span_a, span_b)
    long = (df.Close > top) & (tenkan > kijun)
    short = (df.Close < bot) & (tenkan < kijun)
    return pd.Series(np.where(long, 1.0, np.where(short, -1.0, 0.0)), index=df.index)


def parabolic_sar(df, step=0.02, mx=0.2):
    h, l, c = df.High.to_numpy(), df.Low.to_numpy(), df.Close.to_numpy()
    n = len(c)
    pos = np.zeros(n)
    if n < 3:
        return pd.Series(pos, index=df.index)
    up, af, ep, sar = True, step, h[0], l[0]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, mx)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, mx)
        pos[i] = 1.0 if up else -1.0
    return pd.Series(pos, index=df.index)


def roc_momentum(n):
    return lambda df: _sign(df.Close / df.Close.shift(n) - 1)


def high_52w(df):
    c = df.Close
    long = c >= 0.95 * rolling_high(c, 252)
    short = c <= 1.05 * rolling_low(c, 252)
    return pd.Series(np.where(long, 1.0, np.where(short, -1.0, 0.0)), index=df.index)


def keltner(df):
    mid = ema(df.Close, 20)
    a = atr(df, 10)
    c = df.Close
    return _state(df.index, c > mid + 2 * a, c < mid, c < mid - 2 * a, c > mid)


def heikin_ashi(df):
    hc = ((df.Open + df.High + df.Low + df.Close) / 4).to_numpy()
    ho = np.zeros(len(hc))
    ho[0] = (df.Open.iloc[0] + df.Close.iloc[0]) / 2
    for i in range(1, len(hc)):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    up = pd.Series(hc > ho, index=df.index)
    long = up & up.shift(1, fill_value=False)
    short = ~up & ~up.shift(1, fill_value=True)
    return pd.Series(np.where(long, 1.0, np.where(short, -1.0, np.nan)), index=df.index).ffill().fillna(0.0)


def ma_ribbon(df):
    c = df.Close
    above = sum((c > sma(c, n)).astype(int) for n in (10, 20, 50, 100, 200))
    return pd.Series(np.where(above >= 4, 1.0, np.where(above <= 1, -1.0, 0.0)), index=df.index)


def rsi_trend(df):
    return _sign(rsi(df.Close, 14) - 50)


def cci(df, n=20):
    tp = (df.High + df.Low + df.Close) / 3
    m = sma(tp, n)
    mad = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - m) / (0.015 * mad)


def cci_trend(df):
    x = cci(df)
    return _state(df.index, x > 100, x < 0, x < -100, x > 0)


def bollinger_breakout(df):
    lo, mid, up = bollinger(df.Close, 20, 2)
    c = df.Close
    return _state(df.index, c > up, c < mid, c < lo, c > mid)


def low_vol_long(df):
    v = df.Close.pct_change().rolling(20).std()
    return (v < v.rolling(252, min_periods=200).median()).astype(float)


# --------------------------------------------------------- mean reversion
def rsi2(df):
    r = rsi(df.Close, 2)
    return _state(df.index, r < 10, r > 50, r > 90, r < 50)


def rsi14_revert(df):
    r = rsi(df.Close, 14)
    return _state(df.index, r < 30, r > 50, r > 70, r < 50)


def bollinger_revert(df):
    lo, mid, up = bollinger(df.Close, 20, 2)
    c = df.Close
    return _state(df.index, c < lo, c > mid, c > up, c < mid)


def stochastic(df):
    ll, hh = df.Low.rolling(14).min(), df.High.rolling(14).max()
    k = (100 * (df.Close - ll) / (hh - ll)).rolling(3).mean()
    return _state(df.index, k < 20, k > 50, k > 80, k < 50)


def williams_r(df):
    ll, hh = df.Low.rolling(10).min(), df.High.rolling(10).max()
    w = -100 * (hh - df.Close) / (hh - ll)
    return _state(df.index, w < -90, w > -50, w > -10, w < -50)


def cci_revert(df):
    x = cci(df)
    return _state(df.index, x < -100, x > 0, x > 100, x < 0)


def streak(n):
    def f(df):
        r = np.sign(df.Close.diff())
        down = r.rolling(n).sum() == -n
        up = r.rolling(n).sum() == n
        return _state(df.index, down, r > 0, up, r < 0)
    return f


def big_day_revert(df):
    r = df.Close.pct_change()
    return _hold(r < -0.02, r > 0.02, days=3)


def big_day_follow(df):
    r = df.Close.pct_change()
    return _hold(r > 0.02, r < -0.02, days=3)


def ibs(df):
    rng = (df.High - df.Low).replace(0, np.nan)
    x = (df.Close - df.Low) / rng
    return pd.Series(np.where(x < 0.2, 1.0, np.where(x > 0.8, -1.0, 0.0)), index=df.index)


def trend_pullback(df):
    c = df.Close
    up = c > sma(c, 200)
    r = rsi(c, 2)
    return _state(df.index, up & (r < 10), (c > sma(c, 5)) | ~up, ~up & (r > 90), (c < sma(c, 5)) | up)


# ---------------------------------------------------------------- patterns
def engulfing(df):
    o, c = df.Open, df.Close
    bull = (c.shift(1) < o.shift(1)) & (c > o) & (o <= c.shift(1)) & (c >= o.shift(1))
    bear = (c.shift(1) > o.shift(1)) & (c < o) & (o >= c.shift(1)) & (c <= o.shift(1))
    return _hold(bull, bear, days=5)


def hammer_star(df):
    o, h, l, c = df.Open, df.High, df.Low, df.Close
    body = (c - o).abs().replace(0, 1e-9)
    lower = np.minimum(o, c) - l
    upper = h - np.maximum(o, c)
    downtrend = c < sma(c, 10)
    hammer = (lower >= 2 * body) & (upper <= body) & downtrend
    star = (upper >= 2 * body) & (lower <= body) & ~downtrend
    return _hold(hammer, star, days=5)


def inside_bar(df):
    inside = (df.High.shift(1) < df.High.shift(2)) & (df.Low.shift(1) > df.Low.shift(2))
    return _hold(inside & (df.Close > df.High.shift(1)), inside & (df.Close < df.Low.shift(1)), days=5)


def nr7(df):
    rng = df.High - df.Low
    narrow = (rng == rng.rolling(7).min()).shift(1, fill_value=False)
    return _hold(narrow & (df.Close > df.High.shift(1)), narrow & (df.Close < df.Low.shift(1)), days=5)


# ---------------------------------------------------------------- calendar
def turn_of_month(df):
    nd = _next_day(df)
    s = pd.Series(nd.values, index=df.index)
    month = s.dt.to_period("M")
    pos_in_month = s.groupby(month).cumcount()
    last = month != month.shift(-1)
    return ((pos_in_month < 3) | last).astype(float)


def weekday(d):
    return lambda df: (pd.Series(_next_day(df).values, index=df.index).dt.dayofweek == d).astype(float)


def halloween(df):
    m = pd.Series(_next_day(df).values, index=df.index).dt.month
    return m.isin([11, 12, 1, 2, 3, 4]).astype(float)


def buy_hold(df):
    return pd.Series(1.0, index=df.index)


RULES: list[Rule] = [
    Rule("buy_hold", "Buy & hold (baseline)", "baseline", buy_hold, description="Always long."),
    # trend
    Rule("sma_10_50", "SMA 10/50 cross", "trend", sma_cross(10, 50)),
    Rule("sma_20_100", "SMA 20/100 cross", "trend", sma_cross(20, 100)),
    Rule("sma_50_200", "Golden/death cross 50/200", "trend", sma_cross(50, 200)),
    Rule("ema_9_21", "EMA 9/21 cross", "trend", ema_cross(9, 21)),
    Rule("px_sma20", "Price vs SMA 20", "trend", price_vs_sma(20)),
    Rule("px_sma50", "Price vs SMA 50", "trend", price_vs_sma(50)),
    Rule("px_sma100", "Price vs SMA 100", "trend", price_vs_sma(100)),
    Rule("px_sma200", "Price vs SMA 200", "trend", price_vs_sma(200)),
    Rule("macd_signal", "MACD vs signal line", "trend", macd_signal),
    Rule("macd_zero", "MACD above/below zero", "trend", macd_zero),
    Rule("donchian_20_10", "Donchian 20/10 breakout", "trend", donchian(20, 10)),
    Rule("donchian_55_20", "Turtle 55/20 breakout", "trend", donchian(55, 20)),
    Rule("supertrend", "Supertrend (10,3) daily", "trend", supertrend_daily, True),
    Rule("adx_trend", "ADX > 25 with DI direction", "trend", adx_trend, True),
    Rule("ichimoku", "Ichimoku cloud + TK cross", "trend", ichimoku, True),
    Rule("psar", "Parabolic SAR", "trend", parabolic_sar, True),
    Rule("keltner", "Keltner channel breakout", "trend", keltner, True),
    Rule("heikin_ashi", "Heikin-Ashi 2-candle trend", "trend", heikin_ashi, True),
    Rule("ma_ribbon", "MA ribbon (5 averages)", "trend", ma_ribbon),
    Rule("bb_breakout", "Bollinger breakout", "trend", bollinger_breakout),
    Rule("cci_trend", "CCI ±100 trend", "trend", cci_trend, True),
    Rule("rsi_50", "RSI 14 above/below 50", "momentum", rsi_trend),
    Rule("roc_63", "3-month momentum", "momentum", roc_momentum(63)),
    Rule("roc_126", "6-month momentum", "momentum", roc_momentum(126)),
    Rule("roc_252", "12-month momentum", "momentum", roc_momentum(252)),
    Rule("high_52w", "Near 52-week high / low", "momentum", high_52w),
    Rule("big_day_follow", "Follow a ±2% day (3 days)", "momentum", big_day_follow),
    # mean reversion
    Rule("rsi2", "RSI 2 (10/90) Connors", "mean_reversion", rsi2),
    Rule("rsi14_revert", "RSI 14 (30/70) reversal", "mean_reversion", rsi14_revert),
    Rule("bb_revert", "Bollinger band reversal", "mean_reversion", bollinger_revert),
    Rule("stochastic", "Stochastic (20/80)", "mean_reversion", stochastic, True),
    Rule("williams_r", "Williams %R (-90/-10)", "mean_reversion", williams_r, True),
    Rule("cci_revert", "CCI ±100 reversal", "mean_reversion", cci_revert, True),
    Rule("streak_3", "Fade 3-day streaks", "mean_reversion", streak(3)),
    Rule("streak_4", "Fade 4-day streaks", "mean_reversion", streak(4)),
    Rule("big_day_revert", "Fade a ±2% day (3 days)", "mean_reversion", big_day_revert),
    Rule("ibs", "Internal bar strength (0.2/0.8)", "mean_reversion", ibs, True),
    Rule("trend_pullback", "Pullback in trend (SMA200 + RSI2)", "mean_reversion", trend_pullback),
    # candlestick patterns
    Rule("engulfing", "Engulfing candles (5 days)", "pattern", engulfing, True),
    Rule("hammer_star", "Hammer / shooting star (5 days)", "pattern", hammer_star, True),
    Rule("inside_bar", "Inside-bar breakout (5 days)", "pattern", inside_bar, True),
    Rule("nr7", "NR7 breakout (5 days)", "pattern", nr7, True),
    # calendar / volatility
    Rule("turn_of_month", "Turn of month (last + first 3 days)", "calendar", turn_of_month),
    Rule("halloween", "Nov–Apr only (sell in May)", "calendar", halloween),
    Rule("monday", "Long Mondays only", "calendar", weekday(0)),
    Rule("tuesday", "Long Tuesdays only", "calendar", weekday(1)),
    Rule("wednesday", "Long Wednesdays only", "calendar", weekday(2)),
    Rule("thursday", "Long Thursdays only", "calendar", weekday(3)),
    Rule("friday", "Long Fridays only", "calendar", weekday(4)),
    Rule("low_vol", "Long only in calm markets", "volatility", low_vol_long),
]
BY_KEY = {r.key: r for r in RULES}
