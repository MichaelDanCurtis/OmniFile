"""Configuration for one Amazon S3 binding (one `catalog.backend` row).

S3 is a migration source or destination, never the home backend (decision
18): a dataset's bytes live in S3 while a Prizmanyx migration moves them to
Azure. Refs name the bucket as the container.

**No keys anywhere.** The service gets temporary AWS credentials with STS
``AssumeRoleWithWebIdentity``: the web identity token is an Entra access
token of the service's Azure managed identity, for the one audience the
client's IAM OIDC provider trusts (credentials.py). ``default`` (botocore's
own chain) is for emulators and tests only, and only towards loopback
endpoints, so it can never pick up a stray key towards real AWS.

**Two sources, kept apart (port of B's M5 review B1).** Everything that
decides *where* the service sends a request or *which* identity it uses is
an operator setting, never a catalog row:
- the endpoints (S3, S3 Control, SQS, STS) and the edge's endpoint;
- the role ARNs, Batch's included, and the web-identity audience;
- the CA bundle and the TLS ceiling;
- the event queues, the staging bucket, the Batch buckets and the buckets
  OmniFile owns (`owned_buckets`): together its operations buckets, the only
  places it writes (decision 40, port of #19);
- the S3 Tables endpoint and the buckets' S3 Metadata tables
  (`metadata_tables`, which name table bucket ARNs);
- private networking (which a row can only switch on);
- how edges read (``edge_credentials``) and sim credentials;
- the Roles Anywhere trust anchor made from OmniFile's edge CA, which the
  worker uploads the CA's CRL to (``roles_anywhere_trust_anchor_arn``, through
  ``endpoints.rolesanywhere``; decision 50, `worker/crl_jobs.py`).

They live in `AwsDeployment`, loaded from the service's environment
(`AwsDeployment.from_env`): ``OMNIFILE_AWS_ACCOUNTS`` (JSON) or
``OMNIFILE_AWS_ACCOUNTS_FILE`` (a path), keyed by AWS account id;
``OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE``; ``OMNIFILE_AWS_PRIVATE_NETWORK``
(default on). A backend row's ``config`` names only harmless things
(`ROW_KEYS`: the account, its region, the buckets, a prefix, requester
pays, bucket versioning, the archive class); `validate_backend_config`
refuses anything else, and `S3Config.for_backend` merges the two.

**Private networking (decision 22).** With ``private_network`` the data plane
goes through PrivateLink interface endpoints only: every endpoint must be an
https ``*.vpce.amazonaws.com`` host, and there is no public fallback.
Loopback is allowed only for an account marked ``sim_credentials`` (dev,
emulators). Interface endpoints don't offer TLS 1.3, so the client allows
TLS 1.2 [V]; ``tls_max_version: "1.2"`` (the default) pins it there.

Account example (``OMNIFILE_AWS_ACCOUNTS``)::

    {"123456789012": {
       "region": "eu-west-1",
       "endpoints": {"s3": "https://bucket.vpce-0abc.s3.eu-west-1.vpce.amazonaws.com",
                     "s3_control": "https://control.vpce-0abc.s3.eu-west-1.vpce.amazonaws.com",
                     "sqs": "https://vpce-0def.sqs.eu-west-1.vpce.amazonaws.com",
                     "sts": "https://vpce-0123.sts.eu-west-1.vpce.amazonaws.com"},
       "role_arn": "arn:aws:iam::123456789012:role/omnifile-service",
       "event_queues": {"*": "https://sqs.eu-west-1.amazonaws.com/123456789012/omnifile-events"},
       "batch": {"role_arn": "arn:aws:iam::123456789012:role/omnifile-batch",
                 "manifest_bucket": "omnifile-ops"},
       "staging_bucket": "omnifile-ops"}}
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from ..sim.capabilities import S3_DEEP_ARCHIVE, S3_GLACIER, S3_RESTORE_REQUESTS_PER_SECOND

#: Where urgent recalls are staged in `staging_bucket` (migration-engine.md
#: section 4): OmniFile's own keys, the only ones there it writes or deletes.
STAGING_PREFIX = "omnifile-staging/"

MiB = 1024 * 1024
GiB = 1024 * MiB

CredentialSource = Literal["web_identity", "default"]

ENV_ACCOUNTS = "OMNIFILE_AWS_ACCOUNTS"
ENV_ACCOUNTS_FILE = "OMNIFILE_AWS_ACCOUNTS_FILE"
ENV_AUDIENCE = "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE"
ENV_PRIVATE = "OMNIFILE_AWS_PRIVATE_NETWORK"

#: The only keys a backend row's config may hold.
ROW_KEYS = frozenset(
    {
        "account_id",
        "region",
        "buckets",
        "bucket",
        "container",
        "prefix",
        "requester_pays",
        "edge_access",
        "versioning",
        "archive_class",
    }
)
#: Endpoint services an account may name.
SERVICES = ("s3", "s3_control", "sqs", "sts", "s3tables", "rolesanywhere")
EDGE_CREDENTIALS = ("roles_anywhere", "presigned")

#: VPC interface endpoint hosts (decision 22).
_VPCE_HOST = re.compile(
    r"^([a-z0-9-]+\.)*vpce(-[a-z0-9]+)*\.[a-z0-9.-]+\.vpce\.amazonaws\.com(\.cn)?$"
)
#: The web-identity audience: an Entra application ID URI, nothing else.
_AUDIENCE = re.compile(r"^api://[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_ACCOUNT_ID = re.compile(r"^\d{12}$")
#: An IAM Roles Anywhere trust anchor (decision 50): partition, region, account, id.
_TRUST_ANCHOR = re.compile(
    r"^arn:aws(-[a-z]+)*:rolesanywhere:([a-z0-9-]+):(\d{12}):trust-anchor/[0-9a-f-]{36}$"
)
#: ImportCrl's name pattern.
_CRL_NAME = re.compile(r"^[ a-zA-Z0-9_-]{1,255}$")
_SECRET_KEYS = re.compile(
    r"(secret|password|session_token|access_key|private_key|credential|token)", re.IGNORECASE
)


class ConfigError(ValueError):
    """The S3 config is unusable (refused at load, never at first call)."""


@dataclass(frozen=True)
class BatchOps:
    """S3 Batch Operations for restore waves (migration-engine.md section 8).

    `account_id` and `role_arn`: the job runs in the customer's account,
    assuming the role (it needs s3:RestoreObject on the buckets and
    s3:GetObject on the manifests). Manifests are written to
    `manifest_bucket`, failure reports to `report_bucket` (default: the same).
    `control_endpoint_url`: the S3 Control endpoint (the account's
    ``endpoints.s3_control``; an interface endpoint with private networking).
    """

    account_id: str
    role_arn: str
    manifest_bucket: str
    report_bucket: str | None = None
    control_endpoint_url: str | None = None
    priority: int = 10
    manifest_prefix: str = "omnifile/batch/"


@dataclass(frozen=True)
class S3Config:
    region: str
    #: Buckets this binding may touch; empty means any. A ref naming another
    #: bucket is refused, so a bad catalog row can't reach someone else's data.
    buckets: Sequence[str] = ()
    #: The S3 interface endpoint, e.g.
    #: ``https://bucket.vpce-0a1b2c3d-4e5f.s3.eu-west-1.vpce.amazonaws.com``.
    #: None takes the regional endpoint (only without private networking).
    endpoint_url: str | None = None
    #: The SQS interface endpoint the restore and object events are pulled through.
    sqs_endpoint_url: str | None = None
    #: The STS interface endpoint AssumeRoleWithWebIdentity goes through.
    sts_endpoint_url: str | None = None
    #: Queue per bucket for EventBridge S3 events (Object Created / Deleted /
    #: Restore Initiated / Completed / Expired / Storage Class Changed), or one
    #: queue under ``"*"`` for all buckets.
    event_queues: Mapping[str, str] = field(default_factory=dict)
    private_network: bool = True
    #: ``web_identity`` (production) or ``default`` (emulators, loopback only).
    credentials: CredentialSource = "web_identity"
    #: The role the service assumes with its managed identity's token.
    role_arn: str | None = None
    #: The Entra application ID URI the client's IAM OIDC provider trusts.
    web_identity_audience: str | None = None
    session_duration_s: int = 3600
    #: The AWS account (informational; role ARNs must be in it).
    account_id: str | None = None
    #: Bucket versioning is on (decision 30). S3 source buckets may not have it.
    versioning: bool = True
    #: The archive class `set_class(ARCHIVE)` and `put(tier=ARCHIVE)` use.
    archive_class: str = S3_GLACIER
    #: Path-style addressing (emulators); AWS uses virtual-hosted style.
    path_style: bool = False
    #: "1.2" pins TLS 1.2 (interface endpoints reject 1.3 [V]); None allows
    #: 1.2 and 1.3.
    tls_max_version: str | None = "1.2"
    #: Every request (and the edge's) sends ``x-amz-request-payer: requester``.
    requester_pays: bool = False
    #: Objects up to this size are one PutObject; larger ones are multipart.
    part_size: int = 16 * MiB
    #: CopyObject handles up to 5 GiB; larger copies use UploadPartCopy [V].
    copy_part_size: int = 512 * MiB
    single_copy_limit: int = 5 * GiB
    list_page_size: int = 1000
    #: Messages per SQS receive (10 is the SQS maximum).
    events_per_receive: int = 10
    events_visibility_s: int = 300
    #: Restore requests per second this process may send, at most the
    #: account's 1,000/s, which single restores and Batch Operations share [V].
    restore_rate_per_s: float = float(S3_RESTORE_REQUESTS_PER_SECOND)
    max_attempts: int = 5
    #: CA bundle for a private PKI or an emulator; None means system trust.
    ca_bundle: str | None = None
    #: The endpoint host handed to the edge, if it differs from
    #: ``endpoint_url``'s (the edge has its own interface endpoint).
    edge_endpoint: str | None = None
    #: S3 Batch Operations (restore waves); None: not offered.
    batch: BatchOps | None = None
    #: How edges read S3-resident files: ``roles_anywhere`` (the default: a
    #: pointer, the edge signs with its own IAM Roles Anywhere session, key
    #: in its TPM) or ``presigned`` (the fallback: a GET URL the service
    #: signs for one object version, for the edge's endpoint).
    edge_credentials: str = "roles_anywhere"
    #: The bucket urgent recalls are staged in for Storage Mover
    #: (migration-engine.md section 4), never a dataset's own bucket; None:
    #: they wait for the prefix's next run.
    staging_bucket: str | None = None
    #: Dev and tests only: an emulator account whose endpoints are loopback.
    sim_credentials: bool = False
    #: The S3 Tables interface endpoint (``com.amazonaws.<region>.s3tables``),
    #: for sources seeded from S3 Metadata tables (sources/metadata_s3.py).
    #: None takes the regional endpoint, only without private networking.
    s3tables_endpoint_url: str | None = None
    #: Buckets with an S3 Metadata configuration, bucket -> ``{"table_bucket_arn",
    #: "namespace"?}``. A source on one of them that names no inventory seeds
    #: from its tables (catalog-seeding.md section 7).
    metadata_tables: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Buckets OmniFile owns outright and may write anywhere in. None in v1,
    #: where S3 is only a migration source (decision 23); emulators and the
    #: backend's own contract tests. Every other bucket is read-only to
    #: OmniFile except its own areas (`owns`).
    owned_buckets: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not self.region:
            raise ConfigError("an S3 binding needs a region")
        if self.archive_class not in (S3_GLACIER, S3_DEEP_ARCHIVE):
            raise ConfigError("archive_class is GLACIER or DEEP_ARCHIVE")
        if self.tls_max_version not in (None, "1.2", "1.3"):
            raise ConfigError('tls_max_version is "1.2", "1.3" or null')
        if self.account_id is not None and not _ACCOUNT_ID.match(self.account_id):
            raise ConfigError("account_id must be the 12-digit AWS account id")
        if self.private_network:
            # Decision 22: always the interface endpoints, never a public fallback.
            if not self.endpoint_url:
                raise ConfigError("private networking needs the S3 interface endpoint_url")
            if self.event_queues and not self.sqs_endpoint_url:
                raise ConfigError("private networking needs the SQS interface sqs_endpoint_url")
            if self.credentials == "web_identity" and not self.sts_endpoint_url:
                raise ConfigError("private networking needs the STS interface sts_endpoint_url")
            if self.batch is not None and not self.batch.control_endpoint_url:
                raise ConfigError(
                    "private networking needs the S3 Control batch.control_endpoint_url"
                )
            if self.metadata_tables and not self.s3tables_endpoint_url:
                raise ConfigError(
                    "private networking needs the S3 Tables interface s3tables_endpoint_url"
                )
        # The default chain would read environment keys, profiles or an
        # instance role: only towards an emulator on this host.
        sim = self.credentials == "default" or self.sim_credentials
        urls = {
            "endpoint_url": self.endpoint_url,
            "sqs_endpoint_url": self.sqs_endpoint_url,
            "sts_endpoint_url": self.sts_endpoint_url,
            "batch.control_endpoint_url": self.batch.control_endpoint_url if self.batch else None,
            "s3tables_endpoint_url": self.s3tables_endpoint_url,
        }
        for name, url in urls.items():
            if url is not None:
                check_endpoint(name, url, private=self.private_network, sim=sim)
        if self.edge_endpoint is not None:
            check_endpoint(
                "edge_endpoint",
                f"https://{self.edge_endpoint}",
                private=self.private_network,
                sim=sim,
            )
        if self.credentials == "web_identity":
            if self.sim_credentials:
                raise ConfigError("sim_credentials use the default chain, not web_identity")
            if not self.role_arn:
                raise ConfigError("web_identity credentials need the account's role_arn")
            if self.web_identity_audience is None:
                raise ConfigError(f"web_identity credentials need {ENV_AUDIENCE}")
            check_audience(self.web_identity_audience)
        elif self.credentials == "default":
            # Every client it builds must reach the emulator, never a public
            # default endpoint.
            if (
                not self.endpoint_url
                or (self.event_queues and not self.sqs_endpoint_url)
                or (self.batch is not None and not self.batch.control_endpoint_url)
            ):
                raise ConfigError(
                    "default credentials are for emulators only: name the loopback endpoints"
                )
        else:
            # Anything else would quietly fall back to botocore's default
            # chain (environment keys, instance roles): refuse it.
            raise ConfigError(f"credentials is web_identity or default, not {self.credentials!r}")
        if self.role_arn is not None:
            prefix = f"arn:aws:iam::{self.account_id}:role/" if self.account_id else None
            if not self.role_arn.startswith("arn:aws") or (
                prefix and not self.role_arn.startswith(prefix)
            ):
                raise ConfigError("role_arn must be a role in the backend's own account")
        if not 900 <= self.session_duration_s <= 43200:
            raise ConfigError("session_duration_s is 900..43200 (STS limits)")
        if not 5 * MiB <= self.part_size <= 5 * GiB:
            raise ConfigError("part_size is between 5 MiB and 5 GiB (S3 multipart limits)")
        if not 5 * MiB <= self.copy_part_size <= 5 * GiB:
            raise ConfigError("copy_part_size is between 5 MiB and 5 GiB")
        if not 1 <= self.events_per_receive <= 10:
            raise ConfigError("events_per_receive is 1 to 10")
        if not 0 < self.restore_rate_per_s <= S3_RESTORE_REQUESTS_PER_SECOND:
            raise ConfigError("restore_rate_per_s is above 0 and at most 1000 (per account)")
        if self.edge_credentials not in EDGE_CREDENTIALS:
            raise ConfigError("edge_credentials is roles_anywhere or presigned")
        if self.staging_bucket is not None and not self.allows(self.staging_bucket):
            raise ConfigError("staging_bucket must be one of buckets")
        for bucket, tables in self.metadata_tables.items():
            if not self.allows(bucket):
                raise ConfigError(f"metadata_tables names {bucket!r}, not one of buckets")
            if not isinstance(tables, Mapping) or not tables.get("table_bucket_arn"):
                raise ConfigError(f"metadata_tables[{bucket!r}] needs table_bucket_arn")
        for bucket in self.owned_buckets:
            if not self.allows(bucket):
                raise ConfigError("owned_buckets must be among buckets")

    def allows(self, bucket: str) -> bool:
        return not self.buckets or bucket in self.buckets

    @property
    def ops_buckets(self) -> frozenset[str]:
        """OmniFile's operations buckets: staging, Batch manifests and
        reports, owned buckets. None of them may be a migration source."""
        return ops_buckets_of(
            {"staging_bucket": self.staging_bucket, "owned_buckets": list(self.owned_buckets),
             "batch": None if self.batch is None else
             {"manifest_bucket": self.batch.manifest_bucket,
              "report_bucket": self.batch.report_bucket}}
        )  # fmt: skip

    def owns(self, bucket: str, key: str) -> bool:
        """Whether `key` in `bucket` is OmniFile's own, so it may put, copy
        onto, re-tier, tag or delete it: the staging prefix of the staging
        bucket, the Batch Operations manifests, or an owned bucket. Nothing
        else: a migration source is the client's (catalog-seeding.md
        section 10)."""
        if bucket in self.owned_buckets:
            return True
        if bucket == self.staging_bucket and key.startswith(STAGING_PREFIX):
            return True
        b = self.batch
        return b is not None and bucket == b.manifest_bucket and key.startswith(b.manifest_prefix)

    def queue_for(self, bucket: str) -> str | None:
        return self.event_queues.get(bucket) or self.event_queues.get("*")

    def ssl_context(self) -> ssl.SSLContext:
        """TLS 1.2 allowed (interface endpoints have no TLS 1.3 [V]); 1.2 only
        when `tls_max_version` is "1.2". Certificates checked against
        `ca_bundle`, else system trust."""
        ctx = ssl.create_default_context(cafile=self.ca_bundle)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.tls_max_version == "1.2":
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    @property
    def edge_endpoint_name(self) -> str:
        if self.edge_endpoint:
            return self.edge_endpoint
        if self.endpoint_url:
            return urlsplit(self.endpoint_url).netloc
        return f"s3.{self.region}.amazonaws.com"

    @classmethod
    def for_backend(
        cls,
        raw: Mapping[str, Any],
        *,
        private_network: bool | None = None,
        deployment: AwsDeployment | None = None,
    ) -> S3Config:
        """The config for one backend row: its harmless fields
        (`validate_backend_config`) merged into the deployment's account.
        `private_network` is the row's column: it can only add to the
        deployment's private mode, never weaken it."""
        dep = deployment if deployment is not None else AwsDeployment.from_env()
        row = validate_backend_config(raw, deployment=dep)
        acct = dep.accounts[row["account_id"]]
        private = bool(private_network) or dep.private_network or acct.private_network
        buckets = tuple(row.get("buckets") or ())
        one = row.get("bucket") or row.get("container")
        if one and one not in buckets:
            buckets = (*buckets, one)
        if buckets and acct.staging_bucket and acct.staging_bucket not in buckets:
            buckets = (*buckets, acct.staging_bucket)
        if buckets and acct.batch is not None:
            for b in (acct.batch.manifest_bucket, acct.batch.report_bucket):
                if b and b not in buckets:
                    buckets = (*buckets, b)
        if buckets:
            for b in acct.owned_buckets:
                if b not in buckets:
                    buckets = (*buckets, b)
        # The account's S3 Metadata tables, for this binding's buckets only.
        tables = {
            b: dict(t) for b, t in acct.metadata_tables.items() if not buckets or b in buckets
        }
        kw: dict[str, Any] = {
            name: getattr(acct, name) for name in _TUNING if getattr(acct, name) is not None
        }
        batch = None
        if acct.batch is not None:
            batch = replace(
                acct.batch,
                account_id=acct.account_id,
                control_endpoint_url=acct.endpoints.get("s3_control"),
            )
        return cls(
            region=acct.region,
            account_id=acct.account_id,
            buckets=buckets,
            endpoint_url=acct.endpoints.get("s3"),
            sqs_endpoint_url=acct.endpoints.get("sqs"),
            sts_endpoint_url=acct.endpoints.get("sts"),
            event_queues=dict(acct.event_queues),
            private_network=private,
            credentials="default" if acct.sim_credentials else "web_identity",
            role_arn=acct.role_arn,
            web_identity_audience=dep.web_identity_audience if acct.role_arn else None,
            session_duration_s=acct.session_duration_s,
            versioning=bool(row.get("versioning", True)),
            archive_class=row.get("archive_class", S3_GLACIER),
            path_style=acct.path_style,
            tls_max_version=acct.tls_max_version,
            requester_pays=bool(row.get("requester_pays", False)),
            ca_bundle=acct.ca_bundle,
            edge_endpoint=acct.edge_endpoint,
            batch=batch,
            edge_credentials=acct.edge_credentials,
            staging_bucket=acct.staging_bucket,
            sim_credentials=acct.sim_credentials,
            s3tables_endpoint_url=acct.endpoints.get("s3tables"),
            metadata_tables=tables,
            owned_buckets=tuple(acct.owned_buckets),
            **kw,
        )


