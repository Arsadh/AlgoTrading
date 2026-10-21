import numpy as np
import pandas as pd


def sma(s: pd.DataFrame | pd.Series, n: int):
    return s.rolling(n, min_periods=n).mean()


def ema(s, n: int):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s, n: int = 14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def rolling_high(s, n: int):
    return s.rolling(n, min_periods=n).max()


def rolling_low(s, n: int):
    return s.rolling(n, min_periods=n).min()


def bollinger(s, n: int = 20, k: float = 2.0):
    m = sma(s, n)
    sd = s.rolling(n, min_periods=n).std()
    return m - k * sd, m, m + k * sd
