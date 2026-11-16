"""S3-specific behaviour of the backend, over the in-memory fake: multipart,
large copies, restore rules and the request budget, expiry, the SQS event
reader, and the bucket allow-list."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import pytest
from backend_contract.clock import ManualClock

from omnifile_service.backends.base import (
    BackendError,
    ChangeKind,
    CredentialMode,
    EdgeAccess,
    LogicalTier,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    RestoreState,
    Throttled,
    UnsupportedOperation,
)
from omnifile_service.backends.s3 import S3Backend
from omnifile_service.backends.s3 import backend as s3_backend
from omnifile_service.backends.s3.config import BatchOps
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes

from .conftest import BUCKET, MiB
from .s3_fakes import ACCOUNT, FakeAws

Make = Callable[..., S3Backend]


def run[T](coro) -> T:  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


async def _stream(data: bytes, step: int = 1 * MiB) -> AsyncIterator[bytes]:
    for i in range(0, len(data), step):
        yield data[i : i + step]


async def _read(b: S3Backend, ref: ObjectRef, offset: int = 0, length: int = 1 << 40) -> bytes:
    return b"".join([c async for c in b.get_range(ref, offset, length)])


def _put(b: S3Backend, key: str, data: bytes, tier: LogicalTier | None = None, **meta: str):
    return run(b.put(BUCKET, key, _stream(data), tier, meta))


def _current(ref: ObjectRef) -> ObjectRef:
    return ObjectRef(ref.backend_id, ref.container, ref.key)


# --- uploads ------------------------------------------------------------------


def test_large_put_is_one_multipart_upload_with_a_full_object_crc(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    data = bytes(range(256)) * (11 * MiB // 256)
    ref = _put(b, "big.segy", data, **{"file-owner": "1001"})
    assert fake.ops().count("UploadPart") == 3
    assert "PutObject" not in fake.ops()
    create = dict(fake.calls)["CreateMultipartUpload"]
    assert create["ChecksumAlgorithm"] == "CRC64NVME"
    assert create["ChecksumType"] == "FULL_OBJECT"
    head = run(b.head(ref))
    assert head.size == len(data)
    assert head.crc64nvme == to_bytes(crc64nvme(data))
    assert head.md5 is None  # a multipart ETag is not an MD5
    assert head.metadata["file-owner"] == "1001"
    assert run(_read(b, ref, 5 * MiB - 3, 6)) == data[5 * MiB - 3 : 5 * MiB + 3]


def test_a_put_whose_bytes_do_not_match_the_expected_crc_is_not_committed(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    wrong = b"\x00" * 8
    with pytest.raises(BackendError, match="CRC64NVME"):
        run(b.put(BUCKET, "small", _stream(b"abc"), None, {}, crc64nvme=wrong))
    with pytest.raises(BackendError, match="CRC64NVME"):
        run(b.put(BUCKET, "large", _stream(b"x" * 11 * MiB), None, {}, crc64nvme=wrong))
    assert "PutObject" not in fake.ops()
    assert "CompleteMultipartUpload" not in fake.ops()
    assert "AbortMultipartUpload" in fake.ops()
    assert fake.uploads == {}
    with pytest.raises(ObjectNotFound):
        run(b.head(ObjectRef(1, BUCKET, "large")))


def test_put_with_a_tier_sets_the_storage_class(make_backend: Make, fake: FakeAws) -> None:
    b = make_backend()
    ref = _put(b, "warm", b"x", LogicalTier.WARM)
    assert run(b.head(ref)).native_class == "STANDARD_IA"
    deep = make_backend(archive_class="DEEP_ARCHIVE")
    assert run(deep.head(_put(deep, "deep", b"x", LogicalTier.ARCHIVE))).native_class == (
        "DEEP_ARCHIVE"
    )
    smart = _put(b, "smart", b"x", LogicalTier.SMART)
    assert run(b.head(smart)).logical_tier is LogicalTier.SMART


# --- copies and classes ---------------------------------------------------------


def test_set_metadata_keeps_the_storage_class_and_tags(make_backend: Make) -> None:
    b = make_backend()
    ref = _put(b, "ia", b"bytes", LogicalTier.WARM, **{"file-owner": "1"})
    run(b.set_tags(ref, {"omnifile-state": "verified"}))
    new = run(b.set_metadata(ref, {"file-owner": "2"}))
    head = run(b.head(new))
    assert head.native_class == "STANDARD_IA"
    assert head.metadata == {"file-owner": "2"}
    assert run(b.get_tags(new)) == {"omnifile-state": "verified"}
    assert new.version_id != ref.version_id


def test_copies_above_the_single_copy_limit_use_upload_part_copy(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend(single_copy_limit=6 * MiB, copy_part_size=5 * MiB)
    data = bytes(range(251)) * (11 * MiB // 251)
    src = _put(b, "src", data, **{"file-owner": "7"})
    run(b.set_tags(src, {"hold": "legal"}))
    dst = run(b.copy_object(src, BUCKET, "dst", LogicalTier.COLD))
    assert fake.ops().count("UploadPartCopy") == 3
    assert "CopyObject" not in fake.ops()
    head = run(b.head(dst))
    assert head.native_class == "GLACIER_IR"
    assert head.metadata == {"file-owner": "7"}
    assert head.crc64nvme == to_bytes(crc64nvme(data))
    assert run(b.get_tags(dst)) == {"hold": "legal"}
    assert run(_read(b, dst)) == data


def test_set_class_to_the_current_class_is_a_no_op(make_backend: Make, fake: FakeAws) -> None:
    b = make_backend()
    ref = _put(b, "same", b"x")
    assert run(b.set_class(ref, LogicalTier.HOT)) == ref
    assert "CopyObject" not in fake.ops()


def test_an_unrestored_archived_object_cannot_be_copied_or_reclassed(make_backend: Make) -> None:
    b = make_backend()
    archived = run(b.set_class(_put(b, "a", b"x"), LogicalTier.ARCHIVE))
    with pytest.raises(UnsupportedOperation):
        run(b.copy_object(archived, BUCKET, "b"))
    with pytest.raises(UnsupportedOperation):
        run(b.set_class(archived, LogicalTier.HOT))


def test_an_unknown_native_class_is_refused(make_backend: Make) -> None:
    b = make_backend()
    with pytest.raises(UnsupportedOperation):
        run(b.set_class(_put(b, "n", b"x"), LogicalTier.COLD, native_class="GLACIER"))


# --- restores --------------------------------------------------------------------


def _archived(b: S3Backend, key: str = "arch", data: bytes = b"seismic") -> ObjectRef:
    return run(b.set_class(_put(b, key, data), LogicalTier.ARCHIVE))


def test_restore_rules(make_backend: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    b = make_backend()
    archived = _archived(b)
    with pytest.raises(UnsupportedOperation, match="Days"):
        run(b.restore(archived, RestoreSpeed.STANDARD))
    with pytest.raises(UnsupportedOperation, match="needs no restore"):
        run(b.restore(_put(b, "hot", b"x"), RestoreSpeed.STANDARD, days=1))
    deep = make_backend(archive_class="DEEP_ARCHIVE")
    deep_ref = _archived(deep, "deep")
    with pytest.raises(UnsupportedOperation, match="Deep Archive"):
        run(deep.restore(deep_ref, RestoreSpeed.EXPEDITED, days=1))
    monkeypatch.setattr(s3_backend, "S3_EXPEDITED_MAX_BYTES", 4)
    with pytest.raises(UnsupportedOperation, match="250 MB"):
        run(b.restore(archived, RestoreSpeed.EXPEDITED, days=1))


def test_a_restore_already_running_is_tracked_not_failed(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    b = make_backend()
    archived = _archived(b)
    first = run(b.restore(archived, RestoreSpeed.STANDARD, days=2))
    second = run(b.restore(archived, RestoreSpeed.STANDARD, days=2))
    assert first.provider_ref == second.provider_ref
    assert first.provider_ref == f"s3-restore:{BUCKET}/arch@{archived.version_id}"
    status = run(b.restore_status(second))
    assert status.state is RestoreState.IN_PROGRESS
    assert status.eta == first.requested_at + timedelta(hours=5)  # Standard: 3-5 h


def test_the_restore_budget_throttles_before_s3_does(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    b = make_backend(restore_rate_per_s=2)
    refs = [_archived(b, f"r{i}") for i in range(3)]
    run(b.restore(refs[0], RestoreSpeed.BULK, days=1))
    run(b.restore(refs[1], RestoreSpeed.BULK, days=1))
    with pytest.raises(Throttled) as e:
        run(b.restore(refs[2], RestoreSpeed.BULK, days=1))
    assert e.value.retry_after == timedelta(seconds=0.5)
    assert fake.ops().count("RestoreObject") == 2
    clock.advance(timedelta(seconds=1))
    run(b.restore(refs[2], RestoreSpeed.BULK, days=1))


def test_s3_throttling_is_throttled_with_its_retry_after(make_backend: Make, fake: FakeAws) -> None:
    b = make_backend()
    archived = _archived(b)
    fake.fail_next("RestoreObject", "ThrottlingException", 503, **{"retry-after": "2"})
    with pytest.raises(Throttled) as e:
        run(b.restore(archived, RestoreSpeed.STANDARD, days=1))
    assert e.value.retry_after == timedelta(seconds=2)
    fake.fail_next("RestoreObject", "GlacierExpeditedRetrievalNotAvailable", 503)
    with pytest.raises(Throttled):
        run(b.restore(archived, RestoreSpeed.EXPEDITED, days=1))


def test_expiry_before_and_after_s3_removes_the_copy(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    b = make_backend()
    archived = _archived(b, data=b"temp")
    handle = run(b.restore(archived, RestoreSpeed.STANDARD, days=1))
    clock.advance(timedelta(hours=5))
    done = run(b.restore_status(handle))
    assert done.state is RestoreState.COMPLETED
    assert done.expires_at is not None and done.expires_at.hour == 0  # midnight UTC
    listed = run(b.list(BUCKET, "arch", None)).items[0]
    assert listed.restore_state is RestoreState.COMPLETED
    assert listed.restore_expires_at == done.expires_at
    assert run(_read(b, archived)) == b"temp"
    # Past expiry, S3 may still serve the copy for a while: the clock decides.
    clock.advance(done.expires_at - clock.now())
    assert run(b.head(archived)).restore_state is RestoreState.EXPIRED
    with pytest.raises(NotRestored):
        run(_read(b, archived))
    # Once S3 has removed it, the object reads as archived again; the
    # status of the accepted restore is EXPIRED.
    clock.advance(fake.expiry_lag)
    assert run(b.head(archived)).restore_state is RestoreState.ARCHIVED
    assert run(b.restore_status(handle)).state is RestoreState.EXPIRED
    with pytest.raises(NotRestored):
        run(_read(b, archived))


def test_intelligent_tiering_archive_restores_take_no_days(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    b = make_backend()
    ref = _put(b, "it", b"tiered", LogicalTier.SMART)
    fake.it_archive(BUCKET, "it", "DEEP_ARCHIVE_ACCESS")
    head = run(b.head(ref))
    assert head.restore_state is RestoreState.ARCHIVED
    with pytest.raises(NotRestored):
        run(_read(b, ref))
    with pytest.raises(UnsupportedOperation, match="no Days"):
        run(b.restore(ref, RestoreSpeed.STANDARD, days=1))
    with pytest.raises(UnsupportedOperation, match="Deep Archive"):
        run(b.restore(ref, RestoreSpeed.EXPEDITED))
    handle = run(b.restore(ref, RestoreSpeed.STANDARD))
    status = run(b.restore_status(handle))
    assert status.state is RestoreState.IN_PROGRESS
    assert status.eta == handle.requested_at + timedelta(hours=12)
    clock.advance(timedelta(hours=12))
    status = run(b.restore_status(handle))
    assert status.state is RestoreState.COMPLETED and status.expires_at is None
    assert run(b.head(ref)).restore_state is RestoreState.NOT_NEEDED
    assert run(_read(b, ref)) == b"tiered"


# --- reads, deletes, listings -------------------------------------------------------


def test_reads_past_the_end_and_of_empty_objects_are_empty(make_backend: Make) -> None:
    b = make_backend()
    assert run(_read(b, _put(b, "empty", b""), 0, 10)) == b""
    assert run(_read(b, _put(b, "five", b"12345"), 5, 10)) == b""
    assert run(_read(b, _put(b, "zero", b"x"), 0, 0)) == b""


def test_a_body_that_fails_mid_read_is_an_error_and_is_closed(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    ref = _put(b, "flaky", b"x" * (3 * MiB))
    fake.body_fail_after = 1 * MiB
    with pytest.raises(BackendError):
        run(_read(b, ref))
    assert fake.bodies[-1].closed


def test_deleting_a_version_and_a_delete_marker(make_backend: Make) -> None:
    b = make_backend()
    first = _put(b, "d", b"1")
    run(b.delete(_current(first)))
    marker = next(v for v in run(b.list_versions(_current(first))) if v.is_delete_marker)
    run(b.delete(marker.ref))  # deleting the marker brings the object back
    assert run(_read(b, _current(first))) == b"1"
    run(b.delete(first))
    with pytest.raises(ObjectNotFound):
        run(b.list_versions(_current(first)))


def test_list_all_versions_pages(make_backend: Make) -> None:
    b = make_backend()
    for i in range(3):
        _put(b, "lv/a", str(i).encode())
    _put(b, "lv/b", b"x")
    seen, cursor = [], None
    for _ in range(10):
        page = run(b.list_all_versions(BUCKET, "lv/", cursor))
        seen += [(v.head.ref.key, v.is_current) for v in page.items]
        cursor = page.cursor
        if cursor is None:
            break
    assert sorted(seen) == [("lv/a", False), ("lv/a", False), ("lv/a", True), ("lv/b", True)]


def test_unversioned_buckets_give_refs_without_versions(make_backend: Make, fake: FakeAws) -> None:
    fake.add_bucket("plain", versioning=False)
    b = make_backend(versioning=False, buckets=("plain",), owned_buckets=("plain",))
    ref = run(b.put("plain", "u", _stream(b"1"), None, {}))
    assert ref.version_id is None  # S3 says "null"
    assert run(b.head(ref)).ref.version_id is None
    run(b.put("plain", "u", _stream(b"2"), None, {}))
    assert run(_read(b, ref)) == b"2"
    assert not b.capabilities().versioning


def test_a_bucket_outside_the_binding_is_refused_without_a_request(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    fake.add_bucket("someone-else")
    other = ObjectRef(1, "someone-else", "k")
    with pytest.raises(BackendError, match="not part of this binding"):
        run(b.head(other))
    with pytest.raises(BackendError, match="not part of this binding"):
        run(b.put("someone-else", "k", _stream(b"x"), None, {}))
    assert fake.calls == []


def test_edge_credential_is_a_presigned_get_of_one_version(
    make_backend: Make, clock: ManualClock
) -> None:
    from urllib.parse import parse_qs, urlsplit

    # The operator's fallback mode; the default is a pointer (below).
    b = make_backend(
        edge_credentials="presigned",
        edge_endpoint="bucket.vpce-edge-1.s3.eu-west-1.vpce.amazonaws.com",
    )
    ref = _put(b, "dir/e f", b"x")
    cred = run(b.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=10)))
    assert cred.mode is CredentialMode.S3_PRESIGNED_URL
    assert cred.expires_at == clock.now() + timedelta(minutes=10)
    u = urlsplit(cred.url or "")
    # Signed for the edge's own interface endpoint (virtual-hosted: the
    # bucket in front of the endpoint name), HTTPS, for this version.
    edge = "bucket.vpce-edge-1.s3.eu-west-1.vpce.amazonaws.com"
    assert u.scheme == "https" and u.hostname == f"{BUCKET}.{edge}"
    q = parse_qs(u.query)
    assert q["versionId"] == [ref.version_id]
    assert q["X-Amz-Expires"] == ["600"] and "X-Amz-Signature" in q
    assert u.path.endswith("/dir/e%20f")
    # Edges never write to S3: new writes go to the dataset's own backend.
    with pytest.raises(UnsupportedOperation):
        run(b.edge_credential(ref, EdgeAccess.WRITE, timedelta(minutes=10)))


def test_a_presigned_get_signs_the_catalogs_etag_as_if_match(make_backend: Make) -> None:
    """Codex review on #23: with presigned URLs on an unversioned bucket,
    the ETag is a signed header, so an object replaced between two chunk
    reads answers 412 instead of mixing its bytes with the old one's."""
    from urllib.parse import parse_qs, urlsplit

    b = make_backend(edge_credentials="presigned")
    ref = _put(b, "u", b"x")
    plain = ObjectRef(ref.backend_id, ref.container, ref.key)  # no version id
    ttl = timedelta(minutes=10)
    cred = run(b.edge_credential(plain, EdgeAccess.READ, ttl, if_match='"e1"'))
    q = parse_qs(urlsplit(cred.url or "").query)
    assert "if-match" in q["X-Amz-SignedHeaders"][0].split(";")
    assert cred.details["if_match"] == '"e1"'
    # Without an ETag, nothing is signed (the edge then fills whole files).
    cred = run(b.edge_credential(plain, EdgeAccess.READ, ttl))
    q = parse_qs(urlsplit(cred.url or "").query)
    assert "if-match" not in q["X-Amz-SignedHeaders"][0].split(";")
    assert "if_match" not in cred.details


