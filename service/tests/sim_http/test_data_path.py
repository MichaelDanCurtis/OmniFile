"""Put Block / Put Block List / Get Blob / Get Blob Properties round trips."""

from __future__ import annotations

import base64
import hashlib

from omnifile_service.backends.base import ObjectRef
from omnifile_service.backends.sim import crc64nvme
from omnifile_service.backends.sim_http import CRC64_MISMATCH_CODE, BlobFacade, azure_crc64

from .client import Blob

DATA = bytes(range(256)) * 3 + b"tail"


def test_azure_crc64_is_little_endian_base64() -> None:
    # Check value 0xAE8B14860A799888, little-endian, base64.
    raw = base64.b64decode(azure_crc64(b"123456789"))
    assert int.from_bytes(raw, "little") == 0xAE8B14860A799888


def test_round_trip_records_one_crc_per_block(blob: Blob, facade: BlobFacade) -> None:
    sas = blob.sas()
    r = blob.upload(DATA, sas, block_size=100)
    assert r.status_code == 201, r.text
    assert r.headers["x-ms-request-id"] and r.headers["x-ms-version"] == "2026-04-06"
    assert r.headers["x-ms-request-server-encrypted"] == "true"
    version = r.headers["x-ms-version-id"]
    got = blob.get(sas)
    assert got.status_code == 200 and got.content == DATA
    assert got.headers["x-ms-version-id"] == version
    assert got.headers["x-ms-is-current-version"] == "true"
    assert got.headers["content-length"] == str(len(DATA))
    ref = ObjectRef(facade.sim.backend_id, "live/data", "dir/line_1042.segy")
    blocks = facade.sim.block_checksums(ref)
    assert [n for n, _ in blocks] == [100] * 7 + [len(DATA) - 700]
    assert blocks[0][1] == crc64nvme(DATA[:100]).to_bytes(8, "big")


def test_put_block_echoes_crc_and_rejects_a_bad_one(blob: Blob) -> None:
    sas = blob.sas()
    ok = blob.put_block(b"b1", b"hello", sas)
    assert ok.status_code == 201
    assert ok.headers["x-ms-content-crc64"] == azure_crc64(b"hello")
    big_endian = base64.b64encode(crc64nvme(b"hello").to_bytes(8, "big")).decode()
    bad = blob.put_block(b"b2", b"hello", sas, crc=big_endian)
    assert bad.status_code == 400
    assert bad.headers["x-ms-error-code"] == CRC64_MISMATCH_CODE
    assert b"<Code>" + CRC64_MISMATCH_CODE.encode() + b"</Code>" in bad.content
    assert bad.headers["content-type"].startswith("application/xml")
    malformed = blob.put_block(b"b3", b"hello", sas, crc="!!")
    assert malformed.headers["x-ms-error-code"] == "InvalidHeaderValue"
    # A rejected block is not staged.
    assert blob.put_block_list([b"b2"], sas).headers["x-ms-error-code"] == "InvalidBlockList"


def test_content_md5_on_put_block(blob: Blob) -> None:
    sas = blob.sas()
    good = base64.b64encode(hashlib.md5(b"x").digest()).decode()
    r = blob.put_block(b"m1", b"x", sas, crc=None, headers={"Content-MD5": good})
    assert r.status_code == 201 and r.headers["content-md5"] == good
    bad = blob.put_block(b"m2", b"y", sas, crc=None, headers={"Content-MD5": good})
    assert bad.headers["x-ms-error-code"] == "Md5Mismatch"
    both = blob.put_block(b"m3", b"x", sas, headers={"Content-MD5": good})
    assert both.status_code == 400


def test_block_ids_must_be_base64_and_the_same_length(blob: Blob) -> None:
    sas = blob.sas()
    r = blob.client.put(
        f"{blob.path}?comp=block&blockid=not*base64&{sas}",
        content=b"x",
        headers={"x-ms-version": "2026-04-06"},
    )
    assert r.headers["x-ms-error-code"] == "InvalidBlockId"
    assert blob.put_block(b"aaaa", b"x", sas).status_code == 201
    assert blob.put_block(b"bbbbbbbb", b"x", sas).headers["x-ms-error-code"] == "InvalidBlobOrBlock"


