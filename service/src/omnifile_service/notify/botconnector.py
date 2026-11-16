"""Bot Connector REST: send a card and update it in place (teams.md sections 6.3 and 6.4).

The service URL comes from configuration (`OMNIFILE_TEAMS_SERVICE_URL`), stored with the
conversation when it is created. It is never taken from a click: the relay doesn't forward
`serviceUrl`, by design.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .tokens import TokenError, TokenSource

CARD = "application/vnd.microsoft.card.adaptive"


class BotError(Exception):
    """`code` is safe to log; `status` is the HTTP status, if there was one."""

    def __init__(self, code: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after

    @property
    def gone(self) -> bool:
        """The user removed the app, or the conversation is gone (teams.md 6.5)."""
        return self.status in (403, 404)

    @property
    def throttled(self) -> bool:
        return self.status == 429


def activity(card: dict[str, Any], summary: str, activity_id: str | None = None) -> dict:
    body: dict[str, Any] = {
        "type": "message",
        "summary": summary[:200],
        "attachments": [{"contentType": CARD, "content": card}],
    }
    if activity_id is not None:
        body["id"] = activity_id
    return body


class BotConnector:
    def __init__(
        self, http: httpx.AsyncClient, tokens: TokenSource, *, allow_http: bool = False
    ) -> None:
        self._http = http
        self._tokens = tokens
        #: Dev mode only: the fake Bot Connector may be plain http.
        self._allow_http = allow_http

    def _base(self, service_url: str) -> str:
        scheme = urlsplit(service_url).scheme
        if scheme != "https" and not (self._allow_http and scheme == "http"):
            raise BotError("service_url_not_https")
        return service_url if service_url.endswith("/") else service_url + "/"

    async def _call(self, method: str, url: str, body: dict) -> httpx.Response:
        try:
            token = await self._tokens.token()
        except TokenError as exc:
            raise BotError(str(exc)) from None
        try:
            resp = await self._http.request(
                method,
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise BotError(f"bot_{type(exc).__name__}") from None
        if not 200 <= resp.status_code < 300:
            raw = resp.headers.get("retry-after")
            try:
                after = float(raw) if raw is not None else None
            except ValueError:
                after = None
            raise BotError(f"bot_status_{resp.status_code}", resp.status_code, after)
        return resp

    async def send(
        self, service_url: str, conversation_id: str, card: dict[str, Any], summary: str
    ) -> str:
        url = f"{self._base(service_url)}v3/conversations/{quote(conversation_id, safe='')}"
        resp = await self._call("POST", url + "/activities", activity(card, summary))
        try:
            aid = resp.json().get("id")
        except ValueError:
            aid = None
        if not isinstance(aid, str) or not aid:
            raise BotError("bot_no_activity_id", resp.status_code)
        return aid

    async def update(
        self,
        service_url: str,
        conversation_id: str,
        activity_id: str,
        card: dict[str, Any],
        summary: str,
    ) -> None:
        url = (
            f"{self._base(service_url)}v3/conversations/{quote(conversation_id, safe='')}"
            f"/activities/{quote(activity_id, safe='')}"
        )
        await self._call("PUT", url, activity(card, summary, activity_id))
