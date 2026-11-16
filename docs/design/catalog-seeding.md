# Catalog seeding: S3 data into the catalog (M5)

`migration-namespace.md` step 1, and the `POST /v1/sources` draft in `portal-api.md` section 11. A registered **source** is a bucket prefix that already holds data. Seeding puts every object in it into a dataset's catalog, so the edge can present it (R1 placeholders, `fsal-namespace.md`) without ever listing S3 on a directory read.

This note covers registering a source, reading a native inventory (S3 Metadata tables or S3 Inventory) or a listing, writing catalog rows (sections 1–7), keeping them current from the bucket's events or its Metadata journal once seeded (section 8), reconciling a bucket per source prefix (section 9), and the rule that a source is read-only to OmniFile (section 10).

## 1. A source

`catalog.source` (migration 0013) binds `(backend_id, container, prefix)` to a folder of one dataset:

| Column | Meaning |
|---|---|
| `dataset_id`, `path` | Where the objects appear: `path` is a folder relative to the dataset root (`''` = the root). Missing folders are created. |
| `backend_id`, `container`, `prefix` | The S3 binding, bucket and key prefix (`''` or ending in `/`). The bucket must be in the binding's `buckets`. |
| `inventory` | Where seeding reads the objects (section 7): `{"kind": "s3_metadata", "table_bucket_arn", "namespace"?, "table"?, "journal_table"?}`, `{"kind": "s3_inventory", "bucket", "manifest_key"}`, `{"kind": "listing"}`, or NULL (migration 0028). |
| `journal` | An `s3_metadata` source's position in its journal table once seeded (section 8). |
| `owners_sid`, `writers_sid`, `readers_sid` | AD groups; they become the ACL of every seeded folder and file (section 4). Owners is required. |
| `state` | `registered → seeding → seeded`, or `failed` (with `last_error`). |
| `snapshot_at` | The inventory's creation time, or when the listing started. |
| `resume`, counters | Where the next batch starts; objects seen, files and folders created, already present, skipped. |

A source has a `kind` (migration 0015): `source`, data to migrate from where it is, or `adopt`, data Prizmanyx already moved into the dataset's own backend (`POST /v1/adopt`). An adopted source is seeded the same way, with `origin = 'adopt'` on its rows, but starts no migration.

Prefixes of one bucket may not overlap (`a/` and `a/b/`), so an object belongs to at most one source. Registrations on one bucket take turns (a transaction advisory lock on the backend and bucket), so two at once, even for different datasets, can't both pass the overlap check.

Registering a source whose backend is not the dataset's home backend sets the dataset `migrating` with that backend as `source_backend_id` (decision 23: the home stays Azure and new writes go there). A dataset migrates from one source backend at a time, which is what the existing `dataset.source_backend_id` column can hold.

## 2. Key to path

`key = prefix + rel`; `rel` is split on `/` and placed under `path`.

- A key ending in `/` with size 0 is a folder marker: the folder is created, no file.
- A component that is empty (`a//b`), `.`, `..`, longer than 255 bytes, or contains NUL can't be a POSIX name. The object is skipped and recorded as drift `unmappable` (source `seed`).
- A file where a folder is needed, or a folder where a file is needed (S3 allows both `a/b` and `a/b/c`), is skipped and recorded as drift `path_conflict`. Within one batch the folder wins; across batches, whichever was seeded first.
- Only current versions are seeded (`IsLatest`, not a delete marker). Rewind (decision 30) starts from adoption; older S3 versions stay in S3.

