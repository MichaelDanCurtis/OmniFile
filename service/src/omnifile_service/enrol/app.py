"""The `enrol` app: the only CA signer (control-api.md section 2).

- `POST /v0/enrol`: a new edge (no client certificate yet) trades its
  one-time enrolment token and a CSR for a 30-day client certificate.
- `POST /internal/v0/sign`: `api` forwards a renewal CSR here, or an edge's
  request for its IAM Roles Anywhere certificate (`purpose: roles_anywhere`,
  decision 50), with an Entra token for its managed identity. Edges reach
  this app too (for enrolment), so the route needs both that token and,
  when `sign_sources` is configured, a caller address inside those CIDRs
  (the Container Apps infrastructure subnet). Ingress should also restrict
  the path; the app does not rely on it.
  `purpose: api_server` signs api's own TLS server certificate instead
  (threat model T5: with TCP ingress api terminates mutual TLS itself), for
  the DNS names in enrol's configuration (`api_server_names`), never the
  CSR's. Then the enrolment answer tells each edge to pin the edge CA for
  api (`api_server_ca_sha256`).
- `POST /internal/v0/crl`: the worker fetches the edge CA's CRL (the
  unexpired certificates of revoked edges), built from the catalog and
  signed here, to upload it to Roles Anywhere. Its own caller (the
  worker's identity), and the same address check.

The caller's address is the TCP peer, or, only when the peer is one of
`trusted_ingress` (the ingress), the last address in X-Forwarded-For (the
one the ingress appended). Failed enrolment attempts (a bad token) are
rate-limited per that address and audited; every certificate issued is
audited.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC

from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..api.clientauth import Network, forwarded_client_ip
from ..api.db import Catalog
from ..api.idempotency import Result, request_key, run_idempotent
from ..api.problems import ApiError
from ..api.validate import bad, json_body, parse_id, req, req_enum, req_str, rfc3339
from .ca import (
    API_SERVER_CERT_LIFETIME,
    CertificateSigner,
    CsrError,
    cert_sha256,
    crl_builder,
    issue_api_server_certificate,
    issue_edge_certificate,
    issue_roles_anywhere_certificate,
    key_sha256,
    load_csr,
    renew_after,
)
from .callers import CallerVerifier

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

log = logging.getLogger("omnifile_service.enrol")

SIGN_FIELDS = {"edge_id", "csr_pem", "purpose", "current_cert_sha256", "request_id"}
SIGN_PURPOSES = ("renewal", "roles_anywhere", "api_server")
#: `purpose: api_server` names no edge: api asks for its own certificate.
API_SERVER_FIELDS = {"csr_pem", "purpose", "request_id"}


class FailureLimiter:
    """At most `limit` failed enrolments per source address per `window` seconds."""

    def __init__(
        self, limit: int = 10, window: float = 600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._fails: dict[str, deque[float]] = {}

    def _recent(self, addr: str) -> deque[float]:
        q = self._fails.setdefault(addr, deque())
        cutoff = self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, addr: str) -> bool:
        return len(self._recent(addr)) >= self.limit

    def failed(self, addr: str) -> None:
        self._recent(addr).append(self.clock())
        if len(self._fails) > 100_000:  # bound memory under a scan
            self._fails.clear()


@dataclass
class EnrolServices:
    catalog: Catalog
    signer: CertificateSigner
    callers: CallerVerifier
    api_url: str | None = None
    limiter: FailureLimiter = field(default_factory=FailureLimiter)
    #: Peers whose X-Forwarded-For is believed (the ingress). Empty: none.
    trusted_ingress: tuple[Network, ...] = ()
    #: If set, /internal/v0/sign and /internal/v0/crl are refused from any
    #: other caller address.
    sign_sources: tuple[Network, ...] = ()
    #: Who may fetch the CRL (the worker's identity); None: nobody.
    crl_callers: CallerVerifier | None = None
    #: api's DNS names (TCP ingress, threat model T5): enrol issues api's
    #: server certificate for these, and edges pin the edge CA for api.
    #: Empty: api's certificate is the ingress's, and nothing is issued.
    api_server_names: tuple[str, ...] = ()


async def audit(
    catalog: Catalog, action: str, decision: str, addr: str | None, details: dict
) -> None:
    try:
        async with catalog.tx("system:enrol") as db:
            await db.execute(
                "INSERT INTO catalog.audit_log (actor, action, decision, client_addr, details)"
                " VALUES ('system:enrol', %s, %s, %s::inet, %s::jsonb)",
                (action, decision, addr, json.dumps(details)),
            )
    except ApiError:
        log.warning("audit write failed", extra={"action": action})


def client_ip(request: Request, trusted_ingress: tuple[Network, ...]) -> IPAddress | None:
    """The caller's address (`forwarded_client_ip`)."""
    return forwarded_client_ip(
        request.client.host if request.client else None,
        request.headers.getlist("x-forwarded-for"),
        trusted_ingress,
    )


