# Filer spike, part A: results (fanotify stub guard, SMB path)

Tests A1–A13 of `docs/pilots/filer-spike.md`, run on 2026-09-26.

**Environment:** Lima VM `omnifile-spike`, Ubuntu 26.04 LTS arm64, kernel `7.0.0-28-generic` (4 KiB pages), systemd 259.5, Samba 4.23.6, 4 vCPU and 8 GB. The data volume is xfs on a 20 GB sparse loop image, mounted at `/srv/omni-a/data`. The fake backend is the directory `/srv/omni-a/backend`, plus a synthetic pattern generator for a sparse 200 GB stub.

**Code:** `spikes/filer/guard/` (throwaway):
- `src/main.rs`: the guard daemon, about 1,000 lines of Rust using `libc` only.
- `src/bin/omnitool.rs`: test helpers. It writes pattern files and stubs, verifies reads by several methods, and runs floods.
- `tests/*.sh`: one script per test.
- `systemd/` and `samba/`: the units and the share configuration.

The test data uses a non-zero pattern that depends on position (`u64 at offset o = o ^ "OMNIFILE" ^ seed`), so every zero page and every misplaced block is detected.

**Kernel citations** are from upstream `v7.0` (`torvalds/linux` tag). Every cited behaviour was also observed on the Ubuntu 7.0.0-28 kernel.

## Verdict

**The SMB design can be made fail-closed on kernel 7.0, but only with the complete set of measures in "Recommended design" below.** The hypothesis in the plan held: while the group is alive, readers never saw zeros. The stored fd, the journal, the unit ordering and `FAN_UNLIMITED_QUEUE` together kept the group alive through every crash tried: kill -9 during a fill, 10 kills in a stream, restart, daemon-reload, daemon-reexec and the start limit.

The spike also found problems the plan did not anticipate:
- **Five ways to get zeros or an outage** even with a working daemon:
  - the systemd fd store is garbage-collected when the unit is not referenced;
  - `mremap()` growth bypasses the guard;
  - a whole-file reflink clone arrives as a `(0,0)` event;
  - hitting `RLIMIT_NOFILE` crashed the first version of the daemon;
  - with `BindsTo=` and no `RestartMode=direct`, a guard crash takes smbd down.
- **A silent kernel no-op:** an ignore mark added through the event fd does nothing.
- **A drain that slows as waiters pile up:** the cost of answering an event grows with the number of waiting readers, O(N).

## Results

