from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from omnifile_relay.config import ConfigError, from_env, load_public_key
from omnifile_relay.main import build_app

from .conftest import b64url


def _env(monkeypatch, keys: dict) -> None:
    monkeypatch.setenv("OMNIFILE_RELAY_BOT_APP_ID", "app")
    monkeypatch.setenv("OMNIFILE_RELAY_TENANT_ID", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
    monkeypatch.setenv("OMNIFILE_RELAY_INTENT_PUBLIC_KEYS", json.dumps(keys))


def test_from_env_reads_pem_and_raw_keys(monkeypatch):
    a = ed25519.Ed25519PrivateKey.generate().public_key()
    b = ed25519.Ed25519PrivateKey.generate().public_key()
    pem = a.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    raw = b64url(b.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    _env(monkeypatch, {"k1": pem, "k2": raw})
    s = from_env()
    assert set(s.intent_public_keys) == {"k1", "k2"}
    assert s.tenant_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert s.service_url_hosts == ("smba.trafficmanager.net",)


def test_rsa_intent_key_is_refused():
    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with pytest.raises(ConfigError):
        load_public_key(pem)


def test_missing_keys_refused(monkeypatch):
    _env(monkeypatch, {})
    with pytest.raises(ConfigError):
        from_env()


def test_build_app_requires_mtls_settings(monkeypatch):
    raw = b64url(
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    _env(monkeypatch, {"k": raw})
    with pytest.raises(ConfigError, match="SERVICE_BASE_URL"):
        build_app()
