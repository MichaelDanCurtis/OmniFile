"""A hung test fails rather than hanging the run (and leaving an orphan behind)."""

from __future__ import annotations

import pytest


def test_every_test_has_a_timeout(request: pytest.FixtureRequest) -> None:
    timeout = float(request.config.getini("timeout") or 0)
    assert 0 < timeout <= 600
