"""`Capabilities` for the real Amazon S3 backend.

The tier table, restore options, caps and private-network facts are the
verified S3 column ([V] verification note) that sim uses, imported from
`..sim.capabilities` so the real backend and sim can't drift apart (as the
Azure backend does). Only `kind` and the binding's versioning are set here.
"""

from __future__ import annotations

from dataclasses import replace

from ..base import BackendKind, Capabilities, LogicalTier
from ..sim.capabilities import (
    IT_ARCHIVE_ACCESS,
    IT_DEEP_ARCHIVE_ACCESS,
    S3_DEEP_ARCHIVE,
    S3_EXPEDITED_MAX_BYTES,
    S3_GLACIER,
    S3_GLACIER_IR,
    S3_INTELLIGENT_TIERING,
    S3_STANDARD,
    S3_STANDARD_IA,
    s3_capabilities,
)
from .config import S3Config

__all__ = [
    "ARCHIVED_CLASSES",
    "IT_ARCHIVE_ACCESS",
    "IT_DEEP_ARCHIVE_ACCESS",
    "NATIVE_TO_LOGICAL",
    "S3_DEEP_ARCHIVE",
    "S3_EXPEDITED_MAX_BYTES",
    "S3_GLACIER",
    "S3_INTELLIGENT_TIERING",
    "S3_STANDARD",
    "capabilities_for",
    "native_class_for",
    "s3_backend_capabilities",
]

NATIVE_TO_LOGICAL = {
    S3_STANDARD: LogicalTier.HOT,
    S3_STANDARD_IA: LogicalTier.WARM,
    S3_GLACIER_IR: LogicalTier.COLD,
    S3_GLACIER: LogicalTier.ARCHIVE,
    S3_DEEP_ARCHIVE: LogicalTier.ARCHIVE,
    S3_INTELLIGENT_TIERING: LogicalTier.SMART,
}
#: Classes whose reads need a restore first (Glacier IR needs none) [V].
ARCHIVED_CLASSES = frozenset({S3_GLACIER, S3_DEEP_ARCHIVE})


def capabilities_for(config: S3Config) -> Capabilities:
    return s3_backend_capabilities(versioning=config.versioning)


def s3_backend_capabilities(*, versioning: bool = True) -> Capabilities:
    """For a catalog row whose config may be partial (the api's recall facts)."""
    return replace(s3_capabilities(), kind=BackendKind.S3, versioning=versioning)


def native_class_for(
    caps: Capabilities, tier: LogicalTier, native_class: str | None, config: S3Config
) -> str:
    """The S3 storage class for `tier`: `native_class` if given and offered,
    the configured archive class for ARCHIVE, else the tier's first class."""
    infos = caps.tiers.get(tier)
    if not infos:
        raise ValueError(f"S3 offers no {tier} tier")
    offered = [i.native_class for i in infos]
    if native_class is not None:
        if native_class not in offered:
            raise ValueError(f"{native_class} is not an S3 {tier} class ({', '.join(offered)})")
        return native_class
    if tier is LogicalTier.ARCHIVE:
        return config.archive_class
    return offered[0]
