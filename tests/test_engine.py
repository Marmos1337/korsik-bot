import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl
import pytest

from app import db, engine, max_api
from app.config import cfg

UTC = timezone.utc
HEADERS = ["Шаг", "Когда", "После", "Теги", "Тип", "Текст на «ты»", "Текст на «вы»", "Кнопки", "Плохой ответ",
           "Картинка", "Комментарий"]
REAL_SCENARIO = Path(__file__).resolve().parent.parent / "scenario" / "scenario.xlsx"

# start date in the tests is Monday 2026-11-02, Moscow is UTC+3 and there are no holidays
ROWS = [
    ("1", "старт", "", "", "сообщение", "Здравствуйте! Я Корсик."),
    ("2", "старт", "", "", "обращение", "На ты или на вы?", "", "На «ты» | На «вы»"),
    ("3", "старт", "", "", "сообщение", "Отлично, {имя}, на ты.", "Хорошо, {имя}, на вы."),
    ("10", "р1 10:00", "", "", "сообщение", "С первым днём, {имя}!", "С первым днём, {имя}! Рады вам."),
    ("11", "р1 10:00", "", "GTP", "сообщение", "Схема проезда до GTP"),
    ("12", "р1 12:30", "", "", "вопрос", "Подсказать, где обедать?", "", "Да! | Нет, спасибо"),
    ("12.1", "", "12: Да!", "GrPP", "сообщение", "Кафе рядом с офисом GrPP"),
    ("12.2", "", "12: Да!", "GTP", "сообщение", "Столовая GTP"),
    ("13", "р1 13:00", "", "", "сообщение", "Вот справочник коллег"),
    ("20", "р3 17:30", "", "", "шкала", "Оцени первые дни", "Оцените первые дни", "", "<8"),
    ("21", "р3 17:30", "", "", "опрос", "Задачи понятны?", "", "Да | Нет", "Нет"),
    ("22", "р3 17:30", "", "", "текст", "Что нам улучшить?"),
    ("23", "р3 17:30", "", "", "сообщение", "Спасибо за ответы!"),
    ("30", "+10д 10:00", "", "", "сообщение", "Десять дней с нами"),
    ("90", "напоминание", "", "", "сообщение", "{имя}, опрос остался незаконченным.",
     "{имя}, опрос остался незаконченным, ответьте, пожалуйста."),
    ("91", "эскалация", "", "", "сообщение", "Точного ответа нет, передам HR.", "", "", "", "esc.png"),
]

LINK_TIME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # Sunday, the day before the start
FIRST_DAY_10 = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
FIRST_DAY_1230 = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
FIRST_DAY_13 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
THIRD_DAY_1730 = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def write_scenario(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сценарий"
    ws.append(HEADERS)
    for row in rows:
        ws.append(list(row) + [""] * (len(HEADERS) - len(row)))
    wb.save(path)
    return path


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(write_scenario(tmp_path / "scenario.xlsx", ROWS)))
    monkeypatch.setattr(cfg, "HOLIDAYS_PATH", str(tmp_path / "no-holidays.txt"))
    monkeypatch.setattr(cfg, "DEMO_MODE", False)
    # engine tests run without hr.py: it is missing unless a test puts a fake in its place
    monkeypatch.setitem(sys.modules, "app.hr", None)
    assert engine.reload_scenario() == []


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(LINK_TIME)
    monkeypatch.setattr(engine, "utc_now", fake)
    return fake


@pytest.fixture
def outbox(monkeypatch):
    """What would go to MAX: a dict per message."""
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
def survey_summaries(monkeypatch):
    calls = []
    hr = types.ModuleType("app.hr")

    async def survey_finished(employee, step_ids):
        calls.append((employee["id"], step_ids))

    hr.survey_finished = survey_finished
    monkeypatch.setitem(sys.modules, "app.hr", hr)
    return calls


def texts(outbox):
    return [m["text"] for m in outbox]


def payloads(message):
    return [b.get("payload") for row in message["buttons"] or [] for b in row]


