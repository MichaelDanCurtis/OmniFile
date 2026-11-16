# WP3: chaos, grace, edge scale

Branch `claude/m7-chaos-grace`. Everything ran on the dev edge VM (`omnifile-edge`: Ubuntu 26.04, kernel 7.0.0-42, raised to 8 vCPUs and 48 GiB), with the dev AD (`omnifile-dc`) for SMB.

## Where the runs kept their data

The Mac's internal disk backs the VM's disk, and Time Machine snapshots keep every block the VM writes there. So `tests/chaos/storage.sh on` moved the write-heavy state off it for every run below:
- **In RAM (tmpfs):** Postgres's data directory, the journal (`Storage=volatile`), `/var/lib/omnifile` and `/var/tmp`.
- **On the repo mount:** the data and cache volumes became sparse loop files, xfs with the same UUIDs, in `/work/dev/.artifacts/wp3/`. That mount is virtiofs, on the Mac's external SSD.

The xfs sits on the loop device, so fanotify, `trusted.*` xattrs and hole punching behave the same. Throughput figures include virtiofs.

## 1. NFS grace reclaim (`tests/e2e/nfs_grace_test.sh`)

The test runs as root on an NFS client against any edge. `--restart-cmd` is the only thing that changes between a Ganesha restart, an appliance reboot and an A/B-update reboot; WP2 can reuse it as it is.

It runs three kernel NFS clients. Each has its own network, UTS and mount namespace, with its own rpcbind and rpc.statd, so the server sees three machines:
- **A** holds the state. It has 8 NFSv4.1 opens, each with an fcntl write lock, 4 read-only opens, and 4 NFSv3 fcntl locks (NLM). A is cut off for 20 s after the restart, so B asks before A can reclaim.
- **B** wants the same locks: before the restart, during grace and after it.
- **C** rides the restart on NFSv3 and NFSv4.1 hard mounts, with three writers.

Reclaims and GRACE answers are counted from the kernel's `nfs4` and `lockd` tracepoints.

`tests/chaos/grace_dev.sh` runs it on the dev VM, with the server on the same machine and the appliance's base config (`edge/config/ganesha/omnifile-base.conf`). Final runs were at b07042a, with the new RecoveryRoot:

| run | checks | opens reclaimed | v4.1 locks reclaimed | B answered GRACE / DENIED | NFSv3 locks | grace | server back |
|---|---|---|---|---|---|---|---|
| `systemctl restart nfs-ganesha-omni` | 23/23 | 12/12 | 8/8 | 96 / 16 | SM_NOTIFY reached A; B refused 8, granted 4 once A let go | 90 s | 23 s |
| SIGKILL, restarted by systemd | 23/23 | 12/12 | 8/8 | 96 / 16 | the same | 90 s | 5 s |
| the same base with NLM off | 19/19 | 12/12 | 8/8 | 72 / 16 | not tested (no NLM) | 30 s | 2 s |

- **In every run:**
  - B was never granted a lock A held: not while A was cut off, not in grace, not after.
  - A's first reclaim came after it rejoined, and after B's first GRACE answer.
  - A's 16 fds kept working: pwrite, fsync and pread inside each lock. A lost NFSv4 lock makes them fail with EIO.
  - Once A let go, B got every lock.
- **The riders saw no error and no wrong byte.**
  - NFSv3 writes through new opens, and NFSv4.1 writes through a held fd, waited at most for the restart: 3 s after SIGKILL, 21 s after a clean restart.
  - NFSv3 truncates waited out grace: 95–113 s with NLM on, 34 s with it off. Ganesha answers an NFSv3 SETATTR of the size with JUKEBOX while in grace (`nfs3_setattr.c:139`).
- **The clean restart took 21 s to stop Ganesha.** Its worker threads waited on client A, which was cut off. A SIGKILL restart is back in 5 s.
- **Delegations: there were none to hold or reclaim.** Ganesha grants them only where the FSAL supports leases. FSAL_VFS doesn't (`lease_op2` is the default "not supported", `fs_info.delegations` is 0), and FSAL_OMNI is stacked on it. `NFSV4 Delegations` also defaults to false. The test counts delegations (0 granted) and reports this as a note. If FSAL_OMNI ever supports leases, the same test checks their reclaim.

