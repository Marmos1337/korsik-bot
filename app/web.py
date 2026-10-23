import asyncio
import hmac
import io
import json
import logging
import os
import secrets
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2
import openpyxl
import qrcode
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse, Response
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from app import db, engine, hr, knowledge, llm, miniapp
from app.config import cfg
from app.scenario import ScenarioError, Step, load_scenario, validate_scenario

log = logging.getLogger(__name__)

LOGIN_DELAY = 1.0  # seconds of waiting after a wrong password, a cheap brake on guessing
SESSION_HOURS = 12
MAX_UPLOAD = 20 * 1024 * 1024
FORM_OVERHEAD = 1024 * 1024  # room for the other fields of an upload form
MAX_NAME = 200
UPLOAD_TYPES = (".pdf", ".docx", ".pptx", ".txt", ".md")
RISK_NAMES = {1: "есть трудность", 2: "красный флаг"}
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PUBLIC_PATHS = ("/login", "/health")
# pages have no scripts and load nothing from other hosts, so the policy can be strict
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}
# the MAX mini app runs MAX's bridge script and its own one, and lives inside a MAX frame
MINIAPP_HEADERS = SECURITY_HEADERS | {"Content-Security-Policy": miniapp.POLICY}
# Russian time zones, west to east; offsets never change because Russia has no summer time
TIMEZONES = {
    "Europe/Kaliningrad": "Калининград (UTC+2)",
    "Europe/Moscow": "Москва, Санкт-Петербург (UTC+3)",
    "Europe/Samara": "Самара (UTC+4)",
    "Asia/Yekaterinburg": "Екатеринбург (UTC+5)",
    "Asia/Omsk": "Омск (UTC+6)",
    "Asia/Novosibirsk": "Новосибирск (UTC+7)",
    "Asia/Krasnoyarsk": "Красноярск (UTC+7)",
    "Asia/Irkutsk": "Иркутск (UTC+8)",
    "Asia/Yakutsk": "Якутск (UTC+9)",
    "Asia/Vladivostok": "Владивосток (UTC+10)",
    "Asia/Magadan": "Магадан (UTC+11)",
    "Asia/Kamchatka": "Камчатка (UTC+12)",
}

# the admin is plain HTML pages, an auto-generated API page would only show the routes to strangers
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(miniapp.router)

templates = Jinja2Templates(env=jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
))


def local_time(stamp: str | None) -> str:
    """UTC stamps from the database, shown in the cabinet's own time zone."""
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).astimezone(ZoneInfo(cfg.DEFAULT_TZ)).strftime("%d.%m.%Y %H:%M")
    except (ValueError, KeyError):  # a bad stamp, or a DEFAULT_TZ that is not a time zone
        return stamp


templates.env.filters["local"] = local_time
templates.env.filters["zone"] = lambda name: TIMEZONES.get(name, name)
templates.env.globals["tz_name"] = cfg.DEFAULT_TZ
templates.env.globals["open_questions"] = lambda: db.query_one(
    "SELECT COUNT(*) AS n FROM escalations WHERE status = 'open'")["n"]


@app.middleware("http")
async def guard(request: Request, call_next):
    size = request.headers.get("content-length", "")
    # the mini app has no cabinet session: each of its requests carries MAX's signed initData instead
    in_miniapp = request.url.path == "/app" or request.url.path.startswith("/app/")
    if size.isdigit() and int(size) > MAX_UPLOAD + FORM_OVERHEAD:
        # answered before the body is read, so a huge upload never lands on disk
        response = page(request, "message.html", 413, text="Файл больше 20 МБ.")
    elif in_miniapp or request.url.path in PUBLIC_PATHS or request.session.get("admin"):
        response = await call_next(request)
    else:
        response = RedirectResponse("/login", status_code=303)
    response.headers.update(MINIAPP_HEADERS if in_miniapp else SECURITY_HEADERS)
    return response


if not cfg.SECRET_KEY:
    log.warning("SECRET_KEY is empty: sessions use a random key and end with every restart")
if not cfg.ADMIN_PASSWORD:
    log.warning("ADMIN_PASSWORD is empty: nobody can log in to the cabinet")
