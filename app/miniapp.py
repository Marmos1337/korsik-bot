import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import jinja2
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse
from markupsafe import escape

from app import care, db, engine, hours, hr, learning, managers, risk, tasks
from app.config import cfg
from app.scenario import render_text

log = logging.getLogger(__name__)

MAX_AGE = 3600  # MAX advises to trust initData for an hour
CLOCK_SKEW = 30  # a phone clock may run a little ahead of the server
MAX_LENGTH = 16384
OPEN_FROM_MAX = "Откройте приложение из МАКС."
NOT_CONFIRMED = "Не получилось подтвердить вход через МАКС. Закройте приложение и откройте его снова."
EXPIRED = "Вход устарел. Закройте приложение и откройте его в МАКС снова."
ONLY_RECRUITERS = "Отвечать на вопросы могут только рекрутеры."

router = APIRouter()

templates = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
)


def local_time(stamp: str | None, tz: str | None = None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).astimezone(ZoneInfo(tz or cfg.DEFAULT_TZ)).strftime("%d.%m %H:%M")
    except (ValueError, KeyError):
        return stamp


templates.filters["local"] = local_time
templates.filters["day"] = lambda stamp: datetime.fromisoformat(stamp).strftime("%d.%m") if stamp else ""
templates.globals["risk_levels"] = risk.LEVELS
templates.filters["initials"] = lambda name: "".join(w[0] for w in (name or "").split() if w[0].isalpha())[:2].upper() or "?"
CSS_FILE = Path(__file__).parent / "static" / "korsik.css"


def css_url() -> str:
    # the file's change time in the address: browsers keep static files for a day, a new version must not wait
    return f"/static/korsik.css?v={int(CSS_FILE.stat().st_mtime)}"


templates.globals["css"] = css_url

# The page itself has no data, so it is rendered once. Its policy lets in MAX's bridge and this one inline
# script by its hash: nothing else can run on the page, even if some text slipped past the escaping.
PAGE = templates.get_template("miniapp.html").render()
SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(PAGE.split("<script>")[1].split("</script>")[0].encode()).digest()).decode()
POLICY = (f"default-src 'none'; script-src https://st.max.ru 'sha256-{SCRIPT_HASH}'; connect-src 'self'; "
          "img-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; form-action 'self'; base-uri 'none'; "
          "frame-ancestors https://max.ru https://*.max.ru")


def check_init_data(raw: str, token: str, now: int | None = None) -> int:
    """MAX user id from the signed initData of the mini app. ValueError with a text for people otherwise."""
    if not raw or not token or len(raw) > MAX_LENGTH:
        raise ValueError(OPEN_FROM_MAX)
    # parsed as loosely as URLSearchParams in the MAX docs: the signature decides, not the parser
    pairs = parse_qsl(raw, keep_blank_values=True)
    fields = dict(pairs)
    # with a repeated key the signature and the code below could look at different values
    if len(fields) != len(pairs):
        raise ValueError(NOT_CONFIRMED)
    received = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", received):
        raise ValueError(NOT_CONFIRMED)
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        raise ValueError(NOT_CONFIRMED)

    stamp = fields.get("auth_date", "")
    if not (stamp.isascii() and stamp.isdigit()):
        raise ValueError(NOT_CONFIRMED)
    now = int(time.time()) if now is None else now
    if not now - MAX_AGE <= int(stamp) <= now + CLOCK_SKEW:
        raise ValueError(EXPIRED)

    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        user = None
    user_id = user.get("id") if isinstance(user, dict) else None
    # True is an int in Python, so the type is checked exactly
    if type(user_id) is not int or user_id <= 0:
        raise ValueError(NOT_CONFIRMED)
    return user_id


@router.get("/app")
def app_page():
    return HTMLResponse(PAGE)


TABS = ("home", "tasks", "learning", "questions")


@router.post("/app/view")
def show_view(init_data: str = Form(""), tab: str = Form("home"), lesson: int = Form(0)):
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    return view(user_id, tab=tab, lesson=lesson)


