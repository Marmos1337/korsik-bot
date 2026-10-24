"""How likely a newcomer is to leave, from what the bot already knows, and who has gone quiet."""
import logging
from datetime import datetime, timedelta, timezone

from app import db, engine, hours, hr, tasks
from app.scenario import is_bad_answer

log = logging.getLogger(__name__)

WINDOW = timedelta(days=30)
SILENT_AFTER = timedelta(days=4)  # no reply to the bot this long, while the bot kept writing
HIGH, MEDIUM = 50, 25


def score(employee, now: datetime | None = None) -> dict:
    """0-100 with the reasons behind it. Each signal adds points up to its own cap, so one cause cannot fill it all."""
    now = now or datetime.now(timezone.utc)
    if not employee["linked_at"]:
        return {"score": 0, "level": "low", "reasons": []}
    since = (now - WINDOW).isoformat(timespec="seconds")
    points, reasons = 0, []

    def add(value, cap, text):
        nonlocal points
        value = min(value, cap)
        if value:
            points += value
            reasons.append(text)

    flags = db.query("SELECT risk FROM pulse WHERE employee_id = ? AND created_at >= ?", (employee["id"], since))
    red = sum(1 for f in flags if f["risk"] == 2)
    worry = sum(1 for f in flags if f["risk"] == 1)
    add(35 * red, 45, f"красных флагов: {red}")
    add(8 * worry, 16, f"трудностей в сообщениях: {worry}")

    bad = 0
    scores = []
    for a in db.query("SELECT step_id, answer, score FROM answers WHERE employee_id = ? AND created_at >= ?",
                      (employee["id"], since)):
        step = engine._step(a["step_id"])
        if a["score"] is not None:
            scores.append(a["score"])
        if step is not None and is_bad_answer(step, a["answer"], a["score"]):
            bad += 1
    add(10 * bad, 30, f"плохих ответов в опросах: {bad}")
    if scores and sum(scores) / len(scores) < 6:
        add(15, 15, f"средняя оценка {sum(scores) / len(scores):.1f} из 10")

    quiet = silent_for(employee, now)
    if quiet is not None:
        add(15, 15, f"не отвечает боту {quiet.days} дн.")

    overdue = tasks.counts(employee["id"], engine.employee_now(employee))[2]
    add(8 * overdue, 16, f"просроченных задач: {overdue}")

    disliked = db.query_one("SELECT COUNT(*) AS n FROM bot_answers WHERE employee_id = ? AND helpful = 0"
                            " AND created_at >= ?", (employee["id"], since))["n"]
    add(4 * disliked, 8, f"не подошли ответы бота: {disliked}")

    points = min(points, 100)
    level = "high" if points >= HIGH else "medium" if points >= MEDIUM else "low"
    return {"score": points, "level": level, "reasons": reasons}


LEVELS = {"high": ("высокий риск ухода", "bad"), "medium": ("есть сигналы", "warn"), "low": ("риск низкий", "ok")}


def silent_for(employee, now: datetime) -> timedelta | None:
    """How long the newcomer has not answered while the bot wrote at least twice; None if they talk."""
    last_own = db.query_one("SELECT id, created_at FROM messages WHERE employee_id = ? AND role = 'employee'"
                            " ORDER BY id DESC LIMIT 1", (employee["id"],))
    since_id = last_own["id"] if last_own else 0
    since = last_own["created_at"] if last_own else employee["linked_at"]
    bot_after = db.query_one("SELECT COUNT(*) AS n FROM messages WHERE employee_id = ? AND role = 'bot' AND id > ?",
                             (employee["id"], since_id))["n"]
    quiet = now - datetime.fromisoformat(since)
    return quiet if bot_after >= 2 and quiet >= SILENT_AFTER else None


async def warn_about_silent(now: datetime | None = None) -> int:
    """Tells the recruiter once per silence, in their working hours, that a newcomer stopped answering."""
    now = now or datetime.now(timezone.utc)
    told = 0
    for employee in db.query("SELECT * FROM employees WHERE max_user_id IS NOT NULL"):
        quiet = silent_for(employee, now)
        if quiet is None:
            continue
        last = db.query_one("SELECT MAX(id) AS id FROM messages WHERE employee_id = ? AND role = 'employee'",
                            (employee["id"],))["id"] or 0
        key = f"silent:{employee['id']}:{last}"
        if db.kv_get(key):
            continue
        recruiter = db.query_one("SELECT * FROM recruiters WHERE id = ?", (employee["recruiter_id"],)) \
            if employee["recruiter_id"] else None
        if recruiter and not hours.of_recruiter(recruiter).is_open(now, engine.holidays()):
            continue
        db.kv_set(key, now.isoformat(timespec="seconds"))
        await hr.notify_team(employee, f"не отвечает боту {quiet.days} дн. Возможно, стоит позвонить и узнать, "
                                       "всё ли в порядке.")
        log.info("employee %s: silent for %s days, recruiter told", employee["id"], quiet.days)
        told += 1
    return told