#: Performance knobs an operator may set per account (never a row).
_TUNING = (
    "part_size",
    "copy_part_size",
    "single_copy_limit",
    "list_page_size",
    "events_per_receive",
    "events_visibility_s",
    "restore_rate_per_s",
    "max_attempts",
)


@dataclass(frozen=True)
class AwsAccount:
    """One AWS account the deployment may reach: operator settings only."""

    account_id: str
    region: str
    #: Service -> endpoint URL (`SERVICES`).
    endpoints: Mapping[str, str] = field(default_factory=dict)
    private_network: bool = True
    path_style: bool = False
    tls_max_version: str | None = "1.2"
    #: The role the service assumes with web identity.
    role_arn: str | None = None
    session_duration_s: int = 3600
    #: Bucket (or ``"*"``) -> EventBridge SQS queue URL.
    event_queues: Mapping[str, str] = field(default_factory=dict)
    batch: BatchOps | None = None
    staging_bucket: str | None = None
    ca_bundle: str | None = None
    #: The S3 interface endpoint host edges are told (default: the s3 endpoint's).
    edge_endpoint: str | None = None
    #: ``roles_anywhere`` (default) or ``presigned`` (fallback).
    edge_credentials: str = "roles_anywhere"
    #: Dev and tests only: an emulator account, loopback endpoints, default chain.
    sim_credentials: bool = False
    #: Bucket -> ``{"table_bucket_arn", "namespace"?}``: its S3 Metadata tables.
    metadata_tables: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Buckets OmniFile owns outright (emulators and tests; none in v1).
    owned_buckets: Sequence[str] = ()
    #: The Roles Anywhere trust anchor the client made from OmniFile's edge CA
    #: (decision 50): the worker uploads the CA's CRL to it with the service's
    #: web-identity session (`worker/crl_jobs.py`). None: the client's own CA
    #: issues the edges' certificates, and revokes them itself.
    roles_anywhere_trust_anchor_arn: str | None = None
    #: The name the CRL is imported under at that trust anchor.
    roles_anywhere_crl_name: str = "omnifile-edge-ca"
    part_size: int | None = None
    copy_part_size: int | None = None
    single_copy_limit: int | None = None
    list_page_size: int | None = None
    events_per_receive: int | None = None
    events_visibility_s: int | None = None
    restore_rate_per_s: float | None = None
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if not _ACCOUNT_ID.match(self.account_id):
            raise ConfigError(f"AWS account {self.account_id!r}: not a 12-digit account id")
        unknown = set(self.endpoints) - set(SERVICES)
        if unknown:
            raise ConfigError(f"AWS account {self.account_id}: unknown endpoints {sorted(unknown)}")
        if self.edge_credentials not in EDGE_CREDENTIALS:
            raise ConfigError("edge_credentials is roles_anywhere or presigned")
        if self.roles_anywhere_trust_anchor_arn is not None:
            m = _TRUST_ANCHOR.match(self.roles_anywhere_trust_anchor_arn)
            if m is None or m.group(3) != self.account_id or m.group(2) != self.region:
                raise ConfigError(
                    f"AWS account {self.account_id}: roles_anywhere_trust_anchor_arn must be a"
                    f" trust anchor in this account and {self.region}"
                )
        if not _CRL_NAME.match(self.roles_anywhere_crl_name):
            raise ConfigError("roles_anywhere_crl_name: letters, digits, space, - and _ only")

    @classmethod
    def from_mapping(cls, account_id: str, raw: Mapping[str, Any]) -> AwsAccount:
        if not isinstance(raw, Mapping):
            raise ConfigError(f"AWS account {account_id}: settings must be an object")
        _refuse_secrets(raw, allow=frozenset({"sim_credentials", "edge_credentials"}))
        known = {f.name for f in fields(cls)} - {"account_id"}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"AWS account {account_id}: unknown settings {sorted(unknown)}")
        kw = dict(raw)
        if "endpoints" in kw:
            kw["endpoints"] = dict(kw["endpoints"])
        if "event_queues" in kw:
            kw["event_queues"] = dict(kw["event_queues"])
        if "metadata_tables" in kw:
            kw["metadata_tables"] = {
                str(b): dict(t) if isinstance(t, Mapping) else t
                for b, t in dict(kw["metadata_tables"]).items()
            }
        if "owned_buckets" in kw:
            kw["owned_buckets"] = tuple(kw["owned_buckets"])
        if kw.get("batch") is not None:
            b = dict(kw["batch"])
            if "control_endpoint_url" in b or "account_id" in b:
                raise ConfigError(
                    "batch takes the account's id and its endpoints.s3_control, not its own"
                )
            try:
                kw["batch"] = BatchOps(account_id=str(account_id), **b)
            except TypeError as e:
                raise ConfigError(f"AWS account {account_id}: batch: {e}") from None
        try:
            return cls(account_id=str(account_id), **kw)
        except TypeError as e:
            raise ConfigError(f"AWS account {account_id}: {e}") from None