def make_employee(code="anna", tags="GrPP", tz="Europe/Moscow", start="2026-11-02"):
    return db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, invite_code, created_at)"
        " VALUES ('Смирнова Анна', 'Аня', ?, ?, ?, ?, ?)",
        (start, tz, tags, code, db.now_iso()),
    )


def employee(employee_id):
    return db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))


def statuses(employee_id):
    rows = db.query("SELECT step_id, status FROM sent_steps WHERE employee_id = ?", (employee_id,))
    return {r["step_id"]: r["status"] for r in rows}


def answers(employee_id):
    rows = db.query("SELECT step_id, answer, score FROM answers WHERE employee_id = ? ORDER BY id", (employee_id,))
    return [(r["step_id"], r["answer"], r["score"]) for r in rows]


def press(employee_id, payload):
    run(engine.on_callback(employee(employee_id), payload, "cb-" + payload))


def choose(clock, outbox, address="ty", at=LINK_TIME, **kw):
    """An employee opens the link at `at` and picks «ты» or «вы». The outbox keeps only what came after the choice."""
    employee_id = make_employee(**kw)
    clock.now = at
    assert run(engine.link_employee({"user_id": 500, "name": "Аня"}, "anna")) == "ok"
    outbox.clear()
    press(employee_id, "addr:" + address)
    return employee_id


def linked(clock, outbox, **kw):
    employee_id = choose(clock, outbox, **kw)
    outbox.clear()
    return employee_id


# --- linking


def test_link_by_unknown_code_or_by_second_account(clock, outbox):
    employee_id = make_employee()

    assert run(engine.link_employee({"user_id": 500}, "wrong")) == "unknown"
    assert outbox == []
    assert run(engine.link_employee({"user_id": 500}, "anna")) == "ok"
    sent_after_link = len(outbox)
    assert run(engine.link_employee({"user_id": 777}, "anna")) == "used"

    assert employee(employee_id)["max_user_id"] == 500
    assert len(outbox) == sent_after_link


def test_start_steps_wait_for_address_choice(clock, outbox):
    employee_id = make_employee()

    run(engine.link_employee({"user_id": 500}, "anna"))

    greeting, question = outbox
    assert greeting["user_id"] == 500
    assert greeting["text"].startswith("Здравствуйте! Я Корсик.")
    # spec 4.9: the first message says what is kept and how to delete it, still on «вы»
    assert "/delete" in greeting["text"] and "ваш" in greeting["text"] and "твои" not in greeting["text"]
    assert question["text"] == "На ты или на вы?"
    assert payloads(question) == ["addr:ty", "addr:vy"]
    assert statuses(employee_id) == {"1": "sent", "2": "sent"}

    outbox.clear()
    press(employee_id, "addr:ty")

    assert employee(employee_id)["address"] == "ty"
    assert texts(outbox) == ["Отлично, Аня, на ты."]


def test_vy_choice_changes_texts(clock, outbox):
    employee_id = make_employee()
    run(engine.link_employee({"user_id": 500}, "anna"))
    outbox.clear()

    press(employee_id, "addr:vy")
    clock.now = FIRST_DAY_10
    run(engine.tick())

    assert texts(outbox) == ["Хорошо, Аня, на вы.", "С первым днём, Аня! Рады вам."]


def test_real_scenario_start(clock, outbox, monkeypatch):
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(REAL_SCENARIO))
    assert engine.reload_scenario() == []
    employee_id = make_employee(tags="GrPP", start="2026-11-09")

    run(engine.link_employee({"user_id": 500}, "anna"))
    greeting, question = texts(outbox)
    assert greeting.startswith("Здравствуйте! Я Корсик")
    assert question.endswith("на «ты» или на «вы»?")
    outbox.clear()
    press(employee_id, "addr:vy")

    assert outbox[0]["text"] == "Хорошо, Аня, тогда на «вы». Рад знакомству!"
    sent = statuses(employee_id)
    # GrPP gets its documents list (9) and transport (11), not the GTP (10, 12) or Luzhniki (13) ones
    assert {"4", "5", "7", "8", "9", "11", "14", "15"} == set(sent)


