import asyncio
import json

import httpx
import pytest

from app import max_api


@pytest.fixture
def fake_max(monkeypatch):
    """Routes every httpx call to a local handler. Returns (seen requests, responses by path)."""
    seen = []
    responses = {}

    def handler(request):
        seen.append(request)
        status, body = responses.get(request.url.path, (200, {}))
        return httpx.Response(status, json=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return seen, responses


def test_send_message_with_keyboard_and_image(fake_max):
    seen, _ = fake_max
    buttons = [
        [max_api.callback_button("На ты", "addr:ty"), max_api.callback_button("На вы", "addr:vy")],
        [max_api.link_button("Сайт", "https://example.com")],
    ]

    asyncio.run(max_api.send_message("Привет", user_id=42, buttons=buttons, image_token="tok1"))

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/messages"
    assert request.url.params["user_id"] == "42"
    assert request.headers["Authorization"] == "test-token"
    assert json.loads(request.content) == {
        "text": "Привет",
        "attachments": [
            {"type": "image", "payload": {"token": "tok1"}},
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [
                            {"type": "callback", "text": "На ты", "payload": "addr:ty"},
                            {"type": "callback", "text": "На вы", "payload": "addr:vy"},
                        ],
                        [{"type": "link", "text": "Сайт", "url": "https://example.com"}],
                    ]
                },
            },
        ],
    }


def test_send_plain_text_to_chat(fake_max):
    seen, _ = fake_max

    asyncio.run(max_api.send_message("Сводка", chat_id=-100))

    assert seen[0].url.params["chat_id"] == "-100"
    assert "user_id" not in seen[0].url.params
    assert json.loads(seen[0].content) == {"text": "Сводка"}


def test_error_status_raises_max_error(fake_max):
    _, responses = fake_max
    responses["/messages"] = (401, {"code": "verify.token", "message": "Invalid access_token"})

    with pytest.raises(max_api.MaxError) as err:
        asyncio.run(max_api.send_message("Привет", user_id=7))
    assert err.value.status == 401


def test_get_updates_sends_marker_and_types(fake_max):
    seen, responses = fake_max
    update = {"update_type": "message_created", "message": {"body": {"mid": "m1", "text": "привет"}}}
    responses["/updates"] = (200, {"updates": [update], "marker": 77})

    updates, marker = asyncio.run(max_api.get_updates(70, timeout=30))

    assert updates == [update]
    assert marker == 77
    params = seen[0].url.params
    assert params["marker"] == "70"
    assert params["timeout"] == "30"
    assert set(params["types"].split(",")) == {"message_created", "message_callback", "bot_started"}


def test_answer_callback(fake_max):
    seen, _ = fake_max

    asyncio.run(max_api.answer_callback("cb-1", "Записал"))

    assert seen[0].url.path == "/answers"
    assert seen[0].url.params["callback_id"] == "cb-1"
    assert json.loads(seen[0].content) == {"notification": "Записал"}


def test_upload_image_returns_photo_token(fake_max, tmp_path):
    seen, responses = fake_max
    picture = tmp_path / "office.png"
    picture.write_bytes(b"\x89PNG fake")
    responses["/uploads"] = (200, {"url": "https://upload.invalid/uploadImage?photoIds=1"})
    responses["/uploadImage"] = (200, {"photos": {"1": {"token": "photo-token"}}})

    token = asyncio.run(max_api.upload_image(str(picture)))

    assert token == "photo-token"
    assert seen[0].url.params["type"] == "image"
    assert seen[1].url.host == "upload.invalid"
    assert b'name="data"' in seen[1].content
    assert b"\x89PNG fake" in seen[1].content
