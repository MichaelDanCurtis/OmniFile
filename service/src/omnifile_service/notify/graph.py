"""Microsoft Graph for notifications (teams.md sections 6.2, 7 and 8).

Calls, all with the worker's application token:
- SID -> user: ``GET /users?$filter=onPremisesSecurityIdentifier eq '<SID>'``;
- groups: ``GET /users/<oid>/transitiveMemberOf/microsoft.graph.group``, all pages;
- group members (owner and watcher audiences): ``GET /groups/<oid>/transitiveMembers/...``;
- the synced group directory: ``GET /groups?$filter=onPremisesSyncEnabled eq true``;
- proactive install and the chat: ``/users/<oid>/teamwork/installedApps``;
- email: ``POST /users/<sender>/sendMail``.

`GraphError.code` is safe to log and to store (a status or an exception name, never a
response body). Every URL is built from configuration and catalog values that are validated
here (GUIDs, SIDs), never from free text.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .tokens import TokenError, TokenSource

GRAPH_URL = "https://graph.microsoft.com/v1.0"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
_DOMAIN = re.compile(r"^(S-1-5-21-[0-9]+-[0-9]+-[0-9]+)-[0-9]+$")
AUTHENTICATED_USERS = "S-1-5-11"
MAX_PAGES = 200
USER_SELECT = "id,displayName,userPrincipalName,mail,onPremisesSecurityIdentifier"


class GraphError(Exception):
    def __init__(self, code: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class GraphUser:
    object_id: str
    sid: str | None
    display_name: str | None
    upn: str | None
    mail: str | None


def guid(value: str) -> str:
    v = str(value).lower()
    if not _GUID.match(v):
        raise ValueError("not a GUID")
    return v


def domain_users_sid(user_sid: str) -> str | None:
    """`<domain SID>-513` for a domain account's SID (Entra doesn't sync primary groups)."""
    m = _DOMAIN.match(user_sid)
    return f"{m[1]}-513" if m else None


def group_sids_for(user_sid: str, graph_group_sids: list[str]) -> list[str]:
    """teams.md section 7: the groups' on-premises SIDs, Domain Users and Authenticated Users."""
    out = {s for s in graph_group_sids if isinstance(s, str) and _SID.match(s)}
    du = domain_users_sid(user_sid)
    if du:
        out.add(du)
    out.add(AUTHENTICATED_USERS)
    return sorted(out)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


