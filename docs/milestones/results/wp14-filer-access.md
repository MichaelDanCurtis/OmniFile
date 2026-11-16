# Decision 65: filer setup asks whether a share or export limits access

Branch `claude/annex-filer-access`, stacked on decision 63's `claude/annex-read-set-ancestors`, because it adds one condition to 63's single read-set predicate. The migration is `0053_filer_access.sql` for now; it takes the next free number at merge.

## What was built

- **The question** (migration 0053):
  - `catalog.filer.access_limited` is NULL until answered. True means the share or export lets in only `access_groups`; false means it limits nothing.
  - It is kept with `access_confirmed_by` and `access_confirmed_at`.
  - A check keeps the shape: groups if and only if limited, and who and when if and only if answered.
- **In the read set:**
  - `catalog.filer_admits` is one more condition on decision 63's per-folder gates (`catalog.passable_gates`), so it costs nothing per row, and it narrows the annex's catalog folders too.
  - For an annexed file, the filer must have been answered. When access is limited, the user or one of its groups must be in `access_groups`.
  - Unanswered keeps everyone out (fail closed, as 63's unread folders do). The migration raises a `filer_access_unanswered` problem on each annex that was already there.
  - `catalog/read_set.py`'s `Folders` carries the answer (`FilerAccess`), so placements and share copies refuse a reader the share doesn't let in.
- **The route:**
  - `PATCH /v1/filers/{id}` takes `{access: {limited, groups}}`, with or without `placement_sid`.
  - An ArchiveAdmin answers, freshly signed in, with If-Match.
  - Limited needs at least one AD group and never Everyone. Not limited names none.
  - Each answer is kept with who gave it and when, and audited as `filer.access` with the old answer.
  - `Filer.access` shows it, and `read_set.filer_answered` is on the annex's problems.
- **The portal:**
  - Beside the annex's filer: the answer, who confirmed it and when, and "Answer" or "Confirm again". The form offers two choices, "No, the files' own permissions decide" or "Yes, only these AD groups", with the AD group picker.
  - A "Hidden" row on the Problems card while the filer is unanswered.
- **Tests that predate it:** the test databases' stand-in trigger (`tests/conftest.py`) answers "not limited" for a filer that has no answer, as setup would. Decision 63's opted-out annexes answer it themselves. The exit test's filers are made answered, as setup does.

## Tests

Lanes at 3613d81a:

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | (below, after the review fixes) |
| Service | 2677 passed, 50 skipped, 0 failed, own Postgres 17, `PYTHONHASHSEED=6502` |
| Ruff | check and format clean |
| Portal | `npm test` 332 passed in 39 files; typecheck, lint and `check:client` clean |
| Edge | unchanged by this PR; the edge lanes are decision 63's |

**New tests:**
- **Property tests** (decision 63's, now with a random filer answer: unanswered, not limited, or some groups):
  - the effective set is never wider than the captured one, and equals the model;
  - the set check never lets through a SID a folder or the share keeps out.
- **Behaviour:**
  - a share that lets in only some groups keeps everyone else out, and unanswered keeps everyone out;
  - a staged or copied reader must be one the share lets in;
  - the model's unanswered filer admits nobody;
  - a placement's source carries the filer's answer.
- **Web:** an ArchiveAdmin answers.
  - The owner gets 403.
  - These are refused: limited without groups, Everyone, groups when not limited, a bad SID.
  - Each answer changes who sees the file.
  - Who answered and when are kept, as the audit names them, and audited with the old answer.
  - An unanswered filer hides the file and shows on the problems.
- **Portal:** the answer and "Confirm again" beside the filer, which sends the PATCH; the "Hidden" row while unanswered.
- **Exit test**, in each annex's suite:
  - through the real route, an ArchiveAdmin says the share lets in only one AD group, so a reader outside it can't read what the files let anyone read;
  - answered again "not limited", the files are theirs again.

**Fixes undone in turn: 12, each caught:**
- the filer in the predicate;
- unanswered keeps everyone out;
- only the named groups;
- the model's unanswered filer;
- the model is closed when unanswered;
- the set check's groups;
- `folders_of` loads the filer;
- limited needs groups;
- not Everyone (a wrong answer would have been stored);
- audited;
- who answered;
- the problems' `filer_answered`.

Two were caught only after a test was added: the model's unanswered filer, which the property test missed, and who answered, which the web test only checked was set.

**Not built here:** when the answer was last confirmed shows on the annex page, but nothing asks for it to be confirmed again after a time.

## Review fixes, and decision 63's merged in

| Finding | Commit | Fix |
|---|---|---|
| Codex P2: a `reauth_required` refusal showed only its text, so the confirmation couldn't be made | 39a7ac83 | The form shows `ErrorCallout`, which offers "Sign in again" with reauth. The test fails without it. |
| Decision 63's per-folder gates replaced the function 65's per-row check called | a9a01078 | The filer's answer is `catalog.filer_admits`, one more condition on a gate in `catalog.passable_gates`, since gates are per filer. It costs nothing per row and narrows the annex's catalog folders too. Undoing it is caught by two tests. |

Lanes at 5d8f5f5e (63's re-review fixes merged in):

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | `annex_a1_test.sh` 308 passed, 0 failed, in 9857 s, at #85's head f1b6e50a, which holds this branch |
| Service | 2693 passed, 51 skipped |
| Ruff | check and format clean |
| Portal | 333 passed in 39 files; typecheck, lint and `check:client` clean (at 39a7ac83; no portal change since) |
| Edge | decision 63's lanes |
