"""The newcomer's checklist: tasks from the scenario and from people, and the bot asking whether they are done."""
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db, max_api, messaging

log = logging.getLogger(__name__)

NUDGE_EVERY = timedelta(hours=24)
MAX_NUDGES = 2  # after the second «ещё нет» the recruiter hears about it
DUE_TIME = time(18, 0)  # a task given a date without time is due by the end of that day
MAX_TITLE = 200

# (на «ты», на «вы»), like the phrases in engine.py
PHRASES = {
    "nudge": ("Напомню про задачу «{title}». {check}", "Напомню про задачу «{title}». {check}"),
    "check": ("Получилось?",) * 2,
    "done": ("Отлично, отметил: «{title}» ✅", "Отлично, отметил: «{title}» ✅"),
    "later": (
        "Хорошо, спрошу ещё раз завтра. Если что-то мешает, напиши, я передам HR.",
        "Хорошо, спрошу ещё раз завтра. Если что-то мешает, напишите, я передам HR.",
    ),
    "given_up": (
        "Хорошо. Я сообщил рекрутеру, он поможет разобраться.",
        "Хорошо. Я сообщил рекрутеру, он поможет разобраться.",
    ),
    "added": (
        "{who} добавил тебе задачу: «{title}»{due}. Отметить её можно кнопкой ниже или в приложении бота.",
        "{who} добавил вам задачу: «{title}»{due}. Отметить её можно кнопкой ниже или в приложении бота.",
    ),
}


def phrase(employee, key: str, **values) -> str:
    ty, vy = PHRASES[key]
    return (ty if employee["address"] == "ty" else vy).format(**values)


def buttons(task_id: int) -> list[list[dict]]:
    return [[max_api.callback_button("Да, сделано", f"task:{task_id}:done"),
             max_api.callback_button("Ещё нет", f"task:{task_id}:later")]]


def of_employee(employee_id: int) -> list:
    """Open tasks first, the nearest deadline on top; done ones at the end."""
    return db.query("SELECT * FROM tasks WHERE employee_id = ?"
                    " ORDER BY done_at IS NOT NULL, due_at IS NULL, due_at, id", (employee_id,))


def is_overdue(task, now: datetime) -> bool:
    return not task["done_at"] and task["due_at"] is not None and datetime.fromisoformat(task["due_at"]) < now


def add_from_step(employee_id: int, step_id: str, title: str, check: str, due: datetime | None) -> None:
    db.execute("INSERT OR IGNORE INTO tasks (employee_id, title, check_text, step_id, added_by, due_at, created_at)"
               " VALUES (?, ?, ?, ?, 'сценарий', ?, ?)",
               (employee_id, title, check, step_id, _iso(due), db.now_iso()))


async def add(employee, title: str, due_day: str, added_by: str) -> int:
    """A task from the recruiter, HR or the manager; the newcomer hears about it at once, if linked."""
    title = " ".join(title.split())[:MAX_TITLE]
    if not title:
        raise ValueError("Впишите, что нужно сделать.")
    due = None
    if due_day:
        try:
            day = datetime.fromisoformat(due_day).date()
        except ValueError:
            raise ValueError("Не понял срок, выберите дату в календаре.")
        due = datetime.combine(day, DUE_TIME, tzinfo=ZoneInfo(employee["timezone"])).astimezone(timezone.utc)
    task_id = db.execute("INSERT INTO tasks (employee_id, title, added_by, due_at, created_at) VALUES (?, ?, ?, ?, ?)",
                         (employee["id"], title, added_by, _iso(due), db.now_iso()))
    log.info("employee %s: task %s added", employee["id"], task_id)
    if employee["max_user_id"]:
        when = f" до {due.astimezone(ZoneInfo(employee['timezone'])).strftime('%d.%m')}" if due else ""
        try:
            await messaging.send_to_employee(employee, phrase(employee, "added", who=added_by, title=title, due=when),
                                             buttons=buttons(task_id))
        except Exception as e:  # the task is saved and shows in the app, the message is a courtesy
            log.warning("employee %s: new task message failed: %s", employee["id"], e)
    return task_id


