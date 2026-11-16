# Native Azure and AWS features to adopt (2026-09-26)

Owner direction: use native platform features wherever they make the product easier (see `AGENTS.md` → Research rule). Two research passes collected these, each cited to primary sources. **(verified)** marks claims read at the source during the session. **UNVERIFIED** marks claims with no primary source.

## Azure

| Feature | Replaces / simplifies | Private endpoints? | Verdict |
|---|---|---|---|
| **Blob change feed** | Catalog sync, recall-status polling, detecting changes made outside OmniFile | Yes: plain blobs read over the blob private endpoint | **ADOPT** |
| **Blob Inventory** (Parquet) | Nightly catalog reconciliation crawler | Yes | **ADOPT** |
| **Blob index tags** | Hold / dataset / archive-date lookups; filters for lifecycle and Storage Actions | Yes | **ADOPT** |
| **Version-level WORM + legal hold, versioning, soft delete** | Our "legal hold / never archive" policy; protection against deletes from the edge | Yes | **ADOPT** (see gotchas) |
| **Copy Blob / Put Block From URL** | Smart → archive-account moves, rehydrate-by-copy, dedupe copies | Yes | **ADOPT** |
| **CRC64-NVME transactional checksums** (`x-ms-content-crc64`, structured body) | A separate hash pass | n/a | **ADOPT** |
| **Lifecycle management** | A backstop for our tiering scheduler | Yes | **ADOPT as backstop** |
| **Arc-enabled servers**: managed identity, Azure Monitor Agent | Secrets on the appliance; fleet logs | Yes via Arc Private Link Scope (Entra/ARM still need outbound internet) | **ADOPT**; Update Manager and Machine Configuration: CONSIDER |
| **Data Box** | Seeding the historical estate | Needs the trusted-services exception | **ADOPT** for the seed |
| Container Apps (internal env), PostgreSQL Flexible (private access), Key Vault, DNS Private Resolver, App Gateway WAF | Hosting the control plane and the Teams relay | Yes | **ADOPT** |
| Teams SDK for Python (GA, MIT) + Graph | Notifier | The bot endpoint must be public (allowed) | **ADOPT** |
| Azure Policy built-ins | Enforcing private-only, no shared key, versioning | Yes | **ADOPT** |
| Storage Actions | Admin bulk jobs (legal hold on a dataset, bulk tag) | Needs the trusted-services exception | CONSIDER. It has **no copy operation**, so it cannot do Smart → archive-account moves |
| Event Grid → namespace pull | Lower-latency events | Partly | CONSIDER; the change feed is enough |
| Storage Mover | Bulk S3 → Blob | Cloud-to-cloud with private-only targets: UNVERIFIED | CONSIDER for bulk only; no Glacier rehydrate |
| Defender for Storage malware scan | Scanning SMB uploads | Yes | CONSIDER; files over 50 GB are skipped |
| Data Box Gateway | — | — | REJECT as a component; copy its ideas (evict from 50–60% cache use, per-share refresh, upload throttling, local diagnostics UI) |

Key facts:
- **Change feed** (verified, https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-change-feed):
  - An ordered, durable Avro log in `$blobchangefeed`, readable with ordinary blob calls. Records appear "within an order of a few minutes".
  - Schema v4+ records `BlobTierChanged` and `BlobAsyncOperationInitiated`, so rehydration start and completion are visible.
  - Ordering is per blob only, and segment times are approximate (±15 min), so also read the neighbouring hourly segments.
  - Not supported on hierarchical-namespace accounts. Reads are not logged.
  - The Python SDK `azure-storage-blob-changefeed` is still preview. The file format is documented, so a direct Avro reader is a safe fallback.
- **Index tags can be set on Archive-tier blobs; metadata cannot.** Store the archive date and policy IDs in tags. https://learn.microsoft.com/en-us/azure/storage/blobs/storage-manage-find-blobs
- **Azure's CRC64 is CRC64-NVME,** the same algorithm as S3's default CRC64NVME. https://learn.microsoft.com/en-us/rest/api/storageservices/structured-body-format
- **Cross-account Copy Blob:** the docs contradict themselves on Entra auth for the source, so plan on minting user-delegation SAS tokens. https://learn.microsoft.com/en-us/rest/api/storageservices/copy-blob

Gotchas:
- **Smart tier status is inconsistent in Microsoft's docs.** The smart-tier overview says GA in public-cloud regions (verified). The REST docs for Put Block List and Copy Blob (version 2026-02-06) call the `Smart` header "public preview". The product's Smart + archive-date mode depends on it; confirm with Microsoft. *Resolved 2026-09-26:* GA since 2026-04-14; the REST pages predate GA (`2026-09-26-verification.md`).
- **Version-level WORM blocks Blob Inventory** on the same account, and WORM disables last-access tracking. Keep held data in its own account or container, or reconcile holds from the change feed.
- **Trusted-service exceptions** (Data Box, Storage Actions, Event Grid → queue) apply in "selected networks" mode. With public network access fully *Disabled* they probably don't (inferred).
- **Arc still needs outbound internet** to Entra and ARM. Arc supports Ubuntu 26.04 on x86 only, which meets our kernel ≥ 6.14 requirement. *Resolved 2026-09-26:* RHEL 10 ships 6.12 with no pre-content backport, so Ubuntu 26.04 is the reference OS (`2026-09-26-verification.md`).

## AWS (S3 as migration source or destination only; decision 18)

