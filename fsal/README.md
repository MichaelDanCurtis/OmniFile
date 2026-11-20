# FSAL_OMNI (NFS-Ganesha module)

This is a C module for NFS-Ganesha, licensed LGPL-3.0 to match Ganesha (`COPYING.LESSER`, with the GPL-3.0 text it builds on in `COPYING`). The rest of the repository is Apache-2.0. `FSAL_OMNI` is stacked over `FSAL_VFS` on the edge filer's xfs volume (R1, `docs/design/fsal-namespace.md`). Every file is a real inode there. A file the daemon has stubbed, or a placeholder for a remote object, carries the binary `trusted.omnifile` marker, which says which prefix of the file is local. The module is the NFS half of the stub guard: no NFS operation ever reads, copies or overwrites bytes that the marker does not cover. When an operation needs non-local bytes, the module answers according to the share's policy:

- **wait:** returns `ERR_FSAL_DELAY`, which Ganesha sends as `NFS4ERR_DELAY` (NFSv4) or `NFS3ERR_JUKEBOX` (NFSv3);
- **failfast:** returns an I/O error;
- **waitquick:<ms>:** the daemon picks between the two from its ETA.

It also tells the daemon who asked over a Unix socket: uid, gid, client address, NFS version, sec flavour and client id. It is the only component that can see which NFS user asked. The wire format is in `docs/contracts/fsal-socket.md`.

Keep this module **small**. Policy, catalog and cloud logic belong in the daemon and the service, not here.

## Status

**M1 guard.** It implements `docs/contracts/fsal-socket.md` v0.1:

| Piece | Where | What |
|---|---|---|
| Marker | `omni_wire.c`, `omni_guard.c` | The 64-byte `trusted.omnifile` codec, byte-identical with the Rust one (shared golden vector). The marker is read with `open_by_handle_at(O_PATH)` and `fgetxattr`, never with an `O_RDWR` open, so `chattr +i` files read (spike bug). It is cached per inode only while connected, and dropped on `INVALIDATE` and on disconnect. |
| Connection | `omni_conn.c` | One `SOCK_SEQPACKET` connection per Ganesha. It does the `HELLO` handshake with a version check, `PING` liveness, resync and backoff. Workers only send with `MSG_DONTWAIT` and look things up in memory. One socket thread reads and runs the timers. |
| Pending table | `omni_conn.c` | Answers NFS retries from memory: a waiting file costs about one `READ_INTENT`, where the spike sent one per retry. Never blocks a worker; a hung daemon does not slow other exports. |
| Parked reads | `omni_conn.c`, `file.c` | `ONLINE` ranges are parked without holding a worker. They resume through Ganesha's `fsal_resume`, and the worker re-reads the marker before serving a byte. |
| Guarded ops | `file.c` | `read2`/READ_PLUS and `seek2`. `write2`, size changes, `fallocate` and COPY destinations need a full fill first. Truncate to 0 is allowed and drops the marker, except under the fill fence. The guarded `copy_file_range` takes identity from `src_state`. CLONE stays NOTSUPP in Ganesha. |
| Identity | `omni_guard.c` | Captured at `open2`, keyed by state, for COPY's blank op context; the last opener as a fallback. |
| Stub handshake | `omni_guard.c` | `INVALIDATE` waits for in-flight I/O that saw the old marker before `INVALIDATE_ACK`, so a hole is never punched under a running read. |
| Fail closed | everywhere | Socket missing, stale or hung, timeouts, a bad marker, a version mismatch or a malformed message all give `EIO` or `DELAY`, never zeros. Without `CAP_SYS_ADMIN` the module logs CRIT and fails every regular-file read and write closed. |

M1 used the resident prefix only. Rewind trees (`POPULATE_DIR`, `.snapshots`) came with M5 at minor 1.

**M5 chunk maps (contract v0.2).** The module offers `CHUNK_MAP` and minor 2. It uses maps only when the daemon's `HELLO_ACK` has minor 2 or more and `CHUNK_MAP`; against a minor-1 daemon it behaves as before (prefix only, `POPULATE_DIR` unchanged).

