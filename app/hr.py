import asyncio
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db, engine, hours, knowledge, llm, max_api, messaging
from app.config import cfg

log = logging.getLogger(__name__)

CONTEXT_SIZE = 5
FOLLOWUP_SECONDS = 60
REMIND_EVERY = timedelta(hours=1)
MAX_REMINDERS = 5
OVERDUE_AFTER = timedelta(hours=2)  # after this an open question shows red in the cabinet
OFF_HOURS_GRACE = timedelta(minutes=30)  # a question waiting this long while the recruiter is off work shows red
WAIT_LIMIT = timedelta(hours=24)  # a forgotten «Ответить» must not catch a message written days later
TOPICS = ("доступы", "наставник", "руководитель", "нагрузка", "коллеги", "условия", "оформление", "другое")
TONES = ("позитивный", "нейтральный", "негативный")
ROLE_NAMES = {"employee": "Новичок", "bot": "Бот", "hr": "HR"}
ESCALATION_STEP = "эскалация"  # the scenario row with the «I could not find it, asking HR» text

REASONS = {
    "stop_topic": "тема, которую бот сам не обсуждает",
    "no_match": "в документах нет ничего похожего",
    "no_answer": "в найденных документах нет ответа",
    "unchecked": "ответ модели не прошёл проверку по документам",
    "no_source": "модель не указала, из какого документа ответ",
    "asked_human": "новичок попросил ответ человека",
}

# word markers for the simple mode, when there is no model to read the mood
RED_FLAGS = ("уволюсь", "уволиться", "увольняюсь", "хочу уйти", "думаю уйти", "думаю об уходе", "уйти из компании",
             "никто не помогает", "не справляюсь", "ненавиж", "достали", "надоело", "выгора", "униж", "кричит",
             "орет", "травл", "домогат")
WORRIES = ("сложно", "трудно", "тяжело", "непонятно", "не понимаю", "не понятно", "устал", "плохо", "проблем",
           "не получается", "не нравится", "страшно", "обидно", "не успеваю", "не хватает", "не могу")
TOPIC_MARKERS = {
    "доступы": ("доступ", "пароль", "учетк", "логин", "пропуск", "ноутбук", "компьютер", "техник"),
    "наставник": ("наставник", "ментор"),
    "руководитель": ("руководител", "начальник", "шеф"),
    "нагрузка": ("нагрузк", "переработ", "не успеваю", "завал", "много задач", "дедлайн"),
    "коллеги": ("коллег", "команд", "коллектив"),
    "условия": ("зарплат", "оплат", "график", "офис", "рабочее место", "столов", "обед", "отпуск"),
    "оформление": ("документ", "договор", "оформ", "справк", "трудов", "больничн", "заявлен"),
}

PULSE_PROMPT = """Оцени сообщение нового сотрудника компании и верни JSON с полями:
"tone": "позитивный", "нейтральный" или "негативный";
"topic": одна тема из списка: доступы, наставник, руководитель, нагрузка, коллеги, условия, оформление, другое;
"risk": 0, если всё в порядке; 1, если есть недовольство или трудность; 2, если это красный флаг: хочет уйти, конфликт, давление, сильный стресс;
"reason": коротко, почему такой риск.

Сообщение: {text}"""

# HR is always addressed on «вы»
RECRUITER_HELP = (
    "Сюда приходят вопросы новичков, на которые я не нашёл ответа в документах, и красные флаги. "
    "Чтобы ответить, нажмите «Ответить» под вопросом и напишите ответ следующим сообщением.\n\n"
    "/сводка - сводка по вашим новичкам"
)
RECRUITER_LINKED = "Готово, {name}, ваш МАКС привязан.\n\n" + RECRUITER_HELP
RECRUITER_LINK_UNKNOWN = "Не нашёл такую ссылку рекрутера. Возьмите, пожалуйста, новую в кабинете."
MODEL_DOWN_TEXT = (
    "Языковая модель недоступна, я перешёл в упрощённый режим: на вопросы отвечаю цитатами из документов "
    "или передаю их вам. Сценарий и опросы работают как обычно."
)
MODEL_UP_TEXT = "Языковая модель снова доступна, отвечаю на вопросы как обычно."
QUESTION_GONE = "Этого вопроса больше нет: новичок удалил свои данные."
NOT_DELIVERED = ("Не получилось доставить ответ новичку, вопрос остался открытым. "
                 "Нажмите «Ответить» под вопросом и пришлите ответ ещё раз.")
