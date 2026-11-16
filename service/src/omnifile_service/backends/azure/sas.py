"""User-delegation SAS minting for the edge (control-api.md section 3).

The service holds one user-delegation key per storage account, obtained with
its Entra identity (Get User Delegation Key, needs the Storage Blob Delegator
role), and signs SAS tokens locally, so minting costs no Azure call. Keys may
last up to 7 days; we ask for `delegation_key_lifetime` and fetch a new one
before the old one can't cover a token's whole lifetime.

Scopes [A]:
- read: ``sp=r`` on one blob, or ``sr=bv`` on one version;
- write: ``sp=cw`` on one blob.

Lifetimes: read 60 minutes (`EDGE_READ_SAS_TTL`), so reads of cloud-resident
files survive an hour's outage; write 15 minutes (`EDGE_WRITE_SAS_TTL`), so a
leaked write SAS is short-lived; the edge renews it per `upload_id` while it
uploads. `blob_sas_url` refuses a longer write SAS whatever the caller asks
(read SAS for server-side copy sources may be longer).
The edge may reuse a SAS until ``cache_until`` = expiry - 5 minutes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from urllib.parse import quote

from azure.storage.blob import BlobSasPermissions, UserDelegationKey, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from ..base import EdgeAccess

EDGE_READ_SAS_TTL = timedelta(minutes=60)
EDGE_WRITE_SAS_TTL = timedelta(minutes=15)
EDGE_SAS_TTL = EDGE_READ_SAS_TTL  # the longest an edge SAS may live
CACHE_MARGIN = timedelta(minutes=5)
#: Signed start is backdated a little for clock skew between us and Azure.
START_SKEW = timedelta(minutes=5)
#: Refresh a key when it has less than this left after the token's expiry.
KEY_REFRESH_MARGIN = timedelta(minutes=10)

PERMISSIONS = {
    EdgeAccess.READ: BlobSasPermissions(read=True),
    EdgeAccess.WRITE: BlobSasPermissions(create=True, write=True),
}


def edge_sas_ttl(access: EdgeAccess) -> timedelta:
    return EDGE_WRITE_SAS_TTL if access is EdgeAccess.WRITE else EDGE_READ_SAS_TTL


def cache_until(expires_at: datetime) -> datetime:
    return expires_at - CACHE_MARGIN


def _parse(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class DelegationKeyCache:
    """One user-delegation key per account, refreshed before it runs short."""

    def __init__(self, lifetime: timedelta) -> None:
        if lifetime > timedelta(days=7):
            raise ValueError("user delegation keys last at most 7 days")
        self._lifetime = lifetime
        self._keys: dict[str, UserDelegationKey] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(
        self, account: str, client: BlobServiceClient, now: datetime, needed_until: datetime
    ) -> UserDelegationKey:
        key = self._keys.get(account)
        if key is not None and _parse(key.signed_expiry) - KEY_REFRESH_MARGIN >= needed_until:
            return key
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            key = self._keys.get(account)
            if key is not None and _parse(key.signed_expiry) - KEY_REFRESH_MARGIN >= needed_until:
                return key
            expiry = max(now + self._lifetime, needed_until + KEY_REFRESH_MARGIN)
            expiry = min(expiry, now + timedelta(days=7) - timedelta(minutes=1))
            key = await client.get_user_delegation_key(now - START_SKEW, expiry)
            self._keys[account] = key
            return key


def blob_sas_url(
    *,
    endpoint: str,
    account: str,
    container: str,
    blob: str,
    key: UserDelegationKey,
    access: EdgeAccess,
    start: datetime,
    expiry: datetime,
    version_id: str | None = None,
) -> str:
    """A blob URL carrying a user-delegation SAS for exactly that blob.

    A read of a version is signed with ``sr=bv`` and the URL carries
    ``versionid=`` so the edge's GET selects that version; a write SAS is
    never version-scoped.
    """
    if access is EdgeAccess.WRITE and version_id is not None:
        raise ValueError("a write SAS is for the blob, not a version")
    if access is EdgeAccess.WRITE and expiry - start > EDGE_WRITE_SAS_TTL + START_SKEW:
        raise ValueError(f"a write SAS lasts at most {EDGE_WRITE_SAS_TTL}")
    token = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=blob,
        user_delegation_key=key,
        permission=PERMISSIONS[access],
        start=start,
        expiry=expiry,
        protocol="https",
        version_id=version_id,
    )
    url = f"{endpoint.rstrip('/')}/{quote(container)}/{quote(blob, safe='/~')}?{token}"
    if version_id is not None:
        url += f"&versionid={quote(version_id, safe='')}"
    return url
