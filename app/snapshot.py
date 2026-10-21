"""Publish the whole dashboard to GitHub Pages as a static copy.

GitHub Pages cannot run this Python server, so the local app exports every answer
the web UI asks for (/api/overview, /api/chart/GOLD?tf=5, ...) as JSON files and
pushes them with the same web UI in static mode (web/app.js, STATIC):

  index.html, static/      the app's own page, scripts and styles
  snapshot/*.json          what the API returned at publish time
  log/                     the prediction log page (docs/) and its data

It goes to the gh-pages branch as a single commit that replaces the previous one,
so the frequent snapshots do not pile up in the repository history (the prediction
log keeps its full history on master). GitHub Pages must serve: branch gh-pages, folder /.

Published every PAGES_EVERY_MINUTES while any market is open, once when the last
market closes, and once at start-up. Needs PUBLISH_GIT=1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from .config import DATA_DIR, IST, ROOT, settings
from .market.instruments import INSTRUMENTS, any_open

BUILD = DATA_DIR / "pages_build"
log = logging.getLogger(__name__)
_state = {"last": 0.0, "was_open": False, "running": False}


def _dump(rel: str, obj):
    path = BUILD / "snapshot" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable_encoder(obj), separators=(",", ":"), allow_nan=False), encoding="utf-8")


def build() -> Path:
    """Write the full static site into data/pages_build/."""
    from . import api
    from .live.service import get_service

    s = get_service()
    shutil.rmtree(BUILD, ignore_errors=True)
    (BUILD / "static").mkdir(parents=True)
    for f in ("app.js", "quant.js", "style.css"):
        shutil.copy2(ROOT / "web" / f, BUILD / "static" / f)
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace('"/static/', '"static/')
    page = page.replace('<script src="static/app.js"></script>',
                        '<script>window.ALGTRD_STATIC = true;</script>\n<script src="static/app.js"></script>')
    (BUILD / "index.html").write_text(page, encoding="utf-8")
    (BUILD / ".nojekyll").write_text("")
    shutil.copytree(ROOT / "docs", BUILD / "log", ignore=shutil.ignore_patterns("*.tmp"))

    def safe(rel: str, fn):
        try:
            _dump(rel, fn())
        except Exception as e:  # e.g. an instrument still loading: the page shows "not in this snapshot"
            log.info("snapshot skipped %s: %s", rel, e)

    safe("config.json", api.config)
    safe("overview.json", api.overview)
    safe("breadth.json", api.breadth)
    safe("news.json", lambda: api.news_list(150))
    safe("signals.json", lambda: api.signals(None, 300))
    safe("research.json", api.research)
    safe("quant.json", api.quant_report)
    for key, inst in INSTRUMENTS.items():
        if not inst.available or key not in s.analysis:
            continue
        safe(f"analysis/{key}.json", lambda k=key: api.analysis(k))
        for tf in (1, 3, 5, 15):
            safe(f"chart/{key}_{tf}.json", lambda k=key, t=tf: api.chart(k, t))
        safe(f"chain/{key}.json", lambda k=key: asyncio.run(api.chain(k)))
        if not inst.needs_groww:
            safe(f"backtest/{key}.json", lambda k=key: s.backtest(k))
    now = datetime.now(IST)
    _dump("meta.json", {"updated": now.isoformat(timespec="seconds"), "any_open": any_open(),
                        "every_minutes": settings.pages_every_minutes})
    return BUILD


def maybe_publish():
    """Called after every refresh: publish when due, in a background thread."""
    from . import publish
    if not publish.enabled() or _state["running"]:
        return
    now, is_open = time.time(), any_open()
    due = (_state["last"] == 0
           or (is_open and now - _state["last"] >= settings.pages_every_minutes * 60)
           or (_state["was_open"] and not is_open))  # one last snapshot after the close
    _state["was_open"] = is_open
    if not due:
        return
    _state.update(running=True, last=now)

    def job():
        try:
            t0 = time.time()
            site = build()
            publish.push_site(site, f"Dashboard snapshot {datetime.now(IST):%Y-%m-%d %H:%M} IST")
            log.info("dashboard snapshot published in %.0f s", time.time() - t0)
        except Exception as e:
            publish.status["last_error"] = f"{datetime.now(IST):%H:%M} dashboard snapshot: {e}"[:300]
            log.warning("dashboard snapshot failed: %s", e)
        finally:
            _state["running"] = False
    threading.Thread(target=job, daemon=True, name="pages").start()