| # | Test | Result | What happened |
|---|---|---|---|
| A1 | Resident header read | **PASS** | Reads of `[0,1 MiB)` and `[4096,+3600)` of a stub with 1 MiB resident came back correct in ≤ 21 ms, with no fill: the fill counter did not change. Each `read()` still makes one event round trip, which the daemon's fast path allows. |
| A2 | Past the headers, **wait** | **PASS** | A 2 MiB read at 20 MiB blocked for 2.004 s (backend delay 2 s), and the bytes were correct. A whole-file sequential read of the 2 GiB stub was filled and verified in 1.6–8.5 s; the xattrs then became `state=resident`, `resident_bytes=2147483648`. Fills are aligned to 8 MiB chunks and only fill holes. |
| A3 | **Fail fast** | **PASS** | `EIO` came back in about 1 ms through `FAN_DENY_ERRNO(EIO)`. A `RECALL-QUEUED key=… range=… pid=… comm=… path=…` line was logged. A retry during the recall got `EIO`; after the background recall it read correctly. `cat` failed with `Input/output error`. |
| A4 | **Wait only if quick** (`waitquick:1500`) | **PASS** | With an ETA of 500 ms the read waited and was correct (0.506 s). With an ETA of 3000 ms it got `EIO` at once. |
| A5 | kill -9, no fd holder (baseline) | **FINDING: fails open, as expected** | A reader blocked in a fill **got zeros** 2.1 s after the kill. After an idle kill, the next read **got zeros** at once. |
| A6 | kill -9 **with** the systemd fd store | **PASS, with conditions** | Everything below passed, but only because the unit was enabled, which stops systemd garbage-collecting it: see the `systemctl stop` bullet. Details follow the table. |
| A7 | Group released with events pending | **FINDING: ALLOW** | Every pending permission event is answered **ALLOW** when the last group fd closes. That covers events read but unanswered (`fanotify_user.c:1123`, `fanotify_release()` walking `access_list`) and events still queued (`fanotify_user.c:1140`). Events raised later pass too: the group is shut down, so `fsnotify_insert_event()` returns 2 (`notification.c:95`) and the access proceeds. Zeros were observed for a paused daemon (queued), a stalled daemon (read, unanswered), a clean SIGTERM exit and `systemctl clean --what=fdstore`. Reachability is covered in the A7 bullet below. |
| A8 | Queue flood (> 16,384) | **PASS with `FAN_UNLIMITED_QUEUE`; FINDING without** | 17,000 readers against a paused daemon. **Without** the flag: exactly **616 = 17,000 − 16,384 readers got zeros at once** and `FAN_Q_OVERFLOW` was queued (`notification.c:101` returns 2, then `fanotify.c:1004` sets `ret = 0`, which allows the access). **With** the flag: 0 zeros and all 17,000 blocked. Later findings below. |
| A9 | File opened **before** the mark | **FINDING, as expected** | An fd opened while the filesystem had no pre-content group **stays unguarded for its lifetime** and read zeros (A9a, A9c). The kernel chooses the fd's mode at `open()` (`fsnotify.c:612`: *"Later, fsnotify permission hooks do not check if there are permission event watches, but that there were permission event watches at open time"*). An fd opened while group 1 existed **was guarded by group 2** after group 1 died (A9b PASS). |
| A10 | `mmap()` of a stub | **PASS, with one FAIL (mremap)** | Details follow the table. |
| A11 | Through Samba (smbclient and a local CIFS mount) | **PASS with `RestartMode=direct`** | Details follow the table. |
| A12 | Identity over SMB | **PASS** | The event pid is the **smbd child process** (`comm` `smbd[127.0.0.1]`). Samba's pthreadpool worker threads share its thread-group id, and the event pid is the tgid. While the request waits, `smbstatus -p` maps that pid to `user=geo2 group=geo2 machine=127.0.0.1 proto=SMB3_11`; a CIFS-mount read mapped to `geo1`. The lookup must run while the request is pending; the session disappears when the client disconnects. |
| A13 | Overhead on resident files | **PASS with ignore marks; FINDING without** | Throughput table below. A11 of the plan says to add the ignore mark on filled inodes, and the first attempt **silently did nothing**; details follow the table. |

### A6 in detail

- **kill -9 during a fill.** The reader was blocked in a 15 s fill. The restarted daemon took the group back from the fd store and found the event in its journal: `ORPHAN event fd=6 … fd number reserved`. It reopened the file by file handle through its private fill mount, filled it and answered. The reader got **correct bytes** 5.2 s after the kill.
- **Reads while no daemon runs** (group held only by systemd) blocked in `fanotify_get_response`, state `D`, then completed correctly.
- **10 kills 1 s apart during a stream of 61 reads:** 61 OK, 0 zeros, 0 errors.
- **`systemctl restart`, `daemon-reload` and `daemon-reexec`** with a reader waiting: all correct, and the fd store survived each one.
- **An event read but not journalled** (simulated with `--no-journal`): the restart sweep answered it `DENY(EIO)`, so the reader got `EIO` (fail-closed) and a retry was correct.
- **Crash loop until the start limit:** the unit went `failed` and the fd store was **kept**. Local readers blocked; `reset-failed` plus `start` reused the group and the reader completed correctly.
- **`systemctl stop`:** the fd store survived **only while the unit was enabled**. With the unit neither enabled nor referenced, `stop` closed the store (NFileDescriptorStore 1 → 0), and the next reader **got zeros** (A6c-1). Once the unit was enabled, a reader waited 20 s through the stop and completed correctly after `start`.

### A7: can a release happen while smbd runs?

With smbd running, a release is refused or preceded by stopping smbd:
- `systemctl clean --what=fdstore` refuses while the guard is active: *"Unit is not inactive or has pending job"*.
- Stopping the guard stops smbd first (`smbd` Stopping/Stopped, then the guard).
- Local (non-SMB) readers blocked at that moment still get zeros on release.

