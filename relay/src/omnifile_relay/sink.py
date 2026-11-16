"""Forwarding accepted intents inward.

The relay has no data access. Its only write is one authenticated message to the private
service, behind :class:`IntentSink`. The production sink is :class:`HttpsMtlsSink`
(recommended option; see README "Forwarding inward"). Tests use a fake.

What is sent is deliberately small: the raw signed intent (so the service re-verifies the
signature itself and never trusts the relay's word for it), the clicker's objectId and tenant
as asserted by a verified Bot Framework token, the activity's timestamp and the receive time.
Nothing from the activity that an attacker could steer (``serviceUrl``, conversation ids, free
text) is forwarded.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

INTENTS_PATH = "/internal/v1/teams/intents"
_MAX_REPLY_BYTES = 28 * 1024  # Teams caps a card at about 28 KB


@dataclass(frozen=True)
class Forward:
    """Body of ``POST /internal/v1/teams/intents`` (schema ``IntentForward`` in
    ``service/openapi/control-api.yaml``)."""

    intent_token: str
    intent_id: str
    action: str
    clicker_object_id: str
    tenant_id: str
    activity_time: str  # the Bot Framework activity's `timestamp`, RFC 3339 UTC
    received_at: str  # when the relay received it, RFC 3339 UTC

    def to_json(self) -> dict:
        return {
            "intent": self.intent_token,
            "intent_id": self.intent_id,
            "action": self.action,
            "clicker_aad_object_id": self.clicker_object_id,
            "tenant_id": self.tenant_id,
            "activity_time": self.activity_time,
            "received_at": self.received_at,
        }


@dataclass(frozen=True)
class SinkReply:
    """What the service wants shown in place of the card (both optional)."""

    card: dict | None = None
    message: str | None = None


class SinkError(Exception):
    """Transient: the service could not be reached or failed. The click may be retried."""


class SinkDuplicate(Exception):
    """The service already has this intent (its idempotency check fired)."""


class SinkRejected(Exception):
    """The service refused the intent (for example, the user can no longer read the file)."""


class IntentSink(Protocol):
    async def submit(self, forward: Forward) -> SinkReply: ...

    async def aclose(self) -> None: ...


def parse_reply(body: object) -> SinkReply:
    if not isinstance(body, dict):
        return SinkReply()
    card = body.get("card")
    message = body.get("message")
    if not (isinstance(card, dict) and card.get("type") == "AdaptiveCard"):
        card = None
    if not isinstance(message, str) or len(message) > 1000:
        message = None
    return SinkReply(card=card, message=message)


class HttpsMtlsSink:
    """POST to the private ``api`` over the VNet, authenticated with a client certificate."""

    def __init__(
        self,
        base_url: str,
        *,
        client_cert: str,
        client_key: str,
        ca_bundle: str,
        timeout: float,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("service_base_url must be an https URL")
        ctx = ssl.create_default_context(cafile=ca_bundle)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            verify=ctx,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,  # never pick up proxy settings from the environment
        )

    @classmethod
    def with_client(cls, client: httpx.AsyncClient) -> HttpsMtlsSink:
        """For tests: wrap a pre-built client (e.g. with a mock transport)."""
        self = cls.__new__(cls)
        self._client = client
        return self

    async def submit(self, forward: Forward) -> SinkReply:
        try:
            resp = await self._client.post(
                INTENTS_PATH,
                json=forward.to_json(),
                headers={"Idempotency-Key": forward.intent_id},
            )
        except httpx.HTTPError as exc:
            raise SinkError(type(exc).__name__) from None
        if resp.status_code == 409:
            raise SinkDuplicate()
        if resp.status_code in (400, 403, 404, 410, 422):
            raise SinkRejected(str(resp.status_code))
        if not 200 <= resp.status_code < 300:
            raise SinkError(f"status {resp.status_code}")
        if not resp.content or len(resp.content) > _MAX_REPLY_BYTES:
            return SinkReply()
        try:
            return parse_reply(resp.json())
        except ValueError:
            return SinkReply()

    async def aclose(self) -> None:
        await self._client.aclose()
