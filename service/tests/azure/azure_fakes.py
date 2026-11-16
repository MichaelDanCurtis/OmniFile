"""Test doubles for the Azure backend tests.

This directory has no ``__init__.py`` on purpose: as a package it would be
imported as top-level ``azure`` (the other test directories are top-level
packages) and shadow the Azure SDK's namespace package. So helpers live in
this uniquely named module.

An in-memory Azure Blob service behind an azure-core async transport.

The real SDK builds every request, signs it with a Bearer token and parses
every response, so tests check the exact wire shapes the backend produces
(headers, query, XML) without any network. It is a test double, not a
simulator: it models just enough of each operation for the backend's code
paths, and every request is recorded in `FakeAzure.requests`.

Copy Blob source URLs must carry a user-delegation SAS minted from a key this
fake issued; it is checked with sim_http's validator (the same string-to-sign
rules sim_http enforces for the edge).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit
from xml.sax.saxutils import escape

from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest._http_response_impl_async import AsyncHttpResponseImpl
from azure.core.utils import CaseInsensitiveDict
from azure.storage.extensions import checksums

from omnifile_service.backends.sim_http import sas as simsas


class ManualClock:
    """Virtual time moved by hand (same as the contract suite's)."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


ONLINE = {"Hot", "Cool", "Cold", "Smart"}


@dataclass
class Req:
    method: str
    account: str
    container: str | None
    blob: str | None
    query: dict[str, str]
    headers: CaseInsensitiveDict
    body: bytes


@dataclass
class Version:
    vid: str
    data: bytes
    tier: str
    explicit: bool
    metadata: dict[str, str]
    md5: bytes | None
    created: datetime
    tags: dict[str, str] = field(default_factory=dict)
    current: bool = True
    archive_status: str | None = None
    copy_id: str | None = None
    copy_status: str | None = None
    copy_desc: str | None = None
    copy_source: str | None = None
    policy_until: datetime | None = None
    policy_mode: str | None = None
    legal_hold: bool = False
    etag: str = ""
    due: datetime | None = None  # when a pending rehydrate copy completes
    #: Committed block list [(wire name, size)], for Get Block List.
    blocks: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class Account:
    name: str
    smart: bool
    versioning: bool = True
    containers: dict[str, dict[str, list[Version]]] = field(default_factory=dict)
    blocks: dict[tuple[str, str], dict[str, bytes]] = field(default_factory=dict)


class AzureFault(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message or code


class _Stream:
    """What a transport's stream-download generator returns: an async
    iterator object (the SDK sets attributes on it)."""

    def __init__(self, data: bytes, response: Any) -> None:
        self._parts = [data] if data else []
        self.response = response
        self.request = getattr(response, "request", None)
        self.content_length = len(data)

    def __len__(self) -> int:
        return self.content_length

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> bytes:
        if not self._parts:
            raise StopAsyncIteration
        return self._parts.pop(0)


class _Internal:
    async def close(self) -> None:
        return None


def _fmt(t: datetime) -> str:
    return format_datetime(t.astimezone(UTC), usegmt=True)


def _iso7(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


class FakeAzure(AsyncHttpTransport):
    """Hosts: ``https://<account>.blob.core.windows.net``."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self.now = now
        self.accounts: dict[str, Account] = {}
        self.requests: list[Req] = []
        self.keys: list[simsas.UserDelegationKey] = []
        self.copy_mode = "success"  # or "pending": async copies wait for complete_copies()
        self.fail: dict[tuple[str, str], AzureFault] = {}  # (method, comp) -> one-shot fault
        self._seq = itertools.count(1)

    # --- setup / helpers for tests -------------------------------------

    def add_account(self, name: str, *, smart: bool = False, versioning: bool = True) -> Account:
        acct = Account(name, smart, versioning)
        self.accounts[name] = acct
        return acct

    def container(self, account: str, name: str) -> dict[str, list[Version]]:
        return self.accounts[account].containers.setdefault(name, {})

    def current(self, account: str, container: str, blob: str) -> Version | None:
        for v in self.accounts[account].containers.get(container, {}).get(blob, []):
            if v.current:
                return v
        return None

    def put_raw(self, account: str, container: str, blob: str, data: bytes) -> None:
        """Store a blob directly (e.g. change-feed files)."""
        self._commit(self.accounts[account], container, blob, data, None, {}, None)

    def complete_copies(self, *, fail: bool = False) -> None:
        for acct in self.accounts.values():
            for blobs in acct.containers.values():
                for versions in blobs.values():
                    for v in versions:
                        if v.copy_status == "pending":
                            v.copy_status = "failed" if fail else "success"
                        if v.archive_status and not fail:
                            v.archive_status = None
                            v.tier = "Hot"

    def _settle(self) -> None:
        """Rehydrate copies finish on the clock: High 1 h, Standard 15 h."""
        now = self.now()
        for acct in self.accounts.values():
            for blobs in acct.containers.values():
                for versions in blobs.values():
                    for v in versions:
                        if v.due is not None and v.due <= now and v.archive_status:
                            v.archive_status, v.tier, v.due = None, "Hot", None

    def of(self, method: str, comp: str | None = None) -> list[Req]:
        return [r for r in self.requests if r.method == method and r.query.get("comp") == comp]

    # --- transport ------------------------------------------------------

    async def __aenter__(self) -> FakeAzure:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(self, request: Any, **kwargs: Any) -> Any:
        parts = urlsplit(request.url)
        account = parts.hostname.split(".")[0]
        path = unquote(parts.path).lstrip("/")
        container, _, blob = path.partition("/")
        content = request.content
        if isinstance(content, bytes | bytearray):
            body = bytes(content)
        elif isinstance(content, str):
            body = content.encode()
        elif content is None:
            body = b""
        elif hasattr(content, "read"):
            body = content.read()
        else:
            body = b"".join(content)
        req = Req(
            request.method,
            account,
            container or None,
            blob or None,
            dict(parse_qsl(parts.query, keep_blank_values=True)),
            CaseInsensitiveDict(dict(request.headers)),
            bytes(body),
        )
        self.requests.append(req)
        self._settle()
        try:
            status, headers, out = self._dispatch(req)
        except AzureFault as f:
            status = f.status
            headers = {"x-ms-error-code": f.code, "Content-Type": "application/xml"}
            out = (
                f"<?xml version='1.0' encoding='utf-8'?><Error><Code>{f.code}</Code>"
                f"<Message>{escape(f.message)}</Message></Error>"
            ).encode()
            if req.method == "HEAD":
                out = b""
        headers = {"x-ms-request-id": "fake", "x-ms-version": "2026-10-06", **headers}
        headers.setdefault("Content-Length", str(len(out)))

        def gen(pipeline: Any = None, response: Any = None, **k: Any) -> _Stream:
            return _Stream(out, response)

        response = AsyncHttpResponseImpl(
            request=request,
            internal_response=_Internal(),
            status_code=status,
            reason="fake",
            content_type=headers.get("Content-Type", "application/octet-stream"),
            headers=CaseInsensitiveDict(headers),
            stream_download_generator=gen,
        )
        if not kwargs.get("stream", False):
            await response.read()  # as the aiohttp transport does for non-streamed calls
        return response

    # --- operations -----------------------------------------------------

    def _dispatch(self, r: Req) -> tuple[int, dict[str, str], bytes]:
        acct = self.accounts.get(r.account)
        if acct is None:
            raise AzureFault(404, "AccountNotFound")
        if not r.headers.get("Authorization", "").startswith("Bearer "):
            raise AzureFault(401, "NoAuthenticationInformation")
        comp = r.query.get("comp")
        fault = self.fail.pop((r.method, comp or ""), None)
        if fault:
            raise fault
        if r.query.get("restype") == "service" and comp == "userdelegationkey":
            return self._udk(r)
        if r.query.get("restype") == "container":
            if r.method == "PUT" and comp is None:
                if r.container in acct.containers:
                    raise AzureFault(409, "ContainerAlreadyExists")
                acct.containers[r.container] = {}
                return 201, {}, b""
            if r.method == "GET" and comp == "list":
                return self._list(acct, r)
        if r.container not in acct.containers:
            raise AzureFault(404, "ContainerNotFound")
        handler = {
            ("PUT", "block"): self._put_block,
            ("PUT", "blocklist"): self._put_block_list,
            ("HEAD", None): self._head,
            ("GET", None): self._get,
            ("GET", "blocklist"): self._get_block_list,
            ("PUT", "tier"): self._set_tier,
            ("DELETE", None): self._delete,
            ("PUT", "tags"): self._set_tags,
            ("GET", "tags"): self._get_tags,
            ("PUT", "metadata"): self._set_metadata,
            ("PUT", "immutabilityPolicies"): self._set_policy,
            ("PUT", "legalhold"): self._legal_hold,
        }.get((r.method, comp))
        if r.method == "PUT" and comp is None and "x-ms-copy-source" in r.headers:
            handler = self._copy
        if handler is None:
            raise AzureFault(501, "NotImplemented", f"{r.method} comp={comp}")
        return handler(acct, r)

    def _version(self, acct: Account, r: Req) -> Version:
        versions = acct.containers[r.container].get(r.blob or "", [])
        vid = r.query.get("versionid")
        for v in versions:
            if (vid and v.vid == vid) or (not vid and v.current):
                return v
        raise AzureFault(404, "BlobNotFound")

    def _blob_headers(self, v: Version) -> dict[str, str]:
        h = {
            "Content-Length": str(len(v.data)),
            "Content-Type": "application/octet-stream",
            "Last-Modified": _fmt(v.created),
            "ETag": v.etag,
            "x-ms-blob-type": "BlockBlob",
            "x-ms-version-id": v.vid,
            "x-ms-is-current-version": "true" if v.current else "false",
            "x-ms-access-tier": v.tier,
            "x-ms-tag-count": str(len(v.tags)),
        }
        if not v.explicit:
            h["x-ms-access-tier-inferred"] = "true"
        if v.md5:
            h["Content-MD5"] = base64.b64encode(v.md5).decode()
        if v.archive_status:
            h["x-ms-archive-status"] = v.archive_status
        if v.copy_id:
            h["x-ms-copy-id"] = v.copy_id
            h["x-ms-copy-status"] = v.copy_status or "success"
            h["x-ms-copy-source"] = v.copy_source or ""
            h["x-ms-copy-completion-time"] = _fmt(v.created)
            if v.copy_desc:
                h["x-ms-copy-status-description"] = v.copy_desc
        if v.policy_until:
            h["x-ms-immutability-policy-until-date"] = _fmt(v.policy_until)
            h["x-ms-immutability-policy-mode"] = v.policy_mode or "locked"
        if v.legal_hold:
            h["x-ms-legal-hold"] = "true"
        for k, val in v.metadata.items():
            h[f"x-ms-meta-{k}"] = val
        return h

    def _new_version(self) -> tuple[str, str]:
        n = next(self._seq)
        t = self.now() + timedelta(microseconds=n)
        return _iso7(t), f'"0x{n:016X}"'

    def _protected(self, v: Version) -> bool:
        return v.legal_hold or (v.policy_until is not None and v.policy_until > self.now())

    def _commit(
        self,
        acct: Account,
        container: str,
        blob: str,
        data: bytes,
        tier: str | None,
        meta: dict[str, str],
        md5: bytes | None,
        **extra: Any,
    ) -> Version:
        versions = acct.containers.setdefault(container, {}).setdefault(blob, [])
        old = next((v for v in versions if v.current), None)
        if old is not None and old.tier == "Archive" and old.copy_status != "pending":
            raise AzureFault(409, "BlobArchived")
        if old is not None and not acct.versioning:
            if self._protected(old):
                raise AzureFault(409, "BlobImmutableDueToPolicy")
            versions.remove(old)
        for v in versions:
            v.current = False
        if tier is None:
            tier = "Smart" if acct.smart else "Hot"
            explicit = False
        else:
            if tier == "Archive" and acct.smart:
                raise AzureFault(409, "BlobAccessTierNotSupportedForAccountType")
            explicit = True
        vid, etag = self._new_version()
        v = Version(vid, data, tier, explicit, dict(meta), md5, self.now(), etag=etag, **extra)
        versions.append(v)
        return v

    def _put_block(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        crc_hdr = r.headers.get("x-ms-content-crc64")
        h: dict[str, str] = {"x-ms-request-server-encrypted": "true"}
        if crc_hdr is not None:
            want = base64.b64encode(checksums.crc64.compute(r.body, 0).to_bytes(8, "little"))
            if crc_hdr.encode() != want:
                raise AzureFault(400, "Crc64Mismatch")
            h["x-ms-content-crc64"] = crc_hdr
        bid = r.query["blockid"]
        acct.blocks.setdefault((r.container, r.blob), {})[bid] = r.body
        return 201, h, b""

    def _put_block_list(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        staged = acct.blocks.pop((r.container, r.blob), {})
        root = ET.fromstring(r.body)
        data = b""
        committed: list[tuple[str, int]] = []
        for el in root:
            bid = el.text or ""
            if bid not in staged:
                raise AzureFault(400, "InvalidBlockList")
            data += staged[bid]
            committed.append((bid, len(staged[bid])))
        meta = {
            k[len("x-ms-meta-") :]: v
            for k, v in r.headers.items()
            if k.lower().startswith("x-ms-meta-")
        }
        md5_hdr = r.headers.get("x-ms-blob-content-md5")
        v = self._commit(
            acct,
            r.container,
            r.blob,
            data,
            r.headers.get("x-ms-access-tier"),
            meta,
            base64.b64decode(md5_hdr) if md5_hdr else None,
            blocks=committed,
        )
        return (
            201,
            {"ETag": v.etag, "Last-Modified": _fmt(v.created), "x-ms-version-id": v.vid},
            b"",
        )

    def _get_block_list(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        """Get Block List: names and sizes only, as Azure returns them."""
        if r.query.get("blocklisttype", "committed") != "committed":
            raise AzureFault(501, "NotImplemented", "only committed block lists")
        v = self._version(acct, r)
        items = "".join(f"<Block><Name>{n}</Name><Size>{z}</Size></Block>" for n, z in v.blocks)
        body = (
            '<?xml version="1.0" encoding="utf-8"?><BlockList>'
            f"<CommittedBlocks>{items}</CommittedBlocks><UncommittedBlocks /></BlockList>"
        )
        h = {
            "Content-Type": "application/xml",
            "ETag": v.etag,
            "x-ms-blob-content-length": str(len(v.data)),
        }
        return 200, h, body.encode()

    def _head(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        return 200, self._blob_headers(self._version(acct, r)), b""

    def _get(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        if v.tier == "Archive":
            raise AzureFault(409, "BlobBeingRehydrated" if v.archive_status else "BlobArchived")
        rng = r.headers.get("x-ms-range") or r.headers.get("Range")
        h = self._blob_headers(v)
        if not rng:
            return 200, h, v.data
        start_s, _, end_s = rng.removeprefix("bytes=").partition("-")
        start = int(start_s)
        if start >= len(v.data):
            raise AzureFault(416, "InvalidRange")
        end = min(int(end_s) if end_s else len(v.data) - 1, len(v.data) - 1)
        part = v.data[start : end + 1]
        h["Content-Length"] = str(len(part))
        h["Content-Range"] = f"bytes {start}-{end}/{len(v.data)}"
        return 206, h, part

    def _set_tier(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        tier = r.headers["x-ms-access-tier"]
        if tier == "Archive" and acct.smart:
            raise AzureFault(409, "BlobAccessTierNotSupportedForAccountType")
        if v.tier == "Archive" and tier in ONLINE:
            v.archive_status = f"rehydrate-pending-to-{tier.lower()}"
            return 202, {}, b""
        v.tier, v.explicit = tier, True
        return 200, {}, b""

    def _copy(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        src_url = r.headers["x-ms-copy-source"]
        sp = urlsplit(src_url)
        s_acct = sp.hostname.split(".")[0]
        s_cont, _, s_blob = unquote(sp.path).lstrip("/").partition("/")
        q = dict(parse_qsl(sp.query))
        try:
            simsas.validate(
                self.keys,
                q,
                account=s_acct,
                container=s_cont,
                blob=s_blob,
                request_version_id=q.get("versionid"),
                now=self.now(),
                scheme=sp.scheme,
                client_ip=None,
            )
        except Exception as e:  # noqa: BLE001
            raise AzureFault(403, "CannotVerifyCopySource", str(e)) from None
        vq = {"versionid": q["versionid"]} if q.get("versionid") else {}
        src = self._version(
            self.accounts[s_acct],
            Req("GET", s_acct, s_cont, s_blob, vq, CaseInsensitiveDict(), b""),
        )
        tier = r.headers.get("x-ms-access-tier")
        archived_src = src.tier == "Archive"
        if archived_src and tier not in ONLINE:
            raise AzureFault(409, "BlobArchived")
        tags = dict(parse_qsl(r.headers.get("x-ms-tags", "")))
        pending = self.copy_mode == "pending" or archived_src
        copy_id = f"copy-{next(self._seq)}"
        v = self._commit(
            acct,
            r.container,
            r.blob,
            src.data,
            "Archive" if archived_src else tier,
            src.metadata,
            src.md5,
            tags=tags,
            copy_id=copy_id,
            copy_status="pending" if pending and not archived_src else "success",
            copy_source=src_url.split("?")[0],
            archive_status=f"rehydrate-pending-to-{tier.lower()}" if archived_src else None,
        )
        if archived_src:
            v.explicit = True
            high = r.headers.get("x-ms-rehydrate-priority", "Standard") == "High"
            v.due = self.now() + timedelta(hours=1 if high else 15)
        return (
            202,
            {
                "ETag": v.etag,
                "Last-Modified": _fmt(v.created),
                "x-ms-version-id": v.vid,
                "x-ms-copy-id": copy_id,
                "x-ms-copy-status": "pending" if pending else "success",
            },
            b"",
        )

    def _delete(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        versions = acct.containers[r.container].get(r.blob or "", [])
        v = self._version(acct, r)
        if r.query.get("versionid"):
            if self._protected(v):
                raise AzureFault(
                    409,
                    "BlobImmutableDueToLegalHold" if v.legal_hold else "BlobImmutableDueToPolicy",
                )
            versions.remove(v)
        elif acct.versioning:
            v.current = False
        else:
            if self._protected(v):
                raise AzureFault(409, "BlobImmutableDueToPolicy")
            versions.remove(v)
        return 202, {}, b""

    def _set_tags(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        root = ET.fromstring(r.body)
        v.tags = {t.findtext("Key") or "": t.findtext("Value") or "" for t in root.iter("Tag")}
        if len(v.tags) > 10:
            raise AzureFault(400, "InvalidTag")
        return 204, {}, b""

    def _get_tags(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        tags = "".join(
            f"<Tag><Key>{escape(k)}</Key><Value>{escape(val)}</Value></Tag>"
            for k, val in v.tags.items()
        )
        body = f'<?xml version="1.0" encoding="utf-8"?><Tags><TagSet>{tags}</TagSet></Tags>'
        return 200, {"Content-Type": "application/xml"}, body.encode()

    def _set_metadata(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        if v.tier == "Archive":
            raise AzureFault(409, "BlobArchived")
        meta = {
            k[len("x-ms-meta-") :]: val
            for k, val in r.headers.items()
            if k.lower().startswith("x-ms-meta-")
        }
        if acct.versioning:
            new = self._commit(acct, r.container, r.blob, v.data, None, meta, v.md5, tags=v.tags)
            new.tier, new.explicit = v.tier, v.explicit
            return 200, {"ETag": new.etag, "x-ms-version-id": new.vid}, b""
        v.metadata = meta
        return 200, {"ETag": v.etag}, b""

    def _set_policy(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        until = datetime.strptime(
            r.headers["x-ms-immutability-policy-until-date"], "%a, %d %b %Y %H:%M:%S GMT"
        ).replace(tzinfo=UTC)
        mode = r.headers["x-ms-immutability-policy-mode"].lower()
        if v.policy_mode == "locked" and v.policy_until and until < v.policy_until:
            raise AzureFault(409, "ImmutabilityPolicyLocked")
        v.policy_until, v.policy_mode = until, mode
        return 200, {}, b""

    def _legal_hold(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        v = self._version(acct, r)
        v.legal_hold = r.headers["x-ms-legal-hold"].lower() == "true"
        return 200, {"x-ms-legal-hold": "true" if v.legal_hold else "false"}, b""

    def _udk(self, r: Req) -> tuple[int, dict[str, str], bytes]:
        root = ET.fromstring(r.body)
        start = simsas.parse_time(root.findtext("Start") or "")
        expiry = simsas.parse_time(root.findtext("Expiry") or "")
        key = simsas.UserDelegationKey.issue(start, expiry - start)
        self.keys.append(key)
        fields = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in key.to_json().items())
        body = (
            f'<?xml version="1.0" encoding="utf-8"?><UserDelegationKey>{fields}</UserDelegationKey>'
        )
        return 200, {"Content-Type": "application/xml"}, body.encode()

    def _list(self, acct: Account, r: Req) -> tuple[int, dict[str, str], bytes]:
        if r.container not in acct.containers:
            raise AzureFault(404, "ContainerNotFound")
        prefix = r.query.get("prefix", "")
        include = set(r.query.get("include", "").split(","))
        rows: list[tuple[str, Version]] = []
        for name in sorted(acct.containers[r.container]):
            if not name.startswith(prefix):
                continue
            for v in acct.containers[r.container][name]:
                if v.current or "versions" in include:
                    rows.append((name, v))
        marker = int(r.query.get("marker") or 0)
        size = int(r.query.get("maxresults") or 5000)
        page = rows[marker : marker + size]
        nxt = str(marker + size) if marker + size < len(rows) else ""
        out = []
        for name, v in page:
            meta = ""
            if "metadata" in include:
                meta = (
                    "<Metadata>"
                    + "".join(f"<{k}>{escape(val)}</{k}>" for k, val in v.metadata.items())
                    + "</Metadata>"
                )
            arch = f"<ArchiveStatus>{v.archive_status}</ArchiveStatus>" if v.archive_status else ""
            inferred = "<AccessTierInferred>true</AccessTierInferred>" if not v.explicit else ""
            out.append(
                f"<Blob><Name>{escape(name)}</Name><VersionId>{v.vid}</VersionId>"
                f"<IsCurrentVersion>{'true' if v.current else 'false'}</IsCurrentVersion>"
                f"<Properties><Last-Modified>{_fmt(v.created)}</Last-Modified>"
                f"<Etag>{v.etag}</Etag><Content-Length>{len(v.data)}</Content-Length>"
                f"<Content-Type>application/octet-stream</Content-Type>"
                f"<BlobType>BlockBlob</BlobType><AccessTier>{v.tier}</AccessTier>{inferred}{arch}"
                f"</Properties>{meta}</Blob>"
            )
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<EnumerationResults ServiceEndpoint="https://{acct.name}.blob.core.windows.net/" '
            f'ContainerName="{escape(r.container)}"><Prefix>{escape(prefix)}</Prefix>'
            f"<Blobs>{''.join(out)}</Blobs><NextMarker>{nxt}</NextMarker></EnumerationResults>"
        )
        return 200, {"Content-Type": "application/xml"}, body.encode()


def md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()
