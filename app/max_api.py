import asyncio
import time
from pathlib import Path

import httpx

from app.config import cfg


UPDATE_TYPES = "message_created,message_callback,bot_started"
SEND_GAP = 0.6  # seconds between two messages to the same chat

_last_send = {}


class MaxError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"MAX HTTP {status}: {text[:200]}")
        self.status = status
        self.text = text


async def _request(method: str, path: str, params=None, body=None, timeout: float = 15) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(
            method,
            cfg.MAX_API_BASE + path,
            params=params,
            json=body,
            headers={"Authorization": cfg.MAX_BOT_TOKEN},
        )
    if not r.is_success:
        raise MaxError(r.status_code, r.text)
    data = r.json() if r.content else {}
    if isinstance(data, dict) and data.get("success") is False:
        raise MaxError(r.status_code, str(data.get("message", "")))
    return data


async def get_me() -> dict:
    return await _request("GET", "/me")


async def get_updates(marker: int | None, timeout: int = 30) -> tuple[list[dict], int | None]:
    params = {"timeout": timeout, "types": UPDATE_TYPES}
    if marker is not None:
        params["marker"] = marker
    data = await _request("GET", "/updates", params=params, timeout=timeout + 5)
    return data.get("updates", []), data.get("marker")


async def _wait_turn(recipient: int) -> None:
    # MAX limits how fast a bot may write; the gap also keeps a series of messages in order
    now = time.monotonic()
    at = max(now, _last_send.get(recipient, 0) + SEND_GAP)
    _last_send[recipient] = at
    if at > now:
        await asyncio.sleep(at - now)


async def send_message(
    text: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    buttons: list[list[dict]] | None = None,
    image_token: str | None = None,
) -> dict:
    if user_id is not None:
        params = {"user_id": user_id}
    elif chat_id is not None:
        params = {"chat_id": chat_id}
    else:
        raise ValueError("send_message needs user_id or chat_id")

    attachments = []
    if image_token:
        attachments.append({"type": "image", "payload": {"token": image_token}})
    if buttons:
        attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
    body = {"text": text}
    if attachments:
        body["attachments"] = attachments

    await _wait_turn(user_id if user_id is not None else chat_id)
    for attempt in range(3):
        try:
            return await _request("POST", "/messages", params=params, body=body)
        except MaxError as e:
            # a freshly uploaded picture needs a moment on the MAX side before it can be attached
            if "attachment.not.ready" in e.text and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise


async def answer_callback(callback_id: str, notification: str | None = None) -> None:
    body = {"notification": notification} if notification else {}
    await _request("POST", "/answers", params={"callback_id": callback_id}, body=body)


async def upload_image(path: str) -> str:
    data = await _request("POST", "/uploads", params={"type": "image"})
    file = Path(path)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            data["url"],
            files={"data": (file.name, file.read_bytes())},
            headers={"Authorization": cfg.MAX_BOT_TOKEN},
        )
    if not r.is_success:
        raise MaxError(r.status_code, r.text)
    result = r.json()
    # images come back as {"photos": {"<id>": {"token": ...}}}
    for photo in result.get("photos", {}).values():
        if photo.get("token"):
            return photo["token"]
    token = result.get("token") or data.get("token")
    if not token:
        raise MaxError(r.status_code, "no token in upload response")
    return token


def callback_button(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}
