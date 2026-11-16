# Regression on main, 2026-11-08

Main at **c2bf369** (#85). This was the first end-to-end run on main since owner decisions 63 to 67 and #76 to #85 merged. No product code was changed for it.

**The outcome: every lane passed except the chaos soak, which is RED.** The soak found two product issues, both open (below, "The soak"). The regression also found three test and tooling bugs, each fixed in its own PR and merged since:
- #86: a race in an edge test;
- #87: the Mac's npm toolchain, M4's silent `npm ci`, and a browser test's locator;
- #89: a lane that left stubbing off for the next.

## Results

| Lane | Where | Result | Expected |
|---|---|---|---|
| M1 `tests/e2e/exit_test.sh` | omnifile-edge | **83 passed, 0 failed** | 83 |
| M2 `tests/e2e/m2_exit_test.sh` | omnifile-edge | **111 passed, 0 failed** | 111 |
| M3 `tests/e2e/m3_exit_test.sh` | omnifile-edge | **110 passed, 0 failed** | 110 |
| M5 `tests/e2e/m5_exit_test.sh` | omnifile-edge | **139 passed, 0 failed** | 139 |
| #54's `tests/e2e/large_copy_test.sh` | omnifile-edge | **16 passed, 0 failed** | 16 |
| Chaos soak `tests/chaos/soak.sh` | omnifile-edge | **RED**: 1 recall violation, and `restart_mid_fill` injected 8 of 12 times. Zeros, wrong bytes, ENOENT, lost commits and recovery were each 0 | GREEN: 6 h 15 min, every fault 12 times |
| Edge `cargo clippy --workspace --all-targets -- -D warnings` | omnifile-edge, as root | **clean** | |
| Edge `cargo test --workspace --no-fail-fast` | omnifile-edge, as root, unguarded xfs | **964 passed, 1 failed, 1 ignored**: the failure is the test race below | |
| M4 `tests/e2e/m4_exit_test.sh` | this Mac | **service 15/15; browser 9/10**: the failure is a test locator, below | 15 and 10 |
| Permission matrix `tests/acl/matrix.sh` | omnifile-acl, with omnifile-dc | **PASS 130, FAIL 0**, plus the 1 documented FINDING (`chmod 644`) | 130 |
| Service `uv run pytest -q` | this Mac, own Postgres 17 | **2704 passed, 0 failed, 51 skipped** | |
| Portal `npm test` | this Mac | **335 passed in 39 files** | |

The virtual clock (`/var/lib/omnifile-dev/clock-offset`) read **+0 before and after every lane**.

## Failures and intermittent tests

**Edge: a race in a test, fixed in #86.**
- **The test:** `daemon_linux::a_share_from_the_feed_is_rendered_reloaded_and_its_root_given_its_groups` failed once in the full run, with `assertion failed: read(&ganesha_conf).contains("Pseudo = \"/proj\";")`.
- **The race:** the test waited until `smb.conf` showed the share, then read `ganesha.conf` at once. The share loop (`shares.rs`, `sync_once`) replaces the two files one after the other, in one blocking task, and reloads the servers only after both. So a read between the two writes saw the old Ganesha file.
- **Test-only:** nothing outside the test reads `ganesha.conf` between the writes. Ganesha reads it only through its `%include`, at start, SIGHUP or restart, and all of those come after both writes.
- **Before the fix:** it failed 2 of 7 times in full parallel workspace runs on this VM (this regression and one of #82's lanes), and passed 7 of 7 times run alone, 5 of them in this regression.
- **After the fix (#86, merged since):** it passed 20 of 20 times in a loop on the VM, between lanes. The regression's copy was put back to main's file afterwards.

**M4 browser: a test locator, fixed in #87.**
- **The failure:** `proposals.e2e.mjs`, "a DataSteward proposes a change and an ArchiveAdmin approves it", failed with "strict mode violation: getByLabel('Archive after') resolved to 2 elements".
- **The cause:** the policy timeline's handle, added in #46, has a similar label. wp23 saw the same clash in `policy.e2e.mjs`, which now passes. The product is fine.
- **Runs:** it failed once in one full M4 run.
- **The fix:** #87 (wp27), with the npm finding below. Merged since.

**M4 on this Mac: the npm toolchain.**
- **What happened:** M4 stopped twice at "portal build" with no output, because the script runs `npm ci --silent`.
- **The cause:** under bash, PATH finds `~/.local/bin/npm` 10.9.8, and npm 10 refuses main's `portal/package-lock.json` with "Missing: msw@2.15.0 from lock file". `@vitest/mocker`'s optional peer `msw` ^2 clashes with the portal's `msw` 3.0.2, and npm 11 accepts it.
- **The run that counts** used nvm's node 24 and npm 11.19.0 first on PATH.
- **The fix:** #87: `npm ci` passes under npm 10 and 11, and M4 fails loudly. Merged since.

**Set-up mistakes of mine, then redone:**
- **The soak's first attempt** stopped in its set-up at "AD credentials". `soak.sh` reads `ad.env` from its own repo copy, and the git archive has no secrets. It ran with `AD_ENV_FILE=/work/dev/.secrets/ad.env` instead.
- **The matrix's first run** gave PASS 116 and 5 PENDING. It couldn't build the smb-guard harness itself: cargo, run as the VM's user, got "Permission denied" in a root-owned `/var/tmp/acl-matrix`. I built the harness into my own directory under `/mnt/ext`, as wp4 did, and ran it again: PASS 130.
- **M1's first run** reported 83/83, but its `install-edge` had exited 126. I had put the repo copy in `/root`, which the build user can't read, so that run used an older edge binary. I moved the copy to `/srv/omnifile-reg` and ran M1 again. `install-edge` then rebuilt `omnifile-edge`, `omnifile-detect` and `omnifile-smbguard` from main, and M1 passed 83/83. Only that run counts.

## How it ran

- **omnifile-edge:** a `git archive` of c2bf369 in `/srv/omnifile-reg`, with `REPO` set for `install-edge.sh`, `install-service.sh`, `install-samba.sh` and `install-e2e-tools.sh`, as `dev/README.md` says.
  - **The catalog:** before each install it was reset the way `tests/e2e/e2e.py reset-db` does: drop the `catalog` and `pgmq` schemas and `public.schema_migrations`. Each install migrated 0001 to 0054.
  - **The lanes:** they ran one at a time in your order, after the edge's own `cargo` lane. For that lane the daemon was stopped and its fd store cleared, so the xfs tests ran rather than skipped.
- **Ganesha and the FSAL were left as installed.** The installed `libfsalomni.so` (sha256 `ee0bde61…`) was built from commit 1088f01, whose `fsal/` has no difference from main's.
- **The Samba module** `vfs_omnifile_prevver` was built on 2026-11-05, after `samba-vfs/` last changed on main (2026-10-21).
- **omnifile-acl:** its `/work` is the open-questions worktree, not main. Its `tests/acl`, `fsal/` and the smb-guard, ACL and identity crates are identical to main's, so the run checks main's matrix. It was left running afterwards, with my files removed and wp14's untouched.
- **This Mac:**
  - **M4:** my own Postgres 17 container, with `M4_PORT_BASE=19400`, so the coordinator's stack on 184xx kept running.
  - **The service suite:** the same container, run once.

## The soak

**The run that counts is RED.** It ran the regression's copy of c2bf369, seed 148220832, for 22,522 s, from 17:00 to 23:15 PST on 2026-11-08, with stubbing on (the worker's effective `OMNIFILE_STUB_AFTER_MINUTES` was 0).

Replay: `sudo tests/chaos/soak.sh --seed 148220832 --duration 22500 --each 12 --warmup 300 --cooldown 300 --files 240 --big 4`

| Invariant | Violations |
|---|---|
| zeros | 0 |
| wrong_bytes | 0 |
| enoent | 0 |
| lost_commit | 0 |
| recall | **1** |
| recovery | 0 |

| Fault | Injected | Skipped |
|---|---|---|
| kill_edge | 12 | 0 |
| kill_ganesha | 12 | 0 |
| kill_smbd | 12 | 0 |
| kill_worker | 12 | 0 |
| crash_postgres | 12 | 0 |
| fill_disk | 12 | 0 |
| drop_network | 12 | 0 |
| restart_mid_fill | **8** | **4** ("no big stub ready in 300 s") |

- **The load met every minimum.** There were 17,351 good reads in all:
  - NFS: 12,837 good reads, 1,876 of them of unfilled stubs and 5,839 of archived files; 1,500 good writes;
  - SMB: 4,514 good reads, 368 of unfilled stubs and 2,222 of archived files; 235 good writes;
  - 713 recalls: 712 ended `available`, 1 `failed`.
- **At the end:** 325 files committed and hashed, 325 placeholders present, 1,010 deletes that stayed deleted, and 30 files excluded because a write to them had failed.
- **Errors, not violations:** 229 NFS reads got EIO and 103 SMB reads failed; 136 NFS writes got ENOSPC and 27 SMB writes failed. Of these, 108 writes and 165 NFS reads came with no fault active (issue 1 below).
- **The clock** read +0 before and after.

**Why it is RED: two product issues, both open.**

**Issue 1: the edge keeps a deleted file's space for its whole quiet period.** A regression from decision 67 (#80), and the reason the volume stayed full after each `fill_disk`.
- **The cause:** the write detector holds an open descriptor on every file it waits on, and only notices "deleted while quiet" when the file's period ends. Since #80 a path in no share waits the longest period, 600 s. The soak's `fill_disk` writes `.chaos-fill` at the data root, so its 62 GiB stayed allocated for 600 s after each fault ended.
- **Evidence in the soak:** every ENOSPC write (108) and every EIO read (167 events in `events.jsonl`; the report's table counts 165) that came with no fault active falls within 600 s after a `fill_disk` ended. None came before the first one, and none later than 600 s after one.
- **Evidence on the VM** (2026-11-11, the same edge build): a 2 GiB file at the data root, closed and deleted at once, stayed open in the daemon (`/proc/<pid>/fd` showed it as deleted) and `df` stayed 2 GiB short. It was let go about 600 s later, with the daemon's line "a waited-on file is not reported … deleted while quiet".
- **In production:** a file deleted within its share's quiet period keeps its space until the period ends: 10 s by default, as before, 60 s on a WORM share, up to 600 s if set, and 600 s for anything outside a share.
- **Why `restart_mid_fill` was skipped 4 times:** not established. A full volume is the likely reason no big stub was ready.

**Issue 2: a recall whose file is overwritten ends `failed`.** Older: it is #52's documented rule (control-api.md §4.4, "the placeholder gone or replaced").
- **The violation:** recall 209 of file 515, `wait/soak/d1/f00489.bin` (6,143,745 bytes, archived, on the `wait` share), ended `failed` with "edge fill failed: marker names file 515 version 1930; fetch is for 515/1664". No fault was active.
- **The timeline:**
  - 6001.4 s: an NFSv3 read of the archived stub started the recall, for version 1664. It was `downloading` at 6003.0 s. The reader got EIO after 5.4 s.
  - 6082.1 s: the load overwrote the file over NFSv4.1 with 33,170 bytes, which committed version 1930.
  - 6158.3 s: the edge reported the fill failed, and the service marked the recall `failed`.
- **Why the fill was still pending:** the gap fits #62's resume backoff exactly. A first failure at about 6003.3 s, then resumes after 5, 10, 20, 40 and 80 s, land at 6008, 6018, 6038, 6078 and 6158.3 s. The fifth ran after the overwrite and met the new version. The first four most likely failed with ENOSPC (issue 1): writers got ENOSPC at 6052, 6064, 6074 and 6081 s.
- **Nothing was lost:** the file held its new content, which read back correctly at 6112.9 s.
- **What is wrong:** the reader is told the recall failed, with an internal message, when someone replaced the file. On real Azure a rehydrate takes hours, so this needs no fault.
- **Class:** new. It is not #52's stuck `downloading`, #55's edge-gone sweep or #62's ENOSPC failure. It rides on #62's resume, which worked as designed.

**What couldn't be recovered:** the edge's and the service's log lines, and the catalog. The soak's storage set-up (`tests/chaos/storage.sh`) keeps the journal and Postgres in RAM, and the VM was stopped before I read them. The findings above rest on the soak's report and `events.jsonl`, and on the probe. That the first four fills failed with ENOSPC is inferred from the timing, not read from a log.

**The first run didn't exercise stubbing, so it isn't a soak pass.** It was seed 1490679682, 22,513 s, from 10:38 to 16:54 PST.
- **Its invariants were all clean:** zeros, wrong bytes, ENOENT, lost commits, recalls and recovery were each 0, over 2,841 good reads and 1,753 writes.
- **84 faults were injected,** 12 of each type but `restart_mid_fill`, which was skipped 12 times with "no big stub ready in 300 s".
- **The report was RED on its load minimums:** 0 unfilled reads, 0 archived reads and 0 recalls.
- **81 writes got ENOSPC or DISK_FULL with no fault active,** every one within 600 s after a `fill_disk` ended. That is issue 1 above, seen here too.
- **The cause:** `tests/e2e/large_copy_test.sh`, the lane before, left `/run/systemd/system/omnifile-dev-worker.service.d/90-e2e-verify-only.conf` in place. Its `EnvironmentFile=` sets `OMNIFILE_STUB_AFTER_MINUTES=525600`, and systemd lets it override the soak's `Environment=OMNIFILE_STUB_AFTER_MINUTES=0`.
- **The evidence:**
  - the worker logged "stub sweep queued" 0 times during the run, and the catalog held 1,366 resident files and 0 stubs;
  - `sweep_stub_candidates`, called by hand at 0 minutes, found and queued all 177 eligible big files, inside a transaction that was rolled back;
  - the drop-in was dated during the large-copy lane.
- **The fix is #89, merged since.** Every lane's worker drop-in is now removed on any exit, `worker_env` clears a leftover, and the soak's set-up refuses to start unless the worker's effective value is 0.
- **The rerun,** above, had the drop-in cleared on the VM. That changes runtime state only, so main's code is what ran again.

## Left

- **Issue 1** (the detector keeps a deleted file's space): a fix in its own PR. The detector lets go of deleted files within seconds, and the daemon stops tracking files directly under the data root.
- **Issue 2** (a recall of a replaced file ends `failed`): a second PR. Such a recall ends in its own state with a plain reason, and the edge's fill ends quietly.
- **The soak's evidence:** it should copy the journal and a catalog dump of any violating recall to its results folder when it ends, before anything stops.
- **The soak again on main** with both fixes in. That run is the one that counts.
