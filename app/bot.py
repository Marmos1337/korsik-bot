import asyncio
import logging

from app import db, engine, max_api, messaging
from app.config import cfg

log = logging.getLogger(__name__)

RETRY_PAUSE = 5  # seconds after a failed GET /updates
SEEN_LIMIT = 1000

# these people have not chosen «ты» or «вы» yet, so the texts are on «вы»
UNKNOWN_USER_TEXT = (
    "Я помогаю новым сотрудникам. Попросите, пожалуйста, у своего рекрутера личную ссылку на меня."
)
UNKNOWN_LINK_TEXT = "Не нашёл такую ссылку. Попросите, пожалуйста, у своего рекрутера новую."
USED_LINK_TEXT = (
    "По этой ссылке уже подключился другой аккаунт МАКС. "
    "Если ссылка ваша, напишите, пожалуйста, своему рекрутеру."
)

# keys of recently handled events; dict keeps insertion order, so the oldest is first
_seen: dict[str, None] = {}


def update_key(update: dict) -> str | None:
    callback = update.get("callback") or {}
    if callback.get("callback_id"):
        return "cb:" + callback["callback_id"]
    # a callback also carries the bot message with the button, so mid is checked only after callback_id
    mid = ((update.get("message") or {}).get("body") or {}).get("mid")
    return "mid:" + mid if mid else None


def already_seen(update: dict) -> bool:
    key = update_key(update)
    if key is None:
        return False
    if key in _seen:
        return True
    _seen[key] = None
    if len(_seen) > SEEN_LIMIT:
        del _seen[next(iter(_seen))]
    return False


async def polling_loop() -> None:
    log.info("polling MAX updates")
    marker, marker_loaded = None, False
    while True:
        try:
            if not marker_loaded:
                saved = db.kv_get("max_marker")
                marker, marker_loaded = (int(saved) if saved else None), True
            updates, new_marker = await max_api.get_updates(marker)
            for update in updates:
                if already_seen(update):
                    continue
                try:
                    await handle_update(update)
                except Exception:
                    log.exception("update %s failed", update.get("update_type"))
            if new_marker is not None and new_marker != marker:
                # memory first: if saving fails, the next request still moves on
                marker = new_marker
                db.kv_set("max_marker", str(marker))
        except Exception as e:
            log.warning("polling failed (%s), retry in %s s", e, RETRY_PAUSE)
            await asyncio.sleep(RETRY_PAUSE)


def sender(update: dict) -> dict:
    if "callback" in update:
        return update["callback"].get("user") or {}
    if "message" in update:
        return update["message"].get("sender") or {}
    return update.get("user") or {}


def chat(update: dict) -> tuple[int | None, str | None]:
    recipient = (update.get("message") or {}).get("recipient") or {}
    return recipient.get("chat_id", update.get("chat_id")), recipient.get("chat_type")


async def handle_update(update: dict) -> None:
    kind = update.get("update_type")
    user = sender(update)
    user_id = user.get("user_id")
    if not user_id or user.get("is_bot"):
        return
    _, chat_type = chat(update)
    if chat_type not in (None, "dialog"):
        return  # employees talk to the bot one to one; the HR chat comes with the recruiter ticket

    if kind == "bot_started" and update.get("payload"):
        await start_by_link(user, str(update["payload"]))
        return

    employee = db.query_one("SELECT * FROM employees WHERE max_user_id = ?", (user_id,))
    if employee is None:
        if kind in ("message_created", "bot_started"):
            log.info("%s from unknown user %s", kind, user_id)
            await messaging.send_to_user(user_id, UNKNOWN_USER_TEXT)
        return

    if kind == "message_callback":
        callback = update["callback"]
        await engine.on_callback(employee, callback.get("payload") or "", callback.get("callback_id") or "")
    elif kind == "message_created":
        text = ((update["message"].get("body") or {}).get("text") or "").strip()
        await handle_text(employee, text)


async def start_by_link(user: dict, code: str) -> None:
    result = await engine.link_employee(user, code)
    log.info("start link from user %s: %s", user["user_id"], result)
    if result == "unknown":
        await messaging.send_to_user(user["user_id"], UNKNOWN_LINK_TEXT)
    elif result == "used":
        await messaging.send_to_user(user["user_id"], USED_LINK_TEXT)


async def handle_text(employee, text: str) -> None:
    if not text:
        await engine.say(employee, "only_text")
        return
    messaging.log_message(employee["id"], "employee", text)
    command = text.split()[0].lower() if text.startswith("/") else ""
    if command in ("/start", "/help"):
        await send_help(employee)
    elif command == "/delete":
        await engine.ask_delete(employee)
    elif command == "/next":
        await next_step(employee)
    elif not await engine.on_text(employee, text):
        await engine.say(employee, "not_ready")


async def send_help(employee) -> None:
    text = engine.phrase(employee, "help")
    if cfg.DEMO_MODE:
        text += "\n" + engine.phrase(employee, "help_demo")
    await messaging.send_to_employee(employee, text)


async def next_step(employee) -> None:
    if not cfg.DEMO_MODE:
        await engine.say(employee, "next_off")
    elif await engine.demo_next(employee["id"]) is None:
        await engine.say(employee, "next_none")
    else:
        await engine.advance(employee["id"])  # right away, the presenter should not wait for the tick
