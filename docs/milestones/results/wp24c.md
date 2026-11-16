# WP24c: the detector lets go of a deleted file

Issue 1 of the 2026-11-08 regression (`regression-2026-11-08.md`), and mine: a regression from decision 67 (#80). Branch `claude/detector-lets-go-of-deleted`, off main.

## What was wrong

The write detector holds an open descriptor on every file it waits on, so a later look can tell it is the same file. It only checked "deleted while it waits" when the file's quiet period ended. A deleted file's blocks stay allocated while anything holds it open, so its space came back only then:

- 10 s on a share with the default period, as before #80;
- 60 s on a WORM share, and up to 600 s where a share sets that;
- **600 s for a path in no share,** which since #80 waits the longest period a share may have. The chaos soak's `fill_disk` writes its 62 GiB file at the data root, so the volume stayed full for ten minutes after every such fault ended. Every ENOSPC write (108) and EIO read (167) the soak saw with no fault active falls in those ten minutes.

## The fix

- **The detector looks for deleted files every 2 s** (`omnifile-detect`, `State::let_go_of_deleted`). It asks each held descriptor for its link count (one `fstat`) and lets go of a file whose count is 0. A file the detector held a commit token for is finished as "deleted while quiet", as it was at the period's end. A pass takes at most 20,000 files per tick (`DetectConfig::gone_batch`), so 17,000 writers at once cost one pass, and more are spread over the next ticks.
- **The daemon no longer waits on files directly under the data root** (`DetectConfig::min_depth = 2` in `daemon.rs`). Nothing there is in a share: the first path component is the share's folder. The detector crate's default stays 1, so its other users are unchanged.
- **`docs/design/edge-metadata.md`** says both.

## The other descriptors on deleted files

The coordinator asked for a check of everything else the daemon may hold open on a deleted file. I read each path:

| Path | Holds a descriptor across a wait? |
|---|---|
| The detector's waiting files (`omnifile-detect`, `Held`) | Yes. This was the leak, fixed here. |
| The commit's further wait from #82 (`commit.rs`, `held_still`, `not_yet`, `carry.rs` `Retries`) | No. `commit()` opens the file for each look and closes it; the retry list holds the path and inode number. |
| The pending-close log (`pending.rs`) | No. It stores `(ino, generation)` only. |
| The sweep (`nsync.rs`) | No. It opens, looks and closes. |
| The upload outbox (`retry_pending`) | No descriptor. Its ops hold a commit clone, below. |
| Commit clones in `.omni/commit` | These are reflinks, not descriptors. A clone keeps a deleted file's blocks until its upload is confirmed, by design: a WORM commit must not lose bytes the user deleted a second later (decision 31). It is bounded by the outbox, not by a quiet period. |

So the detector was the only leak.

## Tests, each failing without its part of the fix

- **`omnifile-detect` unit, `a_file_gone_while_it_waits_is_forgotten_and_the_rest_wait_on`:** `QuietTracker::forget_if` drops the file the check names and keeps the others waiting.
- **`omnifile-detect` unit, `a_file_directly_under_the_root_is_not_wanted_at_depth_two`:** `wants()` at depth 1 and 2.
- **`omnifile-detect` `fanotify.rs`, `a_file_deleted_while_it_waits_is_let_go`:** a 600 s period and a 0.5 s check. The deleted file's descriptor is gone from `/proc/self/fd` within seconds, its commit token is finished, it is never reported, and a second file that still exists is still held.
- **`omnifile-detect` `fanotify.rs`, `a_file_directly_under_the_root_is_not_waited_on_at_depth_two`.**
- **`omnifile-edge` `daemon_linux.rs`, `a_file_deleted_while_it_waits_gives_its_space_back`:** the `df` test. 256 MiB written with `dd` on a WORM share whose period is 300 s, then deleted. At least 200 MiB is back in `statvfs` within 15 s, the daemon holds no descriptor on it, and nothing was uploaded.
- **`omnifile-edge` `daemon_linux.rs`, `a_file_directly_under_the_data_root_is_not_held_open`.**

**Only a file with no name left is let go** (#94's review). The check is the commit decision for a file: one it drops is never reported. Three more tests in `fanotify.rs`, each over a 2 s period with a 0.1 s check, so the check runs many times before the period ends:

- **`a_file_renamed_while_it_waits_is_still_reported`:** reported under its new name.
- **`the_file_renamed_over_another_is_still_reported`:** the file it replaced is let go and never reported; the new one is reported under the name it took.
- **`a_file_that_loses_one_of_two_hard_links_is_still_reported`:** a second name is made and removed; the file is reported under its own.

Each fails when the check is loosened, on the VM (`fx-undo.py fix1b`, all of `fanotify.rs` run against each):

| Loosened to | New tests that fail | Others that fail |
|---|---|---|
| `nlink < 2` | all three | `a_file_deleted_while_it_waits_is_let_go` (its kept file), `a_file_moved_into_a_slower_share_waits_its_period` |
| let go on a rename (the inode changed after its last write: ctime past mtime) | all three | `a_file_moved_into_a_slower_share_waits_its_period` |

The new tests use the new config fields, so they don't compile against main. "Fails on main" is shown two ways instead:

- **The undo pass, on the VM, 5 of 5 caught** (`fx-undo.py fix1`: each part undone in the VM's copy, the tests that should notice run, the part put back):

| Undone | Caught by |
|---|---|
| U1: the detector never looks for deleted files | `a_file_deleted_while_it_waits_is_let_go`, `a_file_deleted_while_it_waits_gives_its_space_back` |
| U2: the daemon waits on files at the data root again | `a_file_directly_under_the_data_root_is_not_held_open` |
| U3: `wants()` ignores the depth | both depth tests |
| U4: a let-go file's commit token is not finished | `a_file_deleted_while_it_waits_is_let_go` |
| U5: the check lets go of files that still exist | `a_file_deleted_while_it_waits_is_let_go` (the kept file) |

- **The probe on main's build** (2026-11-11, `regression-2026-11-08.md`): a 2 GiB file at the data root, deleted at once, stayed open in the daemon and `df` stayed 2 GiB short for about 600 s.

**The same probe on this branch's installed daemon** (the omnifile-edge VM, after M1): a 2 GiB file in a share, deleted 2 s after its close.

| After the delete | Space still taken | Descriptors held |
|---|---|---|
| 1 s | 2048 MiB | 1 |
| 5 s | 0 MiB | 0 |
| 10 s | 0 MiB | 0 |

That share has the default 10 s period, so on main the file would have been held for those 10 s. The 600 s case is the data root's, which the daemon no longer waits on at all.

## The soak keeps its evidence

The soak's storage set-up keeps the journal and Postgres in RAM, so the 2026-11-08 run's logs were gone once the VM stopped. Now `soak.py` saves, when a run ends, pass or fail, before its report and before anything is stopped:

- **`journal.log.gz`:** the journal of the run's units from a minute before the run started (`journalctl`, gzipped);
- **`violations-catalog.json`,** when the run has a violation: for every recall and file one names, its rows in `recall`, `recall_requester`, `file`, `file_version`, `location` and `audit_log`. A query that fails is recorded and the rest go on.

Saving never changes the verdict: a failure to save is printed and the report is written as before. Four tests in `tests/chaos/test_soak.py` cover it: the journal is saved gzipped for the run's units; a violation names its recall and its file even after the file is deleted; the dump goes on past a query that fails; and the evidence is saved before the report and never changes the verdict.

## Lanes

All on the omnifile-edge VM (Ubuntu 26.04, kernel 7.0, joined to the dev AD) at 4690598e, as root on unguarded xfs with the daemon stopped and its fd store cleared, so no root test skipped. The branch's head adds only a merge of main, which brought `regression-2026-11-08.md` and no code.

- **Edge:**
  - `cargo fmt --all -- --check`: exit 0;
  - `cargo clippy --workspace --all-targets -- -D warnings`: exit 0;
  - `cargo test --workspace --no-fail-fast`: **971 passed, 0 failed, 1 ignored**, 0 "skipping" notices. In it: `omnifile-detect` unit 11 and `fanotify.rs` 16; `omnifile-edge` unit 310, `daemon_linux` 41, `chunks_linux` 10, `lost_commit_linux` 14, `nsync_linux` 21, `path_safety_linux` 1, `review_m1_linux` 12.
  - After the review's three tests (tests only, in `fanotify.rs`), on the VM: `cargo fmt --all -- --check` exit 0; `cargo clippy -p omnifile-detect --all-targets -- -D warnings` exit 0; `cargo test -p omnifile-detect`: unit 11, **`fanotify.rs` 19**, `ns_linux` 3, 0 failed. The workspace run above was not repeated for them.
- **The exit tests,** each on a catalog dropped and migrated from 0001 to 0057, the clock read +0 before and after each:

| Lane | Command | Result |
|---|---|---|
| M1 | `tests/e2e/exit_test.sh` | 83 passed, 0 failed |
| M2 | `tests/e2e/m2_exit_test.sh` | 111 passed, 0 failed |
| M3 | `tests/e2e/m3_exit_test.sh` | 110 passed, 0 failed |
| M5 | `tests/e2e/m5_exit_test.sh` | 139 passed, 0 failed |

- **The chaos harness's own tests,** `pytest tests/chaos/test_soak.py`: 28 passed on the VM; 30 passed and 2 skipped on the Mac, where they run once per installed bash and two need Linux's `/proc`.
- **Service,** `uv run pytest -q` on its own Postgres 17: **2722 passed, 51 skipped** (at this branch's head, merged with main at 82218eb). No service code changed.
- **Portal,** `npm test`: **335 passed in 39 files.** No portal code changed.

## Known, and left as it is

- **`adopt` doesn't apply `min_depth`.** A file taken back from the pending-close log at start, or injected, is waited on even if it is directly under the data root. It is harmless: such a file is waited on once and then let go as any other, and the deleted-file check covers it.
- **Commit clones still hold a deleted file's space.** The outbox's clones in `.omni/commit` keep a deleted file's blocks until its upload ends, by design (decision 31). On a volume that is full during a control-API outage they are the remaining holder: nothing frees them until the uploads go through.
- **A file whose first name is removed while a second remains.** The check keeps it, as it should. At the period's end the detector does not report it, because its descriptor's path reads as deleted (`path_of`). That is as before this PR, and is left to the sweep, which finds the file under its other name; it is not tested here. Shares refuse hard links over NFS (decision 42).

## Not done here

- **The soak rerun.** It runs on main once this and the recall fix (issue 2) are both in, and that run is the one that counts.
- **Why `restart_mid_fill` was skipped 4 times in the 2026-11-08 rerun** is still not established. A full volume is the likely reason, and the rerun will show whether it goes away with this fix.
