import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, engine, hours, hr, llm, max_api, miniapp, web
from app.config import cfg
from tests.test_miniapp import TOKEN, fields_for, sign

UTC = timezone.utc
PASSWORD = "test-pass-123"
# Monday 2026-11-02; Moscow is UTC+3, Novosibirsk UTC+7
MONDAY_10_MSK = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
MONDAY_20_MSK = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
SATURDAY_12_MSK = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(cfg, "HR_CHAT_ID", None)
    monkeypatch.setattr(llm, "is_up", lambda: False)


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
def clock(monkeypatch):
    class Clock:
        now = MONDAY_10_MSK

        def __call__(self):
            return self.now

    fake = Clock()
    monkeypatch.setattr(engine, "utc_now", fake)
    return fake


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(cfg, "BOT_USERNAME", "korsik_test_bot")
    monkeypatch.setattr(web, "LOGIN_DELAY", 0)
    client = TestClient(web.app, follow_redirects=False)
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    return client


def add_workplace(name="Завод GrPP", tags="GrPP", tz="Europe/Moscow", days="1,2,3,4,5", start="09:00", end="18:00"):
    return db.execute("INSERT INTO workplaces (name, tags, timezone, work_days, work_start, work_end, created_at)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?)", (name, tags, tz, days, start, end, db.now_iso()))


def add_recruiter(name="Ольга", max_user_id=700, code="olga", days="1,2,3,4,5", start="09:00", end="18:00"):
    return db.execute("INSERT INTO recruiters (name, max_user_id, invite_code, created_at, timezone, work_days,"
                      " work_start, work_end) VALUES (?, ?, ?, ?, 'Europe/Moscow', ?, ?, ?)",
                      (name, max_user_id, code, db.now_iso(), days, start, end))


def add_employee(recruiter_id=None, workplace_id=None, tags="GrPP", max_user_id=500, code="anna",
                 tz="Europe/Moscow"):
    return db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, recruiter_id, invite_code,"
        " max_user_id, address, linked_at, created_at, workplace_id)"
        " VALUES ('Смирнова Анна', 'Анна', '2026-11-02', ?, ?, ?, ?, ?, 'ty', ?, ?, ?)",
        (tz, tags, recruiter_id, code, max_user_id, db.now_iso(), db.now_iso(), workplace_id))


def add_question(employee_id, created=MONDAY_10_MSK, question="Где парковка?"):
    return db.execute("INSERT INTO escalations (employee_id, question, context, created_at) VALUES (?, ?, '[]', ?)",
                      (employee_id, question, created.isoformat(timespec="seconds")))


def question(escalation_id):
    return db.query_one("SELECT * FROM escalations WHERE id = ?", (escalation_id,))


# working hours

def test_schedule_is_open_only_on_working_days_and_hours():
    schedule = hours.Schedule("Europe/Moscow", {1, 2, 3, 4, 5}, hours.parse_time("09:00"), hours.parse_time("18:00"))

    assert schedule.is_open(MONDAY_10_MSK)
    assert not schedule.is_open(MONDAY_20_MSK)
    assert not schedule.is_open(SATURDAY_12_MSK)
    assert not schedule.is_open(MONDAY_10_MSK, holidays={MONDAY_10_MSK.date()})
    assert schedule.next_open(MONDAY_20_MSK) == datetime(2026, 11, 3, 6, 0, tzinfo=UTC)  # Tuesday 09:00
    assert schedule.next_open(SATURDAY_12_MSK) == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # next Monday
    assert schedule.describe() == "пн-пт 09:00-18:00"


def test_schedule_counts_in_its_own_zone():
    novosibirsk = hours.Schedule("Asia/Novosibirsk", {1, 2, 3, 4, 5}, hours.parse_time("09:00"),
                                 hours.parse_time("18:00"))
    # 10:00 in Moscow is 14:00 in Novosibirsk, 20:00 in Moscow is midnight there
    assert novosibirsk.is_open(MONDAY_10_MSK)
    assert not novosibirsk.is_open(MONDAY_20_MSK)


