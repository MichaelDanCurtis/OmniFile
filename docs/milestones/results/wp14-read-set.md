# Decision 63: an annexed file's read set takes its parent folders into account

Branch `claude/annex-read-set-ancestors`, off main at 7404ea2 (main merged in since). The migration, `0052_annex_read_set.sql`, takes the next free number at merge.

## The owner's answers (2026-11-07)

- **SMB is strict.** Anyone an ancestor folder's DACL denies is removed from everything below it, even though bypass traverse checking would let them open a file by its full path. OmniFile may under-show, never over-show.
- **Backfill fails closed.** Files are hidden until their folders' sets are known, and Problems show the progress. No client has an annex yet.
- **Up to the export's root.** A folder above the annex root that the edge can't read is fail closed too, and it is reported.
- **The captured ACL stays as it is.** The effective set is never wider than the captured one, and that is tested as a property.
- **One helper.** Every reader of `read_sids` or `can_read_sql` moves to it, and a lint fails on any new one.

## What was built

**One predicate** (`catalog/read_set.py`, migration 0052):
- `catalog.can_read_file(read, deny, deny_first, file, user, groups[, gates])` is the captured ACL plus the folders (first as `catalog.annex_folders_admit`, a walk per file; after the review, the per-folder gates below).
- For a file in an annex, every folder from the export's root down to the file's own must have been read, without an error, and must let the user through (`catalog.acl.search_sids`).
- **Search sets:**
  - POSIX: search (x).
  - NT and NFSv4: traverse (`FILE_TRAVERSE`, the same bit as `ACE4_EXECUTE`), `GENERIC_EXECUTE` or `GENERIC_ALL`, deny-first.
  - A deny for a principal the edge couldn't map leaves the set unknown, and nobody passes.
- **The folders are judged from the file's current folder.** As first built they were looked up at query time, a walk of every folder per file. After the review that is kept per NAS folder instead (below, "Review fixes"). A folder's change still applies to every file below it at once.
- **Not the plan as approved.** The plan had a stored effective ACL per file, recomputed by the worker with a backfill. I built the query-time lookup instead and didn't say so; the review found it. Its per-folder gates are what the plan's stored ACL would have bought, without a row per file.
- **Every caller moved:**
  - the portal's browse, rewind, share map, file journey, tiers, admin and live feed;
  - the annex's files, events, removals and return targets;
  - Teams cards (`notify/facts.py`);
  - the placement gates and the share copy's gates.
- **Lint.** `tests/catalog/test_read_set_lint.py` fails on any other `acl_can_read(`, aliased `.read_sids` or `.deny_sids`, `["read_sids"]` read, or `can_read_sql`. It allows only `read_set.py` and decision 39's `catalog/restricted.py`, and its own test shows each pattern catching.
- **Placements and share copies.**
  - Each SID a staged ACL lets read, or gives control, must get through every folder by name.
  - A folder that denies anyone, or lets through only some, refuses a SID it doesn't name.
  - Cover: a folder whose ACL hash one of the target's folders shares is no obstacle. So a file put back where it was goes back, and elsewhere under a narrower folder it is refused (`acl_mismatch`).
  - Missing folders fail closed.
- **Gone folders.** A gone folder keeps its last ACL (`nas_folder_acl.gone_at`), and the return path ignores it.
- **The annex's file view** (`GET .../files`): each file's `folders_narrow` lists the folders above it that don't let everyone through, shown under its path.
- **Problems** (`GET .../problems`):
  - `read_set` gives `{ready, read_at, folders, unreadable}`;
  - the migration raises a `read_set_pending` problem on each annex that was already there;
  - a folder that can't be read raises `folder_unreadable`;
  - the portal shows a "Hidden" row first.

**The edge** (`annex/folders.rs`, `runner.rs`):
- the folder pass also reads the folders above the annex root, `""` first;
- after each quick scan it reads the folders it hasn't sent yet (`pass_new`), so a new folder's files aren't hidden until the next full scan;
- the update sends every folder again once (`EPOCH`);
- `folders_read` says a pass covered them all;
- the service accepts `folder_acl` for a folder above the root (it refused those as `out_of_scope` before).

**Tests that predate this.** Test databases get a trigger (`tests/conftest.py`, `OPEN_FOLDERS`) that records each annexed file's folders as read and open to Everyone. That is what an edge would report for the a+rX trees those tests assume. Decision 63's own tests opt their annex out. A few tests changed on purpose:
- folder ACLs that granted read but not traverse now grant traverse, as Windows' "Read & execute" does;
- a gone folder is kept, not deleted;
- a file below a folder whose ACL can't be read is hidden (404).

## Tests

