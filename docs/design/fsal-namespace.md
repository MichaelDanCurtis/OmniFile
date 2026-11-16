# FSAL namespace: how `FSAL_OMNI` presents files with no local bytes (risk R1)

**Status: PROPOSED (2026-10-05), M0 design note for risk R1 in `docs/BUILD-PLAN.md`.** It must be reviewed before M1's FSAL is written. Companion notes: `docs/design/edge-metadata.md` (R2, the edge's metadata replica) and `docs/contracts/fsal-socket.md` (the FSAL ⇄ daemon protocol).

Items marked **TEST** are claims a prototype must prove in M0/M1. Items marked **VERIFY** are facts not checked yet. Ganesha citations are to NFS-Ganesha **V15.6** (commit `98eb4be`), the tree the spike built (`/srv/omni-b/src/nfs-ganesha` in the `omnifile-spike` VM). Paths are relative to `src/`.

## The question

Decision 19 puts the migration namespace in v1. One NFS share must show, side by side:
- files written locally and not yet uploaded (landing zone);
- stubs: uploaded files whose bytes were partly or wholly freed;
- objects that have **never** been local: S3-resident files during a migration, and Azure-resident files the edge never held.

Decisions 29–31 (2026-10-05, `docs/design/tiers-versioning-worm.md`) add three more needs:
- **Rewind:** read-only point-in-time views of a share, exposed over NFS (`.snapshots/<label>/…`) and SMB (Previous Versions).
- **WORM shares:** overwrite and delete are allowed but never destroy a committed version before retention ends.
- **Manual tier changes** from the portal: a file can be sent to Archive at any time, and the edge stubs it when it learns.

The spike proved a stacked FSAL over `FSAL_VFS` works for local stubs. It left open how to present the third kind of file, which has no local inode.

## Recommendation

**Option (c): materialise every catalog object as a local sparse placeholder on xfs, and keep `FSAL_OMNI` stacked over `FSAL_VFS` exactly as the spike built it.**

A placeholder is an ordinary xfs file with the object's size, owner, mode, times and POSIX ACL, no data blocks, and an OmniFile marker xattr saying which byte ranges are local. A remote-only object is then simply a stub with nothing resident. The namespace is always a real xfs tree, so:
- LOOKUP, GETATTR and READDIR never leave the box and never touch the daemon;
- the fail-closed guard the spike proved for stubs covers remote objects unchanged;
- AD RBAC (decision 28) is enforced by real POSIX ACLs, with one truth for both protocols;
- SMB sees the same namespace through the same fanotify guard, almost for free.

The price is about **1.1 KB of xfs metadata per object** (measured below), so about 110 GB of SSD for 10⁸ objects, plus a bootstrap that creates 10⁸ inodes.

Rewind views are the one place a virtual tree is unavoidable. They use the same placeholder mechanism, **materialised lazily per directory** into a hidden area, and presented through a reserved `.snapshots` name (NFS) and `vfs_shadow_copy2` (SMB). See section 7.

## 1. The three options

| | (a) Stacked over VFS + virtual entries | (b) Own FSAL, VFS as a delegate for local files | (c) Placeholder per object on xfs (**recommended**) |
|---|---|---|---|
| What it is | The spike's FSAL, plus synthetic objects for remote-only entries, merged into VFS directories | A standalone FSAL whose namespace comes from the replica; local bytes live in files it manages itself | Every catalog object is a real sparse xfs file; `FSAL_OMNI` stays a thin guard over VFS |
| Namespace source for NFS | Two: xfs for local, replica for remote, merged on every READDIR | One: the replica | One: xfs (fed from the replica by the daemon) |
| SMB | Sees local files only; remote objects need a Samba VFS module (GPL) or FUSE | Sees nothing unless the namespace is also projected onto a filesystem | Sees everything through the same fanotify guard |
| AD RBAC (decision 28) | POSIX ACLs for local, replica ACLs for virtual entries: two enforcement paths | ACLs evaluated by our code from the replica: a second permission model | Real POSIX ACLs on every object; Ganesha and Samba enforce them as today |
| FSAL needs the replica in Ganesha threads | Yes, for LOOKUP/GETATTR/READDIR of virtual entries | Yes, for everything | **No.** It reads only its own marker xattr |
| New C code | Large: handle format, cookie merging, every namespace op on virtual objects, promotion of a virtual entry when written | Largest: a whole filesystem | Small: the spike's guard, plus write/setattr guards and an async read path |
| Disk cost per remote object | ~0 | ~0 | ~1.1 KB (measured) |
| File handles | New format; stable if built from catalog ids | New format; stable | VFS handles; stable for an inode's life |
| Rewind views | Virtual, same machinery | Virtual, same machinery | Lazily materialised placeholders (section 7) |

**Why not (a).** It is the worst of both. Every directory that mixes local and remote entries needs a merged READDIR: two cookie spaces, name collisions between a local file and a remote object, and promotion when a virtual entry is written. The FSAL would have to read the replica inside Ganesha worker threads (section 4), and SMB would still see half the namespace.

**Why not (b).** Decision 28 makes POSIX ACLs on the edge's xfs volume the single enforced permission model for NFS and SMB. A namespace that lives only in our database breaks that for every Azure-homed share, not just migrations, and it leaves SMB with nothing to serve. It also means writing a filesystem.

**Why (c).** It turns the migration namespace into a special case of the core product. A remote object is a stub with `resident_bytes = 0`. Everything built for stubs in M1 and M2 applies: the FSAL guard, the smb-guard, the stubber, recall-fill, ACLs and audit.

## 2. What the spike already proved, and what (c) adds

Proved by `docs/pilots/filer-spike-results-B.md` and `-A.md`:
- A stacked FSAL (`FSAL_NULL` copied, over `FSAL_VFS`) loads out of tree and costs nothing measurable on resident reads (B1, B5).
- Reads of holes in a stub are guarded on NFSv3, 4.0, 4.1 and 4.2: `DELAY`/`JUKEBOX`, `EIO`, or correct bytes. A missing, stale or hung daemon fails closed (B2, B4).
- `SEEK` and server-side `COPY` go through the guard (B2x).
- The caller's uid, gid, client address and NFS version reach the daemon, except inside `COPY` (B3).
- The SMB guard is fail-closed with the fd-store group, `FAN_UNLIMITED_QUEUE` and an event journal (part A).

New in (c), measured on the spike VM on 2026-10-05 (Ubuntu 26.04, kernel 7.0, xfs on a loop device, `mkfs.xfs` defaults, `isize=512`):

| Check | Result |
|---|---|
| xfs kernel file handle | `name_to_handle_at` returns **12 bytes**, type `0x81`, for a file and a directory |
| 100,000 placeholders (create, `ftruncate` to 184 GiB, two small xattrs), 1,000 per directory | 0.9 s in page cache (~106,000/s); `df` used grew 107 MB, so **~1.07 KB per placeholder**; `st_blocks = 0` |
| `O_TMPFILE` + `fsetxattr` + `ftruncate` + `utime` + `linkat` | Works on xfs. The file appears with its marker already set (section 5) |
| `trusted.omnifile` xattr | Root reads it; an unprivileged user gets "No such attribute" |
| Reflink (`FICLONE`) | Works on the default `mkfs.xfs` (reflink is on by default) |

The page-cache create rate is an upper bound. Real disks, ACLs, `chown` and journal flushes will be slower; **TEST** the 10⁸ bootstrap rate on the M0 dev VM with a real disk.

## 3. File handles