@pytest.mark.parametrize("days, start, end, problem", [
    ([], "09:00", "18:00", "рабочий день"),
    (["1"], "9", "18:00", "09:00"),
    (["1"], "18:00", "09:00", "позже начала"),
])
def test_schedule_form_problems(days, start, end, problem):
    schedule, text = hours.read_form("Europe/Moscow", days, start, end)
    assert schedule is None and problem in text


# answers wait for the newcomer's working hours

def test_answer_outside_working_hours_waits_and_goes_out_in_the_morning(clock, outbox):
    workplace = add_workplace()
    employee_id = add_employee(workplace_id=workplace)
    escalation = add_question(employee_id)
    clock.now = MONDAY_20_MSK

    later = run(hr.deliver_answer(escalation, "У проходной 2", "Ольга", employee_id, question(escalation)["created_at"]))

    assert later == datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
    assert outbox == []
    assert question(escalation)["status"] == "answered" and question(escalation)["delivered_at"] is None
    assert run(hr.deliver_waiting_answers()) == 0  # still evening

    clock.now = datetime(2026, 11, 3, 6, 1, tzinfo=UTC)
    assert run(hr.deliver_waiting_answers()) == 1
    assert "У проходной 2" in outbox[0]["text"] and outbox[0]["user_id"] == 500
    assert question(escalation)["delivered_at"] is not None
    assert run(hr.deliver_waiting_answers()) == 0  # once only


def test_answer_in_working_hours_or_without_a_workplace_goes_at_once(clock, outbox):
    with_place = add_employee(workplace_id=add_workplace(), code="a", max_user_id=501)
    without = add_employee(code="b", max_user_id=502)
    clock.now = MONDAY_10_MSK
    first = add_question(with_place)
    assert run(hr.deliver_answer(first, "Да", "Ольга", with_place, question(first)["created_at"])) is None
    clock.now = SATURDAY_12_MSK
    second = add_question(without)
    assert run(hr.deliver_answer(second, "Да", "Ольга", without, question(second)["created_at"])) is None
    assert [m["user_id"] for m in outbox] == [501, 502]


def test_hr_can_send_a_waiting_answer_at_once(clock, outbox):
    employee_id = add_employee(workplace_id=add_workplace())
    escalation = add_question(employee_id)
    clock.now = SATURDAY_12_MSK
    run(hr.deliver_answer(escalation, "Ответ", "Ольга", employee_id, question(escalation)["created_at"]))

    run(hr.send_waiting_now(escalation))

    assert len(outbox) == 1 and question(escalation)["delivered_at"]
    with pytest.raises(ValueError):
        run(hr.send_waiting_now(escalation))


def test_recruiter_hears_when_the_answer_will_reach_the_newcomer(clock, outbox):
    recruiter_id = add_recruiter()
    employee_id = add_employee(recruiter_id=recruiter_id, workplace_id=add_workplace())
    escalation = add_question(employee_id)
    recruiter = db.query_one("SELECT * FROM recruiters WHERE id = ?", (recruiter_id,))
    clock.now = MONDAY_20_MSK
    run(hr.handle_recruiter_update(recruiter, {"update_type": "message_callback",
                                               "callback": {"payload": f"esc:reply:{escalation}"}}))
    run(hr.handle_recruiter_update(recruiter, {"update_type": "message_created",
                                               "message": {"body": {"text": "У проходной 2"}}}))

    assert outbox[-1]["user_id"] == 700
    assert "нерабочее время" in outbox[-1]["text"] and "вт 03.11 в 09:00" in outbox[-1]["text"]


# reminders to the recruiter

