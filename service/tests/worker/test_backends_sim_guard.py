"""Sim backends are dev and test only (OMNIFILE_DEV_MODE=1)."""

from __future__ import annotations

import pytest

from omnifile_service.api.dataplane import DataPlanes
from omnifile_service.api.problems import ApiError
from omnifile_service.worker.backends import sim_refusal


def test_a_sim_row_needs_dev_mode() -> None:
    kinds = {1: "azure_blob", 2: "sim", 3: "s3", 4: "sim"}
    why = sim_refusal(kinds, {})
    assert why is not None and "[2, 4]" in why and "OMNIFILE_DEV_MODE=1" in why
    assert sim_refusal(kinds, {"OMNIFILE_DEV_MODE": "0"}) is not None
    assert sim_refusal(kinds, {"OMNIFILE_DEV_MODE": "1"}) is None
    assert sim_refusal({1: "azure_blob", 3: "s3"}, {}) is None


def test_the_api_refuses_a_sim_http_plane_outside_dev_mode() -> None:
    planes = DataPlanes(None, allow_sim=False)
    with pytest.raises(ApiError) as e:
        planes.get(9, "sim", {"data_plane": "sim_http", "url": "http://x", "key_file": "/k"})
    assert "dev mode only" in str(e.value)