### A8: draining the flood

- **Drain speed:** after resuming, the drain completed 5,774–6,407 reads in 130 s, all correct. The drain is slow because of the O(N) wake-up described under "Other kernel facts".
- **fd exhaustion** (daemon at `RLIMIT_NOFILE=1024`):
  - The first version exited on `read()` = `EMFILE`, which released the group: **16,994 readers got zeros**.
  - After the fix (back off, never exit, cap in-flight events below the fd limit): **0 zeros**, and 30 readers got errors. The kernel denies (`EPERM`) any event it cannot open an fd for (`fanotify_user.c:1041`), and the rest got `EIO` from fills that hit `EMFILE`.

### A10 in detail

- **The hook sits in `mmap()` and covers the whole mapped range** (`mm/util.c:577`, `fsnotify_mmap_perm()`); 7.0 has no fsnotify call in `mm/filemap.c` or `mm/memory.c`:
  - a 16 MiB map at 100 MiB was one event `[100 MiB,+16 MiB)`, filled inside `mmap()` (1.0 s), and the page faults were correct;
  - a map of the whole 2 GiB stub was **one event for 2 GiB**, filled before `mmap()` returned (7.5 s);
  - under failfast, `mmap()` itself fails with `EIO`;
  - a map of the whole 200 GB stub is one 200 GB event (`EIO` under failfast).
- **FAIL:** a resident 1 MiB head mapped with `mmap()` and grown with `mremap()` to 1 GiB **read zeros**. `mm/mremap.c` has no fsnotify hook.
- **Other read paths were checked:** `pread`, `O_DIRECT`, `sendfile`, `splice` and `copy_file_range` (same filesystem) were all guarded.
- **Reflink clones:** `cp --reflink=always` (FICLONE, whole file) arrives as range **`(0,0)`**. `remap_verify_area()` passes `len=0` (`remap_range.c:118`), which looks exactly like a truncate to 0. The first daemon treated `(0,0)` as a truncate, and the **clone read zeros**. After the fix (`(0,0)` on a stub is a whole-file access), the clone is filled first under wait and fails with `EIO` under failfast.
- **`O_TRUNC` raises no pre-content event at all:** the truncate hooks sit only in `truncate(2)` and `ftruncate(2)`, `fs/open.c:92` and `:191`.

### A11 in detail

- **Clients:** cifs.ko `vers=3.1.1,cache=none`, and `smbclient -m SMB3`.
- **wait:** both read correct bytes; smbclient's 64 MiB and 2 GiB copies were byte-identical (`cmp`) to the backend object.
- **Long wait:** a **90 s recall succeeded through cifs.ko** (90.008 s).
- **STATUS_PENDING:** smbd sends an SMB2 **STATUS_PENDING interim response about 1.4 ms after each READ** (250 in the capture), then the final response when the fill completes.
- **smbclient's default timeout** (`-t 20`) ends any recall longer than 20 s with `NT_STATUS_IO_TIMEOUT`, reported only when the data arrives (seen at 25, 45 and 60 s). With `-t 120` it works.
- **failfast:** cifs.ko returns `EIO`; smbclient shows `NT_STATUS_IO_DEVICE_ERROR`.
- **Guard kill -9 during two waiting SMB reads:**
  - `Requires`+`BindsTo`+`After` alone: **smbd was stopped and not restarted**, and the client got `EHOSTDOWN` (112) after 10–12 s;
  - `Requires`+`After`: **smbd was restarted**, and the client got `EAGAIN` (11);
  - **either variant plus `RestartMode=direct` on the guard:** smbd kept the same PID, both reads **completed with correct bytes** (12.1 s and 9.1 s), and the guard reused the group.
- **Ordering:** `systemctl start smbd` pulls in the guard. smbd started 116 ms after the guard's `READY=1`, and READY is sent only once the mark is armed. On `systemctl stop omni-guard-a`, smbd is stopped before the guard, and the fd store is kept.

### A13 in detail

