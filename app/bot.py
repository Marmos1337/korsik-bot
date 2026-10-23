import asyncio
import logging

from app import db, max_api, messaging
from app.config import cfg

log = logging.getLogger(__name__)

RETRY_PAUSE = 5  # seconds after a failed GET /updates
SEEN_LIMIT = 1000

UNKNOWN_USER_TEXT = (
    "Я помогаю новым сотрудникам. Попроси, пожалуйста, у своего рекрутера личную ссылку на меня."
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
    saved = db.kv_get("max_marker")
    marker = int(saved) if saved else None
    log.info("polling MAX updates")
    while True:
        try:
            updates, new_marker = await max_api.get_updates(marker)
        except Exception as e:
            log.warning("getting updates failed (%s), retry in %s s", e, RETRY_PAUSE)
            await asyncio.sleep(RETRY_PAUSE)
            continue

        for update in updates:
            if already_seen(update):
                continue
            try:
                await handle_update(update)
            except Exception:
                log.exception("update %s failed", update.get("update_type"))

        if new_marker is not None and new_marker != marker:
            marker = new_marker
            db.kv_set("max_marker", str(marker))


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

    chat_id, chat_type = chat(update)
    in_dialog = chat_type in (None, "dialog")
    if not in_dialog and (cfg.HR_CHAT_ID is None or chat_id != cfg.HR_CHAT_ID):
        return

    recruiter = db.query_one("SELECT * FROM recruiters WHERE max_user_id = ?", (user_id,))
    employee = db.query_one("SELECT * FROM employees WHERE max_user_id = ?", (user_id,))
    if recruiter or employee:
        # TODO: hand over to hr.handle_recruiter_update / engine
        log.info("%s from linked user %s, no handler yet", kind, user_id)
        return

    if in_dialog and kind in ("message_created", "bot_started"):
        log.info("%s from unknown user %s", kind, user_id)
        await messaging.send_to_user(user_id, UNKNOWN_USER_TEXT)
