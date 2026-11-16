"""S3 config and credentials: no keys anywhere, and nothing that decides
where a request goes or which identity it uses comes from a catalog row
(ported from line B's M5 review fix B1, 88c525b)."""

from __future__ import annotations

import asyncio
import json
import ssl
from datetime import timedelta
from typing import Any

import pytest
from backend_contract.clock import ManualClock

from omnifile_service.backends.base import (
    BackendKind,
    CredentialMode,
    EdgeAccess,
    ObjectRef,
    UnsupportedOperation,
)
from omnifile_service.backends.s3 import (
    AwsDeployment,
    BatchOps,
    ConfigError,
    S3Backend,
    S3Config,
    capabilities_for,
)
from omnifile_service.backends.s3.config import (
    ENV_ACCOUNTS,
    ENV_ACCOUNTS_FILE,
    ENV_AUDIENCE,
    ENV_PRIVATE,
    check_audience,
    check_row_keys,
    validate_backend_config,
)
from omnifile_service.backends.s3.credentials import (
    WebIdentityCredentials,
    entra_token_provider,
    session_for,
)

from .conftest import AUDIENCE, ROLE, private_config
from .s3_fakes import FakeAws

ACCT = "123456789012"
S3_VPCE = "https://bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com"
STS_VPCE = "https://vpce-0123.sts.eu-west-1.vpce.amazonaws.com"
SQS_VPCE = "https://vpce-0def.sqs.eu-west-1.vpce.amazonaws.com"
CTL_VPCE = "https://control.vpce-0abc.s3.eu-west-1.vpce.amazonaws.com"

ACCOUNT: dict[str, Any] = {
    "region": "eu-west-1",
    "endpoints": {"s3": S3_VPCE, "sts": STS_VPCE, "sqs": SQS_VPCE, "s3_control": CTL_VPCE},
    "role_arn": ROLE,
    "event_queues": {"*": "https://sqs.eu-west-1.amazonaws.com/123456789012/omnifile-events"},
    "batch": {"role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-batch", "manifest_bucket": "ops"},
    "staging_bucket": "ops",
}
ROW = {"account_id": ACCT, "region": "eu-west-1", "buckets": ["seismic-raw"]}


def _deployment(**kw: Any) -> AwsDeployment:
    return AwsDeployment.from_mapping({ACCT: {**ACCOUNT, **kw}}, web_identity_audience=AUDIENCE)


def _cfg(**kw: Any) -> S3Config:
    base: dict[str, Any] = {
        "region": "eu-west-1",
        "account_id": ACCT,
        "endpoint_url": S3_VPCE,
        "sts_endpoint_url": STS_VPCE,
        "role_arn": ROLE,
        "web_identity_audience": AUDIENCE,
    }
    base.update(kw)
    return S3Config(**base)


# --- private networking and TLS ------------------------------------------------


def test_private_network_refuses_public_hosts_and_missing_endpoints() -> None:
    for url in ("https://s3.eu-west-1.amazonaws.com", "https://attacker.example"):
        with pytest.raises(ConfigError, match="VPC interface endpoint"):
            _cfg(endpoint_url=url)
    with pytest.raises(ConfigError, match="VPC interface endpoint"):
        _cfg(sts_endpoint_url="https://sts.eu-west-1.amazonaws.com")
    with pytest.raises(ConfigError, match="HTTPS"):
        _cfg(endpoint_url="http://bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com")
    with pytest.raises(ConfigError, match="sim accounts only"):
        _cfg(endpoint_url="https://127.0.0.1:9000")
    # No public fallback (decision 22): missing endpoints are refused at load.
    with pytest.raises(ConfigError, match="endpoint_url"):
        _cfg(endpoint_url=None)
    with pytest.raises(ConfigError, match="sts_endpoint_url"):
        _cfg(sts_endpoint_url=None)
    with pytest.raises(ConfigError, match="sqs_endpoint_url"):
        _cfg(event_queues={"*": "q"})
    with pytest.raises(ConfigError, match="control_endpoint_url"):
        _cfg(batch=BatchOps(ACCT, ROLE, "ops"))
    with pytest.raises(ConfigError, match="VPC interface endpoint"):
        _cfg(edge_endpoint="s3.eu-west-1.amazonaws.com")
    # Userinfo or a backslash can't smuggle another host past the check.
    for url in (
        "https://attacker.example@bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com",
        "https://bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com\\@attacker.example",
    ):
        with pytest.raises(ConfigError):
            _cfg(endpoint_url=url)
    cfg = _cfg()
    assert cfg.edge_endpoint_name == "bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com"
    public = _cfg(private_network=False, endpoint_url=None, sts_endpoint_url=None)
    assert not public.private_network and public.edge_endpoint_name == "s3.eu-west-1.amazonaws.com"


