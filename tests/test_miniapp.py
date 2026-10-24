import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlencode

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import db, engine, hr, max_api, miniapp, web
from app.config import cfg

TOKEN = "fake-bot-token"

# initData signed by the Node code of another MAX mini app already in production
# (keys/server/auth.mjs, validateInitData accepts it), token "vector-token-123"
NODE_VECTOR = (
    "auth_date=1761220800&query_id=q-1&user=%7B%22id%22%3A500%2C%22first_name%22%3A%22%D0%9E%D0%BB%D1%8C%D0%B3"
    "%D0%B0%22%2C%22last_name%22%3A%22%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2%D0%B0%22%7D&start_param=x"
    "&hash=d9562df51b5deb91f44d81efc210bee528c796524460d7c4e754571169bdebfa"
)


def sign(fields: dict, token: str = TOKEN) -> str:
    """initData as MAX builds it: HMAC of the sorted «key=value» lines with a secret made from the bot token."""
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def fields_for(user_id=500, auth_date=None, user=None) -> dict:
    return {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "q-1",
        "user": user if user is not None else json.dumps({"id": user_id, "first_name": "Ольга"}, ensure_ascii=False),
    }


def test_init_data_signed_by_the_working_node_code_is_accepted():
    assert miniapp.check_init_data(NODE_VECTOR, "vector-token-123", now=1761220800 + 60) == 500


def test_freshly_signed_init_data_gives_the_user_id():
    assert miniapp.check_init_data(sign(fields_for(777)), TOKEN) == 777


@pytest.mark.parametrize("raw", [
    NODE_VECTOR.replace("hash=d9", "hash=e9"),                   # wrong hash
    NODE_VECTOR.replace("%22id%22%3A500", "%22id%22%3A501"),     # user changed after signing
    NODE_VECTOR.replace("&hash=", "&query_id=q-2&hash="),        # repeated key
    NODE_VECTOR.split("&hash=")[0],                              # no hash
    NODE_VECTOR.split("&hash=")[0] + "&hash=xyz",                # hash is not 64 hex
    NODE_VECTOR + "&hash=d9562df51b5deb91f44d81efc210bee528c796524460d7c4e754571169bdebfa",
    "",
    "мусор",
])
def test_broken_or_tampered_init_data_is_refused(raw):
    with pytest.raises(ValueError):
        miniapp.check_init_data(raw, "vector-token-123", now=1761220800 + 60)


def test_init_data_signed_with_another_token_is_refused():
    with pytest.raises(ValueError):
        miniapp.check_init_data(sign(fields_for(), token="other-token"), TOKEN)


def test_empty_bot_token_never_matches():
    # anybody can count an HMAC with an empty key, so an unset token must lock the mini app
    with pytest.raises(ValueError):
        miniapp.check_init_data(sign(fields_for(), token=""), "")


@pytest.mark.parametrize("age, ok", [(0, True), (3500, True), (3601, False), (-29, True), (-31, False)])
def test_auth_date_lives_one_hour_and_may_run_30_seconds_ahead(age, ok):
    now = 1761220800
    raw = sign(fields_for(auth_date=now - age))
    if ok:
        assert miniapp.check_init_data(raw, TOKEN, now=now) == 500
    else:
        with pytest.raises(ValueError):
            miniapp.check_init_data(raw, TOKEN, now=now)


@pytest.mark.parametrize("auth_date", ["", "abc", "1.5", "１７６１２２０８００"])
def test_auth_date_that_is_not_a_number_is_refused(auth_date):
    with pytest.raises(ValueError):
        miniapp.check_init_data(sign(fields_for(auth_date=auth_date)), TOKEN)


@pytest.mark.parametrize("user", ["не json", "[]", "null", '{"first_name": "Ольга"}', '{"id": -5}', '{"id": 0}',
                                  '{"id": "500"}', '{"id": true}', '{"id": 5.5}'])
def test_user_without_a_positive_whole_id_is_refused(user):
    with pytest.raises(ValueError):
        miniapp.check_init_data(sign(fields_for(user=user)), TOKEN)


