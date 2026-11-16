# WP27c: stuck copies are looked for hourly, and listed

Two follow-ups from the re-check of #78. Branch `claude/stuck-copies-list`, off main at 7b348cb.

## 1. A source that keeps making progress can't hide a stuck copy

**The bug.** `STUCK` ran only for a prefix that verified nothing since its last count. In the reviewer's run, six sweeps with progress stayed at `(0,0,0)`, and a source that keeps churning `files_verified` would hide a stuck copy for as long as that lasted. For the same reason `stranded` and `no_checksum` stayed high after an Exclude.

**Now** `STUCK` runs when any of these holds (`_time_to_look` in `migrate/progress.py`):

| Reason | So |
|---|---|
| The prefix verified nothing since its last count | As before: within a sweep or two of a stall |
| 55 minutes since it last looked (`STUCK_EVERY`) | At least hourly, with a sweep every 5 minutes |
| An object was excluded since it last looked | The number falls by the next sweep |
| It has never looked, or the last look is dated ahead of the clock | The dev VMs move their clock, and a look "in the future" mustn't read as a recent one |

The state gate and `PER_SWEEP` are unchanged. The time is `migration_count.stuck_counted_at`, per prefix, so prefixes keep their own phase instead of all reading the queues in the same sweep of the hour.

**Cost.** At 10^6 pending copies `STUCK` took 1.8 s (`wp27b.md`), so a progressing prefix now pays that once an hour, with no transaction id. Not re-measured whole. The keys add 0.43 s when 10^6 copies are all stuck, measured on just that part of the query (0.68 s with the keys, 0.25 s without) on the Postgres 17 container, and almost nothing when few are.

## 2. Which objects these are

**The problem.** The console's Exclude action for `no_checksum` opens a dialog that needs typed S3 keys, and neither the portal nor the API said which objects were affected.

**Why the worker writes the list.** The answer needs both verify queues, which `web` must never read (a letter holds provider text and object keys, `wp27b.md`), and whether the source runs Batch, which only the worker's binding knows. An owner function would have read the queues whole for each page and still not known about Batch. So `STUCK` returns the keys from the same read as the counts, and they agree.

- **`catalog.migration_stuck_copy`** (migration 0047): `(migration_id, kind, object_key)`. At most `KEEP_KEYS` (10,000) per kind, the first by key.
- **Written only where it changed.** A stalled prefix is looked at every sweep, and rewriting its list would leave dead rows each time. All writes come after the reads, so the id-free scans are unchanged.
- **Cleared** when a prefix stops waiting on a check, on its next count.

**The read.** `GET /v1/migrations/{id}/stuck-copies?kind=stranded|no_checksum` returns `{migration_id, kind, count, counted_at, list_limit, items: [{object_key}], next_cursor}`.

- Signed-in ArchiveAdmin only, through the console's own check. An API client is refused.
- A keyset on the primary key, with no count of the table. `count` is the worker's number. At 20,000 rows the plan has no sort and reads only its page: `test_a_page_of_stuck_objects_reads_only_its_page`.
- An object excluded since the worker last looked is left out at once.
- A copy checked since then can stay listed until the worker next looks, up to an hour for a prefix making progress. That is harmless: Exclude already ignores a key whose copy is verified.
- **Audited.** The console's other reads aren't, but they name no objects. This one names objects an ArchiveAdmin may not be able to read, like the cutover report, so it follows that read: every read is audited (`migration.stuck_copies.read`), and an account with no AD identity gets no rows.

**The dialog** takes from it: a choice of kind with its count, ticks, Choose all shown, and Show more. What is ticked goes into the same box a typed key does, so the 1,000-key limit and the reason are unchanged. It says when the list stops short of the count, and warns that a stranded copy could still be verified.

**Not changed.** One Exclude still takes at most 1,000 keys, so a million SSE-C objects would take a thousand of them. Excluding everything of a kind in one action lowers the bar further, so it is a product decision, and nothing here decides it. `copy_stranded` still offers no fix in the console, since it wasn't asked for: the list is on the migration's page.

**The migration's number is 0047 here, and must change at merge if another PR takes it.** `catalog/migrate.py` refuses gaps, so it has to be the next free number on main at that moment.

## Tests

Each fix has a test that fails without it. The undo pass (a throwaway script) undid 28 things one at a time: 27 were caught, and "no `ORDER BY`" survived, because the index scan hid it. `test_a_page_of_stuck_objects_is_in_key_order_whatever_the_plan` now turns index scans off, and catches it: 28 of 28.

