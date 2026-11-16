"""A tiny client for one blob URL, calling the facade the way the edge does."""

from __future__ import annotations

import base64
from urllib.parse import quote

import httpx
from starlette.testclient import TestClient

from omnifile_service.backends.sim_http import BlobFacade, azure_crc64

VERSION = "2026-04-06"


class Blob:
    def __init__(
        self, client: TestClient, facade: BlobFacade, account: str, container: str, blob: str
    ) -> None:
        self.client = client
        self.facade = facade
        self.account, self.container, self.blob = account, container, blob
        self.path = f"/{account}/{container}/{blob}"

    def sas(self, permissions: str = "rcw", **kw: object) -> str:
        return self.facade.mint_sas(
            self.account,
            self.container,
            self.blob,
            permissions,
            **kw,  # type: ignore[arg-type]
        )

    def put_block(
        self,
        block_id: bytes,
        data: bytes,
        sas: str,
        *,
        crc: str | None = "auto",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        h = {"x-ms-version": VERSION, **(headers or {})}
        if crc == "auto":
            h["x-ms-content-crc64"] = azure_crc64(data)
        elif crc is not None:
            h["x-ms-content-crc64"] = crc
        bid = quote(base64.b64encode(block_id).decode(), safe="")
        return self.client.put(
            f"{self.path}?comp=block&blockid={bid}&{sas}", content=data, headers=h
        )

    def put_block_list(
        self,
        ids: list[bytes],
        sas: str,
        *,
        kind: str = "Latest",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        items = "".join(f"<{kind}>{base64.b64encode(i).decode()}</{kind}>" for i in ids)
        body = f'<?xml version="1.0" encoding="utf-8"?><BlockList>{items}</BlockList>'
        h = {"x-ms-version": VERSION, **(headers or {})}
        return self.client.put(f"{self.path}?comp=blocklist&{sas}", content=body, headers=h)

    def upload(
        self,
        data: bytes,
        sas: str,
        *,
        block_size: int = 4,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        ids = []
        for n, i in enumerate(range(0, len(data), block_size)):
            bid = f"{n:08d}".encode()
            r = self.put_block(bid, data[i : i + block_size], sas)
            assert r.status_code == 201, r.text
            ids.append(bid)
        return self.put_block_list(ids, sas, headers=headers)

    def get(
        self, sas: str, headers: dict[str, str] | None = None, version: str | None = None
    ) -> httpx.Response:
        q = f"&versionid={version}" if version else ""
        h = {"x-ms-version": VERSION, **(headers or {})}
        return self.client.get(f"{self.path}?{sas}{q}", headers=h)

    def head(self, sas: str, version: str | None = None) -> httpx.Response:
        q = f"&versionid={version}" if version else ""
        return self.client.head(f"{self.path}?{sas}{q}", headers={"x-ms-version": VERSION})
