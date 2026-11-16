"""The edge CA's CRL, uploaded to IAM Roles Anywhere (decision 50; threat model T4).

Each edge's Roles Anywhere certificate is issued by OmniFile's edge CA, which
the client registered as a trust anchor. When an edge is revoked, that
certificate must stop working at AWS too, not only at the control API: the
worker gets the CA's CRL from `enrol` (the only app that signs; it builds the
list from the catalog, never from us) and uploads it to every trust anchor
in the operator's settings (``OMNIFILE_AWS_ACCOUNTS``, an account's
``roles_anywhere_trust_anchor_arn``; never a partner's input):

- ``ImportCrl`` the first time (tagged ``omnifile-trust-anchor = <anchor id>``,
  which the setup guide's IAM policy conditions on), ``UpdateCrl`` after. The
  CRL's id is kept in ``catalog.roles_anywhere_crl``; a lost id is found again
  with ``ListCrls`` (same trust anchor and name), so a retry never imports a
  second one.
- With the service's own web-identity AWS session (decision 43), through the
  account's Roles Anywhere interface endpoint with private networking.

When it runs:
- ``roles_anywhere_crl`` (queue): sent by the revocation in the same
  transaction (decision 27). It publishes at once, even during a backoff.
- ``roles_anywhere_crl`` (sweep, every minute): publishes when a revocation
  isn't in the published CRL yet (a lost message, a failure), and refreshes
  it weekly so its nextUpdate (30 days) never passes. A failure backs off
  (30 s doubling to 15 min) and is recorded; the gauge
  ``omnifile.roles_anywhere.crl_failing`` and the alert
  ``roles-anywhere-crl-failing`` report a trust anchor that keeps failing.

A CRL that revokes nothing is never uploaded: Roles Anywhere refuses one, on
``ImportCrl`` ("At least one of the included CRLs doesn't include a revoked
certificate") and on ``UpdateCrl`` ("CRL does not revoke any cert"; real AWS,
2026-11-14). `enrol` keeps one serial in the CRL once an edge has been revoked
(``CRL_SQL`` in `enrol/app.py`), so the weekly refresh always has something to
upload: Roles Anywhere accepts a CRL whose only entry is a stale serial
(verified on real AWS, run rr-202611150718-62e9). Should an empty one arrive
all the same, it is skipped and logged, and asked for again after the longest
backoff. A skip records no failure, and leaves one recorded before as it is.

Live Roles Anywhere sessions end only when they expire: the profile caps them
at an hour (T4), so a revoked edge reads for at most that long after the
upload.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
import psycopg
from cryptography import x509

from ..api.signclient import ManagedIdentityToken, StaticToken, TokenProvider
from .jobs import Job, QueueJob, SweepJob
from .queue import Message

log = logging.getLogger(__name__)

QUEUE = "roles_anywhere_crl"
#: The tag ImportCrl sets and the setup guide's policy conditions on.
TAG_KEY = "omnifile-trust-anchor"
DEFAULT_CRL_NAME = "omnifile-edge-ca"
_ANCHOR_ID = re.compile(r":trust-anchor/([0-9a-f-]{36})$")


class CrlNotFound(Exception):  # noqa: N818
    """UpdateCrl named a CRL that no longer exists."""


@dataclass(frozen=True)
class SignedCrl:
    crl_pem: str
    crl_number: str
    #: The latest revocation the CRL holds (None: it lists nothing).
    through: datetime | None


class CrlSource(Protocol):
    async def fetch(self) -> SignedCrl: ...


class RolesAnywhere(Protocol):
    """The three Roles Anywhere calls the publisher makes."""

    async def list_crls(self) -> list[Mapping[str, Any]]: ...

    async def import_crl(
        self, *, name: str, crl_data: bytes, trust_anchor_arn: str, tags: Sequence[Mapping]
    ) -> str: ...

    async def update_crl(self, *, crl_id: str, crl_data: bytes) -> None: ...


@dataclass(frozen=True)
class Anchor:
    trust_anchor_arn: str
    client: RolesAnywhere
    crl_name: str = DEFAULT_CRL_NAME

    @property
    def anchor_id(self) -> str:
        m = _ANCHOR_ID.search(self.trust_anchor_arn)
        return m.group(1) if m else self.trust_anchor_arn


class HttpCrlSource:
    """`enrol`'s ``POST /internal/v0/crl``, with a token for the worker's identity."""

    def __init__(
        self, url: str, tokens: TokenProvider, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self.url = url
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=15.0, trust_env=False)

    async def fetch(self) -> SignedCrl:
        resp = await self.http.post(
            self.url, json={}, headers={"Authorization": f"Bearer {await self.tokens.token()}"}
        )
        if resp.status_code != 200:
            raise RuntimeError(f"enrol answered {resp.status_code} for the CRL")
        body = resp.json()
        through = body.get("through")
        return SignedCrl(
            crl_pem=str(body["crl_pem"]),
            crl_number=str(body["crl_number"]),
            through=datetime.fromisoformat(through.replace("Z", "+00:00")) if through else None,
        )


class BotoRolesAnywhere:
    """Roles Anywhere through aiobotocore, on a web-identity session
    (`backends.s3.credentials.session_for`)."""

    def __init__(self, session: Any, region: str, endpoint_url: str | None, config: Any) -> None:
        self._session = session
        self._region = region
        self._endpoint = endpoint_url
        self._config = config

    def _client(self):
        from aiobotocore.config import AioConfig

        from ..backends.s3.credentials import _tls_session_cls

        cfg = AioConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            http_session_cls=_tls_session_cls(self._config),
        )
        return self._session.create_client(
            "rolesanywhere",
            region_name=self._region,
            endpoint_url=self._endpoint,
            config=cfg,
            verify=self._config.ca_bundle or True,
        )

    async def list_crls(self) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        async with self._client() as c:
            token = None
            while True:
                kw = {"nextToken": token} if token else {}
                page = await c.list_crls(**kw)
                out += page.get("crls", [])
                token = page.get("nextToken")
                if not token:
                    return out

    async def import_crl(
        self, *, name: str, crl_data: bytes, trust_anchor_arn: str, tags: Sequence[Mapping]
    ) -> str:
        async with self._client() as c:
            resp = await c.import_crl(
                name=name,
                crlData=crl_data,
                trustAnchorArn=trust_anchor_arn,
                enabled=True,
                tags=list(tags),
            )
        return str(resp["crl"]["crlId"])

    async def update_crl(self, *, crl_id: str, crl_data: bytes) -> None:
        from botocore.exceptions import ClientError

        async with self._client() as c:
            try:
                await c.update_crl(crlId=crl_id, crlData=crl_data)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    raise CrlNotFound(crl_id) from None
                raise


#: Whether a revoked edge's unexpired Roles Anywhere certificate isn't in the
#: CRL an anchor was last given (revoked after its `published_through`).
PENDING_SQL = """
SELECT EXISTS (
  SELECT 1 FROM catalog.edge e JOIN catalog.edge_ra_certificate c USING (edge_id)
   WHERE e.state = 'revoked' AND c.not_after > %(now)s
     AND coalesce(e.revoked_at, %(now)s) > coalesce(%(through)s, '-infinity'::timestamptz))
