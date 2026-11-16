"""Container-level immutability on Azure through the management SDK
(retention/policy_admin.py `AzureContainerPolicy`), against a fake of the
SDK's `blob_containers` operations that keeps ARM's rules (ETags on every
change after the first, lock and extend only, no delete once locked) and
records each call with the exact parameters it was sent."""

from __future__ import annotations

import asyncio
import itertools
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.mgmt.storage.models import ImmutabilityPolicy, ImmutabilityPolicyProperty
from psycopg.types.json import Jsonb
from watched import look

from omnifile_service.backends.base import BackendError, Immutable, UnsupportedOperation
from omnifile_service.retention import cancel_worm_test, enable_worm, extend_worm_retention
from omnifile_service.retention.policy_admin import (
    AzureContainerPolicy,
    ManagementConfig,
    policy_admin_for,
)
from omnifile_service.retention.worm import lock_due_shares
from omnifile_service.retention.worm_sync import sync_share_policy
from omnifile_service.worker.queue import Retry

from .support import clock_days_ago, seed, share, with_conn

H = timedelta(hours=1)
CONFIG = ManagementConfig("sub-1", "rg-omnifile", {"wormacct": "rg-worm"})
WHERE = {"resource_group_name": "rg-worm", "account_name": "wormacct", "container_name": "data"}


class FakeBlobContainers:
    """`StorageManagementClient(...).blob_containers`, the immutability
    policy calls only, with ARM's rules."""

    def __init__(self) -> None:
        self.policies: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._etags = (f'"0x8D{n:010X}"' for n in itertools.count(1))
        self.fail_next: int | None = None

    def _record(self, name: str, **kw: Any) -> tuple[str, str, str]:
        sent = dict(kw)
        if sent.get("parameters") is not None:
            sent["parameters"] = dict(sent["parameters"])  # the JSON body sent to ARM
        self.calls.append((name, sent))
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            e = HttpResponseError(message=f"injected {status}")
            e.status_code = status
            raise e
        return (kw["resource_group_name"], kw["account_name"], kw["container_name"])

    def _model(self, p: dict[str, Any]) -> ImmutabilityPolicy:
        m = ImmutabilityPolicy({
            "etag": p["etag"],
            "properties": {"immutabilityPeriodSinceCreationInDays": p["days"],
                           "state": p["state"]},
        })  # fmt: skip
        return m

    def _check_etag(self, p: dict[str, Any] | None, etag: Any, cond: Any, *, required: bool):
        if required and (etag is None or cond is not MatchConditions.IfNotModified):
            raise HttpResponseError(message="If-Match is required")
        if etag is not None and (p is None or etag != p["etag"]):
            e = HttpResponseError(message="precondition failed")
            e.status_code = 412
            raise e

    @staticmethod
    def _days_of(params: Any) -> int:
        return int(dict(params)["properties"]["immutabilityPeriodSinceCreationInDays"])

    async def get_immutability_policy(self, **kw: Any) -> ImmutabilityPolicy:
        key = self._record("get_immutability_policy", **kw)
        p = self.policies.get(key)
        if p is None:
            raise ResourceNotFoundError("ContainerImmutabilityPolicyNotFound")
        return self._model(p)

    async def create_or_update_immutability_policy(self, **kw: Any) -> ImmutabilityPolicy:
        key = self._record("create_or_update_immutability_policy", **kw)
        p = self.policies.get(key)
        if p is not None and p["state"] == "Locked":
            raise HttpResponseError(message="a locked policy can only be extended")
        self._check_etag(p, kw.get("etag"), kw.get("match_condition"), required=False)
        self.policies[key] = {"days": self._days_of(kw["parameters"]), "state": "Unlocked",
                              "etag": next(self._etags)}  # fmt: skip
        return self._model(self.policies[key])

    async def lock_immutability_policy(self, **kw: Any) -> ImmutabilityPolicy:
        key = self._record("lock_immutability_policy", **kw)
        p = self.policies.get(key)
        self._check_etag(p, kw.get("etag"), kw.get("match_condition"), required=True)
        assert p is not None
        p.update(state="Locked", etag=next(self._etags))
        return self._model(p)

    async def extend_immutability_policy(self, **kw: Any) -> ImmutabilityPolicy:
        key = self._record("extend_immutability_policy", **kw)
        p = self.policies.get(key)
        self._check_etag(p, kw.get("etag"), kw.get("match_condition"), required=True)
        assert p is not None
        if p["state"] != "Locked" or self._days_of(kw["parameters"]) < p["days"]:
            raise HttpResponseError(message="extend: locked policies only, and only longer")
        p.update(days=self._days_of(kw["parameters"]), etag=next(self._etags))
        return self._model(p)

    async def delete_immutability_policy(self, **kw: Any) -> ImmutabilityPolicy:
        key = self._record("delete_immutability_policy", **kw)
        p = self.policies.get(key)
        self._check_etag(p, kw.get("etag"), kw.get("match_condition"), required=True)
        assert p is not None
        if p["state"] == "Locked":
            raise HttpResponseError(message="a locked policy can't be deleted")
        del self.policies[key]
        return self._model({**p, "days": 0})


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def body(days: int) -> dict:
    return {"properties": {"immutabilityPeriodSinceCreationInDays": days}}


