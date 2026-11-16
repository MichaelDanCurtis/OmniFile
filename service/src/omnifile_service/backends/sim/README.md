# sim: the simulated storage backend

`sim` implements `StorageBackend` (`../base.py`) in memory, following Azure Blob's or Amazon S3's rules on a virtual clock. It is layer 1 of the test strategy in `docs/BUILD-PLAN.md`. Most higher-layer tests run against it: recall, Teams, fail-closed, the permission matrix, and end to end. It is not a mock that returns canned answers. When it differs from the real provider, that is a bug in sim: the shared contract suite (`service/tests/backend_contract/`) runs against sim now and will run against the real backends later.

Sources: **[V]** `docs/research/2026-09-26-verification.md`, **[W]** `docs/design/tiers-versioning-worm.md`, **[A]** `AGENTS.md` (the Backends table and the Azure backend bullets). **[sim]** marks a choice sim makes where no source states the behaviour.

## Using it

```python
from datetime import timedelta
from omnifile_service.backends.sim import SimBackend, VirtualClock

clock = VirtualClock()  # any object with now() also works
sim = SimBackend.azure_two_account(clock)  # live ZRS (Smart default) + archive LRS
# SimBackend.azure_worm(clock)  # one LRS account, versioning + version-level WORM
# SimBackend.s3(clock)  # versioned buckets
ref = await sim.put("data", "a.segy", stream, None, {})
clock.advance(timedelta(hours=15))  # a Standard rehydrate is now done
```

On Azure, containers are named `account/container`. A bare name means the default account (`live` or `worm`), and returned refs always carry the full name. Containers are created on first write unless `auto_create_containers=False`.

Constructor options:
- `feed_lag` and `feed_page_size` for the change feed.
- `list_page_size` and `read_chunk_size`.
- `spill_threshold`: payloads larger than this go to a temporary directory; `close()` removes it.
- `timings` (a `SimTimings`).
- `restore_rate_limit`.
- `s3_credential_mode`.

Extensions beyond the protocol, for tests:
- `create_container(..., worm=, retention_days=, policy_locked=, archive_class=)`, `set_container_policy`, `delete_container_policy`.
- `put_synthetic(container, key, size)`: a sized object with no stored bytes, for example 10 GiB. Its bytes are a pattern generated on read.
- `it_archive(ref, deep=False)`: S3 Intelligent-Tiering moves the object into Archive Access or Deep Archive Access.
- `block_checksums(ref)` and `compute_checksum(ref)`.
- `validate_credential(cred, ref, access)`.
- `priority_queue_clear_at(container)`.
- `early_deletion_fees`: a list of `EarlyDeletionFee`.
- `faults`: a `FaultInjector`.

## What it models, and where each rule comes from

### Layouts and capabilities

`capabilities()` describes the binding's layout. `kind` is `BackendKind.SIM` for both flavours, so use `sim.flavour` to tell them apart.

| Layout | Smart tier | Archive | WORM | Source |
|---|---|---|---|---|
| `azure_two_account` | yes: the live account's default | a server-side copy into the archive account, then a delete of the original | no | [A][V] |
| `azure_worm` | not offered | Set Blob Tier in place | version-level | [W] |
| `s3` | Intelligent-Tiering | an in-place class change | no: Object Lock is noted, not built | [W] |

Tier tables, minimum durations, minimum billable sizes, per-object overheads, restore options, throughput caps, private-network TLS versions and credential modes are in `capabilities.py`, each traced to [V] or [A].

### Tiers

- **Smart is the account default.** `put(tier=None)` on a Smart account lands in Smart. A put with an explicit tier never enters Smart. [V]
- **Leaving Smart is one-way.** After any explicit `set_class`, `set_class(SMART)` raises `UnsupportedOperation`. [V][W]
- **Smart needs zonal redundancy, and Archive forbids it.**
  - `AccountConfig` refuses `smart_default` on LRS or GRS.
  - A put or `set_class` to Archive on a ZRS, GZRS or RA-GZRS account either moves the blob to the archive account or, if none is configured, raises `UnsupportedOperation`.
  - The archive account itself can't be zonal. [V][A]