| Piece | Where | What |
|---|---|---|
| Chunk maps | `omni_wire.c`, `omni_guard.c` | `trusted.omnifile.map` is read with the marker by the same `O_PATH` route and cached with it (maps up to 8 KiB; larger ones are read per operation). A range is local when the prefix or the set chunks cover it. A map of the wrong length, a bit past the last chunk, a map over 64 KiB, or the flag without a map fails the file closed (`BAD_MARKER` 3); a map without the flag is ignored. Golden vector shared with Rust and Python. |
| Chunk fetches | `omni_conn.c` | One `READ_INTENT` per chunk a reader waits for (4 recent chunks per entry, growing to 256 for at most 1,024 entries), and every `FETCHING` push resumes the file's parked reads to re-check the map. |
| Park caps | `omni_conn.c`, `main.c` | 256 parked reads per export and `Park_Max_Per_Client` (default 16) per client address. An `ONLINE` read over a cap, or whose `READ_INTENT` can't be sent, answers `DELAY` whatever the share's policy. |

## Configuration

```
OMNI { Socket_Path = "/run/omnifile/fsal.sock"; Completion_Threads = 4; Park_Max_Per_Client = 16; }   # optional, module-wide
EXPORT {
    FSAL { Name = OMNI; Policy = "wait" | "failfast" | "waitquick:<ms>"; Park_Ms = 5000;
           FSAL { Name = VFS; } }
}
```

`Policy` defaults to `failfast`, which is fail-closed. An export reload applies a new `Policy`. Run Ganesha with `systemd/nfs-ganesha-omni.service`. Its capability set is the one the tests prove.

## Layout

| Path | What |
|---|---|
| `GANESHA_VERSION` | The **one place** the Ganesha version is pinned: tag, the commit it must resolve to, and the libntirpc submodule commit. |
| `build/Containerfile` | Ubuntu 26.04 image with Ganesha built from that pin, using the filer spike's cmake options (`ALLOCATOR=jemalloc`, every FSAL except VFS and NULL off). It holds the source tree (`$GANESHA_SRC`), the build tree with the generated `config.h` (`$GANESHA_BUILD`), and the installed tree (`/opt/ganesha`). It is architecture-neutral (x86_64 CI, arm64 Macs). |
| `build/run.sh` | `image`, `module`, `smoke`, `all`, `shell`. CI and local use run the same steps. |
| `FSAL_OMNI/` | The module, and a `Makefile` that builds `libfsalomni.so` out of tree with `-Wall -Wextra -Werror`. |
| `FSAL_OMNI/omni_wire.[ch]` | Marker codec and wire format. Pure C, unit-tested on its own. |
| `FSAL_OMNI/omni_guard.[ch]` | Markers, live inodes, pins, identity, the per-operation guard. |
| `FSAL_OMNI/omni_conn.c` | Daemon connection, pending table, parked reads, invalidation. |
| `FSAL_OMNI/omni_rewind.c` | Rewind trees: `.snapshots`, `ERR_FSAL_ROFS` below it, `DELAY` and `POPULATE_DIR` for unfilled directories. |
| `systemd/nfs-ganesha-omni.service` | The production unit, with its capability set. |
| `tests/smoke.sh` | The load smoke test. It runs inside the image. |
| `tests/run_all.sh`, `tests/t_*.sh` | The guard suite (below). |
| `tests/fake_daemon.py`, `tests/omni_proto.py` | A fake daemon with scripted policies, on an independent Python codec of the contract. |
| `tests/mkstub.py`, `tests/reader.py`, `tests/unit/test_wire.c` | Placeholder maker (`O_TMPFILE`, marker, `linkat`), byte-checking reader, C unit tests. |
| `tests/nfsc.py` | A userspace NFS client over libnfs (no kernel client; never retries, so each `DELAY` is seen). |

## Build and test

You need Docker (or `DOCKER=podman`). From the repository root:

```sh
fsal/build/run.sh image     # Ganesha toolchain image, about 45 s on 16 cores
fsal/build/run.sh module    # fsal/.build/libfsalomni.so, about 1 s
fsal/build/run.sh smoke     # load test, about 3 s
fsal/build/run.sh test      # guard suite, about 4 min (or: test t_b4_fail_closed t_park)
fsal/build/run.sh all       # image, smoke, test
```