def test_tls_12_is_allowed_and_pinned_for_interface_endpoints() -> None:
    ctx = _cfg().ssl_context()
    assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version is ssl.TLSVersion.TLSv1_2
    open_ctx = _cfg(tls_max_version=None).ssl_context()
    assert open_ctx.minimum_version is ssl.TLSVersion.TLSv1_2
    assert open_ctx.maximum_version is not ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname
    with pytest.raises(ConfigError, match="tls_max_version"):
        _cfg(tls_max_version="1.1")


def test_config_limits() -> None:
    for bad in (
        {"part_size": 1024},
        {"copy_part_size": 6 * 1024**3},
        {"events_per_receive": 11},
        {"restore_rate_per_s": 1001},
        {"restore_rate_per_s": 0},
        {"archive_class": "STANDARD"},
        {"session_duration_s": 60},
        {"edge_credentials": "keys"},
    ):
        with pytest.raises(ConfigError):
            _cfg(**bad)


# --- credentials: web identity, no keys ------------------------------------------


def test_the_default_chain_is_for_loopback_emulators_only() -> None:
    # botocore's own chain would read environment keys or an instance role.
    with pytest.raises(ConfigError, match="emulators only"):
        S3Config(region="eu-west-1", credentials="default", private_network=False)
    with pytest.raises(ConfigError, match="sim accounts reach loopback"):
        S3Config(region="eu-west-1", credentials="default", endpoint_url=S3_VPCE)
    with pytest.raises(ConfigError, match="web_identity or default"):
        _cfg(credentials="roles_anywhere")  # type: ignore[arg-type]
    emu = S3Config(region="eu-west-1", credentials="default", endpoint_url="http://127.0.0.1:4566")
    assert emu.credentials == "default"


def test_web_identity_needs_a_role_in_the_account_and_the_fixed_audience() -> None:
    with pytest.raises(ConfigError, match="role_arn"):
        _cfg(role_arn=None)
    with pytest.raises(ConfigError, match="own account"):
        _cfg(role_arn="arn:aws:iam::999999999999:role/r")
    with pytest.raises(ConfigError, match=ENV_AUDIENCE):
        _cfg(web_identity_audience=None)
    for bad in ("https://management.azure.com", "https://graph.microsoft.com/", "api:/x", ""):
        with pytest.raises(ConfigError, match="audience"):
            check_audience(bad)
        with pytest.raises(ConfigError, match="audience"):
            _cfg(web_identity_audience=bad)
    with pytest.raises(ConfigError, match="audience"):
        entra_token_provider(object(), "https://management.azure.com")