- **The archive move:** a copy into `<archive account>/<same container>` as Archive, then a permanent delete of the moved version. If that version is under retention or a legal hold, it raises `Immutable`: WORM data can't be copied and then deleted. [A][W]
- **In-place Set Blob Tier,** including to Archive, is allowed on immutable versions. [W]
- **Moving out of Archive in place** is a rehydrate: the blob reads `IN_PROGRESS` and raises `NotRestored` for `azure_standard` (15 h), and an early-deletion fee is recorded. [A]
- **Minimum durations:** Azure Cool 30, Cold 90 and Archive 180 days; S3 Standard-IA 30, Glacier IR 90, Glacier 90 and Deep Archive 180 days.
  - Leaving the class before the minimum, by tier change, delete or unversioned overwrite, appends an `EarlyDeletionFee`. It never blocks the operation.
  - Smart has none. [A][V]
- **S3 classes:**
  - Glacier IR is readable without a restore.
  - `set_class(ARCHIVE, native_class="DEEP_ARCHIVE")` picks Deep Archive.
  - An archived object can't change class until it has been restored.
  - Intelligent-Tiering objects under 128 KB are never tiered. [V]

### Restore and rehydrate (virtual clock)

**Azure** [A][W]:
- A restore is a Copy Blob rehydrate into the recall container: `live/recall` or `worm/recall`, which has no WORM. The target key is `<source container>/<key>[@version]`.
- The target exists at once, in Archive with `IN_PROGRESS`, and is unreadable until done. The archived source is never touched, so no fee is charged.
- Priorities are Standard and High only.
  - **Standard:** 15 h.
  - **High:** one pipeline per storage account at **10 GiB/h**. Requests queue behind earlier ones, with a 1 h floor for small objects.
  - `priority_queue_clear_at()` lets the service see when High stops being faster.
- **No ETA above 10 GB** (10^10 bytes): `RestoreStatus.eta` is `None`. Internally, sim still finishes a large Standard restore after 15 h plus the excess at 10 GiB/h. **[sim]**

**S3** [V]:
- `RestoreObject` needs `days` for Glacier and Deep Archive.
- The temporary copy expires at completion + `Days`, rounded up to the next midnight UTC. The rounding is from AWS's restore documentation, not re-checked in [V].
- Expiry sets the state to `EXPIRED`, emits `RESTORE_EXPIRED`, and reads raise `NotRestored` again. Restoring a live copy again moves its expiry.
- **Intelligent-Tiering archive tiers:** no `days` allowed. On completion the object returns to Frequent Access, with no expiry and no expired event.
- **Expedited:** only under 250 MB (sim uses MiB, **[sim]**), and never from Deep Archive or Deep Archive Access.
- **Restore times** default to the upper end of AWS's ranges: Expedited 5 min; Standard 5 h or 12 h; Bulk 12 h or 48 h.
- **Throttle:** above 1,000 restore requests per second of virtual time, `Throttled` with `retry_after` to the next second.
- Repeating a restore that is in flight returns the same handle. Real S3 answers 409 instead. **[sim]**

### Copy

`copy_object` models Copy Blob, Put Blob from URL and CopyObject.
- It works across accounts and into the recall container.
- `tier=None` takes the destination's default class.
- An archived source raises `NotRestored`: use `restore`.

### Versioning and WORM (decisions 30–31) [W]

**Versioning:**
- Every overwrite creates a version.
- A delete without a version id is a tombstone. On Azure no version stays current; on S3 a delete marker is added. Deleting the marker undeletes the object.
- A delete with a version id removes that version permanently.

**WORM:**
- Retention is set per version with `set_retention`, plus an optional container default policy (`retention_days`), and a legal hold per version.
- A protected version can't be deleted (`Immutable`).
- With versioning on, overwrites and tombstones still succeed. With versioning off, overwriting a protected blob raises `Immutable` ("you must either have versioning enabled or have neither a legal hold nor a time-based retention policy"). New blobs can still be created.
- **Locked versus unlocked:**
  - A locked retention or policy can only be extended. Shortening it, unlocking it or deleting it raises `Immutable`.
  - Retention set with `locked=False` (the validation window) can be shortened.
  - `set_retention` defaults to locked.

### Tags and metadata

- `set_tags` / `get_tags`: at most 10 tags, keys 1–128 characters, values at most 256. Tags work on archived and immutable versions. Copies and archive moves carry tags over. **[sim]**: real Copy Blob copies tags only on request.
- `set_metadata`:
  - refuses an archived object (`UnsupportedOperation`) and a version under retention or legal hold (`Immutable`);
  - on a versioned container, makes a new current version with the same bytes, class and tags; otherwise it changes the metadata in place.

