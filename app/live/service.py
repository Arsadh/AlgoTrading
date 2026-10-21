"""Background loop: refresh data, recompute analysis, push updates to browsers.

While any market is open (NSE 09:15-15:30, MCX 09:00-23:30/23:55): candles for every
instrument (NSE indices and all MCX commodities with options) every
REFRESH_SECONDS, option chain every CHAIN_REFRESH_SECONDS, news every minute, market
breadth every 2 minutes. Otherwise everything refreshes every 5 minutes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime

from .. import db, predictions, publish, snapshot
from ..config import IST, settings
from ..market import contracts, feed
from ..market.instruments import INSTRUMENTS, active, any_open
from ..market.ticks import TickEngine
from ..news import service as news
from ..quant import ensemble, moves
from ..quant import research as quant
from ..signals import backtest, live, tracker

log = logging.getLogger(__name__)


def market_open(now: datetime | None = None) -> bool:
    """NSE cash/F&O session (Mon-Fri 09:15-15:30 IST). Exchange holidays are not modelled."""
    return INSTRUMENTS["NIFTY"].is_open(now)


class LiveService:
    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()
        self.candles: dict = {}        # key -> (df1m, source)
        self.chains: dict = {}         # key -> chain dict
        self.chain_at: dict = {}       # key -> epoch seconds
        self.analysis: dict = {}       # key -> latest analysis
        self.active: dict = {}         # key -> open signal row
        self.vix = None
        self.mood = news.mood()
        self.moods: dict = {}
        self.breadth: list[dict] = []
        self.backtests: dict = {}      # key -> (epoch, result)
        self.errors: dict = {}
        self.daily: dict = {}          # key -> {"bias_series", "today", "setup", "day"}
        self.quant_running = False
        self.ticks = TickEngine(on_tick=self._on_tick)
        self.recorder = predictions.Recorder(on_written=publish.publish)
        self._loop_ref: asyncio.AbstractEventLoop | None = None
        self._t = {"news": 0.0, "breadth": 0.0}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ pub/sub
    def publish(self, kind: str, data: dict):
        msg = {"type": kind, "ts": db.now_ist(), **data}
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # --------------------------------------------------------------- loop
    def start(self):
        if not self._task:
            self._loop_ref = asyncio.get_running_loop()
            self.ticks.start()
            import threading
            threading.Thread(target=contracts.lot_size, args=("NIFTY",), daemon=True).start()  # warm the 20 MB master
            self._task = asyncio.create_task(self._loop())
            asyncio.create_task(self._tick_pusher())
            rep = quant.load()
            from ..quant import data as qdata
            if rep is None or not set(qdata.MARKETS) <= {c["key"] for c in rep.get("coverage", [])}:
                self.run_quant()  # first start, or markets were added since the last study

    def run_quant(self):
        """Multi-decade research in a background thread (first run ~2 min)."""
        import threading
        if self.quant_running:
            return

        def job():
            self.quant_running = True
            try:
                quant.run()
                self.daily.clear()
                self.backtests.clear()
                self.errors.pop("quant", None)
            except Exception as e:
                log.exception("quant research failed")
                self.errors["quant"] = f"quant research: {e}"
            finally:
                self.quant_running = False
        threading.Thread(target=job, daemon=True).start()

    def daily_for(self, key: str) -> dict | None:
        """Daily composite bias (as of the previous close) + today's historical setup, cached per day.
        MCX commodities use the global future's history (same underlying, much longer record).
        None for instruments without a long history (zinc, the bullion index)."""
        today = datetime.now(IST).date()
        key = INSTRUMENTS[key].quant_market if key in INSTRUMENTS else key
        if not key:
            return None
        d = self.daily.get(key)
        if d and d["day"] == today:
            return d
        rep = quant.load()
        if rep is None:
            return None
        try:
            members = [m["key"] for m in rep["ensemble"]["members"]]
            series, now = ensemble.daily_bias(key, members)
            # During market hours the last daily bar may be today's partial session: use the prior close.
            if now["date"] == today.isoformat():
                series = series.iloc[:-1]
                now = {**now, "date": series.index[-1].strftime("%Y-%m-%d"), "bias": round(float(series.iloc[-1]), 3)}
            from ..quant import data as qdata
            close = qdata.load(key)["Close"]
            rv20 = float(close.pct_change(fill_method=None).iloc[-21:-1].std() * (252 ** 0.5) * 100)
            d = {"day": today, "bias_series": series, "today": now, "setup": moves.today(key), "rv20": rv20}
            self.daily[key] = d
            self.errors.pop(f"daily:{key}", None)
            return d
        except Exception as e:
            self.errors[f"daily:{key}"] = f"{key} daily algorithm: {e}"
            return None

    # ------------------------------------------------------------- ticks
    def _on_tick(self, key: str, price: float, ts: float):
        """Runs in the tick thread: instant stop / target alerts on open signals."""
        if not INSTRUMENTS[key].is_open():
            return
        ev = tracker.check_price(key, price, datetime.fromtimestamp(ts, IST).isoformat(timespec="seconds"))
        if ev and self._loop_ref:
            name = INSTRUMENTS[key].name
            s = ev["signal"]
            msg = {"target1": "Target 1 hit, move stop to entry", "target2": "Target 2 hit, exit",
                   "stop": "Stop-loss hit, exit", "breakeven": "Back to entry after T1, exit at cost"}[ev["event"]]
            db.log(f"{name} {s['opt_type']} {s['strike']:.0f}: {msg} (spot {price:.2f})")
            self._loop_ref.call_soon_threadsafe(self.publish, "signal_update",
                                                {"event": ev["event"], "message": msg, "name": name, "signal": s, "spot": price})

    def tick_payload(self) -> dict:
        snap = self.ticks.snapshot()
        now = time.time()
        prices = {}
        for k, p in snap["prices"].items():
            pc = p.get("prev_close")
            prices[k] = {"price": p["price"], "ts": p["ts"], "age": round(now - p["ts"], 1),
                         "change": round(p["price"] - pc, 2) if pc else None,
                         "change_pct": round((p["price"] / pc - 1) * 100, 2) if pc else None}
        lag = [p["recv"] - p["ts"] for p in snap["prices"].values() if "recv" in p]
        behind = -min(lag) if lag and min(lag) < -3 else 0  # exchange time ahead of PC clock
        return {"source": snap["source"], "error": snap["error"], "prices": prices, "options": snap["options"],
                "market_open": market_open(), "open": {k: i.is_open() for k, i in INSTRUMENTS.items()},
                "clock_behind_s": round(behind)}

    async def _tick_pusher(self):
        while True:
            await asyncio.sleep(settings.tick_seconds)
            if self.subscribers:
                self.publish("tick", self.tick_payload())

    def _merge_ticks(self, key: str, df1m):
        """Overlay tick-built minute bars on top of the downloaded 1-min candles (exchange time)."""
        import pandas as pd
        bars = self.ticks.bars_for(key)
        if not bars:
            return df1m
        tb = pd.DataFrame([[v[0], v[1], v[2], v[3], 0.0] for v in bars.values()],
                          columns=["Open", "High", "Low", "Close", "Volume"],
                          index=pd.to_datetime(list(bars.keys()), unit="s", utc=True).tz_convert(IST))
        tb = tb[tb.index > df1m.index[-1]] if len(df1m) else tb
        if INSTRUMENTS[key].exchange == "MCX":
            tb = feed.session_only(tb, INSTRUMENTS[key])
        if len(df1m) and len(bars):
            last = df1m.index[-1]
            if int(last.timestamp()) in bars:  # the forming minute: widen with tick extremes
                o, h, l, c = bars[int(last.timestamp())]
                df1m = df1m.copy()
                df1m.loc[last, "High"] = max(df1m.loc[last, "High"], h)
                df1m.loc[last, "Low"] = min(df1m.loc[last, "Low"], l)
                df1m.loc[last, "Close"] = c
        return pd.concat([df1m, tb]).sort_index() if len(tb) else df1m

    def _option_extras(self, key: str, a: dict):
        """Lot size, rupee risk/reward per lot, and the contract to stream live (Groww)."""
        opt = a.get("option") or {}
        lot = contracts.lot_size(key)
        if lot:
            opt["lot_size"] = lot
            opt["lot_desc"] = INSTRUMENTS[key].lot_desc(lot)
            if opt.get("premium") is not None:
                opt["risk_per_lot"] = round((opt["premium"] - opt["premium_sl"]) * lot, 0)
                opt["reward_t1_per_lot"] = round((opt["premium_t1"] - opt["premium"]) * lot, 0)
                opt["reward_t2_per_lot"] = round((opt["premium_t2"] - opt["premium"]) * lot, 0)
                opt["cost_per_lot"] = round(opt["premium"] * lot, 0)
        if opt.get("expiry") and opt.get("strike"):
            iso = datetime.strptime(opt["expiry"], "%d-%b-%Y").strftime("%Y-%m-%d")
            wanted = {}
            for t in ("CE", "PE"):
                c = contracts.find(key, iso, opt["strike"], t)
                if c:
                    wanted[f"{key}:{int(opt['strike'])}:{t}"] = c
            opt["stream_keys"] = list(wanted)
            return wanted
        return {}

    async def _loop(self):
        while True:
            t0 = time.time()
            try:
                await asyncio.to_thread(self.refresh)
                self.errors.pop("loop", None)
            except Exception as e:
                log.exception("refresh failed")
                self.errors["loop"] = str(e)
            wait = settings.refresh_seconds if any_open() else 300
            await asyncio.sleep(max(2.0, wait - (time.time() - t0)))

    def refresh(self, force_chain: bool = False):
        is_open = any_open()
        now = time.time()

        if now - self._t["news"] > (60 if is_open else 300):
            try:
                nifty = self.analysis.get("NIFTY", {}).get("spot")
                fresh = news.ingest(nifty)
                self.mood = news.mood()
                self.moods = {k: news.mood(pattern=i.news_filter) for k, i in INSTRUMENTS.items() if i.news_filter}
                if fresh:
                    self.publish("news", {"items": fresh[:30], "mood": self.mood})
                self.errors.pop("news", None)
            except Exception as e:
                self.errors["news"] = f"news: {e}"
            self._t["news"] = now

        try:
            self.vix = feed.vix_1m()
            self.errors.pop("vix", None)
        except Exception as e:
            self.errors["vix"] = f"VIX: {e}"

        want_opts: dict = {}
        for key, inst in active().items():
            try:
                df1m, src = feed.intraday_1m(inst)
                if df1m.empty:
                    raise RuntimeError("no candles returned")
                df1m = self._merge_ticks(key, df1m)
                self.candles[key] = (df1m, src)
                chain_note = None
                # MCX chains without Groww cost 2 requests per strike: refresh them less often
                every = settings.mcx_chain_refresh_seconds if inst.exchange == "MCX" and not settings.has_groww_creds                     else settings.chain_refresh_seconds
                if force_chain or now - self.chain_at.get(key, 0) > (every if inst.is_open() else 1800):
                    try:
                        self.chains[key] = feed.option_chain(inst, self.chains.get(key, {}).get("expiry"),
                                                             float(df1m["Close"].iloc[-1]))
                        self.chain_at[key] = now
                        self.errors.pop(f"chain:{key}", None)
                    except Exception as e:
                        self.errors[f"chain:{key}"] = f"{key} option chain: {e}"
                daily = self.daily_for(key)
                mood = self.moods.get(key, self.mood) if inst.news_filter else self.mood
                exps = feed.expiries(inst) if inst.exchange == "MCX" else None
                if exps:  # monthly MCX options: skip a contract expiring within 2 days
                    exps = [e for e in exps if (datetime.strptime(e, "%d-%b-%Y").date() - datetime.now(IST).date()).days > 2] or exps
                a = live.analyze(inst, df1m, self.chains.get(key), mood, self.vix if inst.exchange == "NSE" else None, src,
                                 daily["today"] if daily else None, exps, daily["rv20"] if daily else None)
                a["chain_note"] = chain_note
                a["context"] = self._context(inst)
                a["setup"] = daily["setup"] if daily else None
                want_opts.update(self._option_extras(key, a))
                a["daily"] = daily["today"] if daily else None
                self.active[key] = tracker.update(key, df1m, inst.hours()["eod"])
                if inst.is_open():
                    new = tracker.maybe_open(a)
                    if new:
                        self.active[key] = new
                        self.publish("signal", {"signal": new, "name": inst.name})
                        db.log(f"{inst.name}: BUY {new['opt_type']} {new['strike']:.0f} at spot {new['entry']:.2f} "
                               f"(SL {new['sl']:.2f}, T1 {new['t1']:.2f}, T2 {new['t2']:.2f})")
                a["active"] = self.active.get(key)
                self.analysis[key] = a
                if key == "NIFTY" and is_open:
                    db.execute("INSERT OR IGNORE INTO ticks (ts, symbol, price) VALUES (?, 'NIFTY', ?)",
                               (datetime.now(IST).isoformat(timespec="seconds"), a["spot"]))
                self.errors.pop(key, None)
            except Exception as e:
                log.exception("analysis failed for %s", key)
                self.errors[key] = f"{key}: {e}"

        self.ticks.want_options(want_opts)
        if now - self._t["breadth"] > (120 if is_open else 900):
            try:
                self.breadth = feed.breadth()
                self.errors.pop("breadth", None)
            except Exception as e:
                self.errors["breadth"] = f"breadth: {e}"
            self._t["breadth"] = now

        try:
            self.recorder.tick(self.analysis)
            self.errors.pop("predictions", None)
        except Exception as e:
            log.exception("prediction log failed")
            self.errors["predictions"] = f"prediction log: {e}"
        snapshot.maybe_publish()
        if publish.status["last_error"]:
            self.errors["publish"] = f"GitHub publish: {publish.status['last_error']}"
        else:
            self.errors.pop("publish", None)

        self.publish("update", {"overview": self.overview()})

    def _context(self, inst) -> dict | None:
        """Extra tile for the instrument: India VIX for NSE; for MCX the global USD future + USD/INR,
        or (zinc: no global feed) the MCX open interest."""
        p = self.ticks.snapshot()["prices"]
        if inst.exchange == "MCX":
            k = f"X:{inst.yahoo}"
            w, fx = p.get(k), p.get("USDINR")
            if not w:
                q = p.get(inst.key) or {}
                if q.get("oi") is None:
                    return None
                return {"key": None, "label": "Open interest (lots)", "value": f"{q['oi']:,.0f}", "change_pct": None,
                        "sub": f"{q['chg_oi']:+,.0f} today · {q.get('oi_buildup') or ''}" if q.get("chg_oi") is not None else ""}
            ch = (w["price"] / w["prev_close"] - 1) * 100 if w.get("prev_close") else None
            return {"key": k, "label": inst.proxy_name, "value": f"${w['price']:,.2f}",
                    "change_pct": round(ch, 2) if ch is not None else None,
                    "sub": f"USD/INR {fx['price']:.3f}" if fx else ""}
        return None

    # ------------------------------------------------------------- views
    def overview(self) -> dict:
        vix = None
        if self.vix is not None and len(self.vix):
            v = self.vix
            today = v[v.index.date == v.index[-1].date()]
            prev = v[v.index.date < v.index[-1].date()]
            last = float(v["Close"].iloc[-1])
            pc = float(prev["Close"].iloc[-1]) if len(prev) else float(today["Open"].iloc[0])
            vix = {"value": round(last, 2), "change_pct": round((last / pc - 1) * 100, 2)}
        adv = sum(1 for b in self.breadth if b["change"] > 0)
        return {
            "market_open": market_open(), "any_open": any_open(), "vix": vix, "mood": self.mood,
            "breadth": {"advances": adv, "declines": len(self.breadth) - adv,
                        "top": self.breadth[:5], "bottom": self.breadth[-5:][::-1]},
            "instruments": {k: {kk: a.get(kk) for kk in ("key", "name", "spot", "change", "change_pct", "score",
                                                         "bias", "signal", "option", "levels", "active", "last_tick",
                                                         "source", "blockers", "exchange", "open")}
                            for k, a in self.analysis.items()},
            "errors": list(self.errors.values()),
        }

    def backtest(self, key: str) -> dict:
        cached = self.backtests.get(key)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        inst = INSTRUMENTS[key]
        df5, source = feed.history_5m(inst)
        daily = self.daily_for(key)
        hrs = inst.hours()
        res = backtest.run(df5, daily_bias=daily["bias_series"] if daily else None, hours=hrs)
        res["with_daily_algo"] = bool(daily)
        res["cost_points"] = inst.cost_points
        res["data"] = source
        res["stats_without_daily"] = backtest.run(df5, hours=hrs)["stats"] if daily else None
        for st in (res["stats"], res["stats_without_daily"]):
            if st and st.get("trades"):
                st["avg_points_after_costs"] = round(st["avg_points"] - inst.cost_points, 1)
                st["total_points_after_costs"] = round(st["total_points"] - inst.cost_points * st["trades"], 1)
        self.backtests[key] = (time.time(), res)
        return res


service: LiveService | None = None


def get_service() -> LiveService:
    global service
    if service is None:
        service = LiveService()
    return service
