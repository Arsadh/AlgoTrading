"""Push to GitHub: docs/data (the prediction JSON) to master, and the static dashboard to gh-pages.

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


def push_site(site_dir, message: str):
    """Replace the gh-pages branch with one commit holding site_dir (no history kept).
    Uses a separate index file, so the working tree and master are not touched.
    Files that did not change are not uploaded again: git only sends new objects."""
    import os
    index = ROOT / ".git" / "pages-index"
    index.unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index), "GCM_INTERACTIVE": "never", "GIT_TERMINAL_PROMPT": "0"}

    def git(*args):
        r = subprocess.run(["git", f"--git-dir={ROOT / '.git'}", f"--work-tree={site_dir}", *args],
                           cwd=site_dir, capture_output=True, text=True, timeout=300, env=env)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
        return r.stdout.strip()

    git("add", "-A", ".")
    tree = git("write-tree")
    commit = git("commit-tree", tree, "-m", message)
    git("push", "--quiet", "--force", settings.publish_remote, f"{commit}:refs/heads/gh-pages")
    status.update(last_ok=datetime.now(IST).isoformat(timespec="seconds"), last_error=None)
    log.info("published dashboard: %s", message)