**Rebuild the image after every change to `fsal/build/Containerfile` or `fsal/GANESHA_VERSION`, including ones you pull or merge.** `module`, `smoke` and `test` use whatever image `IMAGE` names (default `omnifile-ganesha:local`) and never rebuild it, and the tag is shared by every checkout and worktree on the machine. The rebuild is mostly cached and takes seconds when only the package list changed. If several branches need different images, give each its own tag (`IMAGE=omnifile-ganesha:<branch> fsal/build/run.sh image`, then the same `IMAGE` for `test`). The suite checks the image for the tools it needs before it starts, and refuses to run with `FAIL setup: the toolchain image lacks: …` if any is missing. Before that check existed, an image built before `libnfs-utils` was added (2026-10-05 or earlier) failed 26 checks on an unchanged `main` (2026-10-21): every check in `t_snapshots` (16) and `t_reload` (8), and the two "descheduled window" checks of `t_resume_race`, because `tests/nfsc.py` could not load `libnfs.so.14`.

**Host load.** The suite passes on a loaded host. On 2026-10-21, on Docker Desktop for Mac (16 CPUs, 7.7 GiB VM), with the host's 1-minute load between 12 and 58 and at least 1.7 GiB available in the VM, it passed 257/0 in seven of eight runs. The eighth run failed one timing check in `t_marker_cache` (B3 expected the `INVALIDATE` ack to take at least 150 ms; it took 117 ms because the test's own processes started late). B3 and S4 now decide from timestamps instead: the slowxattr shim logs each held marker read, and the fake daemon reports when it sent the `INVALIDATE` and when the ack came back. With that change the suite passed 257/0 three runs in a row. The fake daemon answers `HELLO`, `PING` and `INVALIDATE_ACK` without disk I/O, so a slow VM disk no longer causes the liveness disconnects seen earlier. If `t_b1_load` or `t_park` reports a disconnect, check the VM's `MemAvailable` (`/proc/meminfo` in any container) before you suspect the module.

`smoke` runs the container `--privileged`, for two reasons. FSAL_VFS needs `CAP_DAC_READ_SEARCH` for `open_by_handle_at`, and the NFS client mounts need `CAP_SYS_ADMIN`. The exported directory sits on an anonymous Docker volume, because FSAL_VFS cannot export the container's overlayfs root (or tmpfs).

The test checks five things:
1. `ganesha.nfsd` loads `libfsalomni.so`, and the export is created through it over VFS, with no FSAL or CONFIG errors in the log.
2. `showmount -e` lists the export.
3. NFSv3, v4.1 and v4.2 clients can read, write, rename and remove through it, byte for byte.
4. Over NFSv4.2, CLONE is refused. `cp --reflink=always` and a raw `FICLONE` (`xfs_io reflink`) both fail with `EOPNOTSUPP` and leave no destination data, because Ganesha V15.6 wires `OP_CLONE` to NOTSUPP. A plain `cp` goes out as a server-side COPY and round-trips byte for byte. The test fails if a Ganesha bump starts accepting CLONE, since a reflink of a stub would bypass the stub guard.
5. The module links jemalloc.

Set `SMOKE_MOUNT=yes` to make the mount round trip mandatory, as CI does, or `no` to skip it. The default, `auto`, skips it, with a message, when mounting is impossible.

You can build the image for the other architecture with `docker build --platform linux/amd64 …`, and the module compiles in it. However, `ganesha.nfsd` segfaults under qemu user-mode emulation, so run the smoke test only on a native host.

To build by hand inside the image (`fsal/build/run.sh shell`):

```sh
make -C /fsal/FSAL_OMNI O=/tmp/omni install   # installs to /opt/ganesha/lib/ganesha/
```

The `Makefile` defaults to the image's paths. Override `GANESHA_SRC`, `GANESHA_BUILD` and `GANESHA_LIB` to build against another tree. That tree must be the same pinned version, built with the same options.

## Rules the build enforces (and why)

These come from filer spike B (`docs/pilots/filer-spike-results-B.md`, "Build instructions").