NOT_YOUR_QUESTION = "Этот новичок закреплён за другим рекрутером, вопрос ждёт его ответа."


async def escalate(employee, question: str, reason: str) -> int:
    context = messaging.history(employee["id"], CONTEXT_SIZE)
    escalation_id = db.execute(
        "INSERT INTO escalations (employee_id, question, context, created_at) VALUES (?, ?, ?, ?)",
        (employee["id"], question, json.dumps(context, ensure_ascii=False), db.now_iso()),
    )
    log.info("employee %s: question escalated (%s), escalation %s", employee["id"], reason, escalation_id)
    # sqlite gives the id of a deleted question to the next one, so an old «added to the base» mark must go
    db.execute("DELETE FROM kv WHERE key = ?", (_kb_key(escalation_id),))
    try:
        await _tell_employee(employee, reason)
    except Exception:
        # the question is saved; the recruiter should still hear about it
        log.exception("employee %s: could not tell about the escalation", employee["id"])

    lines = [f"Вопрос от новичка: {_who(employee)}", f"Почему передал: {REASONS.get(reason, reason)}", "",
             f"«{question}»"]
    if context:
        lines += ["", "Последние сообщения:"]
        lines += [f"{ROLE_NAMES.get(m['role'], m['role'])}: {_cut(m['text'], 300)}" for m in context]
    button = [[max_api.callback_button("Ответить", f"esc:reply:{escalation_id}")]]
    await notify_recruiter(employee, "\n".join(lines), buttons=button)
    return escalation_id


async def _tell_employee(employee, reason: str) -> None:
    if reason == "stop_topic":
        await engine.say(employee, "escalated_topic")
    elif reason == "asked_human":
        await engine.say(employee, "escalated_human")
    else:
        # without the model the bot only searched by words, so the employee should know why it gave up
        extra = "" if llm.is_up() else engine.phrase(employee, "simple_mode")
        if not await engine.send_special(employee, ESCALATION_STEP, extra=extra):
            text = engine.phrase(employee, "escalated")
            await messaging.send_to_employee(employee, f"{text}\n\n{extra}" if extra else text)


async def deliver_answer(escalation_id: int, answer: str, answered_by: str, employee_id: int,
                         asked_at: str, force: bool = False) -> datetime | None:
    """Closes the question and sends the HR answer to the employee. Also used by the admin pages.

    Outside the newcomer's working hours the answer waits, and the moment it will go out is returned;
    None means it went out now. ValueError with a text for people: the question is gone or answered already.
    """
    # sqlite gives the id of a deleted question to the next one, even to the same newcomer after /delete,
    # so the caller also names whose question it means and when it was asked
    key = (escalation_id, employee_id, asked_at)
    row = _question(*key)
    if row is None:
        raise ValueError(QUESTION_GONE)
    answer = answer.strip()
    if not _claim(*key, answer, answered_by):
        row = _question(*key)
        raise ValueError(_answered_by(row) if row else QUESTION_GONE)
    employee = db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    later = None if force else answer_time(employee)
    if later is not None:
        log.info("escalation %s answered, waits for the newcomer's working hours", escalation_id)
        return later
    try:
        await _send_answer(employee, row["question"], answer, escalation_id)
    except Exception:
        # the employee got nothing: the question goes back to open and can be answered again
        db.execute("UPDATE escalations SET status = 'open', answer = NULL, answered_by = NULL, answered_at = NULL"
                   " WHERE id = ? AND employee_id = ? AND created_at = ? AND status = 'answered'", key)
        raise
    return None


