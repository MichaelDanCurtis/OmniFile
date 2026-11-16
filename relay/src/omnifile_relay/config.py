"""Relay settings, read from environment variables.

Everything that decides *who is trusted* is configuration, never request data.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

# Bot Connector -> bot authentication (public cloud), per Microsoft Learn,
# "Authenticate requests with the Bot Connector API" (checked 2026-10-05).
BOT_OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_ISSUER = "https://api.botframework.com"
BOT_JWKS_HOSTS = ("login.botframework.com",)

# Teams service URLs the Bot Connector may name in `serviceUrl`. Public cloud only by default;
# GCC / GCC High / DoD hosts are opt-in through OMNIFILE_RELAY_SERVICE_URL_HOSTS.
TEAMS_SERVICE_URL_HOSTS = ("smba.trafficmanager.net",)

INTENT_ISSUER = "omnifile-service"
INTENT_AUDIENCE = "omnifile-teams-relay"
INTENT_TYP = "omnifile-intent+jwt"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    # Identity of our bot and tenant (single-tenant bot).
    bot_app_id: str
    tenant_id: str
    # Ed25519 public keys that verify card intents, by key id. The relay never holds a private key.
    intent_public_keys: dict[str, Ed25519PublicKey]

    # Bot Framework token validation.
    openid_metadata_url: str = BOT_OPENID_METADATA_URL
    bot_issuer: str = BOT_ISSUER
    jwks_hosts: tuple[str, ...] = BOT_JWKS_HOSTS
    channel_id: str = "msteams"
    clock_skew_seconds: int = 300  # Microsoft: "industry-standard clock-skew is 5 minutes"
    jwks_refresh_seconds: int = 6 * 3600  # Microsoft: refresh at least once every 24 h
    jwks_min_refresh_seconds: int = 300  # floor on unknown-kid refreshes (anti-amplification)
    jwks_max_stale_seconds: int = 48 * 3600  # serve last-known keys this long if Microsoft is down

    # serviceUrl allowlist (exact hostnames, https only, default port only).
    service_url_hosts: tuple[str, ...] = TEAMS_SERVICE_URL_HOSTS

    # Intent tokens.
    intent_issuer: str = INTENT_ISSUER
    intent_audience: str = INTENT_AUDIENCE
    intent_leeway_seconds: int = 60
    intent_max_lifetime_seconds: int = 14 * 86400

    # Request limits.
    max_body_bytes: int = 64 * 1024
    body_read_timeout_seconds: float = 2.0
    per_ip_rate: float = 20.0
    per_ip_burst: int = 40
    per_user_rate: float = 1.0
    per_user_burst: int = 5

    # Inward forwarding (HTTPS + mTLS to the private `api`).
    service_base_url: str | None = None
    service_client_cert: str | None = None
    service_client_key: str | None = None
    service_ca_bundle: str | None = None
    forward_timeout_seconds: float = 3.0  # Teams waits 5 s for an invoke response

    def __post_init__(self) -> None:
        if not self.bot_app_id:
            raise ConfigError("bot_app_id is required")
        if not self.tenant_id:
            raise ConfigError("tenant_id is required")
        if not self.intent_public_keys:
            raise ConfigError("at least one intent public key is required")
        if self.jwks_refresh_seconds > 24 * 3600:
            raise ConfigError("jwks_refresh_seconds must be <= 24 h (Microsoft requirement)")
        if self.forward_timeout_seconds >= 4.5:
            raise ConfigError("forward_timeout_seconds must leave room inside Teams' 5 s limit")


def load_public_key(text: str) -> Ed25519PublicKey:
    """Accept a PEM SubjectPublicKeyInfo or a base64url raw 32-byte Ed25519 key."""
    text = text.strip()
    if text.startswith("-----BEGIN"):
        key = load_pem_public_key(text.encode())
    else:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        key = Ed25519PublicKey.from_public_bytes(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ConfigError("intent keys must be Ed25519")
    return key


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def from_env() -> Settings:
    """Build settings from OMNIFILE_RELAY_* environment variables."""
    env = os.environ
    keys_raw = env.get("OMNIFILE_RELAY_INTENT_PUBLIC_KEYS", "")
    try:
        keys_json = json.loads(keys_raw) if keys_raw else {}
    except json.JSONDecodeError as exc:
        raise ConfigError("OMNIFILE_RELAY_INTENT_PUBLIC_KEYS must be JSON {kid: key}") from exc
    keys = {kid: load_public_key(value) for kid, value in keys_json.items()}
    return Settings(
        bot_app_id=env.get("OMNIFILE_RELAY_BOT_APP_ID", ""),
        tenant_id=env.get("OMNIFILE_RELAY_TENANT_ID", "").lower(),
        intent_public_keys=keys,
        service_url_hosts=_csv("OMNIFILE_RELAY_SERVICE_URL_HOSTS", TEAMS_SERVICE_URL_HOSTS),
        service_base_url=env.get("OMNIFILE_RELAY_SERVICE_BASE_URL"),
        service_client_cert=env.get("OMNIFILE_RELAY_SERVICE_CLIENT_CERT"),
        service_client_key=env.get("OMNIFILE_RELAY_SERVICE_CLIENT_KEY"),
        service_ca_bundle=env.get("OMNIFILE_RELAY_SERVICE_CA_BUNDLE"),
    )
