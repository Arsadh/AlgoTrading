"""SQLite storage for news, live signals, Nifty ticks and the event log."""
import json
import sqlite3
import threading
from datetime import datetime

from .config import DATA_DIR, IST

DB_PATH = DATA_DIR / "algtrd.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,           -- published time (IST ISO)
    seen_ts TEXT NOT NULL,      -- when we ingested it
    source TEXT, title TEXT, link TEXT,
    sentiment REAL,             -- -1 .. +1
    impact TEXT,                -- low / medium / high
    scope TEXT,                 -- market / sector / stock
    category TEXT,
    symbols TEXT,               -- JSON list of NSE symbols
    analyzer TEXT,              -- vader / claude
    nifty_at_seen REAL          -- Nifty level when ingested (for reaction study)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT, side TEXT, score REAL,
    entry REAL, sl REAL, t1 REAL, t2 REAL, risk REAL,      -- levels on the underlying
    strike REAL, opt_type TEXT, expiry TEXT,
    premium REAL, premium_sl REAL, premium_t1 REAL, premium_t2 REAL,  -- estimates at signal time
    reasons TEXT, status TEXT, t1_hit INTEGER DEFAULT 0, checked_ts TEXT,
    exit_ts TEXT, exit_spot REAL, points REAL, r REAL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS ticks (
    ts TEXT NOT NULL, symbol TEXT NOT NULL, price REAL, PRIMARY KEY (ts, symbol)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_con = connect()
_con.executescript(SCHEMA)


def now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def execute(sql: str, params=()) -> sqlite3.Cursor:
    with _lock:
        cur = _con.execute(sql, params)
        _con.commit()
        return cur


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _con.execute(sql, params).fetchall()]


def log(message: str, level: str = "info") -> None:
    execute("INSERT INTO log (ts, level, message) VALUES (?, ?, ?)", (now_ist(), level, message))


def kv_get(key: str, default=None):
    rows = query("SELECT v FROM kv WHERE k = ?", (key,))
    return json.loads(rows[0]["v"]) if rows else default


def kv_set(key: str, value) -> None:
    execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, json.dumps(value)))
