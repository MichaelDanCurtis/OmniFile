# Filer spike, part B: FSAL_OMNI on NFS-Ganesha — results

Run 2026-09-26 on the local VM from `docs/pilots/filer-spike.md`: Lima `omnifile-spike`, Ubuntu 26.04 LTS arm64, kernel 7.0.0-28, 4 vCPU, 8 GB. Part A ran in the same VM at the same time, so wall-clock numbers are noisy (see B5).

- **Server:** NFS-Ganesha **V15.6** (tag commit `98eb4be`, 2026-09-25), built from source into `/opt/ganesha`. FSAL API `FSAL_MAJOR_VERSION 14`, `FSAL_MINOR_VERSION 0`. Ubuntu's own package is 6.5 and ships no headers.
- **Module:** `FSAL_OMNI`, a stacked FSAL copied from Ganesha's `FSAL_NULL` and placed over `FSAL_VFS`. It is built **out of tree** as `libfsalomni.so`. The code is in `spikes/filer/fsal/FSAL_OMNI/` and is LGPL-3.0-or-later. The guard itself is `omni_guard.c`, about 330 lines; the rest is renamed FSAL_NULL.
- **Daemon:** `spikes/filer/fsal/fake_daemon.py`. It answers over `/run/omnifile/fsal.sock`, and its recall copies bytes from `/srv/omni-b/backend/<key>`.
- **Storage:** `/srv/omni-b/data` is a 20 GB sparse xfs loop volume, with one export per policy: `wait`, `failfast`, `quick` (`waitquick:5000`), and `null` (upstream FSAL_NULL, used as a control). `/srv/omni-b/plain` is a 12 GB xfs loop volume exported through plain FSAL_VFS.
- **Test stubs:** 64 MiB files, of which the first 4 MiB are resident. Every 4 KiB block holds a non-zero word derived from its block number, so both zeros and misplaced blocks are detected. Every read was compared byte-for-byte against the backend object.
- **Clients:** the Linux NFS client in the same VM, over `127.0.0.1`, or over `192.168.5.15` for B3. The mount option was `sec=sys` with the default hard mount.

## Results

| # | Test | Verdict | Observation |
|---|---|---|---|
| B1 | Build and load | **PASS** | Ganesha V15.6 and out-of-tree `libfsalomni.so` build with no warnings. `FSAL { Name = OMNI; Policy = …; Socket = …; Timeout_Ms = …; FSAL { Name = VFS; } }` loads, and all 5 exports appear in `showmount -e`. One trap, now fixed: the module crashed Ganesha at startup until it was linked with `-ljemalloc` (see Build). |
| B2 | Stub read, per policy, NFSv3/4.0/4.1/4.2 | **PASS** + FINDINGS | **wait:** the reader blocks and then gets **correct bytes** on every version, with 0 bad blocks. **failfast:** the application gets `EIO` in 1–14 ms on every version. The client does not cache the error, and the same fd reads correctly once the recall finishes. **waitquick:5000:** a 3 s recall waits, a 10 s recall gets `EIO`. Header reads never contact the daemon, but client **readahead** past the headers does (details below). |
| B2x | Other read paths (NFSv4.2) | **PASS** (READ_PLUS untested) | `SEEK` on a stub reports the whole file as data, and no recall starts. The plain VFS export reports the hole at 4 MiB, which is what an unguarded server would tell `cp`/`tar`/`rsync`. A server-side `COPY` (`copy_file_range`) of a stub goes through the guard: correct bytes on wait, `EIO` with 0 bytes copied on failfast. `mmap` past the headers on failfast gives **SIGBUS**. READ_PLUS could not be exercised: Ubuntu's kernel is built without `CONFIG_NFS_V4_2_READ_PLUS`. |
| B3 | Caller identity, `sec=sys` | **PASS** + FINDING | Run as uid 2001 / gid 3001 from `192.168.5.15`. Both the Ganesha log and the daemon got `uid=2001 gid=3001 client=::ffff:192.168.5.15 nfs=3.0` and `nfs=4.2`, with the path and inode. **Exception:** in an NFSv4.2 `COPY` the daemon sees `uid=0 client=unknown nfs=0.0`, because Ganesha runs COPY on its `xcopy` worker threads with a blank `op_ctx`. |
| B4 | Daemon unreachable | **PASS** + FINDING | Tested with the socket missing (`ENOENT`), with a stale socket after `kill -9` (`ECONNREFUSED`), with a hung daemon (2 s timeout), and with a corrupt `state` xattr. In every case failfast gave `EIO`, wait gave `DELAY`, and **no zeros ever reached the reader**. A wait reader blocked for 12 s, then completed with correct bytes after the daemon restarted. Resident files, stub header ranges, `ls` and `stat` kept working, with no `ENOENT`. **Finding:** a hung daemon also slows **other exports** (see Threading). |
| B5 | Sequential read of a 2 GiB resident file | **PASS** (with caveat) | FSAL_OMNI, FSAL_NULL and plain VFS were indistinguishable within this VM's noise, which swings ±50% between runs. In one quiet run the NFSv4.2 buffered/direct results were: OMNI 3.5/2.4 GB/s, NULL 3.3/2.5 GB/s, VFS 3.8/2.8 GB/s. Ganesha CPU was about 0.3 s/GiB for all three. The guard's measurable cost is **3 extra syscalls per READ** (`open_by_handle_at` + `fgetxattr` + `close`), confirmed with `strace -c` over 256 × 1 MiB READs. The daemon was never contacted. |
| — | Immutable file, not a stub | **FINDING (bug)** | `chattr +i` on an ordinary file makes it unreadable through FSAL_OMNI. The reason is that FSAL_VFS opens the file `O_RDWR` to read an xattr, and that open fails with `EPERM`. The guard then fails closed, so failfast returns `EIO` and wait returns `DELAY` **forever**. `tests/xattr_open_mode.sh` reproduces it. |