# --- schedule


def test_tick_sends_step_once_when_due_for_matching_tags(clock, outbox):
    linked(clock, outbox, tags="GrPP")

    clock.now = FIRST_DAY_10 - timedelta(minutes=1)
    assert run(engine.tick()) == 0
    clock.now = FIRST_DAY_10
    assert run(engine.tick()) == 1
    assert run(engine.tick()) == 0

    assert texts(outbox) == ["С первым днём, Аня!"]  # step 11 is for GTP only


def test_time_is_counted_in_employee_timezone(clock, outbox):
    linked(clock, outbox, tz="Asia/Novosibirsk", tags="GTP")

    clock.now = datetime(2026, 11, 2, 2, 59, tzinfo=UTC)  # 09:59 in Novosibirsk
    run(engine.tick())
    assert outbox == []
    clock.now = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    run(engine.tick())
    assert texts(outbox) == ["С первым днём, Аня!", "Схема проезда до GTP"]


def test_step_overdue_at_linking_is_skipped_except_start(clock, outbox):
    # linked on Tuesday 11:00: step 10 (Mon 10:00) is 25 hours late, step 12 (Mon 12:30) is 22.5 hours late
    employee_id = linked(clock, outbox, at=datetime(2026, 11, 3, 8, 0, tzinfo=UTC))

    assert statuses(employee_id) == {
        "1": "sent", "2": "answered", "3": "sent", "10": "skipped", "12": "sent", "13": "sent"}


def test_survey_step_blocks_then_reminds_once_then_is_skipped(clock, outbox):
    employee_id = choose(clock, outbox, at=THIRD_DAY_1730)
    assert texts(outbox) == ["Отлично, Аня, на ты.", "Оцени первые дни"]
    rating = outbox[-1]
    outbox.clear()

    clock.now = THIRD_DAY_1730 + timedelta(hours=1)
    run(engine.tick())
    assert outbox == []  # step 21 waits for the rating

    clock.now = THIRD_DAY_1730 + timedelta(hours=24)
    run(engine.tick())
    run(engine.tick())
    assert texts(outbox) == ["Аня, опрос остался незаконченным."]
    assert payloads(outbox[0]) == payloads(rating)
    assert statuses(employee_id)["20"] == "reminded"
    outbox.clear()

    clock.now = THIRD_DAY_1730 + timedelta(hours=48)
    run(engine.tick())
    assert outbox == []
    assert {k: v for k, v in statuses(employee_id).items() if k in ("20", "21", "22", "23")} == {
        "20": "skipped", "21": "skipped", "22": "skipped", "23": "skipped"}


def test_question_step_holds_nothing_and_its_buttons_work_later(clock, outbox):
    # D01: branches of a «вопрос» are optional, so the day goes on without the answer
    employee_id = linked(clock, outbox, tags="GrPP")
    clock.now = FIRST_DAY_1230
    run(engine.tick())
    outbox.clear()

    clock.now = FIRST_DAY_13
    run(engine.tick())
    assert texts(outbox) == ["Вот справочник коллег"]
    outbox.clear()

    clock.now = FIRST_DAY_1230 + timedelta(hours=30)
    run(engine.tick())
    assert outbox == []  # no «опрос не закончен» for a question

    press(employee_id, "step:12:0")
    assert texts(outbox) == ["Кафе рядом с офисом GrPP"]
    assert statuses(employee_id)["12"] == "answered"


def test_branch_button_sends_branch_for_tags(clock, outbox):
    employee_id = linked(clock, outbox, tags="GrPP")
    clock.now = FIRST_DAY_1230
    run(engine.tick())
    outbox.clear()

    clock.now = FIRST_DAY_13 + timedelta(minutes=5)
    press(employee_id, "step:12:0")
    assert texts(outbox) == ["Кафе рядом с офисом GrPP", "Вот справочник коллег"]
    assert statuses(employee_id)["12"] == "answered"

    outbox.clear()
    press(employee_id, "step:12:0")  # the same button again
    assert outbox == []


# --- surveys