def get() -> tuple[str, dict]:
    return ("get_immutability_policy", dict(WHERE))


def iff(etag: str) -> dict:
    return {"etag": etag, "match_condition": MatchConditions.IfNotModified}


@pytest.fixture
def fake() -> FakeBlobContainers:
    return FakeBlobContainers()


@pytest.fixture
def admin(fake: FakeBlobContainers) -> AzureContainerPolicy:
    return AzureContainerPolicy(fake, CONFIG)


def test_the_policy_lifecycle_makes_exactly_these_arm_calls(fake, admin) -> None:  # type: ignore[no-untyped-def]
    c = "wormacct/data"
    run(admin.set_unlocked(c, 30))  # enable: create unlocked, unconditionally
    run(admin.set_unlocked(c, 30))  # unchanged: read only
    run(admin.set_unlocked(c, 45))  # changed in the window: conditional update
    run(admin.lock(c, 45))  # window over: lock, on the policy's ETag
    run(admin.extend(c, 45))  # nothing to do
    run(admin.extend(c, 60))  # retention grew
    assert fake.calls == [
        get(),
        ("create_or_update_immutability_policy", {**WHERE, "parameters": body(30)}),
        get(),
        get(),
        ("create_or_update_immutability_policy",
         {**WHERE, "parameters": body(45), **iff('"0x8D0000000001"')}),
        get(),
        ("lock_immutability_policy", {**WHERE, **iff('"0x8D0000000002"')}),
        get(),
        get(),
        ("extend_immutability_policy",
         {**WHERE, "parameters": body(60), **iff('"0x8D0000000003"')}),
    ]  # fmt: skip
    assert fake.policies[("rg-worm", "wormacct", "data")]["days"] == 60
    assert fake.policies[("rg-worm", "wormacct", "data")]["state"] == "Locked"


def test_lock_creates_the_policy_first_and_sets_its_days(fake, admin) -> None:  # type: ignore[no-untyped-def]
    run(admin.lock("live/data", 7))  # default resource group
    where = {"resource_group_name": "rg-omnifile", "account_name": "live",
             "container_name": "data"}  # fmt: skip
    assert fake.calls == [
        ("get_immutability_policy", where),
        ("create_or_update_immutability_policy", {**where, "parameters": body(7)}),
        ("lock_immutability_policy", {**where, **iff('"0x8D0000000001"')}),
    ]
    fake.calls.clear()
    run(admin.lock("live/data", 9))  # already locked: an extension
    assert [n for n, _ in fake.calls] == ["get_immutability_policy",
                                          "extend_immutability_policy"]  # fmt: skip


def test_a_locked_policy_is_never_shortened_unlocked_or_deleted(fake, admin) -> None:  # type: ignore[no-untyped-def]
    c = "wormacct/data"
    run(admin.lock(c, 30))
    fake.calls.clear()
    for attempt in (admin.extend(c, 29), admin.lock(c, 1), admin.set_unlocked(c, 30),
                    admin.delete_unlocked(c)):  # fmt: skip
        with pytest.raises(Immutable):
            run(attempt)
    # Each was refused after reading the policy, before any change was sent.
    assert {n for n, _ in fake.calls} == {"get_immutability_policy"}


def test_an_unlocked_policy_is_deleted_on_its_etag(fake, admin) -> None:  # type: ignore[no-untyped-def]
    c = "wormacct/data"
    run(admin.delete_unlocked(c))  # none: nothing to do
    run(admin.set_unlocked(c, 3))
    run(admin.delete_unlocked(c))
    assert fake.calls[-1] == ("delete_immutability_policy", {**WHERE, **iff('"0x8D0000000001"')})
    assert fake.policies == {}