def test_range_reads(blob: Blob) -> None:
    sas = blob.sas()
    blob.upload(DATA, sas, block_size=64)
    r = blob.get(sas, {"x-ms-range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == DATA[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(DATA)}"
    r = blob.get(sas, {"Range": f"bytes={len(DATA) - 3}-"})
    assert r.status_code == 206 and r.content == DATA[-3:]
    r = blob.get(sas, {"x-ms-range": "bytes=0-1", "Range": "bytes=5-6"})  # x-ms-range wins
    assert r.content == DATA[0:2]
    r = blob.get(sas, {"x-ms-range": "bytes=0-99999"})  # clamped to the end
    assert r.content == DATA
    r = blob.get(sas, {"x-ms-range": f"bytes={len(DATA)}-"})
    assert r.status_code == 416 and r.headers["x-ms-error-code"] == "InvalidRange"


def test_range_crc64(blob: Blob) -> None:
    sas = blob.sas()
    blob.upload(DATA, sas, block_size=64)
    r = blob.get(sas, {"x-ms-range": "bytes=3-200", "x-ms-range-get-content-crc64": "true"})
    assert r.headers["x-ms-content-crc64"] == azure_crc64(DATA[3:201])
    r = blob.get(sas, {"x-ms-range-get-content-crc64": "true"})  # no range
    assert r.status_code == 400
    r = blob.get(
        sas,
        {
            "x-ms-range": "bytes=0-1",
            "x-ms-range-get-content-crc64": "true",
            "x-ms-range-get-content-md5": "true",
        },
    )
    assert r.status_code == 400


def test_range_crc64_limited_to_4_mib(blob: Blob, facade: BlobFacade) -> None:
    facade.sim.put_synthetic("live/data", "big", 5 * 1024 * 1024)
    big = Blob(blob.client, facade, "live", "data", "big")
    sas = big.sas("r")
    crc = {"x-ms-range-get-content-crc64": "true"}
    r = big.get(sas, {"x-ms-range": f"bytes=0-{4 * 1024 * 1024}", **crc})
    assert r.status_code == 400
    r = big.get(sas, {"x-ms-range": f"bytes=0-{4 * 1024 * 1024 - 1}", **crc})
    assert r.status_code == 206 and "x-ms-content-crc64" in r.headers


def test_properties_and_metadata_round_trip(blob: Blob) -> None:
    sas = blob.sas()
    md5 = base64.b64encode(hashlib.md5(DATA).digest()).decode()
    r = blob.upload(
        DATA,
        sas,
        headers={
            "x-ms-meta-file_owner": "1001",
            "x-ms-meta-file_permissions": "0640",
            "x-ms-blob-content-md5": md5,
            "x-ms-blob-content-type": "application/x-segy",
        },
    )
    assert r.status_code == 201, r.text
    h = blob.head(sas)
    assert h.status_code == 200 and h.content == b""
    assert h.headers["content-length"] == str(len(DATA))
    assert h.headers["x-ms-meta-file_owner"] == "1001"
    assert h.headers["x-ms-meta-file_permissions"] == "0640"
    assert h.headers["content-md5"] == md5
    assert h.headers["content-type"] == "application/x-segy"
    assert h.headers["x-ms-access-tier"] == "Smart"  # no tier header: the account default
    assert h.headers["x-ms-blob-type"] == "BlockBlob"
    assert h.headers["x-ms-version-id"]
    full = blob.get(sas)
    assert full.headers["content-md5"] == md5
    part = blob.get(sas, {"x-ms-range": "bytes=0-9"})
    assert "content-md5" not in part.headers and part.headers["x-ms-blob-content-md5"] == md5


def test_hyphenated_metadata_names_are_refused(blob: Blob) -> None:
    """Azure metadata names follow C# identifier rules (Put Block List)."""
    sas = blob.sas()
    r = blob.upload(b"x", sas, headers={"x-ms-meta-file-owner": "1001"})
    assert r.status_code == 400 and r.headers["x-ms-error-code"] == "InvalidMetadata"


def test_explicit_tier_and_inheritance(blob: Blob) -> None:
    sas = blob.sas()
    blob.upload(b"one", sas, headers={"x-ms-access-tier": "Cool"})
    assert blob.head(sas).headers["x-ms-access-tier"] == "Cool"
    blob.upload(b"two", sas)  # overwrite with no tier header inherits Cool
    assert blob.head(sas).headers["x-ms-access-tier"] == "Cool"
    r = blob.upload(b"three", sas, headers={"x-ms-access-tier": "Lukewarm"})
    assert r.headers["x-ms-error-code"] == "InvalidBlobTier"
    r = blob.upload(b"four", sas, headers={"x-ms-access-tier": "Archive"})  # ZRS account
    assert r.status_code == 409
    assert r.headers["x-ms-error-code"] == "BlobAccessTierNotSupportedForAccountType"


def test_committed_blocks_can_be_reused(blob: Blob) -> None:
    sas = blob.sas()
    for bid, data in ((b"A", b"aaa"), (b"B", b"bbb")):
        blob.put_block(bid, data, sas)
    assert blob.put_block_list([b"A", b"B"], sas).status_code == 201
    blob.put_block(b"C", b"ccc", sas)
    r = blob.put_block_list([b"B"], sas, kind="Committed")
    assert r.status_code == 201
    assert blob.get(sas).content == b"bbb"
    # Uncommitted blocks not in the last list were discarded.
    assert blob.put_block_list([b"C"], sas, kind="Uncommitted").status_code == 400


def test_old_versions_are_readable(blob: Blob) -> None:
    sas = blob.sas()
    v1 = blob.upload(b"first", sas).headers["x-ms-version-id"]
    blob.upload(b"second", sas)
    old = blob.get(sas, version=v1)
    assert old.content == b"first" and old.headers["x-ms-is-current-version"] == "false"


def test_missing_blob_and_unknown_operations(blob: Blob) -> None:
    sas = blob.sas()
    r = blob.get(sas)
    assert r.status_code == 404 and r.headers["x-ms-error-code"] == "BlobNotFound"
    h = blob.head(sas)
    assert h.status_code == 404 and h.headers["x-ms-error-code"] == "BlobNotFound"
    assert h.content == b""
    r = blob.client.put(f"{blob.path}?{sas}", content=b"x", headers={"x-ms-blob-type": "BlockBlob"})
    assert r.status_code == 501
    r = blob.client.get(f"/nosuchaccount/data/x?{sas}")
    assert r.headers["x-ms-error-code"] == "InvalidUri"


def test_uncommitted_blob_is_not_readable(blob: Blob) -> None:
    sas = blob.sas()
    blob.put_block(b"x", b"data", sas)
    assert blob.get(sas).status_code == 404
