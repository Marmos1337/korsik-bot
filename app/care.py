"""When a newcomer is not fine: a warm reply instead of a document, a chance to talk, the recruiter called in."""
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import db, engine, hours, hr, max_api, messaging

log = logging.getLogger(__name__)

CHECK_IN_EVERY = timedelta(days=7)
CHECK_IN_DAY = 4  # Friday, 0 is Monday: the week is behind, there is something to tell
CHECK_IN_FROM = time(15, 0)

# (на «ты», на «вы»)
PHRASES = {
    "support": (
        "Мне очень жаль, что тебе сейчас так тяжело. В первые месяцы такое бывает у многих, и с этим можно "
        "справиться вместе. Не переживай, всё наладится 💙\n\n"
        "Расскажи, что случилось? Как ты сейчас себя чувствуешь? Напиши как есть, я передам это твоему рекрутеру, "
        "он свяжется с тобой лично.",
        "Мне очень жаль, что вам сейчас так тяжело. В первые месяцы такое бывает у многих, и с этим можно "
        "справиться вместе. Не переживайте, всё наладится 💙\n\n"
        "Расскажите, что случилось? Как вы сейчас себя чувствуете? Напишите как есть, я передам это вашему "
        "рекрутеру, он свяжется с вами лично.",
    ),
    "listen": (
        "Я рядом и выслушаю. Расскажи, что не так? Можно в нескольких словах. Если захочешь, рекрутер "
        "напишет тебе лично.",
        "Я рядом и выслушаю. Расскажите, что не так? Можно в нескольких словах. Если захотите, рекрутер "
        "напишет вам лично.",
    ),
    "heard": (
        "Спасибо за откровенность. Я передал это рекрутеру, он скоро напишет тебе лично. Держись, мы рядом 💙",
        "Спасибо за откровенность. Я передал это рекрутеру, он скоро напишет вам лично. Мы рядом 💙",
    ),
    "check_in": (
        "Как ты? Неделя позади, и мне правда важно, всё ли у тебя хорошо.",
        "Как вы? Неделя позади, и мне правда важно, всё ли у вас хорошо.",
    ),
    "fine": ("Рад это слышать! Если что-то изменится, просто напиши.", "Рад это слышать! Если что-то изменится, "
                                                                     "просто напишите."),
}
MOODS = {"ok": "Всё хорошо 👍", "meh": "Так себе", "bad": "Мне тяжело"}
# words that say the newcomer is close to leaving or breaking; the documents have nothing to say to that
ALARM = ("уволиться", "уволюсь", "увольняюсь", "хочу уйти", "думаю уйти", "уйти из компании", "не справляюсь",
         "никто не помогает", "выгора", "нет сил", "больше не могу", "не могу больше", "сил нет", "ненавиж",
         "надоело", "хочу уволиться")


def phrase(employee, key: str) -> str:
    ty, vy = PHRASES[key]
    return ty if employee["address"] == "ty" else vy


def wants_to_talk(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(word in lowered for word in ("мне тяжело", "мне плохо", "хочу поговорить", "хочу выговориться",
                                            "мне грустно", "мне страшно"))


def is_alarm(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(word in lowered for word in ALARM)


async def alarm(employee, text: str) -> None:
    """Words about leaving: support, wait for the story, and a red flag to the recruiter at once."""
    db.execute("INSERT INTO pulse (employee_id, text, tone, topic, risk, reason, created_at) VALUES"
               " (?, ?, 'негативный', 'другое', 2, 'слова об уходе или выгорании', ?)",
               (employee["id"], text, db.now_iso()))
    await _listen(employee, "support")
    await hr.notify_team(employee, f"🔴 красный флаг. Сообщение новичка: «{_cut(text)}». Новичку сейчас тяжело, "
                                   "подключитесь и напишите лично.")


async def start_talk(employee, worried: bool) -> None:
    """The newcomer pressed «Мне тяжело» or «Так себе», or asked to talk in the app."""
    await _listen(employee, "support" if worried else "listen")
    if worried:
        db.execute("INSERT INTO pulse (employee_id, text, tone, topic, risk, reason, created_at) VALUES"
                   " (?, 'Мне тяжело', 'негативный', 'другое', 1, 'нажата кнопка «Мне тяжело» или написано, что тяжело', ?)",
                   (employee["id"], db.now_iso()))
        await hr.notify_team(employee, "новичку тяжело, это видно по ответу боту. Бот попросил рассказать, "
                                       "что случилось; лучше написать лично.")


async def heard(employee, text: str) -> None:
    """What the newcomer told after «расскажи, что случилось»: straight to the recruiter, not to the documents."""
    db.execute("INSERT INTO answers (employee_id, step_id, question, answer, created_at) VALUES"
               " (?, 'разговор', 'Что случилось?', ?, ?)", (employee["id"], text, db.now_iso()))
    await hr.notify_team(employee, f"что не так, со слов новичка: «{_cut(text, 1500)}». Напишите лично.")
    await messaging.send_to_employee(employee, phrase(employee, "heard"))


async def on_mood(employee, mood: str) -> None:
    if mood not in MOODS:
        return
    messaging.log_message(employee["id"], "employee", MOODS[mood])
    if mood == "ok":
        await messaging.send_to_employee(employee, phrase(employee, "fine"))
    else:
        await start_talk(employee, worried=mood == "bad")


def due_check_in(employee, now: datetime) -> bool:
    """Once a week, on Friday afternoon in the newcomer's time, during the probation."""
    if not employee["linked_at"] or employee["waiting"]:
        return False
    local = now.astimezone(ZoneInfo(employee["timezone"]))
    if local.date() < datetime.fromisoformat(employee["start_date"]).date():
        return False
    if local.weekday() != CHECK_IN_DAY or local.time() < CHECK_IN_FROM:
        return False
    schedule = hours.of_employee(employee)
    if schedule is not None and not schedule.is_open(now):
        return False
    last = db.kv_get(f"checkin:{employee['id']}")
    return last is None or now - datetime.fromisoformat(last) >= CHECK_IN_EVERY - timedelta(hours=12)


async def check_in(employee, now: datetime) -> None:
    db.kv_set(f"checkin:{employee['id']}", now.isoformat(timespec="seconds"))
    buttons = [[max_api.callback_button(text, f"mood:{key}") for key, text in MOODS.items()]]
    await messaging.send_to_employee(employee, phrase(employee, "check_in"), buttons=buttons)


async def _listen(employee, key: str) -> None:
    # the next text goes to the recruiter, not to the documents; engine keeps it for a day
    await messaging.send_to_employee(employee, phrase(employee, key))
    engine._set_waiting(employee["id"], "разговор", "talk", engine.employee_now(employee))


def _cut(text: str, limit: int = 500) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