**NFS limits.** An NFSv3 handle is at most 64 bytes (`NFS3_FHSIZE`, `include/nfs23.h:30`). Ganesha's v3 handle header is 5 bytes, leaving **59 bytes** for the FSAL's opaque part (`struct file_handle_v3`, `include/nfs_fh.h:59-70`). NFSv4 allows 128 bytes (`NFS4_FHSIZE`, `include/nfsv41.h:73`; `struct file_handle_v4`, `include/nfs_fh.h:78-91`).

**With (c) the wire handle is VFS's.** `FSAL_OMNI`'s `handle_to_wire` and `handle_to_key` pass through to the sub-FSAL, as the spike does (`spikes/filer/fsal/FSAL_OMNI/handle.c:581-622`). VFS builds its handle from one type byte, the encoded fsid, one handle-type byte and the kernel handle (`FSAL/FSAL_VFS/os/linux/handle_syscalls.c:170-240`), capped at `VFS_HANDLE_LEN = 59` (`include/fsal_handle_syscalls.h:55`). With xfs's 12-byte kernel handle this fits v3 with room to spare.

**Stability.**
- An xfs handle is inode number plus generation. It survives Ganesha restarts, daemon restarts and reboots, and it survives rename, because nothing about the path is in it.
- MDCACHE indexes entries by the sub-FSAL's `handle_to_key` (`FSAL/Stackable_FSALs/FSAL_MDCACHE/mdcache_helpers.c:697`). A handle not in cache is rebuilt through `create_handle` (`include/fsal_api.h:1078`; `mdcache_handle.c:1679`, `mdcache_locate_host` at `mdcache_helpers.c:1053`). VFS does this with `open_by_handle_at`, so a client can hold a handle across any amount of cache eviction.
- **A handle dies only when the inode dies.** So the daemon must never rebuild a placeholder in place: updating an object's size, times or location changes the existing inode (`ftruncate`, `utimensat`, marker rewrite). It does not unlink and recreate.
- Rebuilding the whole tree (disaster recovery, a new appliance) gives every object a new inode. Clients then see `ESTALE` and must remount. This is acceptable for DR, and it is the reason the xfs tree is treated as durable state and not as a disposable cache.

For comparison, (a) and (b) would encode `(file_id, kind, generation)` in about 24 bytes, which also fits. Handle size does not separate the options; who owns the namespace does.

## 4. READDIR, LOOKUP and GETATTR at scale

**What Ganesha does with a READDIR.**
- MDCACHE caches directory entries in chunks of `Dir_Chunk` entries, default 128 (`MDCACHE_AVL_CHUNK_DEFAULT`, `mdcache_ext.h:48`; config at `mdcache_read_conf.c:65`).
- For **every** entry the FSAL returns, MDCACHE instantiates a cache entry with `mdcache_new_entry` (`mdc_readdir_chunk_object`, `mdcache_helpers.c:2178`, call at `:2222`). The FSAL must hand MDCACHE a full `fsal_obj_handle` with attributes for each dirent through the readdir callback (`fsal_readdir_cb`, `include/fsal_api.h:1570`; `readdir` op at `:1749`).
- MDCACHE's total size is bounded by `Entries_HWMark` (default 100,000, `mdcache_read_conf.c:69`) and `Chunks_HWMark` (default 1,000, `:73`), not by directory size.

**With (c)** this is what VFS already does: `getdents64` on xfs, then `lookup_with_fd` per entry, then the callback with the getdents offset as cookie (`FSAL/FSAL_VFS/handle.c:1360-1495`). `FSAL_OMNI` wraps the callback as the spike does (`spikes/filer/fsal/FSAL_OMNI/handle.c:322-378`). Nothing calls the daemon.
- **Cookies** are xfs directory offsets, which stay valid while entries are added and removed. That is what knfsd and plain Ganesha VFS rely on today (**VERIFY** for xfs block and B+tree directories under concurrent change). Ganesha does not use cookie verifiers by default (`UseCookieVerifier` defaults to false, `support/exports.c:2642`); when enabled it uses the directory's change attribute (`Protocols/NFS/nfs4_op_readdir.c:705-752`). Leave it off: a directory receiving feed updates would otherwise answer `NFS4ERR_BAD_COOKIE` to every long listing.
- **Millions of entries per directory** are an xfs B+tree directory. Listing cost is one `getdents` pass plus one `fstatat` per entry, the same as any large local directory. **TEST** a 10⁶-entry directory listing over NFSv3 and v4.2 in M1.
- **GETATTR** is VFS `fstat`. Size, mode, owner, times and ACL are real inode fields set by the daemon from the catalog.
- **LOOKUP** is VFS `name_to_handle_at`.

