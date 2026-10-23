import asyncio
from pathlib import Path

import pytest

from app import db, max_api, messaging
from app.config import cfg


@pytest.fixture
def sent(monkeypatch):
    """Collects what would go to MAX instead of sending it."""
    calls = []

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        calls.append({"text": text, "user_id": user_id, "buttons": buttons, "image_token": image_token})
        return {}

    monkeypatch.setattr(max_api, "send_message", fake_send)
    return calls


@pytest.fixture
def uploads(monkeypatch):
    paths = []

    async def fake_upload(path):
        paths.append(path)
        return "token-1"

    monkeypatch.setattr(max_api, "upload_image", fake_upload)
    return paths


def make_employee(max_user_id=500):
    employee_id = db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, invite_code, max_user_id, created_at)"
        " VALUES ('Петрова Анна', 'Анна', '2026-11-02', 'Europe/Moscow', ?, ?, ?)",
        (f"code{max_user_id}", max_user_id, db.now_iso()),
    )
    return db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))


def logged(employee_id):
    return [(r["role"], r["text"]) for r in db.query("SELECT role, text FROM messages WHERE employee_id = ? ORDER BY id", (employee_id,))]


def test_long_text_is_split_by_paragraphs_and_logged(sent):
    employee = make_employee()
    first, second, third = "а" * 1500, "б" * 1500, "в" * 1500
    text = f"{first}\n\n{second}\n\n{third}"

    asyncio.run(messaging.send_to_employee(employee, text))

    assert [c["text"] for c in sent] == [f"{first}\n\n{second}", third]
    assert all(c["user_id"] == 500 for c in sent)
    assert logged(employee["id"]) == [("bot", f"{first}\n\n{second}"), ("bot", third)]


def test_paragraph_longer_than_limit_is_still_cut(sent):
    employee = make_employee()
    words = " ".join(["слово"] * 1500)  # about 9000 chars, no blank lines

    asyncio.run(messaging.send_to_employee(employee, words))

    assert len(sent) == 3
    assert all(len(c["text"]) <= 4000 for c in sent)
    assert " ".join(c["text"] for c in sent).split() == words.split()


def test_buttons_go_under_the_last_part(sent):
    employee = make_employee()
    buttons = [[max_api.callback_button("Да", "del:yes")]]

    asyncio.run(messaging.send_to_employee(employee, "а" * 3000 + "\n\n" + "б" * 3000, buttons=buttons))

    assert [c["buttons"] for c in sent] == [None, buttons]


def test_image_is_uploaded_once_and_cached(sent, uploads):
    employee = make_employee()
    images = Path(cfg.DATA_DIR) / "images"
    images.mkdir()
    (images / "map.png").write_bytes(b"png")

    asyncio.run(messaging.send_to_employee(employee, "Вот схема проезда", image="map.png"))
    asyncio.run(messaging.send_to_employee(employee, "Ещё раз схема", image="map.png"))

    assert len(uploads) == 1
    assert [c["image_token"] for c in sent] == ["token-1", "token-1"]
    assert db.kv_get("img:map.png") == "token-1"


def test_missing_image_is_skipped(sent, uploads):
    employee = make_employee()

    asyncio.run(messaging.send_to_employee(employee, "Текст без картинки", image="nope.png"))

    assert uploads == []
    assert [(c["text"], c["image_token"]) for c in sent] == [("Текст без картинки", None)]


def test_image_name_cannot_leave_images_folder(sent, uploads):
    employee = make_employee()
    (Path(cfg.DATA_DIR) / "images").mkdir()
    (Path(cfg.DATA_DIR) / "secret.png").write_bytes(b"x")

    asyncio.run(messaging.send_to_employee(employee, "Текст", image="../secret.png"))

    assert uploads == []


def test_history_is_oldest_first_and_limited():
    employee = make_employee()
    for i in range(8):
        messaging.log_message(employee["id"], "employee" if i % 2 else "bot", f"m{i}")

    assert messaging.history(employee["id"], limit=3) == [
        {"role": "employee", "text": "m5"},
        {"role": "bot", "text": "m6"},
        {"role": "employee", "text": "m7"},
    ]


def test_send_to_user_does_not_log(sent):
    asyncio.run(messaging.send_to_user(900, "Сотрудник задал вопрос"))

    assert [(c["user_id"], c["text"]) for c in sent] == [(900, "Сотрудник задал вопрос")]
    assert db.query("SELECT * FROM messages") == []
