# WP4: catalog scale and the Azure backlog

Branch `claude/m7-catalog-scale`. Everything here runs on sim and fakes. Nothing called real Azure or AWS.

## What is built

### 1. The bulk seed path (`sources/seed.py`, migration 0033)

- **How a batch is written.** A seed batch of at least 1,000 objects is COPYed into `catalog.seed_stage`, an unlogged table whose rows live for one transaction. One owner-run function, `catalog.seed_merge`, then merges the batch into the catalog. Smaller batches, and every S3 event (`follow.py`), still go row by row. The default batch is now 20,000 objects (it was 5,000).
- **What the merge writes.** It writes each file, version and location in its final, committed form, in one statement: the file names its content version, primary location and tier from the start. It works out each folder's WORM days and legal hold once, not once per file. It writes the `version.retention` audit rows as one INSERT. The statement-level feed trigger emits one feed event per file.
- **The trigger bypass.** While `seed_merge` runs, the per-row INSERT triggers on `file_version` and `location` skip.
  - The check (`catalog.seed_bypassed()`) needs both the `omnifile.seed_bypass` setting, which the function turns on for its merge statement and off before it returns or raises, and `current_user` to be the catalog's owner. That is true only inside an owner-run function.
  - The triggers that fired on both INSERT and UPDATE were split, so no UPDATE trigger ever skips. The split keeps each table's firing order, and a test checks it.
  - Only the worker may call `seed_merge` or touch `seed_stage` (`catalog/privileges.py`).
- **Tests** (`tests/sources/test_bulk_seed.py`, 12):
  - Equivalence: a bulk-seeded and a row-by-row-seeded catalog of the same input compare equal across files, versions, locations, ACLs, the feed in its logical form (each entity's last event, which carries the entity's version), the audit log and the drift rows. The input covers nested folders, folder markers, unmappable keys, path conflicts within the batch and with existing entries, objects already present, a WORM share and a legal hold. A replay changes nothing. Only `entity_version` differs: the row path updates a file twice after its insert, and the bulk path never does.
  - With the bypass forced off, the triggers fire on the merged rows and change nothing but `entity_version`. So the bypass only saves time.
  - api, web and the worker setting `omnifile.seed_bypass` themselves still fire every trigger (retention is set, the file points at its version).
  - No other catalog function inserts files, versions or locations.
  - Only the worker can stage and merge. The worker can't change the bypass function or turn a trigger off.
  - A mutation check: with the merge's WORM flag dropped, the equivalence test fails.
- **Planner-proof.** The first version of the merge joined the staged rows with each other. The staging table's statistics came from an empty table, so the planner chose nested loops and a 20,000-row batch took 30 s (quadratic). Now the merge has no joins: the ids are drawn up front, a taken name is one index probe per row, and a folder's retention and hold is a jsonb lookup.

### 2. The Blob Inventory reader (`reconcile/blob_inventory.py`)

- **Formats.** CSV and Parquet in Azure's documented layout: `YYYY/MM/DD/HH-MM-SS/<rule>/`, `<rule>-manifest.json`, then `<rule>-manifest.checksum` (MD5 of the manifest, hex or base64).
- **Streaming.**
  - CSV is parsed as it streams: only complete records, and a quoted field may span chunks.
  - Parquet is read through a seekable file that fetches byte ranges on demand, one row group at a time.
- **When a report is used.** Only when it is the newest run whose checksum matches, with status `Succeeded`, at most 48 h old (`max_age_hours`), a blob rule over block blobs whose prefixes cover the whole container, `Name`, `Content-Length` and `AccessTier` in its schema, and, on a versioned account, `includeBlobVersions` with `VersionId` and `IsCurrentVersion`. Otherwise reconcile lists the container and logs why. A WORM (version-level immutable) account never uses an inventory.
- **Configuration.** The backend config's `inventory` (`{"container", "rule", "max_age_hours", "accounts": {...}}`) chooses the report (`reconcile/inventory.py` `choose_inventory`).
- **Reconcile changes** (`reconcile/run.py`). Duplicate rows (which Azure documents) are dropped. A blob OmniFile deleted, or a tier OmniFile changed, after the report started is not drift.
- **Tests** (`tests/reconcile/test_blob_inventory.py`):
  - generated CSV and Parquet fixtures, built from sim's own listing;
  - chunk boundaries of 1 to 7 bytes, with quotes, commas, newlines and Unicode in names;
  - streaming: the first record comes from the first chunk, and no Parquet range read is a quarter of the file;
  - every manifest refusal;
  - the newest good run chosen over failed and bad-checksum runs;
  - the same drift found from a CSV or a Parquet report as from a listing;
  - staleness and duplicates;
  - per-account settings and the default tier;
  - fallback to a listing, and WORM accounts.

