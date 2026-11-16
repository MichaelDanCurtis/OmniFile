"""web before its app registration exists: it starts not ready instead of exiting.

Before, `build()` exited on a missing OMNIFILE_WEB_CLIENT_ID, which the first deployment never
has (deploy/entra/register.sh runs after it), so Container Apps restarted web in a loop.
"""

from __future__ import annotations

import logging

import pytest
from starlette.testclient import TestClient

from omnifile_service.web import __main__ as web_main


@pytest.mark.parametrize("missing", web_main.SIGN_IN_SETTINGS)
def test_web_waits_for_its_sign_in_settings(monkeypatch, caplog, missing) -> None:
    monkeypatch.setenv("OMNIFILE_WEB_ORIGIN", "https://portal.example.test")
    monkeypatch.setenv("OMNIFILE_WEB_TENANT_ID", "00000000-0000-0000-0000-00000000000a")
    monkeypatch.setenv("OMNIFILE_WEB_CLIENT_ID", "00000000-0000-0000-0000-00000000000b")
    monkeypatch.setenv(missing, "")  # Bicep passes an empty value before phase 2
    with caplog.at_level(logging.WARNING):
        app = web_main.build()
    lines = [r.getMessage() for r in caplog.records if "not ready" in r.getMessage()]
    assert len(lines) == 1 and missing in lines[0] and "register.sh" in lines[0], lines
    with TestClient(app) as c:
        health = c.get("/healthz")
        me = c.get("/v1/me")
    assert health.status_code == 503, "not ready, so no traffic"
    assert health.json()["status"] == "waiting" and missing in health.json()["reason"]
    assert me.status_code == 503 and me.json()["code"] == "unavailable"