"""


@dataclass
class CrlPublisher:
    anchors: list[Anchor]
    source: CrlSource
    refresh_every: timedelta = timedelta(days=7)
    backoff_base: timedelta = timedelta(seconds=30)
    backoff_max: timedelta = timedelta(minutes=15)
    #: Published CRLs, for tests: (trust anchor, crl_number, how).
    log: list[tuple[str, str, str]] = field(default_factory=list)

    def backoff(self, attempts: int) -> timedelta:
        return min(self.backoff_max, self.backoff_base * 2 ** max(0, attempts - 1))

    async def _state(self, conn: psycopg.AsyncConnection, arn: str) -> dict[str, Any]:
        row = await (
            await conn.execute(
                "SELECT crl_id, published_at, published_through, attempts, next_attempt_at,"
                "       crl_number"
                "  FROM catalog.roles_anywhere_crl WHERE trust_anchor_arn = %s",
                (arn,),
            )
        ).fetchone()
        names = ("crl_id", "published_at", "through", "attempts", "next_attempt_at", "crl_number")
        if row is None:
            return dict.fromkeys(names) | {"attempts": 0}
        return dict(zip(names, row, strict=True))

    async def _due(
        self, conn: psycopg.AsyncConnection, st: Mapping[str, Any], now: datetime, force: bool
    ) -> bool:
        if not force and st["next_attempt_at"] is not None and st["next_attempt_at"] > now:
            return False
        if st["crl_id"] is not None and (
            st["published_at"] is None or st["published_at"] <= now - self.refresh_every
        ):
            return True
        row = await (
            await conn.execute(PENDING_SQL, {"now": now, "through": st["through"]})
        ).fetchone()
        return bool(row and row[0])

    async def run(self, conn: psycopg.AsyncConnection, now: datetime, force: bool = False) -> int:
        """Publishes to every anchor that is due. Returns how many succeeded.

        Worker replicas run this at once (the queue and the sweep, on each).
        So each anchor is published by one of them at a time: its state is
        read, the CRL fetched, uploaded and recorded inside one transaction
        holding the anchor's advisory lock (a replica that can't take it
        leaves the anchor to the one that has it, and to the next sweep). The
        CRL is fetched after the lock is taken, so it is never older than what
        the previous holder uploaded; and a CRL whose number isn't above the
        last one uploaded is never uploaded (review on #32)."""
        ok = 0
        for a in self.anchors:
            async with conn.transaction():
                got = await (
                    await conn.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"roles_anywhere_crl:{a.trust_anchor_arn}",),
                    )
                ).fetchone()
                if not (got and got[0]):
                    log.info("CRL for %s: another worker is publishing it", a.trust_anchor_arn)
                    continue
                ok += await self._run_one(conn, a, now, force)
        return ok

    async def _run_one(
        self, conn: psycopg.AsyncConnection, a: Anchor, now: datetime, force: bool
    ) -> int:
        st = await self._state(conn, a.trust_anchor_arn)
        if not await self._due(conn, st, now, force):
            return 0
        try:
            crl = await self.source.fetch()
            entries = len(x509.load_pem_x509_crl(crl.crl_pem.encode()))
        except Exception as e:  # noqa: BLE001 - recorded, retried with backoff
            await self._failed(conn, a, st, now, f"CRL from enrol: {e}")
            return 0
        if entries == 0:
            # AWS would refuse it (module docstring). Nothing to upload is not a failure.
            log.warning(
                "CRL %s for %s revokes nothing: not uploaded (Roles Anywhere refuses one)",
                crl.crl_number,
                a.trust_anchor_arn,
            )
            await self._skipped(conn, a, now)
            return 0
        if st["crl_number"] is not None and int(crl.crl_number) <= int(st["crl_number"]):
            log.warning(
                "CRL %s for %s is not newer than the uploaded %s: not uploaded",
                crl.crl_number,
                a.trust_anchor_arn,
                st["crl_number"],
            )
            return 0
        try:
            crl_id, how = await self._publish(a, st["crl_id"], crl)
        except Exception as e:  # noqa: BLE001 - one anchor's failure is recorded, not raised
            await self._failed(conn, a, st, now, f"{type(e).__name__}: {e}")
            return 0
        await self._published(conn, a, crl_id, crl, now, how)
        return 1

    async def _publish(self, a: Anchor, crl_id: str | None, crl: SignedCrl) -> tuple[str, str]:
        data = crl.crl_pem.encode()
        if crl_id is not None:
            try:
                await a.client.update_crl(crl_id=crl_id, crl_data=data)
                return crl_id, "update"
            except CrlNotFound:
                log.warning("CRL %s of %s is gone: importing it again", crl_id, a.trust_anchor_arn)
        # An earlier import whose answer was lost: update that one, never a second.
        for c in await a.client.list_crls():
            if c.get("trustAnchorArn") == a.trust_anchor_arn and c.get("name") == a.crl_name:
                found = str(c["crlId"])
                await a.client.update_crl(crl_id=found, crl_data=data)
                return found, "update"
        new = await a.client.import_crl(
            name=a.crl_name,
            crl_data=data,
            trust_anchor_arn=a.trust_anchor_arn,
            tags=[{"key": TAG_KEY, "value": a.anchor_id}],
        )
        return new, "import"

    async def _published(
        self,
        conn: psycopg.AsyncConnection,
        a: Anchor,
        crl_id: str,
        crl: SignedCrl,
        now: datetime,
        how: str,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO catalog.roles_anywhere_crl AS r
                   (trust_anchor_arn, crl_id, crl_number, published_at, published_through)
            VALUES (%(a)s, %(id)s, %(n)s, %(now)s, %(t)s)
            ON CONFLICT (trust_anchor_arn) DO UPDATE
               SET crl_id = EXCLUDED.crl_id, crl_number = EXCLUDED.crl_number,
                   published_at = EXCLUDED.published_at,
                   -- What the CRL actually uploaded holds, never more.
                   published_through = EXCLUDED.published_through,
                   attempts = 0, failing_since = NULL, next_attempt_at = NULL, last_error = NULL
             -- Never record an older CRL over a newer one.
             WHERE r.crl_number IS NULL OR r.crl_number < EXCLUDED.crl_number
            """,
            {
                "a": a.trust_anchor_arn,
                "id": crl_id,
                "n": crl.crl_number,
                "now": now,
                "t": crl.through,
            },
        )
        await conn.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:worker', 'roles_anywhere.crl.publish', 'allow', %s::jsonb)",
            (
                json.dumps(
                    {
                        "trust_anchor_arn": a.trust_anchor_arn,
                        "crl_id": crl_id,
                        "crl_number": crl.crl_number,
                        "how": how,
                    }
                ),
            ),
        )
        self.log.append((a.trust_anchor_arn, crl.crl_number, how))
        log.info("CRL %s %sed to %s", crl.crl_number, how, a.trust_anchor_arn)

    async def _skipped(self, conn: psycopg.AsyncConnection, a: Anchor, now: datetime) -> None:
        """Nothing to upload. A skip is neither a failure nor a success: none is
        recorded, and a failure recorded before stays exactly as it is (the
        alert keeps reporting it until an upload succeeds). Only when to look
        again is set: the sweep asks `enrol` after the longest backoff, not
        every minute, and a revocation's own message still publishes at once
        (`force`)."""
        await conn.execute(
            """
            INSERT INTO catalog.roles_anywhere_crl AS r (trust_anchor_arn, next_attempt_at)
            VALUES (%(a)s, %(next)s)
            ON CONFLICT (trust_anchor_arn) DO UPDATE
               SET next_attempt_at = EXCLUDED.next_attempt_at
            """,
            {"a": a.trust_anchor_arn, "next": now + self.backoff_max},
        )

    async def _failed(
        self,
        conn: psycopg.AsyncConnection,
        a: Anchor,
        st: Mapping[str, Any],
        now: datetime,
        error: str,
    ) -> None:
        attempts = int(st["attempts"]) + 1
        log.warning("CRL upload to %s failed (attempt %d): %s", a.trust_anchor_arn, attempts, error)
        await conn.execute(
            """
            INSERT INTO catalog.roles_anywhere_crl AS r
                   (trust_anchor_arn, attempts, failing_since, next_attempt_at, last_error)
            VALUES (%(a)s, %(n)s, %(now)s, %(next)s, %(e)s)
            ON CONFLICT (trust_anchor_arn) DO UPDATE
               SET attempts = EXCLUDED.attempts,
                   failing_since = coalesce(r.failing_since, EXCLUDED.failing_since),
                   next_attempt_at = EXCLUDED.next_attempt_at, last_error = EXCLUDED.last_error
            """,
            {
                "a": a.trust_anchor_arn,
                "n": attempts,
                "now": now,
                "next": now + self.backoff(attempts),
                "e": error[:500],
            },
        )


def build_jobs(publisher: CrlPublisher, clock: Any) -> list[Job]:
    async def on_revoke(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await publisher.run(conn, clock.now(), force=True)

    async def sweep(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await publisher.run(conn, now)

    return [QueueJob(QUEUE, on_revoke), SweepJob(QUEUE, timedelta(minutes=1), sweep)]


def anchors_from(deployment: Any, credential: Any = None) -> list[Anchor]:
    """One `Anchor` per account with a trust anchor in the operator's settings."""
    from ..backends.s3.config import S3Config
    from ..backends.s3.credentials import entra_token_provider, session_for

    out: list[Anchor] = []
    for acct in deployment.accounts.values():
        arn = acct.roles_anywhere_trust_anchor_arn
        if not arn:
            continue
        config = S3Config.for_backend(
            {"account_id": acct.account_id, "region": acct.region}, deployment=deployment
        )
        token = None
        if config.credentials == "web_identity":
            assert config.web_identity_audience is not None
            token = entra_token_provider(credential, config.web_identity_audience)
        session = session_for(config, token_provider=token)
        client = BotoRolesAnywhere(
            session, acct.region, acct.endpoints.get("rolesanywhere"), config
        )
        out.append(Anchor(arn, client, acct.roles_anywhere_crl_name))
    return out


def publisher_from_env(
    env: Mapping[str, str] | None = None,
    *,
    anchors: Callable[[], list[Anchor]] | None = None,
) -> CrlPublisher | None:
    """The publisher, or None when no trust anchor is configured. The CRL
    comes from ``OMNIFILE_ENROL_CRL_URL`` with a token for the worker's
    managed identity (audience ``OMNIFILE_ENROL_AUDIENCE``), or, dev only,
    ``OMNIFILE_ENROL_DEV_TOKEN``."""
    from ..backends.s3.config import AwsDeployment

    e = dict(os.environ if env is None else env)
    found = anchors() if anchors is not None else anchors_from(AwsDeployment.from_env(e))
    if not found:
        return None
    url = e.get("OMNIFILE_ENROL_CRL_URL")
    if not url:
        raise ValueError(
            "a Roles Anywhere trust anchor is configured but OMNIFILE_ENROL_CRL_URL is not:"
            " revocations would never reach AWS"
        )
    tokens: TokenProvider
    if dev := e.get("OMNIFILE_ENROL_DEV_TOKEN"):
        if e.get("OMNIFILE_DEV_MODE") != "1":
            raise ValueError(
                "OMNIFILE_ENROL_DEV_TOKEN is a dev setting: it needs OMNIFILE_DEV_MODE=1"
            )
        tokens = StaticToken(dev)
    else:
        audience = e.get("OMNIFILE_ENROL_AUDIENCE")
        if not audience:
            raise ValueError("OMNIFILE_ENROL_AUDIENCE is needed for the CRL token")
        tokens = ManagedIdentityToken(audience, e.get("AZURE_CLIENT_ID") or None)
    return CrlPublisher(found, HttpCrlSource(url, tokens))


def crl_jobs(worker: Any) -> list[Job]:
    """Called by `runner._track_jobs`."""
    publisher = publisher_from_env()
    if publisher is None:
        log.info("no Roles Anywhere trust anchor configured: no CRL jobs")
        return []
    return build_jobs(publisher, worker.clock)


__all__ = [
    "Anchor",
    "BotoRolesAnywhere",
    "CrlNotFound",
    "CrlPublisher",
    "HttpCrlSource",
    "SignedCrl",
    "build_jobs",
    "crl_jobs",
    "publisher_from_env",
]
