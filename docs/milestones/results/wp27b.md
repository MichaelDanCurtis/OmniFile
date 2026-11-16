# WP27b: a copy's check through an outage, and what the re-check of #76 found

Branch `claude/read-back-outage-hold`, off main at 2b4462e. One migration, three columns on `migration_count`, numbered 0046 on this branch (below). No new privilege. Tests: `service/tests/migrate/test_read_back_hold.py`, real Postgres, sim and the S3 fake, through the real consumer. Each constant is one line. No product decision needed, though item 1's reading of "12 tries" is mine.

## 1. An outage holds, and a give-up stays on the prefix

**Reproduced on main.** After a dead letter and a new run: `last_error` NULL, a `pending` location, state `verifying`, no verify message. `_record` skips a pending copy, so no run revives it. No data is at risk, since nothing cuts over.

**The hold.** It comes before the last try, not after try 12:

- After 11 failures the worker asks for the object, with `redrive.Probe`, the same code #73 uses for recalls, with no transaction open. It also asks for the S3 object when the copy's checksum must still come from there.
- **No answer is an outage.** The try waits, asking each hour, for up to 3 days (`RedriveConfig.outage_limit`). It spends no read, and `last_error` says why it waits. Past the limit, the last try goes ahead.
- **An answer means the provider is up.** The last try goes ahead. If it fails, the message dead-letters.

So a slow read ends at exactly 12 reads, as you asked, and a recall-style "one more try" after try 12 would have made it 13. And the last try is never spent during an outage, which is what the extra try is for in `redrive.py`. I kept this in the handler rather than a new dead-letter sweep, since it needs no letter to exist during an outage, so the `dead-letters` alert fires only for a real give-up.

**The give-up stays visible, as a count.** It took three tries.

1. In `last_error`, with a query each step. The review of 15027b1 reproduced two faults. It matched letters on the prefix that sent them, so a cancelled prefix's letter left the next prefix `verifying` with health `ok`. And it overwrote `last_error`, so a failed run showed the give-up text, and an access problem or an expiring warning couldn't show.
2. Read by the console from the queues, with a grant to `web`. The re-check of 9ffce87 refused that: a letter holds an `error` and a `traceback` that can carry provider text and object keys, and the read cost about 80 s per prefix at 10^8 on a console polling every 15 s.
3. **Now:** `progress_sweep` counts them, as it does `failing_now`, and the console and the API read the number. `web` has no read on any queue it doesn't send to. At the give-up the handler clears its own `retrying` note, so a copy sent again that works leaves nothing stale.

**Three counts on `migration_count`,** each only of a pending copy that holds up the cutover: its file's current content, with a committed S3 object, not excluded. So a copy whose source is gone, or an old version's, isn't counted.

| Column | What it means | Item in the console | Fix on offer |
|---|---|---|---|
| `copies_given_up` | Its check went to the dead letters. Matched by the copy, whichever prefix sent the letter. | `check_given_up` | None: sending it again is an administrator's command |
| `copies_stranded` | The S3 checksum is known, no check is queued, no letter | `copy_stranded` | None: a new run doesn't revive it |
| `copies_no_checksum` | No S3 checksum on record, no check queued, no letter, and nothing computing one: a job came and went with no value (SSE-C, or its attempts ran out), or the backend has no Batch Operations | `no_checksum` | Exclude |

An SSE-C copy does hold up a cutover, so it gets its own true message, not "stranded". Nothing says a letter was dropped, since nothing knows that.

**No flash.** A copy waiting for the next step to ask for a job, or on a running job, including one later than that job's objects, is counted as none of these. Whether a source runs Batch is in the worker's binding, not the catalog: `catalog.backend.config` refuses a `batch` key, as my first version found. So `migrate_jobs` passes the sweep a `has_batch`.

**Cost, measured at 10^6 pending copies, about 10^6 queued messages and 100,000 letters,** on the Postgres 17 container, with a full undo pass running beside it for part of that time:

| Query | Time | When it runs |
|---|---|---|
| `GIVEN_UP` | 0.4 s | Each sweep (5 minutes), for a prefix in `copying` or `verifying`. It starts from the letters. |
| `STUCK` | 1.8 s | The same, only if the prefix verified nothing since its last count. |

- `STUCK` reads both queues whole, so a prefix making progress skips it and keeps its last count. Linear, a stalled prefix at 10^8 takes about 3 minutes. Not run at 10^8.
- **So a stranded or no-checksum copy shows up to two sweeps after the prefix stalls,** about 10 minutes, and `stuck_copies` can lag by that much.
- **No id through the scans.** A count takes none until its `INSERT`, the last statement, so a long one doesn't hold back the feed: `test_counting_holds_no_id_until_it_writes`.

**The migration's number is 0046 here, not 0049, and must change at merge.** `catalog/migrate.py`'s `discover` refuses gaps (`versions must be [...]`), so a 0049 with no 0046 to 0048 fails every app at start, and every test that migrates. So the branch carries `0046_migration_count_checks.sql`, the number that runs on main today. If #70, wp24 or #77 merge first, `git mv` it to the next free number before merging: two files numbered 0046 fail the same way, at the first migrate. Nothing else names the number, except the file's own first comment.

**On the Migrations page and API.** `Migration` gains `stuck_copies: {given_up, stranded, no_checksum}`, numbers only. The page shows a note for each, and the console's table does too. Readers are the same as for the rest of a migration: an ArchiveAdmin, or an app with `Archive.Read` or `Archive.Adopt`. A DataSteward gets 403, on the list, the item and the console, and a test says so. So this shows nobody more than `last_error` already does.

**A copy clears only its own note.** The note now names its location, so one copy's give-up or recovery leaves another's alone.

