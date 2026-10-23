import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, engine, hr, knowledge, llm, max_api, messaging
from app.config import cfg

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenario"
SIMPLE_MODE_LINE = "⚙️ Сейчас я работаю в упрощённом режиме: умный поиск временно недоступен"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(SCENARIO_DIR / "scenario.xlsx"))
    monkeypatch.setattr(cfg, "HOLIDAYS_PATH", str(SCENARIO_DIR / "holidays.txt"))
    monkeypatch.setattr(cfg, "HR_CHAT_ID", None)
    assert engine.reload_scenario() == []

    async def down(*args, **kwargs):
        raise llm.LLMUnavailable("off")

    # the model is off unless a test says otherwise
    monkeypatch.setattr(llm, "chat", down)
    monkeypatch.setattr(llm, "embed", down)
    monkeypatch.setattr(llm, "is_up", lambda: False)


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        sent.append({"user_id": user_id, "chat_id": chat_id, "text": text, "buttons": buttons})
        return {}

    async def fake_answer(callback_id, notification=None):
        pass

    monkeypatch.setattr(max_api, "send_message", fake_send)
    monkeypatch.setattr(max_api, "answer_callback", fake_answer)
    return sent


def add_recruiter(name="Ольга", max_user_id=700, code="olga"):
    recruiter_id = db.execute(
        "INSERT INTO recruiters (name, max_user_id, invite_code, created_at) VALUES (?, ?, ?, ?)",
        (name, max_user_id, code, db.now_iso()),
    )
    return db.query_one("SELECT * FROM recruiters WHERE id = ?", (recruiter_id,))


def add_employee(recruiter_id=None, name="Смирнова Анна", address="ty", tags="GrPP", max_user_id=500, code="anna",
                 start="2026-11-02"):
    employee_id = db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, recruiter_id, invite_code,"
        " max_user_id, address, linked_at, created_at) VALUES (?, ?, ?, 'Europe/Moscow', ?, ?, ?, ?, ?, ?, ?)",
        (name, name.split()[-1], start, tags, recruiter_id, code, max_user_id, address, db.now_iso(), db.now_iso()),
    )
    return db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))


def callback(payload, user_id=700):
    return {"update_type": "message_callback",
            "callback": {"callback_id": "cb-" + payload, "payload": payload, "user": {"user_id": user_id}},
            "message": {"recipient": {"chat_type": "dialog"}, "body": {"mid": "m-" + payload}}}


def group_callback(payload, user_id=700, chat_id=-900):
    update = callback(payload, user_id)
    update["message"]["recipient"] = {"chat_id": chat_id, "chat_type": "chat"}
    return update


def text_message(text, user_id=700):
    return {"update_type": "message_created",
            "message": {"sender": {"user_id": user_id}, "recipient": {"chat_type": "dialog"},
                        "body": {"mid": "t-" + text, "text": text}}}


def payloads(message):
    return [b["payload"] for row in message["buttons"] or [] for b in row]


def to(outbox, **who):
    return [m for m in outbox if all(m[k] == v for k, v in who.items())]


