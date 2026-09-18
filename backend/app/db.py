"""SQLite store for the dental voice-agent demo (hackathon prototype).

One file, created and seeded on first use. WAL mode so the two uvicorn workers
can read while one writes.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "voice_agent.db")
_lock = threading.Lock()
_ready_paths: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS office (id INTEGER PRIMARY KEY, name TEXT, address TEXT, phone TEXT, hours TEXT);
CREATE TABLE IF NOT EXISTS providers (id INTEGER PRIMARY KEY, name TEXT, role TEXT, color TEXT);
CREATE TABLE IF NOT EXISTS procedures (code TEXT PRIMARY KEY, name TEXT, price REAL, minutes INTEGER);
CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, insurance TEXT,
  status TEXT DEFAULT 'active', notes TEXT DEFAULT '', memory TEXT DEFAULT '', waitlist INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY, patient_id INTEGER, provider_id INTEGER, start TEXT, end TEXT,
  procedure TEXT, status TEXT, price REAL, source TEXT DEFAULT 'seed', note TEXT DEFAULT '',
  created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_appt_start ON appointments(start);
CREATE INDEX IF NOT EXISTS ix_appt_patient ON appointments(patient_id);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, kind TEXT, patient_id INTEGER, context TEXT DEFAULT '{}',
  status TEXT DEFAULT 'queued', call_id INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, patient_id INTEGER, task_id INTEGER, goal TEXT, mode TEXT DEFAULT 'browser',
  started_at TEXT, ended_at TEXT, duration_s REAL DEFAULT 0, outcome TEXT, summary TEXT DEFAULT '',
  transcript TEXT DEFAULT '[]', events TEXT DEFAULT '[]', revenue REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS unmet_demand (
  id INTEGER PRIMARY KEY, patient_id INTEGER, call_id INTEGER, procedure TEXT, hours REAL,
  preference TEXT, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS agent_files (name TEXT PRIMARY KEY, content TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS phone_calls (
  call_id INTEGER PRIMARY KEY, token TEXT UNIQUE, sid TEXT, to_number TEXT, status TEXT, created_at REAL);
"""


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def db_path() -> str:
    return os.path.abspath(os.getenv("VOICE_AGENT_DB", _DEFAULT_PATH))


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure(path: str) -> None:
    if path in _ready_paths:
        return
    with _lock:
        if path in _ready_paths:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = _connect(path)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            if conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
                from .seed import seed

                seed(conn)
            conn.commit()
        finally:
            conn.close()
        _ready_paths.add(path)


def _migrate(conn) -> None:
    """Columns added after the first deploy (CREATE IF NOT EXISTS won't add them to a live DB)."""
    if "dob" not in {r[1] for r in conn.execute("PRAGMA table_info(patients)")}:
        conn.execute("ALTER TABLE patients ADD COLUMN dob TEXT")


@contextmanager
def connect():
    """Yield a connection to the (created-and-seeded) DB; commits on success."""
    path = db_path()
    _ensure(path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset() -> None:
    """Drop every table and reseed (the Settings 'reset demo data' button)."""
    path = db_path()
    with _lock:
        conn = _connect(path)
        try:
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                conn.execute(f"DROP TABLE IF EXISTS {name}")
            conn.commit()
        finally:
            conn.close()
        _ready_paths.discard(path)
    _ensure(path)


def rows(conn, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(conn, sql: str, args=()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None
