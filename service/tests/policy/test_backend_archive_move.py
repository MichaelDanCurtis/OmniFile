"""sim's two-account archive move checks its copy before deleting the
original (M3 review, blocker 2)."""

from __future__ import annotations

import pytest

from omnifile_service.backends.base import BackendError, LogicalTier
from omnifile_service.backends.sim import CopyFault, SimBackend, VirtualClock

from .support import _one, run


def test_short_archive_copy_keeps_the_original() -> None:
    sim = SimBackend.azure_two_account(VirtualClock())
    ref = run(sim.put("data", "f", _one(b"seismic data"), None, {}))
    sim.faults.corrupt_next_copy(CopyFault.PARTIAL)
    with pytest.raises(BackendError, match="does not match"):
        run(sim.set_class(ref, LogicalTier.ARCHIVE))
    assert run(sim.head(ref)).size == len(b"seismic data")
    page = run(sim.list("archive/data", "", None))
    assert [h.ref.key for h in page.items] == []
