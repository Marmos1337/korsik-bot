"""The newcomer's manager: tips before the start and a memo before each meeting, with what the bot knows."""
import logging

from app import db, engine, hr, messaging, risk, tasks

log = logging.getLogger(__name__)

# managers are addressed on «вы», like recruiters
HELP = (
    "Я Корсик, помощник новичков. Сюда я пишу перед важными моментами адаптации ваших новичков: "
    "подсказки на старте и памятки к встречам. Задачи новичкам можно ставить в приложении бота.\n\n"
    "/сводка - как дела у ваших новичков"
)
LINKED = "Готово, {name}, ваш МАКС привязан.\n\n" + HELP
LINK_UNKNOWN = "Не нашёл такую ссылку руководителя. Попросите, пожалуйста, новую у HR."


def link(max_user_id: int, code: str):
    manager = db.query_one("SELECT * FROM managers WHERE invite_code = ?", (code.strip(),))
    if manager is None or manager["max_user_id"] not in (None, max_user_id):
        return None
    db.execute("UPDATE managers SET max_user_id = NULL WHERE max_user_id = ? AND id != ?", (max_user_id, manager["id"]))
    db.execute("UPDATE managers SET max_user_id = ? WHERE id = ?", (max_user_id, manager["id"]))
    log.info("manager %s linked", manager["id"])
    return db.query_one("SELECT * FROM managers WHERE id = ?", (manager["id"],))


def card(employee) -> str:
    """A few lines about the newcomer for a memo: stage, mood, tasks, what is worrying."""
    lines = [f"{employee['full_name']}: {hr._stage(employee)}"]
    scores = [r["score"] for r in db.query("SELECT score FROM answers WHERE employee_id = ? AND score IS NOT NULL"
                                           " ORDER BY id DESC LIMIT 9", (employee["id"],))]
    if scores:
        lines.append(f"Последние оценки в опросах: в среднем {sum(scores) / len(scores):.1f} из 10")
    done, left, overdue = tasks.counts(employee["id"], engine.employee_now(employee))
    if done or left:
        lines.append(f"Задачи: сделано {done}, осталось {left}" + (f", просрочено {overdue}" if overdue else ""))
    state = risk.score(employee)
    if state["reasons"]:
        lines.append(f"{risk.LEVELS[state['level']][0].capitalize()}: " + "; ".join(state["reasons"]))
    worries = db.query("SELECT topic FROM pulse WHERE employee_id = ? AND risk >= 1 ORDER BY id DESC LIMIT 3",
                       (employee["id"],))
    if worries:
        lines.append("О чём говорил с трудом: " + ", ".join(dict.fromkeys(w["topic"] for w in worries)))
    return "\n".join(lines)


async def on_step(employee, step) -> None:
    """The scenario step went to the newcomer; its «Руководителю» text goes to the manager with the card."""
    if not step.manager_text or not employee["manager_id"]:
        return
    manager = db.query_one("SELECT * FROM managers WHERE id = ?", (employee["manager_id"],))
    if manager is None or not manager["max_user_id"]:
        return
    text = step.manager_text.replace("{новичок}", employee["full_name"]).replace("{имя}", employee["first_name"])
    try:
        await messaging.send_to_user(manager["max_user_id"], f"{text}\n\n{card(employee)}")
    except Exception as e:  # the newcomer's step is out already, a missed memo must not stop the scenario
        log.warning("manager %s: memo failed: %s", manager["id"], e)


def digest(manager_id: int) -> str:
    employees = db.query("SELECT * FROM employees WHERE manager_id = ? ORDER BY start_date, id", (manager_id,))
    if not employees:
        return "За вами пока не закреплён ни один новичок."
    return "\n\n".join(card(e) for e in employees)


async def handle_update(manager, update: dict) -> None:
    text = hr._text(update).lower()
    reply = digest(manager["id"]) if text.startswith("/сводка") else HELP
    await messaging.send_to_user(manager["max_user_id"], reply)