def test_edge_credential_is_a_pointer_not_a_secret(make_backend: Make, clock: ManualClock) -> None:
    b = make_backend()  # roles_anywhere is the default
    ref = _put(b, "e", b"x")
    cred = run(b.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=10)))
    assert cred.mode is CredentialMode.S3_ROLES_ANYWHERE
    assert cred.url is None
    assert cred.details == {
        "bucket": BUCKET,
        "key": "e",
        "region": "eu-west-1",
        "endpoint": "bucket.vpce-0a1b2c3d-4e5f6a7b.s3.eu-west-1.vpce.amazonaws.com",
        "addressing": "virtual_host",
        "tls_max_version": "1.2",
        "requester_pays": False,
        "private_network": True,
        "version_id": ref.version_id,
    }
    with pytest.raises(UnsupportedOperation):
        run(b.edge_credential(ref, EdgeAccess.WRITE, timedelta(minutes=10)))
    assert cred.expires_at == clock.now() + timedelta(minutes=10)


def test_worm_is_not_offered(make_backend: Make) -> None:
    b = make_backend()
    ref = _put(b, "w", b"x")
    with pytest.raises(UnsupportedOperation):
        run(b.set_legal_hold(ref, True))
    with pytest.raises(UnsupportedOperation):
        run(b.committed_blocks(ref))