Sequential read of a resident 4 GiB file, warm page cache, median of 5 runs, MB/s:

| Mode | bs=1M | bs=64K | bs=4K |
|---|---|---|---|
| No pre-content group | 14,000 / 17,000 | 10,000 / 11,000 | 3,600 |
| sb mark, no ignore mark | 6,800 | 1,900 | **76** |
| sb mark + inode ignore mark | 17,000 | 11,000 | 3,300 |

The first figure in a pair is the first run, the second the re-run. Cold-cache numbers are not meaningful here: the VM disk is cached by the host.

**Why the first ignore mark did nothing:** the event fd is `O_RDWR`, so the inode counts as open for write. In that case `do_fanotify_mark()` **returns 0 without adding** an ignore mark that lacks `FAN_MARK_IGNORED_SURV_MODIFY` (`fanotify_user.c:2117-2118`). Two ways work:
- **(a)** After closing the event fd, reopen the file read-only through the fill mount and add a plain `FAN_MARK_IGNORE`. The kernel clears it on the next modification, so a re-stub through a normal fd is guarded again: the A13-3 re-stub read was correct.
- **(b)** `FAN_MARK_IGNORE_SURV` through the event fd. This one survives modification, and **after a re-stub the read returned zeros**. The daemon must remove it before re-stubbing.

## Other kernel facts verified (7.0)

- **Responses are matched by the fd number only.**
  - `process_access_response()` walks `access_list` and takes the **first** event whose `event->fd == fd` (`fanotify_user.c:494`). It does not check which process read the event, so a successor process can answer its predecessor's events (A6a).
  - Two pending events can share a number: one read by the dead instance, one by the new instance. The response then goes to the older one. The spike reserves orphan numbers with `dup2(/dev/null)` before it reads new events.
- **Waiters sleep killable** (`TASK_KILLABLE|TASK_FREEZABLE`, `fanotify.c:232`). A blocked reader shows state `D`; only SIGKILL frees it, and SIGKILL cancels the event.
- **The permission watchdog** (`fs.fanotify.watchdog_timeout`, default 0) only logs a warning.
- **Custom deny errnos are limited** to `EIO, EPERM, EBUSY, ETXTBSY, EAGAIN, ENOSPC, EDQUOT` (`fanotify_user.c` `process_access_response()`).
- **Pre-content groups cannot report file handles** (`fid_mode && class != FAN_CLASS_NOTIF` gives `EINVAL`). The journal therefore takes a handle with `name_to_handle_at(event_fd, "", AT_EMPTY_PATH)`.
- **Truncate to a non-zero length** is reported as `(new_len rounded down, 0)` (`fsnotify.h:198`). Ranges are always page-aligned (`fsnotify.c:166`).
- **The event fd is `FMODE_NONOTIFY`.** A `pwrite` fill through it causes no recursion. Reads and writes through a mount carrying a `FAN_MARK_MOUNT|FAN_MARK_IGNORE_SURV` mark also produce no events, even though the sb mark covers that mount: every fill through the private fill mount relied on this.
- **The drain slows as waiters pile up (O(N)).** Every waiter sleeps on the single `group->fanotify_data.access_waitq`, and each answer wakes all of them (`fanotify_user.c:499`). Pure-allow drain, measured with 2 s granularity:

  | Waiting readers | Drain time | Answers/s |
  |---|---|---|
  | 250 | under 2 s | — |
  | 1,000 | 1.5 s | about 670 |
  | 4,000 | 11.7 s | about 340 |
  | 17,000 | — | about 45 |

## Recommended design: fail-closed SMB guard

1. **One group per data filesystem:**
   - Initialise it with `FAN_CLASS_PRE_CONTENT | FAN_UNLIMITED_QUEUE | FAN_CLOEXEC`, event flags `O_RDWR|O_LARGEFILE`.
   - Mark it with `FAN_MARK_FILESYSTEM`, `FAN_PRE_ACCESS`.
   - Check that the mark path really is the data volume (fs UUID, and `RequiresMountsFor=`). Otherwise an unmounted path marks the **root** filesystem.
