"""A dev OIDC issuer (portal-api.md 2.3). Mounted at `/dev-idp/` only with
OMNIFILE_DEV_MODE=1; `__main__` refuses it otherwise.

- `/.well-known/openid-configuration` and `/jwks` (a generated RSA key);
- `/authorize`: a page listing the seeded users, or straight through with
  `login_hint=<user key>`, or (tests) a user given as `dev_*` parameters;
  auth code + PKCE (S256 only), redirect URIs matched exactly;
- `/token`: `authorization_code` (ID tokens shaped like Entra's: `oid`,
  `roles`, `onprem_sid`, on-premises group SIDs, `tid`, `nonce`, the
  `_claim_names`/`hasgroups` overage markers) and `client_credentials` (app
  tokens with `idtyp = app` and the requested app roles);
- `/logout`: a page that links back to the portal.

Nothing here is used outside dev and tests.
"""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .auth import b64url

CODE_TTL_S = 120
ARM_RESOURCE = "https://management.azure.com"
TOKEN_TTL_S = 3600


@dataclass
class DevUser:
    key: str
    oid: str
    name: str
    upn: str
    onprem_sid: str | None
    groups: Sequence[str] = ()
    roles: Sequence[str] = ()
    overage: bool = False
    #: Leave auth_time out of ID tokens, as Entra does unless it is requested.
    no_auth_time: bool = False

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> DevUser:
        return cls(
            key=str(d["key"]),
            oid=str(d.get("oid") or uuid.uuid4()),
            name=str(d.get("name") or d["key"]),
            upn=str(d.get("upn") or f"{d['key']}@dev.test"),
            onprem_sid=d.get("onprem_sid"),
            groups=tuple(d.get("groups") or ()),
            roles=tuple(d.get("roles") or ()),
            overage=bool(d.get("overage", False)),
            no_auth_time=bool(d.get("no_auth_time", False)),
        )


@dataclass
class DevClient:
    client_id: str
    secret: str
    redirect_uris: Sequence[str] = ()
    #: App roles this client may ask for with client_credentials.
    app_roles: Sequence[str] = ()