def answer_time(employee) -> datetime | None:
    """None if the newcomer is at work now, else when the working day of their workplace starts."""
    schedule = hours.of_employee(employee)
    now = engine.employee_now(employee)
    if schedule is None or schedule.is_open(now, engine.holidays()):
        return None
    # the shift is the demo rewind; the real moment is what the scheduler compares with
    return schedule.next_open(now, engine.holidays()) - timedelta(seconds=employee["time_shift"] or 0)


async def _send_answer(employee, question: str, answer: str, escalation_id: int) -> None:
    text = engine.phrase(employee, "hr_answer").format(question=_cut(question, 200), answer=answer)
    for part in messaging.split_text(text):
        await max_api.send_message(part, user_id=employee["max_user_id"])
    db.execute("UPDATE escalations SET delivered_at = ? WHERE id = ?", (db.now_iso(), escalation_id))
    messaging.log_message(employee["id"], "hr", text)
    log.info("escalation %s answered", escalation_id)


async def deliver_waiting_answers() -> int:
    """Answers given outside the newcomer's working hours go out when their day starts."""
    sent = 0
    rows = db.query("SELECT q.* FROM escalations q JOIN employees e ON e.id = q.employee_id"
                    " WHERE q.status = 'answered' AND q.delivered_at IS NULL AND q.answer IS NOT NULL"
                    " AND e.max_user_id IS NOT NULL ORDER BY q.id")
    for row in rows:
        employee = db.query_one("SELECT * FROM employees WHERE id = ?", (row["employee_id"],))
        if answer_time(employee) is not None:
            continue
        try:
            await _send_answer(employee, row["question"], row["answer"], row["id"])
            sent += 1
        except Exception as e:
            # stays waiting and goes on the next round; the recruiter has done their part
            log.warning("escalation %s: waiting answer was not sent: %s", row["id"], e)
    return sent


async def send_waiting_now(escalation_id: int) -> None:
    """HR sends a waiting answer at once, without looking at the newcomer's hours."""
    row = db.query_one("SELECT * FROM escalations WHERE id = ? AND status = 'answered' AND delivered_at IS NULL",
                       (escalation_id,))
    if row is None:
        raise ValueError("Этот ответ уже отправлен.")
    employee = db.query_one("SELECT * FROM employees WHERE id = ?", (row["employee_id"],))
    if employee is None or not employee["max_user_id"]:
        raise ValueError("Новичок не привязан к боту, отправить некому.")
    await _send_answer(employee, row["question"], row["answer"], row["id"])