**Why this matters for "never block a Ganesha worker".** With (a) or (b), each of these operations needs catalog data inside a Ganesha thread. That data would have to come either from the daemon (a socket round trip per LOOKUP: the spike showed a slow daemon stalls unrelated exports) or from a database the FSAL reads directly (an mmap'd LMDB: page faults on a cold 30 GB file become disk reads in worker threads, and the C side must parse a schema the Rust side owns). With (c) neither is needed; `docs/design/edge-metadata.md` builds on this.

**The change attribute (OPEN).** The daemon writes fills, punches holes and rewrites markers behind Ganesha. Each changes the inode's ctime, and VFS's change attribute follows ctime, so clients drop cached pages after every fill (the spike saw this on recall). That is harmless for correctness but costly when a large file is filled chunk by chunk while being read. Overriding `ATTR_CHANGE` from the marker's `version_id` alone is **not** acceptable: NFSv4 requires the change attribute to move on metadata changes too (a `chmod` must invalidate cached attributes). Candidates for M1: a change counter kept in the marker that the daemon carries across its own writes, or accepting the invalidations and batching fills into large chunks. **TEST** both.

**Attribute caching.** MDCACHE caches attributes for `Attr_Expiration_Time`, default 60 s (`EXPORT_DEFAULT_CACHE_EXPIRY`, `include/nfs_exports.h:201`; `support/exports.c:2569`). When the daemon changes a placeholder's attributes from the feed, it tells the FSAL (`INVALIDATE`, `docs/contracts/fsal-socket.md`), and the FSAL calls `up_async_invalidate` (`include/fsal_up.h:298`) with `FSAL_UP_INVALIDATE_ATTRS` (`:105`). The FSAL finds the MDCACHE key from its own table of live handles, indexed by `(fsid, ino)`: anything not in that table is not cached, so there is nothing to invalidate.

## 5. The placeholder and its marker

**One xattr, `trusted.omnifile`, 64 bytes, binary.** The spike used three `user.omnifile.*` string xattrs. Two problems:
- **`user.*` is writable by users.** Ganesha maps NFSv4.2 `SETXATTR` to the `user.` namespace (`FSAL/FSAL_VFS/handle.c:1811`), and Samba stores client extended attributes as `user.*` xattrs (**VERIFY** on the shipped Samba). The owner of a stub could remove `user.omnifile.state`; the file would then look unmanaged, and its holes would read as zeros. This is a fail-open path.
- Three xattrs mean three reads per check.

`trusted.*` needs `CAP_SYS_ADMIN`. Ganesha keeps it: it drops only `CAP_SYS_RESOURCE` (`MainNFSD/nfs_init.c:1317-1373`). Unprivileged users can neither read nor set it (measured). The layout is fixed in `docs/contracts/fsal-socket.md` §8 so the C and Rust sides share one definition.

**States** (from the marker; no marker means an ordinary local file):

| State | Local bytes | Who reads it |
|---|---|---|
| no marker | all | anyone; never stubbed, never uploaded, or uploaded and still wholly local before stubbing |
| `RESIDENT` | all | anyone; the daemon may stub it later |
| `PARTIAL` | `[0, resident_bytes)` plus any chunks set in the optional chunk map | ranges outside need a fetch or recall |

**Which version.** The marker's `version_id` is always the **content version**: the catalog's `file.content_version_id`, the version that holds the bytes. A rename, `chmod`, ACL change or other metadata-only version carries the content version forward and does not change it. Every check against bytes uses the content version: the stub instruction, the fill, credentials for a range GET, and the recall key. The catalog's `current_version_id` (which also moves on metadata changes) is never compared with the marker.

A remote-only object is `PARTIAL` with `resident_bytes = 0`. Flags say whether the remote copy is `ONLINE` (readable by range GET) or `ARCHIVED` (needs a restore), and whether the object is `READONLY` (rewind trees).

**Creation is atomic.** The daemon never creates a placeholder by name and then sets the marker: between the two steps the file would look unmanaged, and its holes would read as zeros. Instead:
1. `open(O_TMPFILE)` in a staging directory **on the same xfs filesystem**;
2. set the marker, size, owner, mode, times and ACL on the fd;
3. `linkat` it into place (measured to work on xfs).

An `O_TMPFILE` inherits the staging directory's default ACL, not the target's, so the daemon sets the access ACL explicitly from the catalog's ACL snapshot.

**Ordering rules (fail closed):**
- **Narrow the marker before removing bytes**, by the stub handshake below.
- **Widen the marker only after the bytes are durable.** Fill, `fsync`, then raise `resident_bytes` or set chunk bits. The spike's fake daemon already did this.
- **A file replaced by truncation:** truncate to 0 first, then remove the marker. A crash in between leaves an empty file that still claims to be a stub, which reads as EOF, not zeros.

### 5.1 The stub handshake

Every operation that removes local bytes (stubbing on a `stub.instruct`, a manual tier change to Archive, chunk eviction, or a content-version change at the source) follows these steps. The control API's stub flow cites this section.

1. **Check.** The file is not open for write, and its marker's `version_id` equals the instruction's **content** version, which is uploaded and verified. Otherwise skip, and report `skipped` with the reason. The check takes a read lease (`F_SETLEASE F_RDLCK`) and **holds it until step 5**: any open for write or truncate meanwhile breaks it, and the daemon then re-widens the marker and skips (M1 review B1).
2. **Remove the smb-guard's ignore mark** on the inode, if it has one (filled inodes carry one, `filer-spike-results.md` SMB item 5). Otherwise SMB and local reads of the punched range would skip the guard and see zeros. This happens before **every** narrowing, not only a stub: a content-version change and a fill's narrow-back too (M2 review S2).
3. **Narrow the marker** to the new `resident_bytes` (or clear chunk bits), with a new `marker_seq`. The stub keeps whole pages: `resident_bytes` is rounded **up** to the larger of the page size and the filesystem block size (never past `size`). fanotify pre-content ranges are page-granular, so a read ending in a partly resident last page would otherwise raise a recall (M2 integration finding).
4. **Acknowledged invalidation.** Send `INVALIDATE` with `MARKER` and `ACK_REQUIRED` to every connected FSAL (`docs/contracts/fsal-socket.md` §5.5). Wait up to 1 s for `INVALIDATE_ACK` from each. **No FSAL connected is not an acknowledgement** (M1 review B2): a restarting Ganesha looks the same as a lost one, so the stubber does not start until an FSAL is ready, and a narrowing with none connected is not punched. Only an edge configured with `fsal.no_ganesha = true` (SMB only) may punch with no FSAL. A connection still in `HELLO` counts as not acknowledged.
5. **If any connected FSAL did not ack:** do not punch. Leave the narrowed marker (that is safe: the bytes are still there, and reads beyond the new watermark only ask the daemon) and retry later. **After the acks,** re-check the lease from step 1 and the file's size and `mtime` (not `ctime`, which the marker write moves): a write that passed the FSAL's check under the old marker lands before the ack. If anything changed, re-widen the marker (`XATTR_REPLACE`) and skip. Then release the lease.
6. **Punch** the holes, only if the FSAL connection epoch recorded before step 3 is unchanged and the narrowed marker is still on disk.
7. **Report** `stubbed` with the content version and new resident size.

A crash between steps 3 and 6 leaves a narrowed marker over bytes still present, which is safe; the next start re-runs the punch.

### 5.2 Chunk map and xattr budget

**Chunk map (optional, for remote objects read at random).** A prefix watermark alone forces a reader at offset 100 GB to wait for 100 GB to be filled. An optional second xattr, `trusted.omnifile.map`, is a bitmap with one bit per chunk (default 4 MiB). A 1 TB file needs 32 KB, within xfs's 64 KB xattr value limit. The FSAL loads it with the marker. This makes the placeholder itself the "SSD chunk cache" of `ARCHITECTURE.md`; eviction is punching chunks, under the narrowing rule above. v1 may ship prefix-only and add the map in M5; the marker reserves a flag for it now. **M5: built** (§5.4).

**xattr size budget.** On a 512-byte xfs inode, xattrs stay inline only while marker, ACL and any Samba `user.DOSATTRIB` fit in the inode's literal area (roughly 300 bytes). Beyond that each file gets a 4 KB attribute block, which would quadruple the placeholder cost. **TEST** the inline limit with a realistic ACL (4–6 named AD groups) and `DOSATTRIB`.

### 5.3 Filling from S3 (M5)

An S3-resident file is filled the same way as a stub, with one difference. Azure returns a CRC-64 for every range up to 4 MiB, so a fill from Azure widens the marker as it goes: every byte it exposes was checked on arrival. An S3 ranged GET carries no checksum of the range (S3 returns `x-amz-checksum-crc64nvme` for whole objects, and per part only for composite multipart checksums), so a **whole-file** fill from S3 **widens once, at the end**, after the whole-file CRC-64/NVME matches the catalog. Until then the marker keeps its resident prefix and every read of the rest gets the share's wait or fail-fast answer, as during a recall.

This is still how writes, truncates, recalls the user asked for, and every read through an FSAL that negotiated socket v0.1 are served. Reads through a v0.2 FSAL and the smb-guard take the chunk path below (§5.4).

Whole-file fills from S3:
- **Resume:** every `widen_every` bytes, after an `fsync`, the fill records how far it got in `trusted.omnifile.fetched` (version id and offset). An attempt that fails resumes there: it reads the bytes on disk into the CRC and fetches the rest. The whole-file CRC still covers every byte, a bad one left on disk included. The record is removed when the CRC is checked, whether it matched or not. A refused credential (403, a presigned URL that expired during the transfer) is replaced through `read_url`, and the fill carries on.
- **Credential:** by default the `s3_roles_anywhere` pointer the edge signs itself (decision 43; `control-api.md` §3); a presigned GetObject URL only as the operator's fallback. The edge checks each range's length and the pinned `x-amz-version-id`.

### 5.4 Chunk-range reads (M5, port of line B)

A reader of the first megabyte of a 184 GB remote file must not wait for 184 GB. So an `ONLINE` read of the live file, from an FSAL that negotiated `CHUNK_MAP` (socket v0.2, `fsal-socket.md` §5.1) or from the smb-guard, is not a recall. The daemon answers `PARK` (or `DELAY`) and hands the range to its SSD cache (`omnifile-cache`, `edge/crates/omnifile-edge/src/remote.rs`).

**Where the bytes go.** The cache is the placeholders themselves, as §5.2 planned. A fetched chunk (4 MiB by default) is written into the file and recorded in `trusted.omnifile.map` (`omnifile_stub::chunks`):
- **Fills write bytes, `fdatasync`, then the map, then the marker.** The marker gets `CHUNK_MAP`, the map's `chunk_shift` and a new `marker_seq` (`XATTR_REPLACE`). A reader that sees a map without the marker's flag ignores it.
- Fills are **fenced like a recall fill**: the file's lock, `begin_fill` (`FETCHING` pushed, acknowledged `INVALIDATE`) before the first write, and the marker re-checked around every write and widen.
- After each widening the daemon pushes `RECALL_STATE` `FETCHING` (`recall_id = 0`), which resumes every parked read of the file: each re-checks the marker and map on its worker. `DONE` follows once nothing is in flight.
- **Per-file chunk shift.** A file without a map gets the configured shift (22), raised until its map fits 64 KiB (2 TiB stays at 4 MiB chunks; 3 TiB gets 8 MiB). A file with a map keeps its own shift, whatever the configuration says later.
- **Header cache.** For an object the edge never held, the first `[cache] header_mib` (default 4 MiB) is fetched as a resident prefix on first access, and evicted last. A stub keeps the prefix the stubber left (the catalog's, under a lease), and the cache never goes below it.

**When a file becomes resident.** S3 ranges are unverified, so a chunk is exposed after it is durable and checked for status, `Content-Range`, length and version (TLS protects it in flight; owner's choice with the port). When the last missing chunk arrives, the whole file's CRC-64/NVME is computed from disk and compared with the catalog's. A match makes the marker `RESIDENT` (map removed), and the edge reports `POST …/files/{id}/filled` like an online fetch. A mismatch narrows the file back to its prefix by the handshake and fails the read. **Unverifiable bytes never become a resident file.** Without a catalog CRC the file stays `PARTIAL`, and every chunk is still served from the map.

**Eviction narrows, waits for an acknowledged `INVALIDATE`, then punches** (the stub handshake, §5.1, without a stub lease, because the catalog never counted those bytes as local). It writes the narrowed map and a new marker, gets every FSAL's ack, and only then punches. It never punches a chunk whose bit is still set on disk. The cache keeps an LRU per chunk within `[cache] capacity_gib` (headers last), in memory only. It is rebuilt from the markers and maps on disk: a file is adopted on first use after a restart. Eviction passes run one at a time.

**Read-ahead** for a sequential reader runs in the same per-file fetch worker as the demanded ranges, after them, so it holds the same fill fence (a detached read-ahead started after the worker ended its fill, and the fence refused it). A read-ahead that fails is not the reader's failure, unless the source changed.

**After a restart** the cache indexes, before it admits any new byte, every `ONLINE` placeholder on disk with bytes above the catalog's prefix (a page of the replica at a time, one marker and map read each); reads park meanwhile. It then evicts down to its budget. Every fill first makes room for the chunks it will add, so the bytes on disk stay within `[cache] capacity_gib` (or `capacity_mib`) across restarts.

**Pinned reads.** S3 reads name the object version. An unversioned bucket's object is pinned by its ETag (`If-Match`). An object with neither is never read. A presigned URL (the operator's fallback) is pinned by its `versionId` or by the catalog's ETag signed into it as `If-Match`; one with neither is never read chunk by chunk (whole-file fills only). A `412` means the source changed under the catalog: the cache narrows the file to its prefix (acknowledged), the read fails closed (`FAILED`, so `EIO` for a few seconds), and the edge sends the audit record `remote.source_changed` (`control-api.md` §4.7).

**Credentials.** The cache reads with the version's read credential from `POST …/credentials` (cached by the control client): an Azure SAS (ranges split to 4 MiB so each is CRC-checked), or the `s3_roles_anywhere` pointer the edge signs itself with SigV4 and its Roles Anywhere credentials. A presigned URL is the operator's fallback. An archived object, an unreachable service, or a refused credential hands the read to the recall flow, which never serves bytes the marker does not cover.

**Old peers.** A v0.1 FSAL negotiates no `CHUNK_MAP` and reads the prefix only; its reads go to the whole-file fill (§5.3), which also completes a file that has chunks (it clears the map when it widens to `RESIDENT`). The smb-guard reads the map as the FSAL does (`omnifile-smbguard` `classify_mapped`); a map of the wrong length is a bad marker (fail closed).

**Measured (G1).** 1,000 random 64 KiB reads of a 100 GiB remote object: see the PR that ports this (FSAL with Ganesha and a fake daemon; the daemon's cache with a scripted FSAL in `tests/chunks_linux.rs`). Fail closed under chunks: a daemon killed during chunk fills leaves only set chunks with verified bytes, never zeros.

## 6. What `FSAL_OMNI` must guard

The spike guarded `read2` and `seek2`. A placeholder needs every data path guarded, because a write into a hole followed by a fill would mix user bytes with backend bytes.

| Op | `include/fsal_api.h` | Rule for a `PARTIAL` file |
|---|---|---|
| `read2` (READ, READ_PLUS) | `:2676` | Resident range: pass through. Otherwise ask the daemon (async) and answer from the pending table; `ONLINE` ranges may be parked, see below |
| `seek2` | `:2716` | Report the whole file as data, as the spike does |
| `write2` | `:2701` | Any write to a file that is not wholly local needs a full fill first: treat it as a read of the whole file (wait or fail per share policy). Exception: after a truncate to 0 |
| `setattr2` size change | `:2811` | Size 0: allowed (truncate, then remove the marker). Any other size: needs a full fill first |
| `open2` with `FSAL_O_TRUNC` | `:2605` | As size 0 |
| `fallocate` | `:2154` | Needs a full fill first |
| `copy_file_range` (COPY) | `:2851` | Source: guard as a read, with identity from the open stateid (`docs/contracts/fsal-socket.md` §6.4). Never pass a source with any non-resident range to VFS (below). Destination: as `write2` |
| `fallocate` for DEALLOCATE (`allocate = false`) | `:2154` | Needs a full fill first, like any write: otherwise a later fill would overwrite the zeros the user asked for |
| `rename`, `unlink`, `link` | `:2079`, `:2105`, `:2052` | `rename` and `unlink` allowed; they are metadata, and `NS_HINT` tells the daemon who made them (`fsal-socket.md` §5.4). Refuse everything under a `READONLY` (rewind) tree with `ERR_FSAL_ROFS`. `link` is refused with `ERR_FSAL_NOTSUPP` (NFS3ERR_NOTSUPP / NFS4ERR_NOTSUPP, decision 42); SMB and local links are split by the daemon (`namespace-sync.md`) |
| `getxattrs`, `setxattrs` | `:2465`, `:2480` | Nothing to guard: VFS maps client xattrs to `user.*`, never `trusted.*` |
| `getattrs` | `:2034` | Pass through; how the change attribute treats daemon writes is OPEN (section 4) |
| NFSv4.2 `CLONE` | no FSAL op | Nothing reaches the FSAL: Ganesha answers `NFS4ERR_NOTSUPP` (below). A regression test pins this |
| NFSv4.2 `WRITE_SAME` | no FSAL op | `NFS4ERR_NOTSUPP` (`Protocols/NFS/nfs4_op_write.c:694-701`) |
| NFSv4.2 `COPY_NOTIFY` (inter-server copy) | no FSAL op | `nfs4_op_notsupp` (`Protocols/NFS/nfs4_Compound.c:548-554`) |

**Clone paths: investigated 2026-10-05.** A clone of a partial file would create a destination that shares its holes and carries no marker, so it would read as zeros: a fail-open. Ganesha V15.6 has **no FSAL clone or dedupe op**. `include/fsal_api.h` declares none, and `FSAL_VFS` never calls `FICLONE`, `FICLONERANGE` or `FIDEDUPERANGE`. `OP_CLONE` is wired to `nfs4_op_notsupp` (`Protocols/NFS/nfs4_Compound.c:618-624`), which returns `NFS4ERR_NOTSUPP` (`Protocols/NFS/nfs4_op_illegal.c:97-104`). **Measured** on the spike VM with the Linux 7.0 client over NFSv4.2: `cp --reflink=always` and a direct `FICLONE` ioctl both fail with `EOPNOTSUPP`, on the plain VFS export and on an OMNI export with a stub source. No file is created.

The one real clone path is **COPY**. `FSAL_VFS`'s `copy_file_range` calls `copy_file_range(2)` (`FSAL/FSAL_VFS/file.c:2361-2420`), which on xfs becomes a reflink (its own comment, `:2339-2345`), so it copies holes. MDCACHE hands a same-FSAL COPY to the sub-FSAL's `copy_file_range` (`FSAL/Stackable_FSALs/FSAL_MDCACHE/mdcache_file.c:923-980`). Here the sub-FSAL is `FSAL_OMNI`, so OMNI's op decides.
- **M0 skeleton (deliberate):** `fsal/FSAL_OMNI/handle.c` registers no `copy_file_range`. The ops vector starts from `def_handle_ops` (`FSAL/commonlib.c:176-179`), whose `default_copy_file_range` (`FSAL/default_methods.c:1512-1520`) runs `fsal_buffered_copy_fsal` (`FSAL/fsal_helper.c:2545`). That reads through OMNI's own `read2` (`fsal_read` at `fsal_helper.c:2652`), so the guard sees every byte.
- **M1 (decided):** `FSAL_OMNI` registers a guarded `copy_file_range`. If the source has any non-resident range, it guards the range as a read, with identity taken from `src_state` (`docs/contracts/fsal-socket.md` §6.4), and returns `DELAY` or `EIO` per policy. Only when the whole source range is local does it delegate to `FSAL_VFS` (reflink is then safe and fast). The destination is checked as a `write2`.
- **If a future Ganesha adds a clone op,** `FSAL_OMNI` must register it and refuse (`ERR_FSAL_NOTSUPP`) whenever the source has any non-resident range; it must never be inherited as a pass-through. The Ganesha version bump procedure (`fsal/GANESHA_VERSION`) must re-run the clone regression test.
- **SMB and local clones** (`FICLONE`, `copy_file_range` by smbd or a local process) are covered by the fanotify guard: the kernel raises a pre-content event on the source, and a clone arrives as `(0,0)`, a whole-file access (`docs/pilots/filer-spike-results.md`, SMB guard item 6). The guard must allow such an event only when the source is wholly resident, otherwise fill first or deny.

**Async reads for `ONLINE` remote data.** An S3 Standard or Azure Hot object is not archived; returning `EIO` on a failfast share would be wrong, and returning `DELAY` costs NFSv3 clients a fixed 5 s per retry (spike B2). Ganesha supports asynchronous `read2`: if the FSAL returns without calling `done_cb`, the protocol layer returns `NFS_REQ_ASYNC_WAIT` and frees the worker (`Protocols/NFS/nfs3_read.c:439-454`; `nfs4_op_read.c:1029-1057`; `MainNFSD/nfs_worker_thread.c:1587`). A later `done_cb` reschedules the request through `svc_resume` (`nfs3_read.c:261-266`; `nfs4_op_read.c:271-279`). `FSAL_CEPH` already calls `done_cb` from its own thread under a simple op context (`FSAL/FSAL_CEPH/handle.c:1773-1800`).

So `FSAL_OMNI` **parks** a read of an `ONLINE` range for up to `park_ms` (default 5,000 ms):
- the daemon fetches the range and reports progress;
- the FSAL's socket thread then does the local VFS read and calls `done_cb`;
- if `park_ms` expires first, it completes with `DELAY`.

No worker thread blocks. `ARCHIVED` ranges are never parked: recalls take hours, and they use the share's wait/fail policy as the spike did. **TEST** in M1: parked reads on v3, v4.0, 4.1 and 4.2; what a parked request does to the client's v4.1 session slot; and a cap on parked reads per export (proposed 256).

**Second layer.** The smb-guard's fanotify mark covers the whole filesystem, so a Ganesha read of a hole that the FSAL wrongly let through would also raise a pre-content event and be denied. It would block that Ganesha thread while the guard answers, which is acceptable for a safety net that should never fire. **TEST** in M2 (the spike did not stack the two layers).

## 7. Rewind: read-only point-in-time trees (decision 30)

**Discrete labels, not arbitrary times.** The portal can show any instant. The file protocols need a finite list: SMB's Previous Versions enumerates snapshots, and an `ls .snapshots` must end. Each share has a rewind granularity and depth (for example daily at 00:00 UTC for 90 days, plus hourly for the last 24 h). A label names one instant, for example `2026-09-01T0000Z`.

**Where the trees live.** Not inside the share, where `find`, backup crawlers and the upload detector would walk every historical tree. They live in a private area of the same xfs filesystem, `.omni/snap/<share>/<label>/<share-relative path>`. They are presented:
- **NFS:** `FSAL_OMNI` answers LOOKUP of the reserved name `.snapshots` in a share's root directory with the handle of `.omni/snap/<share>/` (a sub-FSAL lookup on another directory). The name is not listed by READDIR, like NetApp's hidden `.snapshot`, so crawlers never enter it. Everything below it is refused for writing with `ERR_FSAL_ROFS`.
- **SMB:** Samba `vfs_shadow_copy2` with an absolute `shadow:snapdir` pointing at the same area and `shadow:format = %Y-%m-%dT%H%MZ`. **Verified** 2026-10-15 on Samba 4.23.6 (Ubuntu 26.04) with the stanza `ShareConfig::render_smb` emits (`edge/crates/omnifile-acl/tests/shadow_copy2.sh`, in the smb-guard CI lane):
  - the labels are a file's previous versions, newest first;
  - each label reads its own bytes, in subdirectories too;
  - the rewind area is not in the share;
  - a write into a previous version is refused (`NT_STATUS_MEDIA_WRITE_PROTECTED`).

  Shares have no `veto files`.

**Lazy materialisation.** Label directories are created eagerly (a few hundred per share). Everything below a label is created on first access, one directory at a time:
- A directory not yet filled carries a marker with the `DIR_UNPOPULATED` flag.
- **NFS:** LOOKUP or READDIR in such a directory sends `POPULATE_DIR` to the daemon and returns `DELAY` (READDIR and LOOKUP are not async in Ganesha). NFSv4 clients retry in about 0.1 s. NFSv3 clients wait 5 s per level, so the daemon also populates one level ahead.
- **SMB:** opening the directory raises `FAN_OPEN_PERM` with `FAN_ONDIR`; the guard populates it, then allows the open (**TEST** that the pre-content group can also carry directory open-permission events on kernel 7.0).
- The daemon gets each directory's entries as of the label from the service (`edge-metadata.md`, "as-of listing") and creates `READONLY` placeholders for that version of each file.
- If the version is the file's current **content** version (`content_version_id`) and the live file is resident, the placeholder is a **reflink** of the live file: instant, no download, and copy-on-write, so later writes to the live file don't change history.
- Otherwise the placeholder is `PARTIAL` and its marker carries that version's **content** version id. Reading it starts a recall of that version, with the share's wait/fail behaviour, a note file and a Teams card.
- **One active recall per `(file_id, version_id)`.** A read of an old version never joins a recall of a different version of the same file, including the current one, and a fill only ever writes the version named in the marker. The FSAL's pending table and the daemon's recall table are both keyed by inode plus content version (socket §6.2).

**Permissions** follow decision 30: a user sees an old version only if the ACL **at that time** allowed it **and** they can read the folder now. The daemon sets each placeholder's ACL to the catalog's ACL as of the label. The "now" half is enforced by the `.snapshots` root, which carries the share root's current ACL, and by giving each materialised directory the intersection of its as-of ACL and the live folder's current ACL. POSIX ACLs can't express an intersection exactly in every case (deny entries don't exist), so the daemon errs towards fewer rights. **TEST** in the M2 permission matrix.

**Built in M5 (daemon side; `edge/crates/omnifile-edge/src/rewind.rs`):**
- Label directories, created from the share's `rewind` settings: daily at `daily_at_utc` within `depth_days`, plus hourly for `hourly_for_hours`. The feed loop refreshes them at most once a minute and removes expired labels.
- `POPULATE_DIR`, answered from `GET …/asof`: files become `READONLY` placeholders of the content version valid at the label, with no write bits; subdirectories are created `DIR_UNPOPULATED`. The directory's flag is cleared last.
- Permissions err towards fewer rights. A file gets its as-of ACL. A directory gets its as-of ACL only while the live folder has the same one, otherwise owner-only. A label directory gets the live share root's ACL.
- Reads of a rewind placeholder start a fetch or recall of the version its marker names, keyed by `(file_id, version)`. The daemon finds the placeholder by inode, checks that its marker is `READONLY` and names that version, and takes the whole-file CRC from its directory's as-of listing. The fill writes only that placeholder, and none of the live file's bookkeeping applies: no dirty tracking, no commit, no `filled` report.

**Built in M5 (FSAL side; `fsal/FSAL_OMNI/omni_rewind.c`):**
- **`.snapshots`.** The export's `Snap_Dir` (`FSAL { Name = OMNI; …; Snap_Dir = "<data>/.omni/snap/<share>"; }`, rendered by `ShareConfig::render_ganesha`) is looked up by path when a client looks up `.snapshots` in the export's root.
  - The root is recognised by inode and device against the export's path, so a handle rebuilt from the wire still counts.
  - `..` of the top of the tree is the share's root, never `.omni/snap`.
  - READDIR of the root does not list `.snapshots`. A real `.snapshots` in a share's root is hidden from LOOKUP, and creating, renaming or removing one there is `ERR_FSAL_ROFS`.
  - Ganesha's MDCACHE must not trust negative lookups (`Trust_Readdir_Negative_Cache`, off by default), or a listed root would answer `.snapshots` with ENOENT.
- **`ERR_FSAL_ROFS`.** It is returned for every change to an object with a `READONLY` marker, or inside a `READONLY` directory, before any other check:
  - create, mkdir, mknod, symlink, link (also of a `READONLY` source), rename (either directory), unlink;
  - setattr of anything (mode, owner, times, size), and the legacy xattr ops;
  - write, fallocate, and the COPY destination;
  - an open for writing or truncate, by handle or by name.

  Before this, a write to a `PARTIAL` placeholder would have started a recall, and a truncate would have dropped its marker.
- **`DELAY` and `POPULATE_DIR`.** LOOKUP (other than `..`) and READDIR in a `DIR_UNPOPULATED` directory send `POPULATE_DIR`, at most once a second per directory, and answer `DELAY`; neither ever answers ENOENT or an empty listing. With no daemon, or one that did not negotiate `POPULATE_DIR`, the share's policy answers instead (wait: `DELAY`; failfast: `EIO`).
- **Markers on directories.** A directory handle carries rewind state. A directory's marker is never answered from the cache when it has one, because the daemon clears and sets `DIR_UNPOPULATED` without an `INVALIDATE`. Most live directories are never read at all: a directory found from a live directory (LOOKUP, READDIR, mkdir), and the export's root, is live. Only handles rebuilt from the wire, `..` results, and what is reached through `.snapshots` read the marker.
- **Tested** by `fsal/tests/t_snapshots.sh` over NFSv3 and v4, using libnfs (`fsal/tests/nfsc.py`) rather than the kernel client. Every check fails when its code is removed. Not tested: `..` from inside a tree (libnfs normalises paths before it sends them).

**Built in M5 (SMB side; option (b) of section 14, `omnifile-smbguard`):** unfilled rewind directories are filled when smbd opens them.
- **Marks.** The daemon puts an inode mark (`FAN_OPEN_PERM | FAN_ONDIR`) on each rewind directory it makes unfilled, in the guard's group. It does so when it creates a label, when it creates a subdirectory while filling, and when eviction makes a directory unfilled again. At startup it marks every unfilled one it finds, since a group the guard had to create has no marks. The marks live as long as the group, which is kept in the fd store, and as long as the inode. They are not evictable: a lost mark would let a folder be listed empty.
- **Held, then filled.** smbd opening an unfilled directory raises a permission event. The guard asks the daemon to fill the directory (the same `populate` as `POPULATE_DIR`) and holds the open. Once the marker has lost `DIR_UNPOPULATED`, it allows the open. A fill that fails refuses the open with `EIO` (an SMB error), never an empty listing. A held open survives a guard kill -9, like a held read: the event is journalled, and the new instance reopens it through the fill mount and decides it.
- **Only file servers are held.** smbd and Ganesha are held (the guard's `server_comms`; an smbd session process is named `smbd[<client>]`). Any other process is allowed at once and sees the directory unfilled. That includes the daemon itself, whose fill opens the directory a held user is waiting on, and root's crawlers (find, updatedb, a backup agent), which would otherwise fill the whole history. The fill mount also ignores directory opens, because the restarted guard reopens held events through it before it reads any.
- **While the daemon is stopped,** an open of a marked directory waits, as a read of a stub does. smbd stops with it (`BindsTo=`), so only root's tools can meet this. `omnifile-guardctl clean` releases the group (and with it every mark and every waiting open); it refuses while smbd or Ganesha run. The e2e tests' resets use `systemctl clean --what=fdstore`.
- **Tested:**
  - A-suite `x02_rewind_dirs.sh`: held and filled for `ls` (a stand-in server) and for a real smbd listing; a crawler not held; a failed fill is EIO locally and an `NT_STATUS` error over SMB; held across a guard kill -9.
  - `tests/e2e/m5_exit_test.sh` step 7: a standalone smbd with the stanza `render_smb` emits lists a folder of today's unfilled label through the daemon's guard. The folder fills from the as-of listing, and a file read from it has its bytes.
  - Mutation checks, each of which fails these tests: never holding a directory; holding the daemon's own opens (a deadlock); holding every process; no fill-mount ignore of directory opens (the guard never comes back after a kill -9); and the daemon not marking directories.

**Built (decision 36): "Previous Versions → Open" on a file in an unfilled label.**
- `vfs_omnifile_prevver` (`samba-vfs/`, GPL-3) is stacked before `shadow_copy2`. For a path with a previous-version timestamp, it opens each directory of the path under the label first, and the marks above fill them. It talks to no OmniFile process.
- `omniacl render` names it only where it is installed.
- **Tests:**
  - A-suite `x03_prevver_open.sh`: Ubuntu's smbd with the module and the guard harness;
  - `tests/e2e/m5_exit_test.sh` step 8: a file in a subfolder of yesterday's unfilled label, opened directly. It is `NOT_FOUND` through `shadow_copy2` alone; with the module it opens, and the label and folder fill.
- Build and ABI notes: `samba-vfs/README.md`.

Why directory events alone can't do it. **Measured** 2026-10-15 (kernel 6.18, Samba 4.23.6):
  - `FAN_OPEN_PERM | FAN_ONDIR` marks on directories are accepted in a `FAN_CLASS_CONTENT` group, and smbd's directory opens arrive as permission events. This answers the **TEST** above.
  - But smbd opens a rewind directory only to **list** it:
    - Listing a file's previous versions opens the share's snap root (label enumeration).
    - Listing a previous version's folder opens that label directory.
    - Opening a **file** in a previous version (`@GMT-…/a.txt`, `@GMT-…/sub/b.txt`) opens no directory at all.
  - So directory events fill a folder someone browses in a previous version (built, above). They cannot fill the path of the Windows flow "Previous Versions → Open" on a file in an unfilled label: that path simply does not exist yet. The client probes each version this way, so an unfilled label would not even be listed for the file.
  - This needs a hook inside smbd before `shadow_copy2`: the module above.

Still open:
- reflinks of resident current versions (an optimisation: a fill is correct without one, and a reflink needs the clone checked against the version's CRC).

Eviction is built: a populated rewind directory unused for `[rewind] idle_evict_s` (default 24 h) gets `DIR_UNPOPULATED` back first, then is emptied. A directory a read is filling is left alone, and `POPULATE_DIR` is held off while it is emptied. "Unused" counts what the daemon sees: populating the directory, and reads that need a fill. Reads of bytes already resident, LOOKUP and READDIR never reach the daemon, so a directory browsed only that way for a day is evicted, and its clients get `ESTALE` (as below). Changes to its ACLs (an ACL's body, or a directory's `acl_id`) empty every populated rewind directory and give the labels the live root's ACL again, so access follows the live share. Trees of shares that are gone are removed.

**Eviction.** Rewind trees are a cache. The daemon removes subtrees idle for longer than a set time (proposed 24 h) and marks the directory unpopulated again. Clients holding handles inside an evicted subtree get `ESTALE`, which is acceptable for a historical, read-only view.

## 8. Creating, renaming and deleting

| Operation | What happens on xfs | What the daemon does |
|---|---|---|
| Create a new file anywhere (even in an S3-resident folder) | A normal local file, no marker | Detects the commit (section 9), uploads to the dataset's **write backend**: Azure while migrating (decision 14) |
| Overwrite (truncate to 0) a placeholder | Truncate, marker removed, new local content | New version, uploaded to the write backend. The old version stays in the catalog (rewind) and at the source for reconcile |
| Partial write to a placeholder | Refused until fully filled (section 6) | Starts the recall or fetch |
| Rename (file or directory) | Real xfs rename; handles unchanged | Reports `rename(file_id, new parent, new name)`. The object key does not change: **rename is metadata-only in the catalog for free**. **Decided (M0 review):** v1 allows renaming S3-resident files as a catalog-only rename; the control API matches |
| Delete | Real unlink | Reports a tombstone. ~~Applied at the source at cutover.~~ Superseded by decision 40: listed in the cutover report, never applied to the source. The version stays visible in rewind until its retention ends |
| Feed says an object appeared | `O_TMPFILE` placeholder, linked in | Applied from the replica |
| Feed says an object's attributes changed | Same inode updated in place | Then `INVALIDATE` to the FSAL |
| Feed says an object was deleted at the source | Unlink, unless the local file has unsent changes | Conflict rules in `edge-metadata.md` |

**How the daemon learns of namespace changes.** Through a fanotify **notification** group on the data filesystem with directory-entry events (`FAN_CREATE`, `FAN_DELETE`, `FAN_RENAME`, `FAN_ATTRIB`, `FAN_CLOSE_WRITE`, `FAN_MODIFY`, reported with `FAN_REPORT_DFID_NAME_TARGET`). This sees NFS, SMB and local changes alike. The group's fd is held in the systemd fd store like the pre-content group, so events queued while the daemon restarts are not lost (**TEST**: the A-suite proved this for the pre-content group only). A reconciliation scan covers anything missed (`edge-metadata.md`). The FSAL's `NS_HINT` messages only add who made an NFS change; they are not the source of truth.

The detector and uploader ignore `.omni/` (staging, commit clones, rewind trees).

## 9. WORM shares and committed versions (decision 31)

**Recommendation: do not use `chattr +i` on the edge.** Enforce retention where it is durable, in the version store, and make sure the edge always gets every committed version there.

Why not `chattr +i`:
- WORM here means overwrite and delete **are allowed** and create a new version or a tombstone. An immutable inode forbids exactly those operations.
- It breaks reads through `FSAL_OMNI` today: VFS opens the file `O_RDWR` to read an xattr, which fails with `EPERM` on an immutable file (spike bug, `tests/xattr_open_mode.sh`).
- Stubbing would need clear-flag, punch, set-flag. A crash in between leaves the file mutable with no record that it should not be.
- Retention must survive the edge being lost. Version-level immutability on the blob (already planned for legal holds, `AGENTS.md`) does; a local flag does not.

**What the edge does instead: commit clones.**
1. A version is **committed** when the detector declares the write complete: `FAN_CLOSE_WRITE`, or the FSAL's `WRITE_CLOSED` for an NFSv4 CLOSE, followed by the share's quiet period with no further modification. NFSv3 has no CLOSE, so it relies on the quiet period after the last `FAN_MODIFY`. The period is per share, 60 s by default on a WORM share and never below 10 s (decision 67, `edge-metadata.md`).
2. At commit, the daemon **reflinks** the file into `.omni/commit/<lid>.<n>` on the same filesystem, journals the version in its outbox, then hashes and uploads **from the clone**, not from the live file.
3. A later overwrite or delete of the live file cannot touch the committed bytes: the clone shares blocks copy-on-write. The clone is removed only after the service confirms the version is stored and verified.

This also fixes a race that exists on every share: an upload reading a file the user is still changing. So commit clones apply to all shares whose rewind depth is above zero. Writes made before a version is committed belong to that version and may be lost by a later overwrite, by design.

**Stubbing on WORM shares** is unchanged: the stubber punches holes only in a file whose local content equals a committed, uploaded and verified version: the marker's `version_id` must equal the stub instruction's **content** version id (not the file's current version, which also moves on renames and metadata changes). The steps are the stub handshake (§5.1). Nothing committed exists only on the edge at that point.

**Optional local immutability**, if a client ever needs the live path itself frozen (true WORM, no new versions): refuse writes, truncation, unlink and rename in `FSAL_OMNI` (NFS) and use Samba's `vfs_worm` (SMB). Not in v1; listed so `chattr +i` is not reached for.

## 10. Manual tier changes (decision 29)

The edge needs nothing special. A portal tier change arrives in the feed as a location update and a stub instruction for `(file_id, content version id)`. The daemon stubs the file by the stub handshake (§5.1) if its marker carries that content version and it is not open for write; otherwise it reports the instruction skipped and why. Reads afterwards behave as for any archived stub.

## 11. Memory, disk and inode costs at 10⁸ objects

| Item | (c) | (a)/(b) |
|---|---|---|
| xfs metadata per remote object | ~1.07 KB measured (512-byte inode, directory entry, inline xattrs) → **~110 GB** | ~0 |
| Edge replica (daemon) | ~27 GB in SQLite (`edge-metadata.md`) | about the same, but it must also be readable by the FSAL |
| xfs inode limit | `imaxpct` is 25% below 1 TB and lower on bigger filesystems; it scales with volume size, so plan the data volume ≥ 2 TB SSD per 10⁸ objects | n/a |
| Kernel inode and dentry cache | Scales with the working set, not the total | n/a |
| Ganesha MDCACHE | `Entries_HWMark` 100,000 by default (`mdcache_read_conf.c:69`); independent of total | same |
| `FSAL_OMNI` memory | Marker cache per live handle (≤ 100,000 × ~100 B), pending table (≤ 1M entries × ~64 B) | plus replica pages in Ganesha's address space |
| Bootstrap of 10⁸ objects | 10⁸ inode creations: estimated 35–85 min at 20–50k/s (**TEST**) | none |

The placeholder cost is real but bounded and linear: about 1% of a 10 PB estate's size. It buys the namespace, the ACLs and SMB without new filesystem code.

## 12. SMB view of S3-resident files

Out of v1 per `BUILD-PLAN.md`. With (c) the architectural cost is close to zero, because a placeholder is a stub as far as Samba and the smb-guard are concerned. What would remain:
- **TEST** Windows behaviour on long waits (STATUS_PENDING, already a pilot item);
- the guard blocking an smbd thread during an `ONLINE` fetch (seconds), which is the same as an SMB read of a stub today;
- Explorer thumbnailing triggering fetches (pilot item R4).

Proposal: build v1 so that the SMB share for a migrating dataset is a configuration switch, off by default, and let the owner decide after the on-site pilot whether to turn it on. It stays fail-closed either way, because the guard marks the whole filesystem.

## 13. Fail-closed rule, restated for placeholders

| Path | Guarantee |
|---|---|
| NFS read, FSAL healthy | Marker (cached, or read with `fgetxattr` on an `O_RDONLY` fd, never `O_RDWR`) says the range is local, or the read does not reach VFS |
| NFS read, daemon down or slow | Marker still decides. Non-local ranges get the share's fail-closed answer; no worker blocks (pending table, async socket) |
| NFS read, FSAL disconnected from the daemon | The FSAL drops its whole marker cache on disconnect and reads markers per READ until reconnected, so a stub the daemon narrowed meanwhile is never served from a stale cache |
| SMB or local read | The fanotify pre-content guard, as proven in part A |
| Placeholder creation | `O_TMPFILE` + marker + `linkat`: never visible without its marker |
| Marker tampering | `trusted.*`: unreachable from NFS, SMB and unprivileged local users |
| Stubbing and eviction | Narrow the marker, wait for acknowledged invalidation, then punch |
| Unknown marker version or bad checksum | Treated as `PARTIAL` with nothing resident: fail closed, and reported |

Never `ENOENT` for an archived or remote file: it always exists on xfs.

## 14. Decided here, and open

**Proposed for decision (owner review):**
1. Option (c): placeholders on xfs, `FSAL_OMNI` stacked over VFS.
2. One binary marker in `trusted.omnifile`; atomic creation with `O_TMPFILE`; the narrow-before-punch rule.
3. Async parked reads for `ONLINE` remote ranges; `DELAY`/`EIO` per policy for `ARCHIVED`.
4. Rewind trees materialised lazily under `.omni/snap/`, reached by the hidden name `.snapshots` (NFS) and `vfs_shadow_copy2` (SMB).
5. WORM by commit clones (reflink) and cloud version immutability; no `chattr +i`.
6. Rename and delete of S3-resident files are allowed in v1 as catalog-only changes (rename **decided** in the M0 review).
7. **Decided (M0 review):** `FSAL_OMNI` registers a guarded `copy_file_range` in M1 that takes identity from `src_state`; CLONE stays unsupported (§6).

**Open:**
- Hard links to managed files: refuse `link` in the FSAL (NFS) and accept that SMB can create them, or model them in the catalog? **Decided: refuse (decision 42).** Built: the FSAL answers `LINK` with NOTSUPP; a second name made over SMB or locally (which bypass the FSAL) is split by the daemon into its own reflink copy and committed as a separate file, and one to a placeholder whose bytes are not all local is removed (after a check by handle) and audited (`namespace-sync.md`). No Samba VFS module of ours is needed for it.
- How exactly to compute "as-of ACL and readable now" for a materialised rewind directory when the two ACLs name different groups.
- Chunk map in M1 or M5.
- `.snapshots` name and label format: confirm with the owner. **Decided: `.snapshots/YYYY-MM-DDTHHMMZ/` (decision 41).**
- ~~Previous Versions of files in unfilled labels over SMB (§7, measured).~~ **Decided 2026-10-15 (decision 36): (a) on top of (b), both built.** Directory permission events can't fill them. The options were:
  - **(a) Chosen:** a small Samba VFS module stacked before `shadow_copy2`. It runs inside smbd, so it is GPL-3. It lives in `samba-vfs/` under GPL-3, as `fsal/` does under LGPL.
    - **With (b) built, it needs no protocol at all.** For a path with a previous-version timestamp (`smb_fname->twrp`), it opens each directory of that path under `shadow:snapdir/<label>/` before passing the call on. The guard's marks hold those opens while the daemon fills them, so the module never talks to the daemon.
    - **Prototyped 2026-10-15**, as about 120 lines of C (`stat`, `lstat`, `fstatat` and `openat` hooks), built into Samba 4.23.6 and run on the M5 e2e stack. Opening `@GMT-<yesterday>/deep/d.txt` directly, in a label nothing had filled:
      - `shadow_copy2` alone: `NT_STATUS_OBJECT_PATH_NOT_FOUND`;
      - with the module: the file's bytes in 0.16 s, the label and `deep` filled for it.
    - **Cost, measured:** `allinfo` on that file probes every label, as the Windows Previous Versions tab does. Over 53 labels that took 1.7 s and 75 directory fills (each label, and `deep` where it existed then). Each fill is one as-of call to the service; eviction empties idle directories after `idle_evict_s`.
    - The owner chose it the same day; it shipped in `samba-vfs/`.
  - **(b) Built** (§7): fill on directory events, in the guard. Browsing a previous version's folders works over SMB. On its own it can't fill a file opened directly; (a) adds that.
  - **(c)** For SMB-enabled shares, fill every label's directory chain eagerly for recently changed paths: bounded by the change rate, but unbounded for a whole tree.

**To prototype in M0/M1 (TEST):** the 10⁸ bootstrap rate on a real disk; the xattr inline budget with real ACLs; `trusted.*` reads by the stacked FSAL without `O_RDWR`; parked async reads and v4.1 session slots; the change attribute across daemon writes; the notification group surviving restarts in the fd store; `FAN_OPEN_PERM` on directories (measured, §7); `vfs_shadow_copy2` with an external snapdir (verified, §7); a 10⁶-entry directory over NFSv3 and v4.2; the fanotify guard as a second layer under Ganesha; a regression test that NFSv4.2 CLONE returns `NOTSUPP` and COPY of a stub never yields zeros, run on every Ganesha version bump.