**What a hold says.** As in `redrive.Probe`, any error but a 404 counts as no answer, so a lost permission holds for 3 days too. The hold stays, but `last_error` now says what came back, and a 403's text also raises the console's `access` item. `Probe` keeps the reason in a new `why`.

## 2. A late check's error, confirmed and fixed

A check failing on a prefix already cut over overwrote `LEFT_BEHIND`, which ended the `staging-left` alert early. Reproduced: `'checking a copy failed 1 of 12 times…' == '1 staging objects…'`. The handler now writes only in `pending`, `copying`, `verifying` and `verified`. The step's own provider-error write isn't changed: it needs a provider call to fail, and no step of a cut-over prefix makes one.

## 3. The two missing tests

- `test_a_recovery_clears_only_its_own_error`. My earlier undo of this was caught only because the mutated SQL was invalid (`%s IS NOT NULL` with an untyped parameter), so the "catch" was false. You were right that nothing tested it.
- `test_tries_are_at_most_an_hour_apart`.

Both passed on main, as they should: they guard code that was right.

**A correction.** "About 7 hours" was wrong. The waits after tries 1 to 11 are 1, 2, 4, 8, 16, 32, then 60 minutes five times: 363 minutes, about 6 hours to the last try, plus each read's own time. The code comment and the design note now say 6.

## 4. A cutover's tries at a delete

Reproduced: 10 delete calls per key (`assert 10 == 5`). `unstage` tried at the top of every `verified` step, uncounted, and `drain_staging` tried again. I removed that top call rather than counting it. `drain_staging` covers those objects anyway, a little later in the same step, and counting it would have meant 2 tries per step, so 5 tries would be 3 rounds. So a cutover now makes exactly 5.

`unstage` still runs while a prefix is `copying` or `verifying`. Those tries don't count, and the note says so.

One existing test changed in what it asserts: `test_a_cutover_removes_every_staging_object_before_it_ends` had `stats.acked == 2`, and is now 3, since each step now removes one object, not two.

## 5. Abandoned waves

It was small, so it's in. `waves.close_abandoned`, a sweep each hour (`abandoned_waves`), closes waves `running` past `WAVE_WINDOW` and fails their restores still open, as `wave_progress` does for a live prefix.

I included **cut-over prefixes** as well as cancelled ones. A prefix can verify before a step looks at its finished wave again, and then nothing ever would. Completed restores stay completed. A live prefix's wave, and a `failed` prefix's (a `retry` brings its steps back), are left alone.

Each wave has its own transaction, as the review asked: one can have 100,000 restores, and an id held across many would hold back every edge's feed. A `failed` prefix is left alone, now with a test.

Not changed: what happens to a recall joined to a restore that fails this way. That is the same as for a live prefix's wave past its window today.

## Tests

**Undo pass: 51 undos, one at a time, 51 caught**, each against the whole of `test_read_back_hold.py` or `tests/web/test_migrations.py`, listing every test that failed, with valid SQL. Read it with care: in the full pass, 1 survived, and 2 looked caught only because the 10^6 measurement's long transaction was holding back the feed and failing `test_migrations.py` at baseline. Those 3 were rerun alone and caught, the survivor after its test was fixed, and the steward one after I corrected it, since it had changed the wrong module. It covers what I thought to undo.

**Survivors on the way, each now with a test:**

- Making the probe inside the transaction. It holds no id there, only read locks. `test_asking_for_the_object_holds_nothing`.
- A step clearing any check error, in code since removed. That mutation was invalid SQL at first, a false catch, and it survived once valid.
- From this round: a letter for another prefix's copy. Its test read a prefix in `pending`, where nothing is counted. And a stale check of the steward's refusal: my undo changed the wrong module, so I corrected it.
- From the review: the hold's spacing, the probe's source half (both ways round), and the sweep taking `failed` prefixes, which I had said was left alone with no test behind it.

**Existing tests changed in what they assert,** besides the one under item 4: `test_a_read_back_that_keeps_failing…` asserted "gave up" in `last_error`. It now asserts `last_error` is clear, since the count says it.

## Lanes

At a25893a. The commit after it changes only this file.

`AWS_PROFILE`, `AZURE_SUBSCRIPTION_ID` and `GH_TOKEN` unset for the suite.

- `cd service && uv run pytest -q` on its own Postgres (port 55489): **2195 passed, 50 skipped**. Provider guard: 6 of 6 known sites hit, all `notify/delivery.py`, none unlisted.
- `uv run ruff check src tests`: all checks passed. `ruff format --check src tests`: all formatted.
- `cd portal && npm test`: **313 passed in 39 files**, at 241b7a6, and no portal file changed after it. `npm run typecheck`: clean. `npm run check:client`: up to date.
- `tests/e2e/m5_exit_test.sh` on the omnifile-edge VM at 241b7a6: **139 passed, 0 failed**. Rerun because the sweep, a migration and the worker's jobs all reach it. Only a test changed after it. The clock was +0 before and after, and I stopped the VM.
- `deploy/azure/check.sh`: not rerun, since no metric or alert changed in this round.

**The clock.** `m5_exit_test.sh`'s exit trap now puts the clock back to +0 and restarts the sim, enrol and api, on every exit, including `E2E_KEEP=1`, and `HUP`, `INT` and `TERM` now go through it. Steps still use the clock as before, since this only runs at the end.

- Full runs at 42fdb14 and 241b7a6: +0 after each, where each earlier run left +363600.
- A run interrupted with `TERM` at 300 s, when the clock read +172800 at about 200 s: +0 after it, exit status 124 from `timeout`.

`wp27.md`'s "about 7 hours" now says about 6, and notes that this file's hold has since removed that cost.

**Not run:** M1 to M4, the chaos soak, `tests/real` and the edge lanes (no edge code changed).

No cloud calls. No new dependencies.