### 3. Container-level immutability (`retention/policy_admin.py`)

- **How.** `AzureContainerPolicy` drives `azure-mgmt-storage`'s `blob_containers` (`get_`, `create_or_update_`, `lock_`, `extend_` and `delete_immutability_policy`) with the worker's managed identity. The ARM location comes from the backend config's `management` (`subscription_id`, `resource_group`, `account_groups`).
- **Concurrency.** Every change after the first is conditional on the policy's ETag. A policy changed meanwhile fails with 412, and the worker retries in 5 minutes.
- **Refusals.** A locked policy is never shortened, unlocked or deleted (`Immutable`). Without management settings, the worker retries hourly, as before.
- **Tests** (`tests/retention/test_container_policy.py`, 11). They run against a fake of the SDK operations that keeps ARM's rules and records every call. They check the exact calls and parameters (the JSON body sent, the ETag and the match condition) for each step. They also run the retention queue's `sync_share_policy` through enable, lock, extend and cancel.

### 4. The archive-copy integrity check (`retention/worm_sync.py` `delete_blob`)

- **When it runs.** Before the original of a copy-and-delete archive move is deleted. The copy that replaces it is then already in Archive, where it can't be read back.
- **What it compares.** It reads the copy's properties from the provider and compares them with the catalog: the length; the CRC64-NVME where the provider keeps a whole-object one; and the Content-MD5 where both sides have one. A copy whose properties carry no checksum must have passed the in-Azure CRC64 read-back.
- **On a mismatch.** A mismatch, an unverifiable copy, a missing copy or no copy at all blocks the delete. The original is the primary again (still `verified`), the copy is dropped through the same queue, `location.delete.refused` is audited with what was expected and seen, and the file is looked at again in a day.
- **Azure's limit.** Azure's Get Blob Properties has no whole-blob CRC64 (checked against Microsoft's REST reference, updated 2026-10-29). On Azure the check is length plus MD5, or the read-back.
- **Tests** (`tests/retention/test_archive_copy_check.py`, 8). The moves run through the real tiering and verify consumers on sim; then the provider's answer about the copy is changed. With the check removed, 6 of the 8 fail; the 2 that still pass are the good-copy paths.

### 5. Privileges (`catalog/privileges.py`)

- `seed_stage` is worker-only (SELECT, INSERT); no other app can read it.
- `seed_merge` is an owner function only `omnifile_worker` may EXECUTE.

## Scale

`service/tests/api/test_scale.py`, with one worker and an S3 Inventory read through the real reader. Python ran on the Mac; Postgres ran in the omnifile-acl VM, reached through an SSH forward.

- **Server:** PostgreSQL 18.6 (Ubuntu 26.04 package), a separate cluster on port 55434, its data on ext4 in a 40 GB sparse loop file on the external APFS drive (`/mnt/ext/wp4-pg.img`).
- Native PG 17 on the Mac couldn't start: SysV shared memory is exhausted system-wide.
- Docker couldn't bind-mount the external drive.

| Files | Files/s | Batch of 20,000: first tenth / last tenth | Snapshot page: first / last | Catalog |
|---|---|---|---|---|
| 10^5 | 17,535 | 1.04 s / 1.22 s | 0.13 s / 0.11 s | 153 MB |
| 10^6 | 19,202 | 1.08 s / 1.01 s | 0.12 s / 0.13 s | 1.3 GB |
| 10^7 | 16,546 | 1.02 s / 1.04 s | 0.14 s / 0.15 s | 12.0 GB |

- **Flatness:** the median batch is flat from 10^5 to 10^7. The overall rate dips at 10^7 because time beyond the median batches grows to about 15% of the run, against under 5% at 10^5 and 10^6. The likely cause is the periodic `ANALYZE` on a larger table; it was not timed on its own.
- **10^8:** at the 10^7 rate, about 1.7 hours on one worker. It wasn't run: its catalog would be about 120 GB.
- **Other figures at 10^7:** the paged snapshot took 161 s (1,000 pages), the daily digest 6.3 s, a 1,000-entry as-of listing 35 ms, and one change 0.21 s.
- **Docker comparison:** on the shared Docker Postgres 17, the same test at 10^5 gave 14,862 files/s. The row-by-row path's figure in M7.md was 1,378 files/s.

The command, from `service/`, with N = 100000, 1000000 and 10000000 (the 10^7 run took 13 minutes, the seeding 10):

```
OMNIFILE_SCALE_FILES=N OMNIFILE_SCALE_MIN_RATE=10000 \
  OMNIFILE_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55434/postgres \
  uv run pytest -q -s tests/api/test_scale.py
```

The scale cluster and its loop file were deleted afterwards. The test drops its own database.

## Lanes run

- **Service suite** (`cd service && uv run pytest -q`), after the review fixes, on a private Postgres 17 container: 1312 passed, 50 skipped. The new test files are `test_bulk_seed` 14, `test_blob_inventory` 24, `test_container_policy` 11 and `test_archive_copy_check` 12, plus one in `tests/azure/test_backend.py`.
  - On the shared Docker Postgres (55433), while other packages' suites ran, 2 to 12 feed-reading tests failed per run. A different set failed each time, and each passed when rerun alone.
  - The first run of the branch on 55433, before the review fixes, was clean: 1298 passed, 50 skipped.
- **Lint:** `uv run ruff check src tests` is clean, and `uv run ruff format --check src tests` reports 351 files already formatted.
- **Privileges:** `tests/catalog/test_privileges.py` passes (in the suite). Every test runs each app under its own role.
- **Scale:** 10^5, 10^6 and 10^7 passed, with `OMNIFILE_SCALE_MIN_RATE=10000` (above).
- **ACL permission matrix** (`sudo /work/tests/acl/matrix.sh` in omnifile-acl, with omnifile-dc, `SMBGUARD_BIN` built into `/mnt/ext`): PASS 130, FAIL 0, plus the 1 FINDING the script reports for its documented `chmod 644` cell.
  - WP4 changes nothing the matrix runs. The VM's `/work` is the open-questions worktree, whose `fsal/`, `edge/` and `tests/acl/` differ from main's. So this run checks the VM after its mount and memory change, not this branch's edge code.

## Left

- **P (real Azure, pilot 4):** the VERIFY items below; a reconcile from a real Blob Inventory report; the immutability policy calls on a real WORM account; and 10^8 seeding on Azure Database for PostgreSQL (about 120 GB of catalog).
- **L:** the periodic `ANALYZE` could be spaced out on large catalogs (about 15% of the 10^7 run). Not done here.
- **L (integration PR):** fold this file into M7.md, and tick the three lines of `real-azure-checks.md` "Not real-Azure, but still open".
- **Fixed after the first report** (lead's follow-up):
  - **One check for every delete of an original.** `backends/azure/backend.py` `_archive_move`, and sim's own move, deleted the original after only a length check when it had no Content-MD5. Both now use `backends/copy_check.copy_mismatch`, the rule `delete_blob` uses. An original they can't check is kept, and the unchecked copy is removed.
    - Two tests now fail without the fix: the Azure backend over its fake, and sim.
    - A scan of the source requires the check in every function that copies and deletes, and lists every caller of a backend `delete` outside the backends (delete_blob, export parts, staging copies, failed migration copies). A new one fails until it is looked at.
    - Two test helpers that archived synthetic, MD5-less blobs through that move now archive them as the tiering worker does.
  - **Reconcile records how it read** (migration 0034).
    - `reconcile_run.method` is now `inventory`, `listing` or `listing_changefeed`, and the new `method_detail` holds the report read or why the run listed: no inventory configured, no usable report and why, or a bucket's source prefixes.
    - `ReconcileReport` carries both, and the worker logs them per run. Nothing else shows reconcile runs today: no portal page or metric reads them.
    - Older non-WORM rows keep `inventory`.
- **Fixed after the independent review of #35** (each with a test that fails without it):
  - **A refused delete no longer swaps back and forth.** The refused copy is demoted to a secondary as well as dropped. Its own delete is then not a replacement's and needs no check, so the corrupt copy is never made the primary. One refusal, then the copy goes. (Azure's worst case: no CRC64, no MD5, an original never read back.)
  - **An incomplete report is not believed.**
    - Each report file must have its manifest's length (a HEAD when the report is chosen, and the bytes counted as it streams).
    - The rows must total the manifest's `summary.objectCount`.
    - A `creationTime` rule (only recent blobs) is refused.
    - A problem found while reading rolls the comparison back, and the run lists the container instead, recording why. No partial report becomes drift.
  - **The files' own columns are checked:** a CSV header or a Parquet schema without a field reconcile reads is refused, whatever the manifest's schema says.
  - **The equivalence input** now has a seeded path whose earlier entry is a tombstone. A direct test also covers the merge's parent guard (a file, a deleted folder, another dataset's folder).
  - **`seed_bypassed()`** is schema-qualified (`pg_catalog.current_setting`, `OPERATOR(pg_catalog.=)`), so a schema ahead of `pg_catalog` in a caller's `search_path` can't turn it on. It is still inlined; seeding at 10^5 on Docker ran at 14,842 files/s after the change, against 14,862 before.

