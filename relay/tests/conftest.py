"""Test fixtures: generated keys, a fake Bot Framework OpenID/JWKS server, token builders.

Nothing here talks to Microsoft. The fake server is an ``httpx.MockTransport`` answering for a
``.test`` host, and every key is generated per test session.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from omnifile_relay.app import create_app
from omnifile_relay.config import Settings
from omnifile_relay.intent import DATA_KEY
from omnifile_relay.replay import MemoryReplayStore
from omnifile_relay.sink import Forward, SinkError, SinkReply

BOT_APP_ID = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
USER = "0f0f0f0f-1111-2222-3333-444444444444"
OTHER_USER = "abababab-1111-2222-3333-444444444444"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
FAKE_HOST = "login.botframework.test"
METADATA_URL = f"https://{FAKE_HOST}/v1/.well-known/openidconfiguration"
JWKS_URL = f"https://{FAKE_HOST}/v1/.well-known/keys"
INTENT_KID = "intent-2026-10"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture(scope="session")
def rsa_keys() -> dict[str, rsa.RSAPrivateKey]:
    return {
        name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for name in ("kid-a", "kid-b", "attacker")
    }


@pytest.fixture(scope="session")
def intent_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


@dataclass
class FakeMicrosoft:
    """A fake login.botframework.com: OpenID metadata + JWKS, with request counters."""

    rsa_keys: dict[str, rsa.RSAPrivateKey]
    published: list[str] = field(default_factory=lambda: ["kid-a"])
    endorsements: dict[str, list[str]] = field(default_factory=dict)
    jwks_uri: str = JWKS_URL
    algs: list[str] = field(default_factory=lambda: ["RS256"])
    down: bool = False
    metadata_hits: int = 0
    jwks_hits: int = 0

    def jwks(self) -> dict:
        keys = []
        for kid in self.published:
            jwk = json.loads(RSAAlgorithm.to_jwk(self.rsa_keys[kid].public_key()))
            jwk.update(
                {"kid": kid, "use": "sig", "endorsements": self.endorsements.get(kid, ["msteams"])}
            )
            keys.append(jwk)
        return {"keys": keys}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        url = str(request.url)
        if url == METADATA_URL:
            self.metadata_hits += 1
            return httpx.Response(
                200,
                json={
                    "issuer": "https://api.botframework.com",
                    "jwks_uri": self.jwks_uri,
                    "id_token_signing_alg_values_supported": self.algs,
                },
            )
        if url == JWKS_URL:
            self.jwks_hits += 1
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)


@pytest.fixture
def microsoft(rsa_keys) -> FakeMicrosoft:
    return FakeMicrosoft(rsa_keys=rsa_keys)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class FakeSink:
    def __init__(self) -> None:
        self.forwards: list[Forward] = []
        self.fail_next = 0
        self.reply = SinkReply()
        self.raise_exc: Exception | None = None
        self.closed = False

    async def submit(self, forward: Forward) -> SinkReply:
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.fail_next:
            self.fail_next -= 1
            raise SinkError("down")
        self.forwards.append(forward)
        return self.reply

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


@pytest.fixture
def settings(intent_key) -> Settings:
    return Settings(
        bot_app_id=BOT_APP_ID,
        tenant_id=TENANT,
        intent_public_keys={INTENT_KID: intent_key.public_key()},
        openid_metadata_url=METADATA_URL,
        jwks_hosts=(FAKE_HOST,),
    )


@pytest.fixture
def make_client(settings, microsoft, sink, clock):
    clients: list[TestClient] = []

    def build(settings_override: Settings | None = None) -> TestClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(microsoft.handler))
        app = create_app(
            settings_override or settings,
            sink=sink,
            http_client=http,
            replay=MemoryReplayStore(),
            monotonic=clock,
        )
        client = TestClient(app, client=("203.0.113.7", 50000))
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def bot_token(rsa_keys):
    def mint(
        *,
        kid: str = "kid-a",
        signer: str | None = None,
        alg: str = "RS256",
        **claims,
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": "https://api.botframework.com",
            "aud": BOT_APP_ID,
            "nbf": now - 10,
            "exp": now + 3600,
            "serviceUrl": SERVICE_URL,
        }
        payload.update(claims)
        payload = {k: v for k, v in payload.items() if v is not None}
        key = rsa_keys[signer or kid]
        return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})

    return mint


@pytest.fixture
def intent(intent_key):
    def mint(
        *,
        key: ed25519.Ed25519PrivateKey | None = None,
        kid: str = INTENT_KID,
        typ: str = "omnifile-intent+jwt",
        **claims,
    ) -> str:
        now = int(time.time())
        payload = {
            "jti": str(uuid.uuid4()),
            "act": "recall",
            "fid": "file-184",
            "sub": USER,
            "tid": TENANT,
            "iat": now,
            "exp": now + 7 * 86400,
            "iss": "omnifile-service",
            "aud": "omnifile-teams-relay",
        }
        payload.update(claims)
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(
            payload, key or intent_key, algorithm="EdDSA", headers={"kid": kid, "typ": typ}
        )

    return mint


def make_activity(
    token: str | None,
    *,
    clicker: str = USER,
    verb: str = "recall",
    service_url: str = SERVICE_URL,
    tenant: str = TENANT,
    **overrides,
) -> dict:
    activity = {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "channelId": "msteams",
        "serviceUrl": service_url,
        "id": "f:123",
        "timestamp": "2026-10-05T18:07:27.4630000Z",
        "from": {"id": "29:abc", "aadObjectId": clicker},
        "recipient": {"id": f"28:{BOT_APP_ID}"},
        "conversation": {"id": "a:conv", "conversationType": "personal", "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}},
        "value": {
            "action": {
                "type": "Action.Execute",
                "id": "recallButton",
                "verb": verb,
                "data": {DATA_KEY: token},
            },
            "trigger": "manual",
        },
    }
    activity.update(overrides)
    return activity


@pytest.fixture
def post(client):
    def send(activity: dict, token: str | None, c: TestClient | None = None) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        return (c or client).post("/api/messages", content=json.dumps(activity), headers=headers)

    return send