def test_missing_user_is_refused():
    fields = fields_for()
    del fields["user"]
    with pytest.raises(ValueError):
        miniapp.check_init_data(sign(fields), TOKEN)


# the mini app over HTTP


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MAX_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(cfg, "SCENARIO_PATH", str(write_scenario(tmp_path / "scenario.xlsx", ROWS)))
    monkeypatch.setattr(cfg, "HOLIDAYS_PATH", str(tmp_path / "no-holidays.txt"))
    monkeypatch.setattr(cfg, "DEMO_MODE", False)
    assert engine.reload_scenario() == []
    return TestClient(web.app, follow_redirects=False)


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(text, user_id=None, chat_id=None, buttons=None, image_token=None):
        sent.append({"user_id": user_id, "text": text})
        return {}

    monkeypatch.setattr(max_api, "send_message", fake_send)
    return sent


HEADERS = ["Шаг", "Когда", "После", "Теги", "Тип", "Текст на «ты»", "Текст на «вы»", "Кнопки", "Плохой ответ",
           "Картинка", "Комментарий"]
ROWS = [
    ("10", "р1 10:00", "", "", "сообщение", "С первым днём, {имя}!", "С первым днём, {имя}! Рады вам."),
    ("20", "р3 17:30", "", "", "сообщение", "Как прошли первые дни, {имя}?"),
]


def write_scenario(path, rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Сценарий"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(list(row) + [""] * (len(HEADERS) - len(row)))
    workbook.save(path)
    return path


def add_recruiter(name, max_user_id, code):
    return db.execute("INSERT INTO recruiters (name, max_user_id, invite_code, created_at) VALUES (?, ?, ?, ?)",
                      (name, max_user_id, code, db.now_iso()))


def add_employee(name, recruiter_id, max_user_id, code, address="ty", tz="Asia/Novosibirsk",
                 start="2026-11-02", linked_at="2026-11-01T12:00:00+00:00"):
    return db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, recruiter_id, invite_code,"
        " max_user_id, address, linked_at, created_at) VALUES (?, ?, ?, ?, 'GrPP', ?, ?, ?, ?, ?, ?)",
        (name, name.split()[-1], start, tz, recruiter_id, code, max_user_id, address, linked_at, db.now_iso()),
    )


def add_question(employee_id, question, status="open", answer=None):
    return db.execute(
        "INSERT INTO escalations (employee_id, question, context, status, answer, created_at, delivered_at)"
        " VALUES (?, ?, '[]', ?, ?, ?, ?)",
        (employee_id, question, status, answer, db.now_iso(), db.now_iso() if status == "answered" else None))


def add_flag(employee_id, text):
    db.execute("INSERT INTO pulse (employee_id, text, tone, topic, risk, reason, created_at)"
               " VALUES (?, ?, 'негативный', 'нагрузка', 2, '', ?)", (employee_id, text, db.now_iso()))


@pytest.fixture
def team():
    """Two recruiters with a newcomer each, everybody linked to MAX."""
    olga = add_recruiter("Ольга", 100, "olga")
    igor = add_recruiter("Игорь", 200, "igor")
    anna = add_employee("Смирнова Анна", olga, 500, "anna")
    petr = add_employee("Иванов Пётр", igor, 600, "petr", address="vy")
    add_question(anna, "Где взять пропуск?")
    add_question(petr, "Когда аванс?")
    add_flag(anna, "Не справляюсь с нагрузкой")
    add_flag(petr, "Хочу уйти из компании")
    return {"olga": olga, "igor": igor, "anna": anna, "petr": petr}


def view_as(client, user_id):
    return client.post("/app/view", data={"init_data": sign(fields_for(user_id))})


def answer_form(page, question_id):
    form = page[page.index(f'action="/app/answer/{question_id}"'):]
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', form[:form.index("</form>")]))


def question_id(text):
    return db.query_one("SELECT id FROM escalations WHERE question = ?", (text,))["id"]