def test_escalation_reaches_the_recruiter_with_context_and_a_reply_button(outbox, monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", -900)
    monkeypatch.setattr(llm, "is_up", lambda: True)  # «no_answer» comes from the model
    recruiter = add_recruiter()
    employee = add_employee(recruiter["id"])
    for i in range(5):
        messaging.log_message(employee["id"], "bot" if i % 2 else "employee", f"реплика {i}")
    messaging.log_message(employee["id"], "employee", "Где взять пропуск?")

    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_answer"))

    row = db.query_one("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
    assert (row["status"], row["question"]) == ("open", "Где взять пропуск?")
    assert [m["text"] for m in json.loads(row["context"])] == [
        "реплика 1", "реплика 2", "реплика 3", "реплика 4", "Где взять пропуск?"]

    [told] = to(outbox, user_id=500)
    assert told["text"].startswith("Хороший вопрос, но точного ответа в документах компании я не нашёл.")
    assert SIMPLE_MODE_LINE not in told["text"]
    [alert] = to(outbox, user_id=700)
    assert "Смирнова Анна (GrPP)" in alert["text"] and "«Где взять пропуск?»" in alert["text"]
    assert "реплика 1" in alert["text"] and "реплика 0" not in alert["text"]
    assert payloads(alert) == [f"esc:reply:{escalation_id}"]
    [copy] = to(outbox, chat_id=-900)
    assert copy["text"] == alert["text"] and payloads(copy) == payloads(alert)


def test_escalation_without_a_recruiter_stays_open(outbox):
    employee = add_employee(recruiter_id=None, address="vy")

    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_match"))

    assert db.query_one("SELECT status FROM escalations WHERE id = ?", (escalation_id,))[0] == "open"
    [told] = outbox
    assert told["user_id"] == 500 and "ответят вам прямо здесь" in told["text"]
    # the model is off, so the bot only searched by words: the employee should know
    assert told["text"].endswith(SIMPLE_MODE_LINE)


def test_stop_topic_and_request_for_a_human_get_their_own_phrases(outbox):
    employee = add_employee(recruiter_id=add_recruiter()["id"], address="vy")

    run(hr.escalate(employee, "Какая будет зарплата?", "stop_topic"))
    run(hr.escalate(employee, "Как оформить пропуск?", "asked_human"))

    told = [m["text"] for m in to(outbox, user_id=500)]
    assert told == [
        "С этим лучше помогут коллеги из HR: передал им ваш вопрос, ответ придёт сюда.",
        "Хорошо, передал вопрос коллегам из HR. Ответ придёт сюда.",
    ]


def test_recruiter_reply_goes_to_the_employee_and_then_to_the_knowledge_base(outbox):
    recruiter = add_recruiter()
    employee = add_employee(recruiter["id"])
    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_answer"))
    outbox.clear()

    run(hr.handle_recruiter_update(recruiter, callback(f"esc:reply:{escalation_id}")))
    [prompt] = outbox
    assert prompt["user_id"] == 700 and "«Где взять пропуск?»" in prompt["text"]
    assert hr.is_recruiter_update(recruiter, text_message("любой текст"))
    outbox.clear()

    run(hr.handle_recruiter_update(recruiter, text_message("Пропуск выдаёт охрана на первом этаже.")))

    [answer] = to(outbox, user_id=500)
    assert answer["text"] == "По твоему вопросу «Где взять пропуск?»: Пропуск выдаёт охрана на первом этаже."
    row = db.query_one("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
    assert (row["status"], row["answer"], row["answered_by"]) == (
        "answered", "Пропуск выдаёт охрана на первом этаже.", "Ольга")
    assert messaging.history(employee["id"], 1) == [
        {"role": "hr", "text": answer["text"]}]
    [offer] = to(outbox, user_id=700)
    assert payloads(offer) == [f"esc:kb:{escalation_id}:yes", f"esc:kb:{escalation_id}:no"]
    assert not hr.is_recruiter_update(recruiter, text_message("любой текст"))

    run(hr.handle_recruiter_update(recruiter, callback(f"esc:kb:{escalation_id}:yes")))
    run(hr.handle_recruiter_update(recruiter, callback(f"esc:kb:{escalation_id}:yes")))  # pressed twice

    [document] = db.query("SELECT * FROM documents WHERE kind = 'hr_answers'")
    assert document["tags"] == "GrPP"
    [chunk] = db.query("SELECT text FROM chunks WHERE document_id = ?", (document["id"],))
    assert chunk["text"] == "Вопрос: Где взять пропуск?\nОтвет: Пропуск выдаёт охрана на первом этаже."


def test_pulse_by_the_model_sends_red_flags_at_once(outbox, monkeypatch):
    recruiter = add_recruiter()
    employee = add_employee(recruiter["id"])
    replies = [
        '{"tone": "негативный", "topic": "нагрузка", "risk": 2, "reason": "думает об уходе из-за переработок"}',
        '{"tone": "позитивный", "topic": "коллеги", "risk": 0, "reason": "всё хорошо"}',
    ]

    async def fake_chat(messages, json_mode=False):
        assert json_mode
        return replies.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "is_up", lambda: True)

    run(hr.analyze_pulse(employee, "Сижу до ночи, пожалуй, пора искать другое место"))
    run(hr.analyze_pulse(employee, "Коллеги классные"))

    rows = db.query("SELECT tone, topic, risk, reason FROM pulse WHERE employee_id = ? ORDER BY id", (employee["id"],))
    assert [tuple(r) for r in rows] == [
        ("негативный", "нагрузка", 2, "думает об уходе из-за переработок"),
        ("позитивный", "коллеги", 0, "всё хорошо"),
    ]
    [flag] = outbox
    assert flag["user_id"] == 700
    assert "«Сижу до ночи, пожалуй, пора искать другое место»" in flag["text"]
    assert "думает об уходе из-за переработок" in flag["text"] and "нагрузка" in flag["text"]


def test_pulse_without_the_model_goes_by_word_markers(outbox):
    recruiter = add_recruiter()
    employee = add_employee(recruiter["id"])

    run(hr.analyze_pulse(employee, "Никто не помогает, думаю уволиться"))
    run(hr.analyze_pulse(employee, "Не могу получить доступ к почте, сложно"))
    run(hr.analyze_pulse(employee, "Всё отлично"))

    rows = db.query("SELECT tone, topic, risk FROM pulse WHERE employee_id = ? ORDER BY id", (employee["id"],))
    assert [(r["risk"], r["tone"]) for r in rows] == [(2, "негативный"), (1, "негативный"), (0, "нейтральный")]
    assert rows[1]["topic"] == "доступы"
    [flag] = outbox
    assert "«Никто не помогает, думаю уволиться»" in flag["text"]


def test_digest_lists_newcomers_open_questions_flags_and_topics(outbox):
    recruiter = add_recruiter()
    anna = add_employee(recruiter["id"])
    ivan = add_employee(recruiter["id"], name="Петров Иван", tags="GTP", max_user_id=501, code="ivan")
    add_employee(add_recruiter("Мария", 701, "maria")["id"], name="Чужой Новичок", max_user_id=502, code="other")
    now = db.now_iso()
    db.execute("INSERT INTO escalations (employee_id, question, context, created_at) VALUES (?, ?, '[]', ?)",
               (anna["id"], "Где взять пропуск?", now))
    for employee, text, topic, risk in [(anna, "Завал, не успеваю", "нагрузка", 1),
                                        (ivan, "Хочу уйти", "нагрузка", 2),
                                        (ivan, "Нет доступа", "доступы", 1)]:
        db.execute("INSERT INTO pulse (employee_id, text, tone, topic, risk, reason, created_at)"
                   " VALUES (?, ?, 'негативный', ?, ?, 'причина', ?)", (employee["id"], text, topic, risk, now))

    text = hr.digest_text(recruiter["id"])

    assert "Смирнова Анна (GrPP)" in text and "Петров Иван (GTP)" in text and "Чужой" not in text
    assert "«Где взять пропуск?»" in text
    assert "«Хочу уйти»" in text and "«Завал, не успеваю»" not in text
    assert "нагрузка (2)" in text and "доступы (1)" in text

    run(hr.handle_recruiter_update(recruiter, text_message("/сводка")))
    assert outbox[-1] == {"user_id": 700, "chat_id": None, "text": text, "buttons": None}


def test_mode_change_tells_each_linked_recruiter_once(outbox, monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", -900)
    add_recruiter()
    add_recruiter("Мария", 701, "maria")
    add_recruiter("Без МАКС", None, "nomax")

    run(hr.on_llm_mode_change(False))

    assert sorted(m["user_id"] or m["chat_id"] for m in outbox) == [-900, 700, 701]
    assert all("упрощённ" in m["text"] for m in outbox)
    outbox.clear()
    run(hr.on_llm_mode_change(True))
    assert len(outbox) == 3 and all("снова" in m["text"] for m in outbox)


def test_link_recruiter_once_per_account():
    add_recruiter("Ольга", None, "olga")

    assert hr.link_recruiter(700, "nope") is None
    assert hr.link_recruiter(700, "olga")["max_user_id"] == 700
    assert hr.link_recruiter(700, "olga")["name"] == "Ольга"  # the same person again
    assert hr.link_recruiter(701, "olga") is None


def test_survey_summary_goes_to_the_recruiter(outbox):
    recruiter = add_recruiter()
    employee = add_employee(recruiter["id"])
    for step_id, question, answer, score in [
        ("47.1", "1 из 9. Понятны ли тебе задачи? - Да / Нет", "Нет", None),
        ("47.1", "Расскажи, пожалуйста, что не так? Так мы поймём, что исправить.", "Не знаю, с чего начать", None),
        ("47.3", "3 из 9. Насколько руководитель доступен? - от 1 (недоступен) до 10", "6", 6),
    ]:
        db.execute("INSERT INTO answers (employee_id, step_id, question, answer, score, created_at)"
                   " VALUES (?, ?, ?, ?, ?, ?)", (employee["id"], step_id, question, answer, score, db.now_iso()))

    run(hr.survey_finished(employee, ["47.1", "47.3"]))

    [summary] = outbox
    assert summary["user_id"] == 700 and "Смирнова Анна (GrPP)" in summary["text"]
    assert "Понятны ли тебе задачи?: Нет" in summary["text"]
    assert "что не так?: Не знаю, с чего начать" in summary["text"]
    assert "Насколько руководитель доступен?: 6 из 10" in summary["text"]


def test_reply_to_a_question_whose_author_deleted_the_data(outbox):
    recruiter = add_recruiter()
    employee = add_employee(recruiter["id"])
    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_answer"))
    run(hr.handle_recruiter_update(recruiter, callback(f"esc:reply:{escalation_id}")))
    engine.delete_employee_data(employee["id"])
    outbox.clear()

    run(hr.handle_recruiter_update(recruiter, text_message("Пропуск выдаёт охрана.")))

    assert outbox == [{"user_id": 700, "chat_id": None, "text": hr.QUESTION_GONE, "buttons": None}]


def test_private_buttons_work_only_for_the_newcomers_recruiter(outbox, monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", -900)
    olga, maria = add_recruiter(), add_recruiter("Мария", 701, "maria")
    employee = add_employee(olga["id"])
    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_answer"))
    outbox.clear()

    run(hr.handle_recruiter_update(maria, callback(f"esc:reply:{escalation_id}", user_id=701)))
    assert [(m["user_id"], m["text"]) for m in outbox] == [(701, hr.NOT_YOUR_QUESTION)]
    assert not hr.is_recruiter_update(maria, text_message("Пропуск выдаёт охрана.", user_id=701))
    outbox.clear()

    # in the HR chat any linked recruiter may take the question
    run(hr.handle_recruiter_update(maria, group_callback(f"esc:reply:{escalation_id}", user_id=701)))
    run(hr.handle_recruiter_update(maria, text_message("Пропуск выдаёт охрана.", user_id=701)))
    assert [m["text"] for m in to(outbox, user_id=500)] == [
        "По твоему вопросу «Где взять пропуск?»: Пропуск выдаёт охрана."]

    # the offer to save the pair went to Maria, so it is hers to press
    run(hr.handle_recruiter_update(maria, callback(f"esc:kb:{escalation_id}:yes", user_id=701)))
    assert len(db.query("SELECT 1 FROM documents WHERE kind = 'hr_answers'")) == 1


def test_second_answer_to_an_answered_question_is_not_sent(outbox, monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", -900)
    olga, maria = add_recruiter(), add_recruiter("Мария", 701, "maria")
    employee = add_employee(olga["id"])
    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_answer"))
    run(hr.handle_recruiter_update(olga, callback(f"esc:reply:{escalation_id}")))
    run(hr.handle_recruiter_update(maria, group_callback(f"esc:reply:{escalation_id}", user_id=701)))
    run(hr.handle_recruiter_update(olga, text_message("Пропуск выдаёт охрана.")))
    outbox.clear()

    run(hr.handle_recruiter_update(maria, text_message("Пропуск в бюро пропусков.", user_id=701)))

    assert to(outbox, user_id=500) == []
    assert [m["text"] for m in to(outbox, user_id=701)] == ["На этот вопрос уже ответили (Ольга)."]
    assert db.query_one("SELECT answer FROM escalations WHERE id = ?", (escalation_id,))[0] == "Пропуск выдаёт охрана."
    with pytest.raises(ValueError, match="Ольга"):
        run(hr.deliver_answer(escalation_id, "Ответ из кабинета", "HR"))


def test_waiting_for_the_answer_ends_after_a_day(outbox, monkeypatch):
    olga = add_recruiter()
    employee = add_employee(olga["id"])
    escalation_id = run(hr.escalate(employee, "Где взять пропуск?", "no_answer"))
    real_now = db.now_iso
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    monkeypatch.setattr(db, "now_iso", lambda: day_ago)
    run(hr.handle_recruiter_update(olga, callback(f"esc:reply:{escalation_id}")))
    monkeypatch.setattr(db, "now_iso", real_now)
    outbox.clear()

    assert not hr.is_recruiter_update(olga, text_message("Спасибо за вчерашнее"))
    run(hr.handle_recruiter_update(olga, text_message("Спасибо за вчерашнее")))

    assert to(outbox, user_id=500) == []
    assert db.query_one("SELECT status FROM escalations WHERE id = ?", (escalation_id,))[0] == "open"
    assert [m["text"] for m in outbox] == [hr.RECRUITER_HELP]


def test_a_reused_question_id_starts_clean(outbox, monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", -900)
    olga, maria = add_recruiter(), add_recruiter("Мария", 701, "maria")
    anna = add_employee(olga["id"])
    first = run(hr.escalate(anna, "Где взять пропуск?", "no_answer"))
    run(hr.handle_recruiter_update(maria, group_callback(f"esc:reply:{first}", user_id=701)))
    run(hr.handle_recruiter_update(maria, text_message("Пропуск выдаёт охрана.", user_id=701)))
    run(hr.handle_recruiter_update(maria, callback(f"esc:kb:{first}:yes", user_id=701)))
    engine.delete_employee_data(anna["id"])
    ivan = add_employee(olga["id"], name="Петров Иван", max_user_id=501, code="ivan")
    second = run(hr.escalate(ivan, "Где парковка?", "no_answer"))
    assert second == first  # sqlite hands out the freed id again
    outbox.clear()

    # Maria answered the old question, this one is Olga's newcomer
    run(hr.handle_recruiter_update(maria, callback(f"esc:reply:{second}", user_id=701)))
    assert [m["text"] for m in outbox] == [hr.NOT_YOUR_QUESTION]

    run(hr.handle_recruiter_update(olga, callback(f"esc:reply:{second}")))
    run(hr.handle_recruiter_update(olga, text_message("Парковка у входа.")))
    run(hr.handle_recruiter_update(olga, callback(f"esc:kb:{second}:yes")))
    chunks = [row["text"] for row in db.query("SELECT text FROM chunks")]
    assert "Вопрос: Где парковка?\nОтвет: Парковка у входа." in chunks
