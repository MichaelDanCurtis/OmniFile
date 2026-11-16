# WP9: files whose close the daemon never saw (M7, data loss)

Branch `claude/m7-lost-commit-fix`. Found by WP3's chaos soak.

## The bug

A file was written over NFSv4.1, and fsync and close returned 0. The daemon was SIGKILLed 0.9 s later, inside the 3 s commit quiet period. 25 minutes later the file was still plain local data: no marker, no catalog row, no outbox entry. That was 1 of 239 writes in 30 minutes.

Two things lost it:

- The detector had read the close and was waiting out the quiet period in memory. That wait died with the process. The detector's fanotify group was not in the systemd fd store either, so a close made while the daemon was down was lost too.
- `reconcile_dir` (`nsync.rs`), the sweep at start included, skipped a regular file the replica didn't know. It assumed the detector would see the close.

The same gap lost a file whose commit stopped after its `create` (while it was cloned or hashed). The replica knew the file but had no version for it, and nothing came back to it.

## The fix

- **Sweep** (`nsync.rs`, `hand_unseen`). Every pass, the one at start included, looks for regular files with one name that either have no replica node, or have a node the edge registered and never committed (`commit::never_committed`). A file whose ctime is within the quiet period plus 2 s is looked at again when that time is up. A file open for write is looked at again with a backoff (5 s doubling, up to eight times). The entry is opened by an `O_PATH` handle and checked to be the same regular inode before it is reopened read-only. Files with a `trusted.omnifile` marker and recall notes are skipped. A file that is ready goes to the commit pipeline as a `CloseWrite` with `swept_gen` set. The same file is not handed again for 30 minutes unless its commit is done.
- **Commit** (`commit.rs`, `swept_unready`). The single commit task checks everything again for a swept file: the same inode and generation, no marker, unknown to the replica (or never committed and not held in the upload outbox, `ControlPlane::outbox_holds`), mtime older than the quiet period, and open for write nowhere (read lease). A file that is not ready is deferred on the commit retry backoff (`Retries::deferred`). So a file is never committed while a writer has it open, and never twice.
- **Fd store** (`fdstore.rs`, `omnifile-detect`). The detector's group is created with `FAN_UNLIMITED_QUEUE` and pushed to the fd store (`FDNAME=omnifile-detect`, `FDPOLL=0`) before it is marked. On restart the stored group is reused. `main` reads only its descriptor number from `LISTEN_FDNAMES` before the smb-guard starts, and takes the descriptor once the guard is up. The guard leaves unknown names open. The unit gets `NotifyAccess=main`, so the push is accepted without the guard's `Type=notify` drop-in. No `appliance/` change is needed: the image installs `edge/crates/omnifile-edge/systemd/omnifile-edge.service`.
- **Pending-close log** (`pending.rs`, second round, at the lead's request). This covers an overwrite of a file that is already committed whose close the detector had read when the daemon died: in its quiet period, or queued behind other commits.
  - The detector records each file it starts waiting on (`+ token ino generation`, `<replica dir>/pending-closes`) before acting on the event, with one `fdatasync` per batch of events.
  - The commit pipeline writes `- token` once the commit has an outcome. A failed commit stays pending.
  - At start the pending files are opened by xfs handle, waited on again for a quiet period, and committed as any close is.
  - Why a log rather than comparing each committed file's size, mtime, ctime and generation with the replica:
    - ctime moves on every rename, chmod, chown and ACL edit, so comparing it would upload unchanged files as new versions;
    - size and mtime alone miss a same-size overwrite that keeps its mtime;
    - hashing at start costs too much at 10^8 files.
    - The log has no false positives, and it also covers the commit queue. The window left is between reading an event and appending its record.
- **Second-round fixes:**
  - `omnifile-stub` `fanotify_guarded` now counts only content and pre-content groups (`fdinfo_guards`), so a running daemon's notification groups no longer make parallel tests skip;
  - orphaned `.omni/commit` clones (no outbox upload names them) are removed at start (`Core::remove_orphan_clones`, `ControlPlane::outbox_clones`);
  - the smb-guard leaves the `omnifile-detect` fd-store entry alone without logging (`setup::sort_passed`, `OTHERS_FDNAMES`).

Docs: `docs/design/namespace-sync.md` ("Files no close was seen for"), `docs/design/edge-metadata.md` §5.3, §5.4.

## Review of 19b1cf8 (round 3)

| Finding | Fix | Test (fails on 19b1cf8) |
|---|---|---|
| 1 (should-fix). A replayed overwrite was dropped after a reboot or dentry eviction: the by-handle open was disconnected, so `path_of` fell outside the root and the file was skipped with no `done`. Any open error also called `done`. | Replayed files are resolved through the replica's path and checked by `(ino, gen)`, then by handle only when connected below the root (`daemon::resolve_pending`). `done` is called only on `ESTALE` or `nlink == 0`. Other errors retry with backoff (`retry_pending`, about an hour), and then the file is left pending with a warning. The detector tells the log about any tokened file it won't report (`PendingLog::finished`). | `pending_overwrites_are_replayed_with_cold_caches`: `drop_caches` before replay, one file in place and one renamed while the daemon was down |
| 2 (should-fix). The detector stopped reading fanotify under commit backpressure. | The detector queues what it reports and never awaits the channel in its select loop (`tx.reserve()` only while the queue is non-empty). At shutdown it drains the group to `EAGAIN` into the log. | `omnifile-detect` `a_full_receiver_does_not_stop_the_reading` |
| 3 (minor). A host crash could leave a duplicate version. | `done_durable` (fdatasync) for an upload held in the outbox. A tokened close for a file the outbox holds is ignored, because the outbox re-checks the live file. | `a_close_for_a_file_held_in_the_outbox_is_not_committed_again` |
| 4 (minor). Leftover split temp names were committed. | `.omnifile-split-*` and `.omnifile-unlink-*` are left out of every pass. Those from an earlier run are removed where no bytes go with them; the last link of a catalogued file is kept. `hand_unseen` skips `.omnifile-*`. | `split_leftovers_are_cleaned_up_not_committed` |
| 5 (minor). A failed rewrite at open disabled replay. | The entries are returned anyway; the log is appended to as it is, with a warning. | `pending::a_failed_rewrite_still_returns_what_is_pending` |

Against an export of 19b1cf8, with these tests (and the new `FakeService::set_outbox_holds`) copied in, in the omnifile-acl VM:
- The four new tests fail, each on its finding.
- The cold-cache test fails on its renamed file. Its in-place file still committed on 19b1cf8: `drop_caches` didn't evict that dentry. So the renamed file, found only once the namespace sweep has moved its node, is what reproduces the drop.
- The split-leftover test fails because the cleanup is what lets `user.segy` be committed in time.
- The other 8 `lost_commit_linux` tests pass there, as before.

## Re-review of 91c2271 (round 4)

| Finding | Fix | Test |
|---|---|---|
| 1 (should-fix). Fix 3 opened a loss window: an outbox-held close was `Ignored` and marked done. The overwrite then rested on the outbox's in-memory `again`, which a crash loses, and a failed `finish_outbox_op` forgot the op. | The close is `Deferred` and stays pending in the log until the outbox lets go. An op whose finish fails is kept and finished on a later pass. | `a_close_for_a_file_held_in_the_outbox_waits_and_survives_a_restart` (the daemon stops while the outbox holds the upload; the next start commits the new bytes, once); `an_outbox_op_whose_finish_fails_is_kept_and_finished_later` |
| 2 (minor). The shutdown drain waited behind a blocked commit send. | The hand-over races the stop signal; a stop leaves the close pending and drops the receiver, so the detector drains at once. | `a_stop_with_a_full_commit_queue_is_prompt_and_keeps_the_closes` (4,600 closes, more than the queue holds) |
| Follow-up (should-fix). A kept failed op was finished again at every 10 s pass, with no cap: on a persistent error a big file would be re-hashed forever. | A per-op backoff (10 s doubling, `[commit] outbox_finish_backoff_s`), and after `outbox_finish_attempts` failures (8) the op is forgotten and its file committed again from the live copy; its close stays pending in the log. | `a_failing_outbox_finish_backs_off_then_is_given_up` (3 attempts, 12 s base: still kept at 32 s, given up after 40 s) |
| 3 (minor). The detector's report queue was unbounded. | At most one report per file: a repeat replaces the waiting one. | `omnifile-detect` `repeated_closes_of_one_file_wait_as_one_report` |

Against an export of 91c2271, with these tests and the new fake hooks copied in:
- Both finding-1 tests fail there ("the close is still pending"; "kept while it can't be finished").
- The coalescing test fails there.
- The prompt-stop test passes on both heads: in the VM the commit queue doesn't fill within the test's 15 s, because each close is normalised on the way. It documents the property but doesn't reproduce the old wait.

The flood test takes a write lock on a test-file `FLOOD` lock, and the other tests take read locks, because its 4,600 closes slow every other detector on the filesystem.

Tokio 1.53.3 (from main) deprecates `AsyncFd::with_interest`, so `omnifile-detect` now uses `register_with_interest`. `omnifile-fsal-proto/src/socket.rs:63` and `:226` still call it, outside my globs. As a result, `cargo clippy -p omnifile-edge -- -D warnings` fails on main for any crate that depends on fsal-proto; this round's lint lane uses `--no-deps`.

## Tests

`edge/crates/omnifile-edge/tests/lost_commit_linux.rs` (root, xfs, fanotify):

| Test | What it shows |
|---|---|
| `a_close_one_second_before_a_sigkill_is_committed_after_the_restart` | the soak's case: the real binary as a child, SIGKILL 1 s after the close, the next instance commits it (marker, replica row, one create, one upload, bytes in the store) |
| `files_written_while_the_daemon_was_stopped_are_all_committed_once` | 8 files, 3 in a folder made while stopped: each committed exactly once; a file the detector committed while running isn't committed again; a second restart commits nothing |
| `a_file_open_for_write_at_restart_waits_for_its_close` | held open for write (detector off): not registered or committed for 6 s; committed once closed |
| `a_commit_cut_short_after_its_create_is_finished` | registered and created but never uploaded: committed with the same file id, no second create |
| `the_edges_own_files_are_never_committed_by_the_sweep` | a recall note, a stray partial stub, `.omni/commit` and `.omni/snap` files, the placeholder: none committed; a user's `mine.RECALLING.txt` is; the orphaned clone is removed at start |
| `an_overwrite_closed_one_second_before_a_sigkill_is_committed` | a committed file overwritten, the real binary SIGKILLed 1 s after the close (no systemd, so no fd store): the next instance commits the new content as a new version of the same file, with one more upload and no second create |
| `closes_one_second_before_a_clean_restart_are_committed` | WP3's `restart_mid_fill`: SIGTERM (a clean `systemctl restart`) 1 s after a new file and an overwrite are closed; the next instance commits both (round 3, from WP3's three as-is losses) |
| `a_close_while_the_daemon_restarts_is_read_from_the_fd_store` | the daemon as a transient systemd service: SIGKILLed, a committed file overwritten before systemd restarts it, the new instance reuses the stored group and commits the overwrite (the sweep is off) |