## 2. NFSv3 locking (NLM)

**The owner confirmed NLM on (2026-11-04).** It is the default in the appliance's base config, and the grace test checks it.

There are two separate commits. `ab5611a` sets `Enable_NLM = true` and `NLM_Port = 32803` in the appliance's base config; main has since taken the same lines with fixed MOUNT and statd ports. `b07042a` names `RecoveryRoot = "/var/lib/nfs/ganesha"`. The dev-VM pieces are in `tests/chaos/lib/devstack.sh` (`nlm_server_on`) and `dev/lima/omnifile-edge.yaml`.

**RecoveryRoot (corrected after review):** the appliance's own Ganesha build already defaults to `/var/lib/nfs/ganesha`, so on the appliance the line is redundant, not a fix. The dev build, with prefix `/opt/ganesha`, defaults to `/opt/ganesha/var/lib/nfs/ganesha`, and that is where its records were. Naming the path makes every build keep them in the same persistent place. I first reported that Ganesha would fail to start on the appliance; that was wrong for the appliance build.

NFSv3 locks work across a Ganesha restart and a SIGKILL: acquired, refused to a second client, reclaimed (the counts are above).

**What NLM needs next to Ganesha, which the appliance must add (WP2):**
- **Packages:** `nfs-common`, for rpc.statd and sm-notify. The appliance has rpcbind only. nfs-common is GPL-2 and runs as separate processes.
- **Units:**
  - `rpcbind.socket` and `rpc-statd.service` enabled.
  - A drop-in on `nfs-ganesha-omni.service`: `Wants=rpc-statd.service`, `After=rpc-statd.service`, and `ExecStartPre=-+/usr/sbin/sm-notify -f`.
  - The `+` is needed. sm-notify drops its own privileges (PR_CAPBSET_DROP), which fails under Ganesha's CapabilityBoundingSet, which has no CAP_SETPCAP. It then exits having moved every client's record to `sm.bak` and notified none.
  - Measured without the `+`: the restarted server told no one, A's NFSv3 locks were not reclaimed, and after grace B was granted them.
- **Why sm-notify runs at every start:** Ganesha's first act at start is `SM_UNMON_ALL`, which makes statd forget every client. Without sm-notify, a Ganesha restart, not just a reboot, loses every NFSv3 lock silently.
- **State that must persist across reboots and A/B updates:** `/var/lib/nfs`, which holds statd's `sm/`, `sm.bak/` and `state`, and Ganesha's `ganesha/v4recov` and `v4old`.
- **Ports:** 111 tcp/udp (rpcbind), 2049, 32803 tcp/udp (NLM), and rpc.statd's port. statd's port is random unless `/etc/nfs.conf [statd] port=` sets it; 662 is the usual one. The MOUNT port (`MNT_Port`, random today, 20048 usual) matters for NFSv3 whether or not NLM is on. Outbound, the appliance must reach clients' rpcbind and statd for SM_NOTIFY.
- **DNS:** rpc.statd looks up the name of every client it is asked to monitor (reverse DNS). If that lookup takes longer than Ganesha's SM_MON timeout (about 9 s here), every NFSv3 lock fails with ENOLCK.
  - On the dev VM, systemd-resolved also asked by LLMNR and mDNS on the test bridge and left statd's PTR query unanswered for 10 s (strace).
  - The appliance should turn LLMNR and mDNS off (`resolved.conf`: `LLMNR=no`, `MulticastDNS=no`; also a known spoofing risk in AD networks), and needs a DNS that answers clients' PTR queries quickly, even if only with NXDOMAIN.