async def enrol(request: Request) -> Response:
    svc: EnrolServices = request.app.state.svc
    rid = uuid.uuid4().hex
    ip = client_ip(request, svc.trusted_ingress)
    addr = str(ip) if ip is not None else None
    source = addr or "unknown"
    try:
        if svc.limiter.blocked(source):
            raise ApiError("throttled", "too many failed enrolments", retry_after_s=60)
        key = request_key(request.headers)
        raw, body = await json_body(request, limit=64 * 1024)
        token = req_str(body, "token", max_len=512)
        if len(token) < 32:
            raise bad("token must be at least 32 characters")
        token_hash = hashlib.sha256(token.encode()).digest()

        async def handler() -> Result:
            return await _enrol(svc, body, token_hash, addr)

        return await run_idempotent(
            svc.catalog,
            f"enrol:{token_hash.hex()[:32]}",
            key,
            "POST",
            request.url.path,
            raw,
            handler,
            request_id=rid,
        )
    except ApiError as exc:
        # Only a wrong token counts: malformed requests can't guess a token.
        if exc.code == "enrolment_token_invalid":
            svc.limiter.failed(source)
        return exc.response(rid)
    except Exception:
        log.exception("enrol failed", extra={"request_id": rid})
        return ApiError("internal", "unexpected error").response(rid)


async def _enrol(svc: EnrolServices, body: dict, token_hash: bytes, addr: str | None) -> Result:
    csr_pem = req_str(body, "csr_pem", max_len=16_384)
    facts = req(body, "facts")
    if not isinstance(facts, dict):
        raise bad("facts must be an object")
    hostname = req_str(facts, "hostname", max_len=255)
    version = req_str(facts, "version", max_len=64)

    async with svc.catalog.read() as db:
        row = await (
            await db.execute(
                "SELECT edge_id FROM catalog.edge WHERE enrolment_token_sha256 = %s"
                " AND state = 'pending' AND enrolment_expires_at > now()",
                (token_hash,),
            )
        ).fetchone()
    if row is None:
        await audit(svc.catalog, "edge.enrol", "deny", addr, {"reason": "token"})
        raise ApiError("enrolment_token_invalid", "unknown, used or expired enrolment token")
    edge_id = row["edge_id"]
    try:
        issued = await issue_edge_certificate(svc.signer, edge_id, csr_pem)
    except CsrError as exc:
        raise bad(str(exc)) from None

    # One statement moves the edge to active, and only if the token is still
    # unused and unexpired: a token works once.
    async with svc.catalog.tx("system:enrol") as db:
        done = await (
            await db.execute(
                """
                UPDATE catalog.edge
                   SET state = 'active', enrolled_at = now(), cert_sha256 = %s,
                       cert_serial = %s, cert_not_after = %s, enrolment_token_sha256 = NULL,
                       enrolment_expires_at = NULL, software_version = %s, last_seen_at = now()
                 WHERE edge_id = %s AND enrolment_token_sha256 = %s AND state = 'pending'
                   AND enrolment_expires_at > now()
                RETURNING edge_id
                """,
                (issued.sha256, issued.serial_hex, issued.not_after, version, edge_id, token_hash),
            )
        ).fetchone()
        if done is not None:
            await db.execute(
                "INSERT INTO catalog.audit_log (actor, action, decision, client_addr, details)"
                " VALUES ('system:enrol', 'edge.enrol', 'allow', %s::inet, %s::jsonb)",
                (
                    addr,
                    json.dumps(
                        {
                            "edge_id": str(edge_id),
                            "hostname": hostname,
                            "version": version,
                            "cert_sha256": issued.sha256.hex(),
                            "serial": issued.serial_hex,
                        }
                    ),
                ),
            )
    if done is None:
        await audit(svc.catalog, "edge.enrol", "deny", addr, {"reason": "token_raced"})
        raise ApiError("enrolment_token_invalid", "the enrolment token was used")
    out = {
        "edge_id": str(edge_id),
        "cert_pem": issued.cert_pem,
        "chain_pem": issued.chain_pem,
        "ca_pem": issued.ca_pem,
        "not_after": rfc3339(issued.not_after),
    }
    if svc.api_url:
        out["api_url"] = svc.api_url
    if svc.api_server_names:
        # api's server certificate is the edge CA's (T5): the edge pins it for api.
        out["api_server_ca_sha256"] = cert_sha256(svc.signer.ca_certificate).hex()
    return Result(201, out)


