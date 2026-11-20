# Edge metadata: the edge's local replica of the catalog (risk R2)

**Status: PROPOSED (2026-10-05), M0 design note for risk R2 in `docs/BUILD-PLAN.md`.** It builds on `docs/design/fsal-namespace.md` (R1), which recommends materialising every catalog object as a placeholder on the edge's xfs volume. The FSAL ⇄ daemon protocol is `docs/contracts/fsal-socket.md`. The edge ⇄ service API and catalog schema are being written in parallel (`docs/contracts/control-api.md`, `service/src/omnifile_service/catalog/migrations/`); section 12 lists what this design needs from them.

Items marked **TEST** need a prototype; **VERIFY** means not checked yet.

## The problem

`ARCHITECTURE.md` says the FSAL "resolves every path through the catalog". The catalog is Postgres in Azure, a WAN round trip away (tens of ms over ExpressRoute or VPN), and it may be unreachable. NFS clients issue LOOKUP and GETATTR at thousands per second, and the spike showed that one slow call from a Ganesha worker stalls unrelated exports. So the edge needs a local copy of the catalog rows it serves, kept current by the service's feed, and it must keep working when the service is down.

## Recommendation in brief

1. **The namespace replica is the xfs tree itself** (R1, option c). Every catalog object exists as a real file or placeholder, so LOOKUP, GETATTR and READDIR are served by xfs and never consult a database.
2. **The FSAL reads no database.** Per READ it reads only its own 64-byte marker xattr (cached per handle) and an in-memory pending table fed by the daemon. Nothing in a Ganesha thread waits on the daemon or on disk pages of a database.
3. **The daemon owns a SQLite database** (WAL mode, through `rusqlite`) holding everything xfs can't: catalog ids, versions, locations, recalls, the feed cursor, the outbox of edge changes and commit clones. About **270 bytes per object** (measured), so about 27 GB at 10⁸ objects.
4. **It is fed by the service's ordered feed** (`after=` cursor, `entity_version` per row), bootstrapped from a bulk snapshot export, and checked by a periodic reconciliation walk.
5. **Authority is split by kind of fact**: the edge is authoritative for what users did on the edge until the service acknowledges it; the service is authoritative for everything else.

## 1. What lives on the edge, and where

| Fact | Where it lives | Writer | Reader |
|---|---|---|---|
| Names, directory tree | xfs | daemon (from feed), users (NFS/SMB) | Ganesha, Samba, daemon |
| Size, mtime, uid, gid, mode | xfs inode | daemon (from feed), users | Ganesha, Samba |
| ACL (enforced) | xfs POSIX ACL xattrs (decision 28) | daemon (from feed), users (Explorer, `setfacl`) | Ganesha, Samba |
| Which bytes are local; `ONLINE`/`ARCHIVED`; `file_id`, content `version_id` | marker `trusted.omnifile` (+ optional chunk map) | daemon only | FSAL, smb-guard, daemon |
| Catalog ids, version history known locally, locations, tiers, restore state, hashes | SQLite `replica.db` | daemon | daemon |
| Feed cursor, snapshot progress | SQLite | daemon | daemon |
| Outbox of edge changes, commit clones (decision 31) | SQLite + `.omni/commit/` on xfs | daemon | daemon |
| Recalls in flight | SQLite, pushed to the FSAL's pending table | daemon | daemon, FSAL |
| Share config (policy, quiet period, rewind, WORM, groups) | SQLite; rendered into Ganesha and Samba config | daemon | daemon, config renderer |
| smb-guard event journal | separate SQLite file (M2 design) | daemon | daemon |
| Rewind trees materialised so far | xfs under `.omni/snap/`, index in SQLite | daemon | everyone (read-only) |
| **M5:** cached chunks of `ONLINE` placeholders (chunk-range reads) | the placeholders themselves: bytes on xfs, the chunk map `trusted.omnifile.map` and the marker (`fsal-namespace.md` §5.4) | daemon only | FSAL, smb-guard, daemon |
| **M5:** the cache's LRU and budget accounting | memory only (`omnifile-cache` `Index`), rebuilt from each file's marker and map on first use after a restart; never in `replica.db`, never reported to the service | daemon | daemon |

The guard journal is a separate file because it `fsync`s per event; it must not queue behind a bulk feed apply.

## 2. Why the FSAL does not read the replica

