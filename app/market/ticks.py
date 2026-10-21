"""Live tick engine: polls (or streams) prices every TICK_SECONDS in a background thread.

Sources, best first:
  - Groww live feed (GrowwFeed websocket): real exchange ticks for NSE indices, the
    nearest-month MCX future of every commodity (crude, natural gas, gold, silver,
    copper, zinc, bullion index) and, when a strike is selected, the ATM call and put.
    Needs Groww API keys.
  - Moneycontrol (no keys): real MCX futures quotes for every commodity every
    MCX_TICK_SECONDS (one request each, in parallel), plus the ATM option premiums.
  - Yahoo spark endpoint: one request for NSE indices and context every second. Yahoo itself
    only refreshes values every ~10-30 s, so prices step rather than stream. If the real MCX
    feed fails, MCX prices fall back to a proxy: the global USD future x USD/INR x unit conversion.
Each tick carries the exchange timestamp, so freshness is shown honestly and candles
are built on exchange time (not the PC clock).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import httpx

from ..config import IST, settings
from .instruments import INSTRUMENTS, active

log = logging.getLogger(__name__)
YAHOO_SPARK = "https://query1.finance.yahoo.com/v7/finance/spark"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
# Plain Yahoo quotes: NSE indices + context series (VIX, USD/INR, and each MCX proxy in USD as "X:<ticker>")
YAHOO = ({k: i.yahoo for k, i in INSTRUMENTS.items() if i.yahoo and not i.fx} | {"VIX": "^INDIAVIX", "USDINR": "INR=X"}
         | {f"X:{i.yahoo}": i.yahoo for i in INSTRUMENTS.values() if i.fx})
GROWW_INDEX = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "VIX": "INDIAVIX"}
CONTEXT_EVERY = 5  # seconds between Yahoo context polls while Groww streams


class TickEngine:
    def __init__(self, on_tick=None):
        self.prices: dict[str, dict] = {}      # key -> {price, ts (epoch s, exchange), prev_close, source, recv}
        self.options: dict[str, dict] = {}     # "NIFTY:22700:CE" -> {price, ts, token}
        self.bars: dict[str, dict] = {}        # key -> {minute: [o, h, l, c]} built from ticks
        self.on_tick = on_tick                 # callback(key, price, ts) for instant stop/target checks
        self.source = "starting"
        self.error: str | None = None
        self._opt_subs: dict[str, dict] = {}   # wanted option subscriptions: name -> contract dict
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(headers=_UA, timeout=5)
        self._feed = None
        self._feed_ltp: set[tuple[str, str, str]] = set()   # (exchange, segment, token) subscribed
        self._mcx_futs: dict[str, str] = {}                 # token -> instrument key
        self._last_context = 0.0
        self._real_at: dict[str, float] = {}               # key -> last real exchange tick (groww / mcx)
        self._last_mcx = self._last_mcx_opts = 0.0
        self._mcx_source = ""

    # ---------------------------------------------------------------- api
    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._run, daemon=True, name="ticks")
            self._thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            return {"source": self.source, "error": self.error, "prices": dict(self.prices), "options": dict(self.options)}

    def want_options(self, wanted: dict[str, dict]):
        """{name: contract} option contracts to stream (Groww; MCX ones are polled from Moneycontrol without it)."""
        with self._lock:
            self._opt_subs = wanted

    def bars_for(self, key: str) -> dict:
        with self._lock:
            return dict(self.bars.get(key, {}))

    # -------------------------------------------------------------- loop
    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                if settings.has_groww_creds and self._groww_tick():
                    self.source = "groww live feed"
                    if t0 - self._last_context > CONTEXT_EVERY:
                        self._yahoo_tick(context_only=True)
                        self._last_context = t0
                else:
                    self._yahoo_tick()
                    self.source = "yahoo (~10-30 s exchange snapshots)"
                self.error = None
            except Exception as e:
                self.error = str(e)
                log.debug("tick failed: %s", e)
            try:
                self._mcx_tick()
            except Exception as e:
                self._mcx_source = ""
                log.debug("MCX tick failed: %s", e)
            self.source = self.source.split("; MCX:")[0] + (f"; MCX: {self._mcx_source}" if self._mcx_source else "")
            time.sleep(max(0.05, settings.tick_seconds - (time.time() - t0)))

    def _record(self, key: str, price: float, ts: float, prev_close: float | None, source: str):
        if not price:
            return
        with self._lock:
            old = self.prices.get(key)
            self.prices[key] = {"price": float(price), "ts": float(ts), "source": source, "recv": time.time(),
                                "prev_close": prev_close if prev_close else (old or {}).get("prev_close")}
            minute = int(ts // 60 * 60)
            bars = self.bars.setdefault(key, {})
            b = bars.get(minute)
            if b is None:
                bars[minute] = [price, price, price, price]
                for m in [m for m in bars if m < minute - 3 * 3600]:
                    del bars[m]  # keep ~3 hours
            else:
                b[1], b[2], b[3] = max(b[1], price), min(b[2], price), price
            changed = not old or old["price"] != price
        if changed and self.on_tick and key in INSTRUMENTS:
            try:
                self.on_tick(key, float(price), float(ts))
            except Exception as e:
                log.warning("on_tick failed: %s", e)

    # ------------------------------------------------------------- yahoo
    def _yahoo_tick(self, context_only: bool = False):
        r = self._client.get(YAHOO_SPARK, params={"symbols": ",".join(YAHOO.values()), "range": "1d", "interval": "1m"})
        r.raise_for_status()
        meta = {res["symbol"]: (res.get("response") or [{}])[0].get("meta", {}) for res in r.json()["spark"]["result"]}
        q = {}
        for key, sym in YAHOO.items():
            m = meta.get(sym)
            if m and m.get("regularMarketPrice"):
                q[key] = (m["regularMarketPrice"], m.get("regularMarketTime", time.time()),
                          m.get("previousClose") or m.get("chartPreviousClose"))
        for key, (p, ts, pc) in q.items():
            if context_only and key in INSTRUMENTS:
                continue
            self._record(key, p, ts, pc, "yahoo")
        # MCX proxy = USD future x USD/INR x unit conversion, unless Groww is already streaming the real future
        if "USDINR" in q:
            fp, _, fpc = q["USDINR"]
            for key, inst in INSTRUMENTS.items():
                x = q.get(f"X:{inst.yahoo}")
                if inst.fx and x and time.time() - self._real_at.get(key, 0) > 120:  # real MCX ticks win
                    (wp, wts, wpc), k = x, inst.proxy_mult
                    self._record(key, wp * fp * k, wts, (wpc * fpc * k) if wpc and fpc else None, "proxy")

    # ------------------------------------------------------------- groww
    def _groww_tick(self) -> bool:
        from . import contracts
        from .feed import groww
        api = groww()
        if api is None:
            return False
        if self._feed is None:
            from growwapi import GrowwFeed
            self._feed = GrowwFeed(api)
            self._feed.subscribe_index_value([{"exchange": "NSE", "segment": "CASH", "exchange_token": t}
                                              for t in GROWW_INDEX.values()])
        if not self._mcx_futs:
            for key, inst in active().items():
                if inst.exchange == "MCX":
                    fut = contracts.nearest_future(key)
                    if fut:
                        self._mcx_futs[fut["exchange_token"]] = key
        with self._lock:
            wanted = dict(self._opt_subs)
        want = {("MCX", "COMMODITY", t) for t in self._mcx_futs}
        want |= {(c.get("exchange", "NSE"), "COMMODITY" if c.get("exchange") == "MCX" else "FNO", str(c["exchange_token"]))
                 for c in wanted.values()}
        if want != self._feed_ltp:
            drop = [{"exchange": e, "segment": s, "exchange_token": t} for e, s, t in self._feed_ltp - want]
            add = [{"exchange": e, "segment": s, "exchange_token": t} for e, s, t in want - self._feed_ltp]
            if drop:
                self._feed.unsubscribe_ltp(drop)
            if add:
                self._feed.subscribe_ltp(add)
            self._feed_ltp = want

        got = False
        idx = (self._feed.get_index_value() or {}).get("NSE", {}).get("CASH", {})
        for key, tok in GROWW_INDEX.items():
            v = idx.get(tok)
            if v and v.get("value"):
                self._record(key, v["value"], v.get("tsInMillis", time.time() * 1000) / 1000, None, "groww")
                got = True
        ltp = (self._feed.get_ltp() or {}).get("ltp") or {}
        mcx = ltp.get("MCX", {}).get("COMMODITY", {})
        for tok, key in self._mcx_futs.items():
            v = mcx.get(tok)
            if v and v.get("ltp"):
                self._record(key, v["ltp"], v.get("tsInMillis", time.time() * 1000) / 1000, None, "groww")
                self._real_at[key] = time.time()
                got = True
        with self._lock:
            for name, c in wanted.items():
                seg = "COMMODITY" if c.get("exchange") == "MCX" else "FNO"
                v = ltp.get(c.get("exchange", "NSE"), {}).get(seg, {}).get(str(c["exchange_token"]))
                if v and v.get("ltp"):
                    self.options[name] = {"price": float(v["ltp"]), "ts": v.get("tsInMillis", 0) / 1000,
                                          "token": c["exchange_token"]}
        return got


    # ------------------------------------------------------- moneycontrol
    def _mcx_tick(self):
        """Real MCX futures (and wanted ATM options) from Moneycontrol, unless Groww already streams them."""
        from . import mcx
        now = time.time()
        mcx_open = any(i.is_open() for i in INSTRUMENTS.values() if i.exchange == "MCX")
        if now - self._last_mcx < (settings.mcx_tick_seconds if mcx_open else 60):
            return
        self._last_mcx = now
        streamed = set(self._mcx_futs.values()) if self._feed is not None else set()
        keys = [k for k, i in active().items() if i.exchange == "MCX" and i.moneycontrol and k not in streamed]
        if not keys:
            self._mcx_source = ""
            return
        quotes = mcx.future_quotes(keys)
        for key, qt in quotes.items():
            self._record(key, qt["price"], qt["ts"], qt["prev_close"], "mcx")
            self._real_at[key] = time.time()
            with self._lock:
                self.prices[key].update(oi=qt["oi"], chg_oi=qt["chg_oi"], oi_buildup=qt["oi_buildup"])
        self._mcx_source = f"real prices via Moneycontrol (every {settings.mcx_tick_seconds:g} s)" if quotes else "Moneycontrol unavailable"
        if now - self._last_mcx_opts >= max(10, 3 * settings.mcx_tick_seconds) and self._feed is None:
            self._last_mcx_opts = now
            with self._lock:
                wanted = {n: c for n, c in self._opt_subs.items() if c.get("exchange") == "MCX"}
            for name, qt in mcx.option_quotes(wanted).items():
                with self._lock:
                    self.options[name] = {"price": qt["price"], "ts": qt["ts"], "token": wanted[name].get("exchange_token")}


def exchange_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")
