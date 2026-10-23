import asyncio
import json

import httpx
import pytest

from app import db, llm

TAGS = {"models": [{"name": "qwen3:8b"}, {"name": "bge-m3:latest"}]}


@pytest.fixture
def ollama(monkeypatch):
    """Fake Ollama. Set state["down"] to make every request fail like a switched off Mac."""
    state = {"down": False, "tags": TAGS, "reply": "Ответ", "requests": []}

    def handler(request):
        if state["down"]:
            raise httpx.ConnectError("connection refused", request=request)
        state["requests"].append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=state["tags"])
        if request.url.path == "/api/embed":
            texts = json.loads(request.content)["input"]
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2] for _ in texts]})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": state["reply"]}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return state


def test_chat_and_embed_raise_when_ollama_goes_down(ollama):
    assert asyncio.run(llm.check()) is True
    assert llm.is_up() is True

    ollama["down"] = True
    with pytest.raises(llm.LLMUnavailable):
        asyncio.run(llm.chat([{"role": "user", "content": "привет"}]))
    assert llm.is_up() is False

    asyncio.run(llm.check())
    ollama["down"] = True
    with pytest.raises(llm.LLMUnavailable):
        asyncio.run(llm.embed(["привет"]))
    assert llm.is_up() is False


def test_check_needs_both_models(ollama):
    ollama["tags"] = {"models": [{"name": "qwen3:8b"}]}
    assert asyncio.run(llm.check()) is False
    assert llm.is_up() is False

    ollama["tags"] = TAGS
    assert asyncio.run(llm.check()) is True


def test_chat_turns_thinking_off_and_strips_think_block(ollama):
    ollama["reply"] = "<think>\nхм\n</think>\n\n{\"tone\": \"ok\"}"

    reply = asyncio.run(llm.chat([{"role": "user", "content": "оцени"}], json_mode=True))

    assert reply == '{"tone": "ok"}'
    body = json.loads(ollama["requests"][-1].content)
    assert body["think"] is False
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert body["format"] == "json"


def test_embed_returns_one_vector_per_text(ollama):
    assert asyncio.run(llm.embed(["а", "б", "в"])) == [[0.1, 0.2]] * 3


def run_watch(monkeypatch, states):
    """Runs watch_loop over a fixed list of check() results and returns what on_change got."""
    results = iter(states)
    changes = []

    async def fake_check():
        try:
            return next(results)
        except StopIteration:
            raise asyncio.CancelledError

    async def on_change(up):
        changes.append(up)

    monkeypatch.setattr(llm, "check", fake_check)
    monkeypatch.setattr(llm, "CHECK_EVERY", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(llm.watch_loop(on_change))
    return changes


def test_watch_loop_reports_only_changes(monkeypatch):
    assert run_watch(monkeypatch, [True, True, False, False, True, True]) == [False, True]


def test_watch_loop_does_not_repeat_mode_after_restart(monkeypatch):
    db.kv_set("llm_up", "0")
    assert run_watch(monkeypatch, [False, False]) == []
    assert run_watch(monkeypatch, [True]) == [True]


def test_watch_loop_survives_failing_callback(monkeypatch):
    calls = []

    async def broken(up):
        calls.append(up)
        raise RuntimeError("hr is down too")

    results = iter([True, False, True])

    async def fake_check():
        try:
            return next(results)
        except StopIteration:
            raise asyncio.CancelledError

    monkeypatch.setattr(llm, "check", fake_check)
    monkeypatch.setattr(llm, "CHECK_EVERY", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(llm.watch_loop(broken))
    assert calls == [False, True]
