"""serviceUrl allowlist, request limits, logging hygiene, health, and the mTLS sink."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from omnifile_relay.botauth import service_url_allowed
from omnifile_relay.jsonlog import JsonFormatter
from omnifile_relay.sink import (
    INTENTS_PATH,
    Forward,
    HttpsMtlsSink,
    SinkDuplicate,
    SinkError,
    SinkRejected,
)

from .conftest import USER, make_activity

BAD_SERVICE_URLS = [
    "http://smba.trafficmanager.net/emea/",
    "https://smba.trafficmanager.net.evil.example/emea/",
    "https://evil.example/smba.trafficmanager.net/",
    "https://user:pw@smba.trafficmanager.net/emea/",
    "https://smba.trafficmanager.net@evil.example/",
    "https://smba.trafficmanager.net:8443/emea/",
    "https://169.254.169.254/metadata/",
    "https://localhost/",
    "https://smba.trafficmanager.net\\@evil.example/",
    "https://smba.trafficmanager.net /emea/",
    "file:///etc/passwd",
    "",
    None,
    42,
]


@pytest.mark.parametrize("url", BAD_SERVICE_URLS)
def test_service_url_outside_allowlist_is_rejected(post, bot_token, intent, sink, microsoft, url):
    # Even with a correctly signed token whose serviceUrl claim matches the activity.
    claim = url if isinstance(url, str) else None
    resp = post(make_activity(intent(), service_url=url), bot_token(serviceUrl=claim))
    assert resp.status_code == 403
    assert sink.forwards == []
    # Rejected before any token work: the relay did not even fetch keys.
    assert microsoft.jwks_hits == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://smba.trafficmanager.net/emea/",
        "https://smba.trafficmanager.net/amer/",
        "https://SMBA.trafficmanager.net/teams/",
        "https://smba.trafficmanager.net:443/emea/",
    ],
)
def test_service_url_allowlist_accepts_teams(url):
    assert service_url_allowed(url, ("smba.trafficmanager.net",))


def test_non_invoke_activity_is_acknowledged_not_forwarded(post, bot_token, sink):
    activity = make_activity(None, type="message", text="hello")
    activity.pop("value")
    resp = post(activity, bot_token())
    assert resp.status_code == 200
    assert sink.forwards == []


def test_other_invoke_is_not_implemented(post, bot_token, intent, sink):
    resp = post(make_activity(intent(), name="signin/verifyState"), bot_token())
    assert resp.status_code == 501
    assert sink.forwards == []


def test_body_too_large(client, bot_token):
    big = json.dumps({"pad": "x" * 70_000})
    resp = client.post(
        "/api/messages",
        content=big,
        headers={"content-type": "application/json", "authorization": f"Bearer {bot_token()}"},
    )
    assert resp.status_code == 413


def test_wrong_content_type(client, bot_token):
    resp = client.post(
        "/api/messages",
        content="{}",
        headers={"content-type": "text/plain", "authorization": f"Bearer {bot_token()}"},
    )
    assert resp.status_code == 415


def test_bad_json(client, bot_token):
    resp = client.post(
        "/api/messages",
        content="[1,2",
        headers={"content-type": "application/json", "authorization": f"Bearer {bot_token()}"},
    )
    assert resp.status_code == 400


def test_get_not_allowed(client):
    assert client.get("/api/messages").status_code == 405


def test_per_ip_rate_limit(post, sink):
    codes = [post({"serviceUrl": "x"}, None).status_code for _ in range(60)]
    assert 429 in codes
    assert codes[0] == 403


def test_per_user_rate_limit(post, bot_token, intent, sink):
    codes = [post(make_activity(intent()), bot_token()).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]
    assert len(sink.forwards) == 5


def test_one_users_messages_never_use_up_everyones_clicks(post, bot_token, intent, sink):
    """M7 review: every Teams request comes from a few Bot Connector
    addresses. Authenticated traffic is limited per user, not per address,
    and the per-user limit covers messages too."""
    import uuid

    from .conftest import make_activity as act

    chat = []
    for _ in range(60):  # 60 users each send the bot a message
        a = act(None, clicker=str(uuid.uuid4()))
        a["type"] = "message"
        chat.append(post(a, bot_token()).status_code)
    assert set(chat) == {200}
    assert post(make_activity(intent()), bot_token()).status_code == 200
    assert len(sink.forwards) == 1
    # One user flooding messages is stopped by its own bucket.
    noisy = str(uuid.uuid4())
    codes = []
    for _ in range(8):
        a = act(None, clicker=noisy)
        a["type"] = "message"
        codes.append(post(a, bot_token()).status_code)
    assert 429 in codes


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_logs_contain_no_personal_data(post, bot_token, intent, caplog):
    caplog.set_level(logging.INFO)
    token = intent()
    bearer = bot_token()
    post(make_activity(token), bearer)
    post(make_activity(intent(sub="abababab-1111-2222-3333-444444444444")), bearer)
    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records]
    text = "\n".join(lines)
    assert lines
    for secret in (USER, token, bearer, "smba.trafficmanager.net", "203.0.113.7", "file-184"):
        assert secret not in text
    assert any('"outcome":"accepted"' in line for line in lines)
    assert any('"reason":"wrong_recipient"' in line for line in lines)


def test_json_formatter_drops_unlisted_fields():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    record.user = USER  # not allowlisted
    record.reason = "ok"
    out = json.loads(JsonFormatter().format(record))
    assert "user" not in out
    assert out["reason"] == "ok"


# --- HttpsMtlsSink -------------------------------------------------------------------------


def _forward() -> Forward:
    return Forward(
        intent_token="tok",
        intent_id="id-1",
        action="recall",
        clicker_object_id=USER,
        tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        activity_time="2026-10-05T00:00:00.000Z",
        received_at="2026-10-05T00:00:00.100Z",
    )


async def _submit_with(handler) -> object:
    client = httpx.AsyncClient(
        base_url="https://api.internal.test", transport=httpx.MockTransport(handler)
    )
    sink = HttpsMtlsSink.with_client(client)
    try:
        return await sink.submit(_forward())
    finally:
        await sink.aclose()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_mtls_sink_posts_intent_with_idempotency_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers["idempotency-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"message": "Recall queued"})

    reply = _run(_submit_with(handler))
    assert seen["path"] == INTENTS_PATH
    assert seen["key"] == "id-1"
    assert seen["body"]["intent"] == "tok"
    assert reply.message == "Recall queued"


@pytest.mark.parametrize(
    "status, exc",
    [(409, SinkDuplicate), (403, SinkRejected), (500, SinkError), (302, SinkError)],
)
def test_mtls_sink_maps_status(status, exc):
    def handler(request):
        return httpx.Response(status, headers={"location": "https://evil.example/"})

    with pytest.raises(exc):
        _run(_submit_with(handler))


def test_mtls_sink_network_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(SinkError):
        _run(_submit_with(handler))


def test_mtls_sink_ignores_non_card_reply():
    def handler(request):
        return httpx.Response(200, json={"card": {"type": "Something"}, "message": 7})

    reply = _run(_submit_with(handler))
    assert reply.card is None and reply.message is None


def test_mtls_sink_requires_https():
    with pytest.raises(ValueError):
        HttpsMtlsSink(
            "http://api.internal", client_cert="c", client_key="k", ca_bundle="ca", timeout=1
        )


# --- Hostile bodies -------------------------------------------------------------------------


@pytest.mark.parametrize("nested", ["[" * 50_000, '{"a":' * 10_000])
def test_deeply_nested_json_is_400_not_500(client, bot_token, nested):
    with pytest.raises(RecursionError):
        json.loads(nested)  # the attack is real: it fits in 64 KiB and blows the stack
    resp = client.post(
        "/api/messages",
        content=nested,
        headers={"content-type": "application/json", "authorization": f"Bearer {bot_token()}"},
    )
    assert resp.status_code == 400


def test_slow_body_is_cut_off(settings, sink, microsoft):
    """A slow-loris sender that never finishes its body gets 408 at the deadline."""
    import asyncio
    import dataclasses
    import time

    from omnifile_relay.app import create_app

    app = create_app(
        dataclasses.replace(settings, body_read_timeout_seconds=0.2),
        sink=sink,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(microsoft.handler)),
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/messages",
        "raw_path": b"/api/messages",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.9", 1234),
        "server": ("relay", 443),
    }
    sent_first = False

    async def receive():
        nonlocal sent_first
        if not sent_first:
            sent_first = True
            return {"type": "http.request", "body": b'{"type":', "more_body": True}
        await asyncio.sleep(3600)  # never sends the rest

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    started = time.monotonic()
    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=5))
    assert time.monotonic() - started < 2
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 408
    assert sink.forwards == []
