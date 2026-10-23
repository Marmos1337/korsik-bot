import asyncio
import importlib
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone

from app import db, max_api, messaging
from app.config import cfg
from app.scenario import (
    SPECIAL_WHEN, START, ScenarioError, Step, is_bad_answer, load_holidays, load_scenario, render_text,
    scale_buttons, step_due_at, tags_match,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 20
REMIND_AFTER = timedelta(hours=24)
SKIP_AFTER = timedelta(hours=48)
LATE_LIMIT = timedelta(hours=24)  # a step this late at the moment of linking is not sent at all

INTERACTIVE = ("обращение", "вопрос", "опрос", "шкала", "текст")
SURVEY = ("опрос", "шкала", "текст")
REMINDER = "напоминание"
ACK_TEXT = "Принято"

# (на «ты», на «вы»); until the employee chooses, the bot talks on «вы»
PHRASES = {
    "privacy": (
        "Я храню нашу переписку и твои ответы на опросы, их видит HR. "
        "Удалить свои данные можно в любой момент командой /delete.",
        "Я храню нашу переписку и ваши ответы на опросы, их видит HR. "
        "Удалить свои данные можно в любой момент командой /delete.",
    ),
    "remind": (
        "Кажется, на вопрос выше ещё нет ответа. Ответь, когда будет минутка.",
        "Кажется, на вопрос выше ещё нет ответа. Ответьте, когда будет минутка.",
    ),
    "what_wrong": (
        "Расскажи, пожалуйста, что не так? Так мы поймём, что исправить.",
        "Расскажите, пожалуйста, что не так? Так мы поймём, что исправить.",
    ),
    "thanks": ("Спасибо, записал.",) * 2,
    "delete_ask": (
        "Удалить нашу переписку, твои ответы на опросы и вопросы? После этого я перестану тебе писать.",
        "Удалить нашу переписку, ваши ответы на опросы и вопросы? После этого я перестану вам писать.",
    ),
    "delete_done": (
        "Готово, твои данные удалены. Если захочешь вернуться, открой свою ссылку ещё раз.",
        "Готово, ваши данные удалены. Если захотите вернуться, откройте свою ссылку ещё раз.",
    ),
    "delete_cancel": ("Хорошо, ничего не удаляю.",) * 2,
    "help": (
        "Я пишу тебе в нужные моменты первых трёх месяцев и отвечаю на вопросы о работе. "
        "Просто напиши вопрос своими словами.\n\n/delete - удалить свои данные",
        "Я пишу вам в нужные моменты первых трёх месяцев и отвечаю на вопросы о работе. "
        "Просто напишите вопрос своими словами.\n\n/delete - удалить свои данные",
    ),
    "help_demo": ("/next - перемотать время к следующему шагу (демо)",) * 2,
    "next_off": ("Команда /next работает только в демо-режиме.",) * 2,
    "next_none": ("Дальше по сценарию шагов нет.",) * 2,
    "only_text": (
        "Пока я понимаю только текст. Напиши, пожалуйста, словами.",
        "Пока я понимаю только текст. Напишите, пожалуйста, словами.",
    ),
    # until the questions ticket lands, free text only gets this
    "not_ready": ("Записал. Скоро научусь отвечать на вопросы по документам.",) * 2,
}

_steps: list[Step] = []
_holidays: set[date] = set()
_locks: dict[int, asyncio.Lock] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def employee_now(employee) -> datetime:
    return utc_now() + timedelta(seconds=employee["time_shift"] or 0)


def reload_scenario() -> list[str]:
    global _steps, _holidays
    try:
        steps = load_scenario(cfg.SCENARIO_PATH)
    except ScenarioError as e:
        log.error("scenario %s has %s problems, keeping the old one", cfg.SCENARIO_PATH, len(e.problems))
        return e.problems
    _steps = steps
    _holidays = load_holidays(cfg.HOLIDAYS_PATH)
    log.info("scenario loaded: %s steps", len(steps))
    return []


def invite_link(code: str) -> str | None:
    # a link with an empty bot name opens nothing, so there is no link until MAX tells us the name
    if not cfg.BOT_USERNAME:
        return None
    return f"https://max.ru/{cfg.BOT_USERNAME}?start={code}"


def phrase(employee, key: str) -> str:
    ty, vy = PHRASES[key]
    return ty if employee["address"] == "ty" else vy


async def say(employee, key: str, buttons=None) -> None:
    await messaging.send_to_employee(employee, phrase(employee, key), buttons=buttons)


# --- linking


async def link_employee(max_user: dict, code: str) -> str:
    user_id = max_user.get("user_id")
    employee = db.query_one("SELECT * FROM employees WHERE invite_code = ?", (code.strip(),))
    if employee is None or not user_id:
        return "unknown"
    if employee["max_user_id"] == user_id:
        return "ok"  # the same person opened the link again
    if employee["max_user_id"] is not None:
        return "used"
    if db.query_one("SELECT 1 FROM employees WHERE max_user_id = ?", (user_id,)):
        return "used"  # this MAX account already belongs to another employee
    async with _lock(employee["id"]):
        db.execute(
            "UPDATE employees SET max_user_id = ?, linked_at = ?, waiting = NULL WHERE id = ? AND max_user_id IS NULL",
            (user_id, _stamp(), employee["id"]),
        )
        log.info("employee %s linked", employee["id"])
        await _advance(employee["id"])
    return "ok"


# --- scheduler


async def scheduler_loop() -> None:
    log.info("scheduler started, tick every %s s", TICK_SECONDS)
    while True:
        try:
            await tick()
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


async def tick() -> int:
    sent = 0
    for row in db.query("SELECT id FROM employees WHERE max_user_id IS NOT NULL"):
        try:
            sent += await advance(row["id"])
        except Exception:
            log.exception("employee %s: scheduler step failed", row["id"])
    return sent


async def advance(employee_id: int) -> int:
    """Sends whatever is due for one employee: a reminder, a skip, the next steps. Returns steps sent."""
    async with _lock(employee_id):
        return await _advance(employee_id)


async def _advance(employee_id: int) -> int:
    employee = _employee(employee_id)
    if employee is None or not employee["max_user_id"]:
        return 0
    now = employee_now(employee)
    waiting = _waiting(employee)
    if waiting:
        waited = now - datetime.fromisoformat(waiting["asked_at"])
        if waited < REMIND_AFTER:
            return 0
        if waiting["kind"] == "step" and waited < SKIP_AFTER:
            await _remind(employee, waiting["step"])
            return 0
        # a "что не так?" is optional, so it just expires after a day, without reminders
        await _stop_waiting(employee, waiting)
        employee = _employee(employee_id)
    return await _send_due(employee, now)


async def _stop_waiting(employee, waiting: dict) -> None:
    _clear_waiting(employee["id"])
    step = _step(waiting["step"])
    if waiting["kind"] == "step":
        _mark(employee["id"], waiting["step"], "skipped")
        if step and step.when and step.when != START:
            # an ignored survey is dropped as a whole, otherwise nine questions would take eighteen days
            statuses = _statuses(employee["id"])
            for other in _timed_steps(employee):
                if other.when == step.when and other.id not in statuses:
                    _mark(employee["id"], other.id, "skipped")
    if step:
        await _check_survey(employee, step)


async def _send_due(employee, now: datetime) -> int:
    statuses = _statuses(employee["id"])
    due = []
    for step in _timed_steps(employee):
        if step.id in statuses:
            continue
        at = _due_at(employee, step)
        if at is None or at > now:
            continue
        if _too_late(employee, step, at):
            _mark(employee["id"], step.id, "skipped")
            continue
        # the start steps come first even when a step of the day before the start is already due
        due.append(((step.when != START, at, step.row), step))
    due.sort(key=lambda item: item[0])

    sent = 0
    for _, step in due:
        await _send_step(employee, step)
        sent += 1
        if step.kind in INTERACTIVE:
            break
    return sent


async def _send_step(employee, step: Step) -> None:
    text = render_text(step, employee["address"], employee["first_name"])
    if not db.query_one("SELECT 1 FROM sent_steps WHERE employee_id = ? AND status != 'skipped'", (employee["id"],)):
        # spec 4.9: the very first message tells what we keep and how to delete it
        text += "\n\n" + phrase(employee, "privacy")
    await messaging.send_to_employee(employee, text, buttons=_buttons(step), image=step.image or None)
    _mark(employee["id"], step.id, "sent")
    if step.kind in INTERACTIVE:
        _set_waiting(employee["id"], step.id, "step", employee_now(employee))


async def _remind(employee, step_id: str) -> None:
    statuses = _statuses(employee["id"])
    if statuses.get(step_id) != "sent":
        return  # reminded already
    _mark(employee["id"], step_id, "reminded")
    step = _step(step_id)
    if step is None or all(s.id in statuses for s in _timed_steps(employee)):
        return  # the last step of the scenario holds nothing back, no reason to nag
    buttons = _buttons(step)
    if step.kind == "обращение":
        await messaging.send_to_employee(employee, render_text(step, None, employee["first_name"]), buttons=buttons)
    elif not await send_special(employee, REMINDER, buttons=buttons):
        await say(employee, "remind", buttons=buttons)


async def send_special(employee, when: str, extra: str = "", buttons=None) -> bool:
    """Sends the scenario text kept for a special moment («напоминание», «эскалация»). False if there is none."""
    step = next((s for s in _steps if s.when == when), None)
    if step is None:
        return False
    text = render_text(step, employee["address"], employee["first_name"])
    if extra:
        text += "\n\n" + extra
    await messaging.send_to_employee(employee, text, buttons=buttons, image=step.image or None)
    return True


# --- buttons


async def on_callback(employee, payload: str, callback_id: str) -> None:
    await _ack(callback_id)
    async with _lock(employee["id"]):
        employee = _employee(employee["id"])
        if employee is None or not employee["max_user_id"]:
            return
        action, _, rest = payload.partition(":")
        if action == "addr":
            await _choose_address(employee, rest)
        elif action == "step":
            step_id, _, index = rest.rpartition(":")
            await _press(employee, step_id, index)
        elif action == "scale":
            step_id, _, score = rest.rpartition(":")
            await _rate(employee, step_id, score)
        elif action == "del":
            await _delete(employee, rest == "yes")
        else:
            log.info("employee %s: unknown button payload", employee["id"])


async def on_text(employee, text: str) -> bool:
    async with _lock(employee["id"]):
        employee = _employee(employee["id"])
        waiting = _waiting(employee) if employee else None
        if not waiting:
            return False
        step = _step(waiting["step"])
        if waiting["kind"] == "comment":
            question = phrase(employee, "what_wrong")
        elif step is not None and step.kind == "текст":
            question = render_text(step, employee["address"], employee["first_name"])
            _mark(employee["id"], step.id, "answered")
        else:
            return False  # the bot waits for a button, so this text is a question of its own
        _save_answer(employee["id"], waiting["step"], question, text)
        _clear_waiting(employee["id"])
        if step is not None:
            await _check_survey(employee, step)
        if not await _advance(employee["id"]):
            await say(employee, "thanks")
        return True


async def _ack(callback_id: str) -> None:
    if not callback_id:
        return
    try:
        # always with a notification: MAX may refuse an empty answer, and a short toast does no harm
        await max_api.answer_callback(callback_id, ACK_TEXT)
    except Exception as e:
        log.warning("answering a callback failed: %s", e)


async def _choose_address(employee, address: str) -> None:
    if address not in ("ty", "vy"):
        return
    db.execute("UPDATE employees SET address = ? WHERE id = ?", (address, employee["id"]))
    waiting = _waiting(employee)
    step = _step(waiting["step"]) if waiting else None
    if step is None or step.kind != "обращение":
        return  # an old button pressed again: the new address is saved, nothing else changes
    messaging.log_message(employee["id"], "employee", step.buttons[0 if address == "ty" else 1].text)
    _mark(employee["id"], step.id, "answered")
    _clear_waiting(employee["id"])
    await _advance(employee["id"])


async def _press(employee, step_id: str, index: str) -> None:
    step = _step(step_id)
    if step is None or not index.isdigit() or int(index) >= len(step.buttons):
        return
    button = step.buttons[int(index)]
    if button.url or _statuses(employee["id"]).get(step.id) not in ("sent", "reminded"):
        return  # answered or skipped already
    await _answer(employee, step, button.text)


async def _rate(employee, step_id: str, score: str) -> None:
    step = _step(step_id)
    if step is None or step.kind != "шкала" or not score.isdigit() or not 1 <= int(score) <= 10:
        return
    if _statuses(employee["id"]).get(step.id) not in ("sent", "reminded"):
        return
    await _answer(employee, step, score, int(score))


async def _answer(employee, step: Step, answer: str, score: int | None = None) -> None:
    messaging.log_message(employee["id"], "employee", answer)
    _mark(employee["id"], step.id, "answered")
    if step.kind in SURVEY:
        question = render_text(step, employee["address"], employee["first_name"])
        _save_answer(employee["id"], step.id, question, answer, score)
    waiting = _waiting(employee)
    if waiting and waiting["step"] == step.id:
        _clear_waiting(employee["id"])

    tags = db.split_tags(employee["tags"])
    statuses = _statuses(employee["id"])
    for branch in _steps:
        if (branch.after and branch.after[0] == step.id and branch.after[1].lower() == answer.lower()
                and tags_match(branch.tags, tags) and branch.id not in statuses):
            await _send_step(employee, branch)

    employee = _employee(employee["id"])
    # a branch that already asks for text is enough, no need for a second question
    if is_bad_answer(step, answer, score) and not _waiting(employee):
        await say(employee, "what_wrong")
        _set_waiting(employee["id"], step.id, "comment", employee_now(employee))
    await _check_survey(employee, step)
    await _advance(employee["id"])


async def _check_survey(employee, step: Step) -> None:
    """Tells HR when the last question of a survey (survey steps sharing one «Когда») is closed."""
    root = _root(step)
    if not root.when or root.when == START:
        return
    statuses = _statuses(employee["id"])
    block = {s.id for s in _timed_steps(employee) if s.when == root.when and s.kind in SURVEY}
    if not block or any(statuses.get(step_id) not in ("answered", "skipped") for step_id in block):
        return
    ids = [s.id for s in _steps if s.kind in SURVEY and s.id in statuses
           and (s.id in block or (s.after and s.after[0] in block))]
    waiting = _waiting(_employee(employee["id"]))
    if waiting and waiting["step"] in ids:
        return  # a comment or a text answer is still on its way
    if not any(statuses[step_id] == "answered" for step_id in ids):
        return
    await _survey_finished(employee, ids)


async def _survey_finished(employee, step_ids: list[str]) -> None:
    # hr.py comes with the escalation ticket; without it the summary is simply not sent
    try:
        hr = importlib.import_module("app.hr")
    except ModuleNotFoundError as e:
        if e.name != "app.hr":
            raise
        return
    survey_finished = getattr(hr, "survey_finished", None)
    if survey_finished is None:
        return
    try:
        await survey_finished(employee, step_ids)
    except Exception:
        log.exception("employee %s: survey summary failed", employee["id"])


# --- demo and deletion


async def demo_next(employee_id: int) -> str | None:
    if not cfg.DEMO_MODE:
        return None
    async with _lock(employee_id):
        employee = _employee(employee_id)
        if employee is None or not employee["linked_at"]:
            return None
        now = employee_now(employee)
        target = _next_event(employee)
        if target is None:
            return None
        step_id, at = target
        if at > now:
            shift = employee["time_shift"] + math.ceil((at - now).total_seconds())
            db.execute("UPDATE employees SET time_shift = ? WHERE id = ?", (shift, employee_id))
        return step_id


def _next_event(employee) -> tuple[str, datetime] | None:
    statuses = _statuses(employee["id"])
    waiting = _waiting(employee)
    if waiting:
        # nothing moves until the answer, so the next thing that happens is the reminder or the skip
        asked = datetime.fromisoformat(waiting["asked_at"])
        later = SKIP_AFTER if statuses.get(waiting["step"]) == "reminded" else REMIND_AFTER
        return waiting["step"], asked + later
    upcoming = []
    for step in _timed_steps(employee):
        if step.id in statuses:
            continue
        at = _due_at(employee, step)
        if at is not None and not _too_late(employee, step, at):
            upcoming.append((at, step.row, step.id))
    if not upcoming:
        return None
    at, _, step_id = min(upcoming)
    return step_id, at


async def ask_delete(employee) -> None:
    buttons = [[max_api.callback_button("Да, удалить", "del:yes"), max_api.callback_button("Нет", "del:no")]]
    await say(employee, "delete_ask", buttons=buttons)


async def _delete(employee, confirmed: bool) -> None:
    if not confirmed:
        await say(employee, "delete_cancel")
        return
    goodbye = phrase(employee, "delete_done")
    delete_employee_data(employee["id"])
    # the dialog log is gone and the account is unlinked, so the goodbye goes out directly
    await messaging.send_to_user(employee["max_user_id"], goodbye)


def delete_employee_data(employee_id: int) -> None:
    conn = db.connect()
    try:
        with conn:
            for table in ("messages", "answers", "pulse", "escalations", "sent_steps"):
                conn.execute(f"DELETE FROM {table} WHERE employee_id = ?", (employee_id,))
            conn.execute(
                "UPDATE employees SET max_user_id = NULL, linked_at = NULL, address = NULL, waiting = NULL,"
                " time_shift = 0 WHERE id = ?",
                (employee_id,),
            )
    finally:
        conn.close()
    log.info("employee %s: data deleted on request", employee_id)


def _buttons(step: Step) -> list[list[dict]] | None:
    if step.kind == "шкала":
        return [[max_api.callback_button(b.text, f"scale:{step.id}:{b.text}") for b in row] for row in scale_buttons()]
    answers, links = [], []
    for i, button in enumerate(step.buttons):
        if button.url:
            links.append([max_api.link_button(button.text, button.url)])
        elif step.kind == "обращение":
            answers.append(max_api.callback_button(button.text, "addr:ty" if i == 0 else "addr:vy"))
        else:
            answers.append(max_api.callback_button(button.text, f"step:{step.id}:{i}"))
    # short answers like «Да | Нет» fit in one row, longer ones get a row each
    if sum(len(b["text"]) for b in answers) <= 24:
        rows = [answers] if answers else []
    else:
        rows = [[b] for b in answers]
    return (rows + links) or None


# --- state


def _lock(employee_id: int) -> asyncio.Lock:
    # the scheduler and button presses run side by side; one employee is handled by one of them at a time
    return _locks.setdefault(employee_id, asyncio.Lock())


def _stamp() -> str:
    return utc_now().isoformat(timespec="seconds")


def _employee(employee_id: int):
    return db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))


