"""Long daily history for several markets, cached on disk.

Nifty 50 (from Jul 1990) and Nifty Bank (from 2000) come from NSE's historical
index API in 3-month chunks (it returns at most ~70 rows per request).
Sensex, S&P 500, Nasdaq and the commodity futures (WTI crude, natural gas, gold,
silver, copper: the global markets MCX contracts track) come from Yahoo Finance.
Early NSE years often have open = high = low = close; rules that need real
intraday ranges are evaluated only where ranges exist (see has_ohlc()).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from ..config import DATA_DIR

CACHE = DATA_DIR / "long"
CACHE.mkdir(parents=True, exist_ok=True)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Market:
    key: str
    name: str
    source: str        # "nse" or "yahoo"
    symbol: str        # NSE index name or Yahoo ticker
    start: str
    # train / test / holdout boundaries (holdout = most recent, never used for selection)
    test_from: str
    holdout_from: str


MARKETS = {
    "NIFTY": Market("NIFTY", "Nifty 50", "nse", "NIFTY 50", "1990-07-01", "2006-01-01", "2016-01-01"),
    "BANKNIFTY": Market("BANKNIFTY", "Nifty Bank", "nse", "NIFTY BANK", "2000-01-01", "2010-01-01", "2018-01-01"),
    "SENSEX": Market("SENSEX", "Sensex", "yahoo", "^BSESN", "1997-07-01", "2008-01-01", "2017-01-01"),
    "SP500": Market("SP500", "S&P 500", "yahoo", "^GSPC", "1950-01-01", "1990-01-01", "2010-01-01"),
    "NASDAQ": Market("NASDAQ", "Nasdaq Composite", "yahoo", "^IXIC", "1971-02-05", "1995-01-01", "2012-01-01"),
    # NYMEX WTI front-month continuous: drives MCX crude (priced in INR). Roll days add small jumps.
    "CRUDE": Market("CRUDE", "WTI crude oil", "yahoo", "CL=F", "2000-08-23", "2010-01-01", "2018-01-01"),
    # The other MCX commodities with options, via their global USD futures (front-month continuous)
    "NATGAS": Market("NATGAS", "Henry Hub natural gas", "yahoo", "NG=F", "2000-08-30", "2010-01-01", "2018-01-01"),
    "GOLD": Market("GOLD", "COMEX gold", "yahoo", "GC=F", "2000-08-30", "2010-01-01", "2018-01-01"),
    "SILVER": Market("SILVER", "COMEX silver", "yahoo", "SI=F", "2000-08-30", "2010-01-01", "2018-01-01"),
    "COPPER": Market("COPPER", "COMEX copper", "yahoo", "HG=F", "2000-08-30", "2010-01-01", "2018-01-01"),
}


def _path(key: str):
    return CACHE / f"{key}.csv"


def _nse_chunks(name: str, start: date, end: date) -> pd.DataFrame:
    from ..market.feed import _nse
    from urllib.parse import quote

    rows, a = [], start
    while a <= end:
        b = min(a + timedelta(days=89), end)
        url = (f"https://www.nseindia.com/api/historicalOR/indicesHistory?indexType={quote(name)}"
               f"&from={a:%d-%m-%Y}&to={b:%d-%m-%Y}")
        for attempt in range(3):
            try:
                rows += _nse.get(url).get("data", [])
                break
            except Exception as e:
                log.warning("NSE history %s %s: %s", name, a, e)
                time.sleep(2 + attempt * 3)
        time.sleep(0.4)  # be polite to NSE
        a = b + timedelta(days=1)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["EOD_TIMESTAMP"], format="%d-%b-%Y")
    df = df.rename(columns={"EOD_OPEN_INDEX_VAL": "Open", "EOD_HIGH_INDEX_VAL": "High",
                            "EOD_LOW_INDEX_VAL": "Low", "EOD_CLOSE_INDEX_VAL": "Close"})
    df = df[["Date", "Open", "High", "Low", "Close"]].dropna(subset=["Close"])
    return df.drop_duplicates("Date").set_index("Date").sort_index().astype(float)


def _yahoo(ticker: str, start: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, start=start, interval="1d", progress=False, auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.astype(float)


def load(key: str, refresh: bool = False) -> pd.DataFrame:
    """Daily OHLC, cached; refreshes the tail at most once a day."""
    m = MARKETS[key]
    path = _path(key)
    cached = pd.read_csv(path, index_col=0, parse_dates=True) if path.exists() else pd.DataFrame()
    today = date.today()
    fresh = len(cached) and (today - cached.index[-1].date()).days <= 1 and not refresh
    if len(cached) and (fresh or path.stat().st_mtime > time.time() - 6 * 3600):
        return cached
    try:
        if m.source == "nse":
            start = cached.index[-1].date() - timedelta(days=10) if len(cached) else date.fromisoformat(m.start)
            new = _nse_chunks(m.symbol, start, today)
        else:
            new = _yahoo(m.symbol, m.start)
    except Exception as e:
        log.warning("refresh %s failed: %s", key, e)
        new = pd.DataFrame()
    df = pd.concat([cached, new]) if len(cached) else new
    if df.empty:
        raise RuntimeError(f"no data for {key}")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["Close"] > 0]
    df.to_csv(path)
    return df


def has_ohlc(df: pd.DataFrame) -> pd.Series:
    """True on days with a real intraday range (open/high/low differ from close)."""
    return (df["High"] > df["Low"]) & (df["Open"] != df["Close"].shift(1))


def coverage() -> list[dict]:
    out = []
    for k, m in MARKETS.items():
        p = _path(k)
        if p.exists():
            df = pd.read_csv(p, index_col=0, parse_dates=True)
            out.append({"key": k, "name": m.name, "from": df.index[0].strftime("%Y-%m-%d"),
                        "to": df.index[-1].strftime("%Y-%m-%d"), "days": len(df),
                        "years": round((df.index[-1] - df.index[0]).days / 365.25, 1)})
    return out
