"""The whole way of a newcomer through the bot, without a network.

MAX and the model are fakes. The cabinet, the bot routing, the scenario, search, escalation and the
recruiter's reply are the real code, on a temporary database.
"""
import asyncio
import re
import zlib
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import bot, db, engine, knowledge, llm, max_api, web
from app.config import cfg

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenario"
PASSWORD = "smoke-pass"
RECRUITER, NEWCOMER = 700, 500
NEUTRAL_MARK = '{"tone": "нейтральный", "topic": "другое", "risk": 0, "reason": ""}'


def fake_vector(text: str) -> list[float]:
    # words land in fixed slots: texts with common words are close, texts without are not
    vector = [0.0] * 512
    for word in re.findall(r"[^\W_]+", text.lower()):
        if len(word) > 3:
            vector[zlib.crc32(word[:5].encode()) % 512] += 1
    return vector


@pytest.fixture
def world(monkeypatch):
    sent, model_questions = [], []

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        sent.append({"user_id": user_id, "text": text, "buttons": buttons})
        return {}

    async def fake_callback_answer(callback_id, notification=None):
        pass

    async def fake_embed(texts):
        return [fake_vector(text) for text in texts]

    async def fake_chat(messages, json_mode=False):
        if json_mode:
            return NEUTRAL_MARK  # the mood mark of a free text
        if messages[0]["role"] != "system":
            return "ДА"  # the second look at an answer with numbers
        model_questions.append(messages[-1]["content"])
        return "Рабочий день начинается в 9:00.\nИсточник: 1"

    monkeypatch.setattr(max_api, "send_message", fake_send)
    monkeypatch.setattr(max_api, "answer_callback", fake_callback_answer)
    monkeypatch.setattr(llm, "is_up", lambda: True)
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(SCENARIO_DIR / "scenario.xlsx"))
    monkeypatch.setattr(cfg, "HOLIDAYS_PATH", str(SCENARIO_DIR / "holidays.txt"))
    monkeypatch.setattr(cfg, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(cfg, "BOT_USERNAME", "korsik_test_bot")
    monkeypatch.setattr(cfg, "DEMO_MODE", False)
    monkeypatch.setattr(cfg, "HR_CHAT_ID", None)
    monkeypatch.setattr(web, "LOGIN_DELAY", 0)
    assert engine.reload_scenario() == []
    return SimpleNamespace(sent=sent, model_questions=model_questions)


def feed(update: dict) -> None:
    async def handle_and_settle():
        await bot.handle_update(update)
        await bot.settle()  # the mood mark of a free text is made in the background

    asyncio.run(handle_and_settle())


def start(user_id: int, payload: str) -> dict:
    return {"update_type": "bot_started", "user": {"user_id": user_id, "name": "Гость"}, "payload": payload,
            "chat_id": 9}


def press(user_id: int, payload: str) -> dict:
    return {"update_type": "message_callback",
            "callback": {"callback_id": "cb-" + payload, "payload": payload, "user": {"user_id": user_id}},
            "message": {"recipient": {"chat_type": "dialog"}, "body": {"mid": "m-" + payload}}}


def write(user_id: int, text: str) -> dict:
    return {"update_type": "message_created",
            "message": {"sender": {"user_id": user_id}, "recipient": {"chat_type": "dialog"},
                        "body": {"mid": "t-" + text, "text": text}}}


def texts_to(world, user_id: int) -> list[str]:
    return [m["text"] for m in world.sent if m["user_id"] == user_id]


def payloads(message: dict) -> list[str]:
    return [button["payload"] for row in message["buttons"] or [] for button in row]


def onboard(world, tmp_path) -> int:
    """Recruiter and newcomer get into the cabinet, both open their links, the newcomer picks «вы»."""
    (tmp_path / "pvtr.txt").write_text("6.6.1. Рабочий день начинается в 9:00.", encoding="utf-8")
    asyncio.run(knowledge.add_document(tmp_path / "pvtr.txt", "ПВТР", []))

    cabinet = TestClient(web.app, follow_redirects=False)
    assert cabinet.post("/login", data={"password": PASSWORD}).status_code == 303
    assert cabinet.post("/recruiters", data={"name": "Ольга"}).status_code == 303
    recruiter = db.query_one("SELECT * FROM recruiters")
    form = {"full_name": "Смирнова Анна", "first_name": "Анна", "timezone": "Asia/Novosibirsk", "tags": "GrPP",
            "start_date": (date.today() + timedelta(days=10)).isoformat(), "recruiter_id": str(recruiter["id"])}
    assert cabinet.post("/employees", data=form).status_code == 303
    employee = db.query_one("SELECT * FROM employees")
    assert employee["max_user_id"] is None and employee["address"] is None

    feed(start(RECRUITER, "hr_" + recruiter["invite_code"]))
    assert "ваш МАКС привязан" in texts_to(world, RECRUITER)[0]

    feed(start(NEWCOMER, employee["invite_code"]))
    greeting, question = [m for m in world.sent if m["user_id"] == NEWCOMER]
    assert greeting["text"].startswith("Здравствуйте! Я Корсик") and "/delete" in greeting["text"]
    assert payloads(question) == ["addr:ty", "addr:vy"]
    world.sent.clear()

    feed(press(NEWCOMER, "addr:vy"))
    first_steps = texts_to(world, NEWCOMER)
    assert db.query_one("SELECT address FROM employees")[0] == "vy"
    assert first_steps[0] == "Хорошо, Анна, тогда на «вы». Рад знакомству!"
    assert len(first_steps) >= 4  # the rest of the «старт» block follows the choice at once
    world.sent.clear()
    return employee["id"]


def test_question_without_an_answer_goes_to_the_recruiter_and_the_answer_comes_back(world, tmp_path):
    employee_id = onboard(world, tmp_path)

    feed(write(NEWCOMER, "Где можно оставить велосипед?"))

    told = texts_to(world, NEWCOMER)
    assert len(told) == 1 and told[0].startswith("Хороший вопрос, но точного ответа в документах компании я не нашёл.")
    assert "вам прямо здесь" in told[0]  # the newcomer chose «вы»
    assert world.model_questions == []  # nothing in the documents is close, so the model is not asked
    escalation = db.query_one("SELECT * FROM escalations")
    assert (escalation["employee_id"], escalation["status"]) == (employee_id, "open")

    [alert] = [m for m in world.sent if m["user_id"] == RECRUITER]
    assert "Смирнова Анна (GrPP)" in alert["text"] and "«Где можно оставить велосипед?»" in alert["text"]
    assert payloads(alert) == [f"esc:reply:{escalation['id']}"]
    world.sent.clear()

    feed(press(RECRUITER, f"esc:reply:{escalation['id']}"))
    assert "«Где можно оставить велосипед?»" in texts_to(world, RECRUITER)[0]
    world.sent.clear()

    feed(write(RECRUITER, "Велосипед можно оставить у охраны на первом этаже."))

    assert texts_to(world, NEWCOMER) == [
        "По вашему вопросу «Где можно оставить велосипед?»: Велосипед можно оставить у охраны на первом этаже."
    ]
    closed = db.query_one("SELECT * FROM escalations")
    assert (closed["status"], closed["answered_by"]) == ("answered", "Ольга")
    assert db.query_one("SELECT role FROM messages ORDER BY id DESC LIMIT 1")[0] == "hr"
    [offer] = [m for m in world.sent if m["user_id"] == RECRUITER and m["buttons"]]
    assert payloads(offer) == [f"esc:kb:{escalation['id']}:yes", f"esc:kb:{escalation['id']}:no"]


def test_question_with_an_answer_in_the_documents_gets_it_with_a_source(world, tmp_path):
    onboard(world, tmp_path)

    feed(write(NEWCOMER, "Во сколько начинается рабочий день?"))

    assert texts_to(world, NEWCOMER) == ["Рабочий день начинается в 9:00.\n\nИсточник: ПВТР, п. 6.6.1"]
    assert len(world.model_questions) == 1
    assert db.query("SELECT 1 FROM escalations") == []
    assert texts_to(world, RECRUITER) == []
