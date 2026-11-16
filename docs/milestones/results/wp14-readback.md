# WP14 follow-up: a refused read-back, and the re-check's leftovers

Branch `claude/annex-readback-refused`, off main at 7404ea2 (#70's merge). Migration 0051 (0049 until #77 took it at 635e1a4, then 0050 until #80 took it at 3a153cc).

## A refused read-back

`worker/verify.py`'s `_deny` wrote an audit row and a log line, and acked the message. The location stayed unverified, so nothing left the NAS for it (the removal gate answers `copy_not_verified`), but the annex said nothing, and the edge copied the file again only at its next reconciliation, up to `reconcile_days` away.

**The edge's handling, traced.** The edge never reads a copy's verification itself. Its reconciliation reads the records, and a record whose copy isn't `copy_ok` (its location committed and verified) is set back to `ready` and copied again. That upload can't dedupe onto the refused blob, because a dedupe needs a verified location. So a reconciliation is the re-copy, and the fix is to ask for one now.

**Now:**
- **A refusal on an annexed file's copy** (a CRC or size mismatch, or no blob at all) raises a `copy_refused` problem on each annex whose copy it is, and sets `annex.reconcile_asked_at`. The read of a copy before a removal is the existing `copy_mismatch` and unchanged.
- **The edge** runs a reconciliation when `GET annexes` carries a `reconcile_asked_at` it hasn't answered, and records it as answered once one has run. So a restart in between asks again, and no clock is compared.
- **A refusal before the `copied` event** (a small file is read back at once) found no copy to speak of. The event now looks for the refusal and says it.
- **The two can't miss each other:** the refusal takes the location's row before it looks, and the event takes it before it reads, so one waits for the other.
- **A read-back the redrive gives up on** is said the same way.

## The re-check's leftovers

- **`_returned`** now takes the version's location `FOR SHARE` before the annex file, as `copied` does since dcd5ac6, so a verification between the worker's trigger and its commit isn't lost.
- **The mounted check** now also compares the opened root's device with the mount's, from the mount table, since the mount can go between the check and the open. Both passes.
- **`ticket_no_credential`'s ALERT** is logged when the problem is raised, at most hourly per placement, not on every ask.

## Tests

Lanes at 1ff7a66, the code under review (this file is the only change after it):

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | 291 passed, 0 failed, 9928 s |
| Edge, Linux | `cargo test` 438 passed in 18 suites; clippy clean |
| Service | 2383 passed, 50 skipped, 0 failed, own Postgres 17, `PYTHONHASHSEED=6612`, 692 s |
| Ruff | check and format clean |
| Portal | `npm test` 316 passed in 39 files; `npm run typecheck` clean |

An earlier full service run at the same head (`PYTHONHASHSEED=6611`) had 10 failures and 2 errors, all in `catalog/test_migrate.py`, `migrate/test_engine.py` and one placement test: pytest timeouts, a dropped connection and two timing asserts, while the host's load average was over 80. The 78 tests of those files passed alone (65 s), and the full rerun above passed.

**New tests** (each fails without its fix):
- `api/test_annex.py`: a CRC refusal raises `copy_refused` and asks the edge; a copy Azure no longer has; only the annex whose copy it is hears it; a refusal before the `copied` event is said by the event; a refusal racing the event (a worker thread holding the location) isn't lost.
- `policy/test_redrive.py`: a read-back the redrive gives up on tells the annex.
- `api/test_annex_placements.py`: a verification racing a return isn't lost; the `ticket_no_credential` ALERT is logged once per hour per placement (counts 1, 1, 2 across asks and an hour).
- Edge `annex::runner`: an ask runs one reconciliation, the same ask again runs none, and a new one runs another (the answer is kept in `annex.db`, so a restart doesn't change it, though no test restarts the runner); the opened root must be on the mount's device, for placements and for removals; `annex::mount`: the last mount at a path is the one that counts.

**Fixes undone in turn: 18, each caught.**
- Service, 12: the CRC refusal, the missing copy, the give-up, the problem said, the edge asked, only this copy; the refusal's lock, the event naming an earlier refusal, the edge told from the event; the return's lock; the ALERT on every ask; no ALERT at all.
- Edge, 6: an ask runs a reconciliation; an ask is answered once; the opened root on the mount; the last mount there; placements check the opened root; removals check the opened root. The last two weren't caught at first; the `test_mount` seam and two call-site tests were added, and both are caught now.


## Review of #81 at 1ff7a66: two should-fixes and three minor ones

The review found no broken safety rule: the NAS stays read-only, the removal gate holds, no transaction spans a provider call, and the `st_dev` check is race-free.

**1. A file that is always refused was copied again for ever.** Each refusal asked for a reconciliation, and each reconciliation copied the file again. Every round was a new version, a full upload, a full read-back and a walk of the NAS, with no bound and no alert that stayed up. Now:
- **Refusals are counted per file**, one per version (`annex_file.copy_refusals`). A version read back twice counts once. A verified version sets the count back to 0, through the trigger or a `copied` event that finds the copy already verified.
- **A bound with a backoff** (`annex/readback.py`): the first refusal asks at once, and the second asks an hour later (`ASK_BACKOFF`). `GET annexes` shows an ask only once it is due. At the third (`MAX_REFUSALS`) the service stops asking, logs one ALERT, and the gauge `omnifile.annex.copies_refused` keeps the alert `annex-copies-refused` up while the file is there.
- **The edge's own reconciliation**, every `reconcile_days`, still copies the file again, as before.

**2. An asked reconciliation copied again every copy whose read-back was only pending.** `copy_ok` is false while a read-back is queued, so one refusal re-uploaded the whole backlog.
- `GET records` now says `refused` next to `copy_ok`.
- A reconciliation the service asked for copies again only `refused` records.
- One on the schedule, or asked for by an operator with SIGUSR2, still copies what isn't `copy_ok`, as before.
- An asked run doesn't put off the scheduled one: `last_reconcile` moves only for full runs.
- An asked run that fails waits for the next look at the annexes, not the next tick.

**Minor:**
- **Deduped and resolved.** There is one `copy_refused` problem per file and version. The `copy_mismatch` drift is resolved when a version verifies (the trigger), not when it is copied. A problem's resolution is the file's verified version, which the problem list can read.
- **Lock order.** `copy_refused` takes the annex row first (`FOR UPDATE`, as the event batch does), then the location, then the annex file. A test holds the annex and finds the location still free while the refusal waits.
- **Each cause has its own words.** A mismatch says the copy didn't match. A missing blob says Azure doesn't have it. A redrive give-up says the read-back couldn't be done. The audit row and the problem both carry `cause`.

**Migration 0051** (0049 until #77 took that number at 635e1a4, then 0050 until #80 did at 3a153cc; main merged in at 969a020 and 12812a27).

**Lanes for this round:**

| Lane | Where | Result |
|---|---|---|
| Annex exit test, omnifile-acl | cbf8108, this round's edge and service code | 291 passed, 0 failed, 9795 s |
| Edge, Linux | 339a8190 (after merging main at 3a153cc) | `cargo test -p omnifile-edge -p omnifile-control` 462 passed in 18 suites; the whole workspace 955 in 65; fmt and clippy clean |
| Service | 339a8190 | 2655 passed, 50 skipped, 0 failed, own Postgres 17, `PYTHONHASHSEED=8104` |
| Service | e51085c, before the second merge | 2644 passed, 50 skipped (`PYTHONHASHSEED=8103`) |
| Ruff | 339a8190 | clean |
| Portal | e51085c | `npm test` 316 passed in 39 files; typecheck and lint clean |

- **What came after the exit test's head.** cbf8108 has every edge and annex change of this round. What came after:
  - the put-right flag on the problems list (web and portal only);
  - two merges of main;
  - the migration's renumbering.
- **Main's changes don't reach the exit test's paths.** #77 changed tiering, `worm_sync` and the overdue gauge. #80 and #82 changed the share commit path (`commit.rs`, `quiet.rs`, `carry.rs`, `omnifile-detect`), the share settings, and a heartbeat field in `omnifile-control`. The annex code uses none of them, and the exit test runs only the service and the annex units. So it wasn't run again.

**New tests** (each fails without its fix):
- `api/test_annex.py`:
  - refusals are bounded, backed off, ALERTed once, gauged, and end at a verified copy;
  - a copy already verified when copied ends the run;
  - a version refused twice counts once;
  - records say `refused` only for a refused copy;
  - a `copy_mismatch` drift is resolved by a verified copy, not a `copied` event;
  - a refusal takes the annex before the location;
  - a missing copy's cause and words.
- `policy/test_redrive.py`: a give-up's cause and words.
- `worker/test_metrics.py`: the gauge.
- `web/test_annex_returns.py`: a `copy_refused` problem is put right once a later version verifies.
- Edge `annex::runner`:
  - an asked reconciliation copies again only what was refused, and the schedule's still copies what isn't `copy_ok`;
  - an ask doesn't put off the scheduled run;
  - a failed ask waits for the next look at the annexes.

**Fixes undone in turn: 22, each caught.**
- **Service, 17:**
  - the bound;
  - the backoff;
  - an ask shown only once it is due;
  - one ALERT;
  - a verified version resets the count;
  - a copy verified when copied resets it;
  - one count per version;
  - records say `refused`;
  - not resolved by `copied`;
  - resolved by a verified version;
  - the annex first;
  - a give-up's cause, its audit cause and its words;
  - a missing copy's cause;
  - the gauge;
  - the put-right flag.
- **Edge, 5:**
  - an asked run copies only the refused;
  - an asked run keeps the schedule;
  - an ask alone runs one;
  - an operator's run copies all;
  - a failed ask waits.

**Open (from the re-check, to route later):**
- An admin can't acknowledge or clear `annex-copies-refused`. (Done: `wp14-copy-stop.md`.)
- There's no "stop copying again" switch for a file whose copy is always refused. With `reconcile_days` at 1, a big file is uploaded again every day until the backend is fixed. (Done: `wp14-copy-stop.md`.)
