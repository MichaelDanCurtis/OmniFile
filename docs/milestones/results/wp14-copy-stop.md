# A refused copy can be stopped and acknowledged (#81's follow-up)

Branch `claude/annex-refused-stop`, off main at 42b3bfed (#91 merged). Migration `0058_annex_copy_stop.sql`.

## Why

`wp14-readback.md` left two things open:

- An ArchiveAdmin couldn't acknowledge or quiet the alert `annex-copies-refused`.
- Nothing stopped the edge copying an always-refused file again. At the third refusal the service stops asking, but the edge's own reconciliation, every `reconcile_days`, still copied it. With `reconcile_days` at 1, a big file was uploaded again every day until the backend was fixed.

## What was built

Two per-file switches on `catalog.annex_file`, each with who and when. Both are an ArchiveAdmin's only (never an app's), need a sign-in in the last 10 minutes, and are audited.

- **Stop re-copying until I say** (`POST …/files/{id}/copy-stop`, undone by `…/copy-resume`):
  - The edge's records say `copy_stopped`, and `refused` is false while it is. No reconciliation copies that record again: neither one the service asked for, nor the unit's own schedule, nor an operator's.
  - A later refusal of a stopped file asks for no reconciliation, and its problem says copying is stopped.
  - A file that changes on the NAS is still copied. That is a new copy of new bytes, not the refused copy again.
  - Only a file whose copy a read-back refused can be stopped (`409 copy_not_refused`).
- **Acknowledge** (`POST …/files/{id}/copy-refused-ack`): the file leaves the gauge `omnifile.annex.copies_refused`, so the alert quiets for that file only. Other files at the bound on the same annex keep it up.
- **Both clear when a version verifies:** in the trigger `catalog.annex_location_verified`, and in the `copied` event for a copy that is already verified when the edge reports it (a dedupe, which runs no read-back).
- **A stopped file is never forgotten.** `GET …/problems` gains `copies_refused`: every file still on the NAS with a refusal counted or a stop set, stopped ones first, with no 30-day window. A stopped file stays there until a version verifies or an ArchiveAdmin resumes it.
  - ArchiveAdmins and apps see every one, since they act on them. Everyone else sees the files they may read, through the same check as the annex's other lists.
- **Portal:** each refused copy is a row on the annex's Problems, marked "Refused" or "Stopped", saying how often it was refused, who stopped it and who acknowledged the alert. An ArchiveAdmin gets "Stop re-copying" or "Resume re-copying", and "Acknowledge the alert" while the file is at the bound and not yet acknowledged. A stale sign-in shows the usual "Sign in again".
- **`AnnexFile`** carries `copy_refusals`, `copy_refused_at`, `copy_stopped` and `copy_refused_ack`.

Audit actions: `annex.copy.stopped`, `annex.copy.resumed`, `annex.copy.refused_ack`. Each row says whether the file was already in that state, so a repeat call shows as having done nothing.

## Choices worth knowing