def test_a_policy_changed_meanwhile_fails_the_call(fake, admin) -> None:  # type: ignore[no-untyped-def]
    run(admin.set_unlocked("wormacct/data", 3))
    real_get = fake.get_immutability_policy

    async def stale_get(**kw: Any) -> ImmutabilityPolicy:
        p = await real_get(**kw)
        # Someone else changes it between our read and our write.
        fake.policies[("rg-worm", "wormacct", "data")]["etag"] = '"0x8DFFFFFFFFFF"'
        return p

    fake.get_immutability_policy = stale_get  # type: ignore[method-assign]
    with pytest.raises(BackendError, match="changed meanwhile"):
        run(admin.lock("wormacct/data", 3))
    fake.get_immutability_policy = real_get  # type: ignore[method-assign]
    fake.fail_next = 503
    with pytest.raises(BackendError, match="503"):
        run(admin.lock("wormacct/data", 3))


def test_a_container_with_no_policy_may_answer_zero_days(fake, admin) -> None:  # type: ignore[no-untyped-def]
    """VERIFY on real Azure: a container with no policy may answer Get with a
    zero-day policy rather than a 404; both are read as none."""

    async def zero(**kw: Any) -> ImmutabilityPolicy:
        fake.calls.append(("get_immutability_policy", kw))
        return ImmutabilityPolicy({"etag": '"x"', "properties": {
            "immutabilityPeriodSinceCreationInDays": 0, "state": "Unlocked"}})  # fmt: skip

    fake.get_immutability_policy = zero  # type: ignore[method-assign]
    run(admin.delete_unlocked("wormacct/data"))
    run(admin.set_unlocked("wormacct/data", 5))
    assert fake.calls[-1] == ("create_or_update_immutability_policy",
                              {**WHERE, "parameters": body(5)})  # fmt: skip


def test_the_sdk_models_serialise_as_the_arm_body() -> None:
    """What `_days` sends is ARM's JSON body (the fake reads it the same way)."""
    from omnifile_service.retention.policy_admin import _days

    assert dict(_days(12)) == body(12)
    p = ImmutabilityPolicy(properties=ImmutabilityPolicyProperty(
        immutability_period_since_creation_in_days=12))  # fmt: skip
    assert dict(p) == body(12)


def test_an_azure_binding_needs_its_management_settings() -> None:
    class AzureLike:
        from omnifile_service.backends.base import BackendKind

        kind = BackendKind.AZURE_BLOB

    with pytest.raises(UnsupportedOperation, match="management"):
        policy_admin_for(AzureLike(), {"layout": "single_account"})  # type: ignore[arg-type]
    with pytest.raises(UnsupportedOperation, match="management"):
        ManagementConfig.from_mapping({"subscription_id": "s"})
    m = ManagementConfig.from_mapping({"subscription_id": "s", "resource_group": "g",
                                       "account_groups": {"a": "h"}})  # fmt: skip
    assert (m.group_of("a"), m.group_of("b")) == ("h", "g")


# --- the retention queue drives it ----------------------------------------------------


def test_the_retention_worker_keeps_the_azure_policy_in_line_with_the_share(
    db: psycopg.Connection, connect, fake
) -> None:
    """enable -> unlocked; the window ends -> locked; retention grows ->
    extended; through `sync_share_policy` as the `retention` queue runs it."""
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    # Test setup: the seeded backend is repointed before any file exists (see below).
    db.execute("SET session_replication_role = replica")
    db.execute(
        "UPDATE catalog.backend SET config = config || %s WHERE backend_id = %s",
        (Jsonb({"container": "wormacct/data",
                "management": {"subscription_id": "sub-1", "resource_group": "rg-omnifile",
                               "account_groups": {"wormacct": "rg-worm"}}}),
         w.backend_id),
    )  # fmt: skip
    db.execute("RESET session_replication_role")
    made: list[dict] = []

    def admin_for(backend, config):  # type: ignore[no-untyped-def]
        made.append(dict(config))
        return AzureContainerPolicy(fake, ManagementConfig.from_mapping(config["management"]))

    def sync() -> str:
        return with_conn(connect, lambda c: sync_share_policy(
            c, w.registry, w.share_id, clock.now(), admin_for=admin_for))  # fmt: skip

    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=30,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip
    assert sync() == "unlocked"
    assert fake.policies[("rg-worm", "wormacct", "data")] == {
        "days": 30, "state": "Unlocked", "etag": '"0x8D0000000001"'}  # fmt: skip
    assert made[0]["management"]["resource_group"] == "rg-omnifile"
    clock.advance(2 * H)
    with_conn(connect, lambda c: lock_due_shares(c, clock.now()))
    assert sync() == "locked"
    assert fake.policies[("rg-worm", "wormacct", "data")]["state"] == "Locked"
    with_conn(connect, lambda c: extend_worm_retention(c, w.share_id, 40, actor="user:1"))
    assert sync() == "locked"
    assert fake.policies[("rg-worm", "wormacct", "data")]["days"] == 40
    assert [n for n, _ in fake.calls].count("extend_immutability_policy") == 1
    s = share(db, w.share_id)
    assert (s["worm_policy_state"], s["worm_policy_days"]) == ("locked", 40)
    # In step: nothing more is sent.
    n = len(fake.calls)
    assert sync() == "locked" and len(fake.calls) == n


