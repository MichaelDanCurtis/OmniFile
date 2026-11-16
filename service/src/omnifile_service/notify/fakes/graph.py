"""A fake Microsoft Graph (and Entra token endpoint), as a Starlette app.

Seed it from JSON (`FakeGraph.from_seed`):

    {"tenant": "<guid>",
     "clients": {"<client id>": "<secret>"},
     "users": [{"id": "<oid>", "sid": "S-1-5-21-...", "displayName": "...",
                "userPrincipalName": "...", "mail": "...", "groups": ["<group oid>"]}],
     "groups": [{"id": "<oid>", "sid": "S-1-5-21-...", "displayName": "...",
                 "samAccountName": "...", "groups": ["<parent group oid>"]}],
     "blocked": ["<oid>"]}

Users in `blocked` get 403 on the Teams app install (tenant policy, risk R5). `mail` records
every sendMail. `page_size` forces `@odata.nextLink` paging.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_SID_FILTER = re.compile(r"^onPremisesSecurityIdentifier eq '([^']+)'$")
_APP_FILTER = re.compile(r"^teamsApp/id eq '([^']+)'$")


async def _form(request: Request) -> dict[str, str]:
    """An x-www-form-urlencoded body (no python-multipart dependency)."""
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


@dataclass
class FakeGraph:
    tenant: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    clients: dict[str, str] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    installed: dict[str, str] = field(default_factory=dict)  # oid -> installation id
    chats: dict[str, str] = field(default_factory=dict)  # installation id -> chat id
    mail: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    page_size: int = 100
    #: Answer the next N calls on these path prefixes with this status.
    fail: dict[str, tuple[int, int]] = field(default_factory=dict)
    tokens: set[str] = field(default_factory=set)

    @classmethod
    def from_seed(cls, seed: dict[str, Any]) -> FakeGraph:
        g = cls(tenant=seed.get("tenant", cls.tenant), clients=dict(seed.get("clients", {})))
        for u in seed.get("users", []):
            g.add_user(**u)
        for grp in seed.get("groups", []):
            g.add_group(**grp)
        g.blocked = {b.lower() for b in seed.get("blocked", [])}
        return g

    def add_user(
        self,
        id: str,  # noqa: A002 - Graph's name
        sid: str | None = None,
        displayName: str | None = None,  # noqa: N803
        userPrincipalName: str | None = None,  # noqa: N803
        mail: str | None = None,
        groups: list[str] | None = None,
    ) -> None:
        self.users[id.lower()] = {
            "id": id.lower(),
            "onPremisesSecurityIdentifier": sid,
            "displayName": displayName,
            "userPrincipalName": userPrincipalName,
            "mail": mail,
            "groups": [x.lower() for x in groups or []],
        }

    def add_group(
        self,
        id: str,  # noqa: A002
        sid: str | None = None,
        displayName: str | None = None,  # noqa: N803
        samAccountName: str | None = None,  # noqa: N803
        groups: list[str] | None = None,
        synced: bool = True,
    ) -> None:
        self.groups[id.lower()] = {
            "id": id.lower(),
            "onPremisesSecurityIdentifier": sid,
            "displayName": displayName,
            "onPremisesSamAccountName": samAccountName,
            "onPremisesSyncEnabled": synced,
            "groups": [x.lower() for x in groups or []],
        }

    # --- helpers -------------------------------------------------------------

    def _ancestors(self, direct: list[str]) -> list[str]:
        seen: list[str] = []
        todo = list(direct)
        while todo:
            g = todo.pop()
            if g in seen or g not in self.groups:
                continue
            seen.append(g)
            todo += self.groups[g]["groups"]
        return seen

    def _members(self, group_id: str) -> list[dict[str, Any]]:
        return [u for u in self.users.values() if group_id in self._ancestors(u["groups"])]

    def _page(self, request: Request, items: list[dict[str, Any]]) -> JSONResponse:
        skip = int(request.query_params.get("$skiptoken", "0") or 0)
        page = items[skip : skip + self.page_size]
        body: dict[str, Any] = {"value": page}
        if skip + self.page_size < len(items):
            params = dict(request.query_params)
            params["$skiptoken"] = str(skip + self.page_size)
            body["@odata.nextLink"] = str(request.url.replace(query=None)) + "?" + urlencode(params)
        return JSONResponse(body)

    @staticmethod
    def _user_view(u: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in u.items() if k != "groups"}

    @staticmethod
    def _group_view(g: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in g.items() if k != "groups"}

    # --- the app -------------------------------------------------------------

    def app(self) -> Starlette:
        async def token(request: Request) -> Response:
            form = await _form(request)
            cid, secret = form.get("client_id"), form.get("client_secret")
            if request.path_params["tenant"] != self.tenant or self.clients.get(cid) != secret:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            value = "graph-" + secrets.token_urlsafe(16)
            self.tokens.add(value)
            return JSONResponse({"access_token": value, "expires_in": 3600, "token_type": "Bearer"})

        def guard(request: Request) -> Response | None:
            self.calls.append((request.method, request.url.path))
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
                return JSONResponse({"error": {"code": "InvalidAuthenticationToken"}}, 401)
            for prefix, (status, count) in list(self.fail.items()):
                if request.url.path.startswith(prefix) and count > 0:
                    self.fail[prefix] = (status, count - 1)
                    headers = {"Retry-After": "1"} if status == 429 else None
                    return JSONResponse({"error": {"code": "Injected"}}, status, headers=headers)
            return None

        async def users(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            m = _SID_FILTER.match(request.query_params.get("$filter", ""))
            if m is None:
                return JSONResponse({"error": {"code": "BadRequest"}}, 400)
            found = [
                self._user_view(u)
                for u in self.users.values()
                if u["onPremisesSecurityIdentifier"] == m[1]
            ]
            return self._page(request, found)

        async def member_of(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            u = self.users.get(request.path_params["oid"])
            if u is None:
                return JSONResponse({"error": {"code": "Request_ResourceNotFound"}}, 404)
            found = [self._group_view(self.groups[g]) for g in self._ancestors(u["groups"])]
            return self._page(request, found)

        async def members(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            gid = request.path_params["oid"]
            if gid not in self.groups:
                return JSONResponse({"error": {"code": "Request_ResourceNotFound"}}, 404)
            return self._page(request, [self._user_view(u) for u in self._members(gid)])

        async def groups(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            found = [
                self._group_view(g) for g in self.groups.values() if g["onPremisesSyncEnabled"]
            ]
            return self._page(request, found)

        async def installed_apps(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            oid = request.path_params["oid"]
            if oid not in self.users:
                return JSONResponse({"error": {"code": "Request_ResourceNotFound"}}, 404)
            if request.method == "POST":
                if oid in self.blocked:
                    return JSONResponse({"error": {"code": "Forbidden"}}, 403)
                if oid in self.installed:
                    return JSONResponse({"error": {"code": "Conflict"}}, 409)
                body = await request.json()
                if "teamsApp@odata.bind" not in body:
                    return JSONResponse({"error": {"code": "BadRequest"}}, 400)
                inst = "inst-" + uuid.uuid4().hex[:12]
                self.installed[oid] = inst
                self.chats[inst] = f"19:{uuid.uuid4().hex}@unq.gbl.spaces"
                return Response(status_code=201)
            m = _APP_FILTER.match(request.query_params.get("$filter", ""))
            inst = self.installed.get(oid)
            return self._page(request, [{"id": inst}] if m and inst else [])

        async def chat(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            chat_id = self.chats.get(request.path_params["inst"])
            if chat_id is None:
                return JSONResponse({"error": {"code": "NotFound"}}, 404)
            return JSONResponse({"id": chat_id, "chatType": "oneOnOne"})

        async def send_mail(request: Request) -> Response:
            if (bad := guard(request)) is not None:
                return bad
            body = await request.json()
            msg = body["message"]
            self.mail.append(
                {
                    "sender": request.path_params["sender"],
                    "to": [r["emailAddress"]["address"] for r in msg["toRecipients"]],
                    "subject": msg["subject"],
                    "html": msg["body"]["content"],
                }
            )
            return Response(status_code=202)

        v = "/v1.0"
        return Starlette(
            routes=[
                Route("/{tenant}/oauth2/v2.0/token", token, methods=["POST"]),
                Route(f"{v}/users", users, methods=["GET"]),
                Route(
                    f"{v}/users/{{oid}}/transitiveMemberOf/microsoft.graph.group",
                    member_of,
                    methods=["GET"],
                ),
                Route(
                    f"{v}/groups/{{oid}}/transitiveMembers/microsoft.graph.user",
                    members,
                    methods=["GET"],
                ),
                Route(f"{v}/groups", groups, methods=["GET"]),
                Route(
                    f"{v}/users/{{oid}}/teamwork/installedApps",
                    installed_apps,
                    methods=["GET", "POST"],
                ),
                Route(
                    f"{v}/users/{{oid}}/teamwork/installedApps/{{inst}}/chat",
                    chat,
                    methods=["GET"],
                ),
                Route(f"{v}/users/{{sender}}/sendMail", send_mail, methods=["POST"]),
            ]
        )