### B2 in detail: Linux client behaviour (kernel 7.0.0-28)

| | NFSv3 | NFSv4.0 | NFSv4.1 | NFSv4.2 |
|---|---|---|---|---|
| Server reply for wait | `NFS3ERR_JUKEBOX` | `NFS4ERR_DELAY` | `NFS4ERR_DELAY` | `NFS4ERR_DELAY` |
| Client retry interval (READ) | **~5 s, fixed** | **~0.1 s, fixed** (0.103–0.15 s; no backoff seen) | same | same |
| Sequential read with a 10 s recall | one stall of 10.58 s at 4 MiB, then full speed; 4 daemon requests | stall 10.35 s; 178 requests | stall 10.09 s; 186 | stall 10.23 s; 190 |
| Single pread with a 3 s recall (waitquick) | **5.03 s**: rounded up to the next 5 s retry | 3.12 s; 27 requests | 3.26 s; 29 | 3.08 s; 28 |
| Blocked reader | `D` state, `wchan=folio_wait_bit_common`; SIGINT ignored, SIGKILL works | same | same | same |
| failfast: what the app sees | `read()` → `EIO`, 5 ms; `dd` prints "IO error: Input/output error" | `EIO`, 11 ms | `EIO`, 10 ms | `EIO`, 7 ms |
| failfast: error cached? | No. A retry 4 s later reaches the server again (`EIO`); after the recall the **same fd** reads correct bytes | No | No | No |
| COPY retry (sync op) | n/a | n/a | n/a | exponential, 0.2 → 0.4 → 0.8 s |

Nothing is printed to the user during a wait. `dd` just stops, and the kernel logs nothing. This matches the design assumption: the Teams card and the `.RECALLING.txt` note are the only way a user finds out.

**Readahead crosses the header boundary.** We read a stub through the page cache with 4 MiB resident:

| Data read | NFSv4.x | NFSv3 |
|---|---|---|
| 1, 2 or 3 MiB | no daemon contact | no daemon contact |
| the full 4 MiB | one readahead READ at offset 4 MiB (1 MiB, one `rsize`) | same |
| any amount, `O_DIRECT` | never | never |