class _Token:
    """An azure-identity credential stand-in: records the scopes asked for."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_token(self, scope: str) -> Any:
        self.scopes.append(scope)

        class T:
            token = "entra-jwt"

        return T()


def _attached_session(cfg: S3Config, fake: FakeAws, token: Any, clock: ManualClock) -> Any:
    return session_for(
        cfg,
        token_provider=entra_token_provider(token, AUDIENCE),
        clock=clock,
        on_client=fake.attach,
    )


def test_web_identity_assumes_the_role_with_the_managed_identity_token() -> None:
    clock = ManualClock()
    fake = FakeAws(clock.now)
    token = _Token()
    session = _attached_session(_cfg(session_duration_s=900), fake, token, clock)

    async def frozen() -> Any:
        creds = await session.get_credentials()
        return await creds.get_frozen_credentials()

    got = asyncio.run(frozen())
    assert got.access_key == "ASIAFAKE1" and got.token == "session-1"
    assert fake.sts_calls == [(ROLE, "omnifile-service", "entra-jwt")]
    # The token is for the deployment's application ID URI, never ARM.
    assert token.scopes == [f"{AUDIENCE}/.default"]


def test_web_identity_credentials_refresh_on_the_backend_clock() -> None:
    clock = ManualClock()
    fake = FakeAws(clock.now)
    token = _Token()
    wi = WebIdentityCredentials(
        None,
        _cfg(session_duration_s=900),
        entra_token_provider(token, AUDIENCE),
        clock,
        on_client=fake.attach,
    )
    from aiobotocore.session import AioSession

    wi._session = AioSession()  # noqa: SLF001 - a plain session for STS

    async def run() -> list[str]:
        creds = await wi.credentials()
        keys = [(await creds.get_frozen_credentials()).access_key]
        clock.advance(timedelta(minutes=20))  # past the 15-minute session
        keys.append((await creds.get_frozen_credentials()).access_key)
        return keys

    assert asyncio.run(run()) == ["ASIAFAKE1", "ASIAFAKE2"]
    assert len(fake.sts_calls) == 2 and len(token.scopes) == 2


def test_a_refused_token_is_an_error_not_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # An access key in the environment must never be picked up instead.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASTRAY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "stray")
    clock = ManualClock()
    fake = FakeAws(clock.now)
    fake.refused_tokens.add("entra-jwt")
    session = _attached_session(_cfg(), fake, _Token(), clock)

    async def frozen() -> Any:
        creds = await session.get_credentials()
        return await creds.get_frozen_credentials()

    with pytest.raises(Exception, match="InvalidIdentityToken"):
        asyncio.run(frozen())


def test_credentials_are_fetched_only_when_a_request_is_signed() -> None:
    """Building clients needs no token: STS is called at the first signed
    request, so a backend can be built before the managed identity answers."""
    clock = ManualClock()
    fake = FakeAws(clock.now)
    fake.add_bucket("contract")
    calls: list[int] = []

    async def token() -> str:
        calls.append(1)
        return "t"

    b = S3Backend(1, private_config(fake), clock=clock, on_client=fake.attach, token_provider=token)

    async def go() -> None:
        try:
            await b.list("contract", "", None)
        finally:
            await b.close()

    asyncio.run(go())
    assert calls == [] and fake.sts_calls == []


# --- operator settings vs catalog rows (B1) ---------------------------------------


def test_backend_rows_name_only_harmless_fields() -> None:
    dep = _deployment()
    cfg = S3Config.for_backend(ROW, deployment=dep)
    assert cfg.endpoint_url == S3_VPCE and cfg.sts_endpoint_url == STS_VPCE
    assert cfg.sqs_endpoint_url == SQS_VPCE and cfg.role_arn == ROLE
    assert cfg.web_identity_audience == AUDIENCE and cfg.credentials == "web_identity"
    assert cfg.batch is not None and cfg.batch.control_endpoint_url == CTL_VPCE
    assert cfg.batch.account_id == ACCT
    assert cfg.allows("seismic-raw") and cfg.allows("ops") and not cfg.allows("other")
    assert cfg.edge_credentials == "roles_anywhere"
    assert capabilities_for(cfg).kind is BackendKind.S3
    for extra in (
        {"web_identity_audience": "https://management.azure.com"},
        {"endpoint_url": "https://attacker.example"},
        {"endpoints": {"sts": "https://attacker.example"}},
        {"sts_endpoint_url": "https://attacker.example"},
        {"role_arn": f"arn:aws:iam::{ACCT}:role/x"},
        {"credentials": "default"},
        {"sim_credentials": True},
        {"private_network": False},
        {"ca_bundle": "/tmp/evil.pem"},
        {"event_queues": {"*": "https://sqs.attacker.example/q"}},
        {"staging_bucket": "seismic-raw"},
        {"edge_endpoint": "bucket.vpce-evil.s3.eu-west-1.vpce.amazonaws.com"},
        {"edge_credentials": "presigned"},
        {"batch": {"role_arn": f"arn:aws:iam::{ACCT}:role/b", "manifest_bucket": "x"}},
        {"roles_anywhere": {"private_key": "/etc/key.pem"}},
        {"secret_access_key": "x"},
        {"part_size": 5 * 1024 * 1024},
    ):
        with pytest.raises(ConfigError):
            validate_backend_config({**ROW, **extra}, deployment=dep)
        with pytest.raises(ConfigError):
            check_row_keys({**ROW, **extra})
    with pytest.raises(ConfigError, match="not configured"):
        validate_backend_config({**ROW, "account_id": "999999999999"}, deployment=dep)
    with pytest.raises(ConfigError, match="region"):
        validate_backend_config({**ROW, "region": "us-east-1"}, deployment=dep)
    with pytest.raises(ConfigError, match="account_id"):
        validate_backend_config({"region": "eu-west-1"}, deployment=dep)


def test_private_network_is_never_weakened_by_the_row() -> None:
    dep = _deployment()
    assert S3Config.for_backend(ROW, private_network=False, deployment=dep).private_network
    public = AwsDeployment.from_mapping(
        {ACCT: {**ACCOUNT, "private_network": False}},
        web_identity_audience=AUDIENCE,
        private_network=False,
    )
    assert not S3Config.for_backend(ROW, private_network=False, deployment=public).private_network
    assert S3Config.for_backend(ROW, private_network=True, deployment=public).private_network


def test_deployment_settings_are_checked_at_load() -> None:
    with pytest.raises(ConfigError, match=ENV_AUDIENCE):
        AwsDeployment.from_mapping({ACCT: ACCOUNT})
    with pytest.raises(ConfigError, match="role_arn is needed"):
        AwsDeployment.from_mapping(
            {ACCT: {k: v for k, v in ACCOUNT.items() if k != "role_arn"}},
            web_identity_audience=AUDIENCE,
        )
    with pytest.raises(ConfigError, match="own account"):
        _deployment(role_arn="arn:aws:iam::999999999999:role/r")
    with pytest.raises(ConfigError, match="unknown settings"):
        _deployment(bucket_policy="x")
    with pytest.raises(ConfigError, match="secrets"):
        _deployment(secret_access_key="x")
    with pytest.raises(ConfigError, match="unknown endpoints"):
        _deployment(endpoints={**ACCOUNT["endpoints"], "iam": S3_VPCE})
    with pytest.raises(ConfigError, match="VPC interface endpoint"):
        _deployment(endpoints={**ACCOUNT["endpoints"], "sts": "https://sts.amazonaws.com"})
    with pytest.raises(ConfigError, match="s3_control"):
        _deployment(batch={**ACCOUNT["batch"], "control_endpoint_url": CTL_VPCE})
    # A sim account: loopback endpoints and the default chain, dev only.
    sim = AwsDeployment.from_mapping(
        {
            ACCT: {
                "region": "eu-west-1",
                "sim_credentials": True,
                "endpoints": {"s3": "http://127.0.0.1:4566"},
            }
        }  # fmt: skip
    )
    cfg = S3Config.for_backend(ROW, deployment=sim)
    assert cfg.credentials == "default" and cfg.sim_credentials
    with pytest.raises(ConfigError, match="loopback"):
        AwsDeployment.from_mapping(
            {
                ACCT: {"region": "eu-west-1", "sim_credentials": True, "endpoints": {"s3": S3_VPCE}}
            }  # fmt: skip
        )


def test_the_roles_anywhere_trust_anchor_is_an_operator_setting() -> None:
    """Decision 50: the trust anchor the edge CA's CRL goes to is in the
    operator's account settings, checked for shape, account and region; with
    private networking it is reached through its interface endpoint only.
    A backend row (a partner's input) can't name one."""
    from omnifile_service.worker.crl_jobs import anchors_from

    anchor = (
        f"arn:aws:rolesanywhere:eu-west-1:{ACCT}:trust-anchor/{'0' * 8}-1111-2222-3333-{'4' * 12}"
    )
    ra = "https://vpce-0456.rolesanywhere.eu-west-1.vpce.amazonaws.com"
    endpoints = {**ACCOUNT["endpoints"], "rolesanywhere": ra}
    dep = _deployment(roles_anywhere_trust_anchor_arn=anchor, endpoints=endpoints)
    [a] = anchors_from(dep)
    assert a.trust_anchor_arn == anchor and a.crl_name == "omnifile-edge-ca"
    assert a.anchor_id == anchor.rsplit("/", 1)[1]
    assert a.client._endpoint == ra  # type: ignore[attr-defined]
    assert anchors_from(_deployment()) == []
    with pytest.raises(ConfigError, match="interface endpoint"):
        _deployment(roles_anywhere_trust_anchor_arn=anchor)
    with pytest.raises(ConfigError, match="VPC interface endpoint"):
        _deployment(
            roles_anywhere_trust_anchor_arn=anchor,
            endpoints={
                **endpoints,
                "rolesanywhere": "https://rolesanywhere.eu-west-1.amazonaws.com",
            },
        )
    for bad in (
        anchor.replace(ACCT, "999999999999"),
        anchor.replace("eu-west-1", "us-east-1"),
        anchor.replace("trust-anchor", "profile"),
        "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/x",
    ):
        with pytest.raises(ConfigError, match="trust anchor in this account"):
            _deployment(roles_anywhere_trust_anchor_arn=bad, endpoints=endpoints)
    with pytest.raises(ConfigError, match="roles_anywhere_crl_name"):
        _deployment(
            roles_anywhere_trust_anchor_arn=anchor,
            endpoints=endpoints,
            roles_anywhere_crl_name="no/slashes",
        )
    with pytest.raises(ConfigError):
        validate_backend_config({**ROW, "roles_anywhere_trust_anchor_arn": anchor}, deployment=dep)


def test_deployment_from_env(tmp_path: Any) -> None:
    env = {ENV_ACCOUNTS: json.dumps({ACCT: ACCOUNT}), ENV_AUDIENCE: AUDIENCE}
    dep = AwsDeployment.from_env(env)
    assert dep.private_network and dep.accounts[ACCT].role_arn == ROLE
    assert not AwsDeployment.from_env({**env, ENV_PRIVATE: "false"}).private_network
    path = tmp_path / "aws.json"
    path.write_text(json.dumps({ACCT: ACCOUNT}))
    assert AwsDeployment.from_env({ENV_ACCOUNTS_FILE: str(path), ENV_AUDIENCE: AUDIENCE}).accounts
    with pytest.raises(ConfigError, match="not JSON"):
        AwsDeployment.from_env({ENV_ACCOUNTS: "{"})
    assert AwsDeployment.from_env({}).accounts == {}


def test_a_backend_row_builds_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ACCOUNTS, json.dumps({ACCT: ACCOUNT}))
    monkeypatch.setenv(ENV_AUDIENCE, AUDIENCE)
    b = S3Backend.from_config(7, ROW, private_network=False)
    assert b.config.private_network and b.config.endpoint_url == S3_VPCE
    with pytest.raises(ConfigError):
        S3Backend.from_config(7, {**ROW, "endpoint_url": "https://attacker.example"})


# --- the edge's credential ----------------------------------------------------


def test_the_edge_gets_a_pointer_by_default_and_presigned_only_as_fallback() -> None:
    clock = ManualClock()
    fake = FakeAws(clock.now)
    fake.add_bucket("contract")

    def edge_cred(**kw: Any) -> Any:
        b = S3Backend(
            1,
            private_config(fake, **kw),
            clock=clock,
            on_client=fake.attach,
            token_provider=entra_token_provider(_Token(), AUDIENCE),
        )

        async def go() -> Any:
            try:
                return await b.edge_credential(
                    ObjectRef(1, "contract", "a/b.segy", "v1"), EdgeAccess.READ, timedelta(hours=1)
                )
            finally:
                await b.close()

        return asyncio.run(go())

    ptr = edge_cred(requester_pays=True)
    assert ptr.mode is CredentialMode.S3_ROLES_ANYWHERE and ptr.url is None
    assert dict(ptr.details) == {
        "bucket": "contract",
        "key": "a/b.segy",
        "version_id": "v1",
        "region": "eu-west-1",
        "endpoint": "bucket.vpce-0a1b2c3d-4e5f6a7b.s3.eu-west-1.vpce.amazonaws.com",
        "addressing": "virtual_host",
        "tls_max_version": "1.2",
        "requester_pays": True,
        "private_network": True,
    }
    presigned = edge_cred(edge_credentials="presigned")
    assert presigned.mode is CredentialMode.S3_PRESIGNED_URL and presigned.url

    b = S3Backend(1, private_config(fake), clock=clock, on_client=fake.attach)
    with pytest.raises(UnsupportedOperation):
        asyncio.run(
            b.edge_credential(
                ObjectRef(1, "contract", "k"), EdgeAccess.WRITE, timedelta(minutes=15)
            )
        )


def test_the_pointer_etag_is_quoted_for_if_match() -> None:
    from omnifile_service.api.data import quoted_etag

    assert quoted_etag("abc123") == '"abc123"'  # an inventory row stores it bare
    assert quoted_etag('"abc123"') == '"abc123"'
    assert quoted_etag('"d41d8cd9-2"') == '"d41d8cd9-2"'
    assert quoted_etag('W/"x"') == 'W/"x"'