2. **The unit** (`spikes/filer/guard/systemd/omni-guard-a.service`):
   - `Type=notify`, sending `READY=1` only after the mark is armed.
   - `FDSTORE=1`, `FDNAME=fanotify`, `FDPOLL=0` on first creation.
   - `FileDescriptorStorePreserve=yes`, `Restart=always`, **`RestartMode=direct`**, and a high `LimitNOFILE`.
   - **The unit must stay enabled (referenced)**; otherwise stopping it garbage-collects the store.
   - Treat `disable --now`, `clean --what=fdstore` and removing the unit as dangerous operations.
3. **smbd drop-in:** `Requires=`, `BindsTo=` and `After=` on the guard. With `RestartMode=direct`, a guard crash no longer touches smbd, while an explicit stop or a permanent failure (start limit) still stops smbd first. The same goes for Ganesha.
4. **If the store is ever lost and a new group is created while servers run, restart the servers.** Every fd opened without a group is unguarded for its lifetime (A9). The daemon can detect this: it knows whether it reused the group or created one.
5. **Journal every event** (`fd, ino, range, pid, file handle`) in one `write()` per batch, before acting on any of it. On restart, before reading new events:
   - replay the journal;
   - reserve orphan fd numbers;
   - reopen orphans by handle through the fill mount, and fill and answer them;
   - sweep fd numbers 0..N with `DENY(EIO)` to catch any event read but not journalled.

   A plain `write()` is enough; no `fsync` is needed, because the kernel's events do not survive a reboot either.
6. **A private mount namespace for the daemon:** a bind mount of the volume carrying a `FAN_MARK_MOUNT|FAN_MARK_IGNORE_SURV` mark. Use it for orphan fills, recall writes and re-stubbing, and add it again after every restart (it is a new mount object each time). No other process can see it.
7. **Never exit on `EMFILE`/`ENFILE`.** Back off, and cap in-flight events below the fd limit. An event whose fd cannot be created is denied by the kernel (`EPERM`, which fails closed); a daemon that exits releases the group.
8. **Ignore marks on filled inodes** are essential for resident performance (A13). Add a plain `FAN_MARK_IGNORE | FAN_MARK_EVICTABLE` through a read-only fd from the fill mount, after closing the event fd. Before re-stubbing a file, remove its ignore mark explicitly; state changes go through the daemon only.
9. **Event semantics:**
   - `(0,0)` on a stub is a whole-file access (clone, or truncate to 0), so apply the policy to the whole file;
   - `(L>0, 0)` is a truncate: allow it;
   - huge ranges (`mmap` of the whole file, clone, `copy_file_range`) need a policy of their own, for example failfast above a size, rather than a silent 200 GB wait.
10. **Clients:** smbd sends STATUS_PENDING, so cifs.ko waits indefinitely (90 s tested). Windows behaviour must be checked on-prem. smbclient-style clients with per-operation timeouts will fail on long recalls; this favours **failfast** for interactive shares.
11. **Identity:** at event time, map the pid through the smbd session (smbstatus / messaging) and SID. Consider `FAN_REPORT_PIDFD` so that pid reuse cannot mislabel an event.

## What turned out impossible, or impossible to close from userspace

- **`mremap()` growth of a mapping** reads holes with no event: a kernel gap, to report upstream. Samba does not `mmap` share files, so SMB is not exposed. Local processes on the appliance are, so no user workloads may run on the filer.
- **fds opened while no group exists** are never guarded. The only mitigation is ordering plus restarting the servers.
- **Releasing the group always means ALLOW for everything pending.** It can only be avoided by never releasing the group while anything reads.
- **An event read but not journalled** (a crash in the microseconds between `read()` and the journal `write()`) cannot be answered correctly. It can only be denied (`EIO`) by the sweep, or left hanging.
- **`O_TRUNC` is invisible to pre-content events.** A stub overwritten with `O_TRUNC` keeps stale `state=stub` xattrs pointing at the old backend object. A later sparse write followed by a read of the hole would be "filled" with **old data**. The fix needs a separate notification watcher (`FAN_MODIFY`/`FAN_CLOSE_WRITE`) or a size or generation check.
- **The daemon cannot tell a whole-file clone from a truncate to 0**: both are `(0,0)`. Treating both as a whole-file access is safe, but `ftruncate(fd,0)` on a stub then triggers a recall, or `EIO` under failfast.

