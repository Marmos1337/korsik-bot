from app import db

# copied by hand from interfaces.md, so a renamed column shows up here
EXPECTED_COLUMNS = {
    "recruiters": ["id", "name", "max_user_id", "invite_code", "created_at"],
    "employees": [
        "id", "full_name", "first_name", "start_date", "timezone", "tags", "recruiter_id",
        "invite_code", "max_user_id", "address", "linked_at", "time_shift", "waiting", "created_at",
    ],
    "sent_steps": ["employee_id", "step_id", "status", "sent_at", "answered_at"],
    "messages": ["id", "employee_id", "role", "text", "created_at"],
    "answers": ["id", "employee_id", "step_id", "question", "answer", "score", "created_at"],
    "pulse": ["id", "employee_id", "text", "tone", "topic", "risk", "reason", "created_at"],
    "escalations": [
        "id", "employee_id", "question", "context", "status", "answer", "answered_by",
        "created_at", "answered_at",
    ],
    "documents": ["id", "title", "filename", "tags", "kind", "created_at"],
    "chunks": ["id", "document_id", "section", "text", "embedding"],
    "chunks_fts": ["text", "chunk_id"],
    "kv": ["key", "value"],
}


def add_employee(code="abc", max_user_id=None):
    return db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, invite_code, max_user_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Иванов Иван", "Иван", "2026-11-02", "Europe/Moscow", code, max_user_id, db.now_iso()),
    )


def test_init_db_creates_all_tables_with_contract_columns():
    for table, columns in EXPECTED_COLUMNS.items():
        found = [row["name"] for row in db.query(f"PRAGMA table_info({table})")]
        assert found == columns, table


def test_init_db_twice_keeps_data():
    employee_id = add_employee()
    db.kv_set("max_marker", "42")

    db.init_db()

    assert db.query_one("SELECT first_name FROM employees WHERE id = ?", (employee_id,))["first_name"] == "Иван"
    assert db.kv_get("max_marker") == "42"


def test_wal_and_foreign_keys_cascade():
    assert db.query_one("PRAGMA journal_mode")[0] == "wal"
    employee_id = add_employee()
    db.execute(
        "INSERT INTO messages (employee_id, role, text, created_at) VALUES (?, 'bot', 'привет', ?)",
        (employee_id, db.now_iso()),
    )

    db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))

    assert db.query("SELECT * FROM messages") == []


def test_fts_table_accepts_russian_text():
    db.execute("INSERT INTO chunks_fts (text, chunk_id) VALUES (?, ?)", ("Пропуск выдают на проходной", 7))
    rows = db.query("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?", ("пропуск*",))
    assert [r["chunk_id"] for r in rows] == [7]


def test_kv_get_default_and_overwrite():
    assert db.kv_get("missing") is None
    assert db.kv_get("missing", "x") == "x"
    db.kv_set("a", "1")
    db.kv_set("a", "2")
    assert db.kv_get("a") == "2"


def test_now_iso_is_utc():
    assert db.now_iso().endswith("+00:00")


def test_split_tags():
    assert db.split_tags(" Цех, офис ,,водитель ") == ["Цех", "офис", "водитель"]
    assert db.split_tags("") == []
    assert db.split_tags(None) == []
