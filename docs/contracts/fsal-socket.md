# Contract: FSAL ⇄ daemon socket protocol, v0

**Status: v0.1 (2026-10-05), implemented by `FSAL_OMNI` in M1** (changes from the M0 draft are marked by the M1 notes in sections 4.1, 5.5, 5.6, 6.1, 6.3, 7 and 8). **v0.2 (M5, 2026-10-21; port of line B's chunk maps):** `CHUNK_MAP` is used, behind feature bit 1 and only at minor 2, additively on top of v0.1 and `POPULATE_DIR` (which stays a v0.1 feature on this line). Every v0.2 change is marked "M5"; `proto_major` stays 0 (§9). Between `FSAL_OMNI` (C, LGPL-3, inside `ganesha.nfsd`) and `omnifile-edge` (Rust). It follows from `docs/design/fsal-namespace.md` (R1: placeholders on xfs, FSAL stacked over VFS) and `docs/design/edge-metadata.md` (R2: the daemon owns the replica; the FSAL reads only markers). It replaces the spike's one-JSON-line-per-connection exchange (`spikes/filer/fsal/FSAL_OMNI/omni_guard.c`, `fake_daemon.py`).

Ganesha citations are to V15.6, paths relative to `src/`. **TEST** marks behaviour a prototype must prove.

## 1. Principles

1. **No Ganesha worker thread ever waits on the daemon.** Workers only do non-blocking sends and in-memory lookups. Replies and pushes are handled by the FSAL's own threads. (Spike finding: a hung daemon cut reads on an unrelated export from ~18,000 to 768 in 8 s.)
2. **The marker xattr is the only proof that bytes are local.** Nothing the daemon says can make the FSAL read a range its marker does not cover. A daemon reply can only make the FSAL stricter, or tell it to re-read the marker.
3. **Fail closed on anything unexpected:** no connection, a full send buffer, a timeout, an unknown message, a bad length or a bad marker. "Closed" means the share's answer for non-local data: `EIO` (failfast) or `DELAY` (wait, waitquick).
4. **The daemon is authoritative for recall state; the FSAL caches it** in a pending table and answers NFS retries from memory.
5. **Identity travels with every request** that can start a recall, so the Teams card reaches the right person.

## 2. Transport

| Item | Value |
|---|---|
| Socket | `AF_UNIX`, **`SOCK_SEQPACKET`**, at `/run/omnifile/fsal.sock`, mode `0600`, owner root |
| Why SEQPACKET | Connection-oriented and reliable like a stream, but keeps message boundaries, so there is no framing parser and no partial-message state. A peer's death is seen as EOF or `ECONNRESET` at once |
| Peer check | Both sides read `SO_PEERCRED` and require uid 0. The daemon also checks the peer's executable is `ganesha.nfsd`, through `/proc/<pid>/exe` |
| Connections | **One per Ganesha process**, opened by `FSAL_OMNI` at module init, shared by all OMNI exports |
| Max message | **4,096 bytes.** A sender never builds a larger one: strings are truncated and flagged (§4) |
| Send buffer | FSAL sets `SO_SNDBUF` to 1 MiB (about 4,000 requests in flight) |
| Sends from workers | `send(…, MSG_DONTWAIT \| MSG_NOSIGNAL)`. `EAGAIN` or any error: no retry in the worker; fail closed for that operation and mark the connection congested |
| Byte order | Little-endian. All fixed fields are naturally aligned. Reserved fields are sent as zero and ignored on receipt |

**FSAL threads** (created at module init, as `FSAL_CEPH` runs its own callback threads):
- **one socket thread:** connects, reads every message, applies pushes to the pending table, runs timers (parked-read expiry, request expiry, ping);
- **a completion pool, 4 threads by default** (`OMNI { Completion_Threads }`): calls `done_cb` for parked reads, so the socket thread never runs Ganesha callbacks. A read that became local is resumed onto a worker, which does the disk read (§6.3);
- **one invalidation thread:** applies `INVALIDATE`, waiting for pinned local I/O to drain, and sends `INVALIDATE_ACK` (§5.5).

## 3. Message header

Every message starts with this 16-byte header.

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 2 | `type` | §5. Bit 15 set = reply |
| 2 | 2 | `flags` | bit 0 `TRUNCATED` (a string was cut); others zero |
| 4 | 4 | `length` | Total bytes including the header. Must equal the received record size, or the receiver closes the connection |
| 8 | 8 | `req_id` | Non-zero for requests and echoed in their reply. Zero for notifications. FSAL-originated ids have bit 63 clear; daemon-originated ids have bit 63 set |

**Variable part.** After a message's fixed fields come its strings, in the order the message lists them, each as `u16 len` then `len` bytes (no terminator, no padding). Paths are raw bytes, not necessarily UTF-8.

**Extensibility within v0.** A receiver accepts a message whose fixed part is **longer** than it knows and skips the extra bytes. A fixed part **shorter** than its type's size in section 5 is malformed: the receiver sends `ERROR` 3 and closes. A newer sender appends fields only at the end of the fixed part, and a feature bit (§5.1) announces it. Strings always follow the fixed part, whose size is computed from the version negotiated in `HELLO`.

## 4. Shared structures

### 4.1 `obj_ref` (32 bytes)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 8 | `fsid` | `statfs` `f_fsid` of the xfs volume, as u64 |
| 8 | 8 | `ino` | inode number |
| 16 | 4 | `ino_gen` | inode generation, from the VFS handle |
| 20 | 4 | `export_id` | Ganesha `Export_Id` (0 when not known, e.g. from the daemon) |
| 24 | 8 | `file_id` | from the marker; 0 for unmanaged files |

The daemon keys everything on `(fsid, ino, ino_gen)` or `file_id`. A path is only a hint: it goes stale after rename, and a handle rebuilt from the wire has none (spike B).

Both sides must compute the same keys. The pending table and `RECALL_STATE` match on them, and `INVALIDATE` finds cached markers by `(fsid, ino)`. Agreed with m1-edge-core on 2026-10-05; `t_b3_identity.sh` checks it:
- `fsid` is `statfs(2)` `f_fsid` with its two 32-bit words little-endian, so the u64 is `(u32)__val[0] | (u64)(u32)__val[1] << 32`. This is what glibc's `statvfs` `f_fsid` and Python's `os.statvfs().f_fsid` give on 64-bit Linux.
- `ino` is `st_ino`.
- `ino_gen` is the inode's `i_generation`. The daemon reads it with `ioctl(FS_IOC_GETVERSION)`. The FSAL decodes it from the kernel file handle: it is bytes 4..8 for `FILEID_INO32_GEN` (types 1 and 2), and bytes 8..12 for xfs's 64-bit-inode types `0x81` and `0x82`. It is 0 for other layouts.
- An `INVALIDATE` whose `fsid` the FSAL does not recognise drops **every** cached marker, so a key mismatch never leaves a stale marker cached. Before acking it, the FSAL also waits for the pins of **every** live inode to drain (§5.5), because it cannot tell which inode was meant. It logs a warning, since the two sides disagree on a key (M1 review S4).

### 4.2 `identity` (48 bytes)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | `uid` | `op_ctx->creds.caller_uid` (`include/fsal_api.h:488`); `0xFFFFFFFF` = unknown |
| 4 | 4 | `gid` | `op_ctx->creds.caller_gid`; server-resolved when `Manage_Gids = true` (decision 28) |
| 8 | 1 | `origin` | 0 `REQUEST` (this request's own op context); 1 `OPEN_STATEID` (captured at OPEN, §6.4); 2 `LAST_OPENER` (best guess, §6.4); 3 `UNKNOWN` |
| 9 | 1 | `nfs_vers` | `op_ctx->nfs_vers` (`fsal_api.h:497`) |
| 10 | 1 | `nfs_minor` | `op_ctx->nfs_minorvers` (`:498`) |
| 11 | 1 | `sec` | 0 unknown; 1 `sys`; 2 `krb5`; 3 `krb5i`; 4 `krb5p`. From the RPC credential flavour, `req->rq_msg.cb_cred.oa_flavor` (`support/nfs_creds.c:207`), reached through `op_ctx->nfs_reqdata` (`fsal_api.h:516`); the krb5 service level from the request's RPCSEC_GSS credential, `((struct rpc_gss_cred *)req->rq_msg.rq_cred_body)->gc_svc` (NONE → 2, INTEGRITY → 3, PRIVACY → 4), which is how libntirpc's `svc_auth_gss.c` reads it. Implemented in M1 after m2-identity's finding. **Verified** on a krb5p mount (m2-identity's AD matrix, 2026-10-05: `sec` 4 and the principal arrived); krb5i is **VERIFY** (not yet mounted separately) |
| 12 | 1 | `addr_family` | 0 none; 4 IPv4; 6 IPv6. An IPv4-mapped IPv6 address (`::ffff:a.b.c.d`, seen in spike B3) is sent as family 4 |
| 13 | 3 | reserved | |
| 16 | 16 | `addr` | IPv4 in the first 4 bytes, or IPv6; from `op_ctx->caller_addr` (`:494`) |
| 32 | 2 | `port` | client source port |
| 34 | 2 | reserved | |
| 36 | 4 | reserved | |
| 40 | 8 | `clientid` | NFSv4 client id (`op_ctx->clientid`, `:495`); 0 for v3 |

Strings that follow an `identity`, when the message lists them: `principal` (the Kerberos principal, empty with `sec=sys`). It is the GSS context's client name, `SVCAUTH_PRIVATE(req->rq_auth)->cname`, which Ganesha also maps to the uid (`support/nfs_creds.c`), cut to 255 bytes. It is captured with the rest of the identity, so COPY's `OPEN_STATEID` and `LAST_OPENER` identities carry it too.

With `sec=sys` the uid is only claimed by the client host. The daemon records `claimed` or `proven` from `sec` (`docs/design/access-control.md` §3).

## 5. Messages

| Type | Name | Direction | Kind |
|---|---|---|---|
| `0x0001` | `HELLO` | FSAL → daemon | request |
| `0x8001` | `HELLO_ACK` | daemon → FSAL | reply |
| `0x0010` | `READ_INTENT` | FSAL → daemon | request |
| `0x8010` | `READ_DECISION` | daemon → FSAL | reply |
| `0x0011` | `POPULATE_DIR` | FSAL → daemon | request |
| `0x8011` | `POPULATE_RESULT` | daemon → FSAL | reply |
| `0x0020` | `WRITE_CLOSED` | FSAL → daemon | notification |
| `0x0021` | `NS_HINT` | FSAL → daemon | notification |
| `0x0022` | `BAD_MARKER` | FSAL → daemon | notification |
| `0x0030` | `PING` | either | request |
| `0x8030` | `PONG` | either | reply |
| `0x0040` | `RECALL_STATE` | daemon → FSAL | notification |
| `0x0041` | `INVALIDATE` | daemon → FSAL | request (reply only if `ACK_REQUIRED`) |
| `0x8041` | `INVALIDATE_ACK` | FSAL → daemon | reply |
| `0x0042` | `RESYNC_BEGIN` | daemon → FSAL | notification |
| `0x0043` | `RESYNC_END` | daemon → FSAL | notification |
| `0x0044` | `OPEN_STATE_QUERY` | daemon → FSAL | request (with `OPEN_STATE`) |
| `0x8044` | `OPEN_STATE` | FSAL → daemon | reply |
| `0x7FFF` | `ERROR` | either | notification, then the sender closes |

A receiver ignores (and logs, rate-limited) an unknown **notification** type. An unknown **request** type gets `ERROR` code 2 and the connection stays up. The FSAL treats a missing reply as a timeout (§7).

### 5.1 `HELLO` / `HELLO_ACK`

The first message on a connection. Nothing else may be sent until `HELLO_ACK` arrives; until then the FSAL is in its disconnected state (§7).

`HELLO` (fixed part 32 bytes after the header):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | `magic` | `0x53464D4F` (bytes `O M F S`) |
| 4 | 2 | `proto_major` | 0 |
| 6 | 2 | `proto_minor` | 1 (M1); 2 (M5: `CHUNK_MAP`) |
| 8 | 4 | `features` | bit 0 `PARK`; bit 1 `CHUNK_MAP` (only with a negotiated minor of 2 or more: either side drops it below that); bit 2 `POPULATE_DIR`; bit 3 `NS_HINT`; bit 4 `WRITE_CLOSED`; bit 5 `OPEN_STATE` |
| 12 | 4 | `pid` | Ganesha's pid |
| 16 | 8 | `instance` | random per Ganesha process start |
| 24 | 8 | reserved | |

Strings: `ganesha_version` (Ganesha's `GANESHA_VERSION`, e.g. `15.6`), `fsal_build` (git commit of `fsal/`, or `unknown`).

`HELLO_ACK` (fixed part 32 bytes):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 2 | `proto_major` | must equal the FSAL's, or the daemon sends `ERROR` 1 and closes |
| 2 | 2 | `proto_minor` | min(FSAL's, daemon's) |
| 4 | 4 | `features` | the bits both sides support |
| 8 | 8 | `instance` | random per daemon start. A new value means "daemon restarted": the FSAL clears its pending table |
| 16 | 4 | `max_inflight` | the FSAL keeps no more requests outstanding than this (default 4,096) |
| 20 | 4 | `park_ms_max` | upper bound on parking (default 10,000) |
| 24 | 4 | `answer_ttl_ms` | how long a `DELAY`/`EIO` answer may be reused without asking again (default 1,000) |
| 28 | 4 | reserved | |

After `HELLO_ACK` the daemon sends `RESYNC_BEGIN`, one `RECALL_STATE` for each active recall, then `RESYNC_END`. Pending-table entries that were not refreshed during the resync are dropped. Entries with parked reads are kept: the reads time out at `park_ms` anyway.

### 5.2 `READ_INTENT` / `READ_DECISION`

Sent when an operation needs bytes the marker says are not local and the pending table has no valid answer (§6.1). Also used, with an `op` other than `READ`, for writes and size changes that need a full fill first (R1 §6).

`READ_INTENT` (fixed part 136 bytes):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 32 | `obj` | `obj_ref` |
| 32 | 8 | `version_id` | from the marker: the **content** version (`catalog.file.content_version_id`), never the file's current version |
| 40 | 8 | `offset` | |
| 48 | 8 | `length` | |
| 56 | 8 | `size` | file size from the marker |
| 64 | 8 | `resident_bytes` | from the marker |
| 72 | 1 | `op` | 1 `READ`; 2 `READ_PLUS`; 3 `COPY_SRC`; 4 `WRITE` (needs fill); 5 `SETATTR_SIZE` (needs fill); 6 `FALLOCATE` (needs fill) |
| 73 | 1 | `policy` | the export's: 1 `wait`; 2 `failfast`; 3 `waitquick` (mapping from the catalog's words in §10) |
| 74 | 2 | `marker_flags` | copied from the marker (`ONLINE`, `ARCHIVED`, …) |
| 76 | 4 | `quick_ms` | `waitquick` threshold |
| 80 | 1 | `parked` | 1 if the FSAL has parked this READ waiting for the answer |
| 81 | 7 | reserved | |
| 88 | 48 | `identity` | |

Strings: `principal`, `path_hint` (at most 1,024 bytes; longer paths are cut from the left and `TRUNCATED` is set).

`READ_DECISION` (fixed part 40 bytes):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | `decision` | 1 `RECHECK`; 2 `DELAY`; 3 `EIO`; 4 `PARK` |
| 1 | 1 | `reason` | 0 none; 1 recall started; 2 recall in progress; 3 fetch in progress; 4 service unreachable; 5 no credential; 6 unknown file; 7 over waiter cap; 8 restore failed; 9 too slow for waitquick; **M5:** 10 unsupported backend ("upgrade the edge", decision 35 F4); 11 source not readable by the edge. `reason` is informational: a receiver treats an unknown value as 0 and still obeys `decision` |
| 2 | 2 | reserved | |
| 4 | 4 | `eta_s` | `0xFFFFFFFF` = unknown. Never a promise above 10 GB (`AGENTS.md`) |
| 8 | 4 | `ttl_ms` | how long the FSAL may reuse this answer for this file (0 = use `answer_ttl_ms`) |
| 12 | 4 | reserved | |
| 16 | 8 | `recall_id` | 0 if none |
| 24 | 8 | `resident_bytes` | durable watermark now, as a hint; the marker still decides |
| 32 | 8 | reserved | |

Meanings for the FSAL:
- **`RECHECK`:** the daemon believes the range is now local. Re-read the marker; if it covers the range, do the read, otherwise fail closed. Never treated as permission by itself.
- **`DELAY`:** answer `ERR_FSAL_DELAY` (`NFS4ERR_DELAY` / `NFS3ERR_JUKEBOX`) now and for `ttl_ms`.
- **`EIO`:** answer `ERR_FSAL_IO` now and for `ttl_ms`.
- **`PARK`:** keep (or start) parking this read until `RECALL_STATE` shows the range local, or until `park_ms`; only valid for `ONLINE` ranges and only if `PARK` was negotiated.

The daemon decides `waitquick` (it has the ETA): it answers `DELAY` when the ETA is under `quick_ms`, otherwise `EIO`.

### 5.3 `POPULATE_DIR` / `POPULATE_RESULT`

For rewind directories marked `DIR_UNPOPULATED` (R1 §7).

`POPULATE_DIR` (fixed part 80 bytes): `obj` (32, the directory), `identity` (48). Strings: `principal`, `path_hint`.

`POPULATE_RESULT` (fixed part 8 bytes): `u8 result` (1 `DONE`, the FSAL re-reads the marker; 2 `IN_PROGRESS`; 3 `EIO`), 3 reserved bytes, `u32 eta_ms`.

The FSAL answers the LOOKUP or READDIR with `DELAY` until the directory's marker no longer has `DIR_UNPOPULATED`. It never waits for `POPULATE_RESULT` in the worker.

The daemon finds the directory by `obj` (inode and generation, within `.omni/snap/`); `path_hint` is not trusted for this. It answers `EIO` for a directory that isn't a rewind directory, and `IN_PROGRESS` while another request populates the same one. On any failure the marker keeps `DIR_UNPOPULATED`, so the directory is never shown half-filled.

### 5.4 Notifications from the FSAL

None of these is needed for correctness: the daemon's fanotify notification group and reconciliation are the source of truth (R1 §8). They add who did something over NFS, and speed up commits.

`WRITE_CLOSED` (fixed part 88 bytes): `obj` (32), `u64 size_after` (from VFS after close; 0 if unknown), `identity` (48). Sent from `close2` of a state that was open for write: NFSv4 CLOSE, or the last close. It lets the detector start the quiet period at once rather than waiting for Ganesha's cached fd to close.

`NS_HINT` (fixed part 120 bytes): `u8 op` (1 `CREATE`, 2 `UNLINK`, 3 `RENAME`, 4 `SETATTR`, 5 `SETACL`, 6 `LINK`, 7 `MKDIR`, 8 `RMDIR`), `u8 what` (`SETATTR` only: bit 0 `SIZE`, a truncate to 0; bit 1 `OWNER`, the owner or group changed; 0 from an FSAL that predates it, which sent `SETATTR` only for a truncate), 6 reserved, `obj_ref parent` (32), `obj_ref target` (32; zeros if not known), `identity` (48). Strings: `principal`, `name`, `new_name` (rename only). Sent after the sub-FSAL call succeeds, never blocking (dropped while the daemon is away).

What `FSAL_OMNI` sends (port of line B's `NS_CHANGE`, PR #10, onto this message; no new feature bit, since `NS_HINT` is negotiated already and an older daemon logs what it doesn't use): `MKDIR` (parent, the new directory, its name), `UNLINK` / `RMDIR` (parent, the object, its name), `RENAME` (the old parent, the object, the old and the new name; the new parent is not sent, so the daemon matches by the object and the new name), `SETATTR` `OWNER` (the object) and `SETATTR` `SIZE` (§6.1). `CREATE` and `SETACL` are not sent. `LINK` is never sent: the FSAL refuses it (decision 42). The daemon uses these only to say who made a change (`control-api.md` §4.1 `actor`, `namespace-sync.md`); its namespace sync decides what changed from the disk, so a dropped hint costs only an `unknown` actor.

`BAD_MARKER` (fixed part 40 bytes): `obj` (32), `u32 reason` (1 bad magic; 2 bad checksum; 3 unknown layout; 4 size mismatch with the inode; 5 read error), `u32 errno`. The FSAL has already failed the operation closed; this asks the daemon to repair or alert.

### 5.5 Pushes from the daemon

`RECALL_STATE` (fixed part 72 bytes):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 32 | `obj` | |
| 32 | 8 | `version_id` | the **content** version being recalled |
| 40 | 8 | `recall_id` | |
| 48 | 1 | `state` | 1 `QUEUED`; 2 `RESTORING` (rehydrate or S3 restore); 3 `COPYING` (Storage Mover or recall-container copy); 4 `FETCHING` (bytes coming to the edge); 5 `DONE`; 6 `FAILED`; 7 `CANCELLED`; 8 `BLOCKED` (service unreachable) |
| 49 | 1 | `answer` | what the FSAL answers while this state lasts: 2 `DELAY` or 3 `EIO` (already resolved for the share's policy) |
| 50 | 2 | reserved | |
| 52 | 4 | `eta_s` | |
| 56 | 8 | `resident_bytes` | durable prefix watermark; with `CHUNK_MAP`, the FSAL also re-reads the map |
| 64 | 4 | `valid_ms` | entry lifetime without a refresh; the daemon refreshes active recalls before it ends |
| 68 | 4 | reserved | |

On `DONE` or `CANCELLED` the FSAL completes any parked reads and drops the entry. `DONE` resumes them, and the worker re-reads the marker first; `CANCELLED` gives the policy default. On `FAILED` it completes parked reads with `EIO` and keeps the entry, answering `EIO` until `valid_ms` passes, as the mapping table below says. A `RECALL_STATE` whose `answer` is neither 2 nor 3 is treated as 3 (`EIO`), the stricter answer.

**One recall per `(file_id, version_id)`.** The FSAL applies a `RECALL_STATE` only to a pending entry whose `version_id` equals the message's; a message for another version of the same inode is ignored. A rewind placeholder (an old version) therefore never answers from, or parks on, the current version's recall, and the daemon never joins a read of one version to another version's recall.

**State mapping.** The catalog's recall states (`catalog.recall.state`) are what the portal and Teams show. The FSAL needs fewer distinctions, but keeps the Storage Mover step visible for logs. `base.RestoreState` is the backend's view of the cloud restore underneath (`service/src/omnifile_service/backends/base.py`). Agreed with m0-contracts on 2026-10-05.

| Catalog `recall.state` | FSAL `RECALL_STATE.state` | `base.RestoreState` of the restore underneath | Meaning |
|---|---|---|---|
| `requested` | 1 `QUEUED` | `ARCHIVED` (restore not yet started), or `NOT_NEEDED` (online object, fetch queued) | Accepted, waiting for a restore slot or a fetch |
| `rehydrating` | 2 `RESTORING` | `IN_PROGRESS` | Azure rehydrate copy or S3 `RestoreObject` running |
| `copying` | 3 `COPYING` | `COMPLETED`, with a Storage Mover job (or a recall-container copy) running | S3-resident file: the restored copy is moving into Azure (decision 20). Cards show it as part of "rehydrating" |
| `downloading` | 4 `FETCHING` | `COMPLETED` or `NOT_NEEDED` | The edge is filling the placeholder |
| `available` | 5 `DONE` | `COMPLETED` or `NOT_NEEDED` | Filled and durable; the marker covers the bytes |
| `failed` | 6 `FAILED` | `FAILED`, or `EXPIRED` before the copy or fill finished | Error shown to the user; the FSAL answers `EIO` while the entry lasts |
| `cancelled` | 7 `CANCELLED` | any | Cancelled in the portal; the FSAL answers the policy default on the next read |
| `superseded` | 7 `CANCELLED`, or 6 `FAILED` | any | The file was replaced or deleted while it was being recalled (control API §4.4). `CANCELLED` when the inode is still the file (replaced in place): the next read finds the new marker, under another key. `FAILED` when the inode is no longer the file (deleted, or renamed over) and a reader still holds it: its bytes can never come, and a `wait` reader must get `EIO`, not another wait |
| `available`, for a file that is deleted or holds another version, with a reader still waiting | 7 `CANCELLED`, or 6 `FAILED` | `COMPLETED` | A job in the service kept the recall after the file moved on and closed it (control API §4.4). Nothing was filled on the edge, so it is not `DONE`: the two cases are those of `superseded` |
| (none: edge-local) | 8 `BLOCKED` | unknown | The daemon can't reach the service; the request is queued on the edge (`docs/design/edge-metadata.md` §8) |

`EXPIRED` with the copy still needed makes the service request a new restore: the recall goes back to `rehydrating`, not `failed`.

`INVALIDATE` (fixed part 48 bytes): `obj` (32), `u32 what` (bit 0 `MARKER`, bit 1 `ATTRS`, bit 2 `CONTENT`, bit 3 `DIR`), `u32 flags` (bit 0 `ACK_REQUIRED`), `u64 inval_seq`. It is a request: its `req_id` has bit 63 set, and `INVALIDATE_ACK` echoes it. The FSAL drops its cached marker for every generation of that `(fsid, ino)`. If the inode is in its live-handle table, the FSAL also calls the up-call vector's `invalidate` (`include/fsal_up.h:159`) with the matching `FSAL_UP_INVALIDATE_*` flags (`:105-119`). This happens on an FSAL thread, never on a worker.

With `ACK_REQUIRED` the FSAL replies `INVALIDATE_ACK` (fixed part 8 bytes: `u64 inval_seq`) only once two things are true: the cached marker is gone, and **no I/O that passed the check under the old marker is still running**. Every read, write, COPY and `SEEK` that the marker allows holds a per-inode pin from before its marker check until the sub-FSAL call returns. A marker read from disk is cached only if no `INVALIDATE` of that inode began while the read ran (a per-inode invalidation generation), so a reader that saw the old marker can never store it after the `INVALIDATE` dropped the cache, pinned or not (M1 review B3; `t_marker_cache.sh`). `INVALIDATE` waits for those pins to drain, and new I/O on the inode waits behind it. That wait is for local disk I/O only, never for the daemon. Without it, a read checked just before the narrowing could still be reading when the daemon punches (`t_invalidate.sh`). The ack goes only on the connection the `INVALIDATE` arrived on.

`RESYNC_BEGIN` and `RESYNC_END` have **no fixed part**: header only, `req_id` 0.

The daemon uses `ACK_REQUIRED` before every **narrowing** (stubbing, eviction, a content-version change): it writes the new marker, sends `INVALIDATE`, waits up to 1 s for the ack on every live connection, and only then punches holes. If any live connection does not ack, it does not punch, and retries later. **Zero live connections, or any accepted connection not yet past `HELLO_ACK`, is not an ack** (M1 review B2), unless the edge is configured with no Ganesha at all (`fsal.no_ganesha`). The punch also requires the connection epoch to be unchanged from before the narrowed marker was written. A content-version change is fenced like a fill: the daemon pushes `FETCHING` for the old version first, narrows to `PARTIAL` with an acknowledged `INVALIDATE` before it resizes the file, and ends the fence with `CANCELLED` (M1 review S4, S12). Widening (a fill) needs no ack. This is step 4 of the **stub handshake**, `docs/design/fsal-namespace.md` §5.1, which the control API's stub flow cites; the handshake also removes the smb-guard's ignore mark before narrowing.

`OPEN_STATE_QUERY` (fixed part 32 bytes: `obj`) asks whether an NFSv4 client still holds a file open for write. It is a request: its `req_id` has bit 63 set, and `OPEN_STATE` echoes it. Only on a connection that negotiated `OPEN_STATE` (an older FSAL answers an unknown request with `ERROR` 2). The FSAL answers at once from its open-state table, never waiting on I/O or a lock held across a sub-FSAL call. `OPEN_STATE` (fixed part 8 bytes): `u32 write_opens`, the NFSv4 share states (OPENs) open for write on that `(fsid, ino, ino_gen)`; then 4 reserved bytes. A state is open for write from an `open2` with write access, or an OPEN upgrade that adds it, until an `OPEN_DOWNGRADE` without it or its `close2`. An upgrade never drops it: Ganesha calls `reopen2` with only the new OPEN's access and ORs it into the state afterwards (`nfs4_op_open.c`), so a reader that joins a writer of the same open owner leaves the writer counted (`t_open_state.sh`). Ganesha calls `close2` for every share state it frees (CLOSE, lease expiry). NFSv3 has no open state, so a v3 writer counts 0, and so do NFSv4 reads, locks and delegations.

The daemon asks before it commits a file it saw written (`omnifile-edge` `commit.rs`): a large NFSv4 copy can go quiet for longer than the commit quiet period (a writeback stall) while its OPEN is still in place, and a version committed then is a partial one. These hold the commit, which is looked at again at most every 5 minutes and never given up (the close stays in the pending-close log):

- any `write_opens` above 0, from any ready connection;
- a ready connection that does not answer within 1 s, or one still in its handshake;
- **no FSAL connected**, unless the edge runs no Ganesha (`fsal.no_ganesha`), as for the stub handshake's acks (§5.5, `INVALIDATE`). Ganesha restarting is the usual cause, and its NFSv4 clients keep their opens across it: Ganesha's exit closes their fds (so the detector even sees a close), and they reclaim the opens in grace once it is back. SMB writes wait too while Ganesha is down (the daemon cannot tell who wrote a file), and a Ganesha in a crash loop shows only as the daemon's warnings for commits that have waited long;
- **a Ganesha instance that first connected less than `fsal.reclaim_window_s` ago** (default 150 s, Ganesha's 60 s lease plus its 90 s grace period). After a Ganesha restart, or a daemon restart (an instance it has not seen), an answer of 0 may only mean "not reclaimed yet".

An FSAL that did not negotiate `OPEN_STATE` cannot be asked; it holds nothing. Without an NFSv4 open, a file the detector saw go quiet must keep its size and mtime for a further quiet period (`docs/design/edge-metadata.md`, "A quiet file is not always a finished one").

### 5.6 `PING` / `PONG`, `ERROR`

`PING` and `PONG`: fixed part 8 bytes, `u64 mono_ns` echoed. Either side may ping. The receiver answers at once, also during a resync. The FSAL pings when it has received nothing for 1 s, and disconnects after 3 s with nothing received. So the daemon must answer the FSAL's `PING`s promptly; it need not ping itself.

`ERROR`: fixed part 4 bytes, `u32 code` (1 version mismatch; 2 unknown request; 3 malformed; 4 peer not allowed; 5 shutting down), then a string `message`. The sender closes after an `ERROR` with code 1, 3, 4 or 5.

## 6. FSAL behaviour

### 6.1 The read path

For `read2` (`include/fsal_api.h:2676`) on an OMNI handle:

1. **Marker.** Use the inode's cached marker if present. Otherwise read `trusted.omnifile` without opening the file for I/O: `open_by_handle_at(…, O_PATH)`, then `fgetxattr`. Where `f*xattr` refuses an `O_PATH` fd (`EBADF` before Linux 6.x), `getxattr("/proc/self/fd/N")` reaches the same inode. Never `O_RDWR` (spike `chattr +i` bug). The mount fd given to `open_by_handle_at` must be `O_RDONLY`, because an `O_PATH` one gives `EBADF`. `fstat` on the same fd gives `st_size` for the stale-size check. A missing marker means an unmanaged local file: pass through. A bad marker: fail closed and send `BAD_MARKER`. The cache is used only while connected (§7).
2. **Local range.** If `[offset, offset+len)`, clipped to the marker's `size`, is within `resident_bytes`: pass to VFS. M1 does not offer `CHUNK_MAP` and ignores the map, which is stricter. **M5:** with `CHUNK_MAP` negotiated and the marker's `CHUNK_MAP` flag set, a range is also local if every chunk it touches has its bit set in `trusted.omnifile.map` (§8). The map is read together with the marker, by the same `O_PATH` route, and cached with it (maps up to 8 KiB; larger ones are read per operation); it is dropped with it. A map whose length isn't `ceil(ceil(size / 2^chunk_shift) / 8)` bytes, with a bit set past the last chunk, over 64 KiB, or missing while the flag is set is invalid: fail closed and send `BAD_MARKER` reason 3. Without `CHUNK_MAP` negotiated, a marker with the flag is served by its prefix only, which is stricter and safe. If a **cached** marker does not cover the range, re-read it from disk once before going on: a widening needs no `INVALIDATE`. A read that starts at or after a cached `PARTIAL` marker's `size` is also re-read from disk before it is treated as an end-of-file read. While the daemon replaces a file's content, it resizes the file before the new marker's `INVALIDATE` arrives, and that `INVALIDATE` is not acknowledged. The disk read checks the size against the inode (M1 review S12).
3. **Pending table.** Look up `(fsid, ino, ino_gen, version_id)`, with the marker's content version.
   - A valid entry whose `resident_bytes` now covers the range: re-read the marker; if it covers, pass to VFS.
   - A valid entry: answer its `answer`, unless the range is `ONLINE` and parking is allowed, then park (§6.3). If this caller's `(uid, client)` is not among the entry's reported requesters (up to 8 kept), also send a `READ_INTENT`, so every person waiting gets a card.
4. **No valid entry.** If a `READ_INTENT` for this inode was sent less than 100 ms ago, answer the policy default without sending. Otherwise send `READ_INTENT` without blocking and create an entry in state "asked". Answer at once:
   - `ONLINE` range with parking: park;
   - `failfast`: `EIO` (the recall is queued by the daemon when the message arrives);
   - `wait` and `waitquick`: `DELAY`.
5. If the send failed: fail closed.

This bounds daemon traffic to about one request per waiting file per `ttl_ms`, plus one per new requester, instead of one per client retry (the spike's ~20 requests/s per waiting NFSv4 reader). Measured in M1 (`t_b2_read.sh`): one `READ_INTENT` for a whole 3 s wait, on every NFS version.

**Header margin.** The FSAL has no special case for headers. A read inside `[0, resident_bytes)` never contacts the daemon, and the next read does. The margin against client readahead belongs to the stubber, which leaves the resident region at the headers plus 1 MiB (one `rsize`; `docs/BUILD-PLAN.md` M1), rounded up to a whole page or filesystem block (whichever is larger, never past `size`): the smb-guard's fanotify ranges are page-granular. In M1 (`t_b2_read.sh`, every NFS version), a buffered read of exactly the resident region still sends one `READ_INTENT` at `offset = resident_bytes`: the client's readahead. Reads of 1 MiB short of it send none. The daemon may decline to recall for a lone `READ_INTENT` that starts exactly at `resident_bytes` (spike B, production notes).

**Truncate to 0.** A size change to 0 (`setattr2`, or `open2` with `FSAL_O_TRUNC`) is allowed on a stub. Once the fill fence below lets it through, the FSAL removes `trusted.omnifile` itself **before** it calls the sub-FSAL truncate. After the truncate succeeds, it removes `trusted.omnifile.map` and sends `NS_HINT` `SETATTR`. If the truncate fails, it puts the marker back with `XATTR_CREATE`. If that also fails, it logs CRIT.

The order matters because of the smb-guard's fanotify group on the same filesystem. It sees the VFS truncate as a pre-content event, and for a marked file it waits for a fill, which never comes after a `FAILED` one (M2 integration; `t_trunc_guard.sh`). An unmarked file is allowed.

Between the marker's removal and the truncate, the file looks unmanaged but still has holes. So no other NFS operation on the inode may check its marker in that window. The truncate holds back new pins and waits for the others to drain, as an `INVALIDATE` does. It waits for local I/O only, never for the daemon. Truncates of one inode are serialised.

If the marker cannot be removed (an immutable file), the marker stays, and it is removed after the truncate if the truncate succeeds anyway. That was the order before. An empty file has no non-local bytes, so this is safe. It lets `cp new over stub` work over NFS. The daemon must therefore check, before any fill write and before widening, that the marker it is filling still exists with the same `marker_seq` and size, and it cancels the fill otherwise. This is a daemon obligation.

**Fill fence (closes the window between that check and `pwrite`).** Daemon side implemented by edge-core on 2026-10-05. FSAL side implemented on 2026-10-05 and tested in `fsal/tests/t_fill_fence.sh`, on NFSv3 and v4.2, for SETATTR and OPEN-with-truncate. A truncate of a `PARTIAL` file holds a per-inode pin from its fence check until the marker has been removed and the truncate has returned. So the daemon's fence `INVALIDATE` is acked only after any truncate that passed the check has finished. That truncate saw no `FETCHING` entry, because the FSAL applies `RECALL_STATE` before it queues the later `INVALIDATE`. If the marker cannot be read, the truncate always answers `DELAY` and the FSAL sends `BAD_MARKER`. The content version is unknown, so the fence is on `(fsid, ino, ino_gen)` whatever the version (M1 review S11). A `FETCHING` entry fences until `DONE`, `FAILED` or `CANCELLED` replaces it, or a resync drops it, whatever its `valid_ms`. It is never swept by age, and it is never evicted when the table is full (`t_pending.sh`). MDCACHE opens an existing file by handle. On an open-with-truncate by name, the FSAL also looks the name up, to catch a placeholder linked in since.
- *Daemon.* Before a fill's first write, the daemon pushes the inode's `RECALL_STATE` with `state = 4 FETCHING`. On the same connections, it then sends `INVALIDATE` with `what = CONTENT` and `ACK_REQUIRED`. Every connected FSAL must acknowledge, or the fill writes nothing. The daemon records the server's connection epoch, which changes whenever a connection becomes ready (after `HELLO_ACK`) or stops being ready. Before every write and every widen, it re-checks both the epoch and the marker (`marker_seq`, version, size, and the inode's size). It stops the fill if either changed. Widening uses `XATTR_REPLACE`, so a marker the FSAL removed is never recreated. **Reconnect (2026-11, M2 step 10):** a restarted daemon takes over the smb-guard's held reads before Ganesha's FSAL has reconnected. So before fencing, a fill waits up to `fsal.reconnect_wait_ms` (default 30 s; one bound per fill, however often it retries a broken fence) until every running Ganesha (a process whose `/proc/<pid>/exe` is `fsal.required_exe`) has a ready connection and none is half-way through its handshake. Readers keep waiting meanwhile. Zero connections is accepted for a fill only when no Ganesha process runs at all: a Ganesha starting later changes the epoch when it connects, which stops the fill, and its FSAL refuses truncates until its resync has ended (case 2 and 3 below). When which Ganesha runs can't be told, zero connections is never accepted. A connected FSAL that does not acknowledge fails the fence at once, and the bound expiring fails it too. Punches (§5.5) are unchanged. Within the daemon, stubs, fills and content-version replacements of one file are serialised by a per-file lock.
- *FSAL (required).* A truncate to 0 (`setattr2` size 0, or `open2` with `FSAL_O_TRUNC`) of a file whose marker is `PARTIAL` answers `DELAY` instead of truncating in three cases. (1) The pending table holds a `FETCHING` entry for its `(fsid, ino, ino_gen, version_id)`. (2) The FSAL is disconnected. (3) The FSAL is between `HELLO_ACK` and `RESYNC_END`. Otherwise it truncates as above. Because of (2) and (3), a fill whose fence broke by a connection change cannot race a truncate: the FSAL waits for the resync, which carries every running fill's `FETCHING` state.
- SMB and local writers are fenced by the smb-guard in M2. Until then, a local `truncate` racing a fill is detected by the marker check and stops the fill, but one in-flight `pwrite` can still land.

`READ_PLUS` uses the same path: `read2` never returns `NOTSUPP` for a managed file, and `seek2` (`:2716`) reports a partial file as all data (spike B2x). `write2`, `setattr2` size changes, `fallocate` and `copy_file_range` destinations that need a fill use the same path with the matching `op` (R1 §6).

### 6.2 Pending table

| Field | Type | Notes |
|---|---|---|
| key | `(fsid, ino, ino_gen, version_id)` | `version_id` is the marker's content version: one entry, and one recall, per version |
| `recall_id` | u64 | |
| `state` | u8 | "asked", or a `RECALL_STATE` state |
| `answer` | u8 | `DELAY` or `EIO` |
| `resident_bytes` | u64 | durable watermark from the daemon |
| `expires` | monotonic ms | from `ttl_ms` or `valid_ms` |
| `last_asked` | monotonic ms | |
| `requesters` | up to 8 × `(uid, addr hash)` | |
| `parked` | list | parked reads, oldest first |

A sharded hash table (64 shards, one mutex each). A worker holds a shard lock only for the lookup or update, never across a send or any I/O. At most 10⁶ entries; past that, the oldest entries without parked reads are evicted (an evicted entry costs one more `READ_INTENT`, nothing else). The FSAL evicts within the shard that needs room: every expired entry there, or else the one with the earliest expiry. A `FETCHING` entry is never evicted. A `FETCHING` push is stored even over the cap. Entries are swept 60 s after they expire, except `FETCHING` entries and entries with parked reads.

### 6.3 Parked reads

Only for `ONLINE` ranges, only with the `PARK` feature, at most **256 parked reads per export** and, **M5**, at most **`Park_Max_Per_Client` per client address** (the `OMNI` block, §10; default 16, well below a v4.1 session's slots, so a client's resident reads never queue behind its own parked reads). Over either cap: answer `DELAY`.

- `read2` stores `(read_arg, done_cb, caller_arg, export refs)` in the entry's `parked` list and **returns without calling `done_cb`**. The protocol layer then returns `NFS_REQ_ASYNC_WAIT` and frees the worker (`Protocols/NFS/nfs3_read.c:439-454`; `nfs4_op_read.c:1029-1057`; `MainNFSD/nfs_worker_thread.c:1587`).
- When a `RECALL_STATE` or `RECHECK` shows the range covered, a completion thread builds a simple op context for the export (`init_op_context_simple`, as `FSAL/FSAL_CEPH/handle.c:1773-1800` does). It then sets `read_arg->fsal_resume` and calls `done_cb`. The protocol layer resumes the request with `svc_resume` (`nfs3_read.c:261-266`; `nfs4_op_read.c:271-279`), and because `fsal_resume` is set it **calls `read2` again on a worker** (`nfs3_read.c:184-200`; `nfs4_op_read.c:403-420`). That second `read2` runs the whole guard again: it re-reads the marker and reads from VFS under the request's real op context. FSAL threads therefore never do disk I/O. A read that parks again keeps its first park time: `fsal_io_arg.cbi`, which MDCACHE and VFS do not use, carries it, tagged. **Measured** in M1 on v3, 4.0, 4.1 and 4.2 (`t_park.sh`).
- **M5, edge-local fetches.** An `ONLINE` read of the live file through an FSAL that negotiated `CHUNK_MAP` is fetched chunk by chunk into the placeholder by the daemon's cache, without a service recall (`fsal-namespace.md` §5.4). The daemon answers `PARK` (`DELAY` without `PARK`), pushes `RECALL_STATE` with `recall_id = 0` and state `4 FETCHING` **after every widening of the map**, and `5 DONE` once nothing is in flight for the file. With `CHUNK_MAP` negotiated, **every `FETCHING` push resumes every parked read of the entry** (the socket thread does no disk I/O, so it can't read the map); each resumed read re-checks the marker and map on its worker (§6.1), and one still not covered parks again with its first park time, so `park_ms` still bounds it. A fetch that fails pushes `FAILED` (`EIO` while it lasts, `reason` 8, 10 or 5). `FETCHING` fences a truncate to 0 as in §6.1, whatever the `recall_id`.
- **M5, one ask per chunk.** With `CHUNK_MAP` negotiated and an `ONLINE` marker, a read the map does not cover is asked for once per **chunk**, not once per inode. `READ_INTENT` has no chunk field: the FSAL dedupes on the first chunk of the range that isn't local, and the daemon fetches every chunk the range (`offset`, `length`) touches. A pending entry keeps the chunks it asked recently (4 slots, growing to 256 for at most 1,024 entries; past that a ring wraps, which only costs a repeated `READ_INTENT`). A read of a chunk not asked within `answer_ttl_ms` sends a `READ_INTENT` even from a known requester and even within 100 ms of another (§6.1 step 4). The daemon dedupes chunks in flight. An `ONLINE` read that could park but finds a cap full, or whose `READ_INTENT` can't be sent, answers `DELAY` whatever the share's policy (it is a fetch of seconds); only a `FAILED` push makes such a read `EIO`.
- **Measured (G1, M5 port).** 1,000 random 64 KiB reads of a 100 GiB remote object through Ganesha with `FSAL_OMNI` and a fake daemon fetching 4 MiB chunks (`t_chunk_map.sh`): v3, 4.1 and 4.2 each 1000/1000 correct, no zeros, none over 5 s, 0.98 to 1.01 `READ_INTENT`s per read. Through the daemon's cache with a scripted FSAL (`edge/.../tests/chunks_linux.rs`): 1000/1000 correct, 0.99 `READ_INTENT`s per read, p99 under 100 ms, a 1 GiB budget held while 4 GiB passed through.
- **Resume only once the request waits.** A resumed read whose range is still missing (after a `RECHECK`, or a `RECALL_STATE` watermark ahead of the marker) parks again inside the resume handler's `read2`. Ganesha V15.6's resume handlers (`nfs3_read_resume`, `nfs3_read.c:174`; `nfs4_op_read_resume`, `nfs4_op_read.c:374`; `nfs4_op_read_plus_resume`, `:447`) call `read2` and, if the callback arrives before they set `ASYNC_PROC_EXIT`, complete the request with the callback's status without looking at `fsal_resume`. Only the first call (`nfs3_read`, `nfs4_op_read`) loops on it. A resume delivered as success then went out as a 0-byte READ without EOF, which the Linux client reports as `EIO`.
  - **What Ganesha guarantees, and what it doesn't.** The handshake is the request's private `flags` word, using `ASYNC_PROC_DONE` and `ASYNC_PROC_EXIT` (`include/nfs_proto_data.h:216`):
    - The protocol clears both flags before every `read2` call and sets `EXIT` once `read2` returns.
    - `done_cb` sets `DONE`. If `EXIT` was already set, `done_cb` reschedules the request with `svc_resume`, and the resume handler calls `read2` again because `fsal_resume` is set.
    - A `done_cb` may run at any time and is memory-safe throughout ("the resume may already be scheduled and running on another thread", `MainNFSD/nfs_worker_thread.c:1588`; `suspend_op_context`, `FSAL/commonlib.c:3814`).
    - The FSAL gets no signal that the request has set `EXIT`. A timer after `read2` returns cannot prove it either: the worker can be descheduled before it sets the flag.
  - **The rule.** The FSAL delivers a resume only once it has read `ASYNC_PROC_EXIT` set in the request's own flags word (acquire load). The flag is clear while the parking `read2` runs, so seeing it set proves that the call has returned and that the request is waiting.
    - **Earlier resumes are held.** A resume that comes before then is held: it is detached from its pending entry, and the timer delivers it once the flag is set.
    - **Finding the flags word.** The word lives in a struct private to `nfs3_read.c:88` and `nfs4_op_read.c:59`, so the FSAL mirrors the struct's layout and finds the word from `read_arg` (its last member, which MDCACHE passes down unchanged). It uses the mirror only after the request confirms the layout:
      - **v3:** `nfs_reqdata->proc_data` is that struct, its `req` is the request, and the procedure is `READ`.
      - **v4:** the compound's current op is `READ` or `READ_PLUS`, its `op_data` is that struct, and the struct points back at the compound.
    - **When identification fails** (another caller of `read2`, or a Ganesha bump that moved the members), the read **does not park**. It gets the decision's answer instead, never an unproven handoff.
    - **Version guard.** The build refuses any Ganesha other than the one the mirrors were checked against (V15.6): `#error` on another `GANESHA_VERSION_MAJOR`, and a static assertion on the full `GANESHA_VERSION` string from Ganesha's `config.h`. Ganesha's public headers don't define the two structs, so their layout can't be asserted. Static assertions cover what the headers do expose: the types of `nfs_request_t.proc_data` and `.svc`, `compound_data_t.op_data` and `.opcode`, and the `ASYNC_PROC_*` bits. They also cover the mirrors' own shape (`read_arg` last, `flags` a `uint32_t` where it was copied). After re-checking both structs for a new version, opt in with `make OMNI_READ_MIRRORS_VERIFIED_FOR=<that version>`; the value must equal the Ganesha being built against. See `fsal/README.md`, "Bumping Ganesha".
    - **Status and endings.** Every resume is still delivered as `ERR_FSAL_DELAY` with `fsal_resume` set, as a second line of defence. A held resume still ends at `park_ms` and at unexport, and on disconnect it completes fail-closed at once. Error completions (`DELAY`, `EIO`) are honoured whenever they arrive, so they are never held.
  - **Measured** on v3, 4.0, 4.1 and 4.2 (`t_resume_race.sh`): `EIO` on all four before the fix. With the fix, correct bytes even when a test hook keeps the parking `read2` descheduled for 50 ms before it returns. With the same hook, the first version of this port, which delivered held resumes on a 10 ms timer, gave the never-retrying userspace client `JUKEBOX` on v3 and `NFS4ERR_DELAY` on v4.
- At `park_ms` (export option, default 5,000, capped by `park_ms_max`) the timer completes the read with `ERR_FSAL_DELAY`.
- On disconnect from the daemon, every parked read completes at once with the fail-closed answer.

**TEST** in M1: parked reads on NFSv3, v4.0, 4.1 and 4.2 (done, `t_park.sh`: correct bytes in about the fetch time on all four, with no 5 s v3 retry). Still open: what a parked request does to the client's v4.1 session slot and to Ganesha's duplicate request cache under load, and the export's reference counting across an unexport while reads are parked. The FSAL completes an export's parked reads in `prepare_unexport`.

### 6.4 Identity for server-side COPY

Ganesha runs COPY in `xcopy` worker threads under a blank op context: `init_op_context_simple` (`Protocols/NFS/nfs4_op_copy.c:1039`), then `copy_file_range` per chunk (`:866`). So `op_ctx->creds` is empty there (spike B3: `uid=0 client=unknown`).

The FSAL captures identity earlier:
- **Open-state table.** In `open2` (`include/fsal_api.h:2605`), which runs in a worker with the real op context, the FSAL records `identity` keyed by the `state_t *` it was given; `close2` removes it. `copy_file_range` (`:2851`) receives `src_state`, so the FSAL looks it up and sends `identity` with `origin = OPEN_STATEID`.
- **Last opener.** Each OMNI handle also keeps the identity of its last `open2` (uid, client, time). If `src_state` is NULL (for example an anonymous stateid) or not in the table, the FSAL sends that with `origin = LAST_OPENER`, if it is under 60 s old.
- **Otherwise** `origin = UNKNOWN` and `uid = 0xFFFFFFFF`. The daemon still starts the recall, writes the note file, and records the recall as "server-side copy, requester unknown"; the portal shows it, and no Teams card is sent.

**Decided (M0 review):** `FSAL_OMNI` registers a guarded `copy_file_range` in M1. The M0 skeleton (`fsal/FSAL_OMNI/handle.c`) deliberately registers none, so Ganesha's default buffered copy reads through OMNI's guarded `read2`, but with no identity. The M1 op takes identity as above. A source with any non-resident range is never passed to VFS's `copy_file_range`, which reflinks on xfs and so copies the holes (`FSAL/FSAL_VFS/file.c:2339-2420`; spike B). The FSAL sends `READ_INTENT` with `op = COPY_SRC` and returns `DELAY` or `EIO` (COPY's client retry is exponential, 0.2 → 0.4 → 0.8 s, spike B2). Once the whole source range is local, it delegates.

NFSv4.2 `CLONE` never reaches the FSAL: Ganesha V15.6 answers `NFS4ERR_NOTSUPP` itself (`Protocols/NFS/nfs4_Compound.c:618-624`; measured, `docs/design/fsal-namespace.md` §6). If a later Ganesha adds a clone op, it needs a message type here before `FSAL_OMNI` may implement it.

### 6.5 Marker cache and live-handle table

- Each OMNI handle caches its marker after the first read. The cache is dropped on `INVALIDATE`, on disconnect, and on handle release.
- A table of live OMNI handles keyed by `(fsid, ino)` lets `INVALIDATE` find the MDCACHE key (the sub-FSAL's `handle_to_key`, `include/fsal_api.h:2354`). An inode not in the table is not cached by Ganesha, so there is nothing to invalidate.

## 7. Connection states, timeouts and fail-closed defaults

| State | Marker cache | Pending table | Non-local reads |
|---|---|---|---|
| **Connected** (after `HELLO_ACK`) | used | used | §6.1 |
| **Disconnected** (no socket, refused, `HELLO` not answered, `ERROR`, ping timeout) | **not used**: markers read per operation | cleared; parked reads completed fail-closed | fail closed: `EIO` (failfast) or `DELAY` (wait, waitquick). No queueing in the FSAL |

Resident data, resident prefixes, LOOKUP, GETATTR and READDIR work in both states; they never involve the daemon.

| Timer | Value | On expiry |
|---|---|---|
| Connect attempt | 100 ms, non-blocking | retry with backoff 50 ms → 2 s |
| `HELLO_ACK` | 1 s | close, reconnect |
| Request outstanding | 2 s | forget the `req_id`; the pending entry falls back to the policy default until `ttl_ms` passes; a late reply is dropped |
| Liveness | `PING` after 1 s idle; no traffic for 3 s | disconnect |
| `INVALIDATE` ack (daemon side) | 1 s per connection | do not narrow; retry later |
| Parked read | `park_ms` (default 5,000) | complete with `DELAY` |
| Answer reuse | `ttl_ms`, default `answer_ttl_ms` 1,000 | ask again on the next miss |

Fail-closed defaults, one place:

| Event | FSAL answer for a non-local range |
|---|---|
| Socket missing, refused, closed | policy default (`EIO` / `DELAY`) |
| Send would block | policy default |
| No reply within 2 s | policy default |
| Reply with an unknown decision code | policy default |
| `RECHECK` but the marker still doesn't cover the range | policy default |
| Malformed message from the daemon | close the connection; policy default |
| Marker unreadable or invalid | policy default, plus `BAD_MARKER` |
| Daemon `instance` changed | clear the pending table; markers re-read |
| Ganesha lacks `CAP_SYS_ADMIN` | `trusted.*` reads answer "no such attribute" for every file, so nothing can be trusted. **Every** regular-file read and write on an OMNI export fails closed, with CRIT at export creation and rate-limited per operation. LOOKUP, GETATTR and READDIR still work (`t_caps.sh`, with the capability set of `fsal/systemd/nfs-ganesha-omni.service`) |

A `wait` share with the daemon permanently down hangs readers in `D` state (spike B4). That is correct and fail-closed; monitoring must alarm on "daemon down" in seconds.

## 8. Marker layout: `trusted.omnifile` (64 bytes, binary)

Shared by the FSAL, the daemon and the smb-guard. Written only by the daemon, which runs as root. Read by Ganesha, which keeps `CAP_SYS_ADMIN` (it drops only `CAP_SYS_RESOURCE`, `MainNFSD/nfs_init.c:1317-1373`).

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | `magic` | `0x31464D4F` (bytes `O M F 1`) |
| 4 | 1 | `layout` | 1 |
| 5 | 1 | `state` | 1 `RESIDENT`; 2 `PARTIAL`. 0 or anything else: invalid |
| 6 | 2 | `flags` | bit 0 `ONLINE` (readable by range GET); bit 1 `ARCHIVED` (needs a restore); bit 2 `READONLY` (rewind tree); bit 3 `DIR_UNPOPULATED`; bit 4 `CHUNK_MAP` (a `trusted.omnifile.map` xattr exists); bit 5 `WORM` (informational) |
| 8 | 8 | `file_id` | catalog id |
| 16 | 8 | `version_id` | the **content** version these bytes belong to: `catalog.file.content_version_id`, which renames, `chmod`s and tombstones carry forward unchanged. Stub instructions, fills, credentials and recalls all compare against this field, never against the file's current version |
| 24 | 8 | `size` | logical size when the marker was written; if it differs from `st_size`, the marker is stale (`BAD_MARKER` reason 4) |
| 32 | 8 | `resident_bytes` | local prefix `[0, resident_bytes)` |
| 40 | 8 | `marker_seq` | daemon-wide, increasing on every marker write |
| 48 | 8 | `share_id` | catalog `share_id`, bigint, as u64 |
| 56 | 1 | `chunk_shift` | log2 of the chunk size for the map (22 = 4 MiB); 0 without a map |
| 57 | 3 | reserved | zero |
| 60 | 4 | `crc32c` | over bytes 0–59 |

**Validity (normative; agreed with m1-edge-core 2026-10-05).** A reader fails closed (`BAD_MARKER`) on any of these, checked in this order:

| Check | Reason |
|---|---|
| value length is not 64 | 3 (unknown layout) |
| `magic` wrong | 1 |
| CRC-32C (Castagnoli: reflected poly `0x82F63B78`, init and xorout `0xFFFFFFFF`; `check("123456789") = 0xE3069283`) over bytes 0–59 does not equal `crc32c`, a little-endian u32 at 60 | 2 |
| `layout` ≠ 1, `state` not 1 or 2, `resident_bytes` > `size`, `RESIDENT` with `resident_bytes` ≠ `size`, `chunk_shift` ≠ 0 without `CHUNK_MAP`, or outside 12–40 with it | 3 |
| `size` ≠ the inode's `st_size`, for a `PARTIAL` marker | 4 |
| the xattr cannot be read (other than "no such attribute") | 5 |

**A `RESIDENT` marker whose `size` differs from `st_size` is not bad** (M1 review S7). Every byte was local when the marker was written. The daemon narrows to `PARTIAL`, with an acknowledged `INVALIDATE`, before it removes any byte. So a local append or resize of a committed file cannot have made a byte remote. A reader that serves bytes (the FSAL, and the smb-guard) treats the file as all local up to `st_size`. The daemon still treats the mismatch as stale before it stubs or fills: it must re-upload first.

The reserved bytes are written as zero and ignored on read, but the CRC covers them. Unknown flag bits (6–15) are ignored: a safety-relevant change bumps `layout` instead. All integers are little-endian.

**Golden vector.** `file_id` `0x0102030405060708`, `version_id` 42, `size` 1,048,576, `resident_bytes` 65,536, `marker_seq` 7, `share_id` 3, `PARTIAL`, flags `ONLINE`, `chunk_shift` 0 encode to:

```
4f4d4631 01 02 0100 0807060504030201 2a00000000000000 0000100000000000
0000010000000000 0700000000000000 0300000000000000 00 000000 0f6500c4
```

It is asserted in `edge/crates/omnifile-marker` (`tests::golden_vector`), `fsal/tests/unit/test_wire.c` and `fsal/tests/omni_proto.py`.

`trusted.omnifile.map`, when `CHUNK_MAP` is set: a bitmap, bit *i* set means chunk *i* (`[i << chunk_shift, (i+1) << chunk_shift)`, last chunk short) is local and durable. Least significant bit first within each byte.

**Chunk map rules (M5).** A file without a map yet uses the preferred shift (22), raised until its map fits 64 KiB: `ceil(ceil(size / 2^shift) / 8) <= 65,536` (2 TiB stays at 22; 3 TiB is 23 with a 49,152-byte map; capped at 40). A file with a map always uses the map's shift. ext4 caps a value at one block, so maps over about 4 KiB need xfs (the appliance's filesystem).
- The map has exactly `ceil(ceil(size / 2^chunk_shift) / 8)` bytes. Bits past the last chunk are zero. Any other length is invalid (§6.1).
- **Widening** (setting bits) happens only after the chunk's bytes are `fdatasync`ed, as for `resident_bytes`. The daemon writes the map first and then the marker, whose `marker_seq` grows. A reader that sees a map without the marker's `CHUNK_MAP` flag ignores it.
- **Narrowing** (clearing bits: cache eviction, a changed source) follows the stub handshake (R1 §5.1): write the narrowed map and a new marker, get an acknowledged `INVALIDATE` (`MARKER`) from every FSAL, then punch the chunks. Never punch a chunk whose bit is still set on disk.
- `resident_bytes` stays a prefix watermark (the header cache, or a stub's prefix). Chunks below it are local whatever their bits say.
- A widening to `RESIDENT` (every byte local and the whole-file CRC-64/NVME matched) clears `CHUNK_MAP`, sets `chunk_shift` 0 and removes the map.

**Map golden vector.** `file_id` `0x0102030405060708`, `version_id` 42, `size` 10,485,760 (10 MiB), `resident_bytes` 0, `marker_seq` 8, `share_id` 3, `PARTIAL`, flags `ONLINE | CHUNK_MAP` (`0x0011`), `chunk_shift` 22 (4 MiB: three chunks), chunks 0 and 2 local:

```
marker: 4f4d4631 01 02 1100 0807060504030201 2a00000000000000 0000a00000000000
        0000000000000000 0800000000000000 0300000000000000 16 000000 a2fb3ce0
map:    05
```

A read of `[0, 4 MiB)` or `[8 MiB, 10 MiB)` is local; a read touching `[4 MiB, 8 MiB)` is not. It is asserted in `omnifile-marker`, `fsal/tests/unit/test_wire.c`, `fsal/tests/omni_proto.py` and the smb-guard's `classify` tests.

Rules (R1 §5): create placeholders with `O_TMPFILE` and the marker set before `linkat`; narrow the marker and get acknowledged invalidation before punching; widen only after `fsync`.

## 9. Versioning of this contract

- `proto_major` 0 until M1 exits; any incompatible change bumps it, and both sides ship in one appliance build anyway.
- `proto_minor` grows when fields are appended or message types added; each optional behaviour has a feature bit.
- **v0.2 (M5)** adds no message types and no fields. It turns on `CHUNK_MAP`, defined since v0.1, at minor 2, and adds reason codes 10 and 11 and the module option `Park_Max_Per_Client`. Each side removes `CHUNK_MAP` from a negotiation whose minor is below 2, whatever the bits say. An FSAL and a daemon that negotiate v0.1 behave exactly as in M1 (maps ignored, prefix only; whole-file fills), and `POPULATE_DIR` keeps working at minor 1. The daemon offers `CHUNK_MAP` only while its cache is on (`[cache] enabled`).
- **`OPEN_STATE` (feature bit 5)** adds `OPEN_STATE_QUERY` and `OPEN_STATE` (§5.5) behind its own feature bit, at any minor: a side that does not offer it never sends or expects them, and the daemon treats an FSAL without it as unable to say (it is not asked; only the size-and-mtime check applies).
- The marker's `layout` byte is independent of the socket version. A reader that does not know a layout fails closed.

## 10. Not covered here

- The SMB guard's fanotify protocol with the kernel: `docs/pilots/filer-spike-results-A.md`, M2.
- Edge ⇄ service: `docs/contracts/control-api.md`.
- **Policy vocabulary.** The catalog and the control API say `on_open = wait | fail | auto` with `wait_threshold_minutes`. The daemon renders them for the FSAL as:

  | Catalog `on_open` | FSAL `Policy` / `policy` byte | `quick_ms` |
  |---|---|---|
  | `wait` | `wait` / 1 | 0 |
  | `fail` | `failfast` / 2 | 0 |
  | `auto` | `waitquick:<ms>` / 3 | `wait_threshold_minutes` × 60,000 (u32: at most 71,582 minutes, about 49 days; larger values are capped) |

- Per-share policy reaches the FSAL through the Ganesha export config (`FSAL { Name = OMNI; Policy = …; Park_Ms = …; Snap_Dir = …; }`), rendered by the daemon and applied with Ganesha's export reload, not through this socket. `Snap_Dir` (optional, absolute) is the share's rewind area, shown as `.snapshots` in its root (R1 §7); a change to it takes effect when Ganesha restarts. An export without `Policy` is `failfast`, which is fail-closed. An unparseable `Policy` refuses the export, or the reload. The socket path is module-wide: `OMNI { Socket_Path = "/run/omnifile/fsal.sock"; }`. **M5:** so is `Park_Max_Per_Client` (1 to 256, default 16; §6.3).

## 11. Open

- ~~Whether `NS_HINT` is worth its cost once the notification group carries the same events.~~ It is: Ganesha makes every NFS change as one process, so only the FSAL can say which NFS caller made it (the namespace sync's `actor`).
- The Kerberos principal and service level are implemented as in §4.2. Both are verified on krb5p by m2-identity's AD matrix (116 passed, 0 failed). krb5i still needs its own mount.
- Whether `PARK` should also apply to short `ARCHIVED` recalls on `waitquick` shares (probably not: recalls are minutes at best).