def test_recruiter_is_reminded_hourly_up_to_five_times_only_in_working_hours(outbox):
    recruiter_id = add_recruiter()
    escalation = add_question(add_employee(recruiter_id=recruiter_id), created=MONDAY_10_MSK)

    assert run(hr.remind_recruiters(MONDAY_10_MSK + timedelta(minutes=30))) == 0  # too early
    for hour in range(1, 8):
        run(hr.remind_recruiters(MONDAY_10_MSK + timedelta(hours=hour, minutes=1)))

    # 11:01 ... 17:01 are working hours, but the reminders stop at five
    reminders = [m for m in outbox if m["user_id"] == 700]
    assert len(reminders) == 5
    assert reminders[0]["text"].startswith("Напоминание 1 из 5")
    assert "последнее напоминание" in reminders[-1]["text"]
    assert reminders[0]["buttons"][0][0]["payload"] == f"esc:reply:{escalation}"
    assert question(escalation)["reminders"] == 5


def test_no_reminders_at_night_weekend_or_after_an_answer(outbox):
    recruiter_id = add_recruiter()
    employee_id = add_employee(recruiter_id=recruiter_id)
    escalation = add_question(employee_id, created=MONDAY_20_MSK - timedelta(hours=3))

    assert run(hr.remind_recruiters(MONDAY_20_MSK)) == 0
    assert run(hr.remind_recruiters(SATURDAY_12_MSK)) == 0
    db.execute("UPDATE escalations SET status = 'answered' WHERE id = ?", (escalation,))
    assert run(hr.remind_recruiters(MONDAY_10_MSK + timedelta(days=1))) == 0
    assert outbox == []


# what HR sees about an open question

def test_question_turns_red_when_it_waits_too_long_or_nobody_can_answer():
    recruiter_id = add_recruiter()
    asked = MONDAY_10_MSK
    fresh = question(add_question(add_employee(recruiter_id=recruiter_id, code="a", max_user_id=501), created=asked))
    assert not hr.question_state(fresh, asked + timedelta(minutes=20))["overdue"]
    assert hr.question_state(fresh, asked + timedelta(hours=2, minutes=1))["overdue"]

    orphan = question(add_question(add_employee(code="b", max_user_id=502), created=asked))
    assert hr.question_state(orphan, asked)["overdue"]

    unlinked = add_recruiter(name="Ира", max_user_id=None, code="ira")
    nobody = question(add_question(add_employee(recruiter_id=unlinked, code="c", max_user_id=503), created=asked))
    assert "не привязал МАКС" in " ".join(hr.question_state(nobody, asked)["notes"])


def test_question_while_recruiter_is_off_work_turns_red_after_half_an_hour():
    recruiter_id = add_recruiter()
    asked = MONDAY_20_MSK
    row = question(add_question(add_employee(recruiter_id=recruiter_id), created=asked))

    early = hr.question_state(row, asked + timedelta(minutes=10))
    late = hr.question_state(row, asked + timedelta(minutes=40))

    assert not early["overdue"] and "не работает" in " ".join(early["notes"])
    assert late["overdue"] and "вернётся вт 03.11 в 09:00" in " ".join(late["notes"])


def test_recruiter_stats_count_answers_and_their_speed():
    recruiter_id = add_recruiter()
    employee_id = add_employee(recruiter_id=recruiter_id)
    for minutes in (20, 50, 180):
        escalation = add_question(employee_id)
        db.execute("UPDATE escalations SET status = 'answered', answered_at = ?, delivered_at = ? WHERE id = ?",
                   ((MONDAY_10_MSK + timedelta(minutes=minutes)).isoformat(), db.now_iso(), escalation))
    add_question(employee_id, created=datetime.now(UTC))

    stats = hr.recruiter_stats(recruiter_id)

    assert stats["total"] == 4 and stats["answered"] == 3 and stats["open"] == 1
    assert stats["median"] == "50 мин" and stats["within_hour"] == 67


# workplaces