That readahead READ goes to the daemon:
- On a **wait** share it starts a recall. The client then keeps retrying it every 100 ms in the background (about 20 requests in 2 s) **after the application has finished and closed the file**.
- On a **failfast** share it queues a recall, and the application sees nothing.

So a tool that only scans headers (`file`, SEG-Y header dumpers) can recall whole archived files if it reads right up to the resident boundary.

### B3 in detail

Here is the Ganesha log line for an NFSv3 stub read by uid 2001:

```
OMNI read stub: uid=2001 gid=3001 client=::ffff:192.168.5.15 nfs=3.0 path=/srv/omni-b/data/wait/id3.segy ino=150 off=8388608 len=65536 resident=4194304 policy=wait decision=delay rtt_us=7565
```

The daemon got the same fields as JSON. With `sec=sys` the uid is only what the client claims, so record it as claimed. Kerberos was deferred to the on-prem pilot.

The address arrives as `::ffff:a.b.c.d` because Ganesha listens on a dual-stack socket. The daemon should normalise it before any AD or Teams lookup.

### B4 in detail

| Daemon state | failfast | wait | Log |
|---|---|---|---|
| socket missing | `EIO` in 1 ms | blocks (`D`); correct bytes after restart (v3: 20.8 s total, v4.2: 15.5 s) | `unreachable (No such file or directory) -> eio(fail-closed)` / `delay(fail-closed)` |
| killed, stale socket | `EIO` in 1 ms | (same path as above) | `unreachable (Connection refused)` |
| hung (accepts, never answers) | `EIO` after **4.1 s** (two 2 s timeouts: the demand READ and a readahead READ) | `DELAY` after 2 s each time | `unreachable (Connection timed out)` |
| `state` xattr is garbage | `EIO`, daemon not asked | `DELAY`, daemon not asked | `cannot read stub xattrs … (errno 22); failing closed` |

Resident files, a full 64 MiB read, and header reads were correct with no daemon at all. In the 12 s while the daemon was down, a single v4.2 wait reader produced 122 fail-closed log lines, one per 100 ms retry, so the log needs rate limiting.

The fanotify second layer (part A) was not stacked under this test. Part A's report covers it.

### Threading: a slow daemon hurts unrelated exports

`tests/threads_hung_daemon.sh` puts 32 readers on 32 stubs of the wait share (NFSv4.2) and meanwhile measures O_DIRECT 1 MiB reads of a resident file on the **plain VFS export**:

| Condition | Plain-export reads in 8 s | p50 | p99 | max | Ganesha threads |
|---|---|---|---|---|---|
| no stub readers | 17,984 | 0.3 ms | 2.1 ms | 17 ms | — |
| 32 stub readers, daemon answering `delay` at once | 6,720 | 0.8 ms | 7.6 ms | 74 ms | 51 |
| 32 stub readers, daemon **hung** (2 s timeout) | **768** | 1.1 ms | 22.7 ms | **1,817 ms** | 82 |

Two things show up:
- **The retry storm costs something even when the daemon is healthy.** Each waiting READ comes back every 100 ms, and each retry costs a socket round trip plus xattr reads.
- **A blocked socket call stalls the Ganesha RPC thread that made it,** and it delayed unrelated reads by up to 1.8 s. Ganesha's thread count (82 of the default `RPC_Ioq_ThrdMax` 200) was not exhausted.
  - The likely cause is that the client's single connection or session to the server is shared by all its mounts, so stuck requests hold its session slots. This is a hypothesis; it was not confirmed with more than one client.

## Build instructions

The one-shot scripts are `spikes/filer/fsal/setup_volumes.sh` and `spikes/filer/fsal/build.sh`, run inside the VM as root.

1. **Ganesha.** Clone `https://github.com/nfs-ganesha/nfs-ganesha.git` at tag `V15.6` with `--recurse-submodules`, which brings libntirpc.
   - `cmake` needs `libssl-dev`, which is not in the usual dependency lists. We turned off every FSAL except VFS and NULL, plus monitoring, 9P, LTTng, RADOS and `_MSPAC_SUPPORT`, then ran `make -j4 && make install` to `/opt/ganesha`. It takes about 50 s on 4 vCPU.
   - Create `/opt/ganesha/var/lib/nfs/ganesha` yourself. Without it, the `fs` recovery backend aborts startup.