## For the integration PR and other packages

- **New direct dependency:** `azure-mgmt-storage` 25.3.0 (MIT), with `azure-mgmt-core` 1.6.0 (MIT). `service/pyproject.toml` and `service/uv.lock` gained only these. WP6 owns `LICENSES.md`, so the licence entries are for WP6 to add.
- **VERIFY lines for `real-azure-checks.md`** (WP5's file):
  - Blob Inventory: CSV value formats, whether `Name` is ever encoded, the checksum file's encoding, `AccessTier` on a default-tier blob, and the Parquet column names;
  - Get Immutability Policy on a container with no policy (a 404 or zero days);
  - that `immutabilityPolicies/default` is the version-level default policy;
  - whether Azure ever exposes a whole-blob CRC64 in properties (Blob Inventory documents a `Content-CRC64` field).
- **The archive-copy line** in `real-azure-checks.md` ("Not real-Azure, but still open") can be ticked. So can the container-immutability and inventory-reader lines, except their VERIFY parts.
- **M7.md's scale section** can take the table above.
- **Tools:** PostgreSQL 18.6 (`postgresql-18` from Ubuntu's archive) installed in omnifile-acl, with no default cluster.
- **omnifile-acl's `lima.yaml`** now has a writable mount, `/Volumes/Main External/Development/omnifile-scratch` at `/mnt/ext`, left for WP1, and memory raised to 16 GiB.

## Fixed after merge (WP3 found it)

- **Migration 0033 needed a superuser.** `seed_merge` set `omnifile.seed_bypass` with a SET clause. On PostgreSQL 15+, only a superuser may create a function with a custom parameter in its SET clause. The dev VM, which migrates as a non-superuser, failed, and so would Azure's administrator.
- **The fix, in place.** The function now turns the setting on with `set_config` for its merge statement, and off before it returns or raises. A test checks that the rest of the transaction fires every trigger, even as the owner.
- **Databases that applied the old 0033** are accepted by `migrate.py` (`SUPERSEDED`): the old function works where it applied, and only superusers could apply it.
- **The suite now migrates as the deployment does.** It runs as `omnifile_migrator`: no superuser, CREATEROLE and CREATEDB, owning the database (`tests/conftest.py`). The old 0033 fails there with `permission denied to set parameter "omnifile.seed_bypass"`.
