import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app import bot, db, engine, hr, knowledge, llm, main, max_api, qa
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
        if isinstance(reply, Stop):
            await bot.settle()  # updates run in their own tasks: let them finish before the loop is stopped
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
    monkeypatch.setattr(cfg, "HR_CHAT_ID", None)

    async def down(*args, **kwargs):
        raise llm.LLMUnavailable("off")

    # Ollama is off in these tests: answers go by keywords, no network
    monkeypatch.setattr(llm, "chat", down)
    monkeypatch.setattr(llm, "embed", down)
    monkeypatch.setattr(llm, "is_up", lambda: False)
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
    async def handle_and_settle():
        await bot.handle_update(update)
        await bot.settle()  # the pulse mark runs in the background

    asyncio.run(handle_and_settle())


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


SIMPLE_MODE_LINE = "⚙️ Сейчас я работаю в упрощённом режиме: умный поиск временно недоступен"


def linked_employee(outbox, code="anna", user_id=500):
    employee_id = add_employee(code)
    handle(start_update(user_id, code))
    handle(callback_update("c-" + code, user_id=user_id, payload="addr:ty"))
    outbox.clear()
    return employee_id


def test_button_and_free_text_from_employee(outbox):
    employee_id = add_employee()
    handle(start_update(500, "anna"))
    outbox.clear()

    handle(message_update("t1", user_id=500, text="Где столовая?"))
    # the bot waits for «ты/вы», so the text is a question; the base is empty, so it goes to HR
    assert texts(outbox)[0].startswith("Хороший вопрос, но точного ответа в документах компании я не нашёл.")
    assert db.query_one("SELECT question FROM escalations WHERE employee_id = ?", (employee_id,))[0] == "Где столовая?"
    logged = db.query("SELECT role, text FROM messages WHERE employee_id = ? AND role = 'employee'", (employee_id,))
    assert [(r["role"], r["text"]) for r in logged] == [("employee", "Где столовая?")]

    handle(callback_update("c1", user_id=500, payload="addr:ty"))
    assert db.query_one("SELECT address FROM employees")[0] == "ty"


def test_answer_by_documents_goes_to_the_employee_and_the_text_to_pulse(outbox, monkeypatch):
    employee_id = linked_employee(outbox)

    async def fake_answer(employee, question):
        return qa.Answer("Рабочий день с 9:00.\n\nИсточник: ПВТР, п. 6.6.1", ["ПВТР, п. 6.6.1"], False, "answered",
                         False)

    monkeypatch.setattr(qa, "answer_question", fake_answer)
    handle(message_update("q1", user_id=500, text="Во сколько начинается рабочий день?"))

    assert texts(outbox) == ["Рабочий день с 9:00.\n\nИсточник: ПВТР, п. 6.6.1"]
    assert db.query("SELECT 1 FROM escalations") == []
    assert db.query_one("SELECT text FROM pulse WHERE employee_id = ?", (employee_id,))[0] == (
        "Во сколько начинается рабочий день?")


def test_need_a_human_escalates_the_question_before_it(outbox, tmp_path):
    path = tmp_path / "pvtr.txt"
    path.write_text("6.6.1. Перерыв для отдыха и питания: 13:00 - 13:48.", encoding="utf-8")
    asyncio.run(knowledge.add_document(path, "ПВТР", []))
    employee_id = linked_employee(outbox)

    handle(message_update("q1", user_id=500, text="Какой перерыв для отдыха и питания?"))
    assert "Перерыв для отдыха и питания: 13:00 - 13:48." in texts(outbox)[0]
    assert texts(outbox)[0].endswith("напиши «нужен человек».")
    outbox.clear()

    handle(message_update("q2", user_id=500, text="Нужен человек"))

    assert texts(outbox) == ["Хорошо, передал вопрос коллегам из HR. Ответ придёт сюда."]
    assert db.query_one("SELECT question FROM escalations WHERE employee_id = ?", (employee_id,))[0] == (
        "Какой перерыв для отдыха и питания?")


def add_recruiter(code="olga", user_id=None):
    return db.execute("INSERT INTO recruiters (name, max_user_id, invite_code, created_at) VALUES ('Ольга', ?, ?, ?)",
                      (user_id, code, db.now_iso()))