2. **FSAL_OMNI, out of tree.** Run `make -C FSAL_OMNI GANESHA_SRC=<src>/src GANESHA_BUILD=<build dir> GANESHA_LIB=/opt/ganesha/lib install`.
   - The build needs the Ganesha **source tree** (`src/include`, `include/os/linux`, `libntirpc/ntirpc`) and the **build tree** (the generated `include/config.h`). `make install` installs only ntirpc headers, and no distro ships a Ganesha dev package.
   - The module is installed as `/opt/ganesha/lib/ganesha/libfsalomni.so`. `Name = OMNI` loads `libfsal<name>.so` from `FSAL_Library`.
3. **Link with `-ljemalloc`,** exactly as Ganesha does. Without it:
   - the module binds `calloc@GLIBC_2.17`;
   - Ganesha's jemalloc `malloc_usable_size()` then segfaults on the first `gsh_calloc()` in `create_export`.
   - It was found from a core file; `gdb` cannot attach live in this VM.
4. **ABI.** The module must be built against the **same Ganesha version and the same `config.h` options** as the daemon that loads it:
   - `register_fsal()` checks only the FSAL API version (14.0);
   - struct layouts such as `gsh_client` change with options like `ENABLE_QOS`.

   Ship the module and Ganesha as one versioned build, and do not rely on distro Ganesha.

## Production notes

- **Return `DELAY` immediately; never block a worker.** A `wait` share must answer `ERR_FSAL_DELAY` at once while the recall runs asynchronously in the daemon. The spike's daemon already works that way.
  - The FSAL's synchronous socket call is the weak point: the threading test shows a slow daemon stalls unrelated traffic.
  - Production FSAL_OMNI should keep a per-handle "recall pending until T" state in memory. It would answer the 100 ms retries itself, and talk to the daemon only on the first miss and on a change of state. The daemon exchange should be asynchronous, or have a timeout of 50–100 ms rather than seconds.
  - Blocking for a whole recall is not an option: recalls take hours, and NFS clients retransmit and time out.
- **Retry load and polling cadence.**
  - The Linux 7.0 NFSv4 client retries a delayed READ every ~100 ms with no backoff. With the spike FSAL, one waiting reader with readahead meant ~20 daemon calls/s, so 1,000 users waiting would mean 20,000/s.
  - NFSv3 retries every 5 s. `waitquick` times, and "file ready" latency on v3, are therefore rounded up to 5 s.
- **Per-READ cost.**
  - The stacked design reads `user.omnifile.state` through `FSAL_VFS`'s `getextattr_value_by_name()`. That call opens the file `O_RDWR` by handle, then `fgetxattr`s and `close`s it: 3 syscalls per READ.
  - At `rsize` 1 MiB the cost was invisible, but it grows as `rsize` shrinks.
  - More importantly, the `O_RDWR` open is what breaks immutable files (the bug above). It would also break files on read-only mounts, which were not tested.
  - The fix is to read the xattrs with `fgetxattr` on the fd VFS already holds for the READ, which takes a small VFS-aware hook rather than pure stacking, or to cache the state per handle.
  - The cache would be invalidated by an upcall from the daemon (`fsal_up` invalidate) when a file becomes a stub or resident.
- **Guard every data path, not just READ:**
  - `seek2` must report a stub as all data. Otherwise NFSv4.2 SEEK tells sparse-aware tools the archive is zeros.
  - `read2` must never return `NOTSUPP` for a stub. Ganesha's READ_PLUS fallback would then call `seek2` and emit a HOLE segment.
  - `copy_file_range` must **not** be passed to FSAL_VFS for stubs. In-kernel `copy_file_range` on xfs copies the hole. The spike leaves the op at Ganesha's default, which is a buffered copy through our `read2`.
  - pNFS must stay off, as FSAL_OMNI registers `FSAL_ID_NO_PNFS`. Otherwise clients would read data servers directly.
  - `Drop_Delay_Errors` must stay `false`, which is the default.