- **Stop and acknowledge are separate.** Stopping doesn't quiet the alert, and acknowledging doesn't stop the copies. The portal offers both on the same row.
- **Stop needs a refusal, not the bound.** A file refused once can be stopped, so an admin who already knows the backend is broken doesn't have to wait for three uploads.
- **An acknowledgement lasts until a version verifies, or until a new refusal of a file that isn't stopped** (changed after #93's review; it used to outlive new refusals). The file stays on Problems with its count.
- **Edges older than this change** ignore `copy_stopped`. They still stop on a service-asked reconciliation (the record's `refused` is false), but their own scheduled one copies the file again, as before. No such edge has shipped.

## After #93's review

1. **A file stopped below the bound alerted nobody.** The gauge counted only files at the bound (3 refusals), but a stop is allowed from the first refusal, and a stopped file is never asked for again, so its count never reached 3. Now the gauge counts a file at the bound, or stopped whatever its count, until it is acknowledged (`readback.ALERTING_SQL`). A file with no verified copy is silent only after two audited acts. Each row of `copies_refused` says `alerting`, and the portal offers "Acknowledge the alert" from it.
2. **A stop survived one verify path.** `_returned` (`api/annex_placements.py`, a placement back under the file's own name) reset none of the refusal count, the stop or the acknowledgement. It now clears all three when the placed version is verified. An unverified version leaves them, and the worker's trigger clears them when it is read back.
3. **An acknowledgement outlived new refusals.** A new refusal now ends the acknowledgement of a file that isn't stopped. A stopped file isn't copied again, so its acknowledgement stays.
4. **The `copies_refused` list was capped at 200 with nothing to say so.** `copies_refused_total` is the true count of what the caller may see, and the portal says "Showing 200 of N refused copies".

Tests for each: the gauge with a file stopped at its first refusal, before and after an acknowledgement; a new refusal of an acknowledged file, stopped and not; a verified and an unverified version placed back; a list cut short, with its total, for an ArchiveAdmin and for someone who may read none; the portal's button and note. Undoings 15 to 22 below.

The annex exit test was run again, because `_returned` is on its path (stage 7 places files back under their own names).

## Tests

- **Service, the edge's side** (`tests/api/test_annex.py`):
  - a stopped copy's records say `copy_stopped` and not `refused`, and its next refusal asks for nothing;
  - an acknowledgement takes only that file out of the gauge's count;
  - a verified version ends the stop and the acknowledgement, and not before its read-back;
  - a copy already verified when copied (a dedupe) ends them too.
- **Service, the portal's side** (`tests/web/test_annex_copy_stop.py`):
  - stop, stop again (audited as already stopped), resume; a file never refused is `409`;
  - acknowledge, with its audit row; the file stays on Problems;
  - Owners and DataStewards get `403`; a sign-in 11 minutes old gets `401 reauth_required` and changes nothing;
  - a file refused 40 days ago is still on Problems; an ArchiveAdmin sees every refused copy, and others only those they may read.
- **Edge** (`annex/runner.rs`): a stopped record isn't copied again by the scheduled reconciliation or by one the service asked for, and is copied again once resumed.
- **Portal** (`annex.test.tsx`): stop, acknowledge and resume from Problems; the sign-in prompt; no buttons for a DataSteward.

**Undoings: 22, each caught** (1 to 14 before the review; 15 to 22 its fixes):
1. a stopped copy's records don't say `refused`;
2. records say `copy_stopped`;
3. the edge doesn't copy a stopped record again;
4. a refusal of a stopped copy asks for nothing;
5. an acknowledged file leaves the gauge;
6. a verified version ends the stop and acknowledgement;
7. a copy verified at its event ends them too;
8. Problems keeps refused copies however old;
9. ArchiveAdmin only;
10. a fresh sign-in;
11. a stop is audited;
12. only a refused copy can be stopped;
13. an ArchiveAdmin sees every refused copy;
14. the buttons are an ArchiveAdmin's;
15. a stopped file is in the gauge whatever its count;
16. a row's `alerting` says what the gauge does;
17. a new refusal ends the acknowledgement of a file that isn't stopped;
18. a stopped file's acknowledgement survives a new refusal;
19. a verified version placed back ends the refusals, the stop and the acknowledgement;
20. an unverified version placed back leaves them;
21. a list cut short says its true total;
22. the portal offers the acknowledgement from `alerting`.

## Lanes

After the review's fixes, at ca69416e (2026-11-11):

| Lane | Result |
|---|---|
| Service | 2736 passed, 51 skipped |
| Ruff | check and format clean |
| Portal | 339 passed in 39 files; typecheck, lint and `check:client` clean |
| Bicep (`deploy/azure/check.sh`) | ok (the alert's description changed) |
| Annex exit test on omnifile-acl | `annex_a1_test.sh` 308 passed, 0 failed, in 9970 s |
| Edge | not run again: no edge file changed |

Before the review:


Run from the start on 2026-11-11, at 23ddb976 plus the formatting of two edge files:

| Lane | Result |
|---|---|
| Service | 2730 passed, 51 skipped |
| Ruff | check and format clean |
| Portal | 338 passed in 39 files; typecheck, lint and `check:client` clean |
| Edge on omnifile-acl (`omnifile-edge`, `omnifile-control`) | 473 passed in 18 suites, 0 failed; clippy clean; `cargo fmt --check` clean after formatting two files |
| Annex exit test on omnifile-acl | `annex_a1_test.sh` 308 passed, 0 failed, in 9899 s |
| The portal in a browser (mock) | the Problems row, "Stop re-copying", then "Stopped" with "Resume re-copying" and "Acknowledge the alert" |
| M4 exit test | not run: nothing on its path changed |

Seen in passing, not from this change: with the portal's pane hidden (a zero-height canvas), `theme/dots.ts` `glyphMask` throws `IndexSizeError` from `getImageData`, and the page stays blank until it is reloaded at a real size.