def test_moving_a_newcomer_swaps_workplace_tags_and_keeps_own_tags():
    grpp = add_workplace("Завод GrPP", "GrPP", "Europe/Moscow")
    gtp = add_workplace("Завод GTP", "GTP", "Asia/Novosibirsk")
    employee_id = add_employee(workplace_id=grpp, tags="GrPP, офис")

    hr.change_workplace(employee_id, gtp)

    employee = db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    assert employee["workplace_id"] == gtp and employee["tags"] == "GTP, офис"
    assert employee["timezone"] == "Asia/Novosibirsk"


def test_cabinet_workplace_recruiter_and_newcomer_flow(admin):
    schedule = {"timezone": "Asia/Novosibirsk", "days": ["1", "2", "3", "4", "5", "6"], "work_start": "08:00",
                "work_end": "20:00"}
    assert admin.post("/workplaces", data={"name": "Завод GTP", "tags": "GTP", **schedule}).status_code == 303
    assert "уже есть" in admin.post("/workplaces", data={"name": "завод gtp", **schedule},
                                    follow_redirects=True).text
    workplace = db.query_one("SELECT * FROM workplaces")
    assert workplace["work_days"] == "1,2,3,4,5,6" and workplace["work_start"] == "08:00"

    form = {"name": "Ольга", "timezone": "Europe/Moscow", "days": ["1", "2", "3", "4", "5"], "work_start": "09:00",
            "work_end": "18:00", "workplaces": [str(workplace["id"])]}
    assert admin.post("/recruiters", data=form).status_code == 303
    recruiter = db.query_one("SELECT * FROM recruiters")

    # the newcomer takes tags, zone and recruiter from the workplace
    newcomer = {"full_name": "Петров Иван Ильич", "start_date": "2026-11-02", "timezone": "workplace",
                "tags": "склад", "workplace_id": str(workplace["id"])}
    assert admin.post("/employees", data=newcomer).status_code == 303
    employee = db.query_one("SELECT * FROM employees")
    assert employee["tags"] == "GTP, склад" and employee["timezone"] == "Asia/Novosibirsk"
    assert employee["recruiter_id"] == recruiter["id"] and employee["workplace_id"] == workplace["id"]

    # with workplaces in place, a newcomer without one is refused
    newcomer.pop("workplace_id")
    assert "Выберите место работы" in admin.post("/employees", data=newcomer, follow_redirects=True).text

    page = admin.get(f"/recruiters/{recruiter['id']}").text
    assert "Завод GTP" in page and "пн-пт 09:00-18:00" in page
    form.update(work_end="17:00", days=["1", "2", "3", "4"])
    assert admin.post(f"/recruiters/{recruiter['id']}", data=form).status_code == 303
    assert db.query_one("SELECT work_days, work_end FROM recruiters")[:] == ("1,2,3,4", "17:00")

    assert admin.get("/dashboard").status_code == 200
    assert admin.get("/workplaces").status_code == 200
    assert "Здесь есть новички" in admin.post(f"/workplaces/{workplace['id']}/delete", follow_redirects=True).text


def test_cabinet_shows_questions_by_workplace_with_red_banner(admin):
    grpp = add_workplace("Завод GrPP")
    gtp = add_workplace("Завод GTP", "GTP")
    add_question(add_employee(workplace_id=grpp, code="a", max_user_id=501), question="Где парковка?",
                 created=datetime.now(UTC) - timedelta(hours=5))
    add_question(add_employee(workplace_id=gtp, code="b", max_user_id=502), question="Где столовая?",
                 created=datetime.now(UTC))

    page = admin.get("/questions").text

    assert page.index(">Завод GrPP<") < page.index("Где парковка?") < page.index(">Завод GTP<")
    # both have no recruiter at all; the red banner is on every other page, the questions page shows them itself
    assert "Вопросы ждут ответа слишком долго: 2" in admin.get("/dashboard").text
    assert "Рекрутер не назначен" in page