@router.post("/app/task/{task_id}")
def tick_task(task_id: int, init_data: str = Form(""), done: str = Form("1")):
    """The newcomer ticks a task of their own checklist."""
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    employee = db.query_one("SELECT * FROM employees WHERE max_user_id = ?", (user_id,))
    if employee is None or not tasks.set_done(task_id, employee["id"], done == "1", "новичок"):
        return view(user_id, "Эту задачу отметить нельзя.", status=403, tab="tasks")
    return view(user_id, tab="tasks")


@router.post("/app/tasks/{employee_id}")
async def give_task(employee_id: int, init_data: str = Form(""), title: str = Form(""), due: str = Form("")):
    """A recruiter or a manager gives their own newcomer a task."""
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    who = _boss_of(user_id, employee_id)
    if who is None:
        return view(user_id, "Ставить задачи можно только своим новичкам.", status=403)
    employee = db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    try:
        await tasks.add(employee, title, due, who)
    except ValueError as e:
        return view(user_id, str(e))
    return view(user_id, f"{employee['full_name']}: задача добавлена.", bad=False)


@router.post("/app/talk")
async def want_to_talk(init_data: str = Form("")):
    """«Мне тяжело» in the app: the bot writes in the chat and calls the recruiter in."""
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    employee = db.query_one("SELECT * FROM employees WHERE max_user_id = ?", (user_id,))
    if employee is None:
        return view(user_id, "Эта кнопка для новичков.", status=403)
    await care.start_talk(employee, worried=True)
    ty = employee["address"] == "ty"
    return view(user_id, f"Я написал {'тебе' if ty else 'вам'} в чат. {'Расскажи' if ty else 'Расскажите'}, "
                         "что случилось, рекрутер тоже уже в курсе.", bad=False)


@router.post("/app/lesson/{lesson_id}")
def answer_lesson(lesson_id: int, init_data: str = Form(""), choice: str = Form("")):
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    employee = db.query_one("SELECT * FROM employees WHERE max_user_id = ?", (user_id,))
    if employee is None:
        return view(user_id, "Обучение доступно только новичкам.", status=403)
    try:
        passed = learning.answer(employee, lesson_id, int(choice) if choice.isdigit() else None)
    except ValueError as e:
        return view(user_id, str(e), tab="learning")
    if passed:
        return view(user_id, "Верно! Урок пройден.", bad=False, tab="learning")
    return view(user_id, "Не совсем. Перечитайте урок и попробуйте ещё раз.", tab="learning", lesson=lesson_id)


def _boss_of(user_id: int, employee_id: int) -> str | None:
    recruiter = db.query_one("SELECT r.name FROM recruiters r JOIN employees e ON e.recruiter_id = r.id"
                             " WHERE r.max_user_id = ? AND e.id = ?", (user_id, employee_id))
    if recruiter:
        return f"Рекрутер {recruiter['name']}"
    manager = db.query_one("SELECT m.name FROM managers m JOIN employees e ON e.manager_id = m.id"
                           " WHERE m.max_user_id = ? AND e.id = ?", (user_id, employee_id))
    return f"Руководитель {manager['name']}" if manager else None


@router.post("/app/answer/{escalation_id}")
async def answer_question(escalation_id: int, init_data: str = Form(""), answer: str = Form(""),
                          employee_id: int = Form(0), asked_at: str = Form("")):
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    recruiter = db.query_one("SELECT * FROM recruiters WHERE max_user_id = ?", (user_id,))
    if recruiter is None:
        return view(user_id, ONLY_RECRUITERS, status=403)
    # the same three fields as in the cabinet: a question number alone may already belong to another question
    question = db.query_one("SELECT e.recruiter_id, e.max_user_id FROM escalations q"
                            " JOIN employees e ON e.id = q.employee_id"
                            " WHERE q.id = ? AND q.employee_id = ? AND q.created_at = ?",
                            (escalation_id, employee_id, asked_at))
    if question is not None and question["recruiter_id"] != recruiter["id"]:
        return view(user_id, hr.NOT_YOUR_QUESTION, status=403)
    answer = answer.strip()
    if not answer:
        return view(user_id, "Впишите ответ.")
    if question is not None and not question["max_user_id"]:
        return view(user_id, "Новичок удалил свои данные или ещё не открыл ссылку. Ответить некому.")
    try:
        later = await hr.deliver_answer(escalation_id, answer, recruiter["name"], employee_id, asked_at)
    except ValueError as e:
        return view(user_id, str(e))
    except Exception as e:
        # deliver_answer has already reopened the question, so the text stays in the form for another try
        log.warning("escalation %s: answer from the mini app was not sent: %s", escalation_id, type(e).__name__)
        return view(user_id, "Не получилось отправить ответ в МАКС. Вопрос остался открытым, попробуйте ещё раз.",
                    typed=(escalation_id, answer))
    if later is not None:
        employee = db.query_one("SELECT timezone FROM employees WHERE id = ?", (employee_id,))
        return view(user_id, "Ответ принят. У новичка сейчас нерабочее время, он получит ответ "
                             f"{hours.when_text(later, employee['timezone'])} по своему времени.", bad=False)
    return view(user_id, "Ответ отправлен новичку.", bad=False)