- **The cost:** with NLM on, Ganesha cannot end grace early, because it can't tell when every NLM client has reclaimed (`nfs_try_lift_grace`). Every restart therefore holds new NFSv4 opens and locks, and NFSv3 truncates, for the whole 90 s. With NLM off, grace ended 30 s after the restart in the same test, once every client had reclaimed.
- **The dev base (`dev/edge/ganesha-base.conf`, not WP3's)** stays graceless and without NLM: the e2e lanes mount NFSv3 with `nolock`.

## 3. Chaos soak (`tests/chaos/soak.sh`, `soak.py`)

`soak.sh` sets up the M2 stack. Ganesha runs on the appliance's base config, with grace and NLM on, and smbd serves the `smbwait` share for the dev AD. The worker stubs files at once, and archived files are recalled. `soak.py` then runs load and faults together.

- **Faults:** eight types, on a schedule drawn from `--seed`:
  - SIGKILL the edge daemon;
  - SIGKILL Ganesha;
  - SIGKILL smbd (Ubuntu's unit has no `Restart=`, so the soak starts it again after a 5–25 s hold);
  - SIGKILL the service worker;
  - crash Postgres: either the whole cluster is SIGKILLed and started again after 5–30 s, or one backend is SIGKILLed and Postgres restarts every backend itself;
  - fill the data volume with fallocate for 30–90 s;
  - drop every packet between the edge daemon and the API and the sim for 20–60 s (half the time the outage's flows stay dead afterwards);
  - restart the daemon in the middle of a 96 MiB fill, with readers waiting.
- **Load:** writes over NFSv3, 4.1 and 4.2 and SMB (create, overwrite, delete). O_DIRECT reads of stubs over NFS, and SMB reads, with 60% of files larger than the 1 MiB resident head. Files are archived as M2's tests do, so reads of them are recalls.
- **Invariants, checked continuously:**
  - no block of zeros where the file has data, and no other wrong byte or length;
  - an existing file is never ENOENT, nor missing from its folder's listing over NFS or SMB.
- **Invariants, checked at the end:**
  - every write that returned success is committed with its CRC-64/NVME, and the backend's bytes have its SHA-256;
  - deleted files stay deleted;
  - every recall is `available`;
  - every unit is active again within 180 s of each fault.
- **Allowed:** errors, which are counted by fault (failing closed is the design).
- **Replay:** the seed fixes the schedule and every load decision (`--seed N --duration D --each K`).

**Short run** (seed 2013365296, 30 min, every fault twice; `tests/e2e/results/chaos-short.md`):
- **Reads:** 1,406 good reads, 367 of them across unfilled regions and 159 of archived files; 29 recalls, all `available`.
- **Writes:** 238 committed with matching CRC and backend SHA-256; 26 deletes stayed deleted.
- **Violations:** 0 zeros, 0 wrong bytes, 0 ENOENT.
- **Errors (allowed):** EIO and NT_STATUS_IO_DEVICE_ERROR only while the disk was full, ENOSPC on writes then, and connection refusals while smbd was down.
- **One lost commit, a real bug** (routed to WP9). `wait/soak/d2/f00162.bin` was closed 0.9 s before a daemon SIGKILL, inside the 3 s quiet period. It was never committed: no marker, no catalog row, not in the outbox.
  - In `edge/crates/omnifile-edge/src/nsync.rs:497–500`, reconcile_dir, including its sweep at start, leaves a regular file the replica doesn't know to the write detector.
  - The detector's close event dies with the process.

**On main's daemon, stopped at 55 min** (seed 2843486889; `tests/e2e/results/chaos-asis-55min.md`): 13 faults, 0 zeros, 0 wrong bytes, 0 ENOENT, and 3 lost commits. Each file was closed 1.7–2.5 s before the daemon stopped:
- `wait/soak/d7/f00255.bin`, a new file over NFSv3, then a SIGKILL;
- `smbwait/soak/s00300.bin`, a new file over SMB, then a clean `systemctl restart`;
- `wait/soak/d4/f00172.bin`, an overwrite over NFSv4.1, then the same restart. The catalog kept the older version.

The run was stopped there so that the 6 h soak runs on WP9's fix (#42).

## 4. A8 at scale (`tests/scale-edge/a8_scale.sh`)

The A-suite's `a08_flood.sh` (WP2's) ran unchanged through its `run.sh`, with its volume in RAM. Two limits were raised for the flood's shell: the descriptor limit, from 1024 to 1,048,576, and the process limit (`ulimit -u`, nproc), from 176,972 to unlimited. pid_max (4,194,304), threads-max (353,945) and max_map_count (1,048,576) were already enough on a 46 GiB VM. Results are in `tests/e2e/results/scale-edge-a8.md`.

| readers | zeros while the guard was stopped | zeros after the drain | completed OK | drain | A8b |
|---|---|---|---|---|---|
| 4,000 | 0 | 0 | 4,000 | 2 s | pass |
| 8,000 | 0 | 0 | 8,000 | 2 s | pass |
| 17,000 | 0 | 0 | 17,000 | 3 s (about 5,700 answers/s) | pass |

- **The drain is not the R6 risk on this kernel.** All 17,000 readers were held while the guard was stopped. The flood tool's own clock put the whole drain at 2 s, against the spike's 45 answers/s at 17,000 waiters.
- **A8b**, at LimitNOFILE=1024 with 3,000 readers: 0 zeros, and the guard never exited. 2,992 readers got an error, which is failing closed, as designed.

## 5. Line-rate ceiling (`tests/scale-edge/line_rate.sh`)

One 10 GiB file, on the dev VM. Client and server were on the same VM, the data volume was a loop file on virtiofs (the Mac's external SSD), and the backend was the Python sim, which holds every blob in RAM. Results are in `tests/e2e/results/scale-edge-linerate.md`.

| stage | MB/s |
|---|---|
| NFSv4.1 write, through fsync | 386 |
| upload: fsync to committed (quiet period, hash, upload, commit) | 169 |
| the worker's verification read | 516 |
| recall: the stub read over NFSv4.1 with O_DIRECT, the fill from the sim behind it | 214 |

- **The sim is the likely ceiling here.** These are local figures, not line rate; the 200 GB SEG-Y run is pilot 2, at a client site.
- **The run was stopped after the recall read,** when the Mac's swap passed the lead's limit: the sim held the 10 GiB blob about twice. It was not rerun at 10 GB, at the lead's request. The file was deleted.
- **A partial version (edge finding, routed to WP9):** in the first run, the daemon committed a 4,682,940,416-byte content version while the client was still writing, then the full 10 GiB one, 7 s after fsync.
  - WP9 traced it to the detector's design: a file with no writes for a whole quiet period (here a client writeback stall) is committed while still open, so that NFSv3 writes held open by Ganesha still commit.
  - Three 4 GiB repeats with an inotify watch did not reproduce it. They did show one read-only open and close of the file on the edge per MiB written (4,098 for 4 GiB).

## 6. Environment notes (the dev VM)

- **Stale drop-ins:** `30-m5-aws.conf` on omnifile-dev-worker, -api and -web, left by an `install-m5.sh` that main does not have, pointed at an AWS accounts file whose keys main's code rejects. The worker crash-looped on it: 163 restarts, so no stubs, verification or recalls. They are renamed `*.wp3-disabled`.
- **Migration 0033 (WP4's, now being fixed):** it fails for the dev VM's non-superuser `omnifile` role ("permission denied to set parameter omnifile.seed_bypass"). As a VM-only stopgap, the role was granted `SET ON PARAMETER omnifile.seed_bypass`.
- **Kernel:** the VM booted kernel 7.0.0-42 after its restart (it was 7.0.0-38 before).

## 7. The 6.75 h soak, on WP9's fix (`tests/e2e/results/chaos-6h-wp9.md`)

The run used this branch merged with WP9's lost-commit fix (#42, round 4, 9a8433a), daemon sha256 cbb913e2. Seed 3125631393: 22,500 s planned, 24,320 s run, with 96 faults planned, 12 of each. Replay: `sudo tests/chaos/soak.sh --seed 3125631393 --duration 22500 --each 12`.

**The report is RED:** 4 recalls were still `downloading` at the end, and `restart_mid_fill` reached 8 of its 12. Every other invariant held.

- **Faults:** kill_edge, kill_ganesha, kill_smbd, kill_worker, crash_postgres, fill_disk and drop_network each ran 12 times. restart_mid_fill ran 8 times; 4 were skipped because no 96 MiB stub was ready within 300 s, since re-stubbing the 4 big files after a fill took longer than the gap. Every unit was active again right after each fault. SIGKILLed daemons, Ganesha and the worker had a new main pid within 1.3, 2.4 and 2.1 s.
- **Reads:** 24,930 good reads: 2,367 across unfilled regions and 7,190 of archived files. 0 zeros, 0 wrong bytes, 0 ENOENT, in reads and in listings over NFS and SMB.
  - **Caveat, found in review:** "0 zeros" is proven only for reads that completed. In this run, a read that saw zero blocks and then failed reported only its error: `soak_io.py` dropped its counts, and a failed SMB download was not checked. So for all 73 error reads (57 NFS EIO, 16 SMB), "no zeros" is unproven, including the 31 NFS and 6 SMB errors with no fault active.
  - Both gaps are fixed now (section 9). The next soak checks them.
- **Writes:** 1,817 writes succeeded. At the end, every live file (322) was committed with its CRC-64/NVME and its backend bytes' SHA-256, and 989 deletes stayed deleted. **0 lost commits**, against 1 in 30 min and 3 in 55 min on main's daemon.
- **Recalls:** of 613 recalls, 609 were `available`. **4, ids 235, 273, 274 and 598, were still `downloading` at the end.** The 30 min settle with the load stopped did not move them.
- **Errors (failing closed, allowed):**
  - while the disk was full: 25 NFS EIO, 8 SMB NT_STATUS_IO_DEVICE_ERROR, 19 ENOSPC and 4 NT_STATUS_DISK_FULL writes;
  - while smbd was down: 45 connection refusals;
  - **with no fault active: 31 NFS EIO (13 v3, 11 v4.1, 7 v4.2) and 6 SMB NT_STATUS_IO_DEVICE_ERROR.**

**Findings, routed by the lead, not fixed here:**
- the 4 recalls stuck in `downloading`;
- **smbd has no `Restart=`** (Ubuntu's unit), so after a crash SMB stays down until something starts it; the soak restarts it itself;
- the 31 EIO reads (and 6 SMB errors) with no fault active.

## 9. Fixes from #48's review

`tests/chaos/test_soak.py` covers each fix and needs no VM: `cd service && uv run pytest ../tests/chaos/test_soak.py -q` gives 16 passed. That is 15 tests, with the grace self-test run once under each bash on the machine (macOS's 3.2 and Homebrew's 5.3).

How each test was shown to fail without its fix:
- the first 11 tests, against the files as they were before the first round of fixes;
- the 5 added after the re-review, by reverting just their fix (the SMB `prefix` call, the contender's per-attempt file, `ns_down`'s guard, the check in `main()`, the per-protocol split).

| fix | test |
|---|---|
| `soak_io.py` counts blocks as it reads and, on an OSError, reports the zero and bad blocks seen so far with the errno | `test_a_read_that_sees_zeros_then_fails_reports_the_zeros` |
| `judge_read` takes zeros and wrong bytes before any error, and before a "rewritten during the read" discard | `test_judge_read_puts_zeros_and_wrong_bytes_before_errors` |
| `soak_io.py prefix` checks a partial file over its own length | `test_a_partial_download_is_checked_over_its_length` |
| `read_one`: a failed SMB `get` has its partial file checked with `prefix`, so zeros in it are a violation (smbclient stubbed) | `test_a_failed_smb_read_has_its_partial_file_checked`, `test_a_failed_smb_read_with_good_bytes_is_only_an_error` |
| a run must reach `--minimums` to be GREEN | `test_a_run_that_exercised_nothing_is_red` |
| the minimums are per protocol, so a run whose SMB reads all fail cannot be GREEN on NFS alone | `test_the_minimums_are_per_protocol` |
| a bad `--minimums` (unknown key, not a whole number) is refused in `main()` before the run starts, not in the report at its end | `test_bad_minimums_are_refused_before_the_run` |
| ENOENT on deleting a file the soak wrote and saw committed is an `enoent` violation, over NFS and SMB | `test_enoent_on_deleting_a_written_file_is_a_violation` |
| a kill with no new main pid within 120 s is a `recovery` violation | `test_a_kill_that_never_gets_a_new_pid_is_a_violation` |
| recovery is timed from the kill itself | `test_recovery_is_timed_from_the_kill` |
| a `cancelled` recall of a file that still exists is a `recall` violation | `test_a_cancelled_recall_of_a_file_still_there_is_flagged` |
| the replay line gives `--warmup`, `--cooldown`, `--files` and `--big` too | `test_the_replay_line_names_everything_the_schedule_depends_on` |
| `nfs_grace_test.sh`: a rider must report `done`, so a hung one fails | the self-test, "a rider that never finished fails" |
| each of B's in-grace attempts writes its own `att.ingrace.N.json` as it returns, so a lock granted early is seen at the 20 s check (before, that check read a file written only once every attempt had ended, so it could not fail) | the self-test runs the real contender on local files: "the contender writes each attempt's answer…" and "granted_so_far counts the contender's own answers" |
| `ns_down` never acts on a pid in the host's own network namespace | the self-test calls `ns_down` itself, with `kill`, `ip`, `ps` and `in_ns` stubbed to record calls: "ns_down leaves a pid in this shell's own namespace alone" |
| the script's private copy in `/tmp` removes itself | `test_the_grace_scripts_self_test_passes_and_leaves_no_copy` |
| `storage.sh off` honours a custom BIGDIR, in any argument order (without one, it acts on the directory the volumes are on) | `test_storage_off_honours_a_custom_bigdir` |

The default minimums are listed below; pass `--minimums` to change any of them.

| | writes | reads | unfilled reads | archived reads | listings |
|---|---|---|---|---|---|
| NFS (v3, v4.1 and v4.2 together) | 50 | 100 | 20 | 10 | 10 |
| SMB | 20 | 50 | 10 | 10 | 5 |

Recalls (5) are counted by the catalog, whichever protocol started them. The 6.75 h soak's writes, reads and recalls were all far above these defaults; the smallest was SMB writes, at 250. It did not yet split listings by protocol (2,634 in all).

To call `ns_down` and the contender from the self-test, the script now defines them, and the `grace.py` heredoc (`write_grace_py`), before the self-test. The real run writes `grace.py` at the same point as before.

## 8. Regression lanes

These ran on this branch merged with origin/main at 20a8d21, on the dev VM, as plain runs of the committed lanes with `E2E_RESULTS` outside the repo:

| lane | result |
|---|---|
| M1 `tests/e2e/exit_test.sh` | 83/83 |
| M2 `tests/e2e/m2_exit_test.sh` | 107/111, twice (virtiofs and the VM's own disk) |
| M3 `tests/e2e/m3_exit_test.sh` | 110/110 on the VM's own disk (109/110 on virtiofs: one failfast read's timing) |
| M5 `tests/e2e/m5_exit_test.sh` | 139/139 |

- **M2's four failures are main's, not this branch's.** They are all in step 10, and nothing this branch changes is in M2's path. After a daemon SIGKILL, the new daemon fences a held SMB fill before Ganesha's FSAL has reconnected ("connections: 0"). It fails the fill, and the readers get EIO.
- **M2's history:** the last recorded pass, `tests/e2e/results/m2-latest.md`, is from 2026-10-06. The fix is WP15's.
- **Not rerun after the final merge.** The lanes were not rerun after the final merge of origin/main (dbf8cc3), because the VM went to WP15. The merge adds no code in the lanes' paths beyond what main already carries.

## Tools installed

None. Everything used was already on the Mac and the dev VM: shellcheck, docker, limactl, nfs-common, rpcbind, nftables and strace in the VM.

## The dev VM afterwards

- **Running, at 8 vCPUs and 20 GiB.** It was raised to 48 GiB for A8 and the line-rate test, then lowered.
- **Storage:** `tests/chaos/storage.sh off --delete` put it back on its own disk and deleted the big loop files.
- **`/work`:** this worktree.
- **What stays changed:** the stale `30-m5-aws.conf` drop-ins (set aside as `*.wp3-disabled`) and the `SET ON PARAMETER` grant for 0033.
