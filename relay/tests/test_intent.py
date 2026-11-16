"""Signed intents: recipient, expiry, replay, tampering, forwarding failures."""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from omnifile_relay.intent import DATA_KEY
from omnifile_relay.sink import SinkDuplicate, SinkRejected, SinkReply

from .conftest import OTHER_TENANT, OTHER_USER, USER, b64url, make_activity


def _error_code(resp) -> str | None:
    body = resp.json()
    if body.get("type") == "application/vnd.microsoft.error":
        return body["value"]["code"]
    return None


def test_wrong_clicker_is_rejected(post, bot_token, intent, sink):
    resp = post(make_activity(intent(sub=USER), clicker=OTHER_USER), bot_token())
    assert resp.status_code == 200
    assert resp.json()["statusCode"] == 403
    assert _error_code(resp) == "wrong_recipient"
    assert sink.forwards == []


def test_clicker_guid_case_is_normalised(post, bot_token, intent, sink):
    resp = post(make_activity(intent(sub=USER.upper()), clicker=USER), bot_token())
    assert resp.json()["statusCode"] == 200
    assert len(sink.forwards) == 1


def test_missing_clicker_is_rejected(post, bot_token, intent, sink):
    activity = make_activity(intent())
    del activity["from"]["aadObjectId"]
    resp = post(activity, bot_token())
    assert resp.json()["statusCode"] == 400
    assert sink.forwards == []


def test_expired_intent_is_rejected(post, bot_token, intent, sink):
    now = int(time.time())
    resp = post(make_activity(intent(iat=now - 8 * 86400, exp=now - 3600)), bot_token())
    assert resp.json()["statusCode"] == 400
    assert _error_code(resp) == "intent_expired"
    assert sink.forwards == []


def test_replayed_intent_is_not_forwarded_twice(post, bot_token, intent, sink):
    token = intent()
    first = post(make_activity(token), bot_token())
    second = post(make_activity(token), bot_token())
    assert first.json()["type"] == "application/vnd.microsoft.card.adaptive"
    assert second.json()["type"] == "application/vnd.microsoft.activity.message"
    assert len(sink.forwards) == 1


def test_tampered_intent_payload_is_rejected(post, bot_token, intent, sink):
    token = intent(sub=USER)
    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["act"] = "keep_online"
    claims["fid"] = "someone-elses-file"
    forged = ".".join([header, b64url(json.dumps(claims).encode()), sig])
    resp = post(make_activity(forged, verb="keep_online"), bot_token())
    assert resp.json()["statusCode"] == 400
    assert sink.forwards == []


def test_intent_signed_by_other_key_is_rejected(post, bot_token, intent, sink):
    rogue = ed25519.Ed25519PrivateKey.generate()
    resp = post(make_activity(intent(key=rogue)), bot_token())
    assert resp.json()["statusCode"] == 400
    assert sink.forwards == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"kid": "unknown-kid"},
        {"typ": "JWT"},
        {"aud": "something-else"},
        {"iss": "attacker"},
        {"act": "delete"},
        {"tid": None},
        {"sub": None},
        {"jti": None},
        {"fid": "../../etc/passwd"},
        {"sub": "not-a-guid"},
        {"iat": int(time.time()), "exp": int(time.time()) + 90 * 86400},
        {"iat": int(time.time()) + 3600},
    ],
)
def test_invalid_intents_are_rejected(post, bot_token, intent, sink, overrides):
    resp = post(
        make_activity(intent(**overrides), verb=overrides.get("act", "recall")), bot_token()
    )
    assert resp.json()["statusCode"] == 400
    assert sink.forwards == []


def test_intent_for_other_tenant_is_rejected(post, bot_token, intent, sink):
    resp = post(make_activity(intent(tid=OTHER_TENANT)), bot_token())
    assert resp.json()["statusCode"] == 400
    assert sink.forwards == []


def test_verb_must_match_signed_action(post, bot_token, intent, sink):
    resp = post(make_activity(intent(act="track"), verb="recall"), bot_token())
    assert resp.json()["statusCode"] == 400
    assert sink.forwards == []