@router.post("/app/profile")
def save_profile(init_data: str = Form(""), tz: str = Form("", alias="timezone"), days: list[str] = Form([]),
                 work_start: str = Form(""), work_end: str = Form("")):
    """The recruiter sets their own working days and hours: reminders come only then."""
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    recruiter = db.query_one("SELECT * FROM recruiters WHERE max_user_id = ?", (user_id,))
    if recruiter is None:
        return view(user_id, ONLY_RECRUITERS, status=403)
    schedule, problem = hours.read_form(tz, days, work_start, work_end)
    if schedule is None:
        return view(user_id, problem)
    db.execute("UPDATE recruiters SET timezone = ?, work_days = ?, work_start = ?, work_end = ? WHERE id = ?",
               (*schedule, recruiter["id"]))
    return view(user_id, "График сохранён. Напоминания о вопросах будут приходить только в это время.", bad=False)


@router.post("/app/move/{employee_id}")
def move_newcomer(employee_id: int, init_data: str = Form(""), workplace_id: str = Form("")):
    try:
        user_id = check_init_data(init_data, cfg.MAX_BOT_TOKEN)
    except ValueError as e:
        return refused(e)
    recruiter = db.query_one("SELECT * FROM recruiters WHERE max_user_id = ?", (user_id,))
    if recruiter is None:
        return view(user_id, ONLY_RECRUITERS, status=403)
    employee = db.query_one("SELECT * FROM employees WHERE id = ? AND recruiter_id = ?", (employee_id, recruiter["id"]))
    if employee is None:
        return view(user_id, "Этот новичок закреплён за другим рекрутером.", status=403)
    if workplace_id and not (workplace_id.isdigit()
                             and db.query_one("SELECT 1 FROM workplaces WHERE id = ?", (int(workplace_id),))):
        return view(user_id, "Такого места работы нет.")
    hr.change_workplace(employee_id, int(workplace_id) if workplace_id else None)
    return view(user_id, f"{employee['full_name']}: место работы изменено.", bad=False)


def refused(error: ValueError) -> HTMLResponse:
    return HTMLResponse(f'<p class="flash bad">{escape(str(error))}</p>', 401)


def view(user_id: int, message: str = "", bad: bool = True, typed: tuple[int, str] = (0, ""),
         status: int = 200, tab: str = "home", lesson: int = 0) -> HTMLResponse:
    """What this MAX user may see: a recruiter or a manager their newcomers, a newcomer their own things, others nothing."""
    recruiter = db.query_one("SELECT * FROM recruiters WHERE max_user_id = ?", (user_id,))
    employee = db.query_one("SELECT * FROM employees WHERE max_user_id = ?", (user_id,))
    manager = db.query_one("SELECT * FROM managers WHERE max_user_id = ?", (user_id,))
    context = {"message": message, "bad": bad, "typed": typed, "recruiter": recruiter, "employee": employee,
               "manager": manager, "tab": tab if tab in TABS else "home", "open_lesson": lesson,
               "task_list": tasks.of_employee, "is_overdue": tasks.is_overdue, "now_of": engine.employee_now,
               "risk_of": risk.score}
    if recruiter:
        context |= recruiter_part(recruiter)
    if employee:
        context |= newcomer_part(employee)
    if manager:
        context["managed"] = db.query("SELECT * FROM employees WHERE manager_id = ? ORDER BY start_date DESC",
                                      (manager["id"],))
        context["card_of"] = managers.card
    return HTMLResponse(templates.get_template("miniapp_view.html").render(context), status)


