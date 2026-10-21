"""Real MCX prices without Groww keys, from Moneycontrol's public price API.

MCX's own website blocks automated access, so this uses the endpoints behind
moneycontrol.com's commodity pages (no login, no keys):
  - futures quote: last price, previous close, OI, bid/ask, updated every few seconds
  - option quote, one strike at a time: last price, OI, change in OI, bid/ask, volume
  - candles: 1-minute bars for ~7 days, 5-minute bars for ~90 days, per contract

It is an unofficial endpoint: it can change or rate-limit without notice, so the
app falls back to the Yahoo proxy when it fails. Keep polling rates modest
(MCX_TICK_SECONDS, MCX_CHAIN_REFRESH_SECONDS in .env).

Not covered by Moneycontrol: the MCX Bullion Index.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import pandas as pd

from ..config import IST
from . import contracts

log = logging.getLogger(__name__)
QUOTE = "https://priceapi.moneycontrol.com/pricefeed/mcx"
HISTORY = "https://priceapi.moneycontrol.com/techCharts/commodity/history"
_client = httpx.Client(headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Referer": "https://www.moneycontrol.com/"}, timeout=10)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="mcx")
_COLS = ["Open", "High", "Low", "Close", "Volume"]
_paused_until = 0.0  # back off for a while if Moneycontrol starts refusing us


class Unavailable(RuntimeError):
    pass


def _get(url: str, params: dict | None = None) -> dict:
    global _paused_until
    if time.time() < _paused_until:
        raise Unavailable("Moneycontrol paused after refusing requests; retrying shortly")
    r = _client.get(url, params=params)
    if r.status_code in (403, 429):
        _paused_until = time.time() + 60
        raise Unavailable(f"Moneycontrol refused the request ({r.status_code}); pausing 60 s")
    r.raise_for_status()
    return r.json()


def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def future(underlying: str) -> dict | None:
    """The actively traded future (from the public contract list): its expiry and lot size."""
    return contracts.nearest_future(underlying)


# ------------------------------------------------------------------ quotes
def _quote(path: str, params: dict) -> dict | None:
    d = _get(f"{QUOTE}/{path}", params)
    data = d.get("data")
    if str(d.get("code")) != "200" or not data or _f(data.get("lastPrice")) is None:
        return None
    return {"price": _f(data["lastPrice"]), "prev_close": _f(data.get("prevClose")),
            "ts": (_f(data.get("lastupdEpoch")) or time.time() * 1000) / 1000,
            "oi": _f(data.get("openInt")), "chg_oi": _f(data.get("openIntChg")), "volume": _f(data.get("tradedVol")),
            "bid": _f(data.get("bidPrice")), "ask": _f(data.get("askPrice")), "oi_buildup": data.get("oiBuildup")}


def future_quote(underlying: str, expiry_iso: str | None = None) -> dict | None:
    if expiry_iso is None:
        fut = future(underlying)
        if not fut:
            return None
        expiry_iso = fut["expiry"]
    return _quote(f"commodityfutures/{underlying}", {"expiry": expiry_iso})


def option_quote(underlying: str, expiry_iso: str, opt_type: str, strike: float) -> dict | None:
    return _quote(f"commodityoptions/{underlying}",
                  {"expiry": expiry_iso, "optionType": opt_type, "strikePrice": f"{float(strike):.2f}"})


def future_quotes(underlyings: list[str]) -> dict[str, dict]:
    """Nearest-future quotes for several commodities in parallel."""
    def one(u):
        try:
            return u, future_quote(u)
        except Exception as e:
            log.debug("MCX quote %s failed: %s", u, e)
            return u, None
    return {u: q for u, q in _pool.map(one, underlyings) if q}


def option_quotes(wanted: dict[str, dict]) -> dict[str, dict]:
    """{name: contract (underlying, expiry, strike, type)} -> {name: quote}, in parallel."""
    def one(item):
        name, c = item
        try:
            return name, option_quote(c["underlying"], c["expiry"], c["type"], c["strike"])
        except Exception as e:
            log.debug("MCX option quote %s failed: %s", name, e)
            return name, None
    return {n: q for n, q in _pool.map(one, wanted.items()) if q}


# ----------------------------------------------------------------- candles
def _history(symbol: str, resolution: int, start: int, end: int) -> pd.DataFrame:
    d = _get(HISTORY, {"symbol": symbol, "resolution": resolution, "from": start, "to": end,
                       "countback": 100000, "currencyCode": "INR"})
    if d.get("s") != "ok" or not d.get("t"):
        return pd.DataFrame(columns=_COLS)
    df = pd.DataFrame({"Open": d["o"], "High": d["h"], "Low": d["l"], "Close": d["c"],
                       "Volume": d.get("v") or [0.0] * len(d["t"])},
                      index=pd.to_datetime(d["t"], unit="s", utc=True).tz_convert(IST))
    df = df.astype(float).dropna(subset=["Open", "High", "Low", "Close"])  # some old bars lack O/H/L
    return df[(df["High"] >= df["Low"]) & (df["Close"] > 0)]


_bars: dict[str, tuple[float, pd.DataFrame]] = {}   # symbol -> (time of last full download, 1-min bars)
_bars_lock = threading.Lock()


def candles_1m(underlying: str, days: int = 5) -> pd.DataFrame:
    """1-minute bars of the nearest future. Full download every 10 minutes, otherwise only the last half hour."""
    fut = future(underlying)
    if not fut:
        raise Unavailable(f"no MCX future found for {underlying}")
    sym = f"{underlying}_{fut['expiry']}_MCX"
    now = int(time.time())
    with _bars_lock:
        full_at, df = _bars.get(sym, (0.0, None))
    if df is None or now - full_at > 600:
        df = _history(sym, 1, now - days * 86400, now)
        full_at = now
    else:
        new = _history(sym, 1, now - 1800, now)
        df = pd.concat([df[df.index < new.index[0]], new]) if len(new) else df
    if df.empty:
        raise Unavailable(f"no MCX candles for {sym}")
    df = df[df.index >= df.index[-1] - pd.Timedelta(days=days + 3)]
    with _bars_lock:
        _bars[sym] = (full_at, df)
    return df.copy()


def candles_5m(underlying: str, days: int = 60) -> pd.DataFrame:
    """~60 days of 5-minute bars of today's nearest future.
    Weeks when it was still a far month trade thinly: days where most bars have no range are dropped."""
    fut = future(underlying)
    if not fut:
        raise Unavailable(f"no MCX future found for {underlying}")
    now = int(time.time())
    df = _history(f"{underlying}_{fut['expiry']}_MCX", 5, now - days * 86400, now)
    if df.empty:
        return df
    flat = (df["High"] == df["Low"]).groupby(df.index.date).transform("mean")
    return df[(flat < 0.5).to_numpy()]


# ------------------------------------------------------------- option chain
def option_chain(underlying: str, expiry: str, spot: float | None, step: float, width: int = 6) -> dict:
    """Chain for the strikes within `width` steps of the money, built from one quote per strike and side.
    IV and delta are implied from each option's last price (Black-76 on the future, rate 0)."""
    from ..signals.options import bs_delta, implied_vol, years_to_expiry
    iso = datetime.strptime(expiry, "%d-%b-%Y").strftime("%Y-%m-%d")
    fq = future_quote(underlying)
    spot = (fq or {}).get("price") or spot
    if not spot:
        raise Unavailable(f"no MCX price for {underlying}")
    listed = contracts.strikes(underlying, iso)
    atm = min(listed, key=lambda s: abs(s - spot)) if listed else round(spot / step) * step
    strikes = [s for s in listed if abs(s - atm) <= width * step + 1e-9] if listed else \
        [atm + i * step for i in range(-width, width + 1)]
    wanted = {f"{s}:{t}": {"underlying": underlying, "expiry": iso, "type": t, "strike": s}
              for s in strikes for t in ("CE", "PE")}
    q = option_quotes(wanted)
    if not q:
        raise Unavailable(f"no MCX option quotes for {underlying} {expiry}")
    yrs = years_to_expiry(expiry, "23:30")
    rows = []
    for s in strikes:
        row = {"strike": float(s)}
        for t in ("CE", "PE"):
            o = q.get(f"{s}:{t}")
            if not o:
                row[t.lower()] = None
                continue
            iv = implied_vol(o["price"], spot, s, yrs, t == "CE", r=0.0)
            row[t.lower()] = {"oi": o["oi"], "chg_oi": o["chg_oi"], "ltp": o["price"], "iv": round(iv, 2) if iv else None,
                              "volume": o["volume"], "bid": o["bid"], "ask": o["ask"],
                              "delta": round(bs_delta(spot, s, yrs, iv, t == "CE", r=0.0), 3) if iv else None}
        rows.append(row)
    ts = max(o["ts"] for o in q.values())
    return {"source": "mcx", "timestamp": datetime.fromtimestamp(ts, IST).strftime("%d-%b-%Y %H:%M:%S"),
            "spot": spot, "expiry": expiry, "rows": rows}