async def remind_recruiters(now: datetime | None = None) -> int:
    """An unanswered question reminds its recruiter once an hour, up to five times, only in their working hours."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    rows = db.query(
        "SELECT q.*, r.id AS rid FROM escalations q JOIN employees e ON e.id = q.employee_id"
        " JOIN recruiters r ON r.id = e.recruiter_id"
        " WHERE q.status = 'open' AND r.max_user_id IS NOT NULL AND q.reminders < ? ORDER BY q.id",
        (MAX_REMINDERS,))
    for row in rows:
        recruiter = db.query_one("SELECT * FROM recruiters WHERE id = ?", (row["rid"],))
        if not hours.of_recruiter(recruiter).is_open(now, engine.holidays()):
            continue
        last = datetime.fromisoformat(row["reminded_at"] or row["created_at"])
        if now - last < REMIND_EVERY:
            continue
        employee = db.query_one("SELECT * FROM employees WHERE id = ?", (row["employee_id"],))
        number = row["reminders"] + 1
        text = (f"Напоминание {number} из {MAX_REMINDERS}: вопрос от новичка {_who(employee)} ждёт ответа "
                f"{hours.waited_text(row['created_at'], now)}.\n\n«{_cut(row['question'], 500)}»")
        if number == MAX_REMINDERS:
            text += "\n\nЭто последнее напоминание, дальше вопрос виден в кабинете HR как просроченный."
        db.execute("UPDATE escalations SET reminders = ?, reminded_at = ? WHERE id = ?",
                   (number, now.isoformat(timespec="seconds"), row["id"]))
        await _send_safely(recruiter["max_user_id"], None, text,
                           [[max_api.callback_button("Ответить", f"esc:reply:{row['id']}")]])
        sent += 1
    return sent


async def followup_loop() -> None:
    while True:
        try:
            await deliver_waiting_answers()
            await remind_recruiters()
        except Exception:
            log.exception("follow-up round failed")
        await asyncio.sleep(FOLLOWUP_SECONDS)


def question_state(question, now: datetime | None = None) -> dict:
    """How an open question looks to HR: how long it waits and why it is red, if it is."""
    now = now or datetime.now(timezone.utc)
    waited = now - datetime.fromisoformat(question["created_at"])
    state = {"waited": hours.waited_text(question["created_at"], now), "overdue": False, "notes": [],
             "reminders": question["reminders"]}
    employee = db.query_one("SELECT recruiter_id FROM employees WHERE id = ?", (question["employee_id"],))
    recruiter = None
    if employee and employee["recruiter_id"]:
        recruiter = db.query_one("SELECT * FROM recruiters WHERE id = ?", (employee["recruiter_id"],))
    if recruiter is None:
        state["overdue"] = True
        state["notes"].append("рекрутер не назначен: ответить можно только здесь")
        return state
    schedule = hours.of_recruiter(recruiter)
    working = schedule.is_open(now, engine.holidays())
    state["recruiter"] = recruiter["name"]
    state["working"] = working
    if not recruiter["max_user_id"]:
        state["overdue"] = True
        state["notes"].append(f"{recruiter['name']} не привязал МАКС: вопрос виден только здесь")
    if not working:
        back_at = hours.when_text(schedule.next_open(now, engine.holidays()), recruiter["timezone"])
        state["notes"].append(f"{recruiter['name']} сейчас не работает ({schedule.describe()}), вернётся {back_at}")
        if waited >= OFF_HOURS_GRACE:
            state["overdue"] = True
    if question["reminders"] >= MAX_REMINDERS:
        state["overdue"] = True
        state["notes"].append(f"{MAX_REMINDERS} напоминаний рекрутеру без ответа")
    if waited >= OVERDUE_AFTER:
        state["overdue"] = True
    return state


def overdue_count() -> int:
    now = datetime.now(timezone.utc)
    return sum(question_state(q, now)["overdue"]
               for q in db.query("SELECT * FROM escalations WHERE status = 'open'"))


def recruiter_stats(recruiter_id: int) -> dict:
    rows = db.query("SELECT q.* FROM escalations q JOIN employees e ON e.id = q.employee_id"
                    " WHERE e.recruiter_id = ?", (recruiter_id,))
    answered = [r for r in rows if r["status"] == "answered" and r["answered_at"]]
    minutes = sorted((datetime.fromisoformat(r["answered_at"]) - datetime.fromisoformat(r["created_at"]))
                     .total_seconds() / 60 for r in answered)
    now = datetime.now(timezone.utc)
    opened = [r for r in rows if r["status"] == "open"]
    return {
        "total": len(rows),
        "answered": len(answered),
        "open": len(opened),
        "overdue": sum(question_state(r, now)["overdue"] for r in opened),
        "waiting_delivery": sum(1 for r in answered if not r["delivered_at"]),
        "median": _minutes_text(minutes[len(minutes) // 2]) if minutes else "",
        "within_hour": round(100 * sum(1 for m in minutes if m <= 60) / len(minutes)) if minutes else None,
        "reminders": sum(r["reminders"] for r in rows),
    }


def _minutes_text(minutes: float) -> str:
    return hours.waited_text("2000-01-01T00:00:00+00:00",
                             datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes))


def change_workplace(employee_id: int, workplace_id: int | None) -> None:
    """Moves a newcomer to another workplace: its tags replace the old workplace's tags, its zone comes along."""
    employee = db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    old = db.query_one("SELECT * FROM workplaces WHERE id = ?", (employee["workplace_id"],)) \
        if employee["workplace_id"] else None
    new = db.query_one("SELECT * FROM workplaces WHERE id = ?", (workplace_id,)) if workplace_id else None
    old_tags = {t.lower() for t in db.split_tags(old["tags"])} if old else set()
    # tags HR typed by hand for this newcomer stay, only the workplace's own ones change
    own = [t for t in db.split_tags(employee["tags"]) if t.lower() not in old_tags]
    tags = db.split_tags(new["tags"]) if new else []
    tags += [t for t in own if t.lower() not in {x.lower() for x in tags}]
    db.execute("UPDATE employees SET workplace_id = ?, tags = ?, timezone = ? WHERE id = ?",
               (new["id"] if new else None, ", ".join(tags), new["timezone"] if new else employee["timezone"],
                employee_id))
    log.info("employee %s moved to workplace %s", employee_id, new["id"] if new else None)


