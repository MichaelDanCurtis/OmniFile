"""Before phase 2 the relay starts not ready instead of exiting (main.build_or_wait).

The first deployment has no relay certificate in Key Vault yet, so the mTLS settings are
absent, and before, `omnifile-relay` exited and Container Apps restarted it in a loop.
"""

from __future__ import annotations

import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.testclient import TestClient

from omnifile_relay.main import build_or_wait

from .conftest import b64url


def _key() -> str:
    pub = ed25519.Ed25519PrivateKey.generate().public_key()
    return b64url(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def test_without_its_certificate_the_relay_waits(monkeypatch, caplog):
    monkeypatch.setenv("OMNIFILE_RELAY_BOT_APP_ID", "app")
    monkeypatch.setenv("OMNIFILE_RELAY_TENANT_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("OMNIFILE_RELAY_INTENT_PUBLIC_KEYS", json.dumps({"k": _key()}))
    monkeypatch.setenv("OMNIFILE_RELAY_SERVICE_BASE_URL", "https://api.example.test:8443")
    for name in ("CLIENT_CERT", "CLIENT_KEY", "CA_BUNDLE"):
        monkeypatch.delenv(f"OMNIFILE_RELAY_SERVICE_{name}", raising=False)
    with caplog.at_level(logging.WARNING):
        app = build_or_wait()
    lines = [r.getMessage() for r in caplog.records if "not ready" in r.getMessage()]
    assert len(lines) == 1 and "OMNIFILE_RELAY_SERVICE_CLIENT_CERT" in lines[0], lines
    with TestClient(app) as c:
        health = c.get("/healthz")
        click = c.post("/api/messages", json={})
    assert health.status_code == 503 and health.json()["status"] == "waiting"
    assert "OMNIFILE_RELAY_SERVICE_CLIENT_CERT" in health.json()["reason"]
    assert click.status_code == 503 and click.headers["retry-after"] == "60"


def test_without_intent_keys_the_relay_waits(monkeypatch, caplog):
    """The template's default `relayIntentPublicKeys = '{}'` before the keys exist."""
    monkeypatch.setenv("OMNIFILE_RELAY_BOT_APP_ID", "app")
    monkeypatch.setenv("OMNIFILE_RELAY_TENANT_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("OMNIFILE_RELAY_INTENT_PUBLIC_KEYS", "{}")
    with caplog.at_level(logging.WARNING):
        app = build_or_wait()
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 503
    assert any("intent public key" in r.getMessage() for r in caplog.records)
