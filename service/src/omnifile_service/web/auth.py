"""Sign-in and callers (portal-api.md section 2).

Browsers: Entra OIDC as a backend-for-frontend. Authorization code + PKCE
(S256); the browser never sees a token. A sign-in creates a server-side
session (`catalog.web_session`) whose 256-bit id is the `__Host-` cookie;
the catalog stores only its SHA-256. A new session id is issued at every
sign-in and any session cookie the browser brought is deleted, so a planted
cookie can't be carried into a signed-in session (fixation).

Partners: Entra v2 access tokens with `idtyp = app` and app roles, validated
against the tenant's JWKS. Delegated tokens are refused. The application must
also be registered and enabled in Settings, API clients, and its roles are
cut to the client's scopes (`apiclients/registry.py`). An OmniFile API key
(`Bearer ofk1.…`, `apiclients/keys.py`) is the alternative to a token.

`web` holds no client secret in production: it authenticates to the token
endpoint with a client assertion from its managed identity (a federated
credential on the app registration). A static secret is accepted in dev mode
only. VERIFY on a real tenant (docs/milestones/real-tenant-checks.md).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..api.db import Catalog
from ..api.problems import ApiError
from ..apiclients import keys as api_keys
from ..apiclients import registry as api_clients
from ..worker.queue import send
from .config import APP_ROLES, USER_ROLES, WebSettings
from .context import LOGIN_COOKIE, SESSION_COOKIE, Principal, WebServices, client_addr, tuple_rows
from .problems import WebError

log = logging.getLogger("omnifile_service.web.auth")

_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
_DOMAIN = re.compile(r"^(S-1-5-21-[0-9]+-[0-9]+-[0-9]+)-[0-9]+$")
AUTHENTICATED_USERS = "S-1-5-11"
DOMAIN_USERS_RID = "513"
IDENTITY_RESOLVE = "identity_resolve"
ACTOR = "system:web"
#: Session last-seen writes are skipped when the previous one is this recent.
TOUCH_EVERY = timedelta(seconds=30)
ALGORITHMS = ["RS256"]
AUTH_TIME_CLAIM = '{"id_token":{"auth_time":{"essential":true}}}'


# --- keys and client credentials --------------------------------------------------


class JwksSource(Protocol):
    async def key(self, kid: str | None) -> Any: ...


class StaticJwks:
    """Keys known in process (the dev issuer, tests)."""

    def __init__(self, keys: dict[str, Any]) -> None:
        self.keys = keys

    async def key(self, kid: str | None) -> Any:
        if kid not in self.keys:
            raise WebError("unauthenticated", "token signed by an unknown key")
        return self.keys[kid]


class HttpJwks:
    """The tenant JWKS over HTTPS, cached; refetched for an unknown kid at
    most every `min_refresh` seconds."""

    def __init__(
        self, url: str, http: httpx.AsyncClient, *, ttl: float = 3600, min_refresh: float = 300
    ) -> None:
        self.url, self.http, self.ttl, self.min_refresh = url, http, ttl, min_refresh
        self._keys: dict[str, Any] = {}
        self._at = 0.0

    async def _fetch(self) -> None:
        resp = await self.http.get(self.url, timeout=5)
        resp.raise_for_status()
        keys: dict[str, Any] = {}
        for jwk in resp.json().get("keys", []):
            try:
                keys[jwk["kid"]] = jwt.PyJWK(jwk).key
            except (jwt.PyJWKError, KeyError):
                continue
        self._keys, self._at = keys, time.monotonic()

    async def key(self, kid: str | None) -> Any:
        age = time.monotonic() - self._at
        if kid not in self._keys and (age > self.min_refresh or not self._keys) or age > self.ttl:
            try:
                await self._fetch()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("JWKS fetch failed: %s", e)
                if not self._keys:
                    raise WebError(
                        "unavailable", "signing keys unavailable", retry_after_s=5
                    ) from e
        if kid not in self._keys:
            raise WebError("unauthenticated", "token signed by an unknown key")
        return self._keys[kid]


class ClientCredential(Protocol):
    async def fields(self) -> dict[str, str]: ...


class StaticSecret:
    """Dev only: `__main__` refuses it unless OMNIFILE_DEV_MODE=1."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def fields(self) -> dict[str, str]:
        return {"client_secret": self._secret}