### Change feed

- `read_changes` is ordered and cursor-paged; a `None` cursor means "from now".
- **Record kinds:**
  - created, with `detail["overwrite"] = "true"` on an overwrite, as Azure reports BlobCreated;
  - deleted, with `detail["tombstone"]` for versioned deletes;
  - tier changed;
  - restore started, completed, expired and failed.
- **Timing:** a record becomes visible `feed_lag` after it happened. Delivery stops at the first invisible record, so order is never broken. Records made by scheduled events (completions, expiries) carry the time they fell due, not the time someone polled. [A]
- The feed is filtered by container. Real Azure has one feed per account. **[sim]**

### Integrity [V]

- CRC-64/NVME in pure Python (`crc64.py`): normal polynomial `0xad93d23594c93659`, reflected constant `0x9a6c9329ac4bc9b5`, init and xorout all ones, check value `0xAE8B14860A799888`. `crc64nvme_combine` builds a whole-object value from per-block CRCs.
- `put` records a CRC per block (one per chunk of the stream), a whole-object CRC and MD5. A wrong `crc64nvme=` argument raises `ChecksumMismatch`.
- `head().crc64nvme` is returned on S3 only: Azure checks CRC64 per request but stores no whole-blob value. `md5` is returned on both.
- `head().metadata` returns the user metadata `put` wrote (POSIX `file-owner`, `file-group`, `file-permissions` [A]); copies carry it over.
- `compute_checksum` models S3 Batch Compute checksum, which works on any class without a restore, and an in-Azure read of an online blob.

### Fault injection (`sim.faults`)

| Call | Effect |
|---|---|
| `fail_nth(op, n, error=None)` | The nth call of `op` from now raises `error`. |
| `throttle(op, times, retry_after)` | The next `times` calls raise `Throttled`. |
| `latency(op, delay, virtual=False, count=None)` | Delays `op`: `asyncio.sleep`, or a clock advance when `virtual=True`. |
| `outage(start, end, ops=None)` | Calls in the window raise `Unavailable`. |
| `corrupt_next_copy(CopyFault.CORRUPT \| PARTIAL)` | The next copy, archive move or rehydrate copy lands flipped or truncated bytes. The recorded checksums are copied from the source unchanged, so only a read-back finds the damage. This is the decision-24 verifier case. |
| `fail_next_restore(detail)` | The next restore is accepted, then ends `FAILED`. On Azure the target is removed, and a `RESTORE_FAILED` record is emitted. |
| `clear()` | Removes every fault. |

### Edge credentials [A][V]

**Azure:** a SAS-like URL on a `.invalid` host (`<account>.blob.core.sim.invalid`).
- It is bound to the host, container, key, permissions (`r` or `cw`), expiry and, for reads, the version.
- It is signed with HMAC-SHA256 under a random secret held by each backend.

**S3:** by default Roles Anywhere: no URL and no secret, only a pointer in `details`. Optionally a presigned-style URL.

`validate_credential` rejects:
- a wrong object, account or version;
- missing permission;
- expiry;
- tampering;
- another backend's secret.

## What it deliberately does not model

- **Smart internal movement:** objects stay "Smart" and are never shown moving between hot, cool and cold. Smart monitoring fees are not modelled either.
- **Costs** other than the early-deletion record.
- **SAS cryptography:** tokens prove scope and expiry to tests, nothing more.
- **Lifecycle rules, Blob Inventory and S3 Inventory/Metadata, Storage Mover jobs, S3 Batch Operations jobs, and EventBridge/SQS delivery:** the change feed stands in for all event delivery.
- **S3 Object Lock:** sim's S3 flavour has no WORM.
- **Copy semantics:**
  - S3 `set_class` changes the class in place, like a lifecycle transition. A real CopyObject class change would create a new version.
  - On Azure, sim allows deleting the current version by its id, which leaves no current version. Real Azure refuses version-id deletes of the current version.
- **Network:** private endpoints, TLS, ExpressRoute throughput and egress are all absent.
- **Throughput:** the 1–2 PB/day S3 restore throughput and the Azure Smart-plus-immutability combination are not enforced. The latter is not stated by Microsoft and is to be verified in pilot 4.
- **Per-request block CRC rejection** on Azure Put Block: sim checks only the whole-object `crc64nvme`.
- **Persistence:** everything is lost when the process exits, apart from spilled temporary files until `close()`.