def test_cabinet_send_now_button_for_a_waiting_answer(admin, outbox, clock):
    employee_id = add_employee(workplace_id=add_workplace())
    escalation = add_question(employee_id)
    clock.now = SATURDAY_12_MSK
    run(hr.deliver_answer(escalation, "Ответ", "Ольга", employee_id, question(escalation)["created_at"]))

    page = admin.get("/questions").text
    assert "Ждут рабочего времени новичка" in page and "пн 09.11 в 09:00" in page
    assert admin.post(f"/questions/{escalation}/send-now").status_code == 303
    assert len(outbox) == 1


# mini app for the recruiter

@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(cfg, "MAX_BOT_TOKEN", TOKEN)
    return TestClient(web.app)


def test_recruiter_sets_own_schedule_in_the_mini_app(app_client):
    recruiter_id = add_recruiter(max_user_id=700)
    data = {"init_data": sign(fields_for(700)), "timezone": "Asia/Omsk", "days": ["2", "4"], "work_start": "10:00",
            "work_end": "16:00"}

    response = app_client.post("/app/profile", data=data)

    assert response.status_code == 200 and "График сохранён" in response.text
    row = db.query_one("SELECT * FROM recruiters WHERE id = ?", (recruiter_id,))
    assert (row["timezone"], row["work_days"], row["work_start"], row["work_end"]) == ("Asia/Omsk", "2,4", "10:00",
                                                                                      "16:00")
    assert app_client.post("/app/profile", data={**data, "init_data": sign(fields_for(999))}).status_code == 403
    assert app_client.post("/app/profile", data={**data, "init_data": "bad"}).status_code == 401


def test_recruiter_moves_only_own_newcomer_in_the_mini_app(app_client):
    recruiter_id = add_recruiter(max_user_id=700)
    other = add_recruiter(name="Ира", max_user_id=701, code="ira")
    mine = add_employee(recruiter_id=recruiter_id, workplace_id=add_workplace("Завод GrPP"), code="a", max_user_id=501)
    theirs = add_employee(recruiter_id=other, code="b", max_user_id=502)
    gtp = add_workplace("Завод GTP", "GTP")
    init = sign(fields_for(700))

    assert app_client.post(f"/app/move/{mine}", data={"init_data": init, "workplace_id": str(gtp)}).status_code == 200
    assert db.query_one("SELECT workplace_id FROM employees WHERE id = ?", (mine,))["workplace_id"] == gtp
    assert app_client.post(f"/app/move/{theirs}", data={"init_data": init, "workplace_id": str(gtp)}).status_code == 403

    view = app_client.post("/app/view", data={"init_data": init}).text
    assert "Мой график" in view and "Сменить место работы" in view


def test_editing_a_workplace_moves_its_newcomers_to_the_new_tags_and_zone(admin):
    workplace = add_workplace("Завод", "GrPP", "Europe/Moscow")
    employee_id = add_employee(workplace_id=workplace, tags="GrPP, склад")
    form = {"workplace_id": str(workplace), "name": "Завод", "tags": "Luzhniki", "timezone": "Europe/Samara",
            "days": ["1", "2", "3", "4", "5"], "work_start": "08:00", "work_end": "17:00"}

    assert admin.post("/workplaces", data=form).status_code == 303

    employee = db.query_one("SELECT tags, timezone FROM employees WHERE id = ?", (employee_id,))
    assert employee["tags"] == "Luzhniki, склад" and employee["timezone"] == "Europe/Samara"


def test_newcomer_does_not_see_an_answer_that_waits_for_working_hours(app_client, clock):
    employee_id = add_employee(workplace_id=add_workplace(), max_user_id=500)
    escalation = add_question(employee_id, question="Где парковка?")
    db.execute("UPDATE escalations SET status = 'answered', answer = 'Секретный ответ' WHERE id = ?", (escalation,))

    view = app_client.post("/app/view", data={"init_data": sign(fields_for(500))}).text

    assert "Где парковка?" in view and "Секретный ответ" not in view