def test_survey_answers_and_what_went_wrong(clock, outbox, survey_summaries):
    employee_id = choose(clock, outbox, at=THIRD_DAY_1730)
    # linked late: the first day steps are skipped, the survey is due right now
    assert texts(outbox) == ["Отлично, Аня, на ты.", "Оцени первые дни"]
    assert payloads(outbox[1]) == [f"scale:20:{n}" for n in range(1, 11)]
    outbox.clear()

    press(employee_id, "scale:20:5")
    assert texts(outbox) == ["Расскажи, пожалуйста, что не так? Так мы поймём, что исправить."]
    outbox.clear()
    assert run(engine.on_text(employee(employee_id), "Мало обратной связи"))
    assert texts(outbox) == ["Задачи понятны?"]
    outbox.clear()

    press(employee_id, "step:21:1")  # «Нет» is a bad answer
    assert run(engine.on_text(employee(employee_id), "Не знаю, с чего начать"))
    assert texts(outbox)[-1] == "Что нам улучшить?"
    assert survey_summaries == []
    outbox.clear()

    assert run(engine.on_text(employee(employee_id), "Больше обучения"))
    assert texts(outbox) == ["Спасибо за ответы!"]
    assert answers(employee_id) == [
        ("20", "5", 5), ("20", "Мало обратной связи", None),
        ("21", "Нет", None), ("21", "Не знаю, с чего начать", None),
        ("22", "Больше обучения", None),
    ]
    assert survey_summaries == [(employee_id, ["20", "21", "22"])]
    assert not run(engine.on_text(employee(employee_id), "А где столовая?"))