The question in the M0 brief was whether the C FSAL should read the replica directly (for example a read-only, mmap'd LMDB) or ask the daemon. With R1's option (c) it does neither:

| Need in a Ganesha thread | Answered by |
|---|---|
| LOOKUP, GETATTR, READDIR, ACL checks | xfs via `FSAL_VFS` |
| "Is this range local?" | the marker xattr, cached per handle |
| "Is a recall already running, and what do I answer?" | the FSAL's pending table, pushed by the daemon |
| "Start a recall / fetch this range" | an async message to the daemon; never waited on |

Reading an mmap'd LMDB from Ganesha would be possible (readers never block on the writer), but:
- a page fault on a cold page of a 30 GB file is a disk read inside a worker thread;
- the C side would have to parse rows whose schema the Rust side owns, so every schema change becomes a two-language, two-licence (LGPL FSAL, Apache-2.0 daemon) release;
- it would only be needed if the namespace lived outside xfs, which R1 rejects.

If R1 were decided differently, this section would have to be reopened: LMDB would then be the best fit.

## 3. Store choice for the daemon

| | SQLite (WAL) | RocksDB | LMDB | redb / fjall (pure Rust) |
|---|---|---|---|---|
| Rust crate maturity | `rusqlite`, very mature | `rust-rocksdb`, mature, C++ build | `heed`, mature | younger |
| Secondary indexes, ad-hoc queries | SQL | build your own | build your own | build your own |
| Bulk load 10⁸ rows | fast if indexes are built after (measured below) | fastest | fast | fast |
| Crash safety | WAL, well understood | WAL | copy-on-write | copy-on-write |
| Inspectable by support staff | `sqlite3` CLI | tools exist | `mdb_dump` | weak |
| Licence | public domain | Apache-2.0 / GPLv2 | OpenLDAP (BSD-style) | MIT / Apache |

**Recommend SQLite.** The daemon's access pattern is one writer (feed apply, outbox) and modest reads (by `file_id`, by inode, by `(parent, name)` for reconciliation). SQL makes reconciliation, conflict queries and support diagnostics simple, and the per-row cost is acceptable.

**Measured** on the spike VM (2026-10-05, Python `sqlite3`, WAL, `synchronous=normal`, 8 KB pages), with a row shaped like `catalog.file` plus its primary location (realistic names and object keys, 64-character blob version id):

| Metric | Result |
|---|---|
| Size | 535 MB for 2 × 10⁶ rows with indexes on `(parent_id, name)` and `ino`: **268 bytes per row** |
| Bulk insert, indexes built afterwards | ~508,000 rows/s, then 1.5 s per index |
| Point lookup by `(parent_id, name)` | ~107,000/s from one Python thread |

The database fit in page cache in this test. At 10⁸ rows (≈27 GB) it will not on a typical appliance; **TEST** bulk load and lookup rates at 10⁸ on the M0 dev VM. The plan does not depend on high lookup rates, because no NFS operation needs a replica lookup.

## 4. Replica schema (v0)

Column names follow `catalog.file`, `catalog.file_version` and `catalog.location` (in the catalog migrations), so feed rows map one to one.

| Table | Key | Main columns |
|---|---|---|
| `node` | `lid` (edge-local id) | `file_id` (catalog id; NULL until the service assigns it), `dataset_id`, `parent_lid`, `name`, `kind`, `size`, `mtime_ns`, `uid`, `gid`, `mode`, `acl_id`, `content_version_id` (the version holding the bytes; the feed's `version_id`), `entity_version`, `deleted`, `ino`, `ino_gen` (the xfs inode once materialised), `fs_pending` (a filesystem op not yet done) |
| `version` | `version_id` | `file_id`, `seq`, `kind`, `size`, `mtime_ns`, `crc64nvme`, `blake3`, `committed_at`, `retention_until` (only versions the edge needs: current, ones being uploaded, ones placed in rewind trees) |
| `location` | `location_id` | `version_id`, `backend_id`, `container`, `object_key`, `backend_version_id`, `role`, `logical_tier`, `storage_class`, `verified`, `restore_state`, `restore_expires_at` |
| `acl` | `acl_id` | owner and group SIDs, entries (tag, SID, perms), default entries |
| `dataset`, `share` | ids | write backend, on_open policy, quiet period, resident_mb, rewind granularity and depth, WORM retention, trust level, Owners/Writers/Readers groups |
| `recall` | `(file_id, version_id)`, content version; **at most one active row per key** | `state` (catalog names: requested, rehydrating, copying, downloading, available, failed, cancelled), `eta_s`, `recall_id`, requesters, `started_at` |
| `outbox` | `op_id` | edge change (create, commit, rename, unlink, setattr, ACL change), payload, `state` (queued, sent, acked, conflict), attempts |
| `commit_clone` | `(lid, n)` | clone path, size, hashes, upload state, `version_id` once assigned |
| `snap_dir` | `(share_id, label, dir_file_id)` | populated time, last access (eviction) |
| `feed` | singleton | `cursor` (opaque string from the feed's `next`), `snapshot_as_of` (opaque cursor), `snapshot_state` |

Indexes: `node(file_id)` unique, `node(parent_lid, name)` unique among live rows, `node(ino)`, `outbox(state)`, `recall(state)`.

## 5. How the replica is fed

### 5.1 Steady state: the feed

The control API's feed (`GET /v0/edges/{edge_id}/feed?after=<cursor>&limit=…&wait=…`, long-poll; `docs/contracts/control-api.md`) returns committed events in order. The cursor is an **opaque string** (`"0.0"` to start). The catalog orders events by `(txid, event_id)` below the xmin horizon, so the edge stores the cursor as given and continues from `next`, even after an empty page. Each event names an entity, its id and its `entity_version`.

The apply loop:
1. Read a batch. Skip any event whose `origin` is this edge and whose `op_id` the outbox already holds as acked: it is our own echo.
2. In **one SQLite transaction**: for each event, compare `entity_version` with the stored one. Older or equal: skip. Newer: update the row and set `fs_pending = 1` if the filesystem must change. Store the batch's `next` cursor.
3. Perform the pending filesystem operations (create placeholder, update attributes, rename, unlink, set ACL, stub), each written to be **idempotent**: re-running one after a crash finds the work done and does nothing. For example, a placeholder is created with `O_TMPFILE` and linked to its name; if the name already holds an inode whose marker carries the same `file_id`, the create is already done.
4. Clear `fs_pending` in a second transaction. Send `INVALIDATE` to the FSAL for each inode whose attributes or marker changed.
5. Acknowledge the stored cursor to the service (monitoring and retention only).

On restart the daemon first redoes every row with `fs_pending = 1`, then resumes the feed from the stored cursor. Since application is idempotent and version-guarded, a duplicated or replayed event is harmless.

**Directory moves are one event.** The catalog keys files by `(parent_id, name)`, and a directory rename changes only the directory's row (`catalog.file`, `catalog.file_version` comment "a directory rename needs no child versions"). The edge applies it as one xfs `rename`.

**Order between parents and children is not guaranteed.** The feed's `(txid, event_id)` order is final but follows commit order across transactions, so a child's upsert can arrive before its parent's, and a directory's tombstone can arrive before some of its children's (correction from the M0 review). The edge therefore:
- **parks orphan upserts:** a row whose `parent_id` is not yet known is stored in SQLite with `fs_pending = 1` and a `waiting_parent` flag; when the parent is applied, its waiting children are materialised. Orphans older than 1 hour are reported (the reconciliation digest then finds the gap);
- **retries `rmdir` on `ENOTEMPTY`:** a directory tombstone whose directory still has entries stays pending, and is retried each time a child tombstone in that directory is applied. Entries with local unsent changes are kept, and the directory with them (conflict rules, section 7). The service never sends a recursive delete.

During a snapshot load (below) order is arbitrary anyway; the loader inserts rows first and materialises top-down afterwards.

### 5.2 Bootstrap: 10⁸ rows

A new edge, a new dataset (for example an S3 source registered with `POST /v1/sources`), or a cursor older than feed retention (`reset_required = true`) needs a snapshot.

1. Request a **bulk export** for the dataset. The service writes compressed NDJSON or Parquet parts to a blob container and returns their SAS URLs and the `as_of` cursor (section 12). Paged JSON stays for small datasets.
2. Download the parts over the private endpoint. At ~250 bytes per row raw, 10⁸ rows are ~25 GB, roughly 5–8 GB compressed (estimate).
3. Load into SQLite with indexes dropped, then build indexes.
4. Materialise top-down with a pool of workers per directory: directories first, then placeholders, using the atomic `O_TMPFILE` create (R1 §5).
5. Resume the feed from the `as_of` cursor.

Pages read late in the export may already contain changes made after the `as_of` cursor. Replaying the feed from the `as_of` cursor is still correct, because `entity_version` makes older events no-ops. A row deleted before its page was read is simply absent, and its tombstone event is then a no-op.

**Estimated time for 10⁸ objects:** download minutes; SQLite load 10–30 min; xfs materialisation 35–85 min at 20–50k creates/s (the VM measured ~106,000/s in page cache, R1 §2). About **1–2 hours**, during which the share can already serve whatever is materialised; unmaterialised names simply don't exist yet. Shares with a migrating dataset should not be announced to users until materialisation is complete. **TEST** on a real disk.

**Re-snapshot of a live edge** (after `reset_required`) must not recreate inodes, or every client handle goes stale. It loads into a fresh SQLite file, diffs against the live one, and applies only the differences to the existing tree.

**After a catalog restore** (`deploy/azure/README.md`, catalog backup and restore) the re-snapshot meets one more case: entries the restored catalog never had. These are files and folders created, or versions committed, after the restore point. `catalog.feed_restored()` moves the catalog's sequences 2^40 ahead, so no id is reused: an id in the replica but absent from the snapshot is such an entry, never another file's. The diff therefore:
- applies the snapshot's rows **whatever their `entity_version`**, which the restore wound back;
- never deletes an entry the snapshot lacks. A resident file (its bytes on the edge) goes to the outbox as a new create and commit, like a write made while the service was unreachable. A stub of a version the catalog lost has bytes only in the backend. It stays a placeholder that answers `EIO`, and the reconcile report lists its blob as `unexpected`, for an admin to adopt or delete;
- keeps local unsent changes, as section 7 does.

**Built** for paged snapshots (`feed_loop::resnapshot`, `Replica::resnapshot_*`): on `reset_required` the daemon reads every served dataset's snapshot and applies it in place. A file whose local version is newer than the catalog's (committed after the restore point) is caught by version ids, which only grow. Its local bytes are committed again as a new version of the same file, or, for a stub, the catalog's version is applied. The feed then resumes from `as_of`. **Built** too, the bulk export for 10^8-row datasets (control API §6):
- On `reset_required` the daemon first asks for an export of each dataset, and waits up to `[feed] export_wait_s` (default 4 h; 0 turns it off) for it to be ready.
- Each part is read in CRC-checked ranges and checked against its size and SHA-256 before any row is used. It is then decoded on a blocking thread (`ruzstd`) and applied in batches of 5,000 through the same `resnapshot_*` calls as the pages.
- A dataset with no export (`409 layout_conflict`, an S3 dataset), or an export that fails, is paged instead. Rows a failed export already applied are applied again by the pages.
- Tests: `daemon_linux.rs` runs the restore scenario three ways (paged, bulk, a corrupt part that falls back), and unit tests cover the part decoder.

A fresh edge still reads the feed from `0.0`; only a re-snapshot uses exports.

### 5.3 Reconciliation

Things can drift: an event the notification group missed while the daemon was down, an admin's manual change, a bug. A reconciliation pass:
- **Edge tree vs replica.** Walk xfs with bulkstat-style inode scans (fast, no path resolution) and compare inode, size, mtime, marker and ACL with the replica. Differences become outbox entries (user changes the daemon missed) or repairs (daemon state not applied). Runs after every daemon start that follows an unclean stop, and nightly. Built so far as the namespace sweep (`namespace-sync.md`): at every start and daily, by directory; it also commits new files no close was seen for (WP9, §5.4).
- **Replica vs catalog.** Per dataset, compare counts and an order-independent digest with the service: the XOR, over live rows, of the first 8 bytes of SHA-256(`"<file_id>:<entity_version>"`) (control API). The dataset digest is cached nightly by the service; per-directory digests are computed on demand. A mismatch narrows by directory, then re-fetches the differing rows.

### 5.4 Edge-originated changes

User activity on the edge produces outbox entries:

| Source | Event | Outbox op |
|---|---|---|
| detector (quiet period after `FAN_CLOSE_WRITE`, FSAL `WRITE_CLOSED`, or `FAN_MODIFY` on NFSv3) | a version is complete | `commit` (with commit clone) |
| notification group | `FAN_CREATE` of a directory | `create` |
| notification group | `FAN_RENAME` | `rename` |
| notification group | `FAN_DELETE` | `unlink` (a tombstone) |
| notification group | `FAN_ATTRIB` | `setattr` or `acl` (ACL re-read from xfs) |

**A quiet file is not always a finished one** (WP3's soak: a version committed mid-write on a large NFSv4.1 copy, after a writeback stall outlasted the quiet period). Before it commits, the daemon checks two things (`commit.rs`):

- **NFSv4 opens.** It asks every FSAL whether an NFSv4 client still holds the file open for write (`OPEN_STATE_QUERY`, `fsal-socket.md` §5.5). An OPEN for write, an FSAL that does not answer, no FSAL connected (a Ganesha restart: its clients keep their opens and reclaim them), or a Ganesha instance within its reclaim window holds the commit. SMB writes wait too while Ganesha is down (the daemon cannot tell who wrote a file), and a Ganesha in a crash loop shows only as those waits' warnings. Such a commit is looked at again at most every 5 minutes and never given up, and a wait longer than about 18 minutes is logged as a warning, so an open that never closes shows. The client's CLOSE brings a new close-write.
- **Size and mtime.** Without an NFSv4 open, a file the detector saw go quiet is committed only once its size and mtime are still what the detector saw and have stayed so for a further quiet period, however its last close came. That includes an NFSv3 writer, whose fd Ganesha closes after each request (`Close_Fast`), and a close by Ganesha itself. The check is kept per inode, so a later event without the detector's view (a sweep, a replay at start, the outbox's re-check) waits for one that is under way.

**A write no one watched waits too** (#80 review). Some writes reach the commit with neither the detector's view nor a sweep's checks: the upload outbox committing a file again (an op that didn't commit, or whose finish kept failing), and `commit_modified`, after a repair, a remote change or a conflict. These used to be committed at once, on every share, so a file still being written then got a version part-way. Now the first look starts the size-and-mtime wait, on the daemon's own clock: the file is committed once its size and mtime have held for its share's period since, never from its mtime alone, which a writer can set to any time (an SMB SetInfo, `touch -d`) and then pause (#82 review). Like a sweep, it also waits while anyone has the file open for write (a read lease can't be taken), and no NFSv4 client may hold it open. Such a write is never dropped from the retries: it is looked at again at most every 5 minutes until it commits, since nothing else may bring it back. Only a swept file is given up after about 18 minutes, with a warning, because the next sweep finds it again.

**The NFSv3 limit.** NFSv3 has no open state, so this is a heuristic: a v3 writer that pauses for longer than twice the share's quiet period and then goes on still produces a version of the file as it was in the pause. The finished file becomes the next version, but on a WORM share the partial one is kept, immutable, until its retention ends. Shares with large NFSv3 copies that stall should use NFSv4, or a longer period. An SMB client that pauses with the file still open is the same: `OPEN_STATE_QUERY` only sees NFSv4 opens, and the daemon doesn't look for smbd's own open fd on a file the detector reported.

**Each share's quiet period (decision 67, `crate::quiet`).** The share's `quiet_s` in the catalog (migration 0050), set by its Owners or an ArchiveAdmin, reaches the edge in `share.upsert`. Null means its default: `[commit] worm_quiet_s` (60 s) on a WORM share, `[commit] quiet_s` (10 s, today's period) on any other. So an existing WORM share moves to 60 s when its edge is upgraded, and every other share stays where it was; the migration writes nothing.

- **Every write on the share waits it,** whichever protocol wrote the file, because the edge can't tell (Ganesha and smbd write the same xfs tree). It governs the detector's wait, the further size-and-mtime wait, and the namespace sweep, so a file starts uploading about twice the period after its last write, a little more because the further wait is looked at with a backoff (2, 4, 8 … s).
- **The range is 10 to 600 s,** in the service, the catalog and the edge, which brings a stray value into range rather than using it. 10 s is today's period: anything lower commits a partial version on a pause of under 20 s, which an ordinary writeback stall can reach. 600 s is the ceiling because the further wait gives up after about 18 minutes of looks (`carry.rs`, tested against this bound), and because the detector holds one open file for each file it waits on: at 2,000 new files a second, 600 s would pass `LimitNOFILE` (1,048,576), where 60 s stays at about 120,000.
- **A change needs no restart.** The daemon reads its share rows into a table every 5 s (`daemon::quiet_loop`), and at start before anything looks at a file.
- **Never shorter by mistake** (#80 review). A file is due when its period is over, so an error that shortened periods would commit WORM files mid-write. A read of the replica that fails, a share row that doesn't parse, or a share root whose path can't be read keeps the table as it was, and its generation, so no waiting file is given a new period (`QuietPeriods::refresh`). Before the first good read, and for a path no share resolves, the period is the longest a share can have (600 s), so a share set to 300 s never gets less. A read that keeps failing is counted from its first failure and sent in the heartbeat as `quiet_stale_s`; from 300 s the Edges page shows the edge needs attention and the `edge-quiet-periods-stale` alert fires.
- **The edge reports its defaults** in the heartbeat (`quiet_default_s`, `quiet_worm_default_s`), so the portal shows the default the edge really applies, and says when an edge doesn't support per-share periods yet (`quiet_supported`). That says nothing about whether the edge has a share's latest value, which reaches it through the feed within seconds.
- **A file already being waited on takes the new period at once, measured from its last write.** A shorter period makes it finish as soon as it has been quiet that long, possibly at the next tick; a longer one makes it wait on. In the further wait the same holds, at its next look. A file that has been committed is not affected.
- **A file deleted while it waits is let go within seconds.** The detector holds each waiting file open, and an unlinked file's blocks stay allocated while anyone has it open. So every 2 s it looks at its waiting files (at most 20,000 per tick) and drops any that were deleted. Before this, a deleted file kept its space until its period ended: up to 600 s, which kept a volume full after a 62 GiB file was removed (the 2026-11-08 regression's soak).
- **A file directly under the data root is not waited on.** It is in no share and is never committed, so the detector never holds it open.
- **A file moved while it waits** into a share with a longer period waits that period again, from when the move is noticed. One moved to a shorter period finishes on its old one.
- **Turning WORM on over a short period asks first** (#80 re-check): a share set below 60 s before it is WORM needs `quiet_s_confirmed` in the request that turns WORM on, and `share.worm.confirm` records it.
- **A share's default follows WORM.** A share with no value moves to 60 s when WORM goes on, and back to today's period if WORM is cancelled in its test window. An explicit value stands either way.

A deferred commit is tried again with backoff, which a new close restarts; its close stays in the pending log meanwhile.

The detector's close is only in the daemon's memory while it waits out the quiet period, so a restart in that window used to lose the write (WP3's soak, M7). Three things now cover it (`namespace-sync.md`, "Files no close was seen for"): the detector's group is kept in the systemd fd store, so closes queued while the daemon is down are read by the next instance; a durable log of the closes it has read (`<replica dir>/pending-closes`) is replayed at start until each commit has an outcome; and every reconciliation pass, the one at start included, hands a regular file the replica doesn't know (or knows only from a commit cut short after its `create`) to the commit pipeline once it is quiet and open for write nowhere. The commit checks again that it is unmarked, not yet committed and not held in the upload outbox, so nothing is committed twice and the edge's own files never are.

How the notification group's events become `rename`, `unlink` and `setattr` (owner) ops is in `namespace-sync.md`: an event only marks its directory dirty, and a pass that compares the directory with the replica, by inode and generation, decides the op. A mode change goes to the ACL scan, which sends it with the ACL.

Each op carries an `op_id` (edge id + monotonic counter) and is sent in batches (`POST /v0/edges/{id}/changes`), except `commit`, which goes through `POST …/uploads` with its `op_id` because it needs a write SAS back. The service is idempotent on `(edge_id, op_id)` (`catalog.file_version.edge_op_id`). Before the service assigns a `file_id`, a node is identified by `(fsid, ino, ino_gen)`.

**Commit clones (decision 31).** At `commit`, the daemon reflinks the live file into `.omni/commit/` and uploads from the clone. The clone is deleted only after the service confirms the version stored and verified. An overwrite or delete of the live file after commit therefore can never lose a committed version, on any share. Details in R1 §9.

## 6. Who is authoritative for what

| Fact | Authority | Why |
|---|---|---|
| A user's create, write, rename, delete, chmod, ACL edit on the edge | **Edge**, until acknowledged | It happened here; the service learns from the outbox |
| Content of a version not yet uploaded | **Edge** (commit clone) | Only copy |
| Which bytes are local (marker) | **Edge** | Only the edge can know |
| Object locations, tiers, archive and restore state | **Service** | Backends and the change feed are service-side |
| Location flips during migration (`azure (verified)`) | **Service** | Verifier (decision 24) |
| Stub and tier instructions, including manual portal tier changes (decision 29) | **Service** decides; edge executes if safe | Policy engine |
| Objects appearing, changing or disappearing at an S3 source | **Service** (S3 journal / inventory) | Source of truth until cutover |
| Share config, policies, rewind, WORM, groups | **Service** | Portal |
| `file_id`, `version_id` | **Service** assigns | Catalog identity |

## 7. Conflicts

| Case | Rule |
|---|---|
| Feed updates a file the edge has **uncommitted or unacknowledged** local changes to | Keep the local file. Do not apply. The outbox op reaches the service, which records the edge version as newer (decision 14: new writes win and go to the destination) and sends a `conflict` event for the other side. Both versions remain in history (rewind) |
| Feed creates a name that a **local unmanaged file** already holds | Keep the local file; report the conflict; the service decides the final name. The edge never renames or deletes user data on its own |
| Feed deletes (tombstones) a file with local unsent changes | Keep it; report |
| Feed changes a placeholder's **content** version (S3 object rewritten at the source) and the old version was partly filled | Narrow the marker to nothing resident by the stub handshake (R1 §5.1), punch, then set the new size and content version. Never let old filled bytes be read as the new version. A metadata-only change (rename, chmod) keeps the content version and touches no bytes |
| Service sends `conflict` for an edge op | Mark the outbox op `conflict`; follow the service's resolution in the feed; alert |
| `stub.instruct` for a version that is not the local content | Skip and report `stub.skipped` with the reason |

## 8. When the service is unreachable

| Activity | Behaviour |
|---|---|
| LOOKUP, GETATTR, READDIR, ACLs | **Unaffected**: served from xfs |
| Reads of resident data and resident prefixes | **Unaffected** |
| Writes, creates, renames, deletes | Continue. The outbox and commit clones grow; uploads queue. Alarm on landing-zone and outbox thresholds |
| Reads of `ONLINE` remote ranges | Continue only while the daemon holds a valid credential: the Arc managed identity (Azure) or IAM Roles Anywhere (S3) work without the service. Service-minted SAS tokens last 60 minutes and may be cached until `cache_until` (expiry minus 5 minutes), so outages shorter than that don't stop `ONLINE` reads of files already opened. Otherwise as below |
| Reads that need a restore, or a fetch with no credential | Fail closed per share policy: failfast `EIO`; wait `DELAY` (the reader blocks until the service returns). The request is queued with its requester, so the recall, note file and Teams card follow when the service returns |
| Rewind directories not yet materialised | Cannot be populated: NFS `DELAY`; SMB open denied. Materialised ones keep working |
| Feed | Paused; the namespace may lag (new S3 objects not visible). The staleness is a metric and shows in the appliance status |

The edge never starts a restore on its own: restores are service-owned (decision 15).

## 9. Rewind and the replica (decision 30)

The replica holds the **current** namespace, plus only those versions the edge is uploading or has placed in a rewind tree. It does **not** replicate history: at 10⁸ files with dozens of versions each that would be terabytes, for a feature used occasionally.

History is fetched on demand: when a rewind directory is first opened, the daemon asks the service for that directory's entries as of the label (`catalog.file_version` with its `(parent_id, committed_at)` index serves exactly this), creates read-only placeholders, and records the directory in `snap_dir`. Materialised rewind directories are immutable, so they need no feed updates; they are only evicted when idle or when the label passes the share's depth.

Each rewind placeholder's marker carries the **content** version of the entry as of the label. Recalls are keyed by `(file_id, content version)`, one active recall per key, so opening an old version never joins the current version's recall (or another old version's), and a fill writes only into placeholders whose marker names the same content version. Two rewind labels that resolve to the same content version share one recall and may share the filled bytes by reflink.

## 10. Size estimates at 10⁸ objects

| Item | Size |
|---|---|
| SQLite replica | ~27 GB (268 B/row measured) plus WAL; budget 40 GB |
| xfs placeholders | ~110 GB (~1.07 KB each measured, R1 §11) |
| Bulk snapshot download | 5–8 GB compressed (estimate) |
| Feed volume | proportional to change rate; ~200 B per event |
| FSAL memory | marker cache ≤ 100,000 entries, pending table ≤ 10⁶ entries; under 100 MB |

Plan the appliance metadata budget at **~1.5 KB per object** on SSD.

## 11. Interaction with R1

Because R1 puts the namespace in xfs, the replica's job shrinks from "answer every namespace call fast" to "remember what xfs can't hold, and drive xfs from the feed". If R1 had chosen virtual entries ((a) or (b)), the replica would be on the hot path of every LOOKUP and READDIR, would need to be readable from C without blocking, and the preferred store would become LMDB. The cost of R1's choice is the materialisation step (bootstrap time, ~1.1 KB per object) described above.

## 12. Requirements on the control API and catalog

Sent to m0-contracts on 2026-10-05; `docs/contracts/control-api.md` now covers all of them. Differences adopted here: the feed cursor is an opaque string; `commit` goes through `POST …/uploads`; directory tombstones always follow their children's, and unlinking a non-empty directory is rejected (`directory_not_empty`), with no recursive delete; SAS lifetime is 60 minutes. Items already present in the catalog migrations (`0002_catalog_core.sql`, `0003_rules_feed_audit.sql`) are marked **(in schema)**.

1. **Hierarchical identity.** Files and directories are rows keyed by `(parent_id, name)`; a directory rename is one event on the directory's row **(in schema)**. Feed events carry `file_id`, `parent_id`, `name`, `kind`; a path, if present, is only a hint.
2. **`file_id` and `version_id` are bigints, never reused (in schema).** Every content version has its own location rows with object key and backend version id **(in schema)**. The object key must never be derived from the current path, because renames are metadata-only.
3. **The `file` event row as served** must include: `dataset_id`, `size`, `mtime_ns`, `uid`, `gid`, `mode`, `acl_id`, `version_id` = the content version (`file.content_version_id`; `current_version_id` stays internal to the service), `restore_state` and `restore_expires_at` (kept current by a trigger), primary location (`backend_id`, `container`, `object_key`, `backend_version_id`, `logical_tier`, `storage_class`, archived or not, restore state and expiry), `crc64nvme`, `stub_version_id` and `stub_resident_bytes`, `entity_version`, `deleted_at`. For S3-sourced files the service fills `uid`, `gid`, `mode` and `acl_id` from the source registration (decision 28); the edge never invents them.
4. **ACL events** carry owner and group SIDs, and access and default entries as `(tag, SID, perms)`. The edge maps SIDs to ids through winbind.
5. **Bulk snapshot export** for large datasets: compressed parts in blob, returned as SAS URLs plus the `as_of` cursor. Paged JSON (`GET …/snapshot`) stays for small ones.
6. **Edge changes endpoint**: batched, idempotent on `(edge_id, op_id)` **(in schema: `file_version.edge_op_id`)**; nodes without a `file_id` identified by `(fsid, ino, ino_gen)`; the response returns assigned `file_id` and `version_id`.
7. **Echo suppression**: events caused by an edge carry `origin_edge_id` and `origin_op_id` **(in schema)**. **Conflicts** are explicit events with a reason **(in schema: `op = 'conflict'`)**.
8. **Stub instructions** name `(file_id, version_id)`, where `version_id` is the **content** version (`file.content_version_id`), and the resident size. The edge executes them by the stub handshake in R1 §5.1 **(in schema: `stub_version_id`, `stub_resident_bytes`)**. The edge reports `stubbed` or `skipped` with a reason.
9. **Recall state** events per `(file_id, version_id)` (content version): state, ETA, requester, error. **One active recall per `(file_id, version_id)`:** a read of an old version (rewind) starts or joins only that version's recall, never another version's. State names and their FSAL mapping: `docs/contracts/fsal-socket.md` §5.5.
10. **Rewind**: the share row carries rewind granularity and depth; an endpoint returns a directory's entries **as of a time**, with each entry's version, size, times, ACL id and location (the `file_version_as_of` index serves it); SAS minting accepts a specific `(file_id, version_id)`.
11. **WORM**: the share row carries WORM on/off and retention; the service rejects nothing the edge commits on a WORM share, it only records versions **(in schema: `retention_until`)**.
12. **Digest endpoint** for reconciliation: per dataset (and per directory on demand), live row count and an order-independent digest of `(file_id, entity_version)`.
13. **SAS lifetime**: state how long a minted SAS stays valid, so the edge can keep serving `ONLINE` ranges through short service outages.
14. **Feed retention** of at least 7 days, with `reset_required` beyond it (agreed).
15. **Read reporting for `clock = last_read`.** NFS and SMB reads of resident files never reach the service, so the edge must report last-read times. Proposal: the daemon keeps `last_read_at` per file (from the FSAL's opens and the notification group's `FAN_ACCESS`, coalesced to one update per file per hour) and sends them in batches. Now specified as `POST /v0/edges/{edge_id}/reads` (control API §4.7): up to 10,000 `{file_id, last_read_at}` per call, at most one entry per file per hour, held while the service is down; the service keeps the later time and emits no feed event.

## 13. Decided here, and open

**Proposed for decision:** SQLite in WAL mode, owned by the daemon only; the FSAL reads markers and its pending table, never the replica; feed apply with `entity_version` guards, idempotent filesystem ops and an `fs_pending` redo flag; bulk snapshot export; nightly and post-crash reconciliation; commit clones for every versioned share; the authority and conflict tables above.

**Open:** the exact export format (NDJSON vs Parquet); whether rewind trees may be pre-materialised for the most recent label to hide first-access latency.

**TEST:** SQLite at 10⁸ rows on the dev VM (load, size, lookups); bootstrap materialisation time on a real disk; notification-group survival across daemon restarts; reconciliation walk time at 10⁸ inodes.
