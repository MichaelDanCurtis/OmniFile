"""A fake Teams client: clicks a card's button the way Teams does (teams.md section 11).

`FakeTeams` holds a test RSA key and serves, through `transport()`, the Bot Framework OpenID
metadata and JWKS at Microsoft's own URLs (`login.botframework.com`), with the `msteams`
endorsement. Give that transport to the relay's `create_app(..., http_client=...)`; the
relay's code runs unchanged and nothing leaves the process.

`click(conversation_id, title, clicker=...)` reads the latest card in that conversation from a
`FakeBotConnector`, builds the `adaptiveCard/action` invoke, signs a Bot Framework JWT for it
and POSTs it to the relay's `/api/messages`.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from .botconnector import FakeBotConnector

METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
ISSUER = "https://api.botframework.com"
SERVICE_URL = "https://smba.trafficmanager.net/teams/"


class FakeTeams:
    def __init__(
        self,
        *,
        bot_app_id: str,
        tenant_id: str,
        bot: FakeBotConnector,
        post: Callable[[str, bytes, dict[str, str]], httpx.Response] | None = None,
        service_url: str = SERVICE_URL,
    ) -> None:
        self.bot_app_id = bot_app_id
        self.tenant_id = tenant_id.lower()
        self.bot = bot
        #: How to reach the relay: post(path, body, headers) -> response.
        self.post = post
        self.service_url = service_url
        self.kid = "fake-bf-" + uuid.uuid4().hex[:8]
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # --- Microsoft's documents, for the relay ----------------------------------

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "endorsements": ["msteams"]})
        return {"keys": [jwk]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == METADATA_URL:
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "jwks_uri": JWKS_URL,
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if url == JWKS_URL:
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # --- the click -----------------------------------------------------------------

    def bot_token(self, *, service_url: str | None = None, **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": self.bot_app_id,
            "nbf": now - 10,
            "exp": now + 3600,
            "serviceUrl": service_url or self.service_url,
        }
        payload.update(claims)
        return jwt.encode(payload, self._key, algorithm="RS256", headers={"kid": self.kid})

    def invoke(
        self, action: dict[str, Any], *, clicker: str, conversation_id: str = "a:conv"
    ) -> dict[str, Any]:
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {
            "type": "invoke",
            "name": "adaptiveCard/action",
            "channelId": "msteams",
            "serviceUrl": self.service_url,
            "id": "f:" + uuid.uuid4().hex[:12],
            "timestamp": stamp,
            "from": {"id": "29:" + uuid.uuid4().hex[:8], "aadObjectId": clicker},
            "recipient": {"id": f"28:{self.bot_app_id}"},
            "conversation": {
                "id": conversation_id,
                "conversationType": "personal",
                "tenantId": self.tenant_id,
            },
            "channelData": {"tenant": {"id": self.tenant_id}},
            "value": {
                "action": {
                    "type": "Action.Execute",
                    "id": uuid.uuid4().hex[:8],
                    "verb": action.get("verb"),
                    "data": action.get("data", {}),
                },
                "trigger": "manual",
            },
        }

    @staticmethod
    def button(card: dict[str, Any], title: str) -> dict[str, Any]:
        for a in card.get("actions", []):
            if a.get("type") == "Action.Execute" and a.get("title") == title:
                return a
        raise LookupError(f"no {title!r} button on the card")

    def send(self, activity: dict[str, Any], token: str | None = None) -> httpx.Response:
        if self.post is None:
            raise RuntimeError("FakeTeams needs `post` to reach a relay")
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token or self.bot_token()}",
        }
        return self.post("/api/messages", json.dumps(activity).encode(), headers)

    def click(
        self, conversation_id: str, title: str, *, clicker: str, card: dict | None = None
    ) -> httpx.Response:
        """Click `title` on the latest card in `conversation_id` (or on `card`)."""
        card = card if card is not None else self.bot.card(conversation_id)
        activity = self.invoke(
            self.button(card, title), clicker=clicker, conversation_id=conversation_id
        )
        return self.send(activity)
