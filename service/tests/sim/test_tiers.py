"""Tier rules: Smart, the Archive/ZRS split, minimum durations, S3 classes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    Immutable,
    LogicalTier,
    NotRestored,
    RestoreSpeed,
    RestoreState,
    UnsupportedOperation,
)
from omnifile_service.backends.sim import (
    AccountConfig,
    Conflict,
    Flavour,
    Redundancy,
    SimBackend,
    VirtualClock,
)

from .helpers import current, put, read, run

# --- Azure Smart ------------------------------------------------------------


def test_upload_without_tier_lands_in_smart(azure: SimBackend) -> None:
    head = run(azure.head(put(azure, "data", "a", b"x")))
    assert (head.native_class, head.logical_tier) == ("Smart", LogicalTier.SMART)
    assert head.ref.container == "live/data"


def test_explicit_tier_at_upload_never_enters_smart(azure: SimBackend) -> None:
    ref = put(azure, "data", "a", b"x", tier=LogicalTier.HOT)
    assert run(azure.head(ref)).native_class == "Hot"
    with pytest.raises(UnsupportedOperation, match="Smart"):
        run(azure.set_class(ref, LogicalTier.SMART))


def test_set_class_out_of_smart_is_one_way(azure: SimBackend) -> None:
    ref = put(azure, "data", "a", b"x")
    run(azure.set_class(ref, LogicalTier.WARM))
    run(azure.set_class(ref, LogicalTier.HOT))
    with pytest.raises(UnsupportedOperation, match="Smart"):
        run(azure.set_class(ref, LogicalTier.SMART))
    assert run(azure.head(ref)).native_class == "Hot"


def test_smart_needs_a_zonal_account(worm: SimBackend) -> None:
    assert LogicalTier.SMART not in worm.capabilities().tiers
    with pytest.raises(UnsupportedOperation):
        put(worm, "data", "a", b"x", tier=LogicalTier.SMART)
    with pytest.raises(ValueError):
        AccountConfig("bad", Redundancy.LRS, smart_default=True)


# --- Archive and account redundancy ----------------------------------------


def test_archive_is_not_allowed_on_zrs(azure: SimBackend) -> None:
    with pytest.raises(UnsupportedOperation, match="ZRS"):
        put(azure, "data", "a", b"x", tier=LogicalTier.ARCHIVE)


def test_archive_move_copies_into_the_archive_account_and_deletes(azure: SimBackend) -> None:
    ref = put(azure, "data", "seis.segy", b"bytes", metadata={"file-owner": "1001"})
    archived = run(azure.set_class(ref, LogicalTier.ARCHIVE))
    assert archived.container == "archive/data"
    head = run(azure.head(archived))
    assert (head.native_class, head.restore_state) == ("Archive", RestoreState.ARCHIVED)
    assert run(azure.list_versions(current(ref))) == []  # original gone, versions too
    with pytest.raises(NotRestored):
        read(azure, archived)
    assert azure.early_deletion_fees == []  # leaving Smart costs nothing [V]


def test_zonal_account_without_archive_account_cannot_archive(clock: VirtualClock) -> None:
    sim = SimBackend(clock, Flavour.AZURE, accounts=[AccountConfig("z", Redundancy.ZRS, True)])
    ref = put(sim, "data", "a", b"x")
    with pytest.raises(UnsupportedOperation, match="archive account"):
        run(sim.set_class(ref, LogicalTier.ARCHIVE))
    with pytest.raises(ValueError):
        SimBackend(
            clock,
            Flavour.AZURE,
            accounts=[AccountConfig("z", Redundancy.ZRS), AccountConfig("a", Redundancy.GZRS)],
            archive_account="a",
        )


def test_worm_data_on_a_smart_account_cannot_copy_and_delete(clock: VirtualClock) -> None:
    sim = SimBackend(
        clock,
        Flavour.AZURE,
        accounts=[
            AccountConfig("live", Redundancy.ZRS, smart_default=True, version_level_worm=True),
            AccountConfig("archive", Redundancy.LRS),
        ],
        archive_account="archive",
    )
    ref = put(sim, "data", "a", b"x")
    run(sim.set_retention(ref, clock.now() + timedelta(days=1)))
    with pytest.raises(Immutable):
        run(sim.set_class(ref, LogicalTier.ARCHIVE))


def test_worm_layout_archives_in_place(worm: SimBackend) -> None:
    caps = worm.capabilities()
    assert caps.in_place_archive and not caps.archive_needs_separate_account
    ref = put(worm, "data", "a", b"x")
    assert run(worm.set_class(ref, LogicalTier.ARCHIVE)) == ref


def test_set_blob_tier_out_of_archive_rehydrates_in_place(
    worm: SimBackend, clock: VirtualClock
) -> None:
    ref = put(worm, "data", "a", b"payload")
    run(worm.set_class(ref, LogicalTier.ARCHIVE))
    run(worm.set_class(ref, LogicalTier.HOT))
    assert run(worm.head(ref)).restore_state is RestoreState.IN_PROGRESS
    with pytest.raises(NotRestored):
        read(worm, ref)
    with pytest.raises(Conflict):
        run(worm.set_class(ref, LogicalTier.COLD))
    clock.advance(timedelta(hours=15))
    head = run(worm.head(ref))
    assert (head.native_class, head.restore_state) == ("Hot", RestoreState.NOT_NEEDED)
    assert read(worm, ref) == b"payload"
    # Left Archive the same day: the 180-day minimum is charged [A].
    fee = worm.early_deletion_fees[-1]
    assert (fee.native_class, fee.minimum_days) == ("Archive", 180)
    assert fee.days_charged == pytest.approx(180)


# --- minimum durations -------------------------------------------------------


def test_early_deletion_is_recorded_not_blocked(worm: SimBackend, clock: VirtualClock) -> None:
    ref = put(worm, "data", "a", b"x" * 10, tier=LogicalTier.WARM)
    clock.advance(timedelta(days=10))
    run(worm.set_class(ref, LogicalTier.HOT))
    [fee] = worm.early_deletion_fees
    assert fee.native_class == "Cool" and fee.days_charged == pytest.approx(20)
    ref2 = put(worm, "data", "b", b"x", tier=LogicalTier.COLD)
    clock.advance(timedelta(days=91))
    run(worm.delete(ref2))
    assert len(worm.early_deletion_fees) == 1  # served its 90 days


# --- S3 classes -------------------------------------------------------------


def test_s3_tier_mapping(s3: SimBackend) -> None:
    expected = {
        None: "STANDARD",
        LogicalTier.HOT: "STANDARD",
        LogicalTier.WARM: "STANDARD_IA",
        LogicalTier.COLD: "GLACIER_IR",
        LogicalTier.ARCHIVE: "GLACIER",
        LogicalTier.SMART: "INTELLIGENT_TIERING",
    }
    for i, (tier, native) in enumerate(expected.items()):
        ref = put(s3, "b", f"k{i}", b"x", tier=tier)
        assert run(s3.head(ref)).native_class == native


def test_s3_glacier_ir_needs_no_restore(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"instant", tier=LogicalTier.COLD)
    assert run(s3.head(ref)).restore_state is RestoreState.NOT_NEEDED
    assert read(s3, ref) == b"instant"


def test_s3_deep_archive_by_native_class(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"x")
    run(s3.set_class(ref, LogicalTier.ARCHIVE, native_class="DEEP_ARCHIVE"))
    assert run(s3.head(ref)).native_class == "DEEP_ARCHIVE"
    with pytest.raises(UnsupportedOperation):
        run(s3.set_class(ref, LogicalTier.ARCHIVE, native_class="STANDARD"))


def test_s3_cannot_reclass_an_unrestored_archive(s3: SimBackend, clock: VirtualClock) -> None:
    ref = put(s3, "b", "k", b"x", tier=LogicalTier.ARCHIVE)
    with pytest.raises(UnsupportedOperation, match="restore"):
        run(s3.set_class(ref, LogicalTier.HOT))
    run(s3.restore(ref, RestoreSpeed.EXPEDITED, days=1))
    clock.advance(timedelta(minutes=5))
    run(s3.set_class(ref, LogicalTier.HOT))
    assert read(s3, ref) == b"x"


def test_capabilities_carry_the_verified_numbers(azure: SimBackend, s3: SimBackend) -> None:
    az = azure.capabilities()
    assert {t: [i.minimum_days for i in infos] for t, infos in az.tiers.items()} == {
        LogicalTier.HOT: [None],
        LogicalTier.WARM: [30],
        LogicalTier.COLD: [90],
        LogicalTier.ARCHIVE: [180],
        LogicalTier.SMART: [None],
    }
    assert az.archive_needs_separate_account and az.explicit_tier_leaves_smart
    [cap] = az.throughput_caps
    assert (cap.limit, cap.unit, cap.scope) == (10, "GiB/hour", "storage account")
    assert all(o.no_promise_above_bytes == 10 * 10**9 for o in az.restore_options)
    aws = s3.capabilities()
    archive = {i.native_class: i for i in aws.tiers[LogicalTier.ARCHIVE]}
    assert archive["GLACIER"].minimum_days == 90 and archive["DEEP_ARCHIVE"].minimum_days == 180
    assert archive["GLACIER"].per_object_overhead_bytes == 40 * 1024
    assert aws.tiers[LogicalTier.WARM][0].min_billable_bytes == 128 * 1024
    assert aws.private_network[0].tls_versions == ("TLSv1.2",)
    expedited = [o for o in aws.restore_options if o.speed.value == "expedited"]
    assert "DEEP_ARCHIVE" not in expedited[0].from_classes
    assert not aws.worm