def _question(escalation_id: int, employee_id: int, asked_at: str):
    return db.query_one("SELECT * FROM escalations WHERE id = ? AND employee_id = ? AND created_at = ?",
                        (escalation_id, employee_id, asked_at))


def _claim(escalation_id: int, employee_id: int, asked_at: str, answer: str, answered_by: str) -> bool:
    """Closes an open question in one statement: two recruiters may press «Ответить» at the same time."""
    conn = db.connect()
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE escalations SET status = 'answered', answer = ?, answered_by = ?, answered_at = ?"
                " WHERE id = ? AND employee_id = ? AND created_at = ? AND status = 'open'",
                (answer, answered_by, db.now_iso(), escalation_id, employee_id, asked_at),
            )
        return cursor.rowcount == 1
    finally:
        conn.close()


def _answered_by(escalation) -> str:
    return f"На этот вопрос уже ответили ({escalation['answered_by']})."


def link_recruiter(max_user_id: int, code: str):
    recruiter = db.query_one("SELECT * FROM recruiters WHERE invite_code = ?", (code.strip(),))
    if recruiter is None or recruiter["max_user_id"] not in (None, max_user_id):
        return None
    # the account moves from an older recruiter card, max_user_id is unique
    db.execute("UPDATE recruiters SET max_user_id = NULL WHERE max_user_id = ? AND id != ?",
               (max_user_id, recruiter["id"]))
    db.execute("UPDATE recruiters SET max_user_id = ? WHERE id = ?", (max_user_id, recruiter["id"]))
    log.info("recruiter %s linked", recruiter["id"])
    return db.query_one("SELECT * FROM recruiters WHERE id = ?", (recruiter["id"],))


def is_recruiter_update(recruiter, update: dict) -> bool:
    """For an account that is a recruiter and a newcomer at once: does this update belong to the recruiter side?"""
    if update.get("update_type") == "message_callback":
        return ((update.get("callback") or {}).get("payload") or "").startswith("esc:")
    text = _text(update).lower()
    return text.startswith(("/сводка", "/отмена")) or _waiting_for(recruiter) is not None


async def handle_recruiter_update(recruiter, update: dict) -> None:
    kind = update.get("update_type")
    if kind == "message_callback":
        callback = update.get("callback") or {}
        await _ack(callback.get("callback_id"))
        await _on_button(recruiter, callback.get("payload") or "", _in_hr_chat(update))
    elif kind == "message_created":
        await _on_text(recruiter, _text(update))
    else:
        await _to_recruiter(recruiter, RECRUITER_HELP)