def _step(step_id: str) -> Step | None:
    return next((s for s in _steps if s.id == step_id), None)


def _root(step: Step) -> Step:
    # a branch step belongs to the timed step whose button started it; the bound guards against a loop
    for _ in range(len(_steps)):
        parent = _step(step.after[0]) if step.after else None
        if parent is None:
            return step
        step = parent
    return step


def _timed_steps(employee) -> list[Step]:
    tags = db.split_tags(employee["tags"])
    return [s for s in _steps if not s.after and s.when not in SPECIAL_WHEN and tags_match(s.tags, tags)]


def _due_at(employee, step: Step) -> datetime | None:
    linked = datetime.fromisoformat(employee["linked_at"]) if employee["linked_at"] else None
    return step_due_at(step, date.fromisoformat(employee["start_date"]), employee["timezone"], _holidays,
                       linked, cfg.PROBATION_MONTHS)


def _save_answer(employee_id: int, step_id: str, question: str, answer: str, score: int | None = None) -> None:
    db.execute(
        "INSERT INTO answers (employee_id, step_id, question, answer, score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (employee_id, step_id, question, answer, score, _stamp()),
    )


def _too_late(employee, step: Step, at: datetime) -> bool:
    return step.when != START and at < datetime.fromisoformat(employee["linked_at"]) - LATE_LIMIT