Lanes at 86728019 (main merged in at 96d5333, with #81):

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | `tests/e2e/annex_a1_test.sh` 298 passed, 0 failed, in 9918 s, at 86728019 |
| Edge, Linux | `cargo test -p omnifile-edge -p omnifile-control` 467 passed in 18 suites; `cargo fmt --check` and clippy clean |
| Service | 2673 passed, 50 skipped, 0 failed, own Postgres 17, `PYTHONHASHSEED=6305` |
| Ruff | check and format clean |
| Portal | `npm test` 330 passed in 39 files; typecheck, lint and `check:client` clean |

Lanes after the review and re-review fixes, at cc0185cf:

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | `annex_a1_test.sh` 308 passed, 0 failed, in 9857 s, at #85's head f1b6e50a, which holds this branch, #84 and #85 |
| Edge, Linux | 470 passed in 18 suites; fmt and clippy clean |
| Service | 2689 passed, 51 skipped (the scale bench is the new skip) |
| Ruff | check and format clean |
| Portal | unchanged since 86728019 |

**An intermittent test** (`tests/policy/test_no_id_across_calls.py::test_a_recall_holds_no_id_across_its_calls[high]`): it failed in one full run at 2f137d20 and in 1 of 5 runs of its file afterwards, both while the VM ran an exit test. Then it passed 20 of 20 runs of the file and 20 of 20 alone. On main at 3a153cc it passed 10 of 10. Decision 63 doesn't touch the recall path, and I didn't find a cause.

**New tests:**
- **Properties** (`tests/api/test_read_set.py`, seeded, the seed in each failure):
  - over 150 random worlds of a captured ACL, four folders (some missing, unreadable or unknown) and six users, `catalog.can_read_file` is never true where the captured ACL alone is false, and it equals the Python model;
  - over 400 random folder chains and SID sets, `Folders.allow` never lets through a SID whose member a folder keeps out;
  - with no folders, the model is the captured ACL.
- **Behaviour:**
  - the 0644 file under a 0700 folder, fail closed before its folders are read, with its owner too;
  - an unreadable folder above the root hides the files and raises a problem;
  - SMB's strict traverse, and a deny on a folder;
  - a gone folder still judges;
  - `folders_read`;
  - Teams' reader check;
  - a staged ACL's readers and controllers, with and without cover;
  - the share copy's check;
  - a placement's source carries the folders;
  - a share copy refused under a narrower folder;
  - the problems' `read_set`;
  - the files list's `folders_narrow`.
- **Lint:** `tests/catalog/test_read_set_lint.py` and its self-test.
- **Edge:**
  - the ancestors;
  - a pass reads them and says `folders_read` once;
  - a quick scan's pass reads only new folders and says nothing gone;
  - the update resends every folder;
  - a quick scan in the runner reads a new folder's ACL.
- **Exit test:**
  - in each annex's suite, the edge has read every folder up to the export's root, and an a+rX file is anyone's;
  - over NFS:
    - a 0700 folder on the server keeps a reader out of its files, though the files say 0644, and only of its own;
    - opened again, the files are theirs again.
  - Not over SMB: there a 0700 folder keeps out the annex's own account too, so its files would look gone. The first run did it over SMB, and the timeline check after it failed for that reason.
  - Over NFS, a full scan is asked for every 10 s until one sees the change. The first run asked once, and that scan read the mount's cached mode.

**Fixes undone in turn: 27, each caught.**
- **Service, 21:**
  - the folders in the predicate;
  - a missing folder keeps everyone out;
  - up to the export's root;
  - SMB traverse, not read;
  - POSIX search, not read;
  - folders above the root accepted;
  - a gone folder kept, and it takes no placement;
  - an unreadable folder said;
  - `folders_read` kept;
  - search sets stored;
  - the helper asks the folders;
  - placements check the folders, for control SIDs too, with cover, with the folders loaded;
  - share copies check the folders, with the folders loaded;
  - the set check's deny;
  - the problems' read set;
  - the file view's narrowing folders.
- **Edge, 6:**
  - the folders above the root;
  - a quick scan reads only new folders;
  - the update sends them all again;
  - `folders_read` only when something changed;
  - a quick scan's pass says nothing gone;
  - the runner runs it after a quick scan.
- **Not counted:**
  - the first form of "a missing folder" broke the migration's SQL (every test errored), so it was redone;
  - one undoing ("a file not yet read is hidden", in `annex_folders_admit`'s `coalesce`) changes nothing, because a missing row already fails, so it was dropped.

**Not built here:** decisions 64 and 65, which come next in their own PRs.

## Review fixes (Codex and the Claude review of #83 at 16ff8ca2)

Each with a test that fails without it; commits on this branch:

| Finding | Commit | Fix |
|---|---|---|
| Codex P1: an unmapped named group granting `r--` under `other` `r-x` gave Everyone search | 528d00f0 | `posix_unmapped_restricts` reads the original entries. Where `other` grants the permission and a group the edge couldn't map, after the mask, doesn't, the set is unknown. The search set is then hidden and reported, and not stored (its hash twin's row would stand in). The write set gets a row of its own, so nobody places there. An NFSv4 deny of any write bit for an unmapped principal counts too. SMB descriptors carry every SID, so nothing is dropped there. |
| Review 1: an owning group with no SID (`0705` folder, `0604` file) | 528d00f0 | The same check for the owning group: the folder's search set, and the file's read set, are unknown, and nobody passes. |
| Review 2: `folder_unreadable` named NAS folders to Owners and DataStewards | ee912ff8 | Any event without a catalog file shows only its kind and reason to those who don't see NAS paths, on the events list and Problems, whatever its kind. |
| Review 3: a chmod between full scans waited up to 24 h | 85d2791b | Each folder's inode and ctime are kept from the scan's stat, and with its ACL when sent. After a quick scan the edge reads again those that differ: a chmod, or a folder deleted and made again. The folders above the root are stat-ed by the pass. A ctime from the last `SETTLE_MS` never matches, since the client's cache may have served it. |
| Review 4: about 19 us a row | b908a596 | Kept per NAS folder (`catalog.annex_dir_gate`, `catalog.read_gate`) by triggers, with the gates a caller passes worked out once per query. At 10^6 annexed files in 100 folders: 19.5 s before, 2.0 to 2.5 s after, about 40 ms for the captured ACL alone (`tests/api/test_read_set_scale.py`). A folder's change costs one statement over the folders below it, not the files. |
| Review 5: the lint could be dodged | 7b16995d | It also checks migrations after 0052 (outside the predicate's own functions), bare `read_sids`/`deny_sids` in SQL strings outside INSERT column lists, and `api.acls.can_read` and `deny_first_sets` outside their few callers. |
| Review 6: catalog folders' names weren't narrowed | b908a596 | `catalog.annex_folder` records the annex's catalog folders, and each is narrowed by its parent NAS folder's gate. Folders made before this are narrowed when the annex next files something there. No client has an annex yet. |

**Undoings: 22, each caught** (service 16, edge 6):
- unmapped principals, 9: the folder sets read the original entries; an unknown search set isn't stored; an unknown write set gets its own row; any unmapped write deny; a file's unknown read set; only where `other` lets them in; the mask; the mode alone; the owning group;
- event paths, 1;
- per-folder gates, 6: a folder's change reaches the folders below it; a deleted folder row closes; a moved file takes its new folder; an annexed file with no gate is nobody's; catalog folders narrowed; the annex records its folders;
- edge, 6: the pass compares stamps; a quick scan stamps an unchanged folder; reading a folder stamps it (caught once a test chmods and adds a file in one folder); an unsettled ctime never matches; the folders above the root are stat-ed; an unchanged ACL keeps its new stamp.
- **The lint** isn't undone: its self-test shows each new pattern catching (migrations, bare SQL, the Python checks), and the real tree passes.
- **Not counted, equivalent:** a wider upper bound on the folders below (it recomputes a sibling, rightly); and an ACL trigger for a search set coming to be known, with its closed-on-NULL condition. `passable_gates` already refuses a NULL search set at query time, so I removed both.

## The re-review of #83 (at ee5ce690)

| Finding | Commit | Fix |
|---|---|---|
| Blocking: a gate worked out from a folder change not yet committed | b6d2e5d1 | Every annex on a filer shares the export's root. `annex_dir_gates_recompute` takes `catalog.read_gate_lock(filer)`, a transaction advisory lock, then works the gates out in a new statement, which sees what the last holder committed. The edge's annex batches take it first, right after the annex row, so two batches on one filer can't deadlock over it and another row (the filer's, at a mount report). Nothing else writes the rows the gates follow: placements and web routes don't move annex files or write folder rows. |
| Minor: a file's unknown read set took its twin's row | b6d2e5d1 | Its hash takes a marker, as an unknown write set's does. The other hashes after filtering were checked: an NFSv4 ACL's dropped unmapped allow only adds, so its sets equal its twin's; a dropped unmapped deny makes a folder's set unknown, which isn't stored or gets its own row; SMB descriptors drop nothing. |
| Scale, if straightforward: once per catalog folder | open | Not done: the share map's `readable_dirs` and `_readable_files`, browse and the annex Files tab each put the predicate on a row, and checking a folder's gate once would mean rewriting each as a join on the parent's NAS folder, or keeping the gate on `catalog.file` (moves and folder changes to maintain). As it stands an annexed row costs 2.0 to 2.5 us more than the captured ACL alone (10^6 rows: 2.0 to 2.5 s against about 40 ms), about 4 CPU-minutes per 10^8 rows; the share map stops at its 3 s limit and degrades. A file in no annex pays two index misses. |

Tests that fail without each fix: two connections, the root closed and not yet committed while a file goes into a new folder (`test_a_gate_is_never_worked_out_from_a_folder_change_not_yet_committed`); a batch with no folder in it waits for the lock (`..._takes_its_filers_gate_lock_before_anything_else`); a twin filed first (`test_a_file_whose_read_set_is_unknown_never_takes_a_twins_row`). **Undoings: 3, each caught.**

