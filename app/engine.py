import asyncio
import dataclasses
import json
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import care, db, hours, hr, managers, max_api, messaging, tasks
from app.config import cfg
from app.scenario import (
    SPECIAL_WHEN, START, ScenarioError, Step, is_bad_answer, load_holidays, load_scenario, render_text,
    scale_buttons, step_due_at, tags_match,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 20
REMIND_AFTER = timedelta(hours=24)
SKIP_AFTER = timedelta(hours=48)
LATE_LIMIT = timedelta(hours=24)  # a step this late at the moment of linking is not sent at all, unless required
LATE_JOIN = timedelta(days=7)  # a link opened this long after HR added the newcomer: only required steps
CATCH_UP_PER_DAY = 3  # missed required steps a late newcomer gets per day, so the backlog does not come at once
SEND_TRIES = 3  # refusals from MAX in a row before a step is given up, e.g. the newcomer blocked the bot
# only these say that MAX will not deliver to this newcomer; a bad token (401), 429 and 5xx concern
# everybody at once and pass with time, so on those the step just waits for the next tick
RECIPIENT_REFUSALS = (403, 404)

SURVEY = ("опрос", "шкала", "текст")
# a «вопрос» only offers optional branches: its buttons stay, but the next steps do not wait for it
BLOCKING = ("обращение",) + SURVEY
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
        "Просто напиши вопрос своими словами. Если тяжело или что-то не так, напиши «мне тяжело», "
        "я выслушаю и позову рекрутера.\n\n/delete - удалить свои данные",
        "Я пишу вам в нужные моменты первых трёх месяцев и отвечаю на вопросы о работе. "
        "Просто напишите вопрос своими словами. Если тяжело или что-то не так, напишите «мне тяжело», "
        "я выслушаю и позову рекрутера.\n\n/delete - удалить свои данные",
    ),
    "help_demo": ("/next - перемотать время к следующему шагу (демо)",) * 2,
    "next_off": ("Команда /next работает только в демо-режиме.",) * 2,
    "next_none": ("Дальше по сценарию шагов нет.",) * 2,
    "only_text": (
        "Пока я понимаю только текст. Напиши, пожалуйста, словами.",
        "Пока я понимаю только текст. Напишите, пожалуйста, словами.",
    ),
    # answers to free text (qa.py, hr.py)
    "small_hello": (
        "Привет! Если есть вопрос о работе, напиши его своими словами, я поищу ответ в документах компании.",
        "Здравствуйте! Если есть вопрос о работе, напишите его своими словами, я поищу ответ в документах компании.",
    ),
    "small_thanks": ("Пожалуйста! Если появятся вопросы, пиши.", "Пожалуйста! Если появятся вопросы, пишите."),
    "small_how": (
        "Всё хорошо, спасибо! Если появится вопрос о работе, пиши.",
        "Всё хорошо, спасибо! Если появится вопрос о работе, пишите.",
    ),
    "small_ok": ("Хорошо 👍",) * 2,
    "small_bye": ("Пока! Если что, я здесь.", "До свидания! Если что, я здесь."),
    "unclear": (
        "Не совсем понял вопрос. Напиши, пожалуйста, подробнее, и я поищу ответ в документах.",
        "Не совсем понял вопрос. Напишите, пожалуйста, подробнее, и я поищу ответ в документах.",
    ),
    "found": ("Вот что сказано в документе:",) * 2,
    "source_is": ("Это из документа:",) * 2,
    "simple_quote": (
        "⚙️ Сейчас я работаю в упрощённом режиме: умный поиск временно недоступен, поэтому отвечаю цитатой "
        "из документа. Если это не то, напиши «нужен человек».",
        "⚙️ Сейчас я работаю в упрощённом режиме: умный поиск временно недоступен, поэтому отвечаю цитатой "
        "из документа. Если это не то, напишите «нужен человек».",
    ),
    "simple_mode": ("⚙️ Сейчас я работаю в упрощённом режиме: умный поиск временно недоступен",) * 2,
    # used when the scenario has no «эскалация» step
    "escalated": ("В документах этого нет. Спросил у коллег из HR, ответ придёт сюда.",) * 2,
    "escalated_topic": (
        "С этим лучше помогут коллеги из HR: передал им твой вопрос, ответ придёт сюда.",
        "С этим лучше помогут коллеги из HR: передал им ваш вопрос, ответ придёт сюда.",
    ),
    "escalated_human": ("Хорошо, передал вопрос коллегам из HR. Ответ придёт сюда.",) * 2,
    "hr_answer": ("По твоему вопросу «{question}»: {answer}", "По вашему вопросу «{question}»: {answer}"),
}