class GraphClient:
    def __init__(
        self, http: httpx.AsyncClient, tokens: TokenSource, base_url: str = GRAPH_URL
    ) -> None:
        self._http = http
        self._tokens = tokens
        self.base_url = base_url.rstrip("/")

    async def _request(
        self, method: str, url: str, *, json: Any = None, ok: tuple[int, ...] = (200,)
    ) -> httpx.Response:
        if not url.startswith(self.base_url + "/"):
            # nextLink and friends must stay on Graph.
            raise GraphError("foreign_url")
        try:
            token = await self._tokens.token()
        except TokenError as exc:
            raise GraphError(str(exc)) from None
        try:
            resp = await self._http.request(
                method,
                url,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise GraphError(f"graph_{type(exc).__name__}") from None
        if resp.status_code not in ok:
            raise GraphError(
                f"graph_status_{resp.status_code}", resp.status_code, _retry_after(resp)
            )
        return resp

    async def _pages(self, url: str) -> AsyncIterator[dict[str, Any]]:
        for _ in range(MAX_PAGES):
            doc = (await self._request("GET", url)).json()
            for item in doc.get("value") or []:
                if isinstance(item, dict):
                    yield item
            nxt = doc.get("@odata.nextLink")
            if not isinstance(nxt, str):
                return
            if urlsplit(nxt).netloc != urlsplit(self.base_url).netloc:
                raise GraphError("foreign_next_link")
            url = nxt
        raise GraphError("too_many_pages")

    # --- users and groups ----------------------------------------------------

    async def user_by_sid(self, sid: str) -> GraphUser | None:
        if not _SID.match(sid):
            raise ValueError("not a SID")
        url = (
            f"{self.base_url}/users?$filter="
            + quote(f"onPremisesSecurityIdentifier eq '{sid}'", safe="")
            + f"&$select={USER_SELECT}"
        )
        users = [u async for u in self._pages(url)]
        if len(users) != 1:
            if len(users) > 1:
                raise GraphError("ambiguous_sid")
            return None
        u = users[0]
        return GraphUser(
            object_id=guid(u["id"]),
            sid=u.get("onPremisesSecurityIdentifier"),
            display_name=u.get("displayName"),
            upn=u.get("userPrincipalName"),
            mail=u.get("mail"),
        )

    async def transitive_group_sids(self, object_id: str) -> list[str]:
        url = (
            f"{self.base_url}/users/{guid(object_id)}/transitiveMemberOf/microsoft.graph.group"
            "?$select=id,onPremisesSecurityIdentifier&$top=999"
        )
        return [
            g["onPremisesSecurityIdentifier"]
            async for g in self._pages(url)
            if isinstance(g.get("onPremisesSecurityIdentifier"), str)
        ]

    async def group_members(self, group_object_id: str, limit: int) -> list[GraphUser]:
        """Users in a group, transitively; at most `limit` + 1 (so the caller sees overflow)."""
        url = (
            f"{self.base_url}/groups/{guid(group_object_id)}/transitiveMembers/microsoft.graph.user"
            f"?$select={USER_SELECT}&$top=999"
        )
        out: list[GraphUser] = []
        async for u in self._pages(url):
            out.append(
                GraphUser(
                    guid(u["id"]),
                    u.get("onPremisesSecurityIdentifier"),
                    u.get("displayName"),
                    u.get("userPrincipalName"),
                    u.get("mail"),
                )
            )
            if len(out) > limit:
                break
        return out

    async def synced_groups(self) -> AsyncIterator[dict[str, Any]]:
        url = (
            f"{self.base_url}/groups?$filter="
            + quote("onPremisesSyncEnabled eq true", safe="")
            + "&$select=id,displayName,onPremisesSecurityIdentifier,onPremisesSamAccountName"
            + "&$top=999"
        )
        async for g in self._pages(url):
            yield g

    # --- Teams app install and chat (teams.md 6.2) ---------------------------

    async def install_app(self, object_id: str, teams_app_id: str) -> None:
        """Install the app for the user. Already installed (409) is fine; 403 raises
        `GraphError` with status 403 (blocked by tenant policy, or no Teams licence)."""
        await self._request(
            "POST",
            f"{self.base_url}/users/{guid(object_id)}/teamwork/installedApps",
            json={
                "teamsApp@odata.bind": (
                    f"https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/{quote(teams_app_id)}"
                )
            },
            ok=(200, 201, 204, 409),
        )

    async def installation_id(self, object_id: str, teams_app_id: str) -> str | None:
        flt = quote(f"teamsApp/id eq '{teams_app_id}'", safe="")
        url = (
            f"{self.base_url}/users/{guid(object_id)}/teamwork/installedApps"
            f"?$expand=teamsApp&$filter={flt}"
        )
        async for inst in self._pages(url):
            if isinstance(inst.get("id"), str):
                return inst["id"]
        return None

    async def chat_id(self, object_id: str, installation_id: str) -> str:
        resp = await self._request(
            "GET",
            f"{self.base_url}/users/{guid(object_id)}/teamwork/installedApps/"
            f"{quote(installation_id, safe='')}/chat",
        )
        cid = resp.json().get("id")
        if not isinstance(cid, str) or not cid:
            raise GraphError("no_chat")
        return cid

    # --- email (teams.md 8) ----------------------------------------------------

    async def send_mail(self, sender: str, to: str, subject: str, html_body: str) -> None:
        await self._request(
            "POST",
            f"{self.base_url}/users/{quote(sender, safe='@')}/sendMail",
            json={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": False,
            },
            ok=(200, 202),
        )
