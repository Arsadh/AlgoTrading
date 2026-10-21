"""Intraday indicators on OHLC frames (columns Open/High/Low/Close)."""
import numpy as np
import pandas as pd

from ..strategies.indicators import ema, rsi  # noqa: F401  (re-exported)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> tuple[pd.Series, pd.Series]:
    """Returns (line, direction) where direction is +1 (up-trend) or -1."""
    a = atr(df, n).to_numpy()
    h, l, c = df["High"].to_numpy(), df["Low"].to_numpy(), df["Close"].to_numpy()
    mid = (h + l) / 2
    up_basic, dn_basic = mid - mult * a, mid + mult * a
    up, dn = np.full(len(c), np.nan), np.full(len(c), np.nan)
    line, dirn = np.full(len(c), np.nan), np.zeros(len(c))
    for i in range(len(c)):
        if np.isnan(a[i]):
            continue
        if i == 0 or np.isnan(up[i - 1]):
            up[i], dn[i], dirn[i] = up_basic[i], dn_basic[i], 1
        else:
            up[i] = max(up_basic[i], up[i - 1]) if c[i - 1] > up[i - 1] else up_basic[i]
            dn[i] = min(dn_basic[i], dn[i - 1]) if c[i - 1] < dn[i - 1] else dn_basic[i]
            if dirn[i - 1] == -1 and c[i] > dn[i - 1]:
                dirn[i] = 1
            elif dirn[i - 1] == 1 and c[i] < up[i - 1]:
                dirn[i] = -1
            else:
                dirn[i] = dirn[i - 1]
        line[i] = up[i] if dirn[i] == 1 else dn[i]
    return pd.Series(line, index=df.index), pd.Series(dirn, index=df.index)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l = df["High"], df["Low"]
    upm, dnm = h.diff(), -l.diff()
    plus = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0), index=df.index)
    minus = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0), index=df.index)
    tr = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    m = ema(close, fast) - ema(close, slow)
    return m - ema(m, sig)