_steps: list[Step] = []
_holidays: set[date] = set()
_locks: dict[int, asyncio.Lock] = {}
_refusals: dict[tuple[int, str], int] = {}


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


def holidays() -> set[date]:
    return _holidays


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
    _sync_tasks(employee)
    sent = await _send_due(employee, now)
    employee = _employee(employee_id)
    # a nudge never interrupts the scenario: not in the same round as a step, not while a survey waits
    quiet = not sent and not _waiting(employee)
    nudged = await _check_tasks(employee, now, nudge=quiet)
    if quiet and not nudged and care.due_check_in(employee, now):
        await care.check_in(employee, now)
    return sent


def _sync_tasks(employee) -> None:
    """Puts the scenario's tasks into the newcomer's checklist, all at once, with their deadlines."""
    have = {r["step_id"] for r in db.query("SELECT step_id FROM tasks WHERE employee_id = ? AND step_id IS NOT NULL",
                                          (employee["id"],))}
    for step in _timed_steps(employee):
        if not step.task or step.id in have:
            continue
        at = _due_at(employee, step)
        if at is not None and _plan(employee, step, at) == "skip":
            continue  # a late newcomer does not need to collect documents for a day already gone
        due = _due_at(employee, dataclasses.replace(step, when=step.task_due)) if step.task_due else at
        if due is not None and due < employee_now(employee) - LATE_LIMIT:
            continue  # the moment is long gone: asking about first-day documents in week three only annoys
        tasks.add_from_step(employee["id"], step.id, step.task, step.task_check, due)


async def _check_tasks(employee, now: datetime, nudge: bool) -> bool:
    """Asks about one overdue task at a time, in working hours; a task asked about twice goes to the recruiter."""
    schedule = hours.of_employee(employee)
    if nudge and (schedule is None or schedule.is_open(now, _holidays)):
        task = tasks.next_to_nudge(employee["id"], now)
        if task is not None:
            await tasks.nudge(employee, task, now)
            return True
    for task in db.query("SELECT * FROM tasks WHERE employee_id = ? AND done_at IS NULL AND escalated_at IS NULL"
                         " AND nudges >= ?", (employee["id"], tasks.MAX_NUDGES)):
        if now - datetime.fromisoformat(task["nudged_at"]) >= tasks.NUDGE_EVERY:
            db.execute("UPDATE tasks SET escalated_at = ? WHERE id = ?", (_stamp(), task["id"]))
            await hr.notify_team(employee, f"Задача не выполнена: «{task['title']}». Новичок не ответил на два "
                                           "напоминания, возможно, ему нужна помощь.")
    return False


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
    due, catch_up = [], []
    for step in _timed_steps(employee):
        if step.id in statuses:
            continue
        at = _due_at(employee, step)
        if at is None or at > now:
            continue
        plan = _plan(employee, step, at)
        if plan == "skip":
            _mark(employee["id"], step.id, "skipped")
        elif plan == "catch_up":
            catch_up.append(((at, step.row), step))
        else:
            # the start steps come first even when a step of the day before the start is already due
            due.append(((step.when != START, at, step.row), step))
    due.sort(key=lambda item: item[0])
    catch_up.sort(key=lambda item: item[0])
    room = _catch_up_room(employee, now)
    queue = [(step, False) for _, step in due] + [(step, True) for _, step in catch_up[:room]]

    sent = 0
    for step, short in queue:
        try:
            await _send_step(employee, step, short)
        except max_api.MaxError as e:
            await _send_failed(employee, step, e)
            break
        _refusals.pop((employee["id"], step.id), None)
        sent += 1
        if step.kind in BLOCKING:
            break
    return sent


async def _send_failed(employee, step: Step, error: max_api.MaxError) -> None:
    key = (employee["id"], step.id)
    if error.status in RECIPIENT_REFUSALS:
        _refusals[key] = _refusals.get(key, 0) + 1
    if _refusals.get(key, 0) < SEND_TRIES:
        log.warning("employee %s: step %s not sent, will try again: %s", employee["id"], step.id, error)
        return
    del _refusals[key]
    _mark(employee["id"], step.id, "skipped")
    log.warning("employee %s: step %s skipped, MAX refused it %s times: %s", employee["id"], step.id, SEND_TRIES,
                error)
    await _check_survey(employee, step)


