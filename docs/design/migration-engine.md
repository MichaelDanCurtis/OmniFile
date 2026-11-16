# Migration engine: copy, verify, cut over (M5)

`migration-namespace.md` decided *what* happens while a dataset moves from S3 to Azure: reads go wherever the bytes are, new writes go to Azure, and a restore can serve both a user and the move (decisions 14–16, 19). Decisions 20 and 24 decided *who* copies (Azure Storage Mover, never our own copier) and *how a copy is trusted* (a full CRC64-NVME read in Azure, per file, before cutover). This note turns those into a pipeline the worker runs, and the `POST /v1/locations` contract.

Catalog seeding (`catalog-seeding.md`) comes first. The engine works on the files a source seeded, and on the events it follows.

## 1. Who does what

| Step | Owner | Why |
|---|---|---|
| Plan the move (which prefixes, when) | Prizmanyx | It owns the restore plan and the estate's schedule. |
| Copy bytes | Azure Storage Mover (decision 20) | A job is created by OmniFile, or by Prizmanyx (`copy: external`) |
| Restore archived objects first | OmniFile (decision 15) | S3 Batch Operations restore waves; user recalls as today |
| Verify every file | **OmniFile, always** (decision 24) | A prefix never flips on a copier's word, ours or anyone's |
| Stamp tier, POSIX metadata, tags | OmniFile | Storage Mover sets none of them [V] |
| Flip a prefix to Azure | OmniFile, when verified | `POST /v1/locations` asks for it; the engine decides when |

## 2. Units: migration prefixes

`catalog.migration_prefix` (0002) already exists. Migration 0016 ties each row to a source and gives it what the engine needs:

| Column | Meaning |
|---|---|
| `source_id`, `prefix` | A key prefix inside the source's prefix (`''` = all of it). Prefixes of one source may not overlap. |
| `to_backend_id`, `target_root` | The dataset's own backend and the blob-name prefix the copies land under: `migrated/<bucket>/` + the S3 key, so every copy is traceable to its object. |
| `copy_by` | `omnifile` (the engine runs Storage Mover) or `external` (Prizmanyx does; the engine only watches the target and verifies). |
| `mode` | `serve` only in v1 (decision 35 reserves `hand_off`). |
| `state` | `pending → copying → verifying → verified → cut_over`, or `failed` (0002's values). |
| `storage_mover_job`, `last_run_id`, `last_copy_started_at`, run counters | The current job definition and run. |
| `files_total`, `files_verified`, `files_failed` | Over the prefix's live files. |
| `journal_checked_through` | Every S3 event for the prefix up to this time has been applied (`changefeed_cursor`). |

## 3. Per file: locations, not a new table

Each file under a prefix already has its S3 primary location (seeding). The engine adds, per file version, a **target location** on the Azure backend:

1. `role = 'secondary'`, `state = 'pending'`, `object_key = target_root + key`, created when the copy is seen in the target (the Blob change feed's `BlobCreated`, or a listing of `target_root` after a run). `size` from the blob.
2. **Verify** (§5): the blob is read inside Azure and its CRC64-NVME compared with the S3 object's. A match makes it `committed`, `verified = true`, `verified_by = 'azure_read_crc'`, `crc64nvme` set, and stamps it (§6). A mismatch leaves it `pending`, records drift `size_mismatch` or a new `checksum_mismatch`, re-queues the file for the next run, and alerts.
3. **Flip** (§7): in one transaction per batch, the S3 location becomes `secondary` and the Azure one `primary` (`location_one_primary` allows one primary per version). `file_resync_location` repoints the file; its feed event tells the edge the bytes now come from Azure.

A version written after the copy started (an S3 overwrite the follower applied, `catalog-seeding.md` §8) has no target location, so its file is simply not verified yet. The next Additive run copies it.

## 4. Copying: Storage Mover jobs

A job definition takes one `sourceSubpath` and one `targetSubpath`, copy mode Additive or Mirror, and no object list [V]. So:

- **Bulk.** One job definition per prefix: `sourceSubpath = prefix`, `targetSubpath = target_root + prefix`, **Additive** (never Mirror: it deletes at the target). The engine starts a run, polls it (`status`, counters: scanned, transferred, failed, unsupported, noTransferNeeded), and records it. Unrestored archived objects come back failed or unsupported [V]; they are not an error of the run.
- **After a restore wave** (§8) the prefix's job is run again (Additive copies only what is new), so restored objects follow.
- **Urgent recalls** (decision 24): a user's recall (or the partner API's restore) of an object under the prefix can't wait for the next rerun. The engine copies the restored object inside S3 (CopyObject, as Standard: S3's copy, not ours) into the S3 backend's **staging bucket** (`staging_bucket`, never a bucket any dataset reads, so the staged object is never followed into a catalog; if it is one, nothing is staged and the prefix's `last_error` says so) at `omnifile-staging/<migration_id>/` + the key's rest, and runs a small Storage Mover job over that subpath whose `targetSubpath` is the prefix's real target path. Its copies are found by name and verified against the original object's checksum (CopyObject keeps it). The staging object is deleted once the file has a verified copy. A failed staging run is tried again up to three times; after that the file waits for the prefix's next run. A step's staging copies stop after 6 hours: one cut off is given up the same way, and one not reached goes in the next step. Wave restores are not staged: the rerun after the wave picks them up. (`migrate/staging.py`, migration 0018.)
- **Limits:** 10 concurrent jobs per subscription by default, 500 million objects per job [V]. The engine runs at most `max_concurrent_jobs` (default 8, leaving room) and queues the rest.
- **The `Mover` port.** `ensure_job` (a job definition: source endpoint and subpath, target endpoint and subpath, Additive), `start_run`, `run_status`. Implementations: Azure Storage Mover through ARM's REST API (public per decision 22; the S3 side through the Arc multicloud connector endpoint the customer set up), and a **sim mover** for tests and e2e that copies between sim backends with the same Additive semantics and counters.
- **Private-only target accounts** are unproven for Storage Mover [V] (pilot). The engine doesn't depend on it: it only needs the blob to arrive.