@dataclass(frozen=True)
class AwsDeployment:
    """The operator's AWS settings for this deployment."""

    accounts: Mapping[str, AwsAccount] = field(default_factory=dict)
    #: The fixed web-identity audience (an Entra application ID URI).
    web_identity_audience: str | None = None
    #: The deployment's private-network mode (decision 22); default on.
    private_network: bool = True

    def __post_init__(self) -> None:
        if self.web_identity_audience is not None:
            check_audience(self.web_identity_audience)
        for acct in self.accounts.values():
            if acct.role_arn and self.web_identity_audience is None:
                raise ConfigError(f"{ENV_AUDIENCE} is needed for account {acct.account_id}")
            if not acct.role_arn and not acct.sim_credentials:
                raise ConfigError(
                    f"AWS account {acct.account_id}: role_arn is needed (web identity;"
                    " no keys are stored)"
                )
            # Build once so a bad account fails at load, not at first use.
            config = S3Config.for_backend(
                {"account_id": acct.account_id, "region": acct.region}, deployment=self
            )
            # The CRL upload (decision 50) goes through the Roles Anywhere
            # interface endpoint with private networking, never a public one.
            ra = acct.endpoints.get("rolesanywhere")
            if acct.roles_anywhere_trust_anchor_arn and config.private_network and not ra:
                raise ConfigError(
                    f"AWS account {acct.account_id}: private networking needs the Roles Anywhere"
                    " interface endpoint (endpoints.rolesanywhere) for the CRL upload"
                )
            if ra is not None:
                check_endpoint(
                    "endpoints.rolesanywhere",
                    ra,
                    private=config.private_network,
                    sim=acct.sim_credentials,
                )

    @classmethod
    def from_mapping(
        cls,
        accounts: Mapping[str, Any],
        *,
        web_identity_audience: str | None = None,
        private_network: bool = True,
    ) -> AwsDeployment:
        if not isinstance(accounts, Mapping):
            raise ConfigError(f"{ENV_ACCOUNTS} must be an object keyed by account id")
        return cls(
            {str(k): AwsAccount.from_mapping(str(k), v) for k, v in accounts.items()},
            web_identity_audience,
            private_network,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AwsDeployment:
        e = os.environ if env is None else env
        raw = e.get(ENV_ACCOUNTS)
        path = e.get(ENV_ACCOUNTS_FILE)
        if raw is None and path:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        try:
            accounts = json.loads(raw) if raw else {}
        except ValueError as err:
            raise ConfigError(f"{ENV_ACCOUNTS} is not JSON: {err}") from None
        private = e.get(ENV_PRIVATE, "1").strip().lower() not in ("0", "false", "no")
        return cls.from_mapping(
            accounts, web_identity_audience=e.get(ENV_AUDIENCE) or None, private_network=private
        )


def check_audience(audience: str) -> str:
    """The web-identity audience must be an Entra application ID URI
    (``api://...``). An Azure resource URI (ARM, Graph, Key Vault, Storage)
    would make the managed identity hand out a token for that resource."""
    if not isinstance(audience, str) or not _AUDIENCE.match(audience):
        raise ConfigError(
            "the web-identity audience must be the deployment's api:// application ID URI"
        )
    return audience


def check_endpoint(name: str, url: str, *, private: bool, sim: bool) -> None:
    """An endpoint URL: a host; loopback only for sim accounts; with private
    networking an https VPC interface endpoint host (decision 22)."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host:
        raise ConfigError(f"{name} is not a URL: {url!r}")
    if parts.username or parts.password or "\\" in url or "@" in parts.netloc:
        raise ConfigError(f"{name}: userinfo and backslashes are refused: {url!r}")
    loopback = _is_loopback(host)
    if sim:
        if not loopback:
            raise ConfigError(f"{name}: sim accounts reach loopback endpoints only")
        return
    if loopback:
        raise ConfigError(f"{name}: loopback endpoints are for sim accounts only")
    if parts.scheme != "https":
        raise ConfigError(f"{name}: the data plane is HTTPS only ({url})")
    if private and not _VPCE_HOST.match(host.lower()):
        raise ConfigError(
            f"private_network: {name} host {host} is not a VPC interface endpoint host"
            " (*.vpce.amazonaws.com; decision 22)"
        )


def check_row_keys(raw: Mapping[str, Any]) -> None:
    """The row-only part of `validate_backend_config`: an object holding only
    `ROW_KEYS` and no secret. Source registration checks it before it binds a
    source to the row (sources/run.py), without needing the deployment."""
    if not isinstance(raw, Mapping):
        raise ConfigError("the backend config must be an object")
    _refuse_secrets(raw)
    extra = set(raw) - ROW_KEYS
    if extra:
        raise ConfigError(
            f"not allowed in an S3 backend's config: {sorted(extra)} (endpoints, roles,"
            " audiences, CAs, queues, staging and private networking are deployment"
            f" settings, {ENV_ACCOUNTS})"
        )


def validate_backend_config(
    raw: Mapping[str, Any], *, deployment: AwsDeployment | None = None
) -> dict[str, Any]:
    """Check an ``s3`` backend row's config. Only `ROW_KEYS` are allowed;
    ``account_id`` must be a configured account and ``region`` its region.
    Returns the fields. Raises `ConfigError`.

    Called when the row is loaded (`S3Config.for_backend`), so a row that
    names an endpoint, a role, an audience, a CA, a queue or private
    networking never builds a backend."""
    check_row_keys(raw)
    account_id = str(raw.get("account_id") or "")
    if not _ACCOUNT_ID.match(account_id):
        raise ConfigError("account_id must be the 12-digit AWS account id")
    dep = deployment if deployment is not None else AwsDeployment.from_env()
    acct = dep.accounts.get(account_id)
    if acct is None:
        raise ConfigError(
            f"AWS account {account_id} is not configured for this deployment ({ENV_ACCOUNTS})"
        )
    if raw.get("region", acct.region) != acct.region:
        raise ConfigError(f"region must be {acct.region}, the account's region")
    for key in ("bucket", "container", "prefix"):
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(f"{key} must be a string")
    if "buckets" in raw and (
        isinstance(raw["buckets"], str)
        or not isinstance(raw["buckets"], Sequence)
        or not all(isinstance(b, str) for b in raw["buckets"])
    ):
        raise ConfigError("buckets must be a list of bucket names")
    for key in ("requester_pays", "versioning"):
        if key in raw and not isinstance(raw[key], bool):
            raise ConfigError(f"{key} must be true or false")
    if "edge_access" in raw and raw["edge_access"] not in ("direct", "none"):
        raise ConfigError("edge_access is direct or none")
    if "archive_class" in raw and raw["archive_class"] not in (S3_GLACIER, S3_DEEP_ARCHIVE):
        raise ConfigError("archive_class is GLACIER or DEEP_ARCHIVE")
    return {**dict(raw), "account_id": account_id}


#: Mappings whose keys are names (buckets, services), not settings.
_NAME_MAPS = frozenset({"event_queues", "endpoints"})


def _refuse_secrets(
    raw: Mapping[str, Any], path: Sequence[str] = (), allow: frozenset[str] = frozenset()
) -> None:
    for k, v in raw.items():
        if str(k) not in allow and _SECRET_KEYS.search(str(k)):
            raise ConfigError(f"S3 config must not hold secrets ({'.'.join([*path, str(k)])})")
        if isinstance(v, Mapping) and str(k) not in _NAME_MAPS:
            _refuse_secrets(v, [*path, str(k)])


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ops_buckets_of(raw: Mapping[str, Any] | None) -> frozenset[str]:
    """The operations buckets named in a mapping with S3Config's shape
    (`staging_bucket`, `batch.manifest_bucket`, `batch.report_bucket`,
    `owned_buckets`). OmniFile writes there, so none may be a source. A
    backend row can't name them (`check_row_keys`): they are operator
    settings, read through `S3Config.for_backend`."""
    raw = raw or {}
    out: set[str] = set()
    if raw.get("staging_bucket"):
        out.add(str(raw["staging_bucket"]))
    out.update(str(b) for b in raw.get("owned_buckets") or ())
    batch = raw.get("batch") or {}
    for k in ("manifest_bucket", "report_bucket"):
        if batch.get(k):
            out.add(str(batch[k]))
    return frozenset(out)
