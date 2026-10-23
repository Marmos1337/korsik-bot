import asyncio
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app import bot, db, engine, main, max_api
from app.config import cfg

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenario"


class Stop(BaseException):
    """Ends the endless polling loop from inside a test."""


def message_update(mid, user_id=321, chat_type="dialog", is_bot=False, text="привет"):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": user_id, "name": "Гость", "is_bot": is_bot},
            "recipient": {"chat_id": 9, "chat_type": chat_type},
            "body": {"mid": mid, "text": text},
        },
    }


def callback_update(callback_id, mid="bot-msg", user_id=321, payload="addr:ty"):
    return {
        "update_type": "message_callback",
        "callback": {"callback_id": callback_id, "payload": payload, "user": {"user_id": user_id}},
        "message": {"recipient": {"chat_type": "dialog"}, "body": {"mid": mid}},
    }


def scripted_updates(monkeypatch, replies):
    """Feeds get_updates from a list: a tuple is a reply, an exception is raised. Returns markers asked for."""
    markers = []

    async def fake_get_updates(marker, timeout=30):
        markers.append(marker)
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(max_api, "get_updates", fake_get_updates)
    monkeypatch.setattr(bot, "RETRY_PAUSE", 0)
    return markers


def mid_or_callback(update):
    return update.get("callback", {}).get("callback_id") or update["message"]["body"]["mid"]


def test_polling_survives_errors_saves_marker_and_skips_duplicates(monkeypatch):
    db.kv_set("max_marker", "5")
    first, broken = message_update("p1"), message_update("p2")
    press_one, press_two = callback_update("c1"), callback_update("c2")
    markers = scripted_updates(monkeypatch, [
        httpx.ConnectError("no network"),
        ([first, broken, press_one, press_two], 6),
        ([first, press_one], 7),  # the same events delivered again
        Stop(),
    ])
    handled = []

    async def fake_handle(update):
        handled.append(mid_or_callback(update))
        if update is broken:
            raise RuntimeError("bad update")

    monkeypatch.setattr(bot, "handle_update", fake_handle)

    with pytest.raises(Stop):
        asyncio.run(bot.polling_loop())

    assert markers == [5, 5, 6, 7]
    assert handled == ["p1", "p2", "c1", "c2"]
    assert db.kv_get("max_marker") == "7"


def test_polling_survives_database_errors(monkeypatch):
    db.kv_set("max_marker", "10")
    markers = scripted_updates(monkeypatch, [
        ([message_update("d1")], 11),
        ([message_update("d2")], 12),
        Stop(),
    ])
    handled = []

    async def fake_handle(update):
        handled.append(mid_or_callback(update))

    real_get, real_set = db.kv_get, db.kv_set
    failures = {"get": 1, "set": 1}

    def flaky(name, real):
        def call(*args):
            if failures[name]:
                failures[name] -= 1
                raise sqlite3.OperationalError("database is locked")
            return real(*args)
        return call

    monkeypatch.setattr(bot, "handle_update", fake_handle)
    monkeypatch.setattr(db, "kv_get", flaky("get", real_get))
    monkeypatch.setattr(db, "kv_set", flaky("set", real_set))

    with pytest.raises(Stop):
        asyncio.run(bot.polling_loop())

    assert markers == [10, 11, 12]
    assert handled == ["d1", "d2"]
    assert real_get("max_marker") == "12"


@pytest.fixture
def replies(monkeypatch):
    sent = []

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        sent.append((user_id, text))
        return {}

    monkeypatch.setattr(max_api, "send_message", fake_send)
    return sent


UNKNOWN_TEXT = "Я помогаю новым сотрудникам. Попросите, пожалуйста, у своего рекрутера личную ссылку на меня."


def test_unknown_user_gets_short_reply_on_vy(replies):
    asyncio.run(bot.handle_update(message_update("u1", user_id=321)))
    asyncio.run(bot.handle_update({"update_type": "bot_started", "user": {"user_id": 322}, "chat_id": 10}))

    assert replies == [(321, UNKNOWN_TEXT), (322, UNKNOWN_TEXT)]


def test_bots_and_group_chats_are_ignored(replies):
    asyncio.run(bot.handle_update(message_update("g1", is_bot=True)))
    asyncio.run(bot.handle_update(message_update("g2", chat_type="chat")))

    assert replies == []


# routing for employees, with the real scenario


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        sent.append({"user_id": user_id, "text": text, "buttons": buttons})
        return {}

    async def fake_answer(callback_id, notification=None):
        pass

    monkeypatch.setattr(max_api, "send_message", fake_send)
    monkeypatch.setattr(max_api, "answer_callback", fake_answer)
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(SCENARIO_DIR / "scenario.xlsx"))
    monkeypatch.setattr(cfg, "HOLIDAYS_PATH", str(SCENARIO_DIR / "holidays.txt"))
    monkeypatch.setattr(cfg, "DEMO_MODE", False)
    monkeypatch.setitem(sys.modules, "app.hr", None)  # hr.py is another ticket
    assert engine.reload_scenario() == []
    return sent


