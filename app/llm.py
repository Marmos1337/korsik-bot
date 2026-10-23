import asyncio
import logging
import re

import httpx

from app import db
from app.config import cfg

log = logging.getLogger(__name__)

CHECK_EVERY = 60
CHAT_TIMEOUT = 60
EMBED_TIMEOUT = 120
CHECK_TIMEOUT = 5

_up = False


class LLMUnavailable(Exception):
    pass


def is_up() -> bool:
    return _up


def _mark(up: bool) -> None:
    global _up
    if _up and not up:
        log.warning("ollama is unavailable, switching to keyword mode")
    _up = up


async def _post(path: str, body: dict, timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(cfg.OLLAMA_URL + path, json=body)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as e:
        _mark(False)
        raise LLMUnavailable(f"{path}: {type(e).__name__}") from e


async def chat(messages: list[dict], json_mode: bool = False) -> str:
    body = {
        "model": cfg.LLM_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    if json_mode:
        body["format"] = "json"
    data = await _post("/api/chat", body, CHAT_TIMEOUT)
    content = data.get("message", {}).get("content", "")
    # qwen3 sometimes leaves an empty think block even with think=false
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


async def embed(texts: list[str]) -> list[list[float]]:
    data = await _post("/api/embed", {"model": cfg.EMBED_MODEL, "input": texts}, EMBED_TIMEOUT)
    vectors = data.get("embeddings") or []
    if len(vectors) != len(texts):
        _mark(False)
        raise LLMUnavailable(f"/api/embed returned {len(vectors)} vectors for {len(texts)} texts")
    return vectors


def _full_name(model: str) -> str:
    return model if ":" in model else model + ":latest"


async def check() -> bool:
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            response = await client.get(cfg.OLLAMA_URL + "/api/tags")
            response.raise_for_status()
            names = {_full_name(m["name"]) for m in response.json().get("models", [])}
        up = {_full_name(cfg.LLM_MODEL), _full_name(cfg.EMBED_MODEL)} <= names
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        up = False
    _mark(up)
    return up


async def watch_loop(on_change, on_up=None) -> None:
    # the last announced mode is kept in kv, so a restart does not tell HR the same news again
    while True:
        try:
            up = await check()
            mode = "1" if up else "0"
            before = db.kv_get("llm_up")
            if before != mode:
                db.kv_set("llm_up", mode)
                if before is not None:
                    await on_change(up)
            if up and on_up:
                # an embedding call can time out while the model is up, so the gaps are retried on every check
                await on_up()
        except Exception:
            log.exception("llm watch step failed")
        await asyncio.sleep(CHECK_EVERY)
