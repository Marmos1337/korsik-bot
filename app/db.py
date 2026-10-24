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
    created_at TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    work_days TEXT NOT NULL DEFAULT '1,2,3,4,5',
    work_start TEXT NOT NULL DEFAULT '09:00',
    work_end TEXT NOT NULL DEFAULT '18:00'
);

CREATE TABLE IF NOT EXISTS workplaces (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL,
    work_days TEXT NOT NULL DEFAULT '1,2,3,4,5',
    work_start TEXT NOT NULL DEFAULT '09:00',
    work_end TEXT NOT NULL DEFAULT '18:00',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruiter_workplaces (
    recruiter_id INTEGER NOT NULL REFERENCES recruiters(id) ON DELETE CASCADE,
    workplace_id INTEGER NOT NULL REFERENCES workplaces(id) ON DELETE CASCADE,
    PRIMARY KEY (recruiter_id, workplace_id)
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
    created_at TEXT NOT NULL,
    workplace_id INTEGER REFERENCES workplaces(id)
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
    answered_at TEXT,
    delivered_at TEXT,
    reminders INTEGER NOT NULL DEFAULT 0,
    reminded_at TEXT
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

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    check_text TEXT NOT NULL DEFAULT '',
    step_id TEXT,
    added_by TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    done_at TEXT,
    done_by TEXT,
    nudges INTEGER NOT NULL DEFAULT 0,
    nudged_at TEXT,
    escalated_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (employee_id, step_id)
);

CREATE TABLE IF NOT EXISTS bot_answers (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    helpful INTEGER,
    created_at TEXT NOT NULL,
    rated_at TEXT
);

CREATE TABLE IF NOT EXISTS question_vectors (
    question TEXT PRIMARY KEY,
    embedding BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS managers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    max_user_id INTEGER UNIQUE,
    invite_code TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    question TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL DEFAULT '',
    correct INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_progress (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    done_at TEXT NOT NULL,
    correct INTEGER,
    PRIMARY KEY (employee_id, lesson_id)
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
        _migrate(conn)
    finally:
        conn.close()


# columns added after the first release; CREATE TABLE IF NOT EXISTS leaves old tables as they were
NEW_COLUMNS = {
    "recruiters": [("timezone", "TEXT NOT NULL DEFAULT 'Europe/Moscow'"),
                   ("work_days", "TEXT NOT NULL DEFAULT '1,2,3,4,5'"),
                   ("work_start", "TEXT NOT NULL DEFAULT '09:00'"),
                   ("work_end", "TEXT NOT NULL DEFAULT '18:00'")],
    "employees": [("workplace_id", "INTEGER REFERENCES workplaces(id)"), ("manager_id", "INTEGER REFERENCES managers(id)")],
    "escalations": [("delivered_at", "TEXT"), ("reminders", "INTEGER NOT NULL DEFAULT 0"), ("reminded_at", "TEXT"),
                    ("reason", "TEXT NOT NULL DEFAULT ''")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    with conn:
        for table, columns in NEW_COLUMNS.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name in have:
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                if (table, name) == ("escalations", "delivered_at"):
                    # answers given before this column existed went out at once
                    conn.execute("UPDATE escalations SET delivered_at = answered_at WHERE status = 'answered'")


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