| Undone | Caught by |
|---|---|
| The hourly look; 2 hours instead; every sweep instead | `test_a_source_that_keeps_progressing_doesnt_hide_a_stuck_copy` |
| A clock set back reads as recent | `test_a_clock_set_back_doesnt_stop_it_looking` |
| The look after a stall | `test_a_prefix_that_stalls_is_looked_at_every_sweep` and others |
| The look after an Exclude | `test_an_exclude_lowers_the_number_while_the_prefix_progresses` |
| The state gate | `test_a_state_that_doesnt_wait_on_a_check_isnt_counted` |
| The list kept; its bound; its kind; what stopped being stuck | `test_the_worker_lists_which_objects_they_are` |
| Written only where it changed | `test_a_list_that_hasnt_changed_isnt_written_again` |
| The role; the kind; the keyset; a cursor for one list; the worker's number | `tests/web/test_console.py` |
| Excluded left out; no AD identity; the audit; the limit | `tests/web/test_console.py` |
| The dialog's list; a tick reaching the keys; the next page; the stranded choice; the word on the limit | `portal/src/features/migrations/migration.test.tsx` |

`test_the_queues_are_read_whole_only_once_a_prefix_stalls` encoded the old gate, and the first of the tests above replaces it. Reading the list needs the worker's and `web`'s roles to be right, so `test_only_the_worker_writes_the_list` sweeps as the worker and reads as `web`.

## Lanes

The code is at cde974c. The commits after it change only documents.

| Lane | Command | Result | At |
|---|---|---|---|
| Service, on its own Postgres 17 | `uv run pytest -q --timeout=300` | 2208 passed, 50 skipped | The commit before this file's lanes |
| Ruff | `uv run ruff check src tests`, `ruff format --check` | Clean, 464 files | cde974c |
| Portal | `npm test` | 316 passed in 39 files | cde974c |
| Typecheck | `npm run typecheck` | Clean | cde974c |
| Generated client | `npm run check:client` | Up to date | cde974c |
| Portal lint | `npm run lint` | Clean | cde974c |
| M5, omnifile-edge | `tests/e2e/m5_exit_test.sh` | 139 passed, 0 failed, in 473 s, clock +0 before and after | cde974c |

**M5 was run because the sweep reaches it:** `migration_progress` runs in M5's worker, under its real role, with migration 0047 applied. The test checks neither the counts nor the list, so what the run shows is that the sweep's new writes break nothing around them. The list and the hourly look are proven by the service tests.

The first full service run at cde974c failed one test: the endpoint index in `portal-api.md` didn't list the new route. It does now, and the run above is after that.

**The dev VM's catalog keeps migration 0047.** `install-service.sh` doesn't drop the database, and `catalog/migrate.py` refuses a database with a migration its code lacks. So installing a branch without 0047 on omnifile-edge, after this M5 run, fails until the migration's row, table and column are removed.

## The review of #79

**The bug.** `_keep_keys`'s `INSERT` had no `ON CONFLICT`. Run by the reviewer: with two connections inserting the same keys, the second blocked until the first committed, then raised `UniqueViolation`. That left `progress_sweep`, so the rest of that cycle's prefixes went uncounted, and two replicas with different keys could leave a mixed list until the next look.

**The fix.**
- A transaction-scoped `pg_try_advisory_xact_lock` per prefix around the list's replacement. A replica that doesn't get it leaves the list alone, and the next look puts it right. It needs nothing from the consumer's session-lock handling.
- `ON CONFLICT DO NOTHING` on the `INSERT`, for a row written outside the lock.

**Could one prefix's exception still stop the rest of the sweep? Yes,** for any error at all: a statement timeout, a deadlock, a bad row. It was worse than one cycle, too: the sweep goes oldest count first, and a prefix that fails never gets a newer count, so it would stop the same prefixes behind it every sweep. So each prefix now has a small `try`. A failure is logged with its migration id and traceback, its transaction rolls back, and the next prefix goes on. A connection that died still ends the sweep, since nothing after it can be counted, and the runner logs that once. `progress_sweep` now returns how many it counted, not how many it tried.

**Undone one at a time:**

| Undone | Caught by |
|---|---|
| The try-lock | `test_two_replicas_cant_mix_a_list` |
| `ON CONFLICT` | `test_a_key_being_written_elsewhere_doesnt_fail_the_count` |
| The `try` per prefix; its log line | `test_one_prefix_failing_doesnt_stop_the_others_counts` |
| A dead connection ending the sweep | `test_a_connection_that_died_ends_the_sweep` |

That last one survived at first, because the test checked only what the sweep raised, and the sweep's final `DELETE` raises either way. The test now also checks that no prefix is blamed for it.

The first two tests use two real connections, with the second one's wait on the first seen in `pg_stat_activity`.

**Left alone:** the Exclude question, as asked. The migration stays 0047.

**Lanes at e5263e4:** service `uv run pytest -q --timeout=300`, on its own Postgres 17: 2212 passed, 50 skipped. `ruff check` and `ruff format --check` clean, 464 files. M5 wasn't rerun, as agreed: the last run was at cde974c, before this fix.