async def sign(request: Request) -> Response:
    svc: EnrolServices = request.app.state.svc
    rid = uuid.uuid4().hex
    try:
        await _check_source(svc, request, "edge.certificate.sign")
        caller = await svc.callers.verify(request.headers.get("authorization"))
        raw, body = await json_body(request, limit=64 * 1024)
        extra = set(body) - SIGN_FIELDS
        if extra:
            raise bad(f"unexpected fields: {', '.join(sorted(extra))}")
        purpose = req_enum(body, "purpose", SIGN_PURPOSES)
        if purpose == "api_server":
            return await _api_server_request(svc, caller, raw, body, request.url.path, rid)
        edge_id = parse_id(req(body, "edge_id"), "edge_id")
        csr_pem = req_str(body, "csr_pem", max_len=16_384)
        presented_hex = req_str(body, "current_cert_sha256")
        try:
            presented = bytes.fromhex(presented_hex)
        except ValueError:
            presented = b""
        if len(presented) != 32 or presented_hex != presented.hex():
            raise bad("current_cert_sha256 must be 64 lowercase hex digits")
        request_id = req_str(body, "request_id")
        if not 16 <= len(request_id) <= 128:
            raise bad("request_id must be 16-128 characters")

        async def handler() -> Result:
            if purpose == "roles_anywhere":
                return await _sign_roles_anywhere(svc, caller, edge_id, csr_pem, presented)
            return await _sign(svc, caller, edge_id, csr_pem, presented)

        return await run_idempotent(
            svc.catalog,
            f"sign:{edge_id}" if purpose == "renewal" else f"ra-sign:{edge_id}",
            request_id,
            "POST",
            request.url.path,
            raw,
            handler,
            request_id=rid,
        )
    except ApiError as exc:
        return exc.response(rid)
    except Exception:
        log.exception("sign failed", extra={"request_id": rid})
        return ApiError("internal", "unexpected error").response(rid)


async def _api_server_request(
    svc: EnrolServices, caller: str, raw: bytes, body: dict, path: str, rid: str
) -> Response:
    """`purpose: api_server`: api's own server certificate (T5)."""
    extra = set(body) - API_SERVER_FIELDS
    if extra:
        raise bad(f"unexpected fields for api_server: {', '.join(sorted(extra))}")
    csr_pem = req_str(body, "csr_pem", max_len=16_384)
    request_id = req_str(body, "request_id")
    if not 16 <= len(request_id) <= 128:
        raise bad("request_id must be 16-128 characters")
    if not svc.api_server_names:
        await audit(svc.catalog, "api.server_certificate.sign", "deny", None, {"reason": "off"})
        raise ApiError("caller_not_allowed", "this enrol issues no api server certificates")

    async def handler() -> Result:
        return await _sign_api_server(svc, caller, csr_pem)

    return await run_idempotent(
        svc.catalog, "api-server", request_id, "POST", path, raw, handler, request_id=rid
    )