- **Identity gaps.** In server-side COPY, Ganesha's `xcopy` workers give no caller uid and no client address. The recall still happens, but it cannot be attributed to a person or sent to Teams. The options are:
  - disable COPY offload on OmniFile shares;
  - accept a "copy by unknown" recall;
  - patch Ganesha to carry creds into the copy job.
- **Readahead-triggered recalls.** Make the resident region at least one `rsize` (1 MiB) larger than the headers that tools scan, or have the daemon not recall on a single READ that starts exactly at `resident_bytes`. Deciding this needs the SEG-Y header sizes from the data test.
- **Recall writes behind Ganesha.** The fake daemon writes the file directly and restores `atime`/`mtime`. `ctime`, and so the NFSv4 change attribute, still changes on recall, which makes clients drop cached pages. That is harmless, but it is visible to tools that watch `ctime`.
- **Logging.** Log header-range reads at DEBUG, not EVENT, and rate-limit fail-closed warnings (see B4).
- **Paths.** The FSAL learns a best-effort path from LOOKUP, READDIR and OPEN-create. Renames make it stale, and handles rebuilt from the wire have none. The daemon should key on `(fsid, inode)` or `user.omnifile.key`, both of which the FSAL sends, and treat the path as a hint.

## How to re-run

As root in the VM (`limactl shell --workdir / omnifile-spike -- sudo bash -c '…'`), with Ganesha installed and the volumes mounted:

```
/work/spikes/filer/fsal/tests/b2_read_stub.sh 3 4.0 4.1 4.2   # ~6 min
/work/spikes/filer/fsal/tests/b2_extras.sh                    # SEEK, COPY, mmap, attrs
/work/spikes/filer/fsal/tests/b3_identity.sh
/work/spikes/filer/fsal/tests/b4_daemon_down.sh
/work/spikes/filer/fsal/tests/b5_throughput.sh                # RUNS=5 by default
/work/spikes/filer/fsal/tests/threads_hung_daemon.sh          # N=32 stuck readers
/work/spikes/filer/fsal/tests/xattr_open_mode.sh              # immutable-file bug
```

The scripts start and stop Ganesha (`/opt/ganesha/bin/ganesha.nfsd -f /srv/omni-b/ganesha.conf`, log at `/srv/omni-b/ganesha.log`) and the fake daemon (log at `/srv/omni-b/daemon.log`, one JSON line per request). `tests/dstats.py <since>` summarises the daemon log. Ganesha is left stopped. Part A's `/srv/omni-a` and `smbd` were not touched.

## Not tested, and open risks

- **READ_PLUS.** It is guarded in code (the guard sits in `read2` before the sub-FSAL, and `seek2` is guarded for the fallback), but the client kernel does not send READ_PLUS. It needs a client built with `CONFIG_NFS_V4_2_READ_PLUS`, or a synthetic client.
- **Kerberos, AD/winbind uid mapping, Windows and macOS NFS clients, 200 GB files, and remote backends.** These are deferred to the on-prem pilot per the plan.
- **Several clients at once.** All clients here shared one kernel. The head-of-line effect in the threading test needs re-measuring with separate client machines. The fix (never block) stands either way.
- **Delegations and client caching across recall.** Not studied: whether a read delegation on a stub, or cached header pages plus a `ctime` change, could confuse applications.
- **Stale stub state in MDCACHE.** There is none today, because the guard re-reads the xattr on every READ. A production per-handle cache introduces the risk and needs the upcall invalidation described above.
- **Immutable and read-only files.** Blocked by the `O_RDWR` xattr open until the FSAL reads xattrs itself (see Production notes).
- **`wait` with no daemon means forever.** Correct and fail-closed, but a wait share with a permanently dead daemon hangs every reader in an unkillable-by-Ctrl-C `D` state; only SIGKILL frees them. Monitoring must alarm on "daemon down" well before users notice.
- **Stacked on VFS or standalone?** The spike shows stacking on VFS works for local stubs. The migration namespace (S3/Azure-backed files with no local inode) still needs either a standalone FSAL or a VFS placeholder per object. That decision is outside part B.