class ManagedIdentityAssertion:
    """A client assertion from the app's managed identity (workload identity
    federation). VERIFY on a real tenant."""

    AUDIENCE = "api://AzureADTokenExchange/.default"

    def __init__(self, client_id: str | None = None) -> None:
        from azure.identity.aio import ManagedIdentityCredential

        self._cred = ManagedIdentityCredential(client_id=client_id)

    async def fields(self) -> dict[str, str]:
        token = await self._cred.get_token(self.AUDIENCE)
        return {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": token.token,
        }

    async def aclose(self) -> None:
        await self._cred.close()


# --- helpers -----------------------------------------------------------------------


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode()).digest())


def sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def safe_return_to(value: str | None) -> str:
    """A same-origin path, or "/" (no open redirects): it must start with one
    "/", and hold no backslash, control character or scheme-relative form."""
    if not value or len(value) > 2048:
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    if re.match(r"^/+[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        return "/"
    return value


def session_groups(user_sid: str | None, groups: list[str]) -> list[str]:
    """Token groups plus Domain Users of the user's domain and Authenticated
    Users (section 3.3): Entra doesn't sync the primary-group membership."""
    out = [g for g in groups if isinstance(g, str) and _SID.match(g)]
    if user_sid and (m := _DOMAIN.match(user_sid)):
        out.append(f"{m[1]}-{DOMAIN_USERS_RID}")
    if user_sid:
        out.append(AUTHENTICATED_USERS)
    return sorted(set(out))


def _cookie(name: str, value: str, *, same_site: str, max_age: int | None) -> str:
    parts = [f"{name}={value}", "Path=/", "Secure", "HttpOnly", f"SameSite={same_site}"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def clear_cookie(name: str) -> str:
    return _cookie(name, "", same_site="Strict", max_age=0)


# --- the authenticator ---------------------------------------------------------------


class Authenticator:
    def __init__(
        self,
        settings: WebSettings,
        catalog: Catalog,
        jwks: JwksSource,
        http: httpx.AsyncClient,
        credential: ClientCredential,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.jwks = jwks
        self.http = http
        self.credential = credential
        #: Refused API keys per address: throttled before catalog work, audited in aggregate.
        self.key_refusals = api_clients.KeyRefusals()
        #: identity_id -> when this replica last asked for a group refresh.
        self._refresh_asked: dict[int, datetime] = {}

    # -- callers --

    async def principal(self, request: Request, *, bearer: bool, session: bool) -> Principal:
        authz = request.headers.get("authorization")
        if authz is not None:
            if not bearer:
                raise WebError("unauthenticated", "this route takes a session, not a token")
            scheme, _, token = authz.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise WebError("unauthenticated", "Authorization must be a Bearer token")
            token = token.strip()
            if api_keys.looks_like_key(token):
                addr = client_addr(request, self.settings.trusted_ingress)
                return await api_clients.admit_key(
                    self.catalog, token, self.settings.clock(), addr, self.key_refusals
                )
            return await self.app_principal(token)
        if not session:
            raise WebError("unauthenticated", "a bearer token is required")
        return await self.session_principal(request)

    async def _decode(self, token: str, *, audience: Any, **opts: Any) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise WebError("unauthenticated", "malformed token") from None
        if header.get("alg") not in ALGORITHMS:
            raise WebError("unauthenticated", "token algorithm not allowed")
        key = await self.jwks.key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=audience,
                issuer=self.settings.oidc.issuer,
                leeway=self.settings.leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
                **opts,
            )
        except jwt.PyJWTError as e:
            # The reason goes to the log, not to the caller (M7 review).
            log.info("token rejected: %s: %s", type(e).__name__, e)
            raise WebError("unauthenticated", "token rejected") from None
        if claims.get("tid") != self.settings.tenant_id:
            raise WebError("unauthenticated", "token is for another tenant")
        return claims

    async def app_principal(self, token: str) -> Principal:
        if not self.settings.api_audiences:
            raise WebError("unauthenticated", "partner tokens are not configured")
        claims = await self._decode(token, audience=list(self.settings.api_audiences))
        if claims.get("idtyp") != "app":
            raise WebError("unauthenticated", "only application tokens are accepted")
        app_id = claims.get("azp") or claims.get("appid")
        if not isinstance(app_id, str) or not app_id:
            raise WebError("unauthenticated", "token has no azp")
        roles = frozenset(r for r in (claims.get("roles") or []) if r in APP_ROLES)
        # Registered and enabled in Settings, API clients; roles cut to its scopes.
        return await api_clients.admit_app(self.catalog, app_id, roles, self.settings.clock())

    async def session_principal(self, request: Request) -> Principal:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw or len(raw) > 256:
            raise WebError("unauthenticated", "not signed in")
        now = self.settings.clock()
        async with self.catalog.read() as db:
            row = await (
                await db.execute(
                    "SELECT s.*, s.group_sids::text[] AS group_sids,"
                    "       i.group_sids::text[] AS identity_groups,"
                    "       i.groups_resolved_at AS identity_groups_at,"
                    "       i.groups_source AS identity_groups_source"
                    "  FROM catalog.web_session s"
                    "  LEFT JOIN catalog.identity i ON i.identity_id = s.identity_id"
                    " WHERE s.session_sha256 = %s AND s.expires_at > %s"
                    "   AND s.idle_expires_at > %s",
                    (sha256(raw), now, now),
                )
            ).fetchone()
        if row is None:
            raise WebError("unauthenticated", "session expired; sign in again")
        if now - row["last_seen_at"] > TOUCH_EVERY:
            async with self.catalog.tx(ACTOR) as db:
                await db.execute(
                    "UPDATE catalog.web_session SET last_seen_at = %s,"
                    " idle_expires_at = least(%s, expires_at) WHERE session_sha256 = %s",
                    (now, now + self.settings.session_idle, sha256(raw)),
                )
        groups = list(row["group_sids"] or [])
        source = row["groups_source"]
        resolved_at = row["created_at"] if source == "token" else None
        at = row["identity_groups_at"]
        if source == "pending":
            if at is not None and now - at < self.settings.groups_cache_max_age:
                groups = sorted(set(groups) | set(row["identity_groups"] or []))
                source = row["identity_groups_source"] or "graph"
                resolved_at = at
        elif at is not None and at > row["created_at"]:
            # Graph resolved the user's groups after the session began: those
            # replace the token's, removals included.
            groups = sorted(set(row["identity_groups"] or []))
            source = row["identity_groups_source"] or "graph"
            resolved_at = at
        if (
            row["identity_id"] is not None
            and resolved_at is not None
            and now - resolved_at >= self.settings.groups_refresh_every
        ):
            await self._refresh_groups(row["identity_id"], now)
        return Principal(
            kind="user",
            roles=frozenset(row["roles"] or []),
            object_id=str(row["entra_object_id"]),
            identity_id=row["identity_id"],
            sid=row["user_sid"],
            display_name=row["display_name"],
            upn=row["upn"],
            groups=tuple(groups),
            groups_source=source,
            groups_resolved_at=resolved_at,
            session=row,
        )

    async def _refresh_groups(self, identity_id: int, now: datetime) -> None:
        """Ask the worker to re-read a user's groups from Graph, at most once
        per `groups_refresh_every` per identity on this replica."""
        last = self._refresh_asked.get(identity_id)
        if last is not None and now - last < self.settings.groups_refresh_every:
            return
        self._refresh_asked[identity_id] = now
        if len(self._refresh_asked) > 10_000:
            self._refresh_asked.clear()
        try:
            async with self.catalog.tx(ACTOR) as db:
                await db.execute(
                    "SELECT pgmq.send('identity_resolve', %s::jsonb)",
                    (json.dumps({"identity_id": str(identity_id)}),),
                )
        except ApiError:
            log.warning("could not queue a group refresh")

    def require_recent_auth(self, who: Principal) -> None:
        """Section 2.4: WORM enable/cancel and legal holds."""
        max_age = self.settings.reauth_max_age
        at = (who.session or {}).get("auth_time")
        if at is None or self.settings.clock() - at > max_age:
            raise WebError(
                "reauth_required",
                "sign in again to confirm this change",
                max_age_s=int(max_age.total_seconds()),
            )

    # -- sign-in routes --

    async def login(self, request: Request) -> Response:
        s = self.settings
        return_to = safe_return_to(request.query_params.get("return_to"))
        reauth = request.query_params.get("reauth") == "1"
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        now = s.clock()
        async with self.catalog.tx(ACTOR) as db:
            await db.execute(
                "INSERT INTO catalog.oidc_login (state, nonce, code_verifier, return_to, reauth,"
                " created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (state, nonce, verifier, return_to, reauth, now, now + s.login_ttl),
            )
        params = {
            "client_id": s.client_id,
            "response_type": "code",
            "redirect_uri": s.redirect_uri,
            "response_mode": "query",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
            # auth_time is an optional claim in Entra: ask for it (re-auth needs it).
            "claims": AUTH_TIME_CLAIM,
        }
        if reauth:
            params.update({"prompt": "login", "max_age": "0"})
        resp = RedirectResponse(f"{s.oidc.authorize}?{urlencode(params)}", status_code=302)
        resp.headers.append(
            "set-cookie",
            _cookie(LOGIN_COOKIE, state, same_site="Lax", max_age=int(s.login_ttl.total_seconds())),
        )
        return resp

    async def _redeem(self, code: str, verifier: str) -> dict[str, Any]:
        s = self.settings
        form = {
            "grant_type": "authorization_code",
            "client_id": s.client_id,
            "code": code,
            "redirect_uri": s.redirect_uri,
            "code_verifier": verifier,
            **(await self.credential.fields()),
        }
        try:
            resp = await self.http.post(s.oidc.token, data=form, timeout=10)
        except httpx.HTTPError as e:
            log.warning("token endpoint unreachable: %s", e)
            raise WebError("unavailable", "sign-in service unreachable", retry_after_s=5) from None
        if resp.status_code != 200:
            raise WebError("unauthenticated", "the sign-in code was refused")
        try:
            data = resp.json()
        except ValueError:
            raise WebError("unauthenticated", "bad token response") from None
        if not isinstance(data, dict) or not isinstance(data.get("id_token"), str):
            raise WebError("unauthenticated", "no ID token in the token response")
        return data

    async def callback(self, request: Request) -> Response:
        s = self.settings
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            if request.query_params.get("error"):
                raise WebError("unauthenticated", "sign-in was cancelled or refused")
            raise WebError("invalid_request", "code and state are required")
        cookie = request.cookies.get(LOGIN_COOKIE)
        if not cookie or not hmac.compare_digest(cookie.encode(), state.encode()):
            raise WebError("unauthenticated", "sign-in state does not match this browser")
        now = s.clock()
        async with self.catalog.tx(ACTOR) as db:
            login = await (
                await db.execute(
                    "DELETE FROM catalog.oidc_login WHERE state = %s RETURNING *", (state,)
                )
            ).fetchone()
        if login is None or login["expires_at"] <= now:
            raise WebError("unauthenticated", "sign-in expired; start again")
        tokens = await self._redeem(code, login["code_verifier"])
        claims = await self._decode(tokens["id_token"], audience=s.client_id)
        if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(
            claims["nonce"].encode(), login["nonce"].encode()
        ):
            raise WebError("unauthenticated", "ID token nonce mismatch")
        oid = claims.get("oid")
        if not isinstance(oid, str) or not oid:
            raise WebError("unauthenticated", "ID token has no oid")
        # Without auth_time a sign-in never counts as recent: `iat` is also
        # fresh after a silent SSO, which would let it stand in for re-auth.
        auth_ts = claims.get("auth_time")
        if isinstance(auth_ts, bool) or not isinstance(auth_ts, int | float):
            if login["reauth"]:
                raise WebError(
                    "unauthenticated", "the sign-in service did not confirm a fresh sign-in"
                )
            auth_ts = 0
        auth_time = datetime.fromtimestamp(int(auth_ts), tz=now.tzinfo)
        if login["reauth"] and auth_time < login["created_at"] - timedelta(seconds=60):
            raise WebError("unauthenticated", "re-authentication did not happen")
        session_id = await self._sign_in(request, claims, oid, auth_time, now)
        resp = RedirectResponse(login["return_to"], status_code=302)
        resp.headers.append(
            "set-cookie",
            _cookie(
                SESSION_COOKIE,
                session_id,
                same_site="Strict",
                max_age=int(s.session_absolute.total_seconds()),
            ),
        )
        resp.headers.append("set-cookie", clear_cookie(LOGIN_COOKIE))
        return resp

    async def _sign_in(
        self, request: Request, claims: dict[str, Any], oid: str, auth_time: datetime, now
    ) -> str:
        s = self.settings
        sid = claims.get("onprem_sid")
        if not (isinstance(sid, str) and _SID.match(sid)):
            sid = None
        roles = sorted(r for r in (claims.get("roles") or []) if r in USER_ROLES)
        # Decision 89: with named people holding the roles, every AD user in the tenant
        # is a User. "AD user" is a token with a SID: a guest or a cloud-only account has
        # none, and gets only what Entra gave. What a User sees is still each file's ACL.
        tenant_user = s.role_holders == "people" and sid is not None and "User" not in roles
        if tenant_user:
            roles = sorted([*roles, "User"])
        names = claims.get("_claim_names") or {}
        overage = bool(claims.get("hasgroups")) or (isinstance(names, dict) and "groups" in names)
        token_groups = [g for g in (claims.get("groups") or []) if isinstance(g, str)]
        name = claims.get("name") if isinstance(claims.get("name"), str) else None
        upn = claims.get("preferred_username")
        upn = upn if isinstance(upn, str) else None

        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        async with self.catalog.tx(ACTOR) as db:
            identity = None
            if sid is not None:
                identity = await (
                    await db.execute(
                        "SELECT * FROM catalog.identity WHERE sid = %s FOR UPDATE", (sid,)
                    )
                ).fetchone()
            if identity is None:
                identity = await (
                    await db.execute(
                        "SELECT * FROM catalog.identity WHERE entra_object_id = %s FOR UPDATE",
                        (oid,),
                    )
                ).fetchone()
                if identity is not None and sid is not None and identity["sid"] != sid:
                    # The oid moved to another SID: the token is the authority.
                    await db.execute(
                        "UPDATE catalog.identity SET entra_object_id = NULL WHERE identity_id = %s",
                        (identity["identity_id"],),
                    )
                    identity = None
            if identity is None and sid is not None:
                identity = await (
                    await db.execute(
                        "INSERT INTO catalog.identity (sid, kind) VALUES (%s, 'user')"
                        " ON CONFLICT (sid) DO UPDATE SET sid = EXCLUDED.sid RETURNING *",
                        (sid,),
                    )
                ).fetchone()
            identity_id = identity["identity_id"] if identity else None
            source = "none"
            groups: list[str] = []
            if identity_id is not None:
                await db.execute(
                    "UPDATE catalog.identity SET entra_object_id = NULL"
                    " WHERE entra_object_id = %s AND identity_id <> %s",
                    (oid, identity_id),
                )
                if overage:
                    groups = session_groups(identity["sid"], [])
                    source = "pending"
                    await db.execute(
                        "UPDATE catalog.identity SET entra_object_id = %s, upn = %s,"
                        " display_name = coalesce(%s, display_name), updated_at = now()"
                        " WHERE identity_id = %s",
                        (oid, upn, name, identity_id),
                    )
                    with tuple_rows(db):
                        await send(db, IDENTITY_RESOLVE, {"identity_id": str(identity_id)})
                else:
                    groups = session_groups(identity["sid"], token_groups)
                    source = "token"
                    await db.execute(
                        "UPDATE catalog.identity SET entra_object_id = %s, upn = %s,"
                        " display_name = coalesce(%s, display_name), group_sids = %s,"
                        " groups_source = 'token', groups_resolved_at = %s, updated_at = now()"
                        " WHERE identity_id = %s",
                        # The same set the session uses (with Domain Users and
                        # Authenticated Users), so Teams and the portal agree.
                        (oid, upn, name, groups, now, identity_id),
                    )  # fmt: skip
            if identity_id is not None:
                # For API clients' owners (apiclients/access.py): the roles
                # Entra gave at this sign-in.
                await db.execute(
                    "UPDATE catalog.identity SET app_roles = %s, app_roles_seen_at = %s"
                    " WHERE identity_id = %s",
                    (roles, now, identity_id),
                )
            # Fixation: whatever session the browser brought ends here.
            old = request.cookies.get(SESSION_COOKIE)
            if old and len(old) <= 256:
                await db.execute(
                    "DELETE FROM catalog.web_session WHERE session_sha256 = %s", (sha256(old),)
                )
            await db.execute(
                "INSERT INTO catalog.web_session (session_sha256, identity_id, entra_object_id,"
                " user_sid, display_name, upn, roles, group_sids, groups_source, csrf_token,"
                " auth_time, created_at, last_seen_at, idle_expires_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    sha256(session_id), identity_id, oid,
                    identity["sid"] if identity else None, name, upn, roles, groups, source,
                    csrf, auth_time, now, now,
                    min(now + s.session_idle, now + s.session_absolute),
                    now + s.session_absolute,
                ),
            )  # fmt: skip
            await db.execute(
                "SELECT set_config('omnifile.actor', %s, true)",
                (f"user:{identity_id}" if identity_id else f"user:entra:{oid}",),
            )
            await db.execute(
                "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision,"
                " client_addr, details) VALUES (current_setting('omnifile.actor'), %s, 'proven',"
                " 'portal.sign_in', 'allow', %s::inet, %s::jsonb)",
                (
                    identity["sid"] if identity else None,
                    client_addr(request, self.settings.trusted_ingress),
                    _json(
                        {
                            "roles": roles,
                            "groups_source": source,
                            "oid": oid,
                            **({"user_role": "tenant"} if tenant_user else {}),
                        }
                    ),
                ),
            )
        return session_id

    async def logout(self, who: Principal) -> tuple[str, str]:
        """Deletes the session. Returns (Set-Cookie, end-session URL)."""
        async with self.catalog.tx(who.actor) as db:
            await db.execute(
                "DELETE FROM catalog.web_session WHERE session_sha256 = %s",
                (bytes((who.session or {})["session_sha256"]),),
            )
        url = (
            self.settings.oidc.end_session
            + "?"
            + urlencode({"post_logout_redirect_uri": self.settings.origin + "/"})
        )
        return clear_cookie(SESSION_COOKIE), url


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


async def auth_route(request: Request, fn) -> Response:
    """Errors from the sign-in routes as problem details."""
    try:
        return await fn(request)
    except ApiError as exc:
        return exc.response()
    except Exception:
        log.exception("sign-in error")
        return ApiError("internal", "unexpected error").response()


def services(request: Request) -> WebServices:
    return request.app.state.svc
