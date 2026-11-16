"""Intent signing (worker) and verification (api), against the relay's own verifier."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnifile_service.notify.intents import (
    IntentKeyError,
    IntentRefused,
    IntentSigner,
    IntentVerifier,
    load_private_key,
    load_public_key,
    load_public_keys,
    public_key_b64,
)

from .support import KID, TENANT, relay_modules

USER = "0f0f0f0f-1111-2222-3333-444444444444"
NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def mint(key, **kw):
    args = {"action": "recall", "file_id": 184, "recipient": USER, "tenant": TENANT, "now": NOW}
    args.update(kw)
    return IntentSigner(key, KID).mint(**args)


def test_format_is_the_relays(key) -> None:
    m = mint(key)
    header = jwt.get_unverified_header(m.token)
    assert header == {"alg": "EdDSA", "typ": "omnifile-intent+jwt", "kid": KID}
    claims = jwt.decode(m.token, options={"verify_signature": False})
    assert set(claims) == {"jti", "act", "fid", "sub", "tid", "iat", "exp", "iss", "aud"}
    assert claims["iss"] == "omnifile-service" and claims["aud"] == "omnifile-teams-relay"
    assert claims["fid"] == "184" and claims["sub"] == USER and claims["tid"] == TENANT
    assert str(uuid.UUID(claims["jti"])) == claims["jti"] == m.intent_id
    assert claims["exp"] - claims["iat"] == 7 * 86400
    assert len(m.token) < 700


def test_the_relay_accepts_what_the_worker_mints(key) -> None:
    _, config, _ = relay_modules()
    from omnifile_relay.intent import IntentVerifier as RelayVerifier

    settings = config.Settings(
        bot_app_id="11111111-2222-3333-4444-555555555555",
        tenant_id=TENANT,
        intent_public_keys={KID: config.load_public_key(public_key_b64(key))},
    )
    for act in ("recall", "keep_online", "track"):
        m = mint(key, action=act, now=datetime.now(UTC))
        got = RelayVerifier(settings).verify(m.token)
        assert (got.intent_id, got.action, got.file_id, got.recipient) == (
            m.intent_id,
            act,
            "184",
            USER,
        )


def test_round_trip_and_refusals(key) -> None:
    v = IntentVerifier({KID: key.public_key()})
    m = mint(key)
    got = v.verify(m.token, NOW)
    assert (got.intent_id, got.action, got.file_id, got.recipient, got.tenant) == (
        m.intent_id,
        "recall",
        184,
        USER,
        TENANT,
    )

    def code(token, now=NOW) -> str:
        with pytest.raises(IntentRefused) as e:
            v.verify(token, now)
        return e.value.code

    other = Ed25519PrivateKey.generate()
    assert (
        code(
            IntentSigner(other, KID)
            .mint(action="recall", file_id=1, recipient=USER, tenant=TENANT, now=NOW)
            .token
        )
        == "intent_invalid"
    )
    assert (
        code(
            IntentSigner(key, "other-kid")
            .mint(action="recall", file_id=1, recipient=USER, tenant=TENANT, now=NOW)
            .token
        )
        == "intent_invalid"
    )
    assert code(m.token, NOW + timedelta(days=8)) == "intent_expired"
    assert code(m.token, NOW - timedelta(minutes=5)) == "intent_invalid"  # issued in the future
    assert code("") == "intent_invalid"
    assert code("x" * 3000) == "intent_invalid"
    assert code("not.a.jwt") == "intent_invalid"
    # Tampered claims break the signature.
    h, p, s = m.token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=="))
    claims["fid"] = "185"
    p2 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert code(f"{h}.{p2}.{s}") == "intent_invalid"
    # Another typ or audience, correctly signed, is refused.
    now = int(NOW.timestamp())
    base = {
        "jti": str(uuid.uuid4()),
        "act": "recall",
        "fid": "1",
        "sub": USER,
        "tid": TENANT,
        "iat": now,
        "exp": now + 60,
        "iss": "omnifile-service",
        "aud": "omnifile-teams-relay",
    }
    wrong_typ = jwt.encode(base, key, algorithm="EdDSA", headers={"kid": KID, "typ": "JWT"})
    assert code(wrong_typ) == "intent_invalid"
    wrong_aud = jwt.encode(
        {**base, "aud": "someone-else"},
        key,
        algorithm="EdDSA",
        headers={"kid": KID, "typ": "omnifile-intent+jwt"},
    )
    assert code(wrong_aud) == "intent_invalid"
    too_long = jwt.encode(
        {**base, "exp": now + 15 * 86400},
        key,
        algorithm="EdDSA",
        headers={"kid": KID, "typ": "omnifile-intent+jwt"},
    )
    assert code(too_long) == "intent_invalid"
    not_uuid = jwt.encode(
        {**base, "jti": "abc"},
        key,
        algorithm="EdDSA",
        headers={"kid": KID, "typ": "omnifile-intent+jwt"},
    )
    assert code(not_uuid) == "intent_invalid"


def test_key_loading(tmp_path, key) -> None:
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    raw = public_key_b64(key)
    keys = load_public_keys(json.dumps({"a": pem.decode(), "b": raw}))
    assert set(keys) == {"a", "b"}
    assert load_public_key(raw).public_bytes_raw() == key.public_key().public_bytes_raw()
    path = tmp_path / "k.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    assert load_private_key(path).public_key().public_bytes_raw() == (
        key.public_key().public_bytes_raw()
    )
    with pytest.raises(IntentKeyError):
        load_public_keys("{}")
    with pytest.raises(IntentKeyError):
        load_public_keys("not json")
    with pytest.raises(ValueError):
        mint(key, recipient="Not-A-Guid")
