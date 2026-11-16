# Pilot 1: filer spike — plan and local scope

Pilot 1 in `docs/PILOTS.md`, run first on a local VM: Lima on Apple silicon, `omnifile-spike`, Ubuntu 26.04 LTS arm64, kernel 7.0.0-28, 4 vCPU, 8 GB, xfs on loop devices. The repo is mounted at `/work`. Results go in `docs/pilots/filer-spike-results.md`. Spike code lives in `spikes/filer/` and is throwaway: nothing there is product code until it is rewritten into `edge/` or `fsal/`.

## Why a stub guard is dangerous

A stub is a full-size sparse file. It carries a `user.omnifile.*` xattr state, and only its first N MB (the headers) are resident. **A hole reads as zeros.** Two layers stop zeros reaching a user:
- **NFS:** `FSAL_OMNI` checks the stub state before reading, and answers "wait" or "fail" itself.
- **SMB:** Samba reads the file directly, so the Rust daemon catches the read with fanotify pre-content events (`FAN_PRE_ACCESS`, class `FAN_CLASS_PRE_CONTENT`).

The fanotify layer also sits under Ganesha's own reads, so it defends NFS too.

fanotify fails **open**. If the listening group goes away, pending permission events are answered "allow" (to be confirmed on kernel 7.0), and new reads are no longer intercepted.

## Hypothesis: the guard fails closed if the group never dies with the daemon

1. **Hold the group outside the daemon.** The daemon hands its fanotify fd to systemd's file-descriptor store (`FDSTORE=1`, with `FileDescriptorStorePreserve=yes`). If the daemon crashes, the group survives:
   - new reads queue and **block** instead of returning zeros;
   - the restarted daemon takes the fd back and drains the queue.
2. **Answer the crashed daemon's events.** A response is matched by the event's fd *number*. The daemon journals `(fd number, inode, range)` before acting, so its replacement can answer events that were read but not answered.
3. **Nothing serves files without an armed guard.** `smbd` (and later Ganesha) `Requires=`, `BindsTo=` and `After=` the guard unit. The guard reports ready only once its filesystem mark is armed, so the servers start after arming and stop before the group is released.
4. **No silent drops.** The group uses `FAN_UNLIMITED_QUEUE`; a full queue must never become "allow".

## What the local spike must show

| # | Test | Pass condition |
|---|---|---|
| A1 | Read a stub's resident header range | Served locally; no recall event |
| A2 | Read past the headers, policy **wait** | Reader blocks, the daemon fills from a fake backend (a local directory), and the reader gets the **correct bytes** |
| A3 | Same, policy **fail fast** | Reader gets `EIO` at once (`FAN_DENY_ERRNO(EIO)`); a recall is queued |
| A4 | Policy **wait if quick** | Wait under the threshold, `EIO` over it |
| A5 | `kill -9` the daemon with **no** fd holder (baseline) | Record what happens. Expected: zeros, the danger we are designing out |
| A6 | `kill -9` the daemon **with** the systemd fd store | No reader ever sees zeros. Readers block, and complete correctly after restart |
| A7 | Group released with events pending | Record allow or deny on 7.0; the unit ordering must make this unreachable while servers run |
| A8 | Queue flood (more than 16,384 pending) | No "allow" by overflow, with `FAN_UNLIMITED_QUEUE` |
| A9 | File opened **before** the mark was armed | Record whether it is guarded (expected: not); the startup order covers it |
| A10 | `mmap()` of a stub | The whole mapped range is filled at `mmap()` time, and page faults never see zeros |
| A11 | Through **Samba** (smbd plus a local `smbclient` / CIFS mount) | Wait, fail fast and daemon-kill all behave as in A2–A6. Record the NTSTATUS the client sees, and whether long waits send STATUS_PENDING |
| A12 | Identity over SMB | Map the event's pid to the SMB session user with `smbstatus` |
| A13 | Overhead on resident files | Read throughput with the mark on, before and after an ignore mark on filled inodes |
| B1 | Build NFS-Ganesha and a stacked `FSAL_OMNI` (from `FSAL_NULL`) over `FSAL_VFS` | Loads and exports the xfs volume |
| B2 | NFS read of a stub | **wait** gives `NFS4ERR_DELAY` / `NFS3ERR_JUKEBOX`; **fail fast** gives `EIO`. Record client behaviour for NFSv3, 4.0, 4.1 and 4.2 |
| B3 | Caller identity | The FSAL logs the caller uid and client IP (`sec=sys`) |
| B4 | Daemon socket down | The FSAL fails closed (`EIO` or `DELAY`, never zeros), with fanotify as a second layer |
| B5 | Resident reads | Full-speed sequential read of a filled file through NFS |

## Deferred to the on-prem pilot

These need the client's environment, not a laptop VM:
- real AD, winbind and Kerberos (`sec=krb5`);
- Windows clients and Explorer behaviour;
- a real 200 GB SEG-Y at line rate (the local spike uses a sparse 200 GB stub plus a few GB of real data);
- remote backends (Azure/S3 range reads and the chunk cache). The local spike's backend is a directory.
