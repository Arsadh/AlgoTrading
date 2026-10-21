"""Live market data.

Candles: Groww (if credentials are set) else Yahoo Finance 1-minute bars.
  MCX without Groww: real MCX futures candles from Moneycontrol (see mcx.py). If that
  fails, a proxy built from the global USD future (WTI, Henry Hub gas, COMEX gold /
  silver / copper) x USD/INR x unit conversion (and import duty for bullion), limited
  to MCX trading hours: it tracks direction but is not the MCX price.
  The Bullion Index has no free source: Groww only.
Option chain: NSE's public option chain (has change-in-OI) else Groww.
  MCX option chain: Groww, else Moneycontrol (one quote per strike near the money;
  MCX's own website blocks automated access).
Everything is normalised to plain pandas / dicts so the analysis code does not
care where data came from.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import httpx
import pandas as pd

from ..config import IST, settings
from ..universe import STOCKS, yahoo
from .instruments import VIX_YAHOO, Instrument

log = logging.getLogger(__name__)
_COLS = ["Open", "High", "Low", "Close", "Volume"]


# ------------------------------------------------------------------ Groww
class _Groww:
    def __init__(self):
        from growwapi import GrowwAPI
        self.G = GrowwAPI
        self.api = None
        self.at: datetime | None = None

    def session(self):
        now = datetime.now(IST)
        six = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if self.api is None or (self.at and now >= six > self.at):  # tokens expire 6:00 AM IST
            if settings.groww_totp_token:
                import pyotp
                tok = self.G.get_access_token(api_key=settings.groww_totp_token,
                                              totp=pyotp.TOTP(settings.groww_totp_secret).now())
            else:
                tok = self.G.get_access_token(api_key=settings.groww_api_key, secret=settings.groww_api_secret)
            self.api, self.at = self.G(tok), now
        return self.api


_groww: _Groww | None = None
_groww_failed = False


def groww():
    global _groww, _groww_failed
    if not settings.has_groww_creds or _groww_failed:
        return None
    try:
        if _groww is None:
            _groww = _Groww()
        return _groww.session()
    except Exception as e:
        log.error("Groww login failed, falling back to free data: %s", e)
        _groww_failed = True
        return None


# ---------------------------------------------------------------- candles
_yc = httpx.Client(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}, timeout=15)


_ycache: dict[tuple, tuple[float, pd.DataFrame]] = {}
_ylock = threading.Lock()


def _yahoo_intraday(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Cached a few seconds: several MCX contracts share one proxy (and all share USD/INR)."""
    k = (ticker, period, interval)
    with _ylock:
        hit = _ycache.get(k)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    df = _yahoo_fetch(ticker, period, interval)
    with _ylock:
        _ycache[k] = (time.time(), df)
    return df