def test_recruiter_links_by_hr_link_and_asks_for_a_digest(outbox):
    recruiter_id = add_recruiter()

    handle(start_update(700, "hr_olga"))
    assert db.query_one("SELECT max_user_id FROM recruiters WHERE id = ?", (recruiter_id,))[0] == 700
    assert texts(outbox) == [hr.RECRUITER_LINKED.format(name="Ольга")]
    outbox.clear()

    handle(start_update(701, "hr_nope"))
    handle(message_update("r1", user_id=700, text="/сводка"))

    assert texts(outbox) == [hr.RECRUITER_LINK_UNKNOWN, "За вами пока не закреплён ни один новичок."]


def group_callback(callback_id, chat_id, payload, user_id=700):
    update = callback_update(callback_id, user_id=user_id, payload=payload)
    update["message"]["recipient"] = {"chat_id": chat_id, "chat_type": "chat"}
    return update


def test_hr_chat_takes_only_button_presses_of_recruiters(outbox, monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", -900)
    recruiter_id = add_recruiter(user_id=700)
    linked_employee(outbox)
    db.execute("UPDATE employees SET recruiter_id = ?", (recruiter_id,))
    handle(message_update("q1", user_id=500, text="Где взять пропуск?"))
    [copy] = [m for m in outbox if m["user_id"] is None]  # the HR chat copy goes by chat_id
    escalation_id = db.query_one("SELECT id FROM escalations")[0]
    assert [b["payload"] for row in copy["buttons"] for b in row] == [f"esc:reply:{escalation_id}"]
    outbox.clear()

    handle(message_update("g1", user_id=700, chat_type="chat", text="коллеги, кто ответит?"))
    handle(group_callback("gc0", -555, f"esc:reply:{escalation_id}"))  # some other group
    handle(group_callback("gc1", -900, f"esc:reply:{escalation_id}", user_id=999))  # not a recruiter
    assert outbox == []

    handle(group_callback("gc2", -900, f"esc:reply:{escalation_id}"))
    [prompt] = outbox
    assert prompt["user_id"] == 700 and "«Где взять пропуск?»" in prompt["text"]


def test_one_account_can_be_a_recruiter_and_a_newcomer(outbox):
    add_recruiter(user_id=500)
    linked_employee(outbox)

    handle(message_update("q1", user_id=500, text="Где взять пропуск?"))
    assert db.query("SELECT 1 FROM escalations")  # went the newcomer way
    outbox.clear()

    handle(message_update("q2", user_id=500, text="/сводка"))
    assert texts(outbox) == ["За вами пока не закреплён ни один новичок."]  # went the recruiter way


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


def test_main_starts_the_model_watch_with_hr_notifications(monkeypatch):
    import uvicorn

    started = []

    async def fake_watch(on_change):
        started.append(on_change)

    async def idle():
        pass

    async def fake_serve(self):
        await asyncio.sleep(0.01)

    monkeypatch.setattr(llm, "watch_loop", fake_watch)
    monkeypatch.setattr(main, "wait_for_bot_name", idle)
    monkeypatch.setattr(bot, "polling_loop", idle)
    monkeypatch.setattr(engine, "scheduler_loop", idle)
    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)

    asyncio.run(main.main())

    assert started == [hr.on_llm_mode_change]


def test_slow_model_on_one_question_does_not_hold_other_people(outbox, monkeypatch):
    linked_employee(outbox)
    linked_employee(outbox, code="boris", user_id=501)
    gate = asyncio.Event()
    model = {"busy": False}
    sent_to_boris = []

    async def slow_chat(messages, json_mode=False):
        model["busy"] = True
        try:
            await asyncio.wait_for(gate.wait(), 3)  # a sleeping Mac; the limit only keeps a broken test from hanging
        finally:
            model["busy"] = False
        return "NO_ANSWER"

    async def found(query, employee_tags, limit=5, use_vectors=True):
        return [knowledge.Hit(1, "ПВТР", "п. 6.6.1", "Начало работы 9:00.", 0.03, 1, 0.7)]

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        if user_id == 501:
            sent_to_boris.append((text, model["busy"]))
        return {}

    async def fake_get_updates(marker, timeout=30):
        if marker is None:
            return [message_update("a1", user_id=500, text="Во сколько начинается рабочий день?"),
                    message_update("b1", user_id=501, text="/delete"),
                    callback_update("b2", user_id=501, payload="del:no")], 1
        for _ in range(300):
            if len(sent_to_boris) == 2:
                break
            await asyncio.sleep(0.01)
        gate.set()
        raise Stop()

    monkeypatch.setattr(llm, "is_up", lambda: True)
    monkeypatch.setattr(llm, "chat", slow_chat)
    monkeypatch.setattr(knowledge, "search", found)
    monkeypatch.setattr(max_api, "send_message", fake_send)
    monkeypatch.setattr(max_api, "get_updates", fake_get_updates)

    with pytest.raises(Stop):
        asyncio.run(bot.polling_loop())

    # Boris got both replies in order while the model was still busy with Anna's question
    assert sent_to_boris == [(engine.PHRASES["delete_ask"][0], True), ("Хорошо, ничего не удаляю.", True)]


