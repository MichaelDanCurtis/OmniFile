"""A fake Bot Connector service (and its token endpoint), as a Starlette app.

Records every activity version per conversation, so a test can read the latest card and
click it (`FakeTeams.click`). `fail_next(status, count)` makes the next sends/updates answer
403 (app removed), 404 (conversation gone) or 429 (throttled).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

CARD = "application/vnd.microsoft.card.adaptive"


async def _form(request: Request) -> dict[str, str]:
    """An x-www-form-urlencoded body (no python-multipart dependency)."""
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


@dataclass
class Activity:
    activity_id: str
    versions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def card(self) -> dict[str, Any]:
        return self.versions[-1]["attachments"][0]["content"]


@dataclass
class FakeBotConnector:
    clients: dict[str, str] = field(default_factory=dict)
    conversations: dict[str, list[Activity]] = field(default_factory=dict)
    tokens: set[str] = field(default_factory=set)
    failures: list[tuple[int, float | None]] = field(default_factory=list)
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def fail_next(self, status: int, count: int = 1, retry_after: float | None = None) -> None:
        self.failures += [(status, retry_after)] * count

    def activities(self, conversation_id: str) -> list[Activity]:
        return self.conversations.get(conversation_id, [])

    def latest(self, conversation_id: str) -> Activity:
        acts = self.activities(conversation_id)
        if not acts:
            raise LookupError("no activity in that conversation")
        return max(acts, key=lambda a: a.versions[-1]["_seq"])

    def card(self, conversation_id: str) -> dict[str, Any]:
        return self.latest(conversation_id).card

    _seq: int = 0

    def app(self) -> Starlette:
        async def token(request: Request) -> Response:
            form = await _form(request)
            if self.clients.get(form.get("client_id")) != form.get("client_secret"):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            if form.get("scope") != "https://api.botframework.com/.default":
                return JSONResponse({"error": "invalid_scope"}, status_code=400)
            value = "bot-" + secrets.token_urlsafe(16)
            self.tokens.add(value)
            return JSONResponse({"access_token": value, "expires_in": 3600, "token_type": "Bearer"})

        def check(request: Request) -> Response | None:
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
                return Response(status_code=401)
            if self.failures:
                status, after = self.failures.pop(0)
                headers = {"Retry-After": str(after)} if after is not None else None
                return JSONResponse({"error": {"code": "Injected"}}, status, headers=headers)
            return None

        async def _valid(request: Request) -> dict[str, Any] | Response:
            body = await request.json()
            atts = body.get("attachments") or []
            if body.get("type") != "message" or not atts or atts[0].get("contentType") != CARD:
                return JSONResponse({"error": {"code": "BadArgument"}}, 400)
            self._seq += 1
            body["_seq"] = self._seq
            return body

        async def send(request: Request) -> Response:
            cid = request.path_params["cid"]
            if (bad := check(request)) is not None:
                self.calls.append(("POST", cid, bad.status_code))
                return bad
            body = await _valid(request)
            if isinstance(body, Response):
                return body
            act = Activity("1:" + uuid.uuid4().hex[:16], [body])
            self.conversations.setdefault(cid, []).append(act)
            self.calls.append(("POST", cid, 201))
            return JSONResponse({"id": act.activity_id}, status_code=201)

        async def update(request: Request) -> Response:
            cid, aid = request.path_params["cid"], request.path_params["aid"]
            if (bad := check(request)) is not None:
                self.calls.append(("PUT", cid, bad.status_code))
                return bad
            act = next((a for a in self.activities(cid) if a.activity_id == aid), None)
            if act is None:
                self.calls.append(("PUT", cid, 404))
                return JSONResponse({"error": {"code": "ActivityNotFound"}}, 404)
            body = await _valid(request)
            if isinstance(body, Response):
                return body
            if body.get("id") != aid:
                return JSONResponse({"error": {"code": "BadArgument"}}, 400)
            act.versions.append(body)
            self.calls.append(("PUT", cid, 200))
            return JSONResponse({"id": aid})

        return Starlette(
            routes=[
                Route("/token", token, methods=["POST"]),
                Route("/v3/conversations/{cid}/activities", send, methods=["POST"]),
                Route("/v3/conversations/{cid}/activities/{aid}", update, methods=["PUT"]),
            ]
        )
