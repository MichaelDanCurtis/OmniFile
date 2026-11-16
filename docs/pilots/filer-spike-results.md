# Pilot 1: filer spike — combined verdict (local run, 2026-09-26)

The plan is in `docs/pilots/filer-spike.md`, and the detailed results in `filer-spike-results-A.md` (SMB/fanotify guard) and `filer-spike-results-B.md` (NFS/`FSAL_OMNI`). Both ran on a Lima VM: Ubuntu 26.04 LTS arm64, kernel 7.0.0-28, xfs on loop devices. The code is in `spikes/filer/`.

## Verdict

**The stub design holds on both protocols: no reader saw zeros in any tested configuration of the recommended design.** The SMB guard gets there only with every measure below. Without them it fails open in exactly the ways the plan predicted, and the spike reproduced each one.

| Protocol | Result |
|---|---|
| **NFS** (Ganesha V15.6 + `FSAL_OMNI`) | B1–B5 pass. Wait gives `NFS4ERR_DELAY`/`NFS3ERR_JUKEBOX`, then correct bytes, on v3, 4.0, 4.1 and 4.2. Failfast gives `EIO` in 1–14 ms. A missing, stale or hung daemon fails closed. The caller's uid and client IP reach the daemon. Resident throughput is no different from plain Ganesha. |
| **SMB** (Samba over the fanotify pre-content guard) | A1–A4, A6, A8 and A11–A13 pass with the recommended design. smbd sends STATUS_PENDING, and the Linux CIFS client survived a 90 s recall. The event pid maps to the SMB session user. The guard survived `kill -9` (including 10 in a row), restarts, daemon-reload and daemon-reexec, and the queue survived a 17,000-reader flood. |
| **Fail-open cases, confirmed** | Releasing the fanotify group answers every pending read "allow", so readers get zeros (A5, A7). Without `FAN_UNLIMITED_QUEUE`, overflow allows reads: 616 of 17,000 got zeros (A8). A file opened before any group existed is never guarded (A9). |

## What the build must do (changes to the design)

**SMB guard (Rust daemon plus systemd):**
1. **The group must outlive the daemon.** Hold the fanotify fd in the systemd fd store (`FileDescriptorStorePreserve=yes`). The unit stays **enabled**, because a disabled unit's store is garbage-collected on stop. Use `Restart=always` and `RestartMode=direct`. Treat `disable`, `clean --what=fdstore` and uninstall as dangerous operations.
2. **smbd and Ganesha** use `Requires=`, `BindsTo=` and `After=` on the guard, which reports `READY` only once its mark is armed on the right filesystem (checked by fs UUID). If the group ever has to be recreated, restart the servers.
3. **Use `FAN_UNLIMITED_QUEUE`,** and never exit on `EMFILE`: an exit releases the group.
4. **Journal every event before acting on it.** A restarted daemon answers its predecessor's events, which the kernel matches by fd number. It denies (`EIO`) any event it cannot account for.
5. **Fill through a private bind mount** that carries an ignore mark, and add **ignore marks on filled inodes** through a read-only fd from that mount. Without them, small reads of resident files fall from 3,600 to 76 MB/s. An ignore mark added through the event fd silently does nothing.
6. **Event semantics:**
   - `(0,0)` means a whole-file access (a clone, or truncate to 0);
   - huge ranges (mmap, clone, `copy_file_range`) get a size-capped policy;
   - a separate `FAN_MODIFY` watcher or a generation check catches `O_TRUNC` overwrites, which raise no pre-content event and leave stale stub state.
7. **No user workloads on the filer.** Growing a mapping with `mremap()` reads holes with no event (a kernel gap; Samba doesn't `mmap`).

**NFS FSAL:**
1. **Never block a Ganesha worker thread on the daemon.** With the daemon hung, reads on an unrelated export fell from about 18,000 to 768 in 8 s. The FSAL keeps its own table of pending recalls and answers `DELAY` from memory, asking the daemon only on a first miss.
2. **Read xattrs without an `O_RDWR` open.** FSAL_VFS's xattr path makes `chattr +i` files unreadable (bug reproduced).
3. **Make the resident region at least 1 MiB (one `rsize`) larger than the headers,** or client readahead turns a header scan into a whole-file recall.
4. **Server-side `COPY` runs with a blank identity** (uid 0, no client). It is guarded, but the Teams card can't be addressed; attribute the recall from the COPY's originating request.
5. **Ship Ganesha and the FSAL as one build:** same source tree, same version, same build options, linked with `-ljemalloc`. No distribution ships Ganesha headers.

**Both:**
- A blocked reader ignores Ctrl-C (`D` state); only `kill -9` frees it.
- NFSv3 retries every 5 s and NFSv4 about every 0.1 s.
- smbclient-style clients time out after about 20 s.

All of this favours **failfast** for interactive shares, and **wait** only for batch clusters, as designed.

## Still to prove on-prem

- Real AD, winbind and Kerberos (`sec=krb5`).
- Windows clients: STATUS_PENDING handling and Explorer thumbnailing.
- Remote backends (Azure/S3 range reads and the chunk cache).
- Line-rate reads of real SEG-Y files.
- Behaviour under memory pressure (ignore-mark eviction).
- NFS READ_PLUS: Ubuntu's client doesn't send it.