`omnifile-detect/tests/fanotify.rs`: `a_close_while_no_one_reads_is_reported_on_the_same_group`, `a_bounded_group_is_not_usable`, `waited_on_files_are_logged_and_replayed`. Unit tests: `fdinfo` flag parsing (`omnifile-detect`), the fd-name lookup (`fdstore.rs`), the pending-close log (`pending.rs`, 3), orphaned clones (`commit.rs`), `fdinfo_guards` (`omnifile-stub`), and the smb-guard's handling of the detector's entry (`setup.rs`).

All eight `lost_commit_linux` tests fail on `origin/main`. The check: the same file copied onto an export of main, run single-threaded in the omnifile-acl VM: 0 passed, 7 failed; the clean-restart test, added later, also fails there. The overwrite test fails with "the overwrite was never committed". In a parallel run on main, three of them skip silently: main's `fanotify_guarded` counts the other tests' daemons as guards. The new detector tests and unit tests use API that main doesn't have.

## Lanes

Round 4, at the pushed head, ran in the omnifile-acl VM as root on a loop-mounted xfs, with `OMNIFILE_REQUIRE_FANOTIFY=1` and `OMNIFILE_REQUIRE_SIM=1`:
- `cargo test -p omnifile-edge --all-targets`, in parallel: 125 passed, 0 failed (lib 42, chunks 10, daemon 19, lost_commit 14, nsync 21, path_safety 1, review_m1 12, scripts 2, units 4).
  - One test still skips: nsync's `xfs didn't reuse the inode number`. It depends on chance and skipped before this PR too.
  - One run of the full suite had nsync's `a_later_change_never_takes_an_earlier_users_hint` fail once (wrong uid in a hint). It passed 3 of 3 reruns and the final run. It looks like a timing flake, and the test predates this PR.
- `cargo test -p omnifile-detect --lib --tests -- --test-threads=1`: 22 passed (unit 7, fanotify 12, ns_linux 3). `cargo test -p omnifile-fsal-proto`: 33 passed.
- `cargo test -p omnifile-stub --lib --tests`: 71 passed. `cargo test -p omnifile-smbguard --lib`: 47 passed.
- `cargo clippy --workspace --all-targets -- -D warnings` (no `--no-deps`): clean, with the `omnifile-fsal-proto` deprecation fixed (d7ff0d7).

On the Mac, unprivileged, in `edge/`:
- `cargo test --workspace`: 447 passed, 0 failed;
- `cargo clippy --workspace --all-targets -- -D warnings`: clean;
- `cargo fmt --all --check`: clean.

## Left

- **L.** The namespace watcher's group (`omnifile_detect::ns`) is still not in the fd store. The sweep at start covers what it misses.
- **L.** A crash between reading an event and appending its record (microseconds) still loses that close.
- **L, next PR (the lead's call).** WP3's line-rate run committed a partial content version (4.68 GB of a 10 GiB NFSv4.1 write, mid-write). This is the detector's modify-only quiet path: a file with no writes for the whole quiet period is reported with `closed = false`. Proposed fix: never commit on a modify-only quiet period while the FSAL reports NFSv4 open state for the file, and for NFSv3 require no growth across two quiet periods.
- **L.** WP3 reruns the soak on this branch.

## Out of scope, found

- None left open. The first round's three findings (`fanotify_guarded`, orphaned clones, the smb-guard's log line) are fixed in this PR.

## Tools

None installed. Rust 1.99 and systemd 259 were already in the omnifile-acl VM.