async def _sign_api_server(svc: EnrolServices, caller: str, csr_pem: str) -> Result:
    try:
        issued = await issue_api_server_certificate(svc.signer, svc.api_server_names, csr_pem)
    except CsrError as exc:
        raise bad(str(exc)) from None
    await audit(
        svc.catalog,
        "api.server_certificate.sign",
        "allow",
        None,
        {
            "caller": caller,
            "names": list(svc.api_server_names),
            "cert_sha256": issued.sha256.hex(),
            "serial": issued.serial_hex,
            "not_after": rfc3339(issued.not_after),
        },
    )
    return Result(
        201,
        {
            "cert_pem": issued.cert_pem,
            "chain_pem": issued.chain_pem,
            "ca_pem": issued.ca_pem,
            "not_after": rfc3339(issued.not_after),
            "renew_after": rfc3339(renew_after(issued.not_after, API_SERVER_CERT_LIFETIME)),
        },
    )


async def _check_source(svc: EnrolServices, request: Request, action: str) -> None:
    """The caller's address must be in `sign_sources`, when it is set."""
    if not svc.sign_sources:
        return
    ip = client_ip(request, svc.trusted_ingress)
    if ip is None or not any(ip in net for net in svc.sign_sources):
        await audit(svc.catalog, action, "deny", str(ip) if ip else None, {"reason": "source"})
        raise ApiError("caller_not_allowed", "not from the internal network")


async def _active_edge(svc: EnrolServices, edge_id: int, presented: bytes) -> dict:
    """The edge's row, if it is active and `presented` is its current
    certificate or its unexpired previous one."""
    async with svc.catalog.read() as db:
        edge = await (
            await db.execute(
                "SELECT state, cert_sha256, prev_cert_sha256, ra_key_sha256,"
                "       prev_cert_not_after > now() AS prev_ok"
                "  FROM catalog.edge WHERE edge_id = %s",
                (edge_id,),
            )
        ).fetchone()
    if edge is None:
        raise ApiError("not_found", f"no edge {edge_id}")
    if edge["state"] == "revoked":
        raise ApiError("edge_revoked", "this edge has been revoked")
    if edge["state"] != "active":
        raise ApiError("certificate_invalid", "the edge is not active")
    current = bytes(edge["cert_sha256"]) if edge["cert_sha256"] else None
    prev = bytes(edge["prev_cert_sha256"]) if edge["prev_cert_sha256"] else None
    if presented != current and not (presented == prev and edge["prev_ok"]):
        raise ApiError("certificate_invalid", "the presented certificate is not this edge's")
    return edge


async def _sign_roles_anywhere(
    svc: EnrolServices, caller: str, edge_id: int, csr_pem: str, presented: bytes
) -> Result:
    """The edge's IAM Roles Anywhere certificate (decision 50), for the key in
    its CSR, whose self-signature proves the edge holds it. The key is pinned
    at the first certificate, and the edge's mTLS certificate is untouched."""
    edge = await _active_edge(svc, edge_id, presented)
    try:
        key = load_csr(csr_pem)
    except CsrError as exc:
        raise bad(str(exc)) from None
    ksha = key_sha256(key)
    pinned = bytes(edge["ra_key_sha256"]) if edge["ra_key_sha256"] else None
    if pinned is not None and pinned != ksha:
        await audit(
            svc.catalog,
            "edge.roles_anywhere.sign",
            "deny",
            None,
            {"edge_id": str(edge_id), "reason": "key", "key_sha256": ksha.hex()},
        )
        raise ApiError(
            "aws_key_mismatch",
            "the CSR is not for this edge's Roles Anywhere key; a new key needs a new enrolment",
        )
    issued = await issue_roles_anywhere_certificate(svc.signer, edge_id, key)
    async with svc.catalog.tx("system:enrol") as db:
        # Pin the key the first time; the edge must still be active, with
        # this certificate and no other key pinned meanwhile.
        done = await (
            await db.execute(
                """
                UPDATE catalog.edge SET ra_key_sha256 = coalesce(ra_key_sha256, %(k)s)
                 WHERE edge_id = %(e)s AND state = 'active'
                   AND (cert_sha256 = %(p)s OR prev_cert_sha256 = %(p)s)
                   AND (ra_key_sha256 IS NULL OR ra_key_sha256 = %(k)s)
                RETURNING edge_id
                """,
                {"k": ksha, "e": edge_id, "p": presented},
            )
        ).fetchone()
        if done is None:
            raise ApiError("certificate_invalid", "the edge changed while signing; retry")
        await db.execute(
            "INSERT INTO catalog.edge_ra_certificate"
            " (serial, edge_id, cert_sha256, key_sha256, not_after) VALUES (%s, %s, %s, %s, %s)",
            (issued.serial_hex, edge_id, issued.sha256, ksha, issued.not_after),
        )
        await db.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:enrol', 'edge.roles_anywhere.sign', 'allow', %s::jsonb)",
            (
                json.dumps(
                    {
                        "edge_id": str(edge_id),
                        "caller": caller,
                        "purpose": "roles_anywhere",
                        "presented_sha256": presented.hex(),
                        "cert_sha256": issued.sha256.hex(),
                        "key_sha256": ksha.hex(),
                        "serial": issued.serial_hex,
                        "subject": issued.cert.subject.rfc4514_string(),
                        "not_after": rfc3339(issued.not_after),
                    }
                ),
            ),
        )
    return Result(
        201,
        {
            "cert_pem": issued.cert_pem,
            "chain_pem": issued.chain_pem,
            "ca_pem": issued.ca_pem,
            "not_after": rfc3339(issued.not_after),
            "renew_after": rfc3339(renew_after(issued.not_after)),
        },
    )