def test_missing_or_non_string_intent(post, bot_token, sink):
    for token in (None, 42, {"x": 1}, "", "x" * 5000):
        activity = make_activity(None)
        activity["value"]["action"]["data"] = {DATA_KEY: token}
        assert post(activity, bot_token()).json()["statusCode"] == 400
    assert sink.forwards == []


def test_automatic_refresh_trigger_does_nothing(post, bot_token, intent, sink):
    activity = make_activity(intent())
    activity["value"]["trigger"] = "automatic"
    assert post(activity, bot_token()).json()["statusCode"] == 400
    assert sink.forwards == []


@pytest.mark.parametrize("act", ["recall", "keep_online", "track"])
def test_each_action_is_forwarded(post, bot_token, intent, sink, act):
    resp = post(make_activity(intent(act=act), verb=act), bot_token())
    assert resp.json()["statusCode"] == 200
    assert [f.action for f in sink.forwards] == [act]


def test_forward_carries_raw_intent_and_no_activity_data(post, bot_token, intent, sink):
    token = intent()
    post(make_activity(token), bot_token())
    body = sink.forwards[0].to_json()
    assert body["intent"] == token
    assert set(body) == {
        "intent",
        "intent_id",
        "action",
        "clicker_aad_object_id",
        "tenant_id",
        "activity_time",
        "received_at",
    }
    assert body["activity_time"] == "2026-10-05T18:07:27.463Z"
    assert body["received_at"].endswith("Z")
    assert "smba" not in json.dumps(body)


def test_service_card_is_returned(post, bot_token, intent, sink):
    card = {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": "x"}]}
    sink.reply = SinkReply(card=card)
    resp = post(make_activity(intent()), bot_token())
    assert resp.json()["value"] == card


def test_service_failure_releases_intent_for_retry(post, bot_token, intent, sink):
    token = intent()
    sink.fail_next = 1
    first = post(make_activity(token), bot_token())
    assert first.json()["statusCode"] == 500
    assert sink.forwards == []
    second = post(make_activity(token), bot_token())
    assert second.json()["statusCode"] == 200
    assert len(sink.forwards) == 1


def test_service_timeout_returns_within_budget(
    make_client, settings, post, bot_token, intent, sink
):
    import asyncio
    import dataclasses

    class Slow:
        async def submit(self, forward):
            await asyncio.sleep(5)

        async def aclose(self):
            pass

    fast = dataclasses.replace(settings, forward_timeout_seconds=0.2)
    c = make_client(fast)
    sink.submit = Slow().submit  # the app holds the shared FakeSink; make it slow
    started = time.monotonic()
    resp = post(make_activity(intent()), bot_token(), c)
    assert time.monotonic() - started < 2
    assert resp.json()["statusCode"] == 500


def test_service_duplicate_and_rejection(post, bot_token, intent, sink):
    sink.raise_exc = SinkDuplicate()
    assert post(make_activity(intent()), bot_token()).json()["statusCode"] == 200
    sink.raise_exc = SinkRejected("403")
    resp = post(make_activity(intent()), bot_token())
    assert resp.json()["statusCode"] == 403
    assert _error_code(resp) == "rejected"


@pytest.mark.parametrize("ts", [None, "", "yesterday", "2026-10-05T18:07:27", 12345])
def test_missing_or_bad_activity_timestamp_is_rejected(post, bot_token, intent, sink, ts):
    activity = make_activity(intent())
    if ts is None:
        del activity["timestamp"]
    else:
        activity["timestamp"] = ts
    assert post(activity, bot_token()).json()["statusCode"] == 400
    assert sink.forwards == []


def test_a_full_replay_store_says_so_and_scans_at_most_once_a_second():
    """M7 review: full, it answered as if the click were a repeat, and
    scanned every entry on every click."""
    import pytest

    from omnifile_relay.replay import MemoryReplayStore, ReplayStoreFull

    now = [1000.0]
    store = MemoryReplayStore(max_entries=2, clock=lambda: now[0])
    assert store.reserve("a", 5000.0) and store.reserve("b", 5000.0)
    assert not store.reserve("a", 5000.0)  # a repeat is still a repeat
    with pytest.raises(ReplayStoreFull):
        store.reserve("c", 5000.0)
    # Once entries expire, the next purge (a second later) makes room.
    now[0] = 6000.0
    assert store.reserve("c", 9000.0)