async def _on_button(recruiter, payload: str, in_hr_chat: bool) -> None:
    parts = payload.split(":")
    if len(parts) < 3 or parts[0] != "esc" or not parts[2].isdigit():
        return
    action, escalation_id = parts[1], int(parts[2])
    escalation = db.query_one(
        "SELECT e.*, emp.tags, emp.recruiter_id FROM escalations e JOIN employees emp ON emp.id = e.employee_id"
        " WHERE e.id = ?",
        (escalation_id,),
    )
    if escalation is None:
        await _to_recruiter(recruiter, QUESTION_GONE)
    elif not (in_hr_chat or recruiter["id"] == escalation["recruiter_id"]
              or (action == "kb" and escalation["answered_by"] == recruiter["name"])):
        # in a private chat only the newcomer's own recruiter, or for «в базу» the one who answered from the HR chat
        await _to_recruiter(recruiter, NOT_YOUR_QUESTION)
    elif action == "reply":
        if escalation["status"] == "answered":
            await _to_recruiter(recruiter, _answered_by(escalation))
            return
        db.kv_set(_wait_key(recruiter),
                  f"{escalation_id} {escalation['employee_id']} {escalation['created_at']} {db.now_iso()}")
        await _to_recruiter(
            recruiter,
            f"Напишите ответ на вопрос «{_cut(escalation['question'], 200)}» следующим сообщением, "
            "я передам его новичку.\n\n/отмена - не отвечать",
        )
    elif action == "kb" and len(parts) == 4:
        await _save_to_knowledge(recruiter, escalation, parts[3] == "yes")


async def _save_to_knowledge(recruiter, escalation, wanted: bool) -> None:
    done_key = _kb_key(escalation["id"])
    if not wanted or db.kv_get(done_key) or not escalation["answer"]:
        if not wanted:
            await _to_recruiter(recruiter, "Хорошо, в базу знаний не добавляю.")
        return
    db.kv_set(done_key, "1")
    knowledge.add_hr_answer(escalation["question"], escalation["answer"], db.split_tags(escalation["tags"]))
    if llm.is_up():
        await knowledge.embed_missing()  # without a vector the pair would not pass the similarity threshold
    log.info("escalation %s added to the knowledge base", escalation["id"])
    await _to_recruiter(recruiter, "Добавил в базу знаний. В следующий раз отвечу на такой вопрос сам.")


async def _on_text(recruiter, text: str) -> None:
    command = text.split()[0].lower() if text.startswith("/") else ""
    waiting = _waiting_for(recruiter)
    if command == "/сводка":
        await _to_recruiter(recruiter, digest_text(recruiter["id"]))
    elif command == "/отмена":
        db.kv_set(_wait_key(recruiter), "")
        await _to_recruiter(recruiter, "Хорошо, ответ отменён.")
    elif waiting is not None and text and not command:
        db.kv_set(_wait_key(recruiter), "")
        escalation_id, employee_id, asked_at = waiting
        try:
            later = await deliver_answer(escalation_id, text, recruiter["name"], employee_id, asked_at)
        except ValueError as e:
            await _to_recruiter(recruiter, str(e))
            return
        except Exception as e:
            # deliver_answer has reopened the question; without this line the recruiter would think it went out
            log.warning("escalation %s: answer was not sent: %s", escalation_id, type(e).__name__)
            await _to_recruiter(recruiter, NOT_DELIVERED)
            return
        buttons = [[max_api.callback_button("Добавить в базу знаний", f"esc:kb:{escalation_id}:yes"),
                    max_api.callback_button("Не надо", f"esc:kb:{escalation_id}:no")]]
        done = "Отправил ответ новичку."
        if later is not None:
            employee = db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
            done = (f"Ответ принят. У новичка сейчас нерабочее время, он получит ответ "
                    f"{hours.when_text(later, employee['timezone'])} по своему времени.")
        await _to_recruiter(recruiter, done + " Добавить пару «вопрос-ответ» в базу знаний, "
                                              "чтобы в следующий раз я ответил сам?", buttons)
    elif waiting is not None:
        await _to_recruiter(recruiter, "Пришлите, пожалуйста, ответ текстом.")
    else:
        await _to_recruiter(recruiter, RECRUITER_HELP)


def _waiting_for(recruiter) -> tuple[int, int, str] | None:
    """(id, newcomer, asked at) of the question the recruiter pressed «Ответить» on less than a day ago."""
    parts = (db.kv_get(_wait_key(recruiter)) or "").split()
    if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    if datetime.now(timezone.utc) - datetime.fromisoformat(parts[3]) > WAIT_LIMIT:
        return None
    return int(parts[0]), int(parts[1]), parts[2]


def _in_hr_chat(update: dict) -> bool:
    recipient = (update.get("message") or {}).get("recipient") or {}
    return bool(cfg.HR_CHAT_ID) and recipient.get("chat_id") == cfg.HR_CHAT_ID


