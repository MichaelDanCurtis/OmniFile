"""AWS credentials for the service with no stored secret.

The service gets temporary AWS credentials with STS
``AssumeRoleWithWebIdentity``. The web identity token is an Entra access
token for the service's Azure managed identity, issued for the one audience
the client's IAM OIDC provider trusts (``web_identity_audience``, an
``api://`` application ID URI; `config.check_audience`). The role is scoped
to the binding's buckets (deploy/aws).

- STS is called through the account's STS endpoint (an interface endpoint
  with private networking; decision 22). AssumeRoleWithWebIdentity is
  unsigned, so no credentials are needed to get credentials.
- Credentials refresh before they expire (botocore's refreshable
  credentials, timed on the backend's clock).
- The session's resolver holds this provider alone, so a stray environment
  key, profile or instance role is never picked up instead.
- ``default`` (botocore's own chain) is for emulators on loopback only
  (`S3Config` refuses it towards anything else).
- `botocore_session_for` is the same for synchronous clients: PyIceberg
  signs its S3 Tables REST calls with a botocore session
  (sources/metadata_s3.py, run in a worker thread). It takes the managed
  identity's token through azure-identity's synchronous credential.

VERIFY on real AWS (real-aws-checks.md): the Entra token's audience and
issuer as IAM sees them, and the STS regional interface endpoint.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiobotocore.config import AioConfig
from aiobotocore.credentials import (
    AioCredentialResolver,
    AioDeferredRefreshableCredentials,
    AioRefreshableCredentials,
)
from aiobotocore.session import AioSession
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig

from ..base import BackendError, Clock, SystemClock
from .config import S3Config, check_audience

#: An async callable returning a fresh web identity token (a JWT).
TokenProvider = Callable[[], Awaitable[str]]


def entra_token_provider(credential: Any, audience: str) -> TokenProvider:
    """A token provider from an azure-identity async credential (the
    service's managed identity). `audience` is the deployment's fixed
    application ID URI that the client's IAM OIDC provider trusts: never a
    resource such as ARM, whose token STS would then receive. With no
    credential, `DefaultAzureCredential` (managed identity in Azure) is made
    on first use, as the Azure backend does."""
    check_audience(audience)
    scope = f"{audience.rstrip('/')}/.default"
    held: list[Any] = [credential]

    async def token() -> str:
        if held[0] is None:
            from azure.identity.aio import DefaultAzureCredential

            held[0] = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        got = await held[0].get_token(scope)
        return got.token

    return token


def _tls_session_cls(config: S3Config) -> type:
    """An aiohttp session class whose TLS context is the config's: TLS 1.2
    allowed (interface endpoints have no 1.3 [V]), 1.2 only when pinned.
    aiobotocore builds its own context and refuses an `ssl_context` in
    `connector_args` next to it, so the context comes from here."""
    from aiobotocore.httpsession import AIOHTTPSession

    class _TlsSession(AIOHTTPSession):
        def _get_ssl_context(self) -> Any:
            return config.ssl_context()

    return _TlsSession


class WebIdentityCredentials:
    """Refreshable AWS credentials from a web identity token provider."""

    def __init__(
        self,
        session: Any,
        config: S3Config,
        token_provider: TokenProvider,
        clock: Clock | None = None,
        *,
        session_name: str = "omnifile-service",
        on_client: Callable[[Any], None] | None = None,
    ) -> None:
        if not config.role_arn:
            raise BackendError("web identity needs the account's role_arn (deployment settings)")
        if config.web_identity_audience is None:
            raise BackendError("web identity needs the deployment's audience")
        check_audience(config.web_identity_audience)
        self._session = session
        self._config = config
        self._token = token_provider
        self._clock = clock or SystemClock()
        self._session_name = session_name
        self._on_client = on_client
        self.calls = 0

    async def fetch(self) -> dict[str, str]:
        """One AssumeRoleWithWebIdentity call, in botocore's metadata shape."""
        token = await self._token()
        cfg = AioConfig(
            signature_version=UNSIGNED,
            retries={"max_attempts": self._config.max_attempts, "mode": "standard"},
            http_session_cls=_tls_session_cls(self._config),
        )
        async with self._session.create_client(
            "sts",
            region_name=self._config.region,
            endpoint_url=self._config.sts_endpoint_url,
            config=cfg,
        ) as sts:
            if self._on_client is not None:
                self._on_client(sts)
            resp = await sts.assume_role_with_web_identity(
                RoleArn=self._config.role_arn,
                RoleSessionName=self._session_name,
                WebIdentityToken=token,
                DurationSeconds=self._config.session_duration_s,
            )
        self.calls += 1
        c = resp["Credentials"]
        expiry: datetime = c["Expiration"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return {
            "access_key": c["AccessKeyId"],
            "secret_key": c["SecretAccessKey"],
            "token": c["SessionToken"],
            "expiry_time": expiry.isoformat(),
        }

    def now(self) -> datetime:
        return self._clock.now()

    async def credentials(self) -> AioRefreshableCredentials:
        m = await self.fetch()
        return AioRefreshableCredentials(
            access_key=m["access_key"],
            secret_key=m["secret_key"],
            token=m["token"],
            expiry_time=datetime.fromisoformat(m["expiry_time"]),
            refresh_using=self.fetch,
            method="assume-role-with-web-identity",
            time_fetcher=self.now,
        )


class WebIdentityProvider:
    """A botocore credential provider over `WebIdentityCredentials`."""

    METHOD = "assume-role-with-web-identity"
    CANONICAL_NAME = "omnifile-web-identity"

    def __init__(self, web_identity: WebIdentityCredentials) -> None:
        self.web_identity = web_identity

    async def load(self) -> AioDeferredRefreshableCredentials:
        # Deferred: botocore resolves credentials when it builds a client,
        # but STS is called only when a request is first signed.
        wi = self.web_identity
        return AioDeferredRefreshableCredentials(
            refresh_using=wi.fetch, method=self.METHOD, time_fetcher=wi.now
        )


def _no_token() -> TokenProvider:
    async def token() -> str:
        raise BackendError("no Azure credential to get a web identity token from")

    return token


def session_for(
    config: S3Config,
    *,
    token_provider: TokenProvider | None = None,
    clock: Clock | None = None,
    on_client: Callable[[Any], None] | None = None,
) -> AioSession:
    """The SDK session for a binding. With ``web_identity`` its resolver
    holds the web identity provider alone; ``default`` (loopback emulators
    only) keeps botocore's chain."""
    session = AioSession()
    if config.credentials == "web_identity":
        assert config.web_identity_audience is not None  # S3Config checks it
        provider = WebIdentityProvider(
            WebIdentityCredentials(
                session,
                config,
                token_provider or _no_token(),
                clock,
                on_client=on_client,
            )
        )
        session.register_component("credential_provider", AioCredentialResolver([provider]))
    return session


#: A synchronous callable returning a fresh web identity token (a JWT).
SyncTokenProvider = Callable[[], str]


def entra_token_provider_sync(credential: Any, audience: str) -> SyncTokenProvider:
    """`entra_token_provider` for synchronous clients: an azure-identity
    synchronous credential (made on first use when none is given)."""
    check_audience(audience)
    scope = f"{audience.rstrip('/')}/.default"
    held: list[Any] = [credential]

    def token() -> str:
        if held[0] is None:
            from azure.identity import DefaultAzureCredential

            held[0] = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return held[0].get_token(scope).token

    return token


def botocore_session_for(
    config: S3Config,
    *,
    token_provider: SyncTokenProvider | None = None,
    on_client: Callable[[Any], None] | None = None,
) -> Any:
    """A synchronous botocore session with the binding's credentials: web
    identity from the managed identity alone, or (``default``, loopback
    emulators only) botocore's chain."""
    import botocore.session
    from botocore.credentials import (
        CredentialResolver,
        DeferredRefreshableCredentials,
    )

    session = botocore.session.Session()
    session.set_config_variable("region", config.region)
    if config.credentials != "web_identity":
        return session
    assert config.web_identity_audience is not None  # S3Config checks it
    if not config.role_arn:
        raise BackendError("web identity needs the account's role_arn (deployment settings)")
    token = token_provider or entra_token_provider_sync(None, config.web_identity_audience)
    sts_session = botocore.session.Session()

    def fetch() -> dict[str, str]:
        sts = sts_session.create_client(
            "sts",
            region_name=config.region,
            endpoint_url=config.sts_endpoint_url,
            config=BotoConfig(signature_version=UNSIGNED),
            verify=config.ca_bundle or True,
        )
        if on_client is not None:
            on_client(sts)
        resp = sts.assume_role_with_web_identity(
            RoleArn=config.role_arn,
            RoleSessionName="omnifile-service",
            WebIdentityToken=token(),
            DurationSeconds=config.session_duration_s,
        )
        c = resp["Credentials"]
        expiry: datetime = c["Expiration"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return {
            "access_key": c["AccessKeyId"],
            "secret_key": c["SecretAccessKey"],
            "token": c["SessionToken"],
            "expiry_time": expiry.isoformat(),
        }

    class _Provider:
        METHOD = WebIdentityProvider.METHOD
        CANONICAL_NAME = WebIdentityProvider.CANONICAL_NAME

        def load(self) -> Any:
            return DeferredRefreshableCredentials(refresh_using=fetch, method=self.METHOD)

    session.register_component("credential_provider", CredentialResolver([_Provider()]))
    return session
