"""Checklist tasks, answer ratings, leave risk, knowledge gaps, the manager and the lessons."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import bot, db, engine, gaps, hr, learning, llm, managers, max_api, qa, risk, tasks, web
from app.config import cfg
from tests.test_miniapp import TOKEN, fields_for, sign

UTC = timezone.utc
PASSWORD = "test-pass-123"
HEADERS = ["Шаг", "Когда", "После", "Теги", "Тип", "Текст на «ты»", "Текст на «вы»", "Кнопки", "Плохой ответ",
           "Обязательный", "Задача", "Срок задачи", "Проверка", "Руководителю"]
# start Monday 2026-11-02, Moscow
ROWS = [
    ("1", "старт", "", "", "сообщение", "Привет, {имя}!", "", "", "", "да"),
    ("2", "старт", "", "", "сообщение", "Собери документы", "", "", "", "", "Собрать документы", "-1д 18:00",
     "Документы собраны?"),
    ("3", "-1д 10:00", "", "", "сообщение", "Завтра первый день", "", "", "", "", "", "", "",
     "Завтра выходит {новичок}. Подсказки на старте."),
    ("4", "р1 10:00", "", "", "сообщение", "С первым днём", "", "", "", "", "Пройти обучение в приложении",
     "р3 17:00"),
    ("5", "р3 09:00", "", "", "шкала", "Оцени первые дни", "", "", "<8"),
]
LINK = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)  # Friday before the start
SUNDAY_19 = datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
MONDAY_11 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
TUESDAY_11 = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
WEDNESDAY_11 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


class Clock:
    now = LINK

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    path = tmp_path / "scenario.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сценарий"
    ws.append(HEADERS)
    for row in ROWS:
        ws.append(list(row) + [""] * (len(HEADERS) - len(row)))
    wb.save(path)
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(path))
    monkeypatch.setattr(cfg, "HOLIDAYS_PATH", str(tmp_path / "none.txt"))
    monkeypatch.setattr(cfg, "HR_CHAT_ID", None)
    monkeypatch.setattr(cfg, "DEMO_MODE", False)
    monkeypatch.setattr(llm, "is_up", lambda: False)
    assert engine.reload_scenario() == []


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(engine, "utc_now", fake)
    return fake


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
    return sent


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(cfg, "BOT_USERNAME", "korsik_test_bot")
    monkeypatch.setattr(web, "LOGIN_DELAY", 0)
    client = TestClient(web.app, follow_redirects=False)
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    return client


def to(outbox, user_id):
    return [m for m in outbox if m["user_id"] == user_id]


def payloads(message):
    return [b.get("payload") for row in message["buttons"] or [] for b in row]


def press(user_id, payload):
    return {"update_type": "message_callback", "callback": {"callback_id": "cb", "payload": payload,
                                                             "user": {"user_id": user_id}},
            "message": {"recipient": {"chat_type": "dialog"}, "body": {"mid": "m"}}}


def write(user_id, text, mid="t1"):
    return {"update_type": "message_created", "message": {"sender": {"user_id": user_id},
                                                          "recipient": {"chat_type": "dialog"},
                                                          "body": {"mid": mid, "text": text}}}


def feed(update):
    async def go():
        await bot.handle_update(update)
        await bot.settle()
    run(go())


def team(clock):
    """A recruiter, a manager and Anna who opened her link on Friday, start on Monday, Moscow 9-18."""
    workplace = db.execute("INSERT INTO workplaces (name, tags, timezone, created_at) VALUES ('GrPP', 'GrPP',"
                           " 'Europe/Moscow', ?)", (db.now_iso(),))
    recruiter = db.execute("INSERT INTO recruiters (name, max_user_id, invite_code, created_at) VALUES"
                           " ('Ольга', 700, 'olga', ?)", (db.now_iso(),))
    manager = db.execute("INSERT INTO managers (name, max_user_id, invite_code, created_at) VALUES"
                         " ('Иван Петров', 800, 'ivan', ?)", (db.now_iso(),))
    anna = db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, recruiter_id, invite_code,"
        " address, created_at, workplace_id, manager_id) VALUES ('Смирнова Анна', 'Анна', '2026-11-02',"
        " 'Europe/Moscow', 'GrPP', ?, 'anna', 'ty', ?, ?, ?)",
        (recruiter, (LINK - timedelta(days=3)).isoformat(), workplace, manager))
    clock.now = LINK
    assert run(engine.link_employee({"user_id": 500}, "anna")) == "ok"
    return {"anna": anna, "recruiter": recruiter, "manager": manager}


def anna_tasks(employee_id):
    return {t["title"]: t for t in tasks.of_employee(employee_id)}


# the scenario columns

def test_scenario_checks_task_columns(tmp_path):
    from app.scenario import validate_scenario
    path = tmp_path / "bad.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сценарий"
    ws.append(HEADERS)
    ws.append(["1", "старт", "", "", "сообщение", "Привет", "", "", "", "", "Задача", "когда-нибудь"])
    ws.append(["2", "старт", "", "", "сообщение", "Привет", "", "", "", "", "", "р1 10:00"])
    wb.save(path)
    problems = validate_scenario(path)
    assert any("не понял «Срок задачи»" in p for p in problems)
    assert any("только вместе с «Задачей»" in p for p in problems)


# tasks

def test_scenario_tasks_land_in_the_checklist_at_link_with_their_deadlines(clock, outbox):
    people = team(clock)
    found = anna_tasks(people["anna"])
    assert set(found) == {"Собрать документы", "Пройти обучение в приложении"}
    # -1д 18:00 is Sunday 18:00, moved back to Friday because it is a weekend
    assert found["Собрать документы"]["due_at"] == "2026-10-30T15:00:00+00:00"
    assert found["Пройти обучение в приложении"]["due_at"] == "2026-11-04T14:00:00+00:00"


def test_bot_asks_about_an_overdue_task_in_working_hours_once_a_day(clock, outbox):
    people = team(clock)
    clock.now = SUNDAY_19  # the deadline has passed, but it is Sunday evening
    run(engine.advance(people["anna"]))
    assert not any("Напомню про задачу" in m["text"] for m in outbox)

    clock.now = MONDAY_11
    outbox.clear()
    run(engine.advance(people["anna"]))  # the first-day step goes out, the nudge waits for the next round
    run(engine.advance(people["anna"]))
    nudge = [m for m in to(outbox, 500) if "Напомню про задачу «Собрать документы»" in m["text"]]
    assert nudge and "Документы собраны?" in nudge[0]["text"]
    task = anna_tasks(people["anna"])["Собрать документы"]
    assert f"task:{task['id']}:done" in payloads(nudge[0])

    clock.now = MONDAY_11 + timedelta(hours=3)
    outbox.clear()
    run(engine.advance(people["anna"]))
    assert not any("Напомню" in m["text"] for m in outbox)


def test_done_button_ticks_the_task(clock, outbox):
    people = team(clock)
    task = anna_tasks(people["anna"])["Собрать документы"]
    feed(press(500, f"task:{task['id']}:done"))
    assert anna_tasks(people["anna"])["Собрать документы"]["done_at"]
    assert any("отметил" in m["text"] for m in to(outbox, 500))


def test_two_times_not_yet_tells_the_recruiter_and_the_manager(clock, outbox):
    people = team(clock)
    task = anna_tasks(people["anna"])["Собрать документы"]
    clock.now = MONDAY_11
    run(engine.advance(people["anna"]))
    run(engine.advance(people["anna"]))
    feed(press(500, f"task:{task['id']}:later"))
    assert any("спрошу ещё раз завтра" in m["text"] for m in to(outbox, 500))
    assert not to(outbox, 700)

    clock.now = TUESDAY_11
    run(engine.advance(people["anna"]))
    feed(press(500, f"task:{task['id']}:later"))
    assert any("Задача не выполнена: «Собрать документы»" in m["text"] for m in to(outbox, 700))
    assert any("Задача не выполнена" in m["text"] for m in to(outbox, 800))


def test_unanswered_nudges_reach_the_recruiter_a_day_after_the_second(clock, outbox):
    people = team(clock)
    for now in (MONDAY_11, TUESDAY_11, WEDNESDAY_11):
        clock.now = now
        run(engine.advance(people["anna"]))
        run(engine.advance(people["anna"]))
    assert any("не ответил на два напоминания" in m["text"] for m in to(outbox, 700))


def test_hr_adds_a_task_and_the_newcomer_gets_it_with_buttons(clock, outbox, admin):
    people = team(clock)
    response = admin.post(f"/employees/{people['anna']}/tasks", data={"title": "Принести справку", "due": "2026-11-05"})
    assert response.status_code == 303
    task = anna_tasks(people["anna"])["Принести справку"]
    assert task["due_at"] == "2026-11-05T15:00:00+00:00" and task["added_by"] == "HR"
    message = [m for m in to(outbox, 500) if "Принести справку" in m["text"]][0]
    assert "до 05.11" in message["text"] and f"task:{task['id']}:done" in payloads(message)
    page = admin.get(f"/employees/{people['anna']}").text
    assert "Принести справку" in page and "Собрать документы" in page

    admin.post(f"/employees/{people['anna']}/tasks/{task['id']}", data={"action": "delete"})
    assert "Принести справку" not in anna_tasks(people["anna"])
    scenario_task = anna_tasks(people["anna"])["Собрать документы"]
    admin.post(f"/employees/{people['anna']}/tasks/{scenario_task['id']}", data={"action": "delete"})
    assert "Собрать документы" in anna_tasks(people["anna"])  # scenario tasks stay


def test_delete_removes_tasks_with_the_rest(clock, outbox):
    people = team(clock)
    engine.delete_employee_data(people["anna"])
    assert tasks.of_employee(people["anna"]) == []


# answer ratings

@pytest.fixture
def answered(monkeypatch):
    async def fake_answer(employee, question):
        return qa.Answer("В пятницу до 17:00.\n\nИсточник: ПВТР, п. 6.6.1", ["ПВТР"], False, "answered", False)

    monkeypatch.setattr(qa, "answer_question", fake_answer)


def test_answer_from_documents_comes_with_rating_buttons_and_thumbs_down_goes_to_hr(clock, outbox, answered):
    people = team(clock)
    outbox.clear()
    feed(write(500, "До скольки работаем в пятницу?"))
    reply = to(outbox, 500)[-1]
    row = db.query_one("SELECT * FROM bot_answers WHERE employee_id = ?", (people["anna"],))
    assert row["question"] == "До скольки работаем в пятницу?"
    assert f"fb:{row['id']}:0" in payloads(reply) and f"fb:{row['id']}:1" in payloads(reply)

    feed(press(500, f"fb:{row['id']}:0"))
    feed(press(500, f"fb:{row['id']}:0"))
    escalations = db.query("SELECT * FROM escalations WHERE employee_id = ?", (people["anna"],))
    assert len(escalations) == 1 and escalations[0]["reason"] == "bad_answer"
    assert any("До скольки работаем в пятницу?" in m["text"] for m in to(outbox, 700))
    ratings = hr.answer_ratings()
    assert ratings["rated"] == 1 and ratings["share"] == 0


def test_thumbs_up_is_counted_and_sends_nothing_to_hr(clock, outbox, answered):
    people = team(clock)
    feed(write(500, "До скольки работаем в пятницу?"))
    row = db.query_one("SELECT id FROM bot_answers WHERE employee_id = ?", (people["anna"],))
    feed(press(500, f"fb:{row['id']}:1"))
    assert hr.answer_ratings()["share"] == 100 and not db.query("SELECT 1 FROM escalations")


# leave risk and silence

def test_risk_grows_with_red_flags_low_scores_and_overdue_tasks(clock, outbox):
    people = team(clock)
    anna = db.query_one("SELECT * FROM employees WHERE id = ?", (people["anna"],))
    assert risk.score(anna, LINK)["level"] == "low"
    stamp = (LINK - timedelta(hours=1)).isoformat()
    db.execute("INSERT INTO pulse (employee_id, text, tone, topic, risk, reason, created_at) VALUES"
               " (?, 'думаю уволиться', 'негативный', 'нагрузка', 2, '', ?)", (people["anna"], stamp))
    db.execute("INSERT INTO answers (employee_id, step_id, question, answer, score, created_at) VALUES"
               " (?, '5', 'Оцени', '3', 3, ?)", (people["anna"], stamp))
    clock.now = TUESDAY_11
    state = risk.score(anna, TUESDAY_11)
    assert state["level"] == "high" and state["score"] >= 50
    assert any("красных флагов" in r for r in state["reasons"]) and any("плохих ответов" in r for r in state["reasons"])
    assert any("просроченных задач" in r for r in state["reasons"])


def test_silent_newcomer_is_reported_once(clock, outbox):
    people = team(clock)
    old = (TUESDAY_11 - timedelta(days=6)).isoformat()
    db.execute("UPDATE messages SET created_at = ? WHERE employee_id = ?", (old, people["anna"]))
    db.execute("INSERT INTO messages (employee_id, role, text, created_at) VALUES (?, 'employee', 'ок', ?)",
               (people["anna"], old))
    for text in ("раз", "два"):
        db.execute("INSERT INTO messages (employee_id, role, text, created_at) VALUES (?, 'bot', ?, ?)",
                   (people["anna"], text, old))
    assert run(risk.warn_about_silent(TUESDAY_11)) == 1
    assert any("не отвечает боту 6 дн." in m["text"] for m in to(outbox, 700))
    assert run(risk.warn_about_silent(TUESDAY_11 + timedelta(hours=2))) == 0


# knowledge gaps

def test_gaps_group_questions_by_words_without_the_model_and_skip_stop_topics(clock, outbox):
    people = team(clock)
    now = datetime.now(UTC)
    for question, reason in (("Где получить спецодежду на складе?", "no_match"),
                             ("Спецодежду где получить?", "no_answer"),
                             ("Какая будет зарплата?", "stop_topic"),
                             ("Как оформить пропуск на машину?", "no_match")):
        db.execute("INSERT INTO escalations (employee_id, question, context, created_at, reason) VALUES"
                   " (?, ?, '[]', ?, ?)", (people["anna"], question, now.isoformat(), reason))
    found = run(gaps.find(now))
    assert found[0]["count"] == 2 and "спецодежд" in found[0]["label"].lower()
    assert all("зарплата" not in g["label"] for g in found)
    assert len(found) == 2


# the manager

def test_manager_links_gets_tips_before_the_start_and_a_digest(clock, outbox):
    db.execute("INSERT INTO managers (name, invite_code, created_at) VALUES ('Сергей', 'serg', ?)", (db.now_iso(),))
    feed({"update_type": "bot_started", "user": {"user_id": 801, "name": "С"}, "payload": "mg_serg", "chat_id": 801})
    assert "ваш МАКС привязан" in to(outbox, 801)[-1]["text"]

    people = team(clock)
    clock.now = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)  # step 3: -1д 10:00, Sunday 10:00 Moscow
    run(engine.advance(people["anna"]))
    memo = [m["text"] for m in to(outbox, 800) if "Подсказки на старте" in m["text"]]
    assert memo and "Завтра выходит Смирнова Анна" in memo[0] and "Задачи: сделано 0" in memo[0]

    feed(write(800, "/сводка"))
    assert "Смирнова Анна" in to(outbox, 800)[-1]["text"]
    feed(write(800, "привет", mid="t2"))
    assert to(outbox, 800)[-1]["text"] == managers.HELP


def test_manager_is_chosen_in_the_cabinet(clock, outbox, admin):
    people = team(clock)
    page = admin.get("/managers").text
    assert "Иван Петров" in page
    assert admin.post("/managers", data={"name": "Мария Сидорова"}).status_code == 303
    maria = db.query_one("SELECT id FROM managers WHERE name = 'Мария Сидорова'")["id"]
    assert "mg_" in admin.get("/managers").text
    admin.post(f"/employees/{people['anna']}/move", data={"workplace_id": "", "recruiter_id": str(people["recruiter"]),
                                                         "manager_id": str(maria)})
    assert db.query_one("SELECT manager_id FROM employees WHERE id = ?", (people["anna"],))["manager_id"] == maria


# lessons

@pytest.fixture
def lessons(tmp_path, monkeypatch):
    seed = tmp_path / "learning.json"
    seed.write_text(json.dumps([
        {"title": "Рабочее время", "body": "С 9 до 18", "source": "ПВТР, п. 6.6.1", "question": "До скольки в пятницу?",
         "options": ["18:00", "17:00"], "correct": 1},
        {"title": "Подарки", "body": "Не дороже трёх тысяч"},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(learning, "SEED", seed)
    learning.seed()
    learning.seed()  # the second start does not add them again
    return [r["id"] for r in learning.all_lessons()]


def test_lessons_seed_once_and_a_right_answer_passes(clock, outbox, lessons):
    people = team(clock)
    anna = db.query_one("SELECT * FROM employees WHERE id = ?", (people["anna"],))
    assert len(lessons) == 2
    assert not learning.answer(anna, lessons[0], 0)
    assert learning.answer(anna, lessons[0], 1)
    assert not anna_tasks(people["anna"])["Пройти обучение в приложении"]["done_at"]
    assert learning.answer(anna, lessons[1], None)  # a lesson without a question passes when read
    assert anna_tasks(people["anna"])["Пройти обучение в приложении"]["done_at"]


def test_hr_edits_lessons_in_the_cabinet(clock, admin, lessons):
    assert "Рабочее время" in admin.get("/learning").text
    response = admin.post("/learning", data={"title": "Пропуск", "body": "Пропуск выдаёт охрана", "question": "Кто?",
                                             "options": "Охрана\nБухгалтерия", "correct": "1"})
    assert response.status_code == 303
    assert db.query_one("SELECT options, correct FROM lessons WHERE title = 'Пропуск'")["options"] == "Охрана\nБухгалтерия"
    bad = admin.post("/learning", data={"title": "Х", "body": "т", "question": "?", "options": "один"},
                     follow_redirects=True)
    assert "хотя бы два варианта" in bad.text


# the mini app

@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(cfg, "MAX_BOT_TOKEN", TOKEN)
    return TestClient(web.app)


def test_newcomer_ticks_own_task_and_passes_a_lesson_in_the_app(clock, outbox, app_client, lessons):
    people = team(clock)
    page = app_client.post("/app/view", data={"init_data": sign(fields_for(500)), "tab": "tasks"}).text
    assert "Собрать документы" in page and "Твои задачи" in page
    task = anna_tasks(people["anna"])["Собрать документы"]
    app_client.post(f"/app/task/{task['id']}", data={"init_data": sign(fields_for(500)), "done": "1"})
    assert anna_tasks(people["anna"])["Собрать документы"]["done_at"]
    other = app_client.post(f"/app/task/{task['id']}", data={"init_data": sign(fields_for(700)), "done": "0"})
    assert other.status_code == 403

    page = app_client.post("/app/view", data={"init_data": sign(fields_for(500)), "tab": "learning"}).text
    assert "Рабочее время" in page and "До скольки в пятницу?" in page
    wrong = app_client.post(f"/app/lesson/{lessons[0]}", data={"init_data": sign(fields_for(500)), "choice": "0"}).text
    assert "Не совсем" in wrong
    right = app_client.post(f"/app/lesson/{lessons[0]}", data={"init_data": sign(fields_for(500)), "choice": "1"}).text
    assert "Урок пройден" in right


def test_recruiter_and_manager_give_tasks_only_to_their_newcomers(clock, outbox, app_client):
    people = team(clock)
    stranger = db.execute("INSERT INTO employees (full_name, first_name, start_date, timezone, invite_code, created_at)"
                          " VALUES ('Чужой', 'Чужой', '2026-11-02', 'Europe/Moscow', 'x', ?)", (db.now_iso(),))
    r = app_client.post(f"/app/tasks/{people['anna']}", data={"init_data": sign(fields_for(700)),
                                                             "title": "Подписать договор", "due": ""})
    assert "задача добавлена" in r.text
    assert anna_tasks(people["anna"])["Подписать договор"]["added_by"] == "Рекрутер Ольга"
    r = app_client.post(f"/app/tasks/{people['anna']}", data={"init_data": sign(fields_for(800)),
                                                             "title": "Познакомиться с наставником", "due": ""})
    assert anna_tasks(people["anna"])["Познакомиться с наставником"]["added_by"] == "Руководитель Иван Петров"
    r = app_client.post(f"/app/tasks/{stranger}", data={"init_data": sign(fields_for(800)), "title": "x", "due": ""})
    assert r.status_code == 403 and not tasks.of_employee(stranger)
    page = app_client.post("/app/view", data={"init_data": sign(fields_for(800))}).text
    assert "Новички в подразделении" in page and "Смирнова Анна" in page and "Чужой" not in page
