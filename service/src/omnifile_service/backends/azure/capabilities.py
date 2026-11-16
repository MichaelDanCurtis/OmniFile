"""`Capabilities` for the real Azure Blob backend.

The tier tables, restore options, caps and private-network facts are the
same verified facts sim uses ([V] verification note, [A] AGENTS.md, [W] WORM
note), so they are imported from one place, `..sim.capabilities`, rather
than copied: the real backend and sim can't drift apart. Only `kind` and the
layout flags are set here.
"""

from __future__ import annotations

from dataclasses import replace

from ..base import BackendKind, Capabilities, LogicalTier
from ..sim.capabilities import (
    AZ_ARCHIVE,
    AZ_COLD,
    AZ_COOL,
    AZ_HOT,
    AZ_NO_PROMISE_ABOVE,
    AZ_SMART,
    azure_capabilities,
)
from .config import AzureBlobConfig

__all__ = [
    "AZ_ARCHIVE",
    "AZ_COLD",
    "AZ_COOL",
    "AZ_HOT",
    "AZ_NO_PROMISE_ABOVE",
    "AZ_SMART",
    "LOGICAL_TO_NATIVE",
    "NATIVE_TO_LOGICAL",
    "ONLINE_TIERS",
    "capabilities_for",
]

LOGICAL_TO_NATIVE = {
    LogicalTier.HOT: AZ_HOT,
    LogicalTier.WARM: AZ_COOL,
    LogicalTier.COLD: AZ_COLD,
    LogicalTier.ARCHIVE: AZ_ARCHIVE,
    LogicalTier.SMART: AZ_SMART,
}
NATIVE_TO_LOGICAL = {v.lower(): k for k, v in LOGICAL_TO_NATIVE.items()}
ONLINE_TIERS = frozenset({AZ_HOT, AZ_COOL, AZ_COLD, AZ_SMART})


def capabilities_for(config: AzureBlobConfig) -> Capabilities:
    caps = azure_capabilities(
        smart=config.smart,
        worm=config.worm,
        in_place_archive=config.layout == "single_account",
    )
    return replace(
        caps,
        kind=BackendKind.AZURE_BLOB,
        versioning=config.versioning,
        archive_needs_separate_account=config.layout == "two_account",
    )