def test_page_runs_only_max_script_and_its_own_and_opens_inside_max(client):
    response = client.get("/app")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    script = response.text.split("<script>")[1].split("</script>")[0]
    own = "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"
    script_src = next(part.split()[1:] for part in policy.split(";") if part.split()[0] == "script-src")
    assert script_src == ["https://st.max.ru", own]
    assert "default-src 'none'" in policy and "connect-src 'self'" in policy
    assert "frame-ancestors https://max.ru https://*.max.ru" in policy
    assert '<script src="https://st.max.ru/js/max-web-app.js"></script>' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_cabinet_pages_keep_their_policy(client):
    for response in (client.get("/login"), client.get("/health"), client.get("/")):
        policy = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in policy and "script-src" not in policy
    assert client.get("/").headers["location"] == "/login"


def test_without_valid_init_data_nothing_is_shown(client, team, outbox):
    expired = sign(fields_for(100, auth_date=int(time.time()) - 7200))
    forged = sign(fields_for(100), token="other-token")
    for data in ({}, {"init_data": ""}, {"init_data": expired}, {"init_data": forged},
                 {"init_data": sign(fields_for(100)).replace("q-1", "q-2")}):
        for url in ("/app/view", f"/app/answer/{question_id('Где взять пропуск?')}"):
            response = client.post(url, data={**data, "answer": "Ответ", "employee_id": team["anna"]})
            assert response.status_code == 401, (url, data)
            assert len(response.text) < 200
            for secret in ("Анна", "пропуск", "Ольга", "нагрузк"):
                assert secret not in response.text
    assert "устарел" in client.post("/app/view", data={"init_data": expired}).text
    assert outbox == []
    assert db.query_one("SELECT status FROM escalations WHERE question = 'Где взять пропуск?'")["status"] == "open"


def test_recruiter_sees_only_own_newcomers_their_questions_and_digest(client, team):
    response = view_as(client, 100)
    assert response.status_code == 200
    page = response.text
    assert "Смирнова Анна" in page and "Где взять пропуск?" in page and "Не справляюсь с нагрузкой" in page
    assert "Сводка по вашим новичкам: 1" in page
    assert "Иванов Пётр" not in page and "Когда аванс?" not in page and "Хочу уйти" not in page
    assert f'action="/app/answer/{question_id("Где взять пропуск?")}"' in page


def test_recruiter_answer_goes_through_deliver_answer_to_the_newcomer(client, team, outbox):
    page = view_as(client, 100).text
    number = question_id("Где взять пропуск?")
    form = answer_form(page, number)
    assert form == {"employee_id": str(team["anna"]),
                    "asked_at": db.query_one("SELECT created_at FROM escalations WHERE id = ?", (number,))["created_at"]}

    response = client.post(f"/app/answer/{number}",
                           data={**form, "answer": "В бюро пропусков, 2 этаж", "init_data": sign(fields_for(100))})

    assert response.status_code == 200 and "Ответ отправлен новичку" in response.text
    assert len(outbox) == 1 and outbox[0]["user_id"] == 500 and "В бюро пропусков, 2 этаж" in outbox[0]["text"]
    row = db.query_one("SELECT * FROM escalations WHERE id = ?", (number,))
    assert (row["status"], row["answer"], row["answered_by"]) == ("answered", "В бюро пропусков, 2 этаж", "Ольга")
    assert "Открытых вопросов нет" in response.text

    again = client.post(f"/app/answer/{number}", data={**form, "answer": "Ещё раз", "init_data": sign(fields_for(100))})
    assert "На этот вопрос уже ответили (Ольга)" in again.text and len(outbox) == 1


def test_another_recruiters_question_does_not_open(client, team, outbox):
    number = question_id("Когда аванс?")
    asked_at = db.query_one("SELECT created_at FROM escalations WHERE id = ?", (number,))["created_at"]
    response = client.post(f"/app/answer/{number}", data={
        "init_data": sign(fields_for(100)), "employee_id": team["petr"], "asked_at": asked_at, "answer": "Завтра"})
    assert response.status_code == 403 and hr.NOT_YOUR_QUESTION in response.text
    assert "Когда аванс?" not in response.text and "Иванов Пётр" not in response.text
    assert outbox == [] and db.query_one("SELECT status FROM escalations WHERE id = ?", (number,))["status"] == "open"


