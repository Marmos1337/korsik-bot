import logging
from pathlib import Path

import httpx

from app import db, max_api
from app.config import cfg

log = logging.getLogger(__name__)

MAX_TEXT = 4000


def log_message(employee_id: int, role: str, text: str) -> int:
    return db.execute(
        "INSERT INTO messages (employee_id, role, text, created_at) VALUES (?, ?, ?, ?)",
        (employee_id, role, text, db.now_iso()),
    )


def history(employee_id: int, limit: int = 6) -> list[dict]:
    rows = db.query(
        "SELECT role, text FROM messages WHERE employee_id = ? ORDER BY id DESC LIMIT ?",
        (employee_id, limit),
    )
    return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    parts = []
    current = ""
    for paragraph in text.strip().split("\n\n"):
        joined = f"{current}\n\n{paragraph}" if current else paragraph
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            parts.append(current)
        while len(paragraph) > limit:
            cut = max(paragraph.rfind("\n", 0, limit), paragraph.rfind(" ", 0, limit))
            if cut <= 0:
                cut = limit
            parts.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        current = paragraph
    if current:
        parts.append(current)
    return parts


async def image_token(name: str) -> str | None:
    # only a bare file name: the name comes from the scenario sheet and must not point outside data/images
    name = Path(name).name
    key = f"img:{name}"
    token = db.kv_get(key)
    if token:
        return token
    path = Path(cfg.DATA_DIR) / "images" / name
    if not path.is_file():
        log.warning("image %s not found, sending text only", name)
        return None
    try:
        token = await max_api.upload_image(str(path))
    except (max_api.MaxError, httpx.HTTPError) as e:
        log.warning("image %s upload failed: %s", name, e)
        return None
    db.kv_set(key, token)
    return token


async def send_to_employee(employee, text: str, buttons=None, image: str | None = None) -> None:
    if not employee["max_user_id"]:
        log.warning("employee %s has no MAX account linked yet", employee["id"])
        return
    token = await image_token(image) if image else None
    parts = split_text(text) or [""]
    for i, part in enumerate(parts):
        await max_api.send_message(
            part,
            user_id=employee["max_user_id"],
            buttons=buttons if i == len(parts) - 1 else None,
            image_token=token if i == 0 else None,
        )
        log_message(employee["id"], "bot", part)


async def send_to_user(max_user_id: int, text: str, buttons=None) -> None:
    parts = split_text(text) or [""]
    for i, part in enumerate(parts):
        await max_api.send_message(part, user_id=max_user_id, buttons=buttons if i == len(parts) - 1 else None)