def _yahoo_fetch(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Yahoo chart API directly (~0.2 s, vs ~10 s through yfinance)."""
    r = _yc.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params={"range": period, "interval": interval})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    if not ts:
        return pd.DataFrame(columns=_COLS)
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"],
                       "Volume": q.get("volume") or [0] * len(ts)},
                      index=pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST))
    df = df.dropna(subset=["Close"]).astype(float)
    # Yahoo stamps the forming bar with the current second; align it to its bar start
    df.index = df.index.floor({"1m": "1min", "5m": "5min", "15m": "15min"}.get(interval, "1min"))
    return df.groupby(level=0).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})


def _groww_intraday(inst: Instrument, days: int) -> pd.DataFrame | None:
    api = groww()
    if api is None:
        return None
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    if inst.exchange == "MCX":
        from . import contracts
        fut = contracts.nearest_future(inst.key)
        if not fut:
            return None
        exch, seg, sym = api.EXCHANGE_MCX, api.SEGMENT_COMMODITY, fut["groww_symbol"]
    else:
        exch, seg, sym = api.EXCHANGE_NSE, api.SEGMENT_CASH, inst.groww
    try:
        r = api.get_historical_candles(exchange=exch, segment=seg, groww_symbol=sym,
                                       start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                                       end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
                                       candle_interval=api.CANDLE_INTERVAL_MIN_1, timeout=10)
        rows = r.get("candles") or []
        if not rows:
            return None
        df = pd.DataFrame([x[:6] for x in rows], columns=["ts"] + _COLS)
        df.index = pd.to_datetime(df.pop("ts")).dt.tz_localize(IST)
        return df.astype(float)
    except Exception as e:
        log.warning("Groww candles failed for %s: %s", inst.key, e)
        return None


def session_only(df: pd.DataFrame, inst: Instrument) -> pd.DataFrame:
    """Keep bars inside the instrument's trading session (weekdays)."""
    if df.empty:
        return df
    h = inst.hours()
    t = df.index.time
    return df[(df.index.weekday < 5) & (t >= h["open"]) & (t <= h["close"])]


def usd_proxy(inst: Instrument, period: str, interval: str) -> pd.DataFrame:
    """MCX-style INR price from a USD market (e.g. WTI x USD/INR), MCX hours only."""
    px = _yahoo_intraday(inst.yahoo, period, interval)
    fx = _yahoo_intraday(inst.fx, period, interval)["Close"]
    if px.empty or fx.empty:
        return px
    fx = fx.reindex(px.index.union(fx.index)).ffill().reindex(px.index).bfill()
    out = px[["Open", "High", "Low", "Close"]].mul(fx * inst.proxy_mult, axis=0)
    out["Volume"] = px["Volume"]
    return session_only(out.dropna(), inst)


def intraday_1m(inst: Instrument, days: int = 5) -> tuple[pd.DataFrame, str]:
    df = _groww_intraday(inst, days)
    if df is not None and len(df):
        return (session_only(df, inst) if inst.exchange == "MCX" else df), "groww"
    if inst.needs_groww:
        raise RuntimeError(f"{inst.name} has no free price source: add Groww API keys")
    if inst.moneycontrol:
        try:
            from . import mcx
            return session_only(mcx.candles_1m(inst.key, days), inst), "mcx"
        except Exception as e:
            if not inst.fx:
                raise
            log.warning("MCX candles for %s unavailable, using the proxy: %s", inst.key, e)
    if inst.fx:
        return usd_proxy(inst, f"{days}d", "1m"), f"proxy: {inst.proxy_name} x USD/INR"
    return _yahoo_intraday(inst.yahoo, f"{days}d", "1m"), "yahoo"


def history_5m(inst: Instrument) -> tuple[pd.DataFrame, str]:
    """~60 trading days of 5-minute bars for backtesting, and where they came from."""
    if inst.needs_groww:
        raise RuntimeError(f"no free 5-min history for {inst.name}")
    if inst.moneycontrol:
        try:
            from . import mcx
            df = session_only(mcx.candles_5m(inst.key), inst)
            if len(df):
                return df, "MCX nearest future, 5-min (Moneycontrol)"
        except Exception as e:
            if not inst.fx:
                raise
            log.warning("MCX 5-min history for %s unavailable, using the proxy: %s", inst.key, e)
    if inst.fx:
        return usd_proxy(inst, "60d", "5m"), f"{inst.proxy_name} x USD/INR proxy, MCX hours"
    return _yahoo_intraday(inst.yahoo, "60d", "5m"), "Yahoo 5-min"


def vix_1m() -> pd.DataFrame:
    return _yahoo_intraday(VIX_YAHOO, "5d", "1m")


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1-min -> N-min bars on a grid aligned to 09:15 (which also covers MCX's 09:00 open)."""
    if df.empty:
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    out = df.resample(f"{minutes}min", origin="start_day", offset="15min").agg(agg)
    return out.dropna(subset=["Close"])


# ------------------------------------------------------------ option chain
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


class _NSE:
    def __init__(self):
        self.client = httpx.Client(headers=_NSE_HEADERS, timeout=15, follow_redirects=True)
        self.primed = 0.0
        self.lock = threading.Lock()

    def get(self, url: str) -> dict:
        with self.lock:
            if time.time() - self.primed > 600:  # refresh cookies every 10 min
                self.client.get("https://www.nseindia.com/option-chain")
                self.primed = time.time()
            r = self.client.get(url)
            if r.status_code in (401, 403):
                self.primed = 0
                self.client.get("https://www.nseindia.com/option-chain")
                r = self.client.get(url)
            r.raise_for_status()
            return r.json()


_nse = _NSE()


def _parse_expiry(s: str) -> datetime:
    return datetime.strptime(s, "%d-%b-%Y")


def expiries(inst: Instrument) -> list[str]:
    """Upcoming expiries as 'DD-Mon-YYYY', nearest first."""
    if inst.exchange == "MCX":
        from . import contracts
        return contracts.option_expiries(inst.key)
    d = _nse.get(f"https://www.nseindia.com/api/option-chain-contract-info?symbol={inst.key}")
    today = datetime.now(IST).date()
    exp = [e for e in d.get("expiryDates", []) if _parse_expiry(e).date() >= today]
    return sorted(exp, key=_parse_expiry)


def _nse_chain(inst: Instrument, expiry: str) -> dict:
    d = _nse.get(f"https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol={inst.key}&expiry={expiry}")
    rec = d["records"]
    rows = []
    for x in rec["data"]:
        def side(o):
            if not o:
                return None
            return {"oi": o.get("openInterest"), "chg_oi": o.get("changeinOpenInterest"), "ltp": o.get("lastPrice"),
                    "iv": o.get("impliedVolatility"), "volume": o.get("totalTradedVolume"),
                    "bid": o.get("buyPrice1"), "ask": o.get("sellPrice1"), "delta": None}
        rows.append({"strike": float(x["strikePrice"]), "ce": side(x.get("CE")), "pe": side(x.get("PE"))})
    return {"source": "nse", "timestamp": rec.get("timestamp"), "spot": rec.get("underlyingValue"),
            "expiry": expiry, "rows": sorted(rows, key=lambda r: r["strike"])}


def _groww_chain(inst: Instrument, expiry: str) -> dict | None:
    api = groww()
    if api is None:
        return None
    iso = _parse_expiry(expiry).strftime("%Y-%m-%d")
    exch = api.EXCHANGE_MCX if inst.exchange == "MCX" else api.EXCHANGE_NSE
    r = api.get_option_chain(exchange=exch, underlying=inst.key, expiry_date=iso)
    rows = []
    for k, v in (r.get("strikes") or {}).items():
        def side(o):
            if not o:
                return None
            g = o.get("greeks") or {}
            return {"oi": o.get("open_interest"), "chg_oi": None, "ltp": o.get("ltp"), "iv": g.get("iv"),
                    "volume": o.get("volume"), "bid": None, "ask": None, "delta": g.get("delta")}
        rows.append({"strike": float(k), "ce": side(v.get("CE")), "pe": side(v.get("PE"))})
    return {"source": "groww", "timestamp": datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S"),
            "spot": r.get("underlying_ltp"), "expiry": expiry, "rows": sorted(rows, key=lambda r: r["strike"])}


class ChainUnavailable(RuntimeError):
    pass


def option_chain(inst: Instrument, expiry: str | None = None, spot: float | None = None) -> dict:
    exp_list = expiries(inst)
    if not exp_list:
        raise ChainUnavailable("no upcoming expiries found")
    expiry = expiry if expiry in exp_list else exp_list[0]
    if inst.exchange == "MCX":
        ch = _groww_chain(inst, expiry)
        if ch is None:
            if not inst.moneycontrol:
                raise ChainUnavailable(f"{inst.name} option chain needs Groww API keys (no free source)")
            from . import mcx
            ch = mcx.option_chain(inst.key, expiry, spot, inst.strike_step, settings.mcx_chain_width)
        ch["expiries"] = exp_list[:6]
        return ch
    try:
        ch = _nse_chain(inst, expiry)
    except Exception as e:
        log.warning("NSE chain failed for %s: %s", inst.key, e)
        ch = _groww_chain(inst, expiry)
        if ch is None:
            raise
    ch["expiries"] = exp_list[:8]
    return ch


# ----------------------------------------------------------------- breadth
def breadth() -> list[dict]:
    """Day change for the stock universe (one batched Yahoo request).

    Uses 15-min bars: Yahoo's daily bars for NSE stocks often lack the current
    session, which would compare the wrong two days."""
    import yfinance as yf
    tick = [yahoo(s) for s in STOCKS]
    df = yf.download(tick, period="5d", interval="15m", progress=False, auto_adjust=False, threads=True)
    close = df["Close"]
    out = []
    for s in STOCKS:
        if yahoo(s) not in close:
            continue
        c = close[yahoo(s)].dropna()
        if c.empty:
            continue
        days = c.index.date
        prev = c[days < days[-1]]
        if len(prev):
            last = float(c.iloc[-1])
            out.append({"symbol": s, "price": round(last, 2), "change": last / float(prev.iloc[-1]) - 1})
    return sorted(out, key=lambda r: r["change"], reverse=True)