def test_survey_works_without_hr_module(clock, outbox, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.hr", None)  # import of app.hr fails as if the file did not exist
    employee_id = linked(clock, outbox, at=THIRD_DAY_1730)
    press(employee_id, "scale:20:9")
    press(employee_id, "step:21:0")
    assert run(engine.on_text(employee(employee_id), "Всё хорошо"))
    assert statuses(employee_id)["23"] == "sent"


def test_text_answer_without_follow_up_gets_thanks(clock, outbox, monkeypatch):
    rows = ROWS[:3] + [("40", "р2 10:00", "", "", "текст", "Расскажи о себе", "Расскажите о себе")]
    write_scenario(cfg.SCENARIO_PATH, rows)
    assert engine.reload_scenario() == []
    employee_id = choose(clock, outbox, address="vy", at=datetime(2026, 11, 3, 7, 0, tzinfo=UTC))
    assert texts(outbox) == ["Хорошо, Аня, на вы.", "Расскажите о себе"]
    outbox.clear()

    assert run(engine.on_text(employee(employee_id), "Я инженер"))
    assert texts(outbox) == ["Спасибо, записал."]
    assert answers(employee_id) == [("40", "Я инженер", None)]


# --- demo, special steps, delete


def test_demo_next_moves_time_to_the_next_step(clock, outbox, monkeypatch):
    employee_id = linked(clock, outbox)
    assert run(engine.demo_next(employee_id)) is None  # DEMO_MODE is off
    assert employee(employee_id)["time_shift"] == 0

    monkeypatch.setattr(cfg, "DEMO_MODE", True)
    assert run(engine.demo_next(employee_id)) == "10"
    assert employee(employee_id)["time_shift"] == 19 * 3600  # Sun 12:00 UTC to Mon 07:00 UTC
    assert run(engine.tick()) == 1  # step 10; step 12 at 12:30 is still in the future
    assert texts(outbox) == ["С первым днём, Аня!"]


def test_demo_next_while_waiting_jumps_to_reminder(clock, outbox, monkeypatch):
    monkeypatch.setattr(cfg, "DEMO_MODE", True)
    employee_id = linked(clock, outbox, at=THIRD_DAY_1730)
    outbox.clear()

    assert run(engine.demo_next(employee_id)) == "20"
    run(engine.tick())
    assert texts(outbox) == ["Аня, опрос остался незаконченным."]


def test_special_steps(clock, outbox):
    employee_id = linked(clock, outbox)

    assert run(engine.send_special(employee(employee_id), "эскалация")) is True
    assert run(engine.send_special(employee(employee_id), "нет такого")) is False
    assert texts(outbox) == ["Точного ответа нет, передам HR."]  # esc.png is missing, the text goes alone


def test_delete_after_confirmation(clock, outbox):
    employee_id = linked(clock, outbox)
    now = db.now_iso()
    db.execute("INSERT INTO answers (employee_id, step_id, question, answer, created_at) VALUES (?, '20', 'q', 'a', ?)",
               (employee_id, now))
    db.execute("INSERT INTO pulse (employee_id, text, tone, topic, risk, created_at) VALUES (?, 't', 'ok', 'другое', 0, ?)",
               (employee_id, now))
    db.execute("INSERT INTO escalations (employee_id, question, context, created_at) VALUES (?, 'q', '[]', ?)",
               (employee_id, now))

    run(engine.ask_delete(employee(employee_id)))
    assert payloads(outbox[-1]) == ["del:yes", "del:no"]
    press(employee_id, "del:no")
    assert db.query("SELECT 1 FROM answers WHERE employee_id = ?", (employee_id,))

    outbox.clear()
    press(employee_id, "del:yes")

    for table in ("messages", "answers", "pulse", "escalations", "sent_steps"):
        assert db.query(f"SELECT 1 FROM {table} WHERE employee_id = ?", (employee_id,)) == [], table
    row = employee(employee_id)
    assert (row["max_user_id"], row["linked_at"], row["address"], row["waiting"]) == (None, None, None, None)
    assert [m["user_id"] for m in outbox] == [500]
    assert "удален" in outbox[0]["text"]


def test_failed_callback_answer_does_not_stop_the_press(clock, outbox, monkeypatch):
    employee_id = make_employee()
    run(engine.link_employee({"user_id": 500}, "anna"))

    async def broken_answer(callback_id, notification=None):
        raise max_api.MaxError(400, "callback is too old")

    monkeypatch.setattr(max_api, "answer_callback", broken_answer)
    press(employee_id, "addr:vy")

    assert employee(employee_id)["address"] == "vy"
    assert texts(outbox)[-1] == "Хорошо, Аня, на вы."


def test_broken_scenario_keeps_the_old_one(clock, outbox, tmp_path, monkeypatch):
    broken = write_scenario(tmp_path / "broken.xlsx", [("1", "когда-нибудь", "", "", "сообщение", "Привет")])
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(broken))

    assert engine.reload_scenario()
    make_employee()
    run(engine.link_employee({"user_id": 500}, "anna"))
    assert outbox[0]["text"].startswith("Здравствуйте! Я Корсик.")


def test_invite_link_needs_bot_name(monkeypatch):
    monkeypatch.setattr(cfg, "BOT_USERNAME", "")
    assert engine.invite_link("abc") is None
    monkeypatch.setattr(cfg, "BOT_USERNAME", "korsik_bot")
    assert engine.invite_link("abc") == "https://max.ru/korsik_bot?start=abc"


def test_parallel_ticks_and_presses_send_each_step_once(clock, monkeypatch):
    # the bot handles every update in its own task now, so the scheduler and presses really overlap
    sent = []

    async def yielding_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        await asyncio.sleep(0)  # a real request gives the loop to other tasks
        sent.append(text)
        return {}

    async def fake_answer(callback_id, notification=None):
        pass

    monkeypatch.setattr(max_api, "send_message", yielding_send)
    monkeypatch.setattr(max_api, "answer_callback", fake_answer)
    employee_id = make_employee()
    run(engine.link_employee({"user_id": 500}, "anna"))
    clock.now = FIRST_DAY_13
    sent.clear()

    async def together():
        await asyncio.gather(engine.tick(), engine.tick(), engine.advance(employee_id),
                             engine.on_callback(employee(employee_id), "addr:ty", "c1"))

    run(together())

    assert sorted(sent) == sorted(set(sent))
    assert {"Отлично, Аня, на ты.", "С первым днём, Аня!", "Вот справочник коллег"} <= set(sent)
