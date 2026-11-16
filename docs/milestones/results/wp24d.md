# WP24d: a recall whose file was replaced or deleted ends `superseded`

Issue 2 of the 2026-11-08 regression (`regression-2026-11-08.md`). Branch `claude/recall-superseded`, off main. Migration 0059, which needs #93's 0058 merged first.

## What was wrong

A recall of a file's current content had two ends: `available` or `failed`. When someone replaced the file, or deleted it, while it was being recalled, the edge's fill found another file under the name and reported the recall failed (control-api.md §4.4, #52's rule). Its requesters were told "Couldn't recall *name*" and offered a retry, for a file that had only been replaced. In the soak it took a fill resumed after a full disk. On real Azure a rehydrate takes hours, so it needs no fault: open an archived file, then save a new export over it.

## What changed

**The catalog ends the recall, in the commit's own transaction** (migration 0059, `catalog.file_version_after_commit`). When a content version or a tombstone becomes current, the file's active recalls of what was its current content become `superseded`, with `superseded_reason` (`replaced` or `deleted`) and `superseded_by_version_id`. The same trigger queues `notify` for each. It is the one place every commit goes through: an edge's upload or unlink, a portal restore, an annex copy.

**A recall asked for by version is left alone.** Two things keep a recall of the current version:

| Who asked | What keeps the recall |
|---|---|
| A read in `.snapshots` or Previous Versions | `recall.pinned`. The edge sends `pinned: true` with `POST …/recalls` (the placeholder's READONLY marker flag), and a reader that joins pinned pins a recall that wasn't. It stays: nothing tells the catalog the reader left |
| A portal version restore (decision 34), an annex placement | `recall.held_by_jobs`: how many unfinished restores and placements wait on it. The catalog keeps the count, by triggers on `version_restore` and `annex_placement`, so every path that makes, moves or ends one is covered, and the migration counts the ones already waiting when it runs. When the last one ends, the recall is an ordinary one again |

A recall of an older version is never reached: the trigger only looks at recalls of the then-current version. A rename or an attribute change ends nothing.

A kept recall whose file moves on is marked, and ends with its last job: `available` if the job had its bytes, `superseded` if the job let go ("After the second review").

**The provider's restore is left to finish, not aborted.** The worker starts none for a recall that has ended, and stops polling one for it. A rehydrate already running on Azure stays on record: the change feed closes it and commits its copy, a later recall of that version joins it or reads the copy, and the recall-copy expiry deletes the copy after its 7-day window. So nothing is orphaned. The cost is what the recall had already asked for: the retrieval, and the copy as Hot for up to 7 days. I do not call Abort Copy Blob. The version it would stop is still a kept version, and the one a user is most likely to ask for next (a rewind, or a restore of what was just overwritten); and whether Abort Copy Blob works on a rehydrating copy needs real Azure to confirm. On S3 a `RestoreObject` cannot be cancelled; its temporary copy expires after its `Days`.

**The edge:**
- knows `superseded` (to the FSAL it is `CANCELLED`), stops a fill of that recall, schedules no resume and removes the note;
- **reports nothing for a fill that met another file.** After a fill of the live file ends without the data, the edge asks whether the file is still the recall's version: the replica says it is deleted or holds another version, or on disk another inode has its name, it has no marker (the FSAL removes the marker when a writer truncates a stub), or its marker names another version. If so it reports nothing, before or after the change is committed. A CRC mismatch, an unreadable marker or anything else is still reported as failed;
- **ends what it kept for a recall whose file moved on or was deleted** (`end_left_behind`). Such a row had no placeholder to be applied to, so the edge did nothing with it: the entry stayed, and so did the note. A deleted file's row was applied to nothing at all.

**A waiting reader is released** (the coordinator's first point):
- **replaced in place** (the same inode): the entry ends `CANCELLED`, and the reader's next read finds the new marker, or none, and gets the new bytes;
  - A reader that was part-way through keeps what it had already read. In the M3 step the reader had read the stub's 1 MiB resident header (the old version's, served at once) and was waiting for the rest; after the replace it read on into the new file. That is what any reader of a file replaced under it gets, on any NFS server; it is no error and no zeros.
- **the reader's inode is no longer the file** (deleted, or renamed over, and the reader still holds the old one open): its bytes can never come. The entry ends `FAILED` and the reader gets `EIO`. Ended as `CANCELLED`, the FSAL gives a reader the share's own answer (`fsal-socket.md` §5.5), which on a `wait` share is to wait again. I did not measure how long such a reader would have waited; the M3 step shows the reader released in 1.3 s as it is now.

**A Keep online hold and a tier pin are untouched** (the coordinator's second point). Both are rows on the file, not on a version or a recall, and the trigger writes only to `catalog.recall`. The card still shows "Kept online until".

**What people see:**
- **Teams** (`teams.md` §3.2): the card is updated in place to "*name* was replaced" or "*name* was deleted", with "This file was replaced (deleted) while it was being recalled, so the recall has stopped." and only "Open in portal". No Recall button: there is nothing to try again. One last mail if Teams fell back.
- **Portal** (`portal-api.md` §5.5): `state: superseded`, `failure: null`, and `superseded: {reason, text, by}`. The steps the recall reached stay done and none is marked failed. The recall page says "The recall stopped", with no retry.
- **Who did it** is named only to someone who can read the file, and only when the superseding version records an actor the edge proved (Kerberos NFS, or SMB on a proven share).

## Open, and said plainly

- **A replace names no one today.** An upload's content version records no actor; only `changes` ops do. So a card can say "Deleted by Ana Ruiz" but not who replaced a file. Giving uploads an actor is its own change, and was not done here (the coordinator's call).
- **One window is left.** Between the write on the edge and its commit (the quiet period and the upload), the recall is still `downloading` in the catalog with no fill. The commit ends it. If the commit takes longer than `OMNIFILE_RECALL_NO_FILL_MINUTES` (60 by default: a very large upload on a slow link, or a writer that holds the file open), the idle rule fails the recall first, as `edge not filling:`. Closing it would need the edge to tell the service "replaced" before the commit, which is a new call and was not built.
- **A reader that holds a replaced inode gets `EIO`, not the new bytes.** That is the rename-over case (an editor that saves by renaming a new file over the old one): the reader's open file is the old inode, which no bytes can reach any more. A new open reads the new file. Replaced in place, the reader gets the new bytes.
- **Abort Copy Blob** on a rehydrating copy is unverified (above). If the owner wants rehydrates stopped, it is a `real-azure-checks.md` line first.
- **A kept recall with a live reader too.** If a snapshot reader, a restore or a placement shares one recall with a reader of the live file (the same version) and the file is replaced, the recall goes on, and the live reader's card follows it to "ready". The live reader is not told the file was replaced.
- **A rewind read already waiting at the upgrade** is not pinned: only an edge can say a read was of a rewind placeholder, and the edges that started those recalls did not. Such a recall, of a version that is still current, is ended by the next replace of its file, and the reader's next open starts another.
- **An older edge and a deleted file.** An edge without this PR does nothing with a recall row for a deleted file, whatever its state, so a reader that holds the deleted file open on a `wait` share is not released by it. The service can only choose the state it sends; the release is in the edge.
- **The SMB guard's `pinned`** (a Previous Versions read) is set from the same key as NFS's but has no test of its own here.
- **The lock order** is as it was: see "After the reviews", 3.

## After the reviews (Codex's P2 and the Claude review, on 6b43226 and 2620c89d)

**Codex: an upgrade left jobs' recalls unkept, and a restore could wait for ever.** True. `pinned` was a flag the restore and the placement set when they asked, so a restore or placement already waiting when the migration ran had none, and `portal_jobs` handled a recall that was `failed`, `cancelled` or `available` only.
- **The flag became a count the catalog keeps** (`held_by_jobs`, above), and the migration fills it in for the jobs waiting when it runs. A job's own code sets nothing, so there is no path that can forget.
- **A recall a job joins is locked** (`start_or_join(hold=True)`, the worker's `_start_recall`), so a commit that would end it waits for the job's transaction and then sees the count.
- **A restore that finds its recall `superseded` recalls the version again.** It should not get there; if it does, it no longer retries for ever.
- **The consumers of a recall's state, each checked:**

| Consumer | What it does with `superseded` |
|---|---|
| `worker/portal_jobs.py`, a restore in `waiting_recall` | recalls the version again (new) |
| annex placements (`annex_placement`, moved on by `worker/recalls._set_recall`) | hold their recall while they are unfinished, so it is not superseded under them; a placement's recall is locked when it is joined |
| `worker/recalls.py`, the `restores` consumer | stops: it acts on `requested`, `rehydrating`, `copying` only (test) |
| `worker/redrive.py`, `worker/recall_timeout.py` | select active or `downloading` recalls only |
| `worker/tiering.py`'s High budget, `policy/scheduler.py`'s recall for a pin | count or look for active recalls only |
| the migration engine (`migrate/staging.py`, `waves.py`) | reads restores, not recall states. A restore left running is staged or closed as before |
| the partner API | `GET /v1/recalls/{id}` gives `state: superseded` and the `superseded` block, with `by` null for an app; `recall.state` events now carry `superseded_reason` (new) |
| `web/recall_batches.py` | records that each file's recall was started or joined; it does not follow them |
| Teams cards and mail, the portal's pages | as above |
| `api/recalls.py`: `…/failed` answers 204, `…/filled` answers 409, as for any ended recall | |
| `web/routes_activity.py`, `web/live.py` | the summary counts `available` and `failed`; a superseded recall is in neither. The live feed shows the state |
| the edge | below |

**The review, 1: a held recall of the current version could still end `failed`, an hour late.** True, in two ways.
- *The file is deleted or replaced while the restore waits.* The restore holds the recall, so it goes on. When the restore finished it left a recall of "the file's current content" to the edge; a deleted file keeps its content version, no edge fills it, and the idle rule failed it. **Now the restore closes its recall as `available` when it is done if the file is deleted or holds another version.**
- *The file is replaced after the restore is done.* The recall stayed kept for good, the edge's fill met another file and said nothing, and the recall stayed `downloading`. **Now the restore stops holding the recall when it ends,** so the replace (or a delete) ends it as it ends any recall.
- Tests run both to their ends, for a replace and for a delete (four cases).

**The review, 2: an old edge with the new service.** True: an edge that predates the state reads `superseded` as unknown, ends nothing, and a `wait` reader could wait for good. **The feed now says `cancelled` to an edge that has not said it knows the state.** An edge says so in its heartbeat (`recall_superseded: 1`). To an edge the two mean the same. An edge that has not sent a heartbeat yet is treated as old. The catalog, the portal and Teams keep the true state.

**The review, 3: the lock order.** The commit takes the file's row and then the recall's; `recall_filled` and the worker's recall steps take the recall's and then the file's. I did not change the order. A test makes the two cross for real: Postgres ends one with a deadlock error; `api/db.py` turns that into `unavailable` with a 1 s retry (the edge retries reports and commits); the worker's queue lists it as a conflict, not a failure; and the one that was ended goes through on its next try.
- One more crossing came with the count: the worker moves a placement on while it holds the recall's row, and a placement that ends takes the recall's row after its own. The count is only taken when a job starts or stops waiting, never on a step between two waiting states, so the worker's own step does not cross itself. A placement cancelled at the instant its recall changes state can still cross the worker; the worker retries, and the portal's cancel would fail once.

**The review, 4:** a writer that holds a truncated stub open for over an hour still gets its recall failed by the idle rule before the commit supersedes it. Not changed; it is the "one window" below.

## After the second review (on 6653e724)

**1. A job that fails or is cancelled after its file moved on.** True. Only a job that was done closed its recall. One that let go left an active recall no edge will fill: a deleted file's was failed by the idle rule an hour later, with a failure card; a replaced file's stayed `downloading` for good.
- **A kept recall is marked when its file moves on** (`moved_on_version_id`: the version of the commit that would have ended it).
- **When the last job lets go** (it failed, was cancelled, or its row was deleted) and the recall is not pinned, the catalog ends it in the job's own transaction: `superseded`, with the reason and the version of that commit, and `notify` queued. It ends as it would have if no job had kept it, only later. A file replaced and then deleted ends `replaced`, by the first.
- **A deleted file's recall with no mark** (asked for after the delete, or kept since before the migration) ends as `deleted` the same way: that is the one the idle rule would fail.
- **A job that had its bytes** (`done`, `placed`) still closes its recall itself, as `available`.
- **Narrower than the review's wording, on purpose.** The review said: when the count reaches 0 and the file is deleted or holds another version. That also covers a recall of a version that was already an old one when it was asked for (a restore of last week's version that fails because its name was taken). Its card would say "replaced while it was being recalled", which is false, and name whoever last wrote the file. Nothing judges such a recall today: the idle rule looks only at the file's current content. So it is left as it is on main, active until a later reader or restore of that version uses it. **Recommendation:** end it `cancelled` when its last job lets go, as a follow-up; nothing sets `cancelled` today, so its card and the portal need a look first.

**A race found on the way, and fixed.** A commit and a job letting go at the same instant left the recall active on both sides. The commit's statement tested the count in its WHERE: the row did not match as the statement first saw it (still held), so it was not waited for, and the job, whose transaction came first, saw a file that had not moved on. The commit now passes over every active recall of the old content in one statement, with no test of the count in its WHERE, so a row a job has locked is waited for and read again. A test holds the job's transaction open, starts the commit, sees it wait, and sees the recall end.

**2. A reader holding a deleted file's inode when its kept recall closes `available`.** It hung. The first M3 run with the new step ran on the edge built from 7daa545c (my mistake in the lane order, below), so it is the run without the fix: 150 passed, 2 failed. The restore was done and the recall `available` 3 s after the clock passed the rehydrate; the reader was still waiting 180 s later, and the note was still there.
- **The edge now ends what it kept** for an `available` row with no live placeholder, when a reader is still waiting: `FAILED` and `EIO` if the reader's inode is no longer the file, `CANCELLED` if it is (replaced in place; nothing was filled into it). The note goes. An entry the edge's own fill already ended is left alone.
- **M3 step 13c** (real NFS, the real FSAL, the worker's own restore job): a reader waits on `wait/held.segy`; an owner's restore of its current version as `held-restored.segy` joins the recall (`held_by_jobs` 1); the file is deleted on the edge; the recall goes on, `rehydrating`, and the reader still waits; after the rehydrate the restore is done, the recall is `available` and no longer held; **the reader ends with `Input/output error`** and nothing past the resident header it had read; the note is gone; and `held-restored.segy` reads byte-identical to the deleted file's version.

**A deleted job.** Both tables' triggers now fire on DELETE too: a waiting job that is deleted is counted off and lets go like any other end. Nothing deletes one today.

**A mistake in my lane order.** I ran `m3 m1 m2 m5` to see the new step first. Only the M1 lane installs the edge, so that M3 ran on the daemon built from 7daa545c (sha256 `d4b4d4e12199c80b` in its results header). It is the evidence above, and I ran M3 again after M1 had installed the new edge.

**Step 13b failed once, and its check was wrong.** In the M3 run on the new edge (b9d99ff0: 151 passed, 1 failed), the replaced file's reader ended with exit 0 after 2 MiB: its 1 MiB of resident header, then 1 MiB of zeros. The check asked for no zeros and the whole new file. The reader was released during the replace (inside the writer's 0.22 s), not after it as in the three runs before (0.37 to 0.39 s after the replace began).
- **Read from evidence:** the reader's bytes and the timing. And a probe on the same VM, export and mounts, with ordinary files that never had a marker: a reader reads 1 MiB over NFSv4.2, then reads on while another client (NFSv3) truncates and rewrites the file with `cp`. **5 of 40 such readers read zeros** (8 to 50 blocks of 64 KiB each); none of the 40 files had a marker; none read a byte that was in neither file.
- **Inferred:** that the M3 reader's zeros are the same thing. I did not capture its READ replies. The FSAL removes a stub's marker only together with its truncate to 0, with every other operation on the inode held back until the truncate is done (`omni_trunc0_begin`), so after it the stub's holes no longer exist; what the reader then reads is the new file, as far as the other client's writes have landed. The short end at 2 MiB did not occur in the probe, whose readers all reached 16 MiB.
- **The check now asks for what holds:** no error, nothing of the old version past the header the reader had read, and no byte that is in neither file. It still says "no zeros, every byte the new file's" when that is what the reader got, and says what it got otherwise. A new read after the replace must still be the whole new file, byte-identical (unchanged).
- **This touches "no zeros", so it is the coordinator's to confirm.** The alternative is the strict check, which fails when the reader's release falls inside the write (one run in five here), on what a reader of any file gets while another NFS client rewrites it.
- **For the soak:** its judge counts zeros in any read. If the long run shows zeros in a read of a file another client was rewriting at that moment, look here first.

**Step 13c then found a bug in the restore's close.** In the next M3 run (647509ac: 149 passed, 3 failed) the restore was done but its recall was not closed. The recall was still `rehydrating` when the restore finished: the restore was done at :08.33 and the recall's own poll marked it `downloading` at :09.01 (the catalog's state log). The restore reads the bytes as soon as the change feed has recorded the recall's copy, and its close looked only at a recall that was already `downloading` (#55's test of the state, which the first review's fix kept). The recall then became `downloading` for a deleted file, held by no one: the reader waited on, the note stayed, and the idle rule would have failed the recall an hour later.
- **Fixed:** a restore that is done closes its recall from any active state, when the file is deleted or holds another version. The provider's restore under it is closed by the change feed, as for any recall that has ended.
- **Wider in production than here:** the restore looks again every 60 s and the recall's poll has its own interval, so which comes first is chance. The run before this one had them the other way round, and passed.
- It also covers #55's own case: a restore of an old version that was done before its recall was marked `downloading` left it `downloading` for good.
- Test: the "closed when its file was deleted or replaced" test now runs with the recall `rehydrating` too (4 cases). Undo S43.

**The new steps made steps 13 and 14 late, twice.** The dev stack stubs a recalled file again one to three seconds after its fill: its worker runs with `OMNIFILE_STUB_AFTER_MINUTES=0`, and M3 does not change that, as M1 and M2 do. Steps 13 and 14 check their recalled files inside that time ("resident, local CRC = the catalog's", and a failfast read that must succeed). My after-the-rehydrate checks for 13b (in this PR since afe8e54b) and for 13c stood before them. Two runs failed there: step 13's "back on the edge" for two files (b0a169c5, the second run), and step 14's two failfast reads (f9b676f6, the third run). In both, the edge's log shows the files stubbed again 2 to 3 s after their fills.
- The checks now come after step 14. From the clock advance to the end of step 14 the test is the same text as on main (compared with `difflib`).
- **Corrected after the merge:** the cause is not the worker's setting alone. The edge keeps a finished recall's entry for 30 s (`recall_valid_ms`), and while it does, a read of a file the dev stack has stubbed again is refused (EIO) instead of filled. Step 13's `wait_for 60` absorbed that; step 14's two reads had no wait and failed 3 runs in 8 on this branch. They now have the same minute. This changes a step that is main's. M3 could also set `OMNIFILE_STUB_AFTER_MINUTES=525600` as M1 and M2 do, after checking that its step 15 does not rely on the 0.

## The short soak, before the long one

On this branch's build at afe8e54b (before the review's changes), 30 minutes, one fault of each kind, seed 1952689915:
- **0 violations** in every invariant; 1,532 good reads, 304 writes; 30 recalls, all `available`.
- **All 8 faults injected,** `restart_mid_fill` among them (it was skipped 4 times in the 2026-11-08 rerun).
- **No write or read error with no fault active.** After `fill_disk` (62 GiB) the only errors were SMB's during `kill_smbd`. In the 2026-11-08 runs every `fill_disk` was followed by ten minutes of ENOSPC: the detector's fix (#94) is what changed.
- `journal.log.gz` was saved.
- The report is RED on one load minimum that 30 minutes did not reach (`smb.archived` 7 of 10). The minimums are for the 6-hour run.

## Tests

**Service** (each on its own Postgres 17, with every app's connections under its own database role):

- **`tests/api/test_recall_superseded.py`** (16), through the real upload, `changes` and recall endpoints:
  - a new content version ends the recall of the one it replaces, from each active state (`requested`, `rehydrating`, `copying`, `downloading`): `superseded`, `replaced`, the new version named, no failure text, one more `notify`, and the feed carries it. A late `…/failed` report answers 204 and changes nothing;
  - a delete ends it as `deleted`, and the tombstone records who, proven;
  - a pinned recall outlives a replace and a delete, and joining pinned pins a recall that wasn't;
  - a recall of an older version is left alone while the current one's is ended;
  - a rename or an attribute change ends nothing;
  - a recall that already ended (`available`, `failed`, `cancelled`) stays as it ended, and no one is told again;
  - the next read after a replace is online, and the old version can still be asked for;
  - `superseded` and its reason go together (a CHECK);
  - **a Keep online hold and a tier pin are as they were** after the replace and after the delete;
  - an edge that has not said it knows the state is sent `cancelled` in the feed, and `superseded` once its heartbeat says so; the catalog and the portal hold `superseded` throughout;
  - a commit and a recall step that take the two rows in opposite orders: one gets Postgres's deadlock error, both sides read it as "try again", and the retry goes through.
- **`tests/notify/test_cards.py`** (3 more): the card's title, sentence and buttons for `replaced` and `deleted`; none of "Couldn't recall", "try again", "storage service"; the mail; "Kept online until" still shown; who is named only when given, escaped, and never on the no-access card.
- **`tests/notify/test_delivery.py`** (3 more), with the fake Graph and Bot Connector: the commit itself queues the card; the card is updated in place and Keep online is revoked; "Deleted by Ana Ruiz" on proof, no name on a claim, nothing to someone who lost access; one last mail.
- **`tests/web/test_rewind_recalls.py`** (22 in the file; 3 were added first and 7 after the reviews):
  - the portal's `superseded` block and steps (`done, done, pending, pending`); the `state=superseded` filter; `by` to a reader on proof, null to an ArchiveAdmin who can't read the file and on a claim;
  - `test_a_version_restore_holds_the_recall_it_waits_on`: `held_by_jobs` is 1 for a recall the restore joined and for one the worker started, a replace leaves both `requested`, and the count is 0 when the restore ends;
  - `test_a_restores_recall_is_closed_when_its_file_was_deleted_or_replaced_meanwhile` (tombstone, content): the file changes while the restore waits, the restore runs to `done`, and its recall ends `available`, not `downloading`;
  - `test_a_recall_is_an_ordinary_one_again_once_its_restore_is_done` (content, tombstone): the file changes after the restore is done, and the recall ends `superseded` with the right reason;
  - `test_a_restore_whose_recall_was_superseded_recalls_the_version_again`;
  - `test_the_migration_counts_the_jobs_that_were_already_waiting`: the counts are zeroed, the migration's own backfill statement is run, and a waiting restore's and a waiting placement's recalls are held again; a finished job's is not.
  - after the second review (7 more, and 2 more cases of one: 31 in the file): `test_a_restore_that_fails_ends_the_recall_its_file_moved_on_from` (replaced, deleted, replaced then deleted), `test_a_restore_that_fails_leaves_a_recall_that_is_still_wanted` (the file as it was, a pinned recall, two jobs), `test_a_deleted_files_recall_ends_as_deleted_even_with_no_mark`, `test_a_waiting_restore_that_is_deleted_lets_go_of_its_recall`, `test_a_commit_waits_for_a_job_that_is_letting_go`.
- **`tests/web/test_annex_returns.py`:** an annex placement holds its recall (`held_by_jobs` 1), and the recall outlives a newer copy. Its requester cancels it through the portal: the recall ends `superseded` (`replaced`) and `notify` is queued. A placement cancelled with the file as it was leaves the recall `requested`, unheld; and a waiting placement that is deleted lets go (1 more test).
- **`tests/web/test_partner_events.py`** (1 more): a `recall.state` event for `superseded` carries `superseded_reason`; the others do not.
- **`tests/worker/test_recalls.py`** (2 more): the file is replaced while its rehydrate runs: the recall is `superseded`, its poll ends, the restore stays `in_progress`; the change feed then closes it and commits the copy; the recall-copy expiry leaves the copy inside its window and queues its delete after. And no rehydrate is started for a recall that ended before the worker got to it.

**Edge** (the omnifile-edge VM, root, unguarded xfs):

- **`daemon_linux.rs`** (6 more, and one extended):
  - `a_fill_that_meets_a_file_replaced_in_place_reports_nothing`: the marker removed and new bytes written, as the FSAL's truncate leaves a stub;
  - `a_fill_that_meets_a_newer_marker_reports_nothing`: the soak's case;
  - `a_fill_that_meets_another_file_under_the_name_reports_nothing`: a rename over it;
  - `a_fill_whose_file_was_deleted_reports_nothing`: the file is gone from the disk, and the tombstone reaches the replica while the edge waits to be sure;
  - `a_superseded_recall_of_a_file_that_moved_on_is_tidied_away`: the entry ends `CANCELLED`, the note goes, and the reader's next read is of the new version and gets its bytes;
  - `a_reader_waiting_on_a_file_that_is_deleted_is_released`: the entry ends `FAILED` with `EIO`, and the note goes;
  - `a_rewind_placeholder_of_the_current_version_is_filled_itself` now also expects its `recall_start` to be pinned, and a live read's not to be;
  - `the_heartbeat_reports_health_and_renews_the_certificate` now also expects `recall_superseded: 1`;
  - after the second review (2 more): `a_reader_of_a_deleted_file_is_released_when_its_recall_ends_available` (`FAILED`, `EIO`, the note gone, nothing reported) and `an_available_recall_of_a_version_the_file_left_is_tidied_away` (`CANCELLED`, the note gone, the next read is of the new version).
- **`review_m1_linux.rs`:** `a_superseded_recall_stops_its_fill_and_reports_nothing`.
- `a_failed_recall_fill_is_reported_failed` (a CRC mismatch) still passes: a real failure is still reported.

**The M3 exit test, step 13b** (`tests/e2e/m3_exit_test.sh`, real NFS and the real FSAL): two more readers wait on archived files of the `wait` share over NFSv4.2, with the others.
- **Replaced** from another client (the NFSv3 mount), with `cp` over it: the write did not wait for the recall (0.2 s). The waiting reader was released 0.37 s later with no error and no zeros: after the 1 MiB of resident header it had already read, every byte was the new file's. A new read got the whole new file. The recall ended `superseded` (`replaced`) when the new version was committed, with no failure text; the note was gone; the old version's rehydrate was still `in_progress`; and after the virtual 15 hours the change feed had closed it, with its copy committed, and the file still held the new bytes.
- **Deleted** on the edge: the recall ended `superseded` (`deleted`) with the tombstone; the note was gone; the waiting reader was released 1.3 s later with `Input/output error` and nothing past the resident header it had already read.
- The edge reported no failed fill for either.
- The writer had to be a second client. On one Linux NFS client an open with truncate waits for a read of the same file that is in flight (`do_truncate`, the inode's lock), so on one mount the first try showed the client waiting on itself, not the product.

**The soak's judge** (`tests/chaos/test_soak.py`): a `superseded` recall is accepted only when what it says happened did. Superseded as replaced, the file must hold another version (or be gone); as deleted, it must be gone. Anything else is judged as before.

**Portal** (`recalls.test.tsx`, 2 more): the recall page says "The recall stopped" with the reason, the steps `done, done, pending, pending`, no "Recall it again" and no alert; the list shows "File replaced" and the new filter.

**The harness** (`tests/real`): RT-45, a manual tenant check for the new card, with its mapping; the counts are 145 lines, 45 for the tenant.

## A harness bug the short soak found

Before the long run I ran a 30-minute soak on this branch's build. Its set-up refused to start: "the worker's effective OMNIFILE_STUB_AFTER_MINUTES: unset". That check is mine, from #89. It read the worker's environment right after `systemctl restart`, when the unit's main process was still systemd's own child about to exec the worker, so the setting was not in it yet. It had passed in the 2026-11-08 rerun by timing.

`worker_effective` (`tests/chaos/lib/devstack.sh`) now looks again for up to 10 s while the answer is empty. Two tests in `tests/chaos/test_soak.py`: a process that gets the setting one second later, at its exec, under the same pid; and a worker that never has it, which still answers empty. On the VM the first fails on the old function (1 failed, 30 passed) and passes on the new (31 passed).

## Undo pass

Each part undone alone, the tests that should notice run, the part put back.

**Service, the soak's judge: 43 of 43 caught** (`fx2-undo-local.py`, run again in full on 4999666d, twice):

| Undone | Caught by |
|---|---|
| S1 the commit ends no recall | `test_a_new_content_version_ends_the_recall_of_the_one_it_replaces` |
| S2 a pinned recall is ended too | `test_a_recall_someone_asked_for_by_version_is_left_alone` |
| S3 a delete ends nothing | `test_a_delete_ends_the_recall_as_deleted_and_records_who` |
| S4 a rename or an attribute change ends recalls too | `test_a_rename_or_an_attribute_change_ends_nothing` |
| S5 the commit tells no one | the first test (its `notify` count) |
| S6 a late failure report fails a superseded recall | the first test |
| S7 the edge's `pinned` is ignored on a new recall | the pinned test |
| S8 joining pinned does not pin | the pinned test |
| S9 a superseded card reads as a failure | `test_a_superseded_recall_says_what_happened_not_that_it_failed` |
| S10 a superseded card offers Recall | `test_buttons_per_state_and_audience[superseded]` |
| S11 a card names a person on a claim | `test_a_deleted_files_card_names_who_only_to_a_reader_and_only_on_proof` |
| S12 a card never names who | the same |
| S13 no last mail for a superseded recall | `test_a_superseded_recall_gets_its_one_last_email` |
| S14 the portal names who to someone who can't read the file | `test_who_deleted_it_is_told_to_a_reader_and_only_on_proof` |
| S15 the portal names a person on a claim | the same |
| S16 the portal marks a superseded recall's next step failed | `test_a_recall_of_a_replaced_file_reads_as_superseded_in_the_portal` |
| S17 a job does not hold its recall (the commit ignores the count) | `test_a_version_restore_holds_the_recall_it_waits_on` |
| S18 a version restore is not counted (no trigger on `version_restore`) | the same |
| S19 an annex placement is not counted (no trigger on `annex_placement`) | `test_a_file_in_archive_is_recalled_first` |
| S20 the soak accepts any superseded recall | `test_a_superseded_recall_is_accepted_only_when_what_it_says_happened` |
| S21 the soak fails every superseded recall | the same |
| S22 the migration does not count the jobs already waiting | `test_the_migration_counts_the_jobs_that_were_already_waiting` |
| S23 a restore waits for ever on a superseded recall | `test_a_restore_whose_recall_was_superseded_recalls_the_version_again` |
| S24 a finished restore leaves a deleted file's recall `downloading` | `test_a_restores_recall_is_closed_…[tombstone]` |
| S25 a finished restore leaves a replaced file's recall `downloading` | `test_a_finished_restore_closes_its_recall` |
| S26 an older edge is sent `superseded` | `test_an_edge_that_predates_the_state_is_told_cancelled` |
| S27 the heartbeat's `recall_superseded` is not kept | the first test (its feed check, as an edge that said it knows) |
| S28 a partner's event does not say why | `test_a_superseded_recalls_event_says_why` |
| S29 a job that ends still holds its recall | `test_a_version_restore_holds_the_recall_it_waits_on` |
| S30 a job that lets go ends nothing | `test_a_restore_that_fails_ends_the_recall_its_file_moved_on_from` |
| S31 a kept recall is not marked | the same |
| S32 a job that lets go ends a pinned recall | `test_a_restore_that_fails_leaves_a_recall_that_is_still_wanted` |
| S33 the first job to let go ends the recall | the same |
| S34 a job that had its bytes ends its recall as superseded | `test_a_restores_recall_is_closed_…[tombstone]` |
| S35 a deleted file's recall with no mark is left | `test_a_deleted_files_recall_ends_as_deleted_even_with_no_mark` |
| S36 a job that lets go ends a recall of content the file still holds | `test_a_restore_that_fails_leaves_a_recall_that_is_still_wanted` |
| S37 a deleted restore is not counted | `test_a_waiting_restore_that_is_deleted_lets_go_of_its_recall` |
| S38 a deleted placement is not counted | `test_a_cancelled_placement_leaves_the_recall_of_a_file_that_is_as_it_was` |
| S39 a deleted job that had ended lets go again | `test_a_waiting_restore_that_is_deleted_lets_go_of_its_recall` |
| S40 the commit tests the count in its WHERE, so does not wait for a job | `test_a_commit_waits_for_a_job_that_is_letting_go` |
| S41 the reason is the file's state now, not that of the commit that passed the recall by | `test_a_restore_that_fails_…[replaced then deleted]` |
| S42 a recall ended when its job lets go tells no one | `test_a_restore_that_fails_ends_the_recall_its_file_moved_on_from` |
| S43 a restore done before its recall is marked `downloading` leaves it | `test_a_restores_recall_is_closed_…[tombstone-rehydrating]` |

**Edge, on the VM: 12 of 12 caught** (`fx-undo.py fix2`, run again in full on 4999666d):

| Undone | Caught by |
|---|---|
| E1 the edge does not know `superseded` | `a_superseded_recall_stops_its_fill_and_reports_nothing`, the tidy test, the deleted-reader test |
| E2 a fill that meets another file is reported failed | the three `a_fill_that_meets_…` tests |
| E3 a recall left behind is not ended | the tidy test, the deleted-reader test |
| E4 a deleted file's row is applied to its (gone) placeholder | the deleted-reader test |
| E5 a reader of a deleted file is told to wait again (`CANCELLED`, not `FAILED`) | the deleted-reader test |
| E6 a deleted file's note stays | the deleted-reader test |
| E7 a rewind read is not pinned | `a_rewind_placeholder_of_the_current_version_is_filled_itself` |
| E8 the disk is not asked, only the replica | the three `a_fill_that_meets_…` tests |
| E9 a deleted file in the replica does not count | `a_fill_whose_file_was_deleted_reports_nothing` |
| E10 the edge does not say it knows `superseded` | `the_heartbeat_reports_health_and_renews_the_certificate` |
| E11 an `available` row for a file that moved on is ignored | both new tests; and M3 step 13c on real NFS (the run on the old edge, above) |
| E12 it ends a replaced file's entry `DONE` | `an_available_recall_of_a_version_the_file_left_is_tidied_away` |

**Not undone, and why:**
- The hold and pin test, the two worker tests and the M3 step guard behaviour this PR relies on but did not write (the trigger touches only `catalog.recall`; the worker already stops for a recall that isn't active; the change feed already closes a restore). There is no part of this PR to undo that would fail them, short of S1.
- `moved_on`'s "the replica holds another version" branch has no undo of its own: once a newer version is committed the marker on disk names it too, so the disk check catches the same case (E8's tests).
- The no-access card: `who_superseded` is skipped for a recipient who can't read the file, but that card shows nothing of the file anyway, so undoing the skip changes no output.
- The edge leaves an entry its own fill already ended when a late `available` row finds the file moved on. Undone, a reader of a filled, then deleted file could be sent `FAILED` for a few seconds; no test here waits for a message not to come.
- Three locks, which only matter when two transactions meet at one instant, and no test here makes them meet: the `FOR UPDATE` when a job joins a recall (`start_or_join(hold=True)`, the worker's `_start_recall`); the recall's row lock in `catalog.recall_count_jobs`; and the trigger's early return for a step between two waiting states (undone, the count is the same and only the lock is taken more often). The deadlock test shows what a crossing costs, not that these prevent one. (The commit's wait for a job that is letting go is tested: S40.)

**An existing test changed:** `tests/catalog/test_schema.py::test_one_active_recall_per_version` recalled a file's current version, committed a second version and went on using the first recall. That recall is now ended by the commit, so the test pins it, which is what a recall of an older version stands for there.

## Lanes

Head 7daa545c: the reviews' changes, on the branch merged with main at 502710d (#94, the detector fix). `tests/chaos/soak.py` conflicted in that merge, where #94's evidence block and this PR's judge meet; both are kept. The commit after it is this file, the M3 results and nothing else.

**Migration 0058 is #93's**, not merged yet. Every lane below ran with an empty stand-in for it (`SELECT 1`), never committed, so the catalog migrates 0001 to 0059 without a gap.

**On the omnifile-edge VM** (Ubuntu 26.04, kernel 7.0, joined to the dev AD), as root on unguarded xfs, the daemon stopped and its fd store cleared for the cargo run:

- `cargo fmt --all -- --check`: exit 0;
- `cargo clippy --workspace --all-targets -- -D warnings`: exit 0;
- `cargo test --workspace --no-fail-fast`: **981 passed, 0 failed, 1 ignored**, 0 "skipping" notices (the same count as before the reviews: the heartbeat's check went into an existing test);
- the exit tests, each on a catalog dropped and migrated afresh, the clock read +0 before and after each:

| Lane | Command | Result |
|---|---|---|
| M1 | `tests/e2e/exit_test.sh` | 83 passed, 0 failed |
| M2 | `tests/e2e/m2_exit_test.sh` | 111 passed, 0 failed |
| M3 | `tests/e2e/m3_exit_test.sh` | **137 passed, 0 failed** (110 before this PR; step 13b and its two files add 27). `tests/e2e/results/m3-latest.md` is this run |
| M5 | `tests/e2e/m5_exit_test.sh` | 139 passed, 0 failed |

**On the Mac, at 7daa545c:**

- Service, `uv run pytest -q` on its own Postgres 17: **2757 passed, 51 skipped** (2748 before the reviews, and 9 more tests). `ruff check` and `ruff format --check` on `src` and `tests`: clean.
- Portal: `npm test` **337 passed in 39 files**; `npm run lint`, `npm run typecheck` and `npm run check:client` clean (`schema.gen.ts` was regenerated for the partner event's text).
- M4, `tests/e2e/m4_exit_test.sh` on its own Postgres and ports from 19400: **service 15/15, browser 10/10**.
- The chaos harness's tests, `pytest tests/chaos/test_soak.py`: 32 passed, 4 skipped (the skipped need Linux's `/proc`).

**At afe8e54b, before the reviews.** The VM lanes and M4 had the same counts there. These were not run again, as the reviews changed nothing in `deploy/`, `tests/real`, `tests/chaos` or the portal's own code:

- The portal's main chunk: 521.5 kB (budget 600).
- The chaos harness's tests on the VM: 31 passed.
- The real-cloud harness's own tests, `uv run pytest -q` in `tests/real`, fakes only: **313 passed**.
  - Its Azure golden transcript was stale on main: `deploy/` changed on 2026-11-08 (#91 and decision 64's PR) and the payload size in the golden stayed at 2026-11-07's. `deploy/` is the same here as on main, and the test failed on that one line (603,648 against 605,184 bytes). I updated the line; nothing else in the transcript changed.
- The 30-minute soak (above).

**Not run:** the long soak. It runs on main once this is merged, and that run is the one that counts. The permission matrix (no ACL code changed) and the Windows kit.

## Final state, and the soak that is still owed

**Confirm pass on the changes after the second review** (a fresh reviewer, on `6653e724..be047401`, product code only): no blocking finding. It checked the job count (recomputed, never decremented twice), the lock order (file, then recall, on every path), the commit and job-end orderings (none leaves a recall active), the edge's handling of `available` (nothing makes an unfilled stub readable) and the migration on a catalog that already holds recalls.

**One finding, fixed.** A restore that was done closed its recall as `available` even when a rewind reader had pinned it. The edge then saw `available` before it had `downloading` and its credential, so the label was never filled and the reader had to ask again (a retry, never zeros). The restore now leaves a pinned recall to the edge. Test: `test_a_restore_that_is_done_leaves_a_recall_a_rewind_reader_pinned` (`downloading` and `rehydrating`); both cases fail without the fix.

**Step 13b's check, confirmed by the coordinator.** A reader that is released while another NFS client is rewriting the file may read zeros where that client's writes have not landed yet. That is what any NFS reader of a file being rewritten gets (5 of 40 plain files in the probe above); it is not a stub's hole read as data, which stays an error. The check asks for what holds.

**Lanes at the final head** (the branch merged with main at 00512dc):
- Service, `uv run pytest -q` on its own Postgres 17: **2783 passed, 51 skipped**; `ruff check` and `ruff format --check` clean.
- M3 on omnifile-edge at 2ef4e185: **152 passed, 0 failed**, three runs in a row in the lane summary, and a fourth whose results file (2026-11-12T05:17:44Z) also shows 152 passed. At 4999666d it was 151 passed, 1 failed (step 14, above). With the new steps M3 failed in 9 of 22 runs before these fixes: once on 13b's zeros, the rest on steps 13 and 14 and on the author's own lane mistakes (one run on the old edge; checks first placed before steps 13 and 14; a first step-14 fix that was wrong).
- At 4999666d (merged with main), on the VM: M1 83 passed, 0 failed; M2 111 passed, 0 failed; M5 139 passed, 0 failed; `cargo test --workspace` 984 passed, 0 failed, 1 ignored; fmt and clippy clean. Locally: portal `npm test` 341 passed in 39 files, with lint, typecheck and `check:client` clean; M4 15/15 and 10/10; `tests/real` 313 passed. After 4999666d only `m3_exit_test.sh`, this file, and the pinned-recall fix with its test changed.
- The main baseline for M3 (5 of 5 passed, 110 checks) ran with this branch's edge binary: `git archive` timestamps stopped cargo rebuilding. It is a baseline for the test script and service, not for main's edge.

**Found on the way, not changed here:**
- The dev stack stubs a recalled file again within seconds of its fill, and the edge refuses a read of it (EIO) for the 30 s it keeps the finished recall's entry. M3's step 14 now waits for that. A real worker never stubs a file that soon; M3 could set `OMNIFILE_STUB_AFTER_MINUTES=525600` as M1 and M2 do.
- A recall of a version that was already an old one when it was asked for, whose restore then fails, stays active until a later reader or restore of that version (as on main). It could end `cancelled` instead.
- The edge could drop its kept entries when it stubs a file, which would remove the 30 s refusal above.

**The long soak has not run on this code.** Run it once on main before a first client or a release. On omnifile-edge, as root, from a checkout of main (omnifile-dc must be up):

```bash
REPO=$PWD bash dev/scripts/install-edge.sh
REPO=$PWD bash dev/scripts/install-samba.sh
bash tests/chaos/storage.sh on
# a fresh catalog: drop the catalog and pgmq schemas and public.schema_migrations, then
REPO=$PWD bash dev/scripts/install-service.sh
systemctl reset-failed
AD_ENV_FILE=/work/dev/.secrets/ad.env bash tests/chaos/soak.sh --label main-after-96
```

With no arguments it runs 6 h 15 min, every fault 12 times. The report goes to `tests/e2e/results/chaos-main-after-96.md`, and the run's evidence to `/work/dev/.artifacts/wp3/soak-<seed>/`. `bash tests/chaos/storage.sh off` afterwards. If it shows zeros in a read of a file another client was rewriting at that moment, see step 13b above first. Check first that the checkout is main at or after c4ed9eac, and that `install-edge.sh` reports the omnifile crates compiled: a copy made with `git archive` can leave cargo thinking nothing changed, so touch the edge sources if it compiles none.