## 5. Verifying

Per file, before its prefix can flip (decision 24):

- **Expected:** the S3 object's full-object CRC64NVME. First from the object itself, by HEAD with checksum mode: S3 stores a full-object CRC64NVME for uploads that carried one and adds one to uploads that carried none [V], so objects written before S3 did that have none, and whether HEAD returns it for an archived object is to be checked (`real-aws-checks.md`). Otherwise from an **S3 Batch Operations Compute checksum** job over the prefix, which reads nothing out of S3 and works on every class without a restore [V] (up to a week for Flexible Retrieval or Deep Archive; SSE-C objects are not supported and stay unverified, named in the prefix's report). The value is kept on the S3 location (`crc64nvme`, `verified_by = 's3_batch_checksum'`).
- **Actual:** the blob read inside Azure, from the service (private endpoint; no egress; still Hot, so no retrieval fee), CRC64-NVME over every byte (`worker/verify.read_crc`, the M1 upload verifier's routine). Not sampled: a silent bad copy of seismic data is the failure that matters.
- **Throughput** is the read rate of the worker replicas; the queue (`migrate_verify`) spreads files across them. Sizes are compared first, so a short copy fails without a read.
- **Journal.** A file is verified for the version its target location names. If the follower (`catalog-seeding.md` §8) applied a newer S3 version, that version has no verified target, so the file isn't done; the next run copies it.
- **Unversioned keys.** A HEAD with no version id reads whatever the key holds now, so its checksum is used only while the key still holds the catalog's object (ETag and size). Otherwise the copy is dropped from the catalog (`stale`) and found again, for the new version, once the follower applies the overwrite. A bad copy is not deleted when another committed location names the same unversioned target key.
- **Long reads.** The read runs before any row is locked; the rows are then locked and checked again before anything is written. Its bound is an hour, or the file at 25 MiB/s if that is longer, and the message stays hidden for longer than that. The `migrate_verify` queue holds a message for 2 hours and `migrate` for 15 minutes when it is read, and each reads one at a time. A provider error rolls the step's writes back before the retry and goes on the prefix's `last_error`. A copy whose check keeps failing is tried 12 times, a minute apart at first and an hour at most, about 6 hours to the last try. Before that try the storage is asked for the object, as `worker/redrive.py` does for recalls: no answer is an outage, and the try waits, asking each hour, for up to 3 days, spending no read. An answer means the provider is up, so a read too slow for its bound ends at 12. If the last try fails, the message dead-letters, which fires the `dead-letters` alert. Its copy stays pending, so the prefix doesn't cut over around it, and no run revives it (`_record` skips a pending copy). The worker's progress sweep counts the letters, matching each by its copy, whichever prefix sent it, and the console and the migrations API show the number beside the prefix's other errors until someone sends them again. `web` reads no queue: a letter holds provider text and object keys. Once a prefix stops making progress, the sweep also counts copies with no check queued and no letter, if they hold up the cutover: **stranded** if the S3 object's checksum is known (a dropped letter leaves this), and **no checksum** if it isn't and nothing is computing one: a job came and went with no value (SSE-C), or the backend has no Batch Operations. A copy waiting on a checksum job, or for the next step to ask for one, is neither.
- **Provider calls.** A transaction id held during a call stops the edge feed for every edge, so no step or verification holds one across a call (`migrate/calls.py`). The call is made with no transaction open, and the step runs again from the top with its result. Each pass but the last rolls back. The last pass makes no call: it locks the prefix, reads everything afresh and does all the step's writes in one transaction. So a cutover's checks always run after the step's calls, on what is true then. The exception is a call a retry would make again or pay for again: a Batch job, a staging copy, a staging run. The pass after it records its answer, does nothing else and commits, whatever state the prefix is in by then. So a later call's error can't roll the record back, and a cancel or a closing window can't drop it. Every call has a bound, and a call that passes it is a provider error. What a row lock used to give comes from elsewhere:
  - **One step per prefix:** the worker holds the prefix's lease, a session advisory lock, until the message ends. A second holder leaves its message and looks again in 5 minutes. A crash frees the lease with the connection.
  - **A call twice after a crash:** a Storage Mover job's running run is looked for before one is started, and followed. A Batch job's request token names its objects, below. A staging copy overwrites its own key.
  - **The listing of a target** goes a page a message, with the cursor in the next message. Each page commits, and `_record` skips what is on record.
  - **A bad copy's delete** holds the blob's advisory lock, as `retention.delete_blob` does, and no row. A copy of the same key can't be marked verified, or gone, until it ends.
- **Copies that can't be checked** (no checksum S3 can give) hold nothing up: the prefix's waves and runs go on, a wave skips objects whose copy is waiting, and the prefix never cuts over around them.
- **Batch jobs** (checksums and waves) are created before anything is written, named `omnifile-{checksum,wave}-<migration>-<n>-<digest of the objects>`, so a retry over the same objects sends the same request token, and finds the same job with no second restore. A retry after a crash, once the catalog has moved, names other objects, so it is a new job: S3 refuses a token used with other parameters, which would otherwise stop the prefix for good. An object in both is restored once (S3 refuses a restore already in progress, and extends one that finished; `real-aws-checks.md` RW-18). That takes a worker or database lost in the moment between CreateJob's answer and the commit that follows it, and the catalog moving before the retry. What the job was asked for is recorded in that commit, before any other call, whatever the catalog or the prefix's state says by then. A checksum job still running under a cancelled prefix has nobody to read its report, so a later migration asks about its objects again. A manifest names a version for every object or for none, so each job takes objects of one kind.

## 6. Stamping

On a match, the same pass sets on the blob:

- **Tier:** only when the dataset's policy is not Smart (Smart is the live account's default tier, and an explicit tier takes a blob out of Smart for good, decision 29's rule). Otherwise the tiering worker places it on its own schedule, from the file's dates, as for any upload.
- **POSIX metadata:** `file_owner`, `file_group`, `file_permissions` (Azure's spelling, `CLAUDE.md` backends table) from the catalog row.
- **Index tags:** the hold and state tags the M3 tiering worker uses.
- **Catalog:** `location.crc64nvme`, and `file.crc64nvme` if it was empty.

## 7. Cutting over

A prefix moves `verifying → verified` when:

- every live file under it has a verified target location for its current content version;
- the change feed for its bucket is caught up past the latest run's start (`journal_checked_through ≥ last_copy_started_at`), with no S3 change applied since then to a file under it.

**Retained and held files** (a legal hold, or a WORM dataset, decision 31) flip like any other. The catalog's location guard (0003) freezes the role of every copy of such a version; migration 0027 allows exactly one change to it: a swap of `primary` and `secondary` between committed copies, with nothing that names the bytes changed, towards a verified copy, and a committed primary at commit (a deferred check). Deleting a held copy, marking it `deleting` or `deleted`, relabelling it to any other role, promoting an unverified copy and shortening retention stay refused. Before each batch, the prefix waits (`last_error`: "N copies still wait for their retention or legal hold") until every verified copy of a retained or held version carries that retention and hold (`worm_sync_needed` cleared by the WORM stamp sweep, `retention/worm_sync.py`). The S3 object stays a retained secondary that OmniFile never writes or deletes.

`verified → cut_over` flips the files in batches (§3, 10,000 per transaction). After the last batch the prefix is counted again: a file written or overwritten in S3 since it was verified sends it back to `verifying`. At cut over, staging objects left over are deleted and a staging run still open is closed. The prefix stays `verified` until each has been deleted or tried 5 times, the least tried first, with 5 minutes between when only failures are left. Those are the only tries once the prefix is verified; a failure before that doesn't count against them. An object still there after that costs money and risks no data, so the prefix cuts over: the row stays `failed`, `last_error` names the bucket, the console shows the prefix as needing attention, and the `staging-left` alert fires for a week. Then:

- **reads** of those files go to Azure (the edge follows the feed);
- **writes** were already going to Azure (decision 14);
- **deletes** tombstoned through an edge while the bytes were at the source are listed in the cutover report (`catalog.migration_source_deletes`, `GET /v1/migrations/{id}/tombstones`, decision 40) for Prizmanyx's decommission; the engine deletes nothing in S3.
- **the source stays read-only** to OmniFile after the cutover as before it: no prune, tier move, pin, hold or tag ever writes to it (`catalog-seeding.md` section 10). Its objects remain the catalog's `secondary` locations.
- When every prefix of every source of a dataset is `cut_over`, the dataset becomes `cut_over` (0002's state) and `source_backend_id` stays for history.

## 8. Restore waves

Archived objects (Glacier Flexible Retrieval, Deep Archive, Intelligent-Tiering's archive tiers) must be restored before Storage Mover can copy them [V].

- A wave is an **S3 Batch Operations restore** over a manifest of a prefix's archived keys (Bulk by default; `Days` long enough for copy and verify, default 14), recorded as `catalog.restore` rows with `mechanism = 's3_batch_restore'`, `triggered_by = 'migration_wave'`, `migrate_on_complete = true`.
- Completion comes per object from EventBridge (`Object Restore Completed`, already applied by `changefeed_sync`). When the wave's restores are done (or the wave's deadline passes), the prefix's job runs again.
- The **1,000 restores/s per account** are shared by waves and user recalls [V]. A wave is one S3 Batch Operations job, and S3 paces its tasks itself, so the service has no per-request rate to control for it. Single restores (user recalls) go through each worker's token bucket (`restore_rate_per_s`, at most the account's 1,000/s), and a `SlowDown` while a wave runs is retried with backoff. A shared bucket across workers would be one hot Postgres row on every restore for little gain, so there isn't one. Set `restore_rate_per_s` to the account's budget divided by the worker replicas. Whether a running Batch restore job starves single `RestoreObject` calls is on `real-aws-checks.md`.
- **Abandoned waves:** a wave still running past its window under a cancelled or cut-over prefix has no step to close it. A sweep each hour does, and fails its restores still open, so a task that never ran doesn't block its object in a later wave or hold a recall that joined it.
- **Expiry:** a restored copy that is about to expire (24 h) without a verified target is alerted, and the object goes into the next wave.

## 9. `POST /v1/locations` (portal-api.md §11, settled here)

`{dataset_id, prefix, to_backend_id, mode: "serve", copy: "omnifile" | "external"}` → `202` the migration prefix.

- `prefix` is a key prefix inside one of the dataset's sources (the response names the source). `to_backend_id` must be the dataset's own backend (v1 is S3 → Azure, decision 23). `mode: hand_off` is `400 invalid_request` until decision 35 is built.
- It starts the pipeline: copy (unless `external`), verify, cut over. It never flips anything unverified: there is no "trust me".
- `GET /v1/locations?dataset_id` and `GET /v1/locations/{id}` report state, counters, the job, and files that failed verification.
- The feed (`GET /v1/events`) carries `migration.started`, `migration.verified`, `migration.cut_over` (with `source_deletes_pending`), `migration.failed` and `migration.cancelled`, written by a trigger on `migration_prefix` (migration 0029).

### 9.1 The partner lifecycle (portal-api.md §5.17, ported from line B)

`/v1/migrations` is the same rows with more choices and actions:

- `cutover: manual` holds a prefix at `verified` (counted again every 10 minutes, back to `verifying` if a file changed) until `cutover_requested_at` is set; then the flip runs as above.
- `cancel` sets `cancelled`, and the step stops. Nothing is deleted; a Storage Mover run already started finishes, and its copies are found again if the prefix is started again (a cancelled prefix doesn't block a new one).
- `retry` puts a `failed` prefix back to `pending`.
- An external copier's runs (`migration_run`) are display only, but a run's start moves `last_copy_started_at`, so the journal gate covers it, and a running run sends a `verified` prefix back to `verifying`.
- `migration_exclusion`: keys an ArchiveAdmin excluded are left out of the count and never flipped; they stay served from S3, so the dataset doesn't reach `cut_over` while they live.

## 10. Building it

1. Migration 0016, the `Mover` port with the sim mover, the per-file verifier and stamping, cutover, `POST/GET /v1/locations`: end to end on sim. **Built** (`migrate/`, `worker/migrate_jobs.py`, `web/routes_locations.py`).
2. Azure Storage Mover client behind the port, over ARM's REST API with the worker's managed identity (no SDK), with a fake ARM in tests; real-Azure checks in `real-azure-checks.md`. **Built** (`migrate/storage_mover.py`; the worker's `OMNIFILE_STORAGE_MOVER`; the Bicep's `storageMoverConfig` and "OmniFile Mover Operator" role).
3. S3 Batch Operations restore waves, Compute checksum jobs and staging copies, with the S3 fake; real-AWS checks in `real-aws-checks.md`. **Restore waves built** (`migrate/waves.py`, `S3Backend.batch_restore` / `batch_job`, the backend's `batch` config, migration 0017): a prefix with archived, uncopied objects gets one Bulk wave at a time, with a restore row per object; a restore under the prefix completing (a wave's or a user's recall) copies again within the hour instead of waiting for the 6-hour rerun; restores still open 72 h after the wave are failed, and copies about to expire unverified are reported. **Compute checksum jobs built** (`migrate/checksums.py`, `S3Backend.batch_checksum` / `batch_checksums`, `catalog.checksum_job`): copies waiting on an S3 object with no CRC64NVME get one `S3ComputeObjectChecksum` job (CRC64NVME, full object, an all-tasks report) per prefix at a time. When it is Complete, the values in its report go on the S3 locations and the waiting copies are queued for verification again. An object the job can't checksum (SSE-C) isn't asked about again; the prefix's `last_error` counts them and their copies stay unverified, so the prefix never cuts over around them. A failed job is tried again, at most three times. **Staging copies built** (§4, `migrate/staging.py`): part 3 is done.

[V]: `docs/research/2026-09-26-verification.md`, Azure Storage Mover and S3 sections.
