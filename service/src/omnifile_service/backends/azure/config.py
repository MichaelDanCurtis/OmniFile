"""Configuration for one Azure Blob binding (one `catalog.backend` row).

Two layouts exist [A][W]:

- ``two_account`` (non-WORM shares): a zonal *live* account whose default
  tier is Smart, plus an LRS/GRS *archive* account. Archive is a server-side
  copy into the archive account, then a delete of the original.
- ``single_account`` (WORM shares, or non-Smart shares): one LRS/GRS account
  with versioning (and, for WORM, version-level immutability). Tiering is
  in place with Set Blob Tier.

Refs name containers ``"<account>/<container>"``. A bare container name means
the binding's primary account (live or the single account), as in sim.

No secrets live here: the service authenticates with Entra (managed
identity through `DefaultAzureCredential`), never shared keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

Layout = Literal["two_account", "single_account"]

MiB = 1024 * 1024


@dataclass(frozen=True)
class AzureAccount:
    """One storage account.

    `url` is the blob endpoint the service calls, e.g.
    ``https://acct.blob.core.windows.net``; with private networking (decision
    22) that name resolves to the private endpoint through private DNS, so the
    same URL is used. `sas_url` is the endpoint written into SAS URLs for the
    edge, when it differs (e.g. ``https://acct.privatelink.blob.core.windows.net``).
    """

    name: str
    url: str
    sas_url: str | None = None

    @property
    def edge_url(self) -> str:
        return (self.sas_url or self.url).rstrip("/")


@dataclass(frozen=True)
class AzureBlobConfig:
    accounts: Mapping[str, AzureAccount]
    layout: Layout
    #: The live account (two_account) or the single account.
    primary_account: str
    #: two_account only: the LRS/GRS account that holds Archive blobs.
    archive_account: str | None = None
    #: The primary account's default tier is Smart (two_account, ZRS) [V].
    smart: bool = False
    #: Version-level WORM on the single account (decision 31).
    worm: bool = False
    #: Blob versioning is on (decision 30).
    versioning: bool = True
    #: Non-WORM container for rehydrate copies, in the archived blob's own
    #: account (archive account, or the WORM account) [W].
    recall_container: str = "recall"
    #: Block size for `put` (the edge uploads big files itself with a SAS).
    block_size: int = 8 * MiB
    #: Async Copy Blob polling (archive move and copy_object wait for it).
    copy_poll_initial: float = 0.5
    copy_poll_max: float = 15.0
    copy_timeout: float = 6 * 3600.0
    #: Lifetime asked for user-delegation keys (Azure allows up to 7 days).
    delegation_key_lifetime: timedelta = timedelta(days=1)
    #: Change feed: maximum events scanned per `read_changes` call.
    feed_page_size: int = 1000
    #: Create containers on first write (tests); production containers are
    #: created by the deployment.
    auto_create_containers: bool = False
    #: Pin the REST version; None takes the SDK's newest. Smart needs
    #: 2026-02-06 or later [V].
    api_version: str | None = None
    #: TLS trust for an emulator (a CA bundle path); None = system trust.
    connection_verify: str | bool | None = None
    extra_client_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.primary_account not in self.accounts:
            raise ValueError(f"primary account {self.primary_account!r} is not configured")
        if self.layout == "two_account":
            if not self.archive_account or self.archive_account not in self.accounts:
                raise ValueError("two_account layout needs a configured archive_account")
            if self.archive_account == self.primary_account:
                raise ValueError("the archive account must differ from the live account")
            if self.worm:
                raise ValueError("WORM shares use the single_account layout (decision 31)")
        else:
            if self.archive_account is not None:
                raise ValueError("single_account layout has no archive account")
            if self.smart:
                # Smart needs ZRS and Archive is forbidden on ZRS, so a single
                # account cannot offer both [V].
                raise ValueError("Smart needs the two_account layout")
        if self.worm and not self.versioning:
            raise ValueError("version-level WORM needs versioning")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AzureBlobConfig:
        """Build from `catalog.backend.config` (JSON).

        ``{"accounts": {"name": "https://..."} | {"name": {"url":, "sas_url":}},
        "layout": "two_account", "primary_account": ..., "archive_account": ...,
        "smart": true, "worm": false, ...}``
        """
        accounts: dict[str, AzureAccount] = {}
        for name, spec in dict(raw["accounts"]).items():
            if isinstance(spec, str):
                accounts[name] = AzureAccount(name, spec)
            else:
                accounts[name] = AzureAccount(name, spec["url"], spec.get("sas_url"))
        kwargs: dict[str, Any] = {}
        for key in (
            "archive_account",
            "smart",
            "worm",
            "versioning",
            "recall_container",
            "block_size",
            "feed_page_size",
            "auto_create_containers",
            "api_version",
        ):
            if key in raw:
                kwargs[key] = raw[key]
        return cls(
            accounts=accounts,
            layout=raw.get("layout", "two_account"),
            primary_account=raw["primary_account"],
            **kwargs,
        )
