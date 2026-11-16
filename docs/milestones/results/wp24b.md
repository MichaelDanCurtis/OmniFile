# WP24b: every write waits out the quiet period

Item 4 of #80's review, as its own PR: three paths sent a file to the commit with no detector view and no sweep generation, so they skipped the quiet period on every share, WORM included. A file being written at that moment could be committed part-way. Stacked on #80 (branch `claude/quiet-for-every-commit`, from `claude/quiet-period-per-share`), since it uses #80's per-share period.

## The paths, and why each is safe now

| Path | When | Before | Now |
|---|---|---|---|
| `commit.rs`, `outbox_file_again` | the upload outbox gives up finishing an op and commits the live file again | committed at once | waits out the share's period from the file's mtime |
| `commit.rs`, `finish_outbox_op` (`again`) | an outbox op didn't commit, or its file changed | committed at once | the same |
| `repair.rs`, `commit_modified` | a file the user changed, handed over after a repair; also from `core.rs` (a conflict), `remote.rs` and `feed_loop.rs` | committed at once | the same |

All three build `CloseWrite::new(path, ino)`, with no `seen` and no `swept_gen`. In `still_being_written` the NFSv4 open check already ran for them, so an NFSv4 client's open held them. But the size-and-mtime wait returned at once for such an event ("neither the detector's view nor a check under way").

**Now (after #82's review):**
- **The wait runs on the daemon's own clock.** The first look starts the wait, and the file is committed once its size and mtime have held for the share's period since (`held_still`). The first version of this PR used the file's mtime instead. A writer that puts back an old mtime after each write (an SMB SetInfo, `touch -d`) and pauses would then be committed part-way at once.
- **Like a sweep, it waits while anyone has the file open for write** (`open_for_write_elsewhere`, a read lease). An SMB or local writer holding the file open is not seen by the FSALs.
- **It is never dropped from the retries.** It is looked at again at most every 5 minutes until it commits (`commit::not_yet`). An outbox re-commit dropped after the bounded looks would have had nothing else to bring it back.
- **Only a swept file is given up,** after about 18 minutes, with a warning that names it as such, because the next sweep finds it again.
- **A sweep checks the period itself first,** from the inode's ctime, which no writer can set, so for a sweep nothing changes.

SMB opens are still not seen on the detector's own path (`edge-metadata.md`, "The NFSv3 limit").

## Tests

- **`daemon_linux::a_write_handed_over_with_no_view_isnt_committed_part_way`:** a writer appends every 0.5 s for 4 s, on a share with a 3 s period. The file is handed over 0.7 s in, as `commit_modified` does (`CloseWrite::new(path, 0)`). The test expects one version, of the whole file. On #80's head it fails: "a version of the file part-way".
- **`daemon_linux::a_write_handed_over_with_no_view_waits_out_the_period`:** a file written just now, on a share with a 6 s period. It is not committed within 4 s, and is committed after. On #80's head it fails: "committed within the period".
- **`daemon_linux::a_writer_that_sets_an_old_mtime_isnt_committed_part_way`** (#82 review): a writer appends every 1.2 s on a share with a 3 s period, and puts back an hour-old mtime after each write. The test expects one version, of the whole file. On 6ceb3af it fails: "a version of the file part-way".
- **`daemon_linux::a_write_no_one_watched_waits_while_it_is_open_for_write`** (#82 review): another process holds the file open for write, quiet, for 7 s, on a share with a 2 s period. The file isn't committed meanwhile, and once it is, the version has the writer's last bytes. On 6ceb3af it fails: committed while open.
- **`commit::tests::a_write_no_one_watched_waits_out_the_period_on_the_daemon_s_clock`:**
  - whatever its mtime, the first look starts the wait;
  - a new write starts it over, old mtime or not;
  - a sweep is committed at once.
- **`commit::tests::a_write_not_quiet_yet_is_only_given_up_when_a_sweep_will_find_it`:** an outbox re-commit is still scheduled after 50 looks, and a swept file is dropped after 10.

The two tests from item 6's first round (`a_write_handed_over_with_no_view_…`) still pass.

**The undo is #80's code.** Both VM tests fail on it, as above, and the unit test needs the new branch.

**Tests that relied on the old behaviour.** Nine tests write a file and hand it over by hand at once, expecting a commit within 10 s, while their config left the period at the 10 s default:

- `daemon_linux`: four tests;
- `nsync_linux`: one;
- `path_safety_linux`: one;
- `review_m1_linux`: three.

None of them tests the quiet period. Each file's test config now sets `quiet_s = 1`. The heartbeat test now expects that 1 s.

## Lanes

- **Edge, in the omnifile-edge VM as root, on unguarded xfs, at c020d6c:**
  - `cargo clippy --workspace --all-targets -- -D warnings`: clean;
  - `cargo test --workspace --no-fail-fast`: **944 passed, 0 failed, 1 ignored**.
  - Before the last test fix, `a_share_from_the_feed_is_rendered_reloaded_and_its_root_given_its_groups` failed once in a parallel run and passed twice when run alone. It renders shares and doesn't touch the commit path; it passed in the full run above.
- **M1 on omnifile-edge, at fde11c7** (this branch merged with #80 at e2ac016, which includes #77, merged into main at 635e1a4): **83 passed, 0 failed**.
  - Clean catalog, migrated from 0001 to 0050.
  - The clock read +0 before and after.
  - The catalog was dropped afterwards.
- **No service or portal code changed** in this PR. #80's lanes cover them.

**Merge order:** after #80. This branch carries #80's commits until #80 is merged, so its diff against main shrinks then. Don't delete #80's branch in a way that closes this PR: the PR's base is `main`, not #80's branch.

## The re-check (2026-11-07)

**What changed:**
- **5. The wait runs on the daemon's own clock, and holds while the file is open for write.** See "The paths" above.
- **6. A write that isn't ready is never dropped unless a sweep will find it.** A swept file's give-up is now a warning that says what it is.

**Confirming the bugs:** both new VM tests fail on bd934b3d, whose `commit.rs` and `carry.rs` are 6ceb3af's.
- `a_writer_that_sets_an_old_mtime_isnt_committed_part_way` fails with "a version of the file part-way".
- `a_write_no_one_watched_waits_while_it_is_open_for_write` fails because the file was committed while open.

**Undo pass: 5 of 5 caught,** in the VM:
- the wait timed from the mtime;
- no open-for-write check;
- a sweep waits too;
- every write that isn't ready is given up;
- a swept file is never given up.

"Every write that isn't ready is given up" was missed at first. A dropped entry is put back by the next call, and the test only looked after 50 calls. It now checks after every one.

**Lanes at 4d5e9613** (31504019 adds only the regenerated portal client and #80's merge):
- **Edge, in omnifile-edge as root:**
  - clippy clean;
  - `cargo test --workspace` **948 passed, 0 failed, 1 ignored**.
- **M1: 83 passed, 0 failed.**
  - Clean catalog, 0001 to 0050.
  - The clock read +0 before and after.
- **Service:** the full suite **2640 passed, 0 failed, 50 skipped**.
- **Portal:** `npm test` **328 passed**.

No cloud calls. No new dependencies.
