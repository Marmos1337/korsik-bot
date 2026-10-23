import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS recruiters (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    max_user_id INTEGER UNIQUE,
    invite_code TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    recruiter_id INTEGER REFERENCES recruiters(id),
    invite_code TEXT UNIQUE NOT NULL,
    max_user_id INTEGER UNIQUE,
    address TEXT,
    linked_at TEXT,
    time_shift INTEGER NOT NULL DEFAULT 0,
    waiting TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_steps (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    answered_at TEXT,
    PRIMARY KEY (employee_id, step_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    score INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pulse (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    tone TEXT NOT NULL,
    topic TEXT NOT NULL,
    risk INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    context TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    answered_by TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'file',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    embedding BLOB
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, chunk_id UNINDEXED, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # sqlite turns foreign keys off for every new connection
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    Path(cfg.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def query(sql: str, params=()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def query_one(sql: str, params=()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(sql, params)
        return cur.lastrowid
    finally:
        conn.close()


def kv_get(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_tags(s: str | None) -> list[str]:
    return [tag.strip() for tag in (s or "").split(",") if tag.strip()]
