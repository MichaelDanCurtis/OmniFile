"""The simulated storage backend ("sim"): Azure's and S3's rules on a virtual clock.

See README.md in this directory. Typical use:

    clock = VirtualClock()
    sim = SimBackend.azure_two_account(clock)   # or .azure_worm(clock), .s3(clock)
    ref = await sim.put("data", "a.segy", stream, None, {})
    clock.advance(timedelta(hours=15))
"""

from .backend import (
    AccountConfig,
    ChecksumMismatch,
    ComputedChecksum,
    Conflict,
    EarlyDeletionFee,
    Flavour,
    Redundancy,
    SimBackend,
    SimTimings,
)
from .clock import OffsetClock, VirtualClock, dev_clock
from .crc64 import crc64nvme, crc64nvme_combine
from .credentials import CredentialRejected
from .faults import CopyFault, FaultInjector, InjectedError, Op, Unavailable

__all__ = [
    "AccountConfig",
    "ChecksumMismatch",
    "ComputedChecksum",
    "Conflict",
    "CopyFault",
    "CredentialRejected",
    "EarlyDeletionFee",
    "FaultInjector",
    "Flavour",
    "InjectedError",
    "OffsetClock",
    "Op",
    "Redundancy",
    "SimBackend",
    "SimTimings",
    "Unavailable",
    "VirtualClock",
    "crc64nvme",
    "crc64nvme_combine",
    "dev_clock",
]