Drift rows (`catalog.reconcile_drift`, where reconcile's findings already go) surface these instead of dropping them silently; the source's `skipped` counter counts them.

## 3. Rows per object

One statement per table per batch, set-based:

- **folders** (`kind = 'dir'`, `origin = 's3_inventory'`): created on demand, top-down, with the folder ACL.
- **file**: `state = 'remote'`, `resident_bytes = 0`, `size`, `mtime_ns` = LastModified, `origin = 's3_inventory'`, the file ACL.
- **file_version**: `kind = 'content'`, committed at LastModified.
- **location**: `role = 'primary'`, `state = 'committed'` on the S3 backend, with `container`, `object_key`, `backend_version_id`, `storage_class`, `logical_tier` (the backend's own map; Intelligent-Tiering archive tiers are `archive`), `size`, `etag`, and `md5` only where the ETag is an MD5 (single part, not SSE-KMS/C, from the inventory's `EncryptionStatus`). `feed_seen_at = snapshot_at`, so an S3 event older than the snapshot can't undo it. Not `verified`: decision 24's check is the Storage Mover step.

There are two ways to write them, with the same result (section 6): row by row, where the catalog's triggers link each file to its version and location, and in bulk, where `catalog.seed_merge` writes every row in its final form. The feed trigger, `file_feed_insert`, is statement-level on both.

**POSIX attributes.** Neither S3 Inventory nor a listing carries `x-amz-meta-file-*`, and a HEAD per object is not affordable at 10^8. uid and gid come from `dataset.posix_defaults` (`{"uid", "gid"}`, default 0); the mode follows the ACL (0660 files, 0770 folders). Reading `x-amz-meta-file-*` is left to the recall path, which HEADs the object anyway.

## 4. Permissions

Every row gets an ACL, because `acl_id IS NULL` means unreadable (`web/access.py`), and S3 objects must never be world-readable (`access-control.md` section 5). Two content-addressed ACLs per source (`catalog.acl`, the same canonical form and `read_sids` as edge ACLs):

- **folder**: owning group = Owners `rwx`, Writers `rwx`, Readers `r-x`, mask `rwx`, other `---`, with the same as `default` entries so new files inherit;
- **file**: Owners `rw-`, Writers `rw-`, Readers `r--`, mask `rw-`, other `---`.

## 5. The snapshot gap

A daily inventory is up to a day old when seeding starts. Changes between the snapshot and registration must not be lost, so the source's change-feed cursor is registered **at `snapshot_at`**, not "from now": the backend's `change_cursor_at(container, at)` (S3: the from-now cursor with that start time, so SQS events since the snapshot are applied).

**The feed is held while seeding.** An event for an object that isn't seeded yet would find no row, and be consumed for nothing; the seed would later write the snapshot's state over the change. So `changefeed_sync` doesn't read a container while any source on it is `registered`, `seeding` or `failed`; the cursor's `last_error` says `held: source N is seeding`. Its events wait on the queue and are applied once every seeded row exists, newer than `feed_seen_at = snapshot_at`. User recalls don't wait: they poll their own restores.

**A bucket already followed.** Its cursor stays where it is. Events read before this registration can't be read again, so an inventory source is refused if the cursor has already applied an event newer than the inventory (`last_record_at > snapshot_at`). Register a bucket's inventory sources together (the feed is held from the first), or seed a later prefix from a listing.

Caveats, in the setup guide:

- the bucket's EventBridge rule and queue must exist before the inventory used was taken. By inference, the bucket's EventBridge setting should also have been on for about five minutes by then. What was observed (real AWS, 2026-11-14) is that changes made seconds after the setting was turned on sent no event and changes made five minutes later did; that a change made after the snapshot but inside those minutes would be missed follows from it, and was not itself observed;
- SQS keeps messages for at most 14 days, so an older inventory is refused, and from the snapshot to the last source seeded must stay under 14 days (about 18 hours of seeding at 10^8 objects, section 6). A `failed` source holds its bucket until it is retried.

A listing has no gap: `snapshot_at` is when the listing started, and the cursor starts there.

**S3 Metadata tables** replay their own gap. `snapshot_at` is the inventory table's snapshot time less an hour (the table is "typically" current within one hour [D]), and the journal is read from there once the source is seeded (section 8). So the already-followed check above doesn't apply to them, and the journal's retention (at least 7 days; the setup guide sets 14) bounds the gap instead of SQS's.

## 6. Running it

Two ways in, one transaction each:

- **The partner API** (`POST /v1/sources`, `portal-api.md` §5.10): `web` writes only the catalog (`create_source`): the checks, the source (`registered`, no snapshot yet) and its `seed` message (PGMQ, decision 27). The worker's first seed step reads the snapshot time and starts the change feed (`start_source`); an inventory that is too old, or whose gap can't be replayed, ends the source `failed` with `last_error`.
- **The CLI** (`register_source`): both halves at once, so such an inventory is refused on the spot.

The worker's `seed` job:

1. reads up to `batch` objects (default 20,000) from where `resume` points;
2. writes them and the new `resume` in one transaction, so a crash replays at most one batch;
3. sends itself the next message, so no message is held longer than one batch.

Re-running a batch is harmless: an object whose `(backend, container, key, version)` already has a committed location is counted as present and skipped.

**Two ways to write a batch.** Batches of at least 1,000 objects (`BULK_MIN`) take the bulk path; smaller ones, and every S3 event (section 8), are written row by row. Both share the present check, key to path, the folders and the drift rows (`sources/seed.py`).

- **Row by row:** one INSERT per table, then one UPDATE committing the versions. The catalog's per-row triggers do the rest: each commit supersedes, points the file at its version, re-syncs its location (`file_resync_location`), sets WORM retention and legal holds, and marks the location for WORM stamping; each file update emits a feed event.
- **Bulk:** the files are COPYed into `catalog.seed_stage` (unlogged; the rows live for one transaction) and merged by `catalog.seed_merge` (migration 0033), an owner-run function that only the worker may call. It writes each file, version and location in its final, committed form, in one statement: the ids are drawn first, so the file names its content version, primary location and tier from the start. Each folder's WORM days and legal hold are worked out once, not per file, and the retention audit rows are one INSERT. The statement-level feed trigger emits one event per file.
- **The trigger bypass.** While `seed_merge` runs, the per-row INSERT triggers on `file_version` and `location` skip (`catalog.seed_bypassed()`): the function turns `omnifile.seed_bypass` on with `set_config` for its one merge statement and off again before it returns or raises (set_config lasts to the end of the transaction otherwise; a test checks the rest of the transaction fires every trigger, and a function SET clause would need a superuser on PostgreSQL 15+), and the check also needs `current_user` to be the catalog's owner, which inside an owner-run function it is. An app that sets the variable itself still runs as its own role, so its inserts fire every trigger (a test checks api, web and the worker). The triggers that fired on INSERT and UPDATE were split, so no UPDATE trigger ever skips, and no other catalog function inserts files, versions or locations (a test checks that too). The bypass only saves time: with it off, the triggers fire on rows that are already final and change nothing but each file's `entity_version` (also tested).
- **Planner-proof:** the staging table's statistics are of other batches, so `seed_merge` never joins staged rows with each other or with the catalog: a taken name is one index probe per row, and a folder's retention and hold a jsonb lookup. A writer that takes a name between that probe and the insert fails the batch with a unique violation, and the queue retries it.
- **Equivalence:** `tests/sources/test_bulk_seed.py` seeds the same objects both ways and compares every table the seeder touches, by path: files, versions, locations, ACLs, the feed in its logical form (each entity's last event, and that it carries the entity's version), the audit log and the drift rows. Only `entity_version` differs (row by row, a file is updated twice after its insert; in bulk, never).

**Throughput (2026-11-04, one worker, an S3 Inventory through the real reader, `tests/api/test_scale.py`):**

| Files | Server | Files/s | Batch of 20,000, first tenth / last tenth |
|---|---|---|---|
| 10^5 | PG 18.6 in the omnifile-acl VM, on ext4 in a loop file on the external drive | 17,535 | 1.04 s / 1.22 s |
| 10^6 | same | 19,202 | 1.08 s / 1.01 s |
| 10^7 | same | 16,546 | 1.02 s / 1.04 s |

The median batch costs the same at 10^7 as at 10^5. The overall rate at 10^7 is a little lower: time beyond the median batches is about 15% of the run there, against under 5% at 10^5 and 10^6. The periodic `ANALYZE` (below), which reads more of a larger table, is the likely cause; it was not timed on its own. The row-by-row path ran at about 1,400 files/s (M7.md, 2×10^5 files), nine tenths of it in the per-row triggers. At the 10^7 rate, 10^8 files take about 1.7 hours on one worker. Sources on different prefixes seed in parallel.

**Statistics.** Seeding still runs `ANALYZE` on `file`, `file_version` and `location` before its first 10 batches and then every 20: the row-by-row path's triggers plan their lookups from them, and so does the present check (`file_resync_location` looks the location up by version id directly, migration 0013, an index condition no plan can skip).

## 7. Readers

Never a LIST on a directory read. In order of preference:

1. **S3 Metadata tables** (`sources/metadata_s3.py`), when the source names them or, with no inventory named, when its backend's `metadata_tables` names them for the bucket.
2. **S3 Inventory, CSV**, when the source names one.
3. **A listing**, the last resort: with no inventory named and no Metadata tables, or explicitly (`{"kind": "listing"}`).

Each is chosen per source, at registration (`--metadata-table`, `--inventory` or `--list` on the CLI; `inventory` on `POST /v1/sources`).

- **S3 Metadata tables** (Iceberg, read with PyIceberg and pyarrow): the bucket's live inventory table, in the account's `aws-s3` table bucket, namespace `b_<bucket>` [D]. The REST catalog is S3 Tables' Iceberg endpoint, signed with SigV4 (`s3tables`); the endpoint and credentials are the backend's own (`s3tables_endpoint_url`, required with private networking, and the binding's credential provider, which decision 43 moves to web identity), never the source row's. The table's data files are read through the backend's S3 endpoint.
  - The table lists every version and delete marker. A key's current object is its row with the largest `sequence_number`, unless that is a delete marker. A key's rows can be in different data files, so before its first batch the reader scans `key` and `sequence_number` once and keeps the newest sequence of each key with more than one row (per worker, source and snapshot). Building it holds the prefix's keys in memory once, about 200 bytes per object; a prefix much over 10^7 objects is better split into several sources until the spike says whether the table lists only current rows.
  - `resume` is `(snapshot id, data file, row)`: a seed pins the snapshot it started from. S3 keeps a snapshot about 24 hours at most [D]; a seed that outlives its snapshot starts again on the current one, and rows already seeded count as present.
  - Keys come as they are (not URL-encoded as in a CSV inventory). `md5` is the ETag only for a single-part upload that is unencrypted or SSE-S3. The tables don't show the Intelligent-Tiering access tier [D], so such objects are `smart` until a read finds them archived.
  - Built behind this per-source choice until the spike on real AWS (`s3-metadata.md` §5, `real-aws-checks.md`). The local tests use a PyIceberg SQL catalog with the published schemas.
- **S3 Inventory, CSV** (native, `manifest.json` plus gzip CSV files). Columns come from the manifest's `fileSchema`; keys are URL-encoded in the CSV. The manifest is checked against `manifest.checksum`; each data file is checked by gzip's own CRC as it streams. `resume` is `(file index, line)`. A gzip stream can't be entered in the middle, so a batch that stops inside a file leaves its stream open in the worker, and the source's next batch continues it: each file is downloaded once. A batch that lands elsewhere (a worker restart, another replica, a rolled-back batch) reads the file again up to its line, once.
- **Parquet and ORC** inventories are refused with a clear error; the setup guide asks for CSV. pyarrow is now a dependency (for the Metadata tables), so a Parquet reader is a small addition if a customer needs one.
- **Listing** (`backend.list`, current versions): `resume` is the listing cursor.

## 8. Keeping a seeded source current

Once every source on a bucket is seeded, its events flow again (section 5), and `changefeed_sync` hands each `Object Created` and `Object Deleted` for a key under a seeded source to `sources/follow.py`. Other records, and keys outside any source, are handled as before.

An event is a prompt, not the truth: events arrive at least once and out of order, and `Object Created` carries no storage class. So the key's **current object is HEADed** (one round trip per event), and the catalog is made to match it:

| S3 now | Catalog at the key's path | Result |
|---|---|---|
| no object, or a delete marker | a file whose bytes come from this key | tombstone (`deleted_at`, a `tombstone` version), as a delete through OmniFile would be |
| an object | nothing live | a new remote file, written as a seed batch writes it (folders created, the source's ACLs), even when a tombstoned file's rewind location names that version (its delete marker was removed) |
| a folder marker (`dir/`) | — | its folder, as seeding creates it; a deleted marker leaves the folder (it may hold files) |
| an object | the file, at the same version (unversioned: the same ETag) | nothing: a replay |
| an object | the file, at another version or ETag | a new content version and primary location on the same file; size and mtime follow, the edge's copy is dropped (`remote`) |
| an object | a folder; or a file whose bytes no longer come from this key (written through OmniFile, migrated); or a file busy on the edge (`landing`, `uploading`, `resident`, `recalling`) | drift `path_conflict` (source `changefeed`), nothing overwritten |

- **Versions.** On a versioned bucket an overwrite keeps the old version in S3, so the old location stays `committed` and rewind reaches it. On an unversioned bucket the old bytes are gone: its location becomes `deleted`. A permanently deleted version's location becomes `deleted` too; if it was current, S3's previous version is current again, and the file follows it.
- **Not drift.** While a source's bucket is live in S3, writes and deletes there are expected, so they are applied, not reported. `deleted_externally` drift stays for keys outside any source.
- **OmniFile's own deletes** (a location `deleting`) are completed as before.
- **Order.** New versions commit at the object's LastModified, never before the version they supersede; locations get `feed_seen_at` = the event time. The catalog holds every writer to this (migration 0031): an edge's rename or delete of a file whose version is dated ahead of the database's clock commits at that version's time.

**From the S3 Metadata journal.** For an `s3_metadata` source the journal table is a second source of prompts (`worker/journal_sync.py`, every minute). Each journal row under the source's prefix becomes the record an S3 event would be, and goes through the same `apply_record`: `CREATE` → created, `DELETE` → deleted (a delete marker as a tombstone record, a permanent version delete with its version), `UPDATE_METADATA` → the storage class (`tier_changed`); annotation rows are ignored. So the table above applies unchanged, and each prompt is still a HEAD.

- **Cursor** (`catalog.source.journal`): `{"snapshot_id", "through"}`. A pass reads the rows appended since that journal snapshot (an incremental append scan; compaction adds no rows), sorted by (record time, key, sequence number). The first pass, and one whose snapshot has expired, scans by `record_timestamp ≥ through` (first: the source's `snapshot_at`). Rows are applied in batches of 200, each committing with `{"to", "after"}`, so a crash replays at most one batch; at most 5,000 rows per pass.
- **Both feeds.** The EventBridge → SQS feed still runs for the bucket: restores come only from there. An object change seen both ways is applied twice, harmlessly.
- Nothing is LISTed and nothing is written to the bucket or the tables (decision 40).

## 9. Reconcile

The nightly reconcile (`reconcile/run.py`) lists a whole container and reports objects no location knows as `unexpected`. A bucket with sources is only partly OmniFile's, so it is reconciled **per source prefix**: only those prefixes are listed (`PrefixedInventory`), and only locations under them are checked for `missing` and mismatches. Only current objects can be `unexpected` there: seeding adopts current versions only, so older versions from before adoption are the bucket's history (they are still listed, so a location naming one is matched). While any source on the bucket is not yet `seeded`, the bucket isn't reconciled at all: the sweep skips it, and an on-demand run is retried in 15 minutes (`SourcesSeeding`). Half-seeded, everything would look missing.

Reconcile reports and never repairs, here too: an `unexpected` object under a seeded prefix is an event the feed missed.

The dataset's own Azure containers (not sources) are reconciled from the account's Blob Inventory report when the backend's config names the rule (`reconcile/blob_inventory.py`, CSV or Parquet, read as a stream), and from a listing otherwise. A report is up to a day old, so what OmniFile deleted or re-tiered after the report started is not drift. Accounts with version-level immutability can't run Blob Inventory: they keep the change feed and listings.

## 10. A source is read-only to OmniFile

A source bucket is the client's. OmniFile **reads** it, **restores** archived objects in it (decision 15: `RestoreObject` and Batch Operations restore waves; Batch Compute checksum reads, too), and **reports** what users deleted or renamed (the cutover report). It never deletes, overwrites, re-tiers, re-tags or rewrites the metadata of an object there: before the migration (from the moment the bucket is registered), while it runs, and after the cutover. On S3 every one of those is a write to the client's object: a class change and a metadata change are an in-place `CopyObject`, which makes a new object version.

**The rule** is per bucket: a location is *on a source* when its backend and bucket are those of a registered source of kind `source` on another backend than its dataset's own (`catalog.on_source(backend_id, container)`, migration 0026; `catalog/source_readonly.py`). Keys outside every source prefix of that bucket are covered too. Adopted data (kind `adopt`) is already in the dataset's own backend and is not covered.

**Every write path asks before any provider call:**

| Path | On a source |
|---|---|
| Rewind depth (`retention/depth.prune_versions`) | The version is still `pruned` (gone from rewind), but its location stays `committed`: no `delete_blob` is queued, and the bytes stay at the source. The `version.prune` audit lists it under `retained_at_source`. Tombstones and the deletes report are unchanged. |
| Retention deletes (`worm_sync.delete_blob`) | Refused: the location goes back to `committed` and `location.delete.refused` is audited (a delete queued before the bucket became a source). |
| The catalog itself | A location on a source can't become `deleting` (trigger `location_source_guard`, SQLSTATE `OF008`). |
| Tiering schedule, superseded versions, pins (`policy/scheduler.py`) | Not a candidate. The file waits for its prefix's cutover; the verified Azure copy is then the primary, and the schedule and pins act on it. |
| Tier moves (`worker/tiering.handle_move`) | A move queued earlier is refused in every phase, audited `tier.move.refused` (`why: on_source`). |
| WORM retention, legal holds, state tags (`worm_sync.stamp_worm`) | Nothing is stamped: the catalog alone holds them for the source object, and the location's flag is cleared. The verified destination copy is stamped like any other location. |
| Registration (`sources.run.create_source`) | A bucket that is one of the backend's operations buckets (`staging_bucket`, `batch.manifest_bucket`, `batch.report_bucket`, `owned_buckets`; `S3Config.ops_buckets`) can't be registered as a source: OmniFile writes there. |
| Batch Operations jobs (`migrate/waves.py`, `migrate/checksums.py`) | Not started when the manifest or report bucket is a registered source bucket (a config changed after registration): the migration's `last_error` says so (`migrate/opsbuckets.py`). |
| Staging (`migrate/staging.py`) | Copies go only into the staging bucket's `omnifile-staging/` prefix, never into a bucket that is a source; `unstage` deletes only keys under that prefix of a bucket that isn't one. |

**Second line, in `S3Backend`.** `put`, `copy_object` (its destination), `set_class`, `set_metadata`, `set_tags`, `delete` and a write credential raise `WriteRefused` (an `UnsupportedOperation`), and log it, for any key that isn't OmniFile's own (`S3Config.owns`): the staging bucket's `omnifile-staging/` prefix, the Batch Operations manifests (`batch.manifest_bucket` under `manifest_prefix`), or an `owned_buckets` entry (none on a customer's binding; the backend's contract tests). Nothing is sent to S3.

**Third line, in the customer's IAM policy** (setup guide). The role OmniFile's service and edges assume gets, on each source bucket, `s3:GetObject`, `s3:GetObjectVersion`, `s3:GetObjectTagging`, `s3:ListBucket`, `s3:ListBucketVersions` and `s3:RestoreObject`, and an explicit deny of every object write:

```json
{
  "Sid": "SourceIsReadOnly",
  "Effect": "Deny",
  "Action": [
    "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
    "s3:PutObjectTagging", "s3:PutObjectVersionTagging",
    "s3:DeleteObjectTagging", "s3:DeleteObjectVersionTagging",
    "s3:PutObjectRetention", "s3:PutObjectLegalHold", "s3:PutObjectAcl"
  ],
  "Resource": "arn:aws:s3:::SOURCE-BUCKET/*"
}
```

`s3:PutObject` is denied outright (a same-bucket `CopyObject` needs it, and no OmniFile feature does one). Writes are allowed only on `arn:aws:s3:::STAGING-BUCKET/omnifile-staging/*` (put, delete) and the manifest prefix. This repository has no AWS deployment templates yet; the statement goes into the setup guide's S3 role when it is written.

## 11. Next

- The S3 Metadata spike on real AWS (`s3-metadata.md` §5); then decide whether a bucket with a journal still needs its object events on SQS.
- POSIX attributes from the tables' `user_metadata` (File Gateway's `x-amz-meta-file-*`) instead of the dataset defaults (section 3).
