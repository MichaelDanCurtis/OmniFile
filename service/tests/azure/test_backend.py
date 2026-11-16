"""The Azure backend's request shapes and error mapping, over the SDK with a
fake transport (no network). See fake_azure.py."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from azure.storage.extensions import checksums
from azure_fakes import AzureFault, FakeAzure, ManualClock

from omnifile_service.backends.azure import (
    AccessDenied,
    AzureBlobBackend,
    ChecksumMismatch,
)
from omnifile_service.backends.base import (
    BackendError,
    BackendKind,
    EdgeAccess,
    Immutable,
    LogicalTier,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    RestoreState,
    StorageBackend,
    Throttled,
    UnsupportedOperation,
)
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes
from omnifile_service.backends.sim_http import sas as simsas


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


async def _read(b: AzureBlobBackend, ref: ObjectRef, offset: int, length: int) -> bytes:
    return b"".join([c async for c in b.get_range(ref, offset, length)])


POSIX = {"file-owner": "1001", "file-group": "2001", "file-permissions": "0640"}


def _put(b: AzureBlobBackend, key: str, data: bytes, tier: LogicalTier | None = None) -> ObjectRef:
    return run(b.put("data", key, _stream(data), tier, POSIX))


# --- shape ------------------------------------------------------------------


def test_is_a_storage_backend_with_azure_capabilities(two: AzureBlobBackend) -> None:
    assert isinstance(two, StorageBackend)
    caps = two.capabilities()
    assert caps.kind is BackendKind.AZURE_BLOB
    assert caps.archive_needs_separate_account and caps.explicit_tier_leaves_smart
    assert not caps.worm and not caps.in_place_archive and not caps.metadata_on_archive
    assert {o.speed for o in caps.restore_options} == {RestoreSpeed.STANDARD, RestoreSpeed.HIGH}
    assert all(o.no_promise_above_bytes == 10 * 10**9 for o in caps.restore_options)
    assert caps.throughput_caps[0].limit == 10 and caps.throughput_caps[0].unit == "GiB/hour"


def test_worm_layout_capabilities(worm: AzureBlobBackend) -> None:
    caps = worm.capabilities()
    assert caps.worm and caps.versioning and caps.in_place_archive
    assert LogicalTier.SMART not in caps.tiers  # not on WORM until pilot 4 [W]
    assert not caps.archive_needs_separate_account


# --- put --------------------------------------------------------------------


def test_put_stages_blocks_with_crc64_and_commits_without_a_tier(
    two: AzureBlobBackend, fake: FakeAzure
) -> None:
    data = b"0123456789"  # block_size=4: blocks of 4, 4, 2
    ref = run(two.put("data", "a/f.segy", _stream(data[:3], data[3:]), None, POSIX))
    assert ref.container == "live/data" and ref.key == "a/f.segy" and ref.version_id
    blocks = fake.of("PUT", "block")
    assert [r.body for r in blocks] == [b"0123", b"4567", b"89"]
    for r in blocks:
        want = checksums.crc64.compute(r.body, 0).to_bytes(8, "little")
        assert r.headers["x-ms-content-crc64"] == base64.b64encode(want).decode()
        assert r.headers["Authorization"] == "Bearer fake-entra-token"
        assert "x-ms-content-md5" not in {k.lower() for k in r.headers}
    (commit,) = fake.of("PUT", "blocklist")
    # Smart is the live account's default: an explicit tier would leave Smart [V].
    assert "x-ms-access-tier" not in {k.lower() for k in commit.headers}
    assert commit.headers["x-ms-meta-file_owner"] == "1001"
    assert commit.headers["x-ms-meta-file_permissions"] == "0640"
    assert not any(k.lower().startswith("x-ms-meta-file-") for k in commit.headers)
    assert base64.b64decode(commit.headers["x-ms-blob-content-md5"]) == hashlib.md5(data).digest()
    head = run(two.head(ref))
    assert head.native_class == "Smart" and head.logical_tier is LogicalTier.SMART
    assert head.metadata == POSIX
    assert head.md5 == hashlib.md5(data).digest()
    assert head.crc64nvme is None  # Azure stores no whole-blob CRC64 [V]
    assert head.restore_state is RestoreState.NOT_NEEDED


def test_put_checks_the_callers_whole_object_crc_before_commit(
    two: AzureBlobBackend, fake: FakeAzure
) -> None:
    data = b"seismic trace bytes"
    good = to_bytes(crc64nvme(data))
    run(two.put("data", "ok", _stream(data), None, {}, crc64nvme=good))
    with pytest.raises(ChecksumMismatch):
        run(two.put("data", "bad", _stream(data), None, {}, crc64nvme=b"\0" * 8))
    assert [r.blob for r in fake.of("PUT", "blocklist")] == ["ok"]


def test_committed_blocks_is_get_block_list_for_that_version(
    two: AzureBlobBackend, fake: FakeAzure
) -> None:
    first = run(two.put("data", "bl.segy", _stream(b"0123456789"), None, POSIX))
    second = run(two.put("data", "bl.segy", _stream(b"abcdef"), None, POSIX))
    wire = [base64.b64encode(f"{i:08d}".encode()).decode() for i in range(3)]

    blocks = run(two.committed_blocks(first))
    (req,) = fake.of("GET", "blocklist")
    assert req.account == "live" and req.container == "data" and req.blob == "bl.segy"
    assert req.query["blocklisttype"] == "committed"
    assert req.query["versionid"] == first.version_id
    assert req.headers["Authorization"] == "Bearer fake-entra-token"
    # Wire names (not SDK-decoded ids), sizes, and no CRC: Azure doesn't list one.
    assert [(b.block_id, b.size, b.crc64nvme) for b in blocks] == [
        (wire[0], 4, None),
        (wire[1], 4, None),
        (wire[2], 2, None),
    ]
    assert [(b.block_id, b.size) for b in run(two.committed_blocks(second))] == [
        (wire[0], 4),
        (wire[1], 2),
    ]
    # No version: the current blob, and no versionid parameter.
    current = run(two.committed_blocks(ObjectRef(7, "data", "bl.segy")))
    assert [b.size for b in current] == [4, 2]
    assert "versionid" not in fake.of("GET", "blocklist")[-1].query
    with pytest.raises(ObjectNotFound):
        run(two.committed_blocks(ObjectRef(7, "data", "missing")))


def test_a_block_rejected_by_azure_is_a_checksum_mismatch(
    two: AzureBlobBackend, fake: FakeAzure
) -> None:
    fake.fail[("PUT", "block")] = AzureFault(400, "Crc64Mismatch")
    with pytest.raises(ChecksumMismatch):
        _put(two, "x", b"abc")


def test_empty_object_is_one_empty_block(two: AzureBlobBackend, fake: FakeAzure) -> None:
    ref = _put(two, "empty", b"")
    assert run(two.head(ref)).size == 0
    assert len(fake.of("PUT", "block")) == 1


def test_explicit_tier_is_sent_and_never_returns_to_smart(
    two: AzureBlobBackend, fake: FakeAzure
) -> None:
    ref = _put(two, "hot", b"x", LogicalTier.HOT)
    assert fake.of("PUT", "blocklist")[-1].headers["x-ms-access-tier"] == "Hot"
    assert run(two.head(ref)).logical_tier is LogicalTier.HOT
    with pytest.raises(UnsupportedOperation):
        run(two.set_class(ref, LogicalTier.SMART))
    smart = _put(two, "smart", b"x")
    assert run(two.set_class(smart, LogicalTier.SMART)) == smart  # already Smart: no-op
    moved = run(two.set_class(smart, LogicalTier.WARM))
    (tier_req,) = fake.of("PUT", "tier")
    assert tier_req.headers["x-ms-access-tier"] == "Cool"
    assert run(two.head(moved)).logical_tier is LogicalTier.WARM


def test_unknown_metadata_name_is_refused_before_any_call(two: AzureBlobBackend) -> None:
    with pytest.raises(BackendError):
        run(two.put("data", "m", _stream(b"x"), None, {"not-an-identifier": "1"}))


def test_smart_put_on_worm_layout_is_unsupported(worm: AzureBlobBackend) -> None:
    with pytest.raises(UnsupportedOperation):
        run(worm.put("data", "s", _stream(b"x"), LogicalTier.SMART, {}))


# --- reads ------------------------------------------------------------------


def test_ranged_reads(two: AzureBlobBackend, fake: FakeAzure) -> None:
    data = bytes(range(200))
    ref = _put(two, "r", data)
    assert run(_read(two, ref, 10, 20)) == data[10:30]
    get = fake.of("GET")[-1]
    assert get.headers["x-ms-range"] == "bytes=10-29"
    assert get.query.get("versionid") == ref.version_id
    assert run(_read(two, ref, 190, 100)) == data[190:]
    assert run(_read(two, ref, 500, 10)) == b""  # past the end: nothing exists there


def test_missing_object_is_not_found(two: AzureBlobBackend) -> None:
    with pytest.raises(ObjectNotFound):
        run(two.head(ObjectRef(7, "data", "nope")))
    with pytest.raises(ObjectNotFound):
        run(_read(two, ObjectRef(7, "data", "nope"), 0, 1))
    with pytest.raises(ObjectNotFound):
        run(two.delete(ObjectRef(7, "data", "nope")))


def test_throttling_and_auth_errors_are_mapped(two: AzureBlobBackend, fake: FakeAzure) -> None:
    ref = _put(two, "t", b"x")
    fake.fail[("HEAD", "")] = AzureFault(503, "ServerBusy")
    with pytest.raises(Throttled):
        run(two.head(ref))
    fake.fail[("HEAD", "")] = AzureFault(403, "AuthorizationPermissionMismatch")
    with pytest.raises(AccessDenied):
        run(two.head(ref))


def test_list_pages_and_versions(two: AzureBlobBackend) -> None:
    for i in range(3):
        _put(two, f"p/{i}", b"x")
    _put(two, "q/0", b"x")
    page = run(two.list("data", "p/", None))
    assert sorted(h.ref.key for h in page.items) == ["p/0", "p/1", "p/2"]
    assert all(h.metadata == POSIX for h in page.items)
    v1 = _put(two, "v", b"one")
    v2 = _put(two, "v", b"two")
    versions = run(two.list_versions(ObjectRef(7, "data", "v")))
    assert [v.ref.version_id for v in versions] == [v2.version_id, v1.version_id]
    assert versions[0].is_current and not versions[1].is_current
    assert run(_read(two, v1, 0, 10)) == b"one"


# --- archive and restore (two-account) --------------------------------------


def _sas_ok(fake: FakeAzure, url: str, *, now: Any, version: str | None = None) -> simsas.Grant:
    parts = urlsplit(url)
    container, _, blob = unquote(parts.path).lstrip("/").partition("/")
    q = dict(parse_qsl(parts.query))
    return simsas.validate(
        fake.keys,
        q,
        account=parts.hostname.split(".")[0],
        container=container,
        blob=blob,
        request_version_id=q.get("versionid") or version,
        now=now,
        scheme=parts.scheme,
        client_ip=None,
    )


def test_archive_is_a_copy_into_the_archive_account_then_a_delete(
    two: AzureBlobBackend, fake: FakeAzure, clock: ManualClock
) -> None:
    ref = _put(two, "cold/f", b"seismic")
    run(two.set_tags(ref, {"omnifile-state": "verified"}))
    archived = run(two.set_class(ref, LogicalTier.ARCHIVE))
    assert archived.container == "arch/data" and archived.key == "cold/f"
    copy = [r for r in fake.requests if "x-ms-copy-source" in r.headers][-1]
    assert copy.account == "arch" and copy.headers["x-ms-access-tier"] == "Archive"
    assert "omnifile-state=verified" in copy.headers["x-ms-tags"]  # tags carried over
    src = copy.headers["x-ms-copy-source"]
    assert src.startswith("https://live.blob.core.windows.net/data/cold/f?")
    grant = _sas_ok(fake, src, now=clock.now())
    assert grant.permissions == frozenset("r")
    deletes = fake.of("DELETE")
    assert [(d.account, d.blob) for d in deletes] == [("live", "cold/f")]
    with pytest.raises(ObjectNotFound):
        run(two.head(ObjectRef(7, "data", "cold/f")))
    head = run(two.head(archived))
    assert head.logical_tier is LogicalTier.ARCHIVE
    assert head.restore_state is RestoreState.ARCHIVED
    assert head.metadata == POSIX
    with pytest.raises(NotRestored):
        run(_read(two, archived, 0, 7))
    assert run(two.get_tags(archived)) == {"omnifile-state": "verified"}
    with pytest.raises(UnsupportedOperation):
        run(two.set_metadata(archived, {"file-owner": "0"}))


def test_archive_move_waits_for_a_pending_copy_before_deleting(
    fake: FakeAzure, clock: ManualClock
) -> None:
    from conftest import FakeCredential, two_account_config

    polls: list[float] = []

    async def sleep(seconds: float) -> None:
        polls.append(seconds)
        assert not fake.of("DELETE"), "the original must outlive the copy"
        if len(polls) == 3:
            fake.complete_copies()

    b = AzureBlobBackend(
        7,
        two_account_config(extra_client_kwargs={"retry_total": 0}),
        credential=FakeCredential(),
        clock=clock,
        transport=fake,
        sleep=sleep,
    )
    fake.copy_mode = "pending"
    ref = run(b.put("data", "big", _stream(b"x" * 10), None, {}))
    archived = run(b.set_class(ref, LogicalTier.ARCHIVE))
    assert polls == [0.5, 1.0, 2.0]
    assert archived.container == "arch/data"
    assert len(fake.of("DELETE")) == 1


def test_failed_archive_copy_keeps_the_original(fake: FakeAzure, clock: ManualClock) -> None:
    from conftest import FakeCredential, two_account_config

    async def sleep(_: float) -> None:
        fake.complete_copies(fail=True)

    b = AzureBlobBackend(
        7,
        two_account_config(extra_client_kwargs={"retry_total": 0}),
        credential=FakeCredential(),
        clock=clock,
        transport=fake,
        sleep=sleep,
    )
    fake.copy_mode = "pending"
    ref = run(b.put("data", "f", _stream(b"x"), None, {}))
    with pytest.raises(BackendError, match="failed"):
        run(b.set_class(ref, LogicalTier.ARCHIVE))
    assert not fake.of("DELETE")
    assert run(b.head(ref)).size == 1


def test_archive_move_checks_the_copy_before_deleting_the_original(
    two: AzureBlobBackend, fake: FakeAzure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3 review: a short archive copy must never cost the original."""
    ref = _put(two, "short/f", b"seismic data")
    real = fake._commit

    def truncating(acct, container, blob, data, *a, **kw):  # type: ignore[no-untyped-def]
        if acct.name == "arch":
            data = data[: len(data) // 2]
        return real(acct, container, blob, data, *a, **kw)

    monkeypatch.setattr(fake, "_commit", truncating)
    with pytest.raises(BackendError, match="does not match"):
        run(two.set_class(ref, LogicalTier.ARCHIVE))
    assert [d.account for d in fake.of("DELETE")] == ["arch"]  # only the bad copy
    assert run(two.head(ref)).size == len(b"seismic data")


def test_archive_move_never_deletes_an_original_it_cannot_check(
    two: AzureBlobBackend, fake: FakeAzure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend's own archive move can't read its copy back (it lands in
    Archive), so it deletes the original only when the copy's properties
    prove it (`copy_check`): an original with no Content-MD5 (an upload that
    didn't declare one) is kept, and the unchecked copy removed."""
    real = fake._commit

    def no_md5(acct, container, blob, data, tier, meta, md5, **kw):  # type: ignore[no-untyped-def]
        return real(acct, container, blob, data, tier, meta, None, **kw)

    monkeypatch.setattr(fake, "_commit", no_md5)
    ref = _put(two, "nomd5/f", b"seismic data")
    assert run(two.head(ref)).md5 is None
    with pytest.raises(BackendError, match="can't be checked"):
        run(two.set_class(ref, LogicalTier.ARCHIVE))
    assert [d.account for d in fake.of("DELETE")] == ["arch"]  # only the unchecked copy
    assert run(two.head(ref)).size == len(b"seismic data")


def test_tiering_archive_flow_hot_copy_read_back_then_archive_in_place(
    two: AzureBlobBackend, fake: FakeAzure
) -> None:
    """The worker's two-account archive (M3 coordinator decision): Copy Blob
    into the archive account as Hot, read it back, then Set Blob Tier ->
    Archive in place there (LRS); the original is untouched until then."""
    data = b"seismic trace bytes"
    ref = _put(two, "flow/f", data)
    copy = run(two.copy_object(ref, "arch/data", "flow/f", LogicalTier.HOT))
    assert copy.container == "arch/data"
    head = run(two.head(copy))
    assert head.native_class == "Hot" and head.size == len(data)
    assert run(_read(two, copy, 0, len(data))) == data
    archived = run(two.set_class(copy, LogicalTier.ARCHIVE))
    assert (archived.container, archived.key) == ("arch/data", "flow/f")
    assert run(two.head(archived)).native_class == "Archive"
    assert not fake.of("DELETE")
    assert run(two.head(ref)).size == len(data)


def test_restore_is_a_copy_into_the_recall_container(
    two: AzureBlobBackend, fake: FakeAzure, clock: ManualClock
) -> None:
    archived = run(two.set_class(_put(two, "r/f", b"seismic"), LogicalTier.ARCHIVE))
    with pytest.raises(UnsupportedOperation):
        run(two.restore(archived, RestoreSpeed.EXPEDITED))
    handle = run(two.restore(archived, RestoreSpeed.HIGH))
    copy = [r for r in fake.requests if "x-ms-copy-source" in r.headers][-1]
    assert (copy.account, copy.container) == ("arch", "recall")
    assert copy.blob == f"arch/data/r/f@{archived.version_id}"
    assert copy.headers["x-ms-access-tier"] == "Hot"
    assert copy.headers["x-ms-rehydrate-priority"] == "High"
    _sas_ok(fake, copy.headers["x-ms-copy-source"], now=clock.now())
    assert handle.target == ObjectRef(7, "arch/recall", copy.blob)
    assert not any(r.query.get("comp") == "tier" for r in fake.requests)  # never Set Blob Tier
    status = run(two.restore_status(handle))
    assert status.state is RestoreState.IN_PROGRESS
    assert status.eta == clock.now() + timedelta(hours=1)
    assert run(two.head(archived)).restore_state is RestoreState.IN_PROGRESS
    # A second request joins the running copy.
    again = run(two.restore(archived, RestoreSpeed.HIGH))
    assert again.provider_ref == handle.provider_ref
    assert len([r for r in fake.requests if "x-ms-copy-source" in r.headers]) == 2
    fake.complete_copies()
    status = run(two.restore_status(handle))
    assert status.state is RestoreState.COMPLETED and status.eta is None
    assert status.expires_at is None  # Azure's rehydrated copy is permanent
    assert run(_read(two, status.target, 0, 7)) == b"seismic"
    assert run(two.head(archived)).restore_state is RestoreState.COMPLETED


def test_restore_of_an_online_blob_is_refused(two: AzureBlobBackend) -> None:
    with pytest.raises(UnsupportedOperation):
        run(two.restore(_put(two, "online", b"x"), RestoreSpeed.STANDARD))


def test_no_eta_above_10_gb(two: AzureBlobBackend, fake: FakeAzure) -> None:
    archived = run(two.set_class(_put(two, "huge", b"x"), LogicalTier.ARCHIVE))
    handle = run(two.restore(archived, RestoreSpeed.STANDARD))
    fake.current("arch", "recall", handle.target.key).data = b"\0" * 10  # sized below
    status = run(two.restore_status(handle))
    assert status.eta is not None
    fake.current("arch", "recall", handle.target.key).data = _Big()  # type: ignore[assignment]
    assert run(two.restore_status(handle)).eta is None


class _Big(bytes):
    def __len__(self) -> int:
        return 10 * 10**9 + 1


def test_copy_object_refuses_an_archived_source(two: AzureBlobBackend) -> None:
    archived = run(two.set_class(_put(two, "c", b"x"), LogicalTier.ARCHIVE))
    with pytest.raises(NotRestored):
        run(two.copy_object(archived, "data", "c2"))
    src = _put(two, "c3", b"abc")
    dst = run(two.copy_object(src, "live/data", "c4"))
    assert run(_read(two, dst, 0, 10)) == b"abc"


# --- WORM (single account) --------------------------------------------------


def test_worm_tiers_in_place_and_protects_versions(
    worm: AzureBlobBackend, fake: FakeAzure, clock: ManualClock
) -> None:
    ref = run(worm.put("data", "w", _stream(b"v1"), None, {}))
    assert ref.container == "worm/data"
    until = clock.now() + timedelta(days=30)
    run(worm.set_retention(ref, until))
    (pol,) = fake.of("PUT", "immutabilityPolicies")
    assert pol.headers["x-ms-immutability-policy-mode"].lower() == "locked"
    assert pol.query["versionid"] == ref.version_id
    with pytest.raises(Immutable):
        run(worm.set_retention(ref, until - timedelta(days=1)))
    assert len(fake.of("PUT", "immutabilityPolicies")) == 1  # refused locally
    with pytest.raises(Immutable):
        run(worm.delete(ref))
    run(worm.set_legal_hold(ref, True))
    assert fake.of("PUT", "legalhold")[0].headers["x-ms-legal-hold"] == "true"
    archived = run(worm.set_class(ref, LogicalTier.ARCHIVE))
    assert archived == ref  # in place [W]
    (tier,) = fake.of("PUT", "tier")
    assert tier.headers["x-ms-access-tier"] == "Archive"
    assert tier.query["versionid"] == ref.version_id
    assert not any("x-ms-copy-source" in r.headers for r in fake.requests)
    head = run(worm.head(ref))
    assert head.retention_until == until and head.legal_hold
    # A tombstone of the current version destroys nothing.
    run(worm.delete(ObjectRef(8, "data", "w")))
    assert fake.of("DELETE")[-1].query.get("versionid") is None


def test_worm_restore_targets_the_non_worm_recall_container(
    worm: AzureBlobBackend, fake: FakeAzure
) -> None:
    ref = run(worm.put("data", "w2", _stream(b"v1"), None, {}))
    run(worm.set_class(ref, LogicalTier.ARCHIVE))
    handle = run(worm.restore(ref, RestoreSpeed.STANDARD))
    assert handle.target.container == "worm/recall"
    copy = [r for r in fake.requests if "x-ms-copy-source" in r.headers][-1]
    assert copy.headers["x-ms-rehydrate-priority"] == "Standard"


def test_two_account_layout_has_no_worm(two: AzureBlobBackend, clock: ManualClock) -> None:
    ref = _put(two, "nw", b"x")
    with pytest.raises(UnsupportedOperation):
        run(two.set_retention(ref, clock.now()))
    with pytest.raises(UnsupportedOperation):
        run(two.set_legal_hold(ref, True))


def test_archive_move_refuses_retained_data(two: AzureBlobBackend, fake: FakeAzure) -> None:
    ref = _put(two, "held", b"x")
    fake.current("live", "data", "held").legal_hold = True
    with pytest.raises(Immutable):
        run(two.set_class(ref, LogicalTier.ARCHIVE))
    assert not any("x-ms-copy-source" in r.headers for r in fake.requests)


# --- tags and metadata ------------------------------------------------------


def test_tags_are_limited_to_ten(two: AzureBlobBackend, fake: FakeAzure) -> None:
    ref = _put(two, "tags", b"x")
    with pytest.raises(BackendError):
        run(two.set_tags(ref, {f"k{i}": "v" for i in range(11)}))
    assert not fake.of("PUT", "tags")
    run(two.set_tags(ref, {"hold": "legal"}))
    assert run(two.get_tags(ref)) == {"hold": "legal"}


def test_set_metadata_makes_a_new_version(two: AzureBlobBackend, fake: FakeAzure) -> None:
    ref = _put(two, "meta", b"x")
    new = run(two.set_metadata(ref, {"file-owner": "0"}))
    assert new.version_id and new.version_id != ref.version_id
    (req,) = fake.of("PUT", "metadata")
    assert req.headers["x-ms-meta-file_owner"] == "0"
    assert run(two.head(ObjectRef(7, "data", "meta"))).metadata == {"file-owner": "0"}


# --- edge credentials -------------------------------------------------------


def test_read_and_write_sas_for_the_edge(
    two: AzureBlobBackend, fake: FakeAzure, clock: ManualClock
) -> None:
    ref = _put(two, "dir/a b.segy", b"x")
    ttl = timedelta(minutes=60)
    read = run(two.edge_credential(ref, EdgeAccess.READ, ttl))
    assert read.expires_at == clock.now() + ttl
    assert read.details["cache_until"] == (clock.now() + timedelta(minutes=55)).isoformat()
    # Private endpoint host, never a public fallback (decision 22).
    assert read.url.startswith(
        "https://live.privatelink.blob.core.windows.net/data/dir/a%20b.segy?"
    )
    grant = _sas_ok(fake, read.url, now=clock.now())
    assert grant.permissions == frozenset("r") and grant.resource == "bv"
    assert grant.version_id == ref.version_id
    q = dict(parse_qsl(urlsplit(read.url).query))
    assert q["spr"] == "https" and q["versionid"] == ref.version_id
    # A write SAS lives 15 minutes at most (M1 security review).
    with pytest.raises(ValueError, match="at most"):
        run(two.edge_credential(ObjectRef(7, "data", "dir/new"), EdgeAccess.WRITE, ttl))
    wttl = timedelta(minutes=15)
    write = run(two.edge_credential(ObjectRef(7, "data", "dir/new"), EdgeAccess.WRITE, wttl))
    grant = _sas_ok(fake, write.url, now=clock.now())
    assert grant.permissions == frozenset("cw") and grant.resource == "b"
    with pytest.raises(Exception, match="xpired|not valid|Authentication"):
        _sas_ok(fake, write.url, now=clock.now() + wttl + timedelta(seconds=1))
    # One user-delegation key serves both: minting costs no Azure call [C].
    assert len([r for r in fake.requests if r.query.get("comp") == "userdelegationkey"]) == 1


def test_delegation_key_is_refreshed_before_it_runs_short(
    two: AzureBlobBackend, fake: FakeAzure, clock: ManualClock
) -> None:
    ref = _put(two, "k", b"x")
    run(two.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=60)))
    clock.advance(timedelta(hours=23))
    cred = run(two.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=60)))
    assert len([r for r in fake.requests if r.query.get("comp") == "userdelegationkey"]) == 2
    _sas_ok(fake, cred.url, now=clock.now())