# --- events (EventBridge -> SQS) -------------------------------------------------------


def test_events_page_through_the_queue_and_are_deleted_when_kept(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    cursor = run(b.read_changes(BUCKET, None)).cursor
    for i in range(13):
        _put(b, f"ev/{i}", b"x")
    first = run(b.read_changes(BUCKET, cursor))
    assert len(first.records) == 10 and not first.caught_up
    second = run(b.read_changes(BUCKET, first.cursor))
    assert len(second.records) == 3 and second.caught_up
    assert len(fake.queues[fake.queue_url("omnifile-events")]) == 3  # second page not yet kept
    rest = run(b.read_changes(BUCKET, second.cursor))
    assert rest.records == [] and rest.cursor == second.cursor
    assert fake.queues[fake.queue_url("omnifile-events")] == []
    keys = [r.ref.key for r in [*first.records, *second.records]]
    assert sorted(keys) == sorted(f"ev/{i}" for i in range(13))
    assert all(r.kind is ChangeKind.CREATED and r.size == 1 for r in first.records)


def test_a_page_never_kept_comes_back_after_the_visibility_timeout(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    b = make_backend(events_visibility_s=60)
    start = run(b.read_changes(BUCKET, None)).cursor
    _put(b, "again", b"x")
    assert len(run(b.read_changes(BUCKET, start)).records) == 1
    restarted = make_backend(events_visibility_s=60)  # the cursor's page is forgotten
    assert run(restarted.read_changes(BUCKET, start)).records == []
    clock.advance(timedelta(seconds=61))
    again = run(restarted.read_changes(BUCKET, start))
    assert [r.ref.key for r in again.records] == ["again"]


def test_restore_events_carry_the_expiry(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    b = make_backend()
    archived = _archived(b)
    cursor = run(b.read_changes(BUCKET, None)).cursor
    run(b.restore(archived, RestoreSpeed.STANDARD, days=3))
    clock.advance(timedelta(days=5))
    records = []
    page = run(b.read_changes(BUCKET, cursor))
    records += page.records
    while not page.caught_up:
        page = run(b.read_changes(BUCKET, page.cursor))
        records += page.records
    by_kind = {r.kind: r for r in records if r.ref.key == "arch"}
    started = by_kind[ChangeKind.RESTORE_STARTED]
    completed = by_kind[ChangeKind.RESTORE_COMPLETED]
    expired = by_kind[ChangeKind.RESTORE_EXPIRED]
    assert started.native_class == "GLACIER" and started.ref.version_id == archived.version_id
    assert completed.detail["expires_at"].endswith("T00:00:00Z")
    handle_ref = f"s3-restore:{BUCKET}/arch@{archived.version_id}"
    assert started.detail["provider_ref"] == completed.detail["provider_ref"] == handle_ref
    assert started.at < completed.at < expired.at


def test_other_buckets_events_are_released_and_junk_is_dropped(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    queue = fake.queue_url("omnifile-events")
    start = run(b.read_changes(BUCKET, None)).cursor
    other = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "time": "2026-10-05T12:00:00Z",
        "detail": {"bucket": {"name": "elsewhere"}, "object": {"key": "k", "size": 1}},
    }
    fake.send_raw(queue, json.dumps(other))
    fake.send_raw(queue, json.dumps({"source": "aws.ec2", "detail-type": "x"}))
    fake.send_raw(queue, json.dumps({"source": "aws.s3", "detail-type": "Object Created"}))
    fake.send_raw(queue, json.dumps({**other, "detail-type": "Object Tags Added"}))
    page = run(b.read_changes(BUCKET, start))
    assert page.records == [] and page.cursor == start
    left = fake.queues[queue]
    assert len(left) == 1 and json.loads(left[0].body)["detail"]["bucket"]["name"] == "elsewhere"
    assert "ChangeMessageVisibility" in fake.ops()


def test_a_bucket_without_a_queue_has_no_change_feed(make_backend: Make) -> None:
    b = make_backend(event_queues={})
    with pytest.raises(UnsupportedOperation):
        run(b.read_changes(BUCKET, None))


def test_a_shared_queue_full_of_other_buckets_events_reads_as_caught_up(
    make_backend: Make, fake: FakeAws
) -> None:
    b = make_backend()
    queue = fake.queue_url("omnifile-events")
    start = run(b.read_changes(BUCKET, None)).cursor
    other = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "time": "2026-10-05T12:00:00Z",
        "detail": {"bucket": {"name": "elsewhere"}, "object": {"key": "k", "size": 1}},
    }
    for _ in range(12):
        fake.send_raw(queue, json.dumps(other))
    page = run(b.read_changes(BUCKET, start))
    assert page.records == [] and page.caught_up
    assert len(fake.queues[queue]) == 12


def test_events_from_before_the_first_cursor_are_never_applied(
    make_backend: Make, fake: FakeAws, clock: ManualClock
) -> None:
    """`read_changes(c, None)` is "from now", even with a queue that already
    holds events: a stale delete must not reach the catalog."""
    b = make_backend()
    queue = fake.queue_url("omnifile-events")
    old = _put(b, "k", b"1")
    run(b.delete(_current(old)))
    clock.advance(timedelta(seconds=1))
    start = run(b.read_changes(BUCKET, None)).cursor
    _put(b, "k", b"2")
    page = run(b.read_changes(BUCKET, start))
    assert [(r.kind, r.ref.key) for r in page.records] == [(ChangeKind.CREATED, "k")]
    assert len(fake.queues[queue]) == 1  # the stale ones are deleted; this one awaits the ack
    later = run(b.read_changes(BUCKET, page.cursor))
    assert later.records == [] and fake.queues[queue] == []


def test_a_cursor_for_another_bucket_or_garbage_is_refused(make_backend: Make) -> None:
    b = make_backend()
    for bad in ("s3ev:other:0:0", "nonsense", "s3ev:contract:x:0"):
        with pytest.raises(BackendError, match="cursor"):
            run(b.read_changes(BUCKET, bad))


def test_a_versioned_delete_is_reported_as_a_tombstone(make_backend: Make) -> None:
    b = make_backend()
    start = run(b.read_changes(BUCKET, None)).cursor
    run(b.delete(_current(_put(b, "t", b"x"))))
    page = run(b.read_changes(BUCKET, start))
    deleted = [r for r in page.records if r.kind is ChangeKind.DELETED]
    assert len(deleted) == 1 and deleted[0].detail["tombstone"] == "true"


# --- Batch Operations: checksum reports -----------------------------------------


def test_a_checksum_report_is_read_as_s3_writes_its_keys(make_backend: Make, fake: FakeAws) -> None:
    """Real AWS (run rr-202611150626-34b3, 2026-11-14 PST): a completion
    report's key column is form-encoded whatever the job's manifest used. A
    space comes back as `+` and a literal plus as `%2B`; slashes are kept.
    Read with plain `unquote`, `with space-01.segy` came back as
    `with+space-01.segy`: its checksum matched no file, so a file with a space
    in its name could never be verified before cutover.

    Every name below and its spelling in the report are from real S3 reports
    (that run, and rr-202611150718-62e9 for the percent sign, the name that
    already looks encoded, the comma and the non-ASCII letters)."""
    ops = "omnifile-ops"
    fake.add_bucket(ops)
    client = make_backend()
    real = {
        "seed/standard/with space-01.segy": "seed/standard/with+space-01.segy",
        "seed/standard/plus+sign-02.segy": "seed/standard/plus%2Bsign-02.segy",
        "seed/standard/plain-00.segy": "seed/standard/plain-00.segy",
        "seed/with space-00.bin": "seed/with+space-00.bin",
        "seed/plus+sign-01.bin": "seed/plus%2Bsign-01.bin",
        "seed/per%cent-02.bin": "seed/per%25cent-02.bin",
        "seed/looks%2Bencoded-03.bin": "seed/looks%252Bencoded-03.bin",
        "seed/com,ma-04.bin": "seed/com%2Cma-04.bin",
        "seed/nörd-ünï-05.bin": "seed/n%C3%B6rd-%C3%BCn%C3%AF-05.bin",
    }
    keys = list(real)
    refs = [_put(client, k, k.encode()) for k in keys]
    service = make_backend(
        buckets=(BUCKET, ops),
        owned_buckets=(),
        private_network=False,
        endpoint_url=None,
        sqs_endpoint_url=None,
        batch=BatchOps(ACCOUNT, f"arn:aws:iam::{ACCOUNT}:role/omnifile-batch", ops),
    )
    name = "omnifile-checksums-1-1"
    job = run(service.batch_checksum(refs, name=name))

    # What the job was given, and what the report holds: not the same spelling.
    stored = {k: chain[-1].data.decode() for k, chain in fake.buckets[ops].keys.items()}
    manifest = stored[f"omnifile/batch/{name}.csv"]
    assert f"{BUCKET},seed/standard/with%20space-01.segy," in manifest
    assert f"{BUCKET},seed/standard/plus%2Bsign-02.segy," in manifest
    [report] = [text for k, text in stored.items() if "/results/" in k]
    rows = list(csv.reader(io.StringIO(report)))
    assert [r[1] for r in rows] == list(real.values())
    assert all(r[3:6] == ["succeeded", "200", ""] for r in rows)
    assert set(json.loads(rows[0][6])) == {
        "checksum_base64", "ETag", "checksumAlgorithm", "checksumType", "checksum_hex"}  # fmt: skip

    got = run(service.batch_checksums(job, name=name))
    assert got == {(BUCKET, r.key, r.version_id): to_bytes(crc64nvme(r.key.encode())) for r in refs}
    assert sorted(k for _, k, _ in got) == sorted(keys)