| Feature | Replaces / simplifies | Private? | Verdict |
|---|---|---|---|
| **S3 interface VPC endpoints** (PrivateLink) | Public S3 for the edge's ranged GETs and restores | Yes, reachable on-prem over Direct Connect/VPN (gateway endpoints are not) | **ADOPT** |
| **IAM Roles Anywhere** + `aws_signing_helper serve` (Apache-2.0, TPM-held keys) | Static keys and a presigning service | Yes (own interface endpoint) | **ADOPT** |
| **S3 Batch Operations: Restore** (Standard/Bulk, up to 4B objects/job) | Our per-object restore loop | Yes | **ADOPT** |
| **S3 Batch Operations: Compute checksum** (MD5, CRC64NVME, SHA-256 … full-object, **any storage class, no restore**) | Our verification read pass | Yes | **ADOPT** |
| **EventBridge restore events → SQS standard queue, pulled by our control plane** | Polling HEAD per object; inbound webhooks | Yes (SQS interface endpoint) | **ADOPT** |
| **S3 Metadata live inventory + journal** (Iceberg REST) | LIST crawling and change detection | Yes (`s3tables` endpoint) | **ADOPT** |
| S3 Inventory | Intelligent-Tiering access tier (not in Metadata tables); fallback seed | Yes | CONSIDER |
| DataSync Enhanced (S3 ↔ Azure Blob, agentless) | Copy engine for restore waves; Azure → S3 | Agentless path uses public Azure endpoints; needs account-key SAS | CONSIDER |
| Mountpoint for S3 (Apache-2.0, Rust) | Reference design for the S3 read path and prefetch | — | CONSIDER as reference |
| Snowball | — | — | **REJECT**: no new customers since 2025-11-07, support ends 2026-12-31 |
| Object Lambda, S3 Files, Access Grants | — | — | REJECT |

Key facts:
- **Compute checksum** works "without downloading or restoring objects" on all storage classes. Glacier Flexible and Deep Archive jobs can take up to a week; SSE-C is not supported. *(verified)* https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-compute-checksums.html
  - **End-to-end copy check:** take the S3 full-object CRC64NVME (stored, or computed at rest), compute CRC64 while streaming, send `x-ms-content-crc64` on each Azure block, combine the block CRCs, and compare. Stamp the S3 MD5 as the blob's Content-MD5. The copy itself is the only read.
- **Restore signals:** `s3:ObjectRestore:Completed` and `:Delete`, and EventBridge "Object Restore Completed/Expired". Delivery is at least once and may be out of order, so key on `sequencer`. Batch "job complete" means the restores were *requested*, not finished. https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventBridge.html
- **Interface endpoints:** 10 → 100 Gbps per AZ, $0.01/GB processed. They **reject TLS 1.3 and post-quantum TLS** (rustls must allow TLS 1.2). MTU is 8500 with no Path MTU Discovery. https://docs.aws.amazon.com/AmazonS3/latest/userguide/privatelink-interface-endpoints.html
- **Roles Anywhere:** sessions of 900–43,200 s; CreateSession limited to 10 TPS; revocation by imported CRL only. Since May 2026 the endpoint policy must allow `CreateSession`.
- **Network path from the Azure control plane to AWS** (for SQS and the S3 control API): VPN, ExpressRoute + Direct Connect, or AWS Interconnect – multicloud with Azure (preview since 2026-08, four Regions).

What to copy from **AWS S3 File Gateway** (still offered; FSx File Gateway closed to new customers since 2024-10-28):
- Store POSIX attributes as `x-amz-meta-file-owner`, `-group`, `-permissions` (plus times), so files interoperate with File Gateway, S3 Files and FSx for Lustre.
- It answers an archived read with a bare I/O error plus an `InaccessibleStorageClass` log; AWS's blog adds a Lambda that auto-restores and makes the user retry. **We beat that** with NFS wait, a note file and a Teams card.
- Refresh the cache per directory on a TTL, plus explicit recursive refresh through the API. Don't refresh on every event.
- Keep a report of files that fail to upload, and expose cache metrics.

AWS gotchas:
- The 1,000 restore TPS limit is **per account and shared** by user recalls and Batch waves; budget it with a token bucket.
- A lifecycle expiry can remove an object before its restore window ends.
- Intelligent-Tiering restores take no `Days` and raise no Expired event.
- Old objects may lack a stored CRC64NVME; run Compute checksum first.
- SCPs or data-perimeter policies can silently deny `s3:RestoreObject`; preflight with a real restore.
- Requester-pays buckets need the `x-amz-request-payer` header.

## Effect on the design

1. **Azure events without Event Grid:** read the **change feed** over the private endpoint as the catalog and recall stream. Keep Get Blob Properties for the single file a user is waiting on.
2. **S3 restores:** run **Batch Restore waves**, feed **EventBridge → SQS** for completion, and have the control plane pull from SQS privately. A completion event triggers migration of the restored objects (decision 15).
3. **Integrity:** use **CRC64-NVME end to end** across both clouds. Compute checksum (at rest, even in Glacier) replaces verification reads.
4. **Credentials:** the edge uses the **Arc managed identity** (Azure) and **IAM Roles Anywhere** (AWS). No long-lived secrets anywhere.
5. **Catalog:** seed from **S3 Metadata tables** or **Blob Inventory**; keep it current from the **journal** or **change feed**; reconcile nightly from inventory.
6. **Holds:** use **index tags + version-level legal hold**, set at upload.
7. **Who copies restored Glacier objects?** Storage Mover (UNVERIFIED for restored objects), DataSync Enhanced (skips unrestored objects, reads restored ones, needs account-key SAS and a public Azure path), or OmniFile's own copier. Decide after the pilot. DataSync's needs clash with a private-only, Entra-only Azure account.