def recruiter_part(recruiter) -> dict:
    workplaces = db.query("SELECT * FROM workplaces ORDER BY name")
    employees = db.query(
        "SELECT e.*, (SELECT COUNT(*) FROM sent_steps s WHERE s.employee_id = e.id AND s.status != 'skipped')"
        " AS steps_sent," + hr.CARD_COUNTS + " FROM employees e WHERE e.recruiter_id = ?"
        " ORDER BY e.start_date DESC, e.id DESC", (recruiter["id"],))
    newcomers = [{
        "employee": e,
        "workplace": next((w["name"] for w in workplaces if w["id"] == e["workplace_id"]), ""),
        "card": hr.newcomer_card(e),
        "stage": hr._stage(e),
        "flags": db.query("SELECT * FROM pulse WHERE employee_id = ? AND risk >= 1 ORDER BY id DESC LIMIT 3",
                          (e["id"],)),
        "answers": db.query("SELECT * FROM answers WHERE employee_id = ? ORDER BY id DESC LIMIT 5", (e["id"],)),
    } for e in employees]
    questions = db.query(
        "SELECT q.*, e.full_name, w.name AS workplace_name FROM escalations q JOIN employees e ON e.id = q.employee_id"
        " LEFT JOIN workplaces w ON w.id = e.workplace_id"
        " WHERE e.recruiter_id = ? AND q.status = 'open' ORDER BY q.id", (recruiter["id"],))
    blocks: dict[str, list] = {}
    for q in questions:
        blocks.setdefault(q["workplace_name"] or "Место работы не указано", []).append((q, hr.question_state(q)))
    schedule = hours.of_recruiter(recruiter)
    return {"newcomers": newcomers, "questions": questions, "blocks": blocks, "digest": hr.digest_text(recruiter["id"]),
            "workplaces": workplaces, "zones": hours.TIMEZONES, "day_names": hours.DAY_NAMES,
            "my_days": hours.parse_days(recruiter["work_days"]), "my_schedule": schedule.describe()}


def newcomer_part(employee) -> dict:
    steps_done = db.query_one("SELECT COUNT(*) AS n FROM sent_steps WHERE employee_id = ? AND status != 'skipped'",
                              (employee["id"],))["n"]
    questions = db.query("SELECT * FROM escalations WHERE employee_id = ? ORDER BY id DESC LIMIT 20",
                         (employee["id"],))
    # the bar is the share of the planned steps whose moment has passed; branches after buttons do not count
    planned = engine._timed_steps(employee)
    passed = {r["step_id"] for r in db.query("SELECT step_id FROM sent_steps WHERE employee_id = ?", (employee["id"],))}
    progress = round(100 * sum(1 for s in planned if s.id in passed) / len(planned)) if planned else 0
    my_tasks = tasks.of_employee(employee["id"])
    lessons = learning.for_employee(employee)
    return {"ty": employee["address"] == "ty", "stage": hr._stage(employee), "steps_done": steps_done,
            "progress": progress, "next_step": next_step(employee), "my_questions": questions,
            "my_tasks": my_tasks, "my_now": engine.employee_now(employee), "lessons": lessons,
            "lessons_done": sum(1 for item in lessons if item["done"])}


def next_step(employee) -> tuple[str, str] | None:
    """Text and local time of the next planned message; None while the bot waits for an answer or has no plans."""
    if employee["waiting"] or not employee["linked_at"]:
        return None
    upcoming = engine._next_event(employee)
    step = engine._step(upcoming[0]) if upcoming else None
    if step is None:
        return None
    at = upcoming[1].astimezone(ZoneInfo(employee["timezone"]))
    return render_text(step, employee["address"], employee["first_name"]), at.strftime("%d.%m в %H:%M")