- **Same Ganesha, same `config.h`.** `register_fsal()` checks only the FSAL API version (14.0). Struct layouts change with build options. The module therefore always builds against the image's own build tree, with the defines Ganesha's cmake uses for in-tree `FSAL_NULL`. The Makefile names the file they come from. Ship the module and Ganesha as one versioned build, never with a distro Ganesha. No distro ships Ganesha headers, and Ubuntu's package is 6.5.
- **POSIX ACLs are on** (`-DENABLE_VFS_POSIX_ACL=ON`; libacl, LGPL-2.1+, is linked into Ganesha only). FSAL_VFS never switches to the caller's credentials, so without this option Ganesha checks mode bits only: a named AD-group ACL entry would grant nothing over NFS (decision 28). The image build fails if cmake turned the option off. With it on, every GETATTR on a filesystem without POSIX ACL support fails (NOTSUPP), and FSAL_OMNI logs CRIT for such a filesystem. Docker Desktop's kernel has no `CONFIG_XFS_POSIX_ACL`, so there the guard suite runs on an ext4 loop volume and records a NOTE; CI and the appliance use xfs.
- **Link `-ljemalloc`.** Without it, Ganesha segfaults while loading the export: the module binds glibc's `calloc`, and Ganesha's jemalloc `malloc_usable_size()` then crashes. This was re-checked in this image, where the result was SIGSEGV, exit 139. The build fails if the `.so` has no `libjemalloc` `NEEDED` entry. The image build fails if cmake fell back to libc.
- **The pin is verified.** The image build fails if the `V15.6` tag no longer resolves to the pinned commit, or if the libntirpc submodule differs.
- **Bumping Ganesha:**
  1. Edit `GANESHA_VERSION`, changing all three lines.
  2. Diff the in-tree `FSAL_NULL` and its `flags.make` against the previous version, and carry the changes into `FSAL_OMNI/`.
  3. Re-check the two Ganesha-private structs that `FSAL_OMNI/omni_guard.c` mirrors to find a READ's `ASYNC_PROC_*` flags (contract §6.3, "Resume only once the request waits"):
     - `struct nfs3_read_data` in `src/Protocols/NFS/nfs3_read.c`;
     - `struct nfs4_read_data` in `src/Protocols/NFS/nfs4_op_read.c`.

     Also check the flag handshake in `nfs3_read`, `nfs4_op_read` and their resume handlers. The build refuses any other Ganesha version than the one the mirrors were checked against: `#error` on another major version, and a static assertion on the full `GANESHA_VERSION` string. Static assertions also cover the public types the check reads and the mirrors' shape.

     Once you have updated the mirrors, either change `OMNI_READ_MIRRORS_CHECKED` in `omni_guard.c`, or build with `make OMNI_READ_MIRRORS_VERIFIED_FOR=<version>`. That value must equal the Ganesha being built against, so an opt-in never carries over to a later bump. If Ganesha changes the structs in a way the runtime checks catch, reads stop parking (they get the decision's answer); the handoff never goes wrong silently.
  4. Run `fsal/build/run.sh all`.

## The guard suite

`fsal/build/run.sh test` runs `tests/run_all.sh` in a privileged container. The tests run against a real `ganesha.nfsd`, the fake daemon, and an xfs loop volume, over NFSv3, 4.0, 4.1 and 4.2. Each check prints PASS or FAIL, and the run fails on any FAIL.

| Test | Covers |
|---|---|
| unit | Marker codec against the Rust golden vector, validity rules, message layouts (C and Python) |
| `t_b1_load` | B1: exports with policies, HELLO/HELLO_ACK, PING/PONG, a daemon whose disk stalls 4 s per message stays connected, plain round trips, POSIX ACL named user/group entries enforced over NFSv3 and v4.2 |
| `t_b2_read` | B2: header reads never ask; wait gives correct bytes through a recall; failfast gives EIO, then correct bytes; waitquick; about 1 `READ_INTENT` per waiting file |
| `t_b2x_paths` | SEEK reports all data; COPY guarded and carrying the opener's identity; CLONE NOTSUPP; mmap SIGBUS; WRITE/SETATTR/ALLOCATE need a fill; `cp` over a stub (truncate 0) |
| `t_b3_identity` | B3: uid, gid, version, client address, sec, client id; `obj_ref` keys equal the daemon's `statfs`/`st_ino`/`FS_IOC_GETVERSION` |
| `t_b4_fail_closed` | B4 and the contract 7 table, on all four versions: socket missing, stale, hung (twice), version mismatch, malformed, unknown or lying decisions, bad markers, immutable files. A wait read counts as blocked only if `timeout` fires (exit 124) and the mount still answers |
| `t_park` | Parked reads on all four versions; `park_ms` expiry (v4.0, 4.1, 4.2); no PARK feature (4.1, 4.2); daemon lost while parked (all four) |
| `t_resume_race` | A parked read resumed again while the `read2` that re-parked it is still running (the slowxattr shim holds the re-park's `READ_INTENT`, the fake daemon pushes `FETCHING` every 20 ms with a watermark ahead of the marker), on all four versions: correct bytes, never Ganesha's 0-byte READ that the client reports as `EIO`; and, with a hook that keeps the parking `read2` 50 ms from returning while a second push lands, the userspace client (no retries) on v3 and v4 gets every byte with no `DELAY` (resumes wait for the request's `ASYNC_PROC_EXIT`) |
| `t_invalidate` | The stub handshake: acked `INVALIDATE`, then punch; a concurrent O_DIRECT reader sees bytes or EIO, never zeros |
| `t_marker_cache` | The marker cache against `INVALIDATE` and size changes, with marker reads held by a shim (`tests/slowxattr.c`, linked into a copy of the module): a read that saw `RESIDENT` before a stub never caches it after (`SEEK` pins); an unknown-fsid `INVALIDATE` drains pins before acking; an appended `RESIDENT` file reads; a read past a cached `PARTIAL` size of a grown file fails closed; truncate with an unreadable marker waits |
| `t_fill_fence` | The fill fence: truncate to 0 of a `PARTIAL` file waits while the daemon fills it, while disconnected, or before `RESYNC_END`; `cp` over a stub being filled ends up with exactly the new bytes |
| `t_trunc_guard` | Truncate to 0 next to the smb-guard: the marker is removed before the truncate (inotify order) for SETATTR and OPEN-truncate on v3 and v4.2; with a fanotify pre-content group that never answers for marked files, a stub whose fill FAILED still truncates (skipped with a NOTE on kernels without pre-content events) |
| `t_chunk_map` | M5 gate G1: map coverage with crafted maps on all four versions; broken maps fail closed; a minor-1 daemon leaves the prefix alone; 1,000 random reads of a 100 GiB remote object through chunk fetches (no zeros, no 5 s v3 retry); the 256-parked-reads cap; daemon `kill -9` during chunk fills; written-but-unmapped chunks never served |
| `t_pending` | The pending table at 10⁶ entries evicts its oldest (a new read still parks); a `FETCHING` entry outlives its `valid_ms` by over 60 s and the eviction, and still fences truncate |
| `t_threads` | The spike's threading regression: 32 stuck readers and a hung daemon do not slow another export |
| `t_snapshots` | Rewind trees over NFSv3 and v4 (`tests/nfsc.py`): `.snapshots` looks up but is not listed; LOOKUP and READDIR in an unfilled directory answer `DELAY` and send `POPULATE_DIR`, then list and read; every change below `.snapshots` is `EROFS`; with no `POPULATE_DIR`, the share's policy answers |
| `t_reload` | Shares added and removed in the portal: an `EXPORT` block added to an included file is served after SIGHUP (what `systemctl reload` sends), one removed is gone, and the others keep serving, over NFSv3 and v4 |
| `t_b5_throughput` | B5: resident reads cost the same as FSAL_NULL and VFS, and never reach the daemon |
| `t_caps` | Ganesha under the unit's capabilities, via `setpriv` rather than `--privileged`: works with `CAP_SYS_ADMIN`; without it, a CRIT log and fail-closed reads |

## CI

`.github/workflows/fsal.yml` runs on pull requests that touch `fsal/**` and on every push to `main`. It builds the image with buildx, caching it in the GitHub Actions cache (scope `fsal-ganesha`). It then runs `run.sh module`, `run.sh smoke` with `SMOKE_MOUNT=yes`, and `run.sh test`.