def test_a_cancelled_window_deletes_the_unlocked_policy(db, connect, fake) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    cfg = {"subscription_id": "s", "resource_group": "rg"}
    # Test setup: the seeded backend is repointed before any file exists. The catalog's
    # backend_guard (0035) refuses moving a backend a dataset uses, so it is set aside here.
    db.execute("SET session_replication_role = replica")
    db.execute(
        "UPDATE catalog.backend SET config = config || %s WHERE backend_id = %s",
        (Jsonb({"container": "wormacct/data", "management": cfg}), w.backend_id),
    )
    db.execute("RESET session_replication_role")

    def admin_for(backend, config):  # type: ignore[no-untyped-def]
        return AzureContainerPolicy(fake, ManagementConfig.from_mapping(config["management"]))

    def sync() -> str:
        return with_conn(connect, lambda c: sync_share_policy(
            c, w.registry, w.share_id, clock.now(), admin_for=admin_for))  # fmt: skip

    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=5,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip
    assert sync() == "unlocked"
    with_conn(connect, lambda c: cancel_worm_test(c, w.share_id, actor="user:1",
                                                  now=clock.now()))  # fmt: skip
    assert sync() == "none"
    assert fake.policies == {} and fake.calls[-1][0] == "delete_immutability_policy"


def test_without_management_settings_the_worker_retries_hourly(db, connect) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=5,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip

    def admin_for(backend, config):  # type: ignore[no-untyped-def]
        return policy_admin_for(type("Az", (), {"kind": __import__(
            "omnifile_service.backends.base", fromlist=["BackendKind"]).BackendKind.AZURE_BLOB
        })(), config)  # fmt: skip

    with pytest.raises(Retry) as e:
        with_conn(connect, lambda c: sync_share_policy(c, w.registry, w.share_id, clock.now(),
                                                       admin_for=admin_for))  # fmt: skip
    assert e.value.delay_seconds == 3600
    assert share(db, w.share_id)["worm_policy_state"] == "none"


def test_a_share_policy_sync_holds_no_id_across_its_call(
    db: psycopg.Connection, connect, retention_db_url: str
) -> None:
    """By reading on main: its row lock did. And what it records is what it did."""
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    seen = []

    class Admin:
        async def set_unlocked(self, container, days):  # type: ignore[no-untyped-def]
            seen.append(look(retention_db_url, "set_unlocked"))

    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=30,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip
    state = with_conn(connect, lambda c: sync_share_policy(
        c, w.registry, w.share_id, clock.now(), admin_for=lambda *_: Admin()))  # fmt: skip
    assert state == "unlocked"
    assert [s.ids for s in seen] == [0]


def test_a_share_changed_during_the_policy_call_isnt_recorded_as_done(
    db: psycopg.Connection, connect, retention_db_url: str
) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    made = []

    class Admin:
        async def set_unlocked(self, container, days):  # type: ignore[no-untyped-def]
            made.append(days)
            with psycopg.connect(retention_db_url, autocommit=True) as c, c.transaction():
                c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                c.execute(
                    "UPDATE catalog.share SET worm_retention_days = worm_retention_days + 5"
                    " WHERE share_id = %s",
                    (w.share_id,),
                )

    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=30,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip
    with pytest.raises(Retry):
        with_conn(connect, lambda c: sync_share_policy(
            c, w.registry, w.share_id, clock.now(), admin_for=lambda *_: Admin()))  # fmt: skip
    assert made == [30]
    (state, days) = db.execute(
        "SELECT worm_policy_state, worm_policy_days FROM catalog.share WHERE share_id = %s",
        (w.share_id,),
    ).fetchone()
    assert (state, days) == ("none", None)