def add_employee(code="anna", tags="GrPP"):
    start = (date.today() + timedelta(days=10)).isoformat()
    return db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, invite_code, created_at)"
        " VALUES ('Смирнова Анна', 'Аня', ?, 'Europe/Moscow', ?, ?, ?)",
        (start, tags, code, db.now_iso()),
    )


def start_update(user_id, payload):
    return {"update_type": "bot_started", "user": {"user_id": user_id, "name": "Аня"}, "payload": payload,
            "chat_id": 9}


def handle(update):
    asyncio.run(bot.handle_update(update))


def texts(outbox):
    return [m["text"] for m in outbox]


def test_start_link_greets_and_asks_address(outbox):
    employee_id = add_employee()

    handle(start_update(500, "anna"))

    assert db.query_one("SELECT max_user_id FROM employees WHERE id = ?", (employee_id,))[0] == 500
    assert [m["user_id"] for m in outbox] == [500, 500]
    assert outbox[0]["text"].startswith("Здравствуйте! Я Корсик") and "/delete" in outbox[0]["text"]
    assert [b["payload"] for row in outbox[1]["buttons"] for b in row] == ["addr:ty", "addr:vy"]


def test_start_with_unknown_or_used_link(outbox):
    add_employee()
    handle(start_update(500, "nope"))
    handle(start_update(500, "anna"))
    outbox.clear()

    handle(start_update(501, "anna"))

    assert texts(outbox) == [bot.USED_LINK_TEXT]
    assert db.query_one("SELECT max_user_id FROM employees")[0] == 500
    assert "Попросите" in bot.UNKNOWN_LINK_TEXT and "ваш" in bot.USED_LINK_TEXT


def test_unknown_link_reply(outbox):
    handle(start_update(500, "nope"))
    assert outbox == [{"user_id": 500, "text": bot.UNKNOWN_LINK_TEXT, "buttons": None}]


def test_button_and_free_text_from_employee(outbox):
    employee_id = add_employee()
    handle(start_update(500, "anna"))
    outbox.clear()

    handle(message_update("t1", user_id=500, text="Где столовая?"))
    # the bot waits for «ты/вы», so the text is a question; answers by documents come with the next ticket
    assert texts(outbox) == ["Записал. Скоро научусь отвечать на вопросы по документам."]
    logged = db.query("SELECT role, text FROM messages WHERE employee_id = ? AND role = 'employee'", (employee_id,))
    assert [(r["role"], r["text"]) for r in logged] == [("employee", "Где столовая?")]

    handle(callback_update("c1", user_id=500, payload="addr:ty"))
    assert db.query_one("SELECT address FROM employees")[0] == "ty"


def test_delete_command_asks_first(outbox):
    add_employee()
    handle(start_update(500, "anna"))

    handle(message_update("t1", user_id=500, text="/delete"))

    assert [b["payload"] for row in outbox[-1]["buttons"] for b in row] == ["del:yes", "del:no"]


def test_next_command_only_in_demo(outbox, monkeypatch):
    employee_id = add_employee()
    handle(start_update(500, "anna"))
    handle(callback_update("c1", user_id=500, payload="addr:ty"))
    outbox.clear()

    handle(message_update("n1", user_id=500, text="/next"))
    assert texts(outbox) == ["Команда /next работает только в демо-режиме."]

    monkeypatch.setattr(cfg, "DEMO_MODE", True)
    outbox.clear()
    handle(message_update("n2", user_id=500, text="/next"))
    # the day before the start: the mood question goes out right away, not on the next scheduler tick
    assert outbox[0]["text"].startswith("Привет, Аня! Завтра твой первый рабочий день.")
    assert db.query_one("SELECT time_shift FROM employees WHERE id = ?", (employee_id,))[0] > 0


def test_message_without_text(outbox):
    add_employee()
    handle(start_update(500, "anna"))
    outbox.clear()

    handle(message_update("p1", user_id=500, text=None))

    assert texts(outbox) == ["Пока я понимаю только текст. Напишите, пожалуйста, словами."]


def test_bot_name_is_asked_again_until_known(monkeypatch):
    calls = []

    async def flaky_get_me():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("no network")
        return {"name": "Корсик Помощник", "username": "korsik_bot"}

    monkeypatch.setattr(max_api, "get_me", flaky_get_me)
    monkeypatch.setattr(cfg, "BOT_USERNAME", "")
    monkeypatch.setattr(main, "BOT_NAME_RETRY", 0)

    asyncio.run(main.wait_for_bot_name())

    assert len(calls) == 2
    assert cfg.BOT_USERNAME == "korsik_bot"
