"""Derivative contract details (lot size, expiries, Groww tokens) from Groww's public
instrument master, cached once a day. Works without Groww API keys.

Covers NSE index options and the MCX commodity futures and options in INSTRUMENTS.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd

from ..config import DATA_DIR
from .instruments import MCX_KEYS

URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
PATH = DATA_DIR / "instruments_derivs.csv"
MCX_UNDERLYINGS = MCX_KEYS
FUT_ROLL_DAYS = 3  # switch to the next future this many days before expiry (liquidity moves early)
log = logging.getLogger(__name__)
_df: pd.DataFrame | None = None
_loaded = 0.0


def _load() -> pd.DataFrame | None:
    global _df, _loaded
    if _df is not None and time.time() - _loaded < 6 * 3600:
        return _df
    try:
        df = pd.read_csv(PATH, dtype=str) if PATH.exists() and time.time() - PATH.stat().st_mtime < 20 * 3600 else None
        if df is None or not MCX_UNDERLYINGS <= set(df.underlying_symbol):  # stale, or a commodity was added
            raw = pd.read_csv(URL, usecols=["exchange", "exchange_token", "trading_symbol", "groww_symbol", "instrument_type",
                                            "segment", "underlying_symbol", "expiry_date", "strike_price", "lot_size"], dtype=str)
            nse = (raw.exchange == "NSE") & (raw.segment == "FNO") & raw.instrument_type.isin(["CE", "PE"])
            mcx = (raw.exchange == "MCX") & raw.underlying_symbol.isin(MCX_UNDERLYINGS) & raw.instrument_type.isin(["CE", "PE", "FUT"])
            df = raw[nse | mcx]
            df.to_csv(PATH, index=False)
        _df = df.copy()
        _df["strike_price"] = _df["strike_price"].astype(float)
        _loaded = time.time()
    except Exception as e:
        log.warning("instrument master unavailable: %s", e)
    return _df


def find(underlying: str, expiry_iso: str, strike: float, opt_type: str) -> dict | None:
    df = _load()
    if df is None:
        return None
    m = df[(df.underlying_symbol == underlying) & (df.expiry_date == expiry_iso) &
           (df.strike_price == float(strike)) & (df.instrument_type == opt_type)]
    if m.empty:
        return None
    r = m.iloc[0]
    return {"exchange": r.exchange, "exchange_token": str(r.exchange_token), "trading_symbol": r.trading_symbol,
            "lot_size": int(float(r.lot_size)), "underlying": underlying, "expiry": expiry_iso,
            "strike": float(strike), "type": opt_type}


def strikes(underlying: str, expiry_iso: str) -> list[float]:
    """Listed option strikes for one expiry, ascending."""
    df = _load()
    if df is None:
        return []
    m = df[(df.underlying_symbol == underlying) & (df.expiry_date == expiry_iso) & df.instrument_type.isin(["CE", "PE"])]
    return sorted(set(m.strike_price.astype(float)))


def lot_size(underlying: str) -> int | None:
    """Option lot size, in quoted price units (MCX: same as the future, e.g. crude 100 bbl, gold 100 x 10 g)."""
    df = _load()
    if df is None:
        return None
    m = df[(df.underlying_symbol == underlying) & df.instrument_type.isin(["CE", "PE"])]
    return int(float(m.sort_values("expiry_date").iloc[0].lot_size)) if len(m) else None


def option_expiries(underlying: str) -> list[str]:
    """Upcoming option expiries as 'DD-Mon-YYYY', nearest first."""
    df = _load()
    if df is None:
        return []
    today = date.today().isoformat()
    exp = sorted(set(df[(df.underlying_symbol == underlying) & df.instrument_type.isin(["CE", "PE"])
                        & (df.expiry_date >= today)].expiry_date))
    return [date.fromisoformat(e).strftime("%d-%b-%Y") for e in exp]


def nearest_future(underlying: str) -> dict | None:
    """The actively traded future: nearest expiry at least FUT_ROLL_DAYS away."""
    df = _load()
    if df is None:
        return None
    cutoff = (date.today() + timedelta(days=FUT_ROLL_DAYS)).isoformat()
    m = df[(df.underlying_symbol == underlying) & (df.instrument_type == "FUT") & (df.expiry_date >= cutoff)]
    if m.empty:
        return None
    r = m.sort_values("expiry_date").iloc[0]
    return {"exchange": r.exchange, "exchange_token": str(r.exchange_token), "trading_symbol": r.trading_symbol,
            "groww_symbol": r.groww_symbol, "expiry": r.expiry_date, "lot_size": int(float(r.lot_size))}
