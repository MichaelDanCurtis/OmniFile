"""The edge CA's CRL reaches Roles Anywhere when an edge is revoked
(decision 50; worker/crl_jobs.py). `enrol` signs the CRL (over ASGI, on its
own database here, so other tests' revocations don't interfere) and a fake
Roles Anywhere records ImportCrl, UpdateCrl and ListCrls: no AWS is called.
The fake refuses a CRL that revokes nothing, as Roles Anywhere does.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from botocore.exceptions import ClientError
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.api.db import Catalog
from omnifile_service.api.signclient import StaticToken
from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.app import EnrolServices, create_enrol_app
from omnifile_service.enrol.ca import FileCASigner, crl_builder
from omnifile_service.enrol.callers import SharedTokenVerifier
from omnifile_service.worker import crl_jobs
from omnifile_service.worker.crl_jobs import (
    Anchor,
    CrlNotFound,
    CrlPublisher,
    HttpCrlSource,
    SignedCrl,
    build_jobs,
)
from omnifile_service.worker.queue import Consumer

from .conftest import ENV, SIGN_TOKEN

CRL_TOKEN = "dev-crl-token-" + "w" * 16
ANCHOR = "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/{}"


@pytest.fixture(scope="module")
def fresh_db() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set")
    name = f"omnifile_crl_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(server)
    params["dbname"] = name
    url = make_conninfo(**params)
    migrate(url)
    yield url
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


#: What Roles Anywhere answers to a CRL that revokes nothing (real AWS,
#: 2026-11-14; a ValidationException each, these its whole messages). A CRL
#: whose only entry is a stale serial is accepted (UpdateCrl, run
#: rr-202611150718-62e9), and so it is here.
IMPORT_REVOKES_NOTHING = "At least one of the included CRLs doesn't include a revoked certificate."
UPDATE_REVOKES_NOTHING = "CRL does not revoke any cert"


class FakeRolesAnywhere:
    """ImportCrl / UpdateCrl / ListCrls, in memory. `fail` makes the next
    calls raise, as a throttled or unreachable endpoint would. A CRL that
    revokes nothing is refused on both, as Roles Anywhere refuses it."""

    def __init__(self) -> None:
        self.crls: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail = 0
        #: Set: the next UpdateCrl waits for it (a slow call), after setting `held`.
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if self.fail:
            self.fail -= 1
            raise RuntimeError(f"{op}: ThrottlingException")

    async def list_crls(self):
        self._maybe_fail("list")
        return [{k: v for k, v in c.items() if k != "data"} for c in self.crls.values()]

    @staticmethod
    def _must_revoke(op: str, crl_data: bytes, message: str) -> None:
        if len(x509.load_pem_x509_crl(crl_data)) == 0:
            raise ClientError({"Error": {"Code": "ValidationException", "Message": message}}, op)

    async def import_crl(self, *, name, crl_data, trust_anchor_arn, tags):
        self._maybe_fail("import")
        self._must_revoke("ImportCrl", crl_data, IMPORT_REVOKES_NOTHING)
        crl_id = uuid.uuid4().hex
        self.crls[crl_id] = {"crlId": crl_id, "name": name, "trustAnchorArn": trust_anchor_arn,
                             "tags": list(tags), "data": crl_data}  # fmt: skip
        return crl_id

    async def update_crl(self, *, crl_id, crl_data):
        self._maybe_fail("update")
        if crl_id not in self.crls:
            raise CrlNotFound(crl_id)
        self._must_revoke("UpdateCrl", crl_data, UPDATE_REVOKES_NOTHING)
        if self.hold is not None:
            gate, self.hold = self.hold, None
            self.held.set()
            await gate.wait()
        self.crls[crl_id]["data"] = crl_data

    def serials(self, crl_id: str) -> set[int]:
        crl = x509.load_pem_x509_crl(self.crls[crl_id]["data"])
        return {r.serial_number for r in crl}


def edge_with_ra_cert(sql: psycopg.Connection) -> tuple[int, int]:
    """An active edge and one unexpired Roles Anywhere certificate (serial)."""
    (edge_id,) = sql.execute(
        "INSERT INTO catalog.edge (name, state, cert_sha256, enrolled_at)"
        " VALUES (%s, 'active', %s, now()) RETURNING edge_id",
        (f"edge-{uuid.uuid4().hex[:8]}", secrets.token_bytes(32)),
    ).fetchone()
    serial = x509.random_serial_number()
    sql.execute(
        "INSERT INTO catalog.edge_ra_certificate (serial, edge_id, cert_sha256, key_sha256,"
        " not_after) VALUES (%s, %s, %s, %s, now() + interval '30 days')",
        (format(serial, "x"), edge_id, secrets.token_bytes(32), hashlib.sha256(b"k").digest()),
    )
    return edge_id, serial


def revoke(sql: psycopg.Connection, edge_id: int) -> None:
    sql.execute(
        "UPDATE catalog.edge SET state = 'revoked', revoked_at = now() WHERE edge_id = %s",
        (edge_id,),
    )
    sql.execute(
        "SELECT pgmq.send('roles_anywhere_crl', jsonb_build_object('edge_id', %s::text))",
        (edge_id,),
    )


def state(sql: psycopg.Connection, arn: str) -> dict:
    row = sql.execute(
        "SELECT crl_id, attempts, failing_since, next_attempt_at, last_error, published_at"
        "  FROM catalog.roles_anywhere_crl WHERE trust_anchor_arn = %s",
        (arn,),
    ).fetchone()
    keys = ("crl_id", "attempts", "failing_since", "next_attempt_at", "last_error", "published_at")
    return dict(zip(keys, row, strict=True)) if row else {}


def test_a_revocation_reaches_every_trust_anchor(fresh_db: str) -> None:
    ca = FileCASigner.generate("OmniFile CRL test CA")
    enrol = create_enrol_app(
        EnrolServices(
            catalog=Catalog(as_app(fresh_db, "enrol"), max_size=2),
            signer=ca,
            callers=SharedTokenVerifier(SIGN_TOKEN),
            crl_callers=SharedTokenVerifier(CRL_TOKEN, caller="test-worker"),
        )
    )
    a_arn, b_arn = ANCHOR.format(uuid.uuid4()), ANCHOR.format(uuid.uuid4())
    fake_a, fake_b = FakeRolesAnywhere(), FakeRolesAnywhere()

    async def go() -> None:
        async with enrol.router.lifespan_context(enrol):
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=enrol), base_url="https://enrol.test"
            )
            source = HttpCrlSource(
                "https://enrol.test/internal/v0/crl", StaticToken(CRL_TOKEN), http=http
            )
            pub = CrlPublisher([Anchor(a_arn, fake_a), Anchor(b_arn, fake_b)], source)
            await scenario(pub, http)
            await http.aclose()

    async def scenario(pub: CrlPublisher, http: httpx.AsyncClient) -> None:
        sql = psycopg.connect(fresh_db, autocommit=True)
        conn = await psycopg.AsyncConnection.connect(as_app(fresh_db, "worker"), autocommit=True)
        t0 = datetime.now(UTC)

        # Nothing revoked: nothing to publish, no AWS call.
        e1, s1 = edge_with_ra_cert(sql)
        assert await pub.run(conn, t0) == 0
        assert fake_a.calls == [] and fake_b.calls == []

        # A revocation: the queue message publishes at once. The first time
        # it is an ImportCrl (after ListCrls finds none), tagged with the anchor.
        revoke(sql, e1)

        async def connect() -> psycopg.AsyncConnection:
            return await psycopg.AsyncConnection.connect(as_app(fresh_db, "worker"))

        class Clock:
            def now(self) -> datetime:
                return t0

        [queue_job, sweep_job] = build_jobs(pub, Clock())
        consumer = Consumer(connect, crl_jobs.QUEUE, queue_job.handler)
        assert await consumer.drain() == 1
        await consumer.close()
        assert fake_a.calls == ["list", "import"] and fake_b.calls == ["list", "import"]
        [(a_id, a_crl)] = fake_a.crls.items()
        assert a_crl["trustAnchorArn"] == a_arn and a_crl["name"] == "omnifile-edge-ca"
        assert a_crl["tags"] == [{"key": "omnifile-trust-anchor", "value": a_arn.rsplit("/", 1)[1]}]
        assert s1 in fake_a.serials(a_id)
        assert x509.load_pem_x509_crl(a_crl["data"]).is_signature_valid(
            ca.ca_certificate.public_key()  # type: ignore[arg-type]
        )
        assert state(sql, a_arn)["crl_id"] == a_id
        assert (
            sql.execute(
                "SELECT count(*) FROM catalog.audit_log WHERE action = 'roles_anywhere.crl.publish'"
                " AND details->>'trust_anchor_arn' = %s",
                (a_arn,),
            ).fetchone()[0]
            == 1
        )

        # Published: the sweep has nothing to do.
        assert await sweep_job.fn(conn, t0) == 0

        # The next revocation, while anchor A keeps failing: B gets an
        # UpdateCrl; A backs off (30 s, then 60 s) and records why.
        e2, s2 = edge_with_ra_cert(sql)
        revoke(sql, e2)
        fake_a.fail = 2
        fake_a.calls.clear()
        fake_b.calls.clear()
        assert await pub.run(conn, t0, force=True) == 1
        assert fake_b.calls == ["update"] and {s1, s2} <= fake_b.serials(
            state(sql, b_arn)["crl_id"]
        )
        st = state(sql, a_arn)
        assert st["attempts"] == 1 and "ThrottlingException" in st["last_error"]
        assert st["failing_since"] is not None
        assert st["next_attempt_at"] == t0 + timedelta(seconds=30)
        assert s2 not in fake_a.serials(a_id)
        # In the backoff, the sweep waits; after it, it retries.
        assert await sweep_job.fn(conn, t0 + timedelta(seconds=10)) == 0
        assert fake_a.calls == ["update"]
        assert await sweep_job.fn(conn, t0 + timedelta(seconds=31)) == 0
        st2 = state(sql, a_arn)
        assert st2["attempts"] == 2 and st2["failing_since"] == st["failing_since"]
        assert st2["next_attempt_at"] == t0 + timedelta(seconds=91)
        # The alert's gauge: how long it has failed.
        rows = sql.execute(
            "SELECT extract(epoch FROM %s - failing_since) FROM catalog.roles_anywhere_crl"
            " WHERE trust_anchor_arn = %s",
            (t0 + timedelta(minutes=20), a_arn),
        ).fetchone()
        assert float(rows[0]) > 15 * 60
        assert await sweep_job.fn(conn, t0 + timedelta(seconds=92)) == 1
        assert {s1, s2} <= fake_a.serials(a_id) and len(fake_a.crls) == 1
        st3 = state(sql, a_arn)
        assert st3["attempts"] == 0 and st3["failing_since"] is None and st3["last_error"] is None

        # Weekly refresh, so the CRL's nextUpdate never passes.
        fake_a.calls.clear()
        assert await sweep_job.fn(conn, t0 + timedelta(days=6)) == 0
        assert await sweep_job.fn(conn, t0 + timedelta(days=8)) == 2
        assert fake_a.calls == ["update"]

        # The CRL id was lost (a response lost after ImportCrl): ListCrls finds
        # it, so there is never a second CRL at the anchor.
        sql.execute(
            "UPDATE catalog.roles_anywhere_crl SET crl_id = NULL WHERE trust_anchor_arn = %s",
            (a_arn,),
        )
        e3, s3 = edge_with_ra_cert(sql)
        revoke(sql, e3)
        fake_a.calls.clear()
        assert await pub.run(conn, t0 + timedelta(days=8), force=True) == 2
        assert fake_a.calls == ["list", "update"] and len(fake_a.crls) == 1
        assert s3 in fake_a.serials(a_id) and state(sql, a_arn)["crl_id"] == a_id
        # Someone deleted it at AWS: UpdateCrl fails, and it is imported again.
        fake_a.crls.clear()
        e4, _ = edge_with_ra_cert(sql)
        revoke(sql, e4)
        fake_a.calls.clear()
        assert await pub.run(conn, t0 + timedelta(days=8), force=True) == 2
        assert fake_a.calls == ["update", "list", "import"] and len(fake_a.crls) == 1

        await conn.close()
        sql.close()

    asyncio.run(go())


def test_the_publisher_needs_its_settings() -> None:
    fake = FakeRolesAnywhere()
    anchors = [Anchor(ANCHOR.format(uuid.uuid4()), fake)]
    assert crl_jobs.publisher_from_env({}, anchors=list) is None
    with pytest.raises(ValueError, match="OMNIFILE_ENROL_CRL_URL"):
        crl_jobs.publisher_from_env({}, anchors=lambda: anchors)
    env = {"OMNIFILE_ENROL_CRL_URL": "https://enrol.test/internal/v0/crl"}
    with pytest.raises(ValueError, match="OMNIFILE_ENROL_AUDIENCE"):
        crl_jobs.publisher_from_env(env, anchors=lambda: anchors)
    with pytest.raises(ValueError, match="dev setting"):
        crl_jobs.publisher_from_env(
            env | {"OMNIFILE_ENROL_DEV_TOKEN": CRL_TOKEN}, anchors=lambda: anchors
        )
    pub = crl_jobs.publisher_from_env(
        env | {"OMNIFILE_ENROL_DEV_TOKEN": CRL_TOKEN, "OMNIFILE_DEV_MODE": "1"},
        anchors=lambda: anchors,
    )
    assert pub is not None and pub.anchors == anchors
    assert [pub.backoff(n).total_seconds() for n in (1, 2, 3, 6, 10)] == [30, 60, 120, 900, 900]


def crl_number(pem: bytes) -> int:
    crl = x509.load_pem_x509_crl(pem)
    return crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number


def test_overlapping_publishers_never_leave_an_older_crl(fresh_db: str) -> None:
    """Review on #32 (P1): worker replicas publish at once. One fetches a
    CRL, is slow at UpdateCrl, and a revocation lands meanwhile: the CRL at
    AWS must end up holding it, and the state must say what AWS has. One
    publisher per anchor at a time; a CRL no newer than the last uploaded is
    never uploaded."""
    ca = FileCASigner.generate("OmniFile CRL race CA")
    enrol = create_enrol_app(
        EnrolServices(
            catalog=Catalog(as_app(fresh_db, "enrol"), max_size=2),
            signer=ca,
            callers=SharedTokenVerifier(SIGN_TOKEN),
            crl_callers=SharedTokenVerifier(CRL_TOKEN, caller="test-worker"),
        )
    )
    arn = ANCHOR.format(uuid.uuid4())
    fake = FakeRolesAnywhere()

    async def go() -> None:
        async with enrol.router.lifespan_context(enrol):
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=enrol), base_url="https://enrol.test"
            )
            source = HttpCrlSource(
                "https://enrol.test/internal/v0/crl", StaticToken(CRL_TOKEN), http=http
            )
            # Two worker replicas, each with its own connection.
            one = CrlPublisher([Anchor(arn, fake)], source)
            two = CrlPublisher([Anchor(arn, fake)], source)
            sql = psycopg.connect(fresh_db, autocommit=True)
            c1 = await psycopg.AsyncConnection.connect(as_app(fresh_db, "worker"), autocommit=True)
            c2 = await psycopg.AsyncConnection.connect(as_app(fresh_db, "worker"), autocommit=True)
            now = datetime.now(UTC)
            e0, _ = edge_with_ra_cert(sql)
            revoke(sql, e0)
            assert await one.run(c1, now, force=True) == 1  # the CRL exists at AWS
            [crl_id] = fake.crls

            # Replica one fetches a CRL and is slow to upload it.
            e1, s1 = edge_with_ra_cert(sql)
            revoke(sql, e1)
            gate = asyncio.Event()
            fake.hold = gate
            fake.held.clear()
            slow = asyncio.create_task(one.run(c1, now, force=True))
            await asyncio.wait_for(fake.held.wait(), 10)
            # A revocation lands; replica two publishes while one is still at it.
            e2, s2 = edge_with_ra_cert(sql)
            revoke(sql, e2)
            await asyncio.wait_for(two.run(c2, now, force=True), 10)
            # The slow upload finishes last.
            gate.set()
            await asyncio.wait_for(slow, 10)
            # Whatever ran, a sweep catches up, and AWS has every revocation.
            await two.run(c2, now + timedelta(seconds=1))
            await one.run(c1, now + timedelta(seconds=2))
            assert len(fake.crls) == 1
            assert {s1, s2} <= fake.serials(crl_id), "an older CRL was uploaded last"
            st = sql.execute(
                "SELECT crl_number FROM catalog.roles_anywhere_crl WHERE trust_anchor_arn = %s",
                (arn,),
            ).fetchone()
            assert int(st[0]) == crl_number(fake.crls[crl_id]["data"])

            # A CRL no newer than the uploaded one is never uploaded.
            sql.execute(
                "UPDATE catalog.roles_anywhere_crl SET crl_number = 10^30,"
                " published_through = NULL WHERE trust_anchor_arn = %s",
                (arn,),
            )
            fake.calls.clear()
            assert await one.run(c1, now + timedelta(seconds=3), force=True) == 0
            assert fake.calls == []
            for c in (c1, c2):
                await c.close()
            sql.close()
            await http.aclose()

    asyncio.run(go())


def expire(sql: psycopg.Connection, *serials: int) -> None:
    """The certificates ran out (30 days): each a day further back than the last."""
    for days, serial in enumerate(serials, start=1):
        sql.execute(
            "UPDATE catalog.edge_ra_certificate SET not_after = now() - make_interval(days => %s)"
            " WHERE serial = %s",
            (days, format(serial, "x")),
        )


def test_the_crl_never_goes_back_to_empty_once_an_edge_was_revoked(fresh_db: str) -> None:
    """Real AWS, 2026-11-14: Roles Anywhere refuses a CRL that revokes
    nothing, on ImportCrl and on UpdateCrl. The CRL listed only unexpired
    certificates, so once every revoked edge's had expired, each weekly
    UpdateCrl would be refused, the publisher would fail for good and the
    published CRL's nextUpdate would pass. Now the CRL keeps one serial, the
    most recently revoked edge's last certificate, and the refresh goes on."""
    ca = FileCASigner.generate("OmniFile CRL keep CA")
    enrol = create_enrol_app(
        EnrolServices(
            catalog=Catalog(as_app(fresh_db, "enrol"), max_size=2),
            signer=ca,
            callers=SharedTokenVerifier(SIGN_TOKEN),
            crl_callers=SharedTokenVerifier(CRL_TOKEN, caller="test-worker"),
        )
    )
    arn = ANCHOR.format(uuid.uuid4())
    fake = FakeRolesAnywhere()

    async def go() -> None:
        async with enrol.router.lifespan_context(enrol):
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=enrol), base_url="https://enrol.test"
            )
            source = HttpCrlSource(
                "https://enrol.test/internal/v0/crl", StaticToken(CRL_TOKEN), http=http
            )
            pub = CrlPublisher([Anchor(arn, fake)], source)
            sql = psycopg.connect(fresh_db, autocommit=True)
            conn = await psycopg.AsyncConnection.connect(
                as_app(fresh_db, "worker"), autocommit=True
            )
            t0 = datetime.now(UTC)
            # This module's own database: what the tests above revoked has expired.
            sql.execute(
                "UPDATE catalog.edge_ra_certificate SET not_after = now() - interval '60 days'"
            )

            # An edge revoked earlier, and one revoked now that has renewed once.
            e0, s0 = edge_with_ra_cert(sql)
            revoke(sql, e0)
            sql.execute(
                "UPDATE catalog.edge SET revoked_at = now() - interval '45 days'"
                " WHERE edge_id = %s",
                (e0,),
            )
            e1, s1_old = edge_with_ra_cert(sql)
            s1 = x509.random_serial_number()
            sql.execute(
                "INSERT INTO catalog.edge_ra_certificate (serial, edge_id, cert_sha256,"
                " key_sha256, not_after) VALUES (%s, %s, %s, %s, now() + interval '30 days')",
                (format(s1, "x"), e1, secrets.token_bytes(32), hashlib.sha256(b"k").digest()),
            )
            revoke(sql, e1)
            assert await pub.run(conn, t0, force=True) == 1
            [crl_id] = fake.crls
            assert fake.calls == ["list", "import"]
            # While any is unexpired, the CRL is the unexpired ones and nothing else.
            assert fake.serials(crl_id) == {s0, s1_old, s1}

            # Every revoked edge's certificates expire. The weekly refresh still
            # uploads: one serial, the last certificate of the edge revoked last.
            expire(sql, s1, s1_old, s0)
            fake.calls.clear()
            assert await pub.run(conn, t0 + timedelta(days=8)) == 1
            assert fake.calls == ["update"]
            assert fake.serials(crl_id) == {s1}
            st = state(sql, arn)
            assert st["attempts"] == 0 and st["failing_since"] is None
            assert st["last_error"] is None and st["published_at"] == t0 + timedelta(days=8)
            # And the week after, and the one after that: it never fails.
            for days in (16, 24):
                assert await pub.run(conn, t0 + timedelta(days=days)) == 1
                assert fake.serials(crl_id) == {s1}

            # An edge revoked later that had only its mTLS certificate, expired
            # too: its serial is the one kept. One entry, never every expired one.
            mtls = x509.random_serial_number()
            (e2,) = sql.execute(
                "INSERT INTO catalog.edge (name, state, cert_sha256, cert_serial,"
                " cert_not_after, enrolled_at) VALUES (%s, 'active', %s, %s,"
                " now() - interval '1 hour', now() - interval '31 days') RETURNING edge_id",
                (f"edge-{uuid.uuid4().hex[:8]}", secrets.token_bytes(32), format(mtls, "x")),
            ).fetchone()
            revoke(sql, e2)
            assert await pub.run(conn, t0 + timedelta(days=32)) == 1
            assert fake.serials(crl_id) == {mtls}

            # A new revocation with a live certificate: the kept serial gives way.
            # (Issued on the publisher's day 32, so it outlives that clock.)
            e3, s3 = edge_with_ra_cert(sql)
            sql.execute(
                "UPDATE catalog.edge_ra_certificate SET not_after = now() + interval '62 days'"
                " WHERE serial = %s",
                (format(s3, "x"),),
            )
            revoke(sql, e3)
            assert await pub.run(conn, t0 + timedelta(days=32), force=True) == 1
            assert fake.serials(crl_id) == {s3}
            assert len(fake.crls) == 1 and state(sql, arn)["failing_since"] is None

            await conn.close()
            sql.close()
            await http.aclose()

    asyncio.run(go())


