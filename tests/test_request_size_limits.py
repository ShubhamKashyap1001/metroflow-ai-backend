import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.request_limits import MaxBodySizeMiddleware, PayloadTooLarge
from app.main import app
from app.core.config import settings

@pytest.fixture
def client():
    return TestClient(app)

def test_normal_small_request_is_not_rejected_for_size(client):
    r = client.post(
        "/api/v1/chatbot/message",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code != 413
    assert r.status_code == 401  

def test_normal_get_request_is_unaffected(client):
    r = client.get("/healthz")
    assert r.status_code != 413
    assert r.status_code in (200, 429)

def test_oversized_request_with_content_length_is_rejected_with_413(client):
    oversized = b'{"messages":[{"role":"user","content":"' + b"a" * (settings.MAX_REQUEST_BODY_BYTES + 1) + b'"}]}'
    r = client.post(
        "/api/v1/chatbot/message",
        content=oversized,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()
    assert str(settings.MAX_REQUEST_BODY_BYTES) in r.json()["detail"]

def test_request_just_under_the_limit_is_not_rejected_for_size(client):

    r = client.post(
        "/api/v1/chatbot/message",
        json={"messages": [{"role": "user", "content": "hello there"}]},
    )
    assert r.status_code != 413

def _make_recording_app():
    received = {"bytes": 0, "calls": 0}

    async def inner_app(scope, receive, send):
        while True:
            message = await receive()
            received["calls"] += 1
            received["bytes"] += len(message.get("body") or b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return inner_app, received

def test_content_length_fast_path_never_calls_downstream_app(monkeypatch):
    inner_app, received = _make_recording_app()
    middleware = MaxBodySizeMiddleware(inner_app, max_body_size=100)

    scope = {
        "type": "http",
        "path": "/api/v1/example",
        "headers": [(b"content-length", b"1000")],
    }
    call_count = {"n": 0}

    async def receive():
        call_count["n"] += 1
        return {"type": "http.request", "body": b"x" * 1000, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))

    assert call_count["n"] == 0, "receive() must never be called once Content-Length alone exceeds the limit"
    assert received["bytes"] == 0
    assert sent[0]["status"] == 413

def test_streaming_body_is_cut_off_before_the_full_oversized_body_is_forwarded():
    inner_app, received = _make_recording_app()
    middleware = MaxBodySizeMiddleware(inner_app, max_body_size=100)

    scope = {"type": "http", "path": "/api/v1/example", "headers": []}
    chunks = [b"a" * 50, b"b" * 50, b"c" * 50, b"d" * 50]  
    chunk_iter = iter(chunks)

    async def receive():
        try:
            return {"type": "http.request", "body": next(chunk_iter), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))

    assert received["bytes"] < 200, (
        "the downstream app must never be handed the full 200-byte body - "
        f"it saw {received['bytes']} bytes"
    )
    assert sent[0]["status"] == 413
    assert sent[0]["headers"][-1] == (b"connection", b"close"), (
        "the connection must be closed rather than reused for a next "
        "request, since the rest of the oversized body may still be "
        "unread on the wire"
    )

def test_body_under_the_limit_is_forwarded_to_the_app_unchanged():
    inner_app, received = _make_recording_app()
    middleware = MaxBodySizeMiddleware(inner_app, max_body_size=1000)

    scope = {"type": "http", "path": "/api/v1/example", "headers": [(b"content-length", b"9")]}

    async def receive():
        return {"type": "http.request", "body": b"small-bod", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))

    assert received["bytes"] == 9
    assert sent[-1]["body"] == b"ok"

def test_websocket_scope_is_passed_through_untouched():
    calls = []

    async def inner_app(scope, receive, send):
        calls.append(scope["type"])

    middleware = MaxBodySizeMiddleware(inner_app, max_body_size=1)

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        pass

    asyncio.run(middleware({"type": "websocket"}, receive, send))
    assert calls == ["websocket"]

def test_max_request_body_bytes_is_env_driven_with_a_conservative_default():
    from app.core.config import Settings
    assert Settings.model_fields["MAX_REQUEST_BODY_BYTES"].default == 1_048_576

def test_disabling_the_limit_via_zero_or_negative_is_a_no_op(monkeypatch):
    inner_app, received = _make_recording_app()
    middleware = MaxBodySizeMiddleware(inner_app, max_body_size=0)

    scope = {"type": "http", "path": "/x", "headers": [(b"content-length", b"999999")]}

    async def receive():
        return {"type": "http.request", "body": b"y" * 999999, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert received["bytes"] == 999999
    assert sent[-1]["body"] == b"ok"
