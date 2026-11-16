# sim_http: an Azure Blob HTTP facade over sim

`sim_http` serves the Azure Blob data-plane REST API over `SimBackend` (`../sim/`), so the Rust edge's real uploader and downloader can run end to end on a laptop without Azure. Floci is no use here: it has no CRC64 and no tiers (`docs/BUILD-PLAN.md`, "Test strategy").

It implements exactly the operations the edge uses. Everything else answers `501 NotImplemented`, a code that is deliberately not an Azure one.

## Running it

```
python -m omnifile_service.backends.sim_http --port 10000 --layout two-account --clock system
```

- **Output:** one JSON line on stdout, `{"url": ..., "layout": ..., "key": {...}}`. The key is the user delegation key, in Get User Delegation Key's field names.
- **`--key-file PATH`:** also writes the key to a file, mode 0600.
- **`--layout`:** `two-account` (a ZRS live account with Smart as default, plus an LRS archive account) or `worm`.
- **`--clock virtual`:** time moves only through `POST /_sim/advance {"seconds": N}`.
- **`--clock offset --clock-file F`:** the wall clock plus the offset in `F` (libfaketime's `+<seconds>`), shared with the api, the worker and, under libfaketime, Postgres. `POST /_sim/advance` moves it for all of them; `GET /_sim/clock` reads it. A user delegation key that lapses is reissued and rewritten to `--key-file`.
- **`POST /_sim/sas`** with `{"account", "container", "blob", "permissions", "ttl_seconds", "version_id"}` returns `{"sas", "url"}`, for non-Python harnesses.
- **`POST /_sim/backend`** with `{"op", "args", "kwargs"}` runs one `SimBackend` control call (tier moves, restores, change feed, retention, holds, tags, versions, deletes, container policy) for another process; see `rpc.py`. The worker's `SimHttpBackend` uses it.
- Every `/_sim/` endpoint is off unless `--admin` is given (dev and tests only: it mints SAS tokens, moves the clock, injects faults and drives the backend).
- **URL style:** path-style, as for Azurite: `http://127.0.0.1:10000/<account>/<container>/<blob>`. Host-style (`<account>.blob.<domain>`) is accepted too.

In Python tests, use `BlobFacade(sim)`, `create_app(facade)`, and `facade.mint_sas(account, container, blob, "rcw", ttl=...)`.

## S3-resident files (`--s3`)

`--s3` (or `BlobFacade(..., s3=SimBackend.s3(clock))`) also hosts an S3-flavoured sim. It is what the edge reads S3-resident files from in the end-to-end stack (M5):

- **Reads:** `GET` and `HEAD /_s3/<bucket>/<key>`, with `versionId` and `Range: bytes=a-b` or `bytes=a-`. The answer carries `x-amz-version-id`. An archived object that isn't restored answers 403 `InvalidObjectState`, as S3 does. With `x-amz-checksum-mode: ENABLED`, a whole-object read answers the stored full-object CRC64NVME (`x-amz-checksum-crc64nvme`, base64 of the big-endian value, and `x-amz-checksum-type: FULL_OBJECT`); a range read does not. Code: `s3get.py`.
- **Signing:** URLs are signed with the facade's user delegation key. `X-Amz-Signature` is an HMAC over the method, bucket, key, version and `X-Sim-Expires`. **This is not SigV4.** The edge treats the URL as opaque, as it does a real presigned URL.
- **The api** mints these URLs for a catalog backend whose config has `data_plane = "sim_http"` and `flavour = "s3"` (`SimHttpDataPlane`, credential mode `s3_presigned_url`).
- **The worker** reaches this sim through `POST /_sim/backend` with `"backend": "s3"` (`SimHttpBackend` with the `s3` layout).

## The S3 layout (M5): `--layout s3`

`s3.py` serves an S3-shaped facade over sim's S3 flavour on one port, ported from line B. Its client is the edge's Rust S3 reader (`omnifile-blob`, `tests/s3_sim.rs`), which reads S3 with IAM Roles Anywhere credentials and SigV4 (no presigned URL).

```
python -m omnifile_service.backends.sim_http --port 0 --layout s3 --admin [--bucket B ...] [--region R] [--clock virtual|offset --clock-file F]
```

- **Output:** one JSON line, `{"url", "layout": "s3", "imds_url", "region", "access_key_id", "secret_access_key"}`. The credentials are the edge's static test ones, never real ones. `imds_url` is the same port.
- **S3**, path style or virtual host (when the first `Host` label is an existing bucket), every request checked with SigV4 (`sigv4.py`: header auth only; `SignatureDoesNotMatch`, `InvalidAccessKeyId`, `InvalidToken`, `ExpiredToken`, and `RequestTimeTooSkewed` with `--clock system` only):
  - GetObject with `Range`, `versionId`, `x-amz-checksum-mode` (a full-object CRC64NVME on whole reads only), and `If-Match` (412 `PreconditionFailed`, evaluated before the archived check). An archived object that isn't restored answers 403 `InvalidObjectState`;
  - HeadObject with `x-amz-restore`, `x-amz-archive-status`, `x-amz-storage-class` and `x-amz-meta-*`;
  - requester pays: 403 `AccessDenied` without `x-amz-request-payer: requester`;
  - PutObject (one part; `x-amz-checksum-crc64nvme` and `Content-MD5` checked), CopyObject (no metadata `REPLACE`), Get/Put/DeleteObjectTagging, RestoreObject, DeleteObject and CreateBucket.
- **STS** `AssumeRoleWithWebIdentity` (unsigned, as on AWS), on the sim clock; the token `invalid` gets `InvalidIdentityToken`.
- **IMDSv2, as `aws_signing_helper serve` offers it:** `PUT /latest/api/token`, then the role name and the credential JSON (`Code`, `AccessKeyId`, `SecretAccessKey`, `Token`, `Expiration`). A missing or lapsed token gets 401.
- **Admin routes** (`--admin`): `POST /_sim/s3/buckets {bucket, versioning, requester_pays}`; `POST /_sim/s3/objects {bucket, key, data_b64 | synthetic_size + seed, storage_class, metadata, crc64nvme_b64, it_archive, content_type}` → `{version_id, etag, crc64nvme_b64}` (synthetic bytes: byte *i* = (seed + *i*) mod 251, never held in memory); `POST /_sim/s3/restore {bucket, key, version_id, speed, days}`; `GET /_sim/s3/stats` (per bucket: `GET`, `HEAD`, `PUT`, `DELETE`, `TAG`, `COPY_FROM`, `RESTORE`, `bytes_out`) and `POST /_sim/s3/stats/reset`; `GET|POST /_sim/imds {ttl_seconds}` (credential lifetime and counters); `GET /_sim/sts`; `POST /_sim/advance`, `GET /_sim/clock`, `POST /_sim/backend`.
- **Not in this layout** (B's has them, over sim features this tree lacks): multipart uploads, ListObjects/ListObjectVersions, S3 Control Batch jobs and the EventBridge → SQS queue. They answer `501 NotImplemented`.

## What it implements

Sources: Microsoft's REST reference at https://learn.microsoft.com/en-us/rest/api/storageservices/ (read 2026-10-05), and the `StorageErrorCode` enum in `azure-rest-api-specs`, `Microsoft.BlobStorage/stable/2026-12-06/blob.json`.

### Put Block (`PUT ?comp=block&blockid=`), per `put-block`

- **Block ID:** must be base64, at most 64 bytes before encoding, and the same length as the blob's other uncommitted IDs. Otherwise it fails with `InvalidBlockId` or `InvalidBlobOrBlock`.
- **Checksums:**
  - `Content-MD5` is checked against the body (`Md5Mismatch` on a difference).
  - `x-ms-content-crc64` is checked against the base64 of the **little-endian** CRC-64/NVME of the body. The byte order comes from azure-storage-blob's `calculate_crc64_bytes`, which uses `to_bytes(8, "little")`.
  - Sending both headers is a 400.
- **Response:** 201, with `x-ms-content-crc64` (or `Content-MD5` when the request had one) and `x-ms-request-server-encrypted: true`.
- **CRC64 mismatch:** Microsoft documents only "400 (Bad Request)", and the REST spec's error enum has no CRC64 member. sim_http answers 400 with the code `Crc64Mismatch` (`errors.CRC64_MISMATCH_CODE`). **That code string is UNVERIFIED: check it in the real-Azure contract lane.**
- **Limits:** a missing `Content-Length` gives 411 `MissingContentLengthHeader`. A block over 4,000 MiB gives 413 `RequestBodyTooLarge`. More than 100,000 uncommitted blocks gives 409 `BlockCountExceedsLimit`.

### Put Block List (`PUT ?comp=blocklist`), per `put-block-list`

- **Block list:** `<BlockList>` with `Committed`, `Uncommitted` and `Latest` entries, resolved as documented. A missing block gives `InvalidBlockList`; more than 50,000 gives `BlockListTooLong`; bad XML gives `InvalidXmlDocument`. The body's own `Content-MD5` or `x-ms-content-crc64` is checked.
- **Commit:** the blocks go to `SimBackend.put` in order, so sim records one CRC per block. Uncommitted blocks that aren't in the list are discarded.
- **Headers honoured:**
  - `x-ms-blob-content-md5`: stored, not validated, and returned as `Content-MD5`.
  - `x-ms-blob-content-type`.
  - `x-ms-meta-*`: names must be C# identifiers, otherwise 400 `InvalidMetadata`.
  - `x-ms-access-tier`: Hot, Cool, Cold, Archive or Smart; anything else gives `InvalidBlobTier`.
- **Tier rules:**
  - With no tier header, a new blob takes the account default, which is **Smart** on the two-account layout.
  - An overwrite with no tier header inherits the old blob's tier ("a blob inherits the tier from the old blob").
  - Overwriting an Archive blob fails with `BlobArchived`.
  - Archive on a ZRS account gives `BlobAccessTierNotSupportedForAccountType`. The code is from the spec enum; the 409 status is sim's choice.
- **Response:** 201, with `ETag`, `Last-Modified`, `x-ms-version-id` and the body checksum.

### Get Blob (`GET`), per `get-blob`

- `versionid` is supported.
- **Ranges:** `x-ms-range` takes precedence over `Range`. The forms are `bytes=a-b` and `bytes=a-`. The response is 206 with `Content-Range`. A range starting past the end gives 416 `InvalidRange`.
- **Range checksums:** `x-ms-range-get-content-crc64` or `-md5` works for ranges of 4 MiB or less. It is a 400 without a range, above 4 MiB, or with both headers.
- **MD5 headers:** `Content-MD5` on full reads, and `x-ms-blob-content-md5` on range reads.
- **Archived blobs:** an archived blob gives 409 `BlobArchived` ("A Get Blob operation on an archived block blob will fail"). A rehydrate target still pending gives 409 `BlobBeingRehydrated`.

### Get Blob Properties (`HEAD`)

- **Headers returned:**
  - `Content-Length`, `Content-Type` and `Content-MD5`;
  - `x-ms-access-tier`, plus `x-ms-smart-access-tier: Hot` for Smart blobs, since sim doesn't model Smart's inner movement;
  - `x-ms-archive-status: rehydrate-pending-to-hot`;
  - `x-ms-meta-*`;
  - `x-ms-version-id` and `x-ms-is-current-version`;
  - `x-ms-immutability-policy-until-date`, `x-ms-legal-hold` and `x-ms-tag-count`.
- **Errors** have no body; the code is in `x-ms-error-code`.

### Every response

Every response carries `x-ms-request-id`, `x-ms-version` (the request's header, else `sv`, else `2026-04-06`) and `Date`. Errors are Azure-style XML: `<Error><Code/><Message/>[<AuthenticationErrorDetail/>]</Error>`, plus `x-ms-error-code`.

**Mapping from sim:**

| sim condition | Azure error |
|---|---|
| `Throttled` | 503 `ServerBusy`, with `Retry-After` |
| an outage | 503 `ServerBusy` |
| `Immutable` | 409 `BlobImmutableDueToPolicy` |
| other faults | 500 `InternalError` |

### Authorization: user-delegation SAS only, per `create-user-delegation-sas`

- **Signature:** HMAC-SHA256 under the facade's user delegation key.
- **Supported versions:** all three documented string-to-sign layouts:
  - 2020-12-06 and later;
  - 2025-07-05 and later, which adds `skdutid` and `sduoid`;
  - 2026-04-06 and later, which adds the `srh` and `srq` canonical values.
- **Resource scope:**
  - `sr=b`: the blob.
  - `sr=c`: any blob in the container.
  - `sr=bv`: one blob version. The version id is signed in the signedSnapshotTime field, as the SDKs do. The SAS reads that version only and never writes.
- **Validity:**
  - The SAS is valid in `[st, se)`, and the key in `[skt, ske)`. Keys last at most seven days; a new one is issued when the old one lapses.
  - Permission letters must follow the documented order `racwdxltmeop`, with no repeats.
  - `spr=https` refuses plain HTTP with `AuthorizationProtocolMismatch`.
  - `sip` is enforced against the client IP with `AuthorizationSourceIPMismatch`.
- **Permissions per operation:**
  - Get Blob and Get Blob Properties need `r`.
  - Put Block and Put Block List need `w`.
  - `c` alone may create a new blob but never overwrite one, and only for `x-ms-version` 2026-04-06 and later, per both operations' SAS tables.
  - A missing permission gives 403 `AuthorizationPermissionMismatch`.
- **Refused:**
  - no SAS: 401 `NoAuthenticationInformation`;
  - an `Authorization` header (Bearer or SharedKey): 403 `AuthenticationFailed`;
  - service or account SAS (no `skoid`): 403 `AuthenticationFailed`;
  - `sv` before 2020-12-06, `sr=bs` or `sr=d`, `srh`/`srq`, and user-bound SAS (`sduoid`, which also needs a Bearer token): all 403 `AuthenticationFailed`.

## What it deliberately does not do

- **Operations outside the subset:**
  - Put Blob, Get Block List, Set Blob Tier, Set Blob Metadata, tags, leases, Copy Blob, List Blobs, container operations;
  - conditional headers (`If-Match`), and encryption scopes and customer-provided keys;
  - the structured body (`x-ms-structured-body`).
- **TLS:** the server is plain HTTP; put a TLS proxy in front if the edge insists.
- **Blob properties and naming:**
  - `x-ms-access-tier-inferred` and `x-ms-creation-time` are not returned.
  - Metadata names come back lower-cased, because ASGI lower-cases header names. Azure preserves case.
  - Unknown containers report `BlobNotFound`, not `ContainerNotFound`.
- **Staging and memory:** uncommitted blocks are never garbage-collected after a week. Blocks are held in memory until committed.

## Finding for the design

Azure metadata names must be C# identifiers. So the hyphenated POSIX names that `AGENTS.md` uses for S3 (`file-owner`, `file-group`, `file-permissions`) are refused by Put Block List on Azure with 400 `InvalidMetadata`. Real Azure behaves this way per the documented naming rule; sim_http enforces it. The Azure side needs its own names, for example `file_owner`, or the documented mapping needs updating.