#: The CRL: every unexpired certificate of a revoked edge, its Roles Anywhere
#: ones and its current mTLS one.
#:
#: It never goes back to empty once an edge has been revoked. Roles Anywhere
#: refuses a CRL that revokes nothing, on ImportCrl and on UpdateCrl (real AWS,
#: 2026-11-14), so the weekly refresh would fail from the day the last revoked
#: certificate expired, and the published CRL's nextUpdate would pass. When
#: no revoked edge has an unexpired certificate, the CRL keeps one serial: the
#: most recently revoked edge's last certificate. Roles Anywhere accepts a CRL
#: whose only entry is a stale serial, one no live certificate has (verified
#: on real AWS: UpdateCrl, run rr-202611150718-62e9). It is one entry, never
#: every expired serial.
CRL_SQL = """
WITH issued AS (
    SELECT c.serial, c.not_after, coalesce(e.revoked_at, now()) AS revoked_at
      FROM catalog.edge_ra_certificate c JOIN catalog.edge e USING (edge_id)
     WHERE e.state = 'revoked'
    UNION
    SELECT e.cert_serial, e.cert_not_after, coalesce(e.revoked_at, now())
      FROM catalog.edge e
     WHERE e.state = 'revoked' AND e.cert_serial IS NOT NULL
), unexpired AS (
    SELECT serial, revoked_at FROM issued WHERE not_after > now()
), kept AS (
    SELECT serial, revoked_at FROM issued
     WHERE NOT EXISTS (SELECT 1 FROM unexpired)
     ORDER BY revoked_at DESC, not_after DESC NULLS LAST, serial
     LIMIT 1
)
SELECT serial, revoked_at FROM unexpired
UNION
SELECT serial, revoked_at FROM kept
"""


async def crl(request: Request) -> Response:
    """`POST /internal/v0/crl {}`: the edge CA's CRL, built from the catalog
    (never from the caller) and signed now, for the worker to upload to the
    Roles Anywhere trust anchors (decision 50). Audited."""
    svc: EnrolServices = request.app.state.svc
    rid = uuid.uuid4().hex
    try:
        await _check_source(svc, request, "edge_ca.crl.sign")
        if svc.crl_callers is None:
            raise ApiError("caller_not_allowed", "no CRL caller is configured")
        caller = await svc.crl_callers.verify(request.headers.get("authorization"))
        _, body = await json_body(request, limit=1024)
        if body:
            raise bad(f"unexpected fields: {', '.join(sorted(body))}")
        async with svc.catalog.read() as db:
            rows = await (await db.execute(CRL_SQL)).fetchall()
            now_row = await (await db.execute("SELECT now() AS now")).fetchone()
        assert now_row is not None
        now = now_row["now"]
        revoked = [(int(r["serial"], 16), r["revoked_at"]) for r in rows]
        # Monotonic across builds and replicas: the build time in microseconds.
        number = int(now.timestamp() * 1_000_000)
        signed = await svc.signer.sign_crl(
            crl_builder(svc.signer.ca_certificate, revoked, now, number)
        )
        through = max((at for _, at in revoked), default=None)
        next_update = signed.next_update_utc
        assert next_update is not None
        out = {
            "crl_pem": signed.public_bytes(serialization.Encoding.PEM).decode(),
            "crl_number": str(number),
            "this_update": rfc3339(signed.last_update_utc),
            "next_update": rfc3339(next_update),
            "revoked": len(revoked),
            # To the microsecond, as the catalog keeps it: the worker compares
            # it with revocation times to know the CRL is current.
            "through": (
                through.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                if through
                else None
            ),
        }
        await audit(
            svc.catalog,
            "edge_ca.crl.sign",
            "allow",
            None,
            {"caller": caller, "crl_number": str(number), "revoked": len(revoked)},
        )
        return JSONResponse(out)
    except ApiError as exc:
        return exc.response(rid)
    except Exception:
        log.exception("crl failed", extra={"request_id": rid})
        return ApiError("internal", "unexpected error").response(rid)