## Open risks

1. **Unit garbage collection and fd-store handling** are operational single points of failure: the unit must stay enabled, and `clean`, `disable` or an uninstall must be interlocked. A crash loop that reaches the start limit stops smbd (fail-closed, but an outage).
2. **The O(N) wake-up makes large stalls drain slowly** (about 45 answers/s with 17,000 waiters). Samba bounds the waiters with its pthreadpool (`aio max threads`, default 100 per smbd process), but a busy filer with many sessions can accumulate thousands.
3. **Very large ranges.** An `mmap`, clone or `copy_file_range` of a 200 GB stub is a single event, and the reader waits for the whole file.
4. **Client timeouts on long recalls:** smbclient fails after 20 s by default, and Windows is untested.
5. **Stale stub state after `O_TRUNC`**, and a recall on `ftruncate(0)`.
6. **Kernel API maturity.** Pre-content events are new in 6.14–7.0 (the page-fault hook was reverted, and ignore-mark semantics are subtle), and Ubuntu may backport changes. The spike's citations are from the upstream v7.0 source; the Ubuntu kernel's behaviour matched in every test.
7. **Overhead without ignore marks is large** (4 KiB reads fell 3,600 → 76 MB/s). If eviction drops ignore marks (`FAN_MARK_EVICTABLE`), the per-read round trips return until the file is read again.
8. **The spike's single-threaded reader** was fast enough here: 17,000 events were read in a few seconds. Fill concurrency, per-chunk deduplication and backpressure are spike-grade.

## Not tested, and why

- **Real AD, winbind, Kerberos and Windows clients (Explorer, and Windows' STATUS_PENDING handling):** deferred to the on-prem pilot, as the plan says.
- **Remote backends** (Azure/S3 range reads): the backend is a local directory with simulated latency (`state/delay_ms`, `delay_scope=file|fill`).
- **The interaction with Ganesha on the same filesystem** (fanotify as NFS's second layer) is part B.
- **Behaviour under memory pressure** (eviction of ignore marks, or of the journal's page cache) was not exercised.
- **io_uring reads** were not run. The source shows they pass through `rw_verify_area()` (`io_uring/rw.c:947`).

## How to reproduce

Inside the VM, as root. The repo is mounted at `/work`.

```
cd /work/spikes/filer/guard && CARGO_TARGET_DIR=/srv/omni-a/target cargo build --release
# data volume: truncate -s 20G /srv/omni-a/xfs.img; mkfs.xfs /srv/omni-a/xfs.img; mount -o loop … /srv/omni-a/data
cp systemd/omni-guard-a.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable omni-guard-a
bash tests/a1_a4_policies.sh; bash tests/a5_a7_failopen_baseline.sh; bash tests/a6_fdstore.sh
bash tests/a6c_stop_preserve.sh; bash tests/a6g_systemd_ops.sh; bash tests/a6h_startlimit.sh
FT=150 bash tests/a8_flood.sh; bash tests/a8d_drain_rate.sh; bash tests/a9_preopened.sh
bash tests/a10_mmap_and_paths.sh; bash tests/a10b_clone_cfr.sh
bash tests/a11_a12_samba.sh; bash tests/a11b_samba_followup.sh; bash tests/a11c_restartmode.sh; bash tests/a12_a7_misc.sh
bash tests/a13b_bench.sh; bash tests/a13_overhead.sh
```

**State left in the VM:**
- The guard unit and smbd are installed but **stopped and disabled**. They were disabled because at boot the loop volume is not mounted, and the guard would then mark the root filesystem.
- The fd store is empty.
- Test users `geo1` and `geo2` exist (local Samba accounts; test-only passwords are in the scripts).
- The volume is still mounted at `/srv/omni-a/data`.

To stop everything: `systemctl stop smbd omni-guard-a && systemctl clean --what=fdstore omni-guard-a`. Stopping alone keeps the group, and every access to the volume then blocks.
