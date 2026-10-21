"""Historical daily bars.

Long history (2007 onwards) comes from Yahoo Finance because Groww's API only
keeps ~3 months of intraday data and caps daily candles at ~3 years per call.
Groww is still used for live prices (see broker/groww.py).
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..config import DATA_DIR
from ..universe import INDEX, STOCKS, VIX, yahoo

CACHE = DATA_DIR / "prices"
CACHE.mkdir(parents=True, exist_ok=True)
START = "2007-01-01"
log = logging.getLogger(__name__)


def _cache_path(symbol: str):
    safe = symbol.replace("^", "IDX_").replace("&", "and")
    return CACHE / f"{safe}.csv"


def load(symbol: str, refresh: bool = False) -> pd.DataFrame:
    """Daily OHLCV for one symbol, cached on disk; refreshed at most once a day."""
    path = _cache_path(symbol)
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(df) and df.index[-1].date() >= _last_weekday():
            return df
    import yfinance as yf

    raw = yf.download(yahoo(symbol), start=START, auto_adjust=True, progress=False, threads=False)
    if raw is None or raw.empty:
        if path.exists():
            return pd.read_csv(path, index_col=0, parse_dates=True)
        raise RuntimeError(f"No data for {symbol}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.to_csv(path)
    return df


def _last_weekday() -> date:
    d = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d.date()


def load_panel(symbols: list[str] | None = None, field: str = "Close") -> pd.DataFrame:
    """Dates x symbols matrix of one field (Close by default)."""
    symbols = symbols or STOCKS
    cols = {}
    for s in symbols:
        try:
            cols[s] = load(s)[field]
        except Exception as e:  # keep going if one ticker fails
            log.warning("skip %s: %s", s, e)
    return pd.DataFrame(cols).sort_index()


def load_index() -> pd.DataFrame:
    return load(INDEX)


def load_vix() -> pd.Series:
    return load(VIX)["Close"]