class EmptyCrl:
    """An `enrol` that answers a CRL with no entries."""

    def __init__(self) -> None:
        self.ca = FileCASigner.generate("OmniFile CRL empty CA")
        self.fetched = 0

    async def fetch(self) -> SignedCrl:
        self.fetched += 1
        now = datetime.now(UTC)
        number = int(now.timestamp() * 1_000_000)
        signed = await self.ca.sign_crl(crl_builder(self.ca.ca_certificate, [], now, number))
        pem = signed.public_bytes(serialization.Encoding.PEM).decode()
        return SignedCrl(pem, str(number), None)


def test_a_crl_that_revokes_nothing_is_never_uploaded(fresh_db: str) -> None:
    """The worker's own guard, whatever `enrol` answers (an older replica
    during a rollout): Roles Anywhere would refuse the CRL, so it is skipped
    and logged, never uploaded and never counted as a failure."""

    async def go() -> None:
        sql = psycopg.connect(fresh_db, autocommit=True)
        conn = await psycopg.AsyncConnection.connect(as_app(fresh_db, "worker"), autocommit=True)
        now = datetime.now(UTC)
        empty = (await EmptyCrl().fetch()).crl_pem.encode()

        # The fake is as strict as Roles Anywhere, with its messages.
        fake = FakeRolesAnywhere()
        with pytest.raises(ClientError, match="doesn't include a revoked certificate"):
            await fake.import_crl(name="n", crl_data=empty, trust_anchor_arn="a", tags=[])
        fake.crls["c"] = {"crlId": "c", "name": "n", "trustAnchorArn": "a", "data": b""}
        with pytest.raises(ClientError) as refused:
            await fake.update_crl(crl_id="c", crl_data=empty)
        assert str(refused.value) == (
            "An error occurred (ValidationException) when calling the UpdateCrl operation:"
            " CRL does not revoke any cert"
        )

        # A weekly refresh of a published CRL: nothing is sent, nothing fails,
        # and the sweep doesn't ask again every minute.
        arn = ANCHOR.format(uuid.uuid4())
        fake, source = FakeRolesAnywhere(), EmptyCrl()
        fake.crls["was"] = {"crlId": "was", "name": "omnifile-edge-ca", "trustAnchorArn": arn,
                            "data": b"the CRL AWS has"}  # fmt: skip
        sql.execute(
            "INSERT INTO catalog.roles_anywhere_crl (trust_anchor_arn, crl_id, crl_number,"
            " published_at) VALUES (%s, 'was', 1, %s)",
            (arn, now - timedelta(days=8)),
        )
        pub = CrlPublisher([Anchor(arn, fake)], source)
        assert await pub.run(conn, now) == 0
        assert source.fetched == 1 and fake.calls == []
        assert fake.crls["was"]["data"] == b"the CRL AWS has"
        st = state(sql, arn)
        assert st["attempts"] == 0 and st["failing_since"] is None and st["last_error"] is None
        assert st["crl_id"] == "was" and st["published_at"] == now - timedelta(days=8)
        assert st["next_attempt_at"] == now + timedelta(minutes=15)
        assert await pub.run(conn, now + timedelta(minutes=1)) == 0
        assert source.fetched == 1
        assert await pub.run(conn, now + timedelta(minutes=16)) == 0
        assert source.fetched == 2 and fake.calls == []
        assert sql.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'roles_anywhere.crl.publish'"
            " AND details->>'trust_anchor_arn' = %s",
            (arn,),
        ).fetchone() == (0,)

        # A first import: a revocation is pending, yet the CRL revokes nothing.
        arn2 = ANCHOR.format(uuid.uuid4())
        fake2, source2 = FakeRolesAnywhere(), EmptyCrl()
        edge_id, _ = edge_with_ra_cert(sql)
        revoke(sql, edge_id)
        pub2 = CrlPublisher([Anchor(arn2, fake2)], source2)
        assert await pub2.run(conn, now, force=True) == 0
        assert source2.fetched == 1 and fake2.calls == [] and fake2.crls == {}
        st = state(sql, arn2)
        assert st["crl_id"] is None and st["attempts"] == 0 and st["failing_since"] is None

        await conn.close()
        sql.close()

    asyncio.run(go())