def _statuses(employee_id: int) -> dict[str, str]:
    rows = db.query("SELECT step_id, status FROM sent_steps WHERE employee_id = ?", (employee_id,))
    return {r["step_id"]: r["status"] for r in rows}


def _mark(employee_id: int, step_id: str, status: str) -> None:
    stamp = _stamp()
    db.execute(
        "INSERT INTO sent_steps (employee_id, step_id, status, sent_at, answered_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(employee_id, step_id) DO UPDATE SET status = excluded.status,"
        " answered_at = COALESCE(excluded.answered_at, sent_steps.answered_at)",
        (employee_id, step_id, status, stamp, stamp if status == "answered" else None),
    )


def _waiting(employee) -> dict | None:
    return json.loads(employee["waiting"]) if employee["waiting"] else None


def _set_waiting(employee_id: int, step_id: str, kind: str, asked_at: datetime) -> None:
    # asked_at is in the employee's (maybe shifted) time, so the demo rewind moves reminders too
    value = json.dumps({"step": step_id, "kind": kind, "asked_at": asked_at.isoformat(timespec="seconds")})
    db.execute("UPDATE employees SET waiting = ? WHERE id = ?", (value, employee_id))


def _clear_waiting(employee_id: int) -> None:
    db.execute("UPDATE employees SET waiting = NULL WHERE id = ?", (employee_id,))
