"""Commit and push docs/data (the prediction JSON) to GitHub, so GitHub Pages shows it.

Off unless PUBLISH_GIT=1 in .env and the project is a git repository with a remote.
Authentication is left to git itself (Git Credential Manager on Windows), so no
token is stored by this app. Runs in a background thread; one push at a time.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from datetime import datetime

from .config import IST, ROOT, settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
status = {"enabled": False, "last_ok": None, "last_error": None}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=120,
                          env={**__import__("os").environ, "GCM_INTERACTIVE": "never", "GIT_TERMINAL_PROMPT": "0"})


def enabled() -> bool:
    return settings.publish_git and (ROOT / ".git").exists()


def publish(message: str):
    status["enabled"] = enabled()
    if status["enabled"]:
        threading.Thread(target=_run, args=(message,), daemon=True, name="publish").start()


def _run(message: str):
    if not _lock.acquire(blocking=False):
        return  # a push is already running; the next hourly write picks these changes up
    try:
        _git("add", "docs/data")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return
        c = _git("commit", "-m", message)
        if c.returncode != 0:
            raise RuntimeError(c.stderr.strip() or c.stdout.strip())
        p = _git("push", "--quiet", settings.publish_remote, "HEAD")
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip())
        status.update(last_ok=datetime.now(IST).isoformat(timespec="seconds"), last_error=None)
        log.info("published: %s", message)
    except Exception as e:
        status["last_error"] = f"{datetime.now(IST):%H:%M} {e}"[:300]
        log.warning("publish failed: %s", e)
    finally:
        _lock.release()