def test_a_skipped_crl_leaves_a_recorded_failure_as_it_was(fresh_db: str) -> None:
    """A skip is not a success (review of #102): an upload that was failing
    before a CRL with no entries arrived is still on record after it, so the
    gauge and `roles-anywhere-crl-failing` keep reporting it. The skip only
    sets when to look again."""

    async def go() -> None:
        sql = psycopg.connect(fresh_db, autocommit=True)
        conn = await psycopg.AsyncConnection.connect(as_app(fresh_db, "worker"), autocommit=True)
        now = datetime.now(UTC)
        arn = ANCHOR.format(uuid.uuid4())
        fake, source = FakeRolesAnywhere(), EmptyCrl()
        since = now - timedelta(hours=1)
        sql.execute(
            "INSERT INTO catalog.roles_anywhere_crl (trust_anchor_arn, crl_id, crl_number,"
            " published_at, attempts, failing_since, next_attempt_at, last_error)"
            " VALUES (%s, 'was', 1, %s, 3, %s, %s, 'RuntimeError: update: ThrottlingException')",
            (arn, now - timedelta(days=8), since, now - timedelta(seconds=1)),
        )
        pub = CrlPublisher([Anchor(arn, fake)], source)
        assert await pub.run(conn, now) == 0
        assert source.fetched == 1 and fake.calls == []
        st = state(sql, arn)
        assert st["attempts"] == 3 and st["failing_since"] == since
        assert st["last_error"] == "RuntimeError: update: ThrottlingException"
        assert st["next_attempt_at"] == now + timedelta(minutes=15)
        # What the alert reads (settings/alerts.py) still finds the anchor.
        assert sql.execute(
            "SELECT count(*) FROM catalog.roles_anywhere_crl WHERE trust_anchor_arn = %s"
            " AND failing_since IS NOT NULL AND failing_since < %s",
            (arn, now - timedelta(minutes=15)),
        ).fetchone() == (1,)
        # A second skip changes nothing but when to look again.
        later = now + timedelta(minutes=16)
        assert await pub.run(conn, later) == 0
        st = state(sql, arn)
        assert source.fetched == 2 and st["attempts"] == 3 and st["failing_since"] == since
        assert st["next_attempt_at"] == later + timedelta(minutes=15)
        await conn.close()
        sql.close()

    asyncio.run(go())