def test_pulse_mark_does_not_hold_the_answer(outbox, monkeypatch):
    linked_employee(outbox)
    marked = asyncio.Event()

    async def slow_pulse(employee, text):
        await asyncio.sleep(3)
        marked.set()

    async def fake_answer(employee, question):
        return qa.Answer("Рабочий день с 9:00.", ["ПВТР"], False, "answered", False)

    monkeypatch.setattr(hr, "analyze_pulse", slow_pulse)
    monkeypatch.setattr(qa, "answer_question", fake_answer)

    async def ask():
        await asyncio.wait_for(bot.handle_update(message_update("q1", user_id=500, text="Во сколько?")), 1)
        return marked.is_set()

    assert asyncio.run(ask()) is False
    assert texts(outbox) == ["Рабочий день с 9:00."]


def test_newcomer_deleted_while_the_pulse_is_marked_leaves_no_trace(outbox, monkeypatch):
    recruiter_id = add_recruiter(user_id=700)
    employee_id = linked_employee(outbox)
    db.execute("UPDATE employees SET recruiter_id = ?", (recruiter_id,))
    gate = asyncio.Event()

    async def slow_pulse_chat(messages, json_mode=False):
        await asyncio.wait_for(gate.wait(), 3)
        return '{"tone": "негативный", "topic": "нагрузка", "risk": 2, "reason": "хочет уйти"}'

    async def fake_answer(employee, question):
        return qa.Answer("Понимаю.", [], False, "answered", False)

    monkeypatch.setattr(llm, "is_up", lambda: True)
    monkeypatch.setattr(llm, "chat", slow_pulse_chat)
    monkeypatch.setattr(qa, "answer_question", fake_answer)

    async def write_then_delete():
        bot.dispatch(message_update("p1", user_id=500, text="Всё достало, хочу уволиться"))
        bot.dispatch(message_update("p2", user_id=500, text="/delete"))
        bot.dispatch(callback_update("p3", user_id=500, payload="del:yes"))
        for _ in range(300):
            if db.query_one("SELECT linked_at FROM employees WHERE id = ?", (employee_id,))[0] is None:
                break
            await asyncio.sleep(0.01)
        gate.set()  # the model finishes the mark only now
        await bot.settle()

    asyncio.run(write_then_delete())

    assert db.query_one("SELECT linked_at FROM employees WHERE id = ?", (employee_id,))[0] is None
    assert db.query("SELECT 1 FROM pulse") == []
    assert [m for m in outbox if m["user_id"] == 700] == []


def test_updates_of_one_person_wait_for_each_other(outbox, monkeypatch):
    linked_employee(outbox)
    gate = asyncio.Event()

    async def slow_answer(employee, question):
        await asyncio.wait_for(gate.wait(), 3)
        return qa.Answer("Первый ответ.", [], False, "answered", False)

    monkeypatch.setattr(qa, "answer_question", slow_answer)

    async def two_in_a_row():
        bot.dispatch(message_update("o1", user_id=500, text="Сколько дней отпуска?"))
        bot.dispatch(message_update("o2", user_id=500, text="/help"))
        await asyncio.sleep(0.1)
        before = list(texts(outbox))
        gate.set()
        await bot.settle()
        return before

    assert asyncio.run(two_in_a_row()) == []  # /help waits while the first question is still being answered
    assert texts(outbox)[0] == "Первый ответ." and texts(outbox)[1].startswith("Я пишу тебе")