def _kb_key(escalation_id: int) -> str:
    return f"hr_kb:{escalation_id}"


def _wait_key(recruiter) -> str:
    return f"hr_wait:{recruiter['id']}"


async def _to_recruiter(recruiter, text: str, buttons=None) -> None:
    await messaging.send_to_user(recruiter["max_user_id"], text, buttons)


async def _ack(callback_id: str | None) -> None:
    if not callback_id:
        return
    try:
        await max_api.answer_callback(callback_id, engine.ACK_TEXT)
    except Exception as e:
        log.warning("answering a callback failed: %s", e)


async def notify_recruiter(employee, text: str, buttons=None) -> None:
    recruiter = None
    if employee["recruiter_id"]:
        recruiter = db.query_one("SELECT * FROM recruiters WHERE id = ?", (employee["recruiter_id"],))
    if recruiter and recruiter["max_user_id"]:
        await _send_safely(recruiter["max_user_id"], None, text, buttons)
    if cfg.HR_CHAT_ID:
        await _send_safely(None, cfg.HR_CHAT_ID, text, buttons)


async def _send_safely(user_id: int | None, chat_id: int | None, text: str, buttons=None) -> None:
    # a notification that did not go out must not break what caused it: the record is in the database anyway
    try:
        parts = messaging.split_text(text)
        for i, part in enumerate(parts):
            keyboard = buttons if i == len(parts) - 1 else None
            await max_api.send_message(part, user_id=user_id, chat_id=chat_id, buttons=keyboard)
    except Exception as e:
        log.warning("notification to %s failed: %s", user_id or chat_id, e)


async def on_llm_mode_change(up: bool) -> None:
    text = MODEL_UP_TEXT if up else MODEL_DOWN_TEXT
    for row in db.query("SELECT max_user_id FROM recruiters WHERE max_user_id IS NOT NULL"):
        await _send_safely(row["max_user_id"], None, text)
    if cfg.HR_CHAT_ID:
        await _send_safely(None, cfg.HR_CHAT_ID, text)


async def survey_finished(employee, step_ids: list[str]) -> None:
    marks = ",".join("?" * len(step_ids))
    rows = db.query(
        f"SELECT question, answer, score FROM answers WHERE employee_id = ? AND step_id IN ({marks}) ORDER BY id",
        (employee["id"], *step_ids),
    )
    if not rows:
        return
    lines = [f"Опрос пройден: {_who(employee)}", ""]
    for row in rows:
        answer = f"{row['score']} из 10" if row["score"] is not None else row["answer"]
        lines.append(f"- {_short_question(row['question'])}: {_cut(answer, 300)}")
    await notify_recruiter(employee, "\n".join(lines))


def _short_question(text: str) -> str:
    # survey steps start with «Привет, Аня!» or «3 из 9.», the question itself is the last sentence with «?»
    questions = re.findall(r"[^.!?\n]*\?", text)
    question = questions[-1] if questions else text.splitlines()[0]
    return _cut(question.strip(), 100)


async def analyze_pulse(employee, text: str) -> None:
    mark = None
    if llm.is_up():
        try:
            mark = _parse_pulse(await llm.chat([{"role": "user", "content": PULSE_PROMPT.format(text=text)}],
                                               json_mode=True))
        except llm.LLMUnavailable:
            pass
        except (ValueError, TypeError, AttributeError) as e:
            log.info("employee %s: the model returned a bad pulse mark (%s)", employee["id"], type(e).__name__)
    if mark is None:
        mark = _pulse_by_words(text)
    # this runs in the background: while the model was busy the newcomer may have deleted their data
    current = db.query_one("SELECT linked_at FROM employees WHERE id = ?", (employee["id"],))
    if current is None or not current["linked_at"] or current["linked_at"] != employee["linked_at"]:
        log.info("employee %s: data deleted meanwhile, pulse mark dropped", employee["id"])
        return
    tone, topic, risk, reason = mark
    db.execute(
        "INSERT INTO pulse (employee_id, text, tone, topic, risk, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (employee["id"], text, tone, topic, risk, reason, db.now_iso()),
    )
    if risk == 2:
        log.info("employee %s: red flag", employee["id"])
        await notify_recruiter(
            employee,
            f"Красный флаг: {_who(employee)}\nТема: {topic}\nПричина: {reason}\n\n«{_cut(text, 1000)}»",
        )


