"""Native classes and `Capabilities` for sim's two flavours.

Every number here is a verified fact: [V] docs/research/2026-09-26-verification.md,
[W] docs/design/tiers-versioning-worm.md, [A] AGENTS.md (Backends table and
Azure backend bullets). Change the source note first, then this file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..base import (
    BackendKind,
    Capabilities,
    CredentialMode,
    LogicalTier,
    PrivateNetworkOption,
    RestoreOption,
    RestoreSpeed,
    ThroughputCap,
    TierInfo,
)

KB = 1024
MB = 1024 * 1024
GiB = 1024**3
GB = 10**9

# --- Azure Blob ----------------------------------------------------------

AZ_HOT = "Hot"
AZ_COOL = "Cool"
AZ_COLD = "Cold"
AZ_ARCHIVE = "Archive"
AZ_SMART = "Smart"

#: Azure's "up to 15 h" rehydrate holds only under 10 GB; above it, no promise [A].
AZ_NO_PROMISE_ABOVE = 10 * GB
#: Priority (High) rehydrate throughput per storage account [A].
AZ_HIGH_PRIORITY_BYTES_PER_HOUR = 10 * GiB

AZURE_TIERS: Mapping[LogicalTier, Sequence[TierInfo]] = {
    LogicalTier.HOT: [TierInfo(LogicalTier.HOT, AZ_HOT, None, False)],
    LogicalTier.WARM: [TierInfo(LogicalTier.WARM, AZ_COOL, 30, False)],
    LogicalTier.COLD: [TierInfo(LogicalTier.COLD, AZ_COLD, 90, False)],
    LogicalTier.ARCHIVE: [
        TierInfo(
            LogicalTier.ARCHIVE,
            AZ_ARCHIVE,
            180,
            True,
            note="rehydrate by copy; priority capped at 10 GiB/h per account; "
            "not allowed on ZRS/GZRS/RA-GZRS",
        )
    ],
    LogicalTier.SMART: [
        TierInfo(
            LogicalTier.SMART,
            AZ_SMART,
            None,
            False,
            note="ZRS/GZRS/RA-GZRS only; no Archive; account default tier, so uploads set no "
            "tier; an explicit tier leaves Smart for good; objects <=128 KiB never tier down",
        )
    ],
}

AZURE_RESTORE_OPTIONS: Sequence[RestoreOption] = [
    RestoreOption(
        RestoreSpeed.STANDARD,
        frozenset({AZ_ARCHIVE}),
        (0.0, 15.0),
        no_promise_above_bytes=AZ_NO_PROMISE_ABOVE,
        note="Copy Blob rehydrate; 'up to 15 h' only under 10 GB",
    ),
    RestoreOption(
        RestoreSpeed.HIGH,
        frozenset({AZ_ARCHIVE}),
        (0.0, 1.0),
        no_promise_above_bytes=AZ_NO_PROMISE_ABOVE,
        note="may complete under 1 h for objects under 10 GB; shares 10 GiB/h per account",
    ),
]

AZURE_CAPS: Sequence[ThroughputCap] = [
    ThroughputCap("priority rehydrate", 10, "GiB/hour", "storage account"),
]

AZURE_PRIVATE = [
    PrivateNetworkOption("azure_private_endpoint", ("TLSv1.2", "TLSv1.3"), "one per account"),
]

AZURE_CREDENTIALS = [
    CredentialMode.AZURE_USER_DELEGATION_SAS,
    CredentialMode.AZURE_ARC_MANAGED_IDENTITY,
]

# --- Amazon S3 -----------------------------------------------------------

S3_STANDARD = "STANDARD"
S3_STANDARD_IA = "STANDARD_IA"
S3_GLACIER_IR = "GLACIER_IR"
S3_GLACIER = "GLACIER"  # Glacier Flexible Retrieval
S3_DEEP_ARCHIVE = "DEEP_ARCHIVE"
S3_INTELLIGENT_TIERING = "INTELLIGENT_TIERING"

#: Intelligent-Tiering's opt-in archive access tiers (an object attribute, not a class).
IT_ARCHIVE_ACCESS = "ARCHIVE_ACCESS"
IT_DEEP_ARCHIVE_ACCESS = "DEEP_ARCHIVE_ACCESS"

S3_EXPEDITED_MAX_BYTES = 250 * MB  # "only for objects under 250 MB" [V]
S3_MIN_BILLABLE = 128 * KB  # Standard-IA, Glacier IR [V]
S3_ARCHIVE_OVERHEAD = 40 * KB  # Flexible Retrieval, Deep Archive [V]
S3_IT_MIN_MONITORED = 128 * KB  # objects under 128 KB are never tiered [V]
S3_RESTORE_REQUESTS_PER_SECOND = 1000  # per account, ThrottlingException above [V]

S3_TIERS: Mapping[LogicalTier, Sequence[TierInfo]] = {
    LogicalTier.HOT: [TierInfo(LogicalTier.HOT, S3_STANDARD, None, False)],
    LogicalTier.WARM: [
        TierInfo(LogicalTier.WARM, S3_STANDARD_IA, 30, False, min_billable_bytes=S3_MIN_BILLABLE)
    ],
    LogicalTier.COLD: [
        TierInfo(
            LogicalTier.COLD,
            S3_GLACIER_IR,
            90,
            False,
            min_billable_bytes=S3_MIN_BILLABLE,
            note="no restore needed",
        )
    ],
    LogicalTier.ARCHIVE: [
        TierInfo(
            LogicalTier.ARCHIVE,
            S3_GLACIER,
            90,
            True,
            per_object_overhead_bytes=S3_ARCHIVE_OVERHEAD,
            note="Glacier Flexible Retrieval; RestoreObject makes a temporary copy for Days",
        ),
        TierInfo(
            LogicalTier.ARCHIVE,
            S3_DEEP_ARCHIVE,
            180,
            True,
            per_object_overhead_bytes=S3_ARCHIVE_OVERHEAD,
            note="no Expedited restore",
        ),
    ],
    LogicalTier.SMART: [
        TierInfo(
            LogicalTier.SMART,
            S3_INTELLIGENT_TIERING,
            None,
            False,
            note="objects under 128 KB never tiered; archive access tiers restore with no "
            "Days and never expire",
        )
    ],
}

_FLEX_AND_IT = frozenset({S3_GLACIER, S3_INTELLIGENT_TIERING})
S3_RESTORE_OPTIONS: Sequence[RestoreOption] = [
    RestoreOption(
        RestoreSpeed.EXPEDITED,
        _FLEX_AND_IT,
        (1 / 60, 5 / 60),
        max_object_bytes=S3_EXPEDITED_MAX_BYTES,
        note="not Deep Archive; may not be accepted under high demand",
    ),
    RestoreOption(RestoreSpeed.STANDARD, _FLEX_AND_IT, (3.0, 5.0)),
    RestoreOption(RestoreSpeed.STANDARD, frozenset({S3_DEEP_ARCHIVE}), (0.0, 12.0)),
    RestoreOption(RestoreSpeed.BULK, _FLEX_AND_IT, (5.0, 12.0)),
    RestoreOption(RestoreSpeed.BULK, frozenset({S3_DEEP_ARCHIVE}), (0.0, 48.0)),
]

S3_CAPS: Sequence[ThroughputCap] = [
    ThroughputCap(
        "restore requests",
        S3_RESTORE_REQUESTS_PER_SECOND,
        "requests/s",
        "AWS account",
        note="shared by Batch Operations and single restores (sharing INFERRED)",
    ),
    ThroughputCap("restore throughput", 1, "PB/day", "AWS account", note="1-2 PB/day"),
]

S3_PRIVATE = [
    PrivateNetworkOption(
        "s3_privatelink_interface_endpoint",
        ("TLSv1.2",),
        "no TLS 1.3 on interface endpoints; reachable over Direct Connect or VPN",
    ),
]

S3_CREDENTIALS = [CredentialMode.S3_ROLES_ANYWHERE, CredentialMode.S3_PRESIGNED_URL]


def native_info(tiers: Mapping[LogicalTier, Sequence[TierInfo]]) -> dict[str, TierInfo]:
    return {i.native_class: i for infos in tiers.values() for i in infos}


def azure_capabilities(*, smart: bool, worm: bool, in_place_archive: bool) -> Capabilities:
    """`smart`: the binding's primary account is zonal with Smart as default
    (two-account layout). Otherwise it is the single LRS/GRS account layout,
    WORM or not [W]."""
    tiers = dict(AZURE_TIERS)
    if not smart:
        del tiers[LogicalTier.SMART]
    return Capabilities(
        kind=BackendKind.SIM,
        tiers=tiers,
        restore_options=AZURE_RESTORE_OPTIONS,
        throughput_caps=AZURE_CAPS,
        private_network=AZURE_PRIVATE,
        credential_modes=AZURE_CREDENTIALS,
        versioning=True,
        worm=worm,
        in_place_archive=in_place_archive,
        archive_needs_separate_account=smart,
        explicit_tier_leaves_smart=smart,
    )


def s3_capabilities() -> Capabilities:
    return Capabilities(
        kind=BackendKind.SIM,
        tiers=S3_TIERS,
        restore_options=S3_RESTORE_OPTIONS,
        throughput_caps=S3_CAPS,
        private_network=S3_PRIVATE,
        credential_modes=S3_CREDENTIALS,
        versioning=True,
        worm=False,  # S3 Object Lock is noted, not built (S3 is a migration source) [W]
        in_place_archive=True,
        archive_needs_separate_account=False,
        explicit_tier_leaves_smart=False,
    )
