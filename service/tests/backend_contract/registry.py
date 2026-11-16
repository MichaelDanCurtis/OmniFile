"""Backends the contract suite runs against.

Each entry maps a name to a factory taking a clock. Add sim, and later the
real Azure and S3 backends (behind an opt-in environment variable, since they
need cloud accounts), here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from omnifile_service.backends.base import Clock, StorageBackend
from omnifile_service.backends.sim import SimBackend

from .memory_backend import MemoryBackend

BackendFactory = Callable[[Clock], StorageBackend]

FACTORIES: dict[str, BackendFactory] = {
    "memory": lambda clock: MemoryBackend(clock),
    # sim, with a realistic change-feed lag and small pages so paging is exercised.
    "sim-azure": lambda clock: SimBackend.azure_two_account(
        clock, feed_lag=timedelta(minutes=2), list_page_size=2, read_chunk_size=4
    ),
    "sim-azure-worm": lambda clock: SimBackend.azure_worm(
        clock, feed_lag=timedelta(minutes=2), list_page_size=2, read_chunk_size=4
    ),
    "sim-s3": lambda clock: SimBackend.s3(
        clock, feed_lag=timedelta(minutes=2), list_page_size=2, read_chunk_size=4
    ),
}

# The real Azure backend, against a real dev subscription. Opt-in only:
# OMNIFILE_TEST_AZURE=1 plus the account URLs (see backends/azure/testing.py).
# Adds "azure" (two-account, Smart) and, with a WORM account, "azure-worm".
from omnifile_service.backends.azure.testing import contract_factories  # noqa: E402

FACTORIES.update(contract_factories())