async def _send_step(employee, step: Step, short: bool = False) -> None:
    text = render_text(step, employee["address"], employee["first_name"], short)
    if not db.query_one("SELECT 1 FROM sent_steps WHERE employee_id = ? AND status != 'skipped'", (employee["id"],)):
        # the very first message tells what we keep and how to delete it
        text += "\n\n" + phrase(employee, "privacy")
    await messaging.send_to_employee(employee, text, buttons=_buttons(step), image=step.image or None)
    _mark(employee["id"], step.id, "sent")
    await managers.on_step(employee, step)
    if step.kind in BLOCKING:
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
    elif step.kind in SURVEY and not await send_special(employee, REMINDER, buttons=buttons):
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
        elif action == "task":
            await tasks.on_button(employee, rest, hr.notify_team)
        elif action == "fb":
            await hr.on_feedback(employee, rest)
        elif action == "mood":
            await care.on_mood(employee, rest)
        else:
            log.info("employee %s: unknown button payload", employee["id"])


async def on_text(employee, text: str) -> bool:
    async with _lock(employee["id"]):
        employee = _employee(employee["id"])
        waiting = _waiting(employee) if employee else None
        if not waiting:
            return False
        if waiting["kind"] == "talk":
            _clear_waiting(employee["id"])
            await care.heard(employee, text)
            return True
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
    try:
        await hr.survey_finished(employee, ids)
    except Exception:
        # the answers are saved; a summary that did not reach HR must not stop the scenario
        log.exception("employee %s: survey summary failed", employee["id"])


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
    now = employee_now(employee)
    for step in _timed_steps(employee):
        if step.id in statuses:
            continue
        at = _due_at(employee, step)
        if at is None:
            continue
        plan = _plan(employee, step, at)
        if plan == "catch_up":
            upcoming.append((max(at, _catch_up_at(employee, now)), step.row, step.id))
        elif plan == "send":
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
            for table in ("messages", "answers", "pulse", "escalations", "sent_steps", "tasks", "bot_answers",
                          "lesson_progress"):
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


def is_late(employee) -> bool:
    """Opened the link a week after HR added them, or only after the first working day."""
    if not employee["linked_at"]:
        return False
    linked = datetime.fromisoformat(employee["linked_at"])
    first_day = date.fromisoformat(employee["start_date"])
    return (linked - datetime.fromisoformat(employee["created_at"]) >= LATE_JOIN
            or linked.astimezone(ZoneInfo(employee["timezone"])).date() > first_day)


def _plan(employee, step: Step, at: datetime) -> str:
    """send, skip, or catch_up: a missed required step that goes out later, short and a few a day."""
    if _too_late(employee, step, at):
        return "catch_up" if step.required else "skip"
    if step.when == START and not step.required and step.kind != "обращение" and is_late(employee):
        return "skip"  # the long hello before the first day is no use to someone who has worked for a week
    return "send"


def _catch_up_room(employee, now: datetime) -> int:
    schedule = hours.of_employee(employee)
    if schedule is not None and not schedule.is_open(now, _holidays):
        return 0
    zone = ZoneInfo(employee["timezone"])
    today = now.astimezone(zone).date()
    done = 0
    for row in db.query("SELECT step_id, sent_at FROM sent_steps WHERE employee_id = ? AND status != 'skipped'",
                        (employee["id"],)):
        step = _step(row["step_id"])
        if (step is not None and step.required and step.when and step.when not in SPECIAL_WHEN
                and datetime.fromisoformat(row["sent_at"]).astimezone(zone).date() == today):
            at = _due_at(employee, step)
            if at is not None and _too_late(employee, step, at):
                done += 1
    return max(CATCH_UP_PER_DAY - done, 0)


def _catch_up_at(employee, now: datetime) -> datetime:
    """When the next missed required step may go out: now, or the next working morning."""
    schedule = hours.of_employee(employee)
    at = now
    if _catch_up_room(employee, now) == 0 and (schedule is None or schedule.is_open(now, _holidays)):
        zone = ZoneInfo(employee["timezone"])
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        at = datetime.combine(tomorrow, time(0), tzinfo=zone).astimezone(timezone.utc)
    return schedule.next_open(at, _holidays) if schedule is not None else at


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