def test_answer_with_a_wrong_newcomer_or_time_is_refused(client, team, outbox):
    number = question_id("Где взять пропуск?")
    form = answer_form(view_as(client, 100).text, number)
    for wrong in ({**form, "asked_at": "2026-01-01T00:00:00+00:00"}, {**form, "employee_id": str(team["petr"])}, {}):
        response = client.post(f"/app/answer/{number}",
                               data={**wrong, "answer": "Ответ", "init_data": sign(fields_for(100))})
        assert hr.QUESTION_GONE in response.text or hr.NOT_YOUR_QUESTION in response.text
    assert outbox == [] and db.query_one("SELECT status FROM escalations WHERE id = ?", (number,))["status"] == "open"


def test_answer_that_max_did_not_take_stays_in_the_form(client, team, monkeypatch):
    async def broken_send(*args, **kwargs):
        raise max_api.MaxError(503, "unavailable")

    monkeypatch.setattr(max_api, "send_message", broken_send)
    number = question_id("Где взять пропуск?")
    form = answer_form(view_as(client, 100).text, number)
    response = client.post(f"/app/answer/{number}",
                           data={**form, "answer": "В бюро пропусков", "init_data": sign(fields_for(100))})
    assert "Не получилось отправить" in response.text and ">В бюро пропусков</textarea>" in response.text
    assert db.query_one("SELECT status FROM escalations WHERE id = ?", (number,))["status"] == "open"


def test_newcomer_cannot_answer_questions(client, team, outbox):
    number = question_id("Где взять пропуск?")
    form = answer_form(view_as(client, 100).text, number)
    response = client.post(f"/app/answer/{number}", data={**form, "answer": "Сам", "init_data": sign(fields_for(500))})
    assert response.status_code == 403 and miniapp.ONLY_RECRUITERS in response.text
    assert outbox == [] and db.query_one("SELECT status FROM escalations WHERE id = ?", (number,))["status"] == "open"


def test_newcomer_sees_own_progress_next_step_and_questions(client, team):
    add_question(team["anna"], "Где столовая?", status="answered", answer="На первом этаже")
    db.execute("INSERT INTO sent_steps (employee_id, step_id, status, sent_at) VALUES (?, '10', 'sent', ?)",
               (team["anna"], db.now_iso()))
    page = view_as(client, 500).text
    assert "Твой прогресс" in page and "Пройдено шагов: 1" in page
    # step 20 is on the third day at 17:30 in Novosibirsk, start Monday 2026-11-02
    assert "04.11 в 17:30" in page and "Как прошли первые дни, Анна?" in page
    assert "Где взять пропуск?" in page and "ждёт ответа" in page
    assert "Где столовая?" in page and "На первом этаже" in page
    assert "Иванов Пётр" not in page and "Когда аванс?" not in page and "Не справляюсь" not in page
    assert "Вопросы новичков" not in page


def test_newcomer_on_vy_gets_vy_texts_and_a_waiting_step_is_named(client, team):
    db.execute("UPDATE employees SET waiting = ? WHERE id = ?",
               (json.dumps({"step": "10", "kind": "step", "asked_at": "2026-11-02T07:00:00+00:00"}), team["petr"]))
    page = view_as(client, 600).text
    assert "Ваш прогресс" in page and "Бот ждёт вашего ответа" in page and "Когда аванс?" in page
    assert "Где взять пропуск?" not in page


def test_stranger_only_gets_the_text_about_the_link(client, team):
    page = view_as(client, 999).text
    assert "по личной ссылке от рекрутера" in page
    for secret in ("Анна", "Пётр", "пропуск", "аванс", "Сводка"):
        assert secret not in page


def test_newcomer_who_deleted_the_data_is_a_stranger(client, team):
    engine.delete_employee_data(team["anna"])
    page = view_as(client, 500).text
    assert "по личной ссылке от рекрутера" in page and "Анна" not in page