# Added after guard, so it wraps guard and the session is ready when guard looks at it.
# No Secure flag: the cabinet is also opened over plain http on 127.0.0.1, the HTTPS proxy sits in front of it.
app.add_middleware(
    SessionMiddleware,
    secret_key=cfg.SECRET_KEY or secrets.token_hex(32),
    session_cookie="korsik_session",
    max_age=SESSION_HOURS * 3600,
    same_site="lax",
)


def page(request: Request, name: str, status: int = 200, **context):
    context["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, context, status_code=status)


def back(request: Request, url: str, message: str | None = None, bad: bool = False):
    """Redirect after a POST; the message shows once on the next page."""
    if message:
        request.session["flash"] = {"text": message[:300], "bad": bad}
    return RedirectResponse(url, status_code=303)


@app.exception_handler(HTTPException)
@app.exception_handler(RequestValidationError)
async def http_error(request: Request, error: Exception):
    # a mistyped address like /employees/abc is also "no such page" for a person
    status = getattr(error, "status_code", 404)
    text = "Такой страницы нет." if status == 404 else "Запрос не получился."
    return page(request, "message.html", status, text=text)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login")
def login_form(request: Request):
    return page(request, "login.html")


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    # bytes, because compare_digest refuses str with non-ASCII letters; an empty setting never matches
    if cfg.ADMIN_PASSWORD and hmac.compare_digest(password.encode(), cfg.ADMIN_PASSWORD.encode()):
        request.session.clear()
        request.session["admin"] = True
        return RedirectResponse("/", status_code=303)
    await asyncio.sleep(LOGIN_DELAY)
    return page(request, "login.html", 401, error="Неверный пароль.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def index(request: Request):
    employees = db.query(
        "SELECT e.*, r.name AS recruiter_name,"
        " (SELECT COUNT(*) FROM sent_steps s WHERE s.employee_id = e.id AND s.status != 'skipped') AS steps_sent,"
        " (SELECT COUNT(*) FROM escalations q WHERE q.employee_id = e.id AND q.status = 'open') AS open_questions,"
        " (SELECT COUNT(*) FROM pulse p WHERE p.employee_id = e.id AND p.risk = 2) AS red_flags"
        " FROM employees e LEFT JOIN recruiters r ON r.id = e.recruiter_id ORDER BY e.start_date DESC, e.id DESC"
    )
    recruiters = db.query("SELECT id, name FROM recruiters ORDER BY name")
    default_zone = cfg.DEFAULT_TZ if cfg.DEFAULT_TZ in TIMEZONES else "Europe/Moscow"
    return page(request, "index.html", employees=employees, recruiters=recruiters, zones=TIMEZONES,
                default_zone=default_zone, tags=known_tags(), model_up=llm.is_up())


def known_tags() -> list[str]:
    """Tags already used by the scenario and the documents: a typo in a tag silently hides steps from a newcomer."""
    found = {}
    for step in read_scenario()[0]:
        for tag in step.tags:
            found.setdefault(tag.lstrip("-").strip().lower(), tag.lstrip("-").strip())
    for row in db.query("SELECT tags FROM documents"):
        for tag in db.split_tags(row["tags"]):
            found.setdefault(tag.lower(), tag)
    return sorted(found.values(), key=str.lower)


@app.post("/employees")
def add_employee(request: Request, full_name: str = Form(""), first_name: str = Form(""), start_date: str = Form(""),
                 tz: str = Form("", alias="timezone"), tags: str = Form(""), recruiter_id: str = Form("")):
    full_name = " ".join(full_name.split())
    names = full_name.split()
    tags = ", ".join(db.split_tags(tags))
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        start = None
    recruiter_id = recruiter_id.strip()

    if not names or len(full_name) > MAX_NAME:
        return back(request, "/", "Впишите ФИО, не длиннее 200 знаков.", bad=True)
    if start is None or not 2020 <= start.year <= 2100:
        return back(request, "/", "Дата выхода нужна в виде год-месяц-день, например 2026-11-02.", bad=True)
    if tz not in TIMEZONES:
        return back(request, "/", "Выберите часовой пояс из списка.", bad=True)
    if len(tags) > MAX_NAME or len(first_name) > MAX_NAME:
        return back(request, "/", "Теги и имя слишком длинные.", bad=True)
    if recruiter_id and not (recruiter_id.isdigit()
                             and db.query_one("SELECT 1 FROM recruiters WHERE id = ?", (int(recruiter_id),))):
        return back(request, "/", "Такого рекрутера нет.", bad=True)

    # «Фамилия Имя Отчество»: the name to address is the second word unless HR typed it separately
    first_name = first_name.strip() or (names[1] if len(names) > 1 else names[0])
    employee_id = db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, recruiter_id, invite_code,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (full_name, first_name, start.isoformat(), tz, tags, int(recruiter_id) if recruiter_id else None,
         secrets.token_hex(8), db.now_iso()),
    )
    return back(request, f"/employees/{employee_id}", "Новичок добавлен. Отправьте ему ссылку или QR.")


@app.get("/employees/{employee_id}")
def employee_card(request: Request, employee_id: int):
    employee = db.query_one(
        "SELECT e.*, r.name AS recruiter_name FROM employees e LEFT JOIN recruiters r ON r.id = e.recruiter_id"
        " WHERE e.id = ?", (employee_id,))
    if employee is None:
        raise HTTPException(404)
    steps = db.query("SELECT * FROM sent_steps WHERE employee_id = ? ORDER BY sent_at, rowid", (employee_id,))
    titles = {step.id: step.text_ty for step in read_scenario()[0]}
    # the newest 300 are enough to follow a dialogue, three months of chat would make the page heavy
    messages = db.query("SELECT * FROM (SELECT * FROM messages WHERE employee_id = ? ORDER BY id DESC LIMIT 300)"
                        " ORDER BY id", (employee_id,))
    answers = db.query("SELECT * FROM answers WHERE employee_id = ? ORDER BY id", (employee_id,))
    flags = db.query("SELECT * FROM pulse WHERE employee_id = ? AND risk >= 1 ORDER BY id", (employee_id,))
    now = engine.employee_now(employee).astimezone(ZoneInfo(employee["timezone"]))
    return page(request, "employee.html", employee=employee, link=engine.invite_link(employee["invite_code"]),
                steps=steps, titles=titles, messages=messages, answers=answers, flags=flags, demo=cfg.DEMO_MODE,
                employee_now=now.strftime("%d.%m.%Y %H:%M"))


@app.post("/employees/{employee_id}/next")
async def next_step(request: Request, employee_id: int):
    url = f"/employees/{employee_id}"
    if not cfg.DEMO_MODE:
        return back(request, url, "Кнопка работает только в демо-режиме.", bad=True)
    step_id = await engine.demo_next(employee_id)
    if step_id is None:
        return back(request, url, "Дальше по сценарию шагов нет, или новичок ещё не открыл ссылку.", bad=True)
    try:
        sent = await engine.advance(employee_id)
    except Exception as e:
        log.warning("employee %s: demo step was not sent: %s", employee_id, type(e).__name__)
        return back(request, url, f"Время сдвинуто к шагу {step_id}, но сообщение не ушло. "
                                  "Планировщик отправит его сам.", bad=True)
    return back(request, url, f"Время сдвинуто к шагу {step_id}, отправлено сообщений: {sent}.")


@app.get("/employees/{employee_id}/qr.png")
def employee_qr(employee_id: int):
    employee = db.query_one("SELECT invite_code FROM employees WHERE id = ?", (employee_id,))
    return qr_response(engine.invite_link(employee["invite_code"]) if employee else None)


def qr_response(link: str | None) -> Response:
    if link is None:
        raise HTTPException(404)
    image = io.BytesIO()
    qrcode.make(link).save(image, format="PNG")
    return Response(image.getvalue(), media_type="image/png")


@app.get("/recruiters")
def recruiters_page(request: Request):
    rows = db.query(
        "SELECT r.*, (SELECT COUNT(*) FROM employees e WHERE e.recruiter_id = r.id) AS employees"
        " FROM recruiters r ORDER BY r.id"
    )
    links = {row["id"]: engine.invite_link("hr_" + row["invite_code"]) for row in rows}
    return page(request, "recruiters.html", recruiters=rows, links=links)


@app.post("/recruiters")
def add_recruiter(request: Request, name: str = Form("")):
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME:
        return back(request, "/recruiters", "Впишите имя рекрутера.", bad=True)
    db.execute("INSERT INTO recruiters (name, invite_code, created_at) VALUES (?, ?, ?)",
               (name, secrets.token_hex(8), db.now_iso()))
    return back(request, "/recruiters", "Рекрутер добавлен. Пусть откроет ссылку в МАКС.")


@app.get("/recruiters/{recruiter_id}/qr.png")
def recruiter_qr(recruiter_id: int):
    recruiter = db.query_one("SELECT invite_code FROM recruiters WHERE id = ?", (recruiter_id,))
    return qr_response(engine.invite_link("hr_" + recruiter["invite_code"]) if recruiter else None)


@app.get("/knowledge")
def knowledge_page(request: Request):
    return page(request, "knowledge.html", documents=list_documents(), hits=None, question="", tags="")


def list_documents():
    return db.query("SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunks"
                    " FROM documents d ORDER BY d.title, d.id")


@app.post("/knowledge/upload")
def upload_document(request: Request, file: UploadFile | None = File(None), title: str = Form(""),
                    tags: str = Form("")):
    # A plain def: FastAPI runs it in a worker thread, so reading a big scan (OCR can take minutes)
    # does not stall the bot's event loop.
    if file is None or not file.filename:
        return back(request, "/knowledge", "Выберите файл.", bad=True)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in UPLOAD_TYPES:
        return back(request, "/knowledge", "Такие файлы не принимаю: нужен PDF, DOCX, PPTX, TXT или MD.", bad=True)
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return back(request, "/knowledge", "Файл больше 20 МБ.", bad=True)

    folder = Path(cfg.DATA_DIR) / "uploads"
    # a random name: whatever the browser sent never reaches the file system
    path = folder / (uuid.uuid4().hex + suffix)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as e:
        log.warning("upload was not saved: %s", type(e).__name__)
        return back(request, "/knowledge", "Не удалось сохранить файл на сервере. Проверьте место и права на data/.",
                    bad=True)
    title = " ".join(title.split())[:MAX_NAME] or Path(file.filename).stem[:MAX_NAME]
    try:
        asyncio.run(knowledge.add_document(path, title, db.split_tags(tags)))
    except ValueError as e:
        path.unlink(missing_ok=True)
        return back(request, "/knowledge", str(e), bad=True)
    except Exception as e:
        # pdf, docx and pptx readers raise their own errors on a damaged file
        log.warning("document %s could not be read: %s", suffix, type(e).__name__)
        path.unlink(missing_ok=True)
        return back(request, "/knowledge", f"Не получилось прочитать файл: он повреждён или это не {suffix}.",
                    bad=True)
    return back(request, "/knowledge", "Документ добавлен.")


@app.post("/knowledge/{document_id}/delete")
def delete_document(request: Request, document_id: int):
    row = db.query_one("SELECT filename FROM documents WHERE id = ?", (document_id,))
    if row is None:
        return back(request, "/knowledge", "Этого документа уже нет.", bad=True)
    knowledge.delete_document(document_id)
    # only cabinet uploads live in data/uploads, documents loaded by tools/load_kb are somewhere else
    stored = Path(cfg.DATA_DIR) / "uploads" / Path(row["filename"]).name
    if stored.is_file():
        stored.unlink()
    return back(request, "/knowledge", "Документ удалён.")


@app.post("/knowledge/check")
async def check_question(request: Request, question: str = Form(""), tags: str = Form("")):
    question = question.strip()
    if not question:
        return back(request, "/knowledge", "Впишите вопрос.", bad=True)
    # a POST, so the question does not end up in the access log
    hits = await knowledge.search(question, db.split_tags(tags))
    return page(request, "knowledge.html", documents=list_documents(), hits=hits, question=question, tags=tags)


@app.get("/questions")
def questions_page(request: Request):
    rows = db.query(
        "SELECT q.*, e.full_name, e.tags, r.name AS recruiter_name, r.max_user_id AS recruiter_linked"
        " FROM escalations q JOIN employees e ON e.id = q.employee_id"
        " LEFT JOIN recruiters r ON r.id = e.recruiter_id ORDER BY q.id DESC"
    )
    opened = [(row, read_context(row["context"])) for row in reversed([r for r in rows if r["status"] == "open"])]
    answered = [row for row in rows if row["status"] != "open"][:30]
    return page(request, "questions.html", opened=opened, answered=answered)


def read_context(raw: str) -> list[dict]:
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


@app.post("/questions/{escalation_id}/answer")
async def answer_question(request: Request, escalation_id: int, answer: str = Form(""), employee_id: int = Form(0),
                          asked_at: str = Form("")):
    answer = answer.strip()
    # the form names the newcomer and the time too: after /delete the question number may go to a new question
    owner = db.query_one("SELECT e.max_user_id FROM escalations q JOIN employees e ON e.id = q.employee_id"
                         " WHERE q.id = ? AND q.employee_id = ? AND q.created_at = ?",
                         (escalation_id, employee_id, asked_at))
    if not answer:
        return back(request, "/questions", "Впишите ответ.", bad=True)
    if owner is not None and not owner["max_user_id"]:
        return back(request, "/questions", "Новичок не привязан к боту: удалил свои данные или не открыл "
                                           "ссылку. Ответить некому.", bad=True)
    try:
        await hr.deliver_answer(escalation_id, answer, "кабинет", employee_id, asked_at)
    except ValueError as e:
        return back(request, "/questions", str(e), bad=True)
    except Exception as e:
        # deliver_answer has already reopened the question, nothing is lost
        log.warning("escalation %s: answer was not sent: %s", escalation_id, type(e).__name__)
        return back(request, "/questions", "Не получилось отправить ответ в МАКС. Вопрос остался открытым, "
                                           "попробуйте ещё раз.", bad=True)
    return back(request, "/questions", "Ответ отправлен новичку.")


def read_scenario() -> tuple[list[Step], list[str]]:
    """The working scenario file: its steps and the problems that keep it from loading."""
    try:
        return load_scenario(cfg.SCENARIO_PATH), []
    except ScenarioError as e:
        return [], e.problems


@app.get("/scenario")
def scenario_page(request: Request):
    steps, problems = read_scenario()
    return page(request, "scenario.html", steps=len(steps), problems=problems, upload_problems=None)


@app.get("/scenario/download")
def download_scenario():
    if not Path(cfg.SCENARIO_PATH).is_file():
        raise HTTPException(404)
    return FileResponse(cfg.SCENARIO_PATH, filename="scenario.xlsx")


@app.post("/scenario/upload")
async def upload_scenario(request: Request, file: UploadFile | None = File(None)):
    if file is None or not file.filename:
        return back(request, "/scenario", "Выберите файл.", bad=True)
    if Path(file.filename).suffix.lower() != ".xlsx":
        return back(request, "/scenario", "Нужен файл Excel с расширением .xlsx.", bad=True)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return back(request, "/scenario", "Файл больше 20 МБ.", bad=True)

    working = Path(cfg.SCENARIO_PATH)
    # next to the working file: on the same disk the swap below is one rename, nobody sees half a file
    incoming = working.with_name(f".new-{uuid.uuid4().hex}.xlsx")
    try:
        incoming.write_bytes(data)
        problems = validate_scenario(incoming)
        if problems:
            steps, current = read_scenario()
            return page(request, "scenario.html", 400, steps=len(steps), problems=current, upload_problems=problems)
        if working.is_file():
            Path(cfg.DATA_DIR).mkdir(parents=True, exist_ok=True)
            shutil.copy2(working, Path(cfg.DATA_DIR) / "scenario-previous.xlsx")
        os.replace(incoming, working)
    except OSError as e:
        log.warning("scenario file was not written: %s", type(e).__name__)
        return back(request, "/scenario", "Не удалось записать файл сценария. Проверьте права на папку scenario/.",
                    bad=True)
    finally:
        incoming.unlink(missing_ok=True)

    problems = engine.reload_scenario()
    if problems:
        return back(request, "/scenario", "Файл заменён, но бот его не загрузил: " + problems[0], bad=True)
    return back(request, "/scenario", "Сценарий проверен, заменён и загружен в бота.")


@app.get("/export.xlsx")
def export_excel():
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for title, (header, rows) in (("Новички", newcomers_sheet()), ("Опросы", surveys_sheet()),
                                  ("Флаги", flags_sheet()), ("Вопросы", questions_sheet())):
        add_sheet(workbook, title, header, rows)
    out = io.BytesIO()
    workbook.save(out)
    return Response(out.getvalue(), media_type=XLSX_TYPE,
                    headers={"Content-Disposition": 'attachment; filename="korsik-report.xlsx"'})


def newcomers_sheet():
    rows = db.query(
        "SELECT e.*, r.name AS recruiter_name,"
        " (SELECT COUNT(*) FROM sent_steps s WHERE s.employee_id = e.id AND s.status != 'skipped') AS sent,"
        " (SELECT COUNT(*) FROM sent_steps s WHERE s.employee_id = e.id AND s.status = 'skipped') AS skipped,"
        " (SELECT COUNT(*) FROM answers a WHERE a.employee_id = e.id) AS answers,"
        " (SELECT COUNT(*) FROM escalations q WHERE q.employee_id = e.id AND q.status = 'open') AS open_questions,"
        " (SELECT COUNT(*) FROM pulse p WHERE p.employee_id = e.id AND p.risk = 2) AS red_flags"
        " FROM employees e LEFT JOIN recruiters r ON r.id = e.recruiter_id ORDER BY e.start_date, e.id"
    )
    header = ["ФИО", "Имя", "Дата выхода", "Часовой пояс", "Теги", "Рекрутер", "В боте с", "Обращение",
              "Шагов отправлено", "Шагов пропущено", "Ответов на опросы", "Открытых вопросов", "Красных флагов"]
    return header, [
        [e["full_name"], e["first_name"], e["start_date"], e["timezone"], e["tags"], e["recruiter_name"] or "",
         local_time(e["linked_at"]), {"ty": "на ты", "vy": "на вы"}.get(e["address"], ""), e["sent"], e["skipped"],
         e["answers"], e["open_questions"], e["red_flags"]]
        for e in rows
    ]


def surveys_sheet():
    rows = db.query("SELECT a.*, e.full_name FROM answers a JOIN employees e ON e.id = a.employee_id ORDER BY a.id")
    header = ["Новичок", "Шаг", "Вопрос", "Ответ", "Оценка", "Когда"]
    return header, [[a["full_name"], a["step_id"], a["question"], a["answer"], a["score"],
                     local_time(a["created_at"])] for a in rows]


def flags_sheet():
    rows = db.query("SELECT p.*, e.full_name FROM pulse p JOIN employees e ON e.id = p.employee_id"
                    " WHERE p.risk >= 1 ORDER BY p.id")
    header = ["Новичок", "Когда", "Риск", "Тема", "Тон", "Причина", "Сообщение"]
    return header, [[p["full_name"], local_time(p["created_at"]), RISK_NAMES[p["risk"]], p["topic"], p["tone"],
                     p["reason"], p["text"]] for p in rows]


def questions_sheet():
    rows = db.query("SELECT q.*, e.full_name, r.name AS recruiter_name FROM escalations q"
                    " JOIN employees e ON e.id = q.employee_id LEFT JOIN recruiters r ON r.id = e.recruiter_id"
                    " ORDER BY q.id")
    header = ["Новичок", "Рекрутер", "Когда", "Вопрос", "Статус", "Ответ", "Кто ответил", "Когда ответили"]
    return header, [[q["full_name"], q["recruiter_name"] or "", local_time(q["created_at"]), q["question"],
                     "открыт" if q["status"] == "open" else "отвечен", q["answer"] or "", q["answered_by"] or "",
                     local_time(q["answered_at"])] for q in rows]


def add_sheet(workbook, title: str, header: list[str], rows: list[list]) -> None:
    sheet = workbook.create_sheet(title)
    sheet.append(header)
    for row in rows:
        sheet.append([clean_cell(value) for value in row])
    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            # newcomers type this text: a value that starts with "=" would run as a formula in Excel
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
    for column in sheet.columns:
        widest = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(widest + 2, 10), 60)


def clean_cell(value):
    if not isinstance(value, str):
        return value
    # control characters make openpyxl refuse the whole sheet, and a cell holds at most 32767 characters
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:32000]