async def _sign(
    svc: EnrolServices, caller: str, edge_id: int, csr_pem: str, presented: bytes
) -> Result:
    await _active_edge(svc, edge_id, presented)
    try:
        issued = await issue_edge_certificate(svc.signer, edge_id, csr_pem)
    except CsrError as exc:
        raise bad(str(exc)) from None
    async with svc.catalog.tx("system:enrol") as db:
        # The certificate the edge presented becomes the previous one: it
        # keeps working until it expires, so a lost response never locks the
        # edge out. (If it presented its previous certificate, a certificate
        # it never received is dropped instead.)
        done = await (
            await db.execute(
                """
                UPDATE catalog.edge
                   SET prev_cert_not_after = CASE WHEN cert_sha256 = %(p)s THEN cert_not_after
                                                  ELSE prev_cert_not_after END,
                       prev_cert_sha256 = %(p)s,
                       cert_sha256 = %(n)s, cert_serial = %(s)s, cert_not_after = %(a)s
                 WHERE edge_id = %(e)s AND state = 'active'
                   AND (cert_sha256 = %(p)s OR prev_cert_sha256 = %(p)s)
                RETURNING edge_id
                """,
                {
                    "p": presented,
                    "n": issued.sha256,
                    "s": issued.serial_hex,
                    "a": issued.not_after,
                    "e": edge_id,
                },
            )
        ).fetchone()
        if done is None:
            raise ApiError("certificate_invalid", "the edge changed while signing; retry")
        await db.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:enrol', 'edge.certificate.sign', 'allow', %s::jsonb)",
            (
                json.dumps(
                    {
                        "edge_id": str(edge_id),
                        "caller": caller,
                        "purpose": "renewal",
                        "previous_sha256": presented.hex(),
                        "cert_sha256": issued.sha256.hex(),
                        "serial": issued.serial_hex,
                    }
                ),
            ),
        )
    return Result(
        201,
        {
            "cert_pem": issued.cert_pem,
            "chain_pem": issued.chain_pem,
            "ca_pem": issued.ca_pem,
            "not_after": rfc3339(issued.not_after),
        },
    )


async def healthz(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    if exc.status_code == 405:
        return ApiError("invalid_request", "method not allowed").response()
    return ApiError("not_found", "no such endpoint").response()


def create_enrol_app(services: EnrolServices, *, manage_lifecycle: bool = True) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        # A Key Vault signer is opened here, in the loop that serves
        # (kvsigner.DeferredKeyVaultSigner); a wrong key stops enrol at start.
        if (start := getattr(services.signer, "start", None)) is not None:
            await start()
        if manage_lifecycle:
            await services.catalog.open()
        try:
            yield
        finally:
            if manage_lifecycle:
                await services.catalog.close()

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/v0/enrol", enrol, methods=["POST"]),
            Route("/internal/v0/sign", sign, methods=["POST"]),
            Route("/internal/v0/crl", crl, methods=["POST"]),
        ],
        lifespan=lifespan,
        exception_handlers={HTTPException: http_error},
    )
    app.state.svc = services
    return app