def _parse_pulse(raw: str) -> tuple[str, str, int, str]:
    data = json.loads(raw)
    tone = data.get("tone") if data.get("tone") in TONES else "нейтральный"
    topic = data.get("topic") if data.get("topic") in TOPICS else "другое"
    risk = min(max(int(data.get("risk", 0)), 0), 2)
    return tone, topic, risk, str(data.get("reason") or "")[:300]


def _pulse_by_words(text: str) -> tuple[str, str, int, str]:
    lowered = text.lower().replace("ё", "е")
    red = [word for word in RED_FLAGS if word in lowered]
    worries = [word for word in WORRIES if word in lowered]
    topic = next((name for name, words in TOPIC_MARKERS.items() if any(w in lowered for w in words)), "другое")
    if red:
        return "негативный", topic, 2, "слова-маркеры: " + ", ".join(red)
    if worries:
        return "негативный", topic, 1, "слова-маркеры: " + ", ".join(worries)
    return "нейтральный", topic, 0, ""


def digest_text(recruiter_id: int) -> str:
    employees = db.query("SELECT * FROM employees WHERE recruiter_id = ? ORDER BY start_date, id", (recruiter_id,))
    if not employees:
        return "За вами пока не закреплён ни один новичок."
    lines = [f"Сводка по вашим новичкам: {len(employees)}", ""]
    questions, flags, topics = [], [], Counter()
    for employee in employees:
        opened = db.query("SELECT question, created_at FROM escalations WHERE employee_id = ? AND status = 'open'"
                          " ORDER BY id", (employee["id"],))
        pulse = db.query("SELECT text, topic, risk, reason, created_at FROM pulse WHERE employee_id = ? ORDER BY id",
                         (employee["id"],))
        red = [row for row in pulse if row["risk"] == 2]
        topics.update(row["topic"] for row in pulse if row["topic"] != "другое")
        lines.append(f"{_who(employee)}: {_stage(employee)}; открытых вопросов: {len(opened)}, "
                     f"красных флагов: {len(red)}")
        questions += [f"- {employee['full_name']}, {_day(q['created_at'])}: «{_cut(q['question'], 200)}»"
                      for q in opened]
        flags += [f"- {employee['full_name']}, {_day(f['created_at'])}: «{_cut(f['text'], 200)}» ({f['topic']})"
                  for f in red]
    if questions:
        lines += ["", "Открытые вопросы:"] + questions
    if flags:
        lines += ["", "Красные флаги:"] + flags[-10:]
    if topics:
        lines += ["", "Частые темы: " + ", ".join(f"{topic} ({count})" for topic, count in topics.most_common(5))]
    return "\n".join(lines)


def _stage(employee) -> str:
    if not employee["max_user_id"]:
        return "ссылка ещё не открыта"
    today = engine.employee_now(employee).astimezone(ZoneInfo(employee["timezone"])).date()
    start = date.fromisoformat(employee["start_date"])
    if today < start:
        return f"выход {start:%d.%m}, через {(start - today).days} дн."
    months = (today.year - start.year) * 12 + today.month - start.month - (today.day < start.day)
    if months >= cfg.PROBATION_MONTHS:
        return "испытательный срок позади"
    return f"{(today - start).days + 1}-й день работы, {months + 1}-й месяц"


def _who(employee) -> str:
    return f"{employee['full_name']} ({employee['tags']})" if employee["tags"] else employee["full_name"]


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _day(stamp: str) -> str:
    return datetime.fromisoformat(stamp).strftime("%d.%m")


def _text(update: dict) -> str:
    return (((update.get("message") or {}).get("body") or {}).get("text") or "").strip()