@dataclass
class DevIdp:
    issuer: str
    tenant_id: str
    clients: dict[str, DevClient]
    users: dict[str, DevUser]
    #: Audience of app tokens (the API's application ID URI).
    api_audience: str
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    kid: str = field(default_factory=lambda: secrets.token_hex(8))
    _codes: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- keys --

    @property
    def public_keys(self) -> dict[str, Any]:
        return {self.kid: self.key.public_key()}

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def sign(self, claims: Mapping[str, Any]) -> str:
        return jwt.encode(dict(claims), self.key, algorithm="RS256", headers={"kid": self.kid})

    # -- tokens --

    def id_claims(self, user: DevUser, client_id: str, nonce: str | None) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer, "aud": client_id, "tid": self.tenant_id, "ver": "2.0",
            "sub": user.oid, "oid": user.oid, "name": user.name,
            "preferred_username": user.upn, "roles": list(user.roles),
            "iat": now, "nbf": now, "exp": now + TOKEN_TTL_S, "auth_time": now,
        }  # fmt: skip
        if nonce is not None:
            claims["nonce"] = nonce
        if user.no_auth_time:
            del claims["auth_time"]
        if user.onprem_sid:
            claims["onprem_sid"] = user.onprem_sid
        if user.overage:
            claims["_claim_names"] = {"groups": "src1"}
            claims["hasgroups"] = True
        else:
            claims["groups"] = list(user.groups)
        return claims

    def app_token(self, client_id: str, roles: Sequence[str], audience: str | None = None) -> str:
        now = int(time.time())
        return self.sign({
            "iss": self.issuer, "aud": audience or self.api_audience, "tid": self.tenant_id,
            "ver": "2.0", "sub": client_id, "oid": client_id, "azp": client_id,
            "idtyp": "app", "roles": list(roles), "iat": now, "nbf": now,
            "exp": now + TOKEN_TTL_S,
        })  # fmt: skip

    # -- routes --

    def _user_from(self, q: Mapping[str, str]) -> DevUser | None:
        if q.get("dev_oid"):
            return DevUser(
                key="adhoc",
                oid=q["dev_oid"],
                name=q.get("dev_name", "Dev user"),
                upn=q.get("dev_upn", "dev@dev.test"),
                onprem_sid=q.get("dev_sid") or None,
                groups=tuple(g for g in q.get("dev_groups", "").split(",") if g),
                roles=tuple(r for r in q.get("dev_roles", "").split(",") if r),
                overage=q.get("dev_overage") == "1",
                no_auth_time=q.get("dev_no_auth_time") == "1",
            )
        hint = q.get("login_hint")
        return self.users.get(hint) if hint else None

    async def authorize(self, request: Request) -> Response:
        q = request.query_params
        client = self.clients.get(q.get("client_id", ""))
        if client is None or q.get("redirect_uri") not in client.redirect_uris:
            return HTMLResponse("<p>Unknown client or redirect URI.</p>", status_code=400)
        if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256":
            return HTMLResponse("<p>Only response_type=code with PKCE S256.</p>", status_code=400)
        if not q.get("code_challenge") or not q.get("state"):
            return HTMLResponse("<p>code_challenge and state are required.</p>", status_code=400)
        user = self._user_from(q)
        if user is None:
            return self._picker(q)
        code = secrets.token_urlsafe(32)
        self._codes[code] = {
            "client_id": client.client_id,
            "redirect_uri": q["redirect_uri"],
            "challenge": q["code_challenge"],
            "nonce": q.get("nonce"),
            "scope": q.get("scope", ""),
            "user": user,
            "exp": time.time() + CODE_TTL_S,
        }
        return RedirectResponse(
            f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}",
            status_code=302,
        )

    def _picker(self, q: Mapping[str, str]) -> Response:
        keep = {k: v for k, v in q.items() if not k.startswith("dev_") and k != "login_hint"}
        items = []
        for u in self.users.values():
            href = "authorize?" + urlencode({**keep, "login_hint": u.key})
            items.append(
                f'<li><a href="{html.escape(href)}">{html.escape(u.name)}</a>'
                f" ({html.escape(u.upn)}; roles: {html.escape(', '.join(u.roles) or 'none')};"
                f" SID {html.escape(u.onprem_sid or 'none')})</li>"
            )
        body = (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<title>OmniFile dev sign-in</title></head><body>"
            "<h1>Dev sign-in (not Entra)</h1><p>Pick a seeded user:</p><ul>"
            + "".join(items)
            + "</ul></body></html>"
        )
        return HTMLResponse(body)

    async def token(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        grant = form.get("grant_type")
        client = self.clients.get(str(form.get("client_id", "")))
        if client is None or not secrets.compare_digest(
            str(form.get("client_secret", "")), client.secret
        ):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if grant == "authorization_code":
            code = self._codes.pop(str(form.get("code", "")), None)
            if code is None or code["exp"] < time.time() or code["client_id"] != client.client_id:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if form.get("redirect_uri") != code["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = str(form.get("code_verifier", ""))
            if b64url(hashlib.sha256(verifier.encode()).digest()) != code["challenge"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"},
                                    status_code=400)  # fmt: skip
            id_token = self.sign(self.id_claims(code["user"], client.client_id, code["nonce"]))
            out = {"id_token": id_token, "token_type": "Bearer", "expires_in": TOKEN_TTL_S}
            if ARM_RESOURCE in code["scope"]:
                # Connect Azure's delegated token (docs/design/cloud-connect.md 1.2): a dev
                # token for ARM, which only the dev and test fakes ever see.
                now = int(time.time())
                out["access_token"] = self.sign({
                    "iss": self.issuer, "aud": ARM_RESOURCE, "tid": self.tenant_id,
                    "oid": code["user"].oid, "scp": "user_impersonation", "iat": now,
                    "nbf": now, "exp": now + TOKEN_TTL_S,
                })  # fmt: skip
            return JSONResponse(out)
        if grant == "client_credentials":
            asked = [r for r in str(form.get("roles", "")).split(",") if r]
            roles = [r for r in (asked or client.app_roles) if r in client.app_roles]
            return JSONResponse({
                "access_token": self.app_token(client.client_id, roles),
                "token_type": "Bearer", "expires_in": TOKEN_TTL_S,
            })  # fmt: skip
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def configuration(self, request: Request) -> Response:
        return JSONResponse({
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "end_session_endpoint": f"{self.issuer}/logout",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "subject_types_supported": ["pairwise"],
        })  # fmt: skip

    async def jwks_route(self, request: Request) -> Response:
        return JSONResponse(self.jwks())

    async def logout(self, request: Request) -> Response:
        back = request.query_params.get("post_logout_redirect_uri", "")
        allowed = {u.rsplit("/auth/callback", 1)[0] + "/" for c in self.clients.values()
                   for u in c.redirect_uris}  # fmt: skip
        link = (
            f'<p><a href="{html.escape(back)}">Back to the portal</a></p>'
            if back in allowed
            else ""
        )
        return HTMLResponse(
            "<!doctype html><html lang=en><head><meta charset=utf-8><title>Signed out</title>"
            f"</head><body><h1>Signed out</h1>{link}</body></html>"
        )

    def mounted(self) -> Starlette:
        """The issuer under /dev-idp, as `web` mounts it: for an in-process
        httpx transport to the token endpoint."""
        return Starlette(routes=[Mount("/dev-idp", app=self.app())])

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self.configuration, methods=["GET"]),
                Route("/jwks", self.jwks_route, methods=["GET"]),
                Route("/authorize", self.authorize, methods=["GET"]),
                Route("/token", self.token, methods=["POST"]),
                Route("/logout", self.logout, methods=["GET"]),
            ]
        )


def default_users(domain: str = "S-1-5-21-1000-2000-3000") -> list[DevUser]:
    """Seeded users for the dev stack: an ArchiveAdmin, a DataSteward and a
    User, in one AD domain. Group RIDs: 1100 admins, 1101 stewards, 1102
    geoscience readers."""
    return [
        DevUser("admin", "11111111-1111-4111-8111-111111111111", "Ada Admin", "ada@dev.test",
                f"{domain}-1001", (f"{domain}-1100",), ("ArchiveAdmin",)),
        DevUser("steward", "22222222-2222-4222-8222-222222222222", "Sam Steward",
                "sam@dev.test", f"{domain}-1002", (f"{domain}-1101",), ("DataSteward",)),
        DevUser("reader", "33333333-3333-4333-8333-333333333333", "Rita Reader",
                "rita@dev.test", f"{domain}-1003", (f"{domain}-1102",), ("User",)),
    ]  # fmt: skip
