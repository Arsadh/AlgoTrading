"""FastAPI app: REST + WebSocket backend for the live analysis dashboard in /web."""
from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import ROOT, settings
from .live.service import get_service
from .market import feed
from .market.instruments import INSTRUMENTS
from .news import service as news
from .research import analyze
from .signals import live, options, tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
app = FastAPI(title="algtrd")
WEB = ROOT / "web"
_research = {"running": False, "error": None}


@app.on_event("startup")
async def _startup():
    get_service().start()


def _inst(key: str):
    if key not in INSTRUMENTS:
        raise HTTPException(404, "unknown instrument")
    if not INSTRUMENTS[key].available:
        raise HTTPException(503, f"{INSTRUMENTS[key].name} needs Groww API keys (no free price source)")
    return INSTRUMENTS[key]


# ------------------------------------------------------------------- live
@app.get("/api/overview")
def overview():
    return get_service().overview()


@app.get("/api/ticks")
def ticks():
    return get_service().tick_payload()


@app.get("/api/config")
def config():
    return {"instruments": [{"key": i.key, "name": i.name, "step": i.strike_step, "exchange": i.exchange,
                             "kind": i.kind, "group": i.group, "available": i.available, "needs_groww": i.needs_groww,
                             "hours": {k: v.strftime("%H:%M") for k, v in i.hours().items()}}
                            for i in INSTRUMENTS.values()],
            "signal_threshold": settings.signal_threshold, "min_adx": settings.min_adx,
            "window": [settings.no_signal_before, settings.no_signal_after],
            "groww": settings.has_groww_creds, "claude": bool(settings.anthropic_key), "tick_seconds": settings.tick_seconds}


@app.get("/api/analysis/{key}")
def analysis(key: str):
    _inst(key)
    a = get_service().analysis.get(key)
    if not a:
        raise HTTPException(503, "analysis not ready yet, data is loading")
    return a


@app.get("/api/chart/{key}")
def chart(key: str, tf: int = 5):
    _inst(key)
    if tf not in (1, 3, 5, 15):
        raise HTTPException(400, "tf must be 1, 3, 5 or 15")
    s = get_service()
    if key not in s.candles:
        raise HTTPException(503, "data is loading")
    a = s.analysis.get(key, {})
    return live.chart(s.candles[key][0], tf, sessions=2 if tf == 1 else 3, chain_info=a.get("chain"), inst=_inst(key))


@app.get("/api/chain/{key}")
async def chain(key: str, expiry: str | None = None):
    inst = _inst(key)
    s = get_service()
    ch = s.chains.get(key)
    if expiry and (not ch or ch["expiry"] != expiry):
        try:
            ch = await asyncio.to_thread(feed.option_chain, inst, expiry, s.analysis.get(key, {}).get("spot"))
        except Exception as e:
            raise HTTPException(503, f"option chain unavailable: {e}")
    if not ch:
        raise HTTPException(503, "option chain not available yet")
    spot = s.analysis.get(key, {}).get("spot") or ch["spot"]
    return {"meta": {k: ch.get(k) for k in ("source", "timestamp", "expiry", "expiries", "spot")},
            "analytics": options.analyze_chain(ch, spot, inst.strike_step),
            "rows": options.chain_window(ch, spot, inst.strike_step), "spot": spot}


@app.post("/api/refresh")
async def refresh():
    await asyncio.to_thread(get_service().refresh, True)
    return {"ok": True}


# ---------------------------------------------------------------- signals
@app.get("/api/signals")
def signals(key: str | None = None, limit: int = 200):
    return {"items": tracker.history(key, limit), "scorecard": tracker.scorecard()}


@app.get("/api/backtest/{key}")
async def backtest(key: str):
    if _inst(key).needs_groww:
        raise HTTPException(503, f"no free price history for {INSTRUMENTS[key].name}, so no backtest")
    return await asyncio.to_thread(get_service().backtest, key)


@app.get("/api/breadth")
def breadth():
    return get_service().breadth


# ------------------------------------------------------------------- news
@app.get("/api/news")
def news_list(limit: int = 100):
    return {"items": news.recent(limit), "mood": news.mood()}


@app.post("/api/news/refresh")
async def news_refresh():
    s = get_service()
    fresh = await asyncio.to_thread(news.ingest, s.analysis.get("NIFTY", {}).get("spot"))
    s.mood = news.mood()
    return {"new": len(fresh), "mood": s.mood}


@app.get("/api/log")
def logs(limit: int = 100):
    return db.query("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------- quant lab
@app.get("/api/quant")
def quant_report():
    from .quant import research as quant
    return {"running": get_service().quant_running, "report": quant.load()}


@app.post("/api/quant/run")
def quant_run():
    get_service().run_quant()
    return {"ok": True}


# --------------------------------------------------------------- research
@app.get("/api/research")
def research():
    return {"report": analyze.load_report(), **_research}


@app.post("/api/research/run")
def research_run():
    if _research["running"]:
        return {"ok": False, "message": "already running"}

    def job():
        _research.update(running=True, error=None)
        try:
            analyze.run()
        except Exception as ex:
            _research["error"] = str(ex)
            logging.exception("research failed")
        finally:
            _research["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


# -------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    s = get_service()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    s.subscribers.add(q)
    try:
        while True:
            await sock.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        s.subscribers.discard(q)


# ----------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