def set_done(task_id: int, employee_id: int, done: bool, by: str) -> bool:
    row = db.query_one("SELECT id FROM tasks WHERE id = ? AND employee_id = ?", (task_id, employee_id))
    if row is None:
        return False
    if done:
        db.execute("UPDATE tasks SET done_at = COALESCE(done_at, ?), done_by = COALESCE(done_by, ?) WHERE id = ?",
                   (db.now_iso(), by, task_id))
    else:
        db.execute("UPDATE tasks SET done_at = NULL, done_by = NULL, nudges = 0, nudged_at = NULL WHERE id = ?",
                   (task_id,))
    return True


def remove(task_id: int, employee_id: int) -> bool:
    # scenario tasks come back with the next sync, so only tasks people added can go
    row = db.query_one("SELECT step_id FROM tasks WHERE id = ? AND employee_id = ?", (task_id, employee_id))
    if row is None or row["step_id"]:
        return False
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return True


def next_to_nudge(employee_id: int, now: datetime):
    """The open task whose deadline passed and whose last question was a day ago, if any."""
    for task in db.query("SELECT * FROM tasks WHERE employee_id = ? AND done_at IS NULL AND due_at IS NOT NULL"
                         " AND nudges < ? ORDER BY due_at, id", (employee_id, MAX_NUDGES)):
        if datetime.fromisoformat(task["due_at"]) > now:
            continue
        if task["nudged_at"] and now - datetime.fromisoformat(task["nudged_at"]) < NUDGE_EVERY:
            continue
        return task
    return None


async def nudge(employee, task, now: datetime) -> None:
    text = phrase(employee, "nudge", title=task["title"], check=task["check_text"] or phrase(employee, "check"))
    await messaging.send_to_employee(employee, text, buttons=buttons(task["id"]))
    db.execute("UPDATE tasks SET nudges = nudges + 1, nudged_at = ? WHERE id = ?", (_iso(now), task["id"]))


async def on_button(employee, rest: str, notify) -> None:
    """«Да, сделано» or «Ещё нет» under a nudge; notify(employee, text) tells the recruiter when help is needed."""
    task_id, _, answer = rest.partition(":")
    task = db.query_one("SELECT * FROM tasks WHERE id = ? AND employee_id = ?",
                        (int(task_id) if task_id.isdigit() else 0, employee["id"]))
    if task is None:
        return
    if answer == "done":
        messaging.log_message(employee["id"], "employee", "Да, сделано")
        set_done(task["id"], employee["id"], True, "новичок")
        await messaging.send_to_employee(employee, phrase(employee, "done", title=task["title"]))
        return
    if answer != "later" or task["done_at"]:
        return
    messaging.log_message(employee["id"], "employee", "Ещё нет")
    if task["nudges"] >= MAX_NUDGES and not task["escalated_at"]:
        db.execute("UPDATE tasks SET escalated_at = ? WHERE id = ?", (db.now_iso(), task["id"]))
        await notify(employee, f"Задача не выполнена: «{task['title']}». Новичок дважды ответил «ещё нет», "
                               "возможно, ему нужна помощь.")
        await messaging.send_to_employee(employee, phrase(employee, "given_up"))
    else:
        await messaging.send_to_employee(employee, phrase(employee, "later"))


def counts(employee_id: int, now: datetime) -> tuple[int, int, int]:
    """Done, open and overdue tasks of one newcomer."""
    rows = db.query("SELECT done_at, due_at FROM tasks WHERE employee_id = ?", (employee_id,))
    done = sum(1 for r in rows if r["done_at"])
    overdue = sum(1 for r in rows if is_overdue(r, now))
    return done, len(rows) - done, overdue


def _iso(at: datetime | None) -> str | None:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds") if at else None
