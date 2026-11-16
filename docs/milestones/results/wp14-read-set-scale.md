# Decision 63 at scale: the folders walked per query

Branch `claude/read-set-gates-scale`, off main at c2bf3690 (decisions 63 to 65 merged). Migration `0055_read_set_walk.sql`.

## Why

The re-review of #83 pointed out that its bench had only 100 folders, so few gates. 0052 kept a gate per distinct chain of folder ACLs. Every query listed the gates the caller passes, which meant a scan of all of them. Each row was then tested with `gate_id = ANY(array)`, which is linear in that list. Per-user home folders mean one gate each.

## The bench

`service/tests/api/test_read_set_scale.py` is off by default. It takes the files, the folders, and the depth:

- `OMNIFILE_READ_SET_BENCH=<files>`
- `OMNIFILE_READ_SET_BENCH_DIRS=<folders>`
- `OMNIFILE_READ_SET_BENCH_DEPTH=<depth>`

The setup:

- **Folder ACLs:** each NAS folder has an ACL of its own, as home folders have. The export's root and a shared chain let Everyone through, and each home folder lets through its owner and GEO.
- **Catalog:** it mirrors the NAS, with a catalog folder per home folder. It runs on my own Postgres 17 in Docker on the Mac, with `shared_buffers` 1 GB.
- **Callers:** "passes all" is in GEO and passes every folder, the worst case for the walk. "Passes few" owns one home folder.
- **Measures, best of three:**
  - a count of every file through `readable_sql`, against the captured ACL alone;
  - the share map's count (`readable_dirs` and `_readable_files`), and the same query with the captured ACL alone;
  - the walk alone (`catalog.passable_dirs`).
- **The per-row test** is the count less the captured ACL and the walk, per file.

### Before (0052's gates, at c2bf3690)

| Files | Folders (gates) | ACL alone | Through the folders | Share map | `passable_gates` | Filing them |
|---|---|---|---|---|---|---|
| 10^6 | 10^4 | 40 ms | 5.2 s | 18.8 s | 53 ms | |
| 10^6 | 10^5 | 54 ms | 29.5 s (29 us a row) | 94 s | 398 ms | 722 s, recomputes for each new folder |
| 10^7 | 10^4 | 512 ms | 52.8 s | 435 s | 43 ms | |

### After (the walk, at e9ebc5d3)

Each row of the table gives the caller who passes all, then the caller who passes few. The walk column gives the time and the folders reached.

| Files | Folders, depth | Through the folders | Share map (captured ACL alone) | The walk | Per-row test | Filing the files and their new folders |
|---|---|---|---|---|---|---|
| 10^6 | 10^4, 1 | 982 ms / 762 ms | 4.0 s (1.3 s) / 3.4 s (1.3 s) | 19 ms, 10^4 / 1 ms, 2 | 0.92 / 0.72 us | 16.0 s |
| 10^6 | 10^5, 1 | 1.25 s / 0.80 s | 4.5 s (1.4 s) / 3.6 s (1.4 s) | 228 ms, 10^5 / 5 ms, 2 | 0.96 / 0.74 us | 17.6 s |
| 10^6 | 10^5, 30 deep | 1.9 s / 0.88 s | 5.1 s (1.5 s) / 3.8 s (1.5 s) | 930 ms, 10^5 / 1 ms, 31 | 0.92 / 0.82 us | 21.6 s |
| 10^6 | 10^6, 1 | 5.0 s / 0.88 s | 12.7 s (3.3 s) / 6.9 s (3.3 s) | 4.0 s, 10^6 / 45 ms, 2 | 0.95 / 0.76 us | 31.6 s |
| 10^7 | 10^4, 1 | 10.5 s / 8.1 s | 211 s (72 s) / 217 s (45 s) | 20 ms, 10^4 / 2 ms, 2 | 0.99 / 0.74 us | 168 s |
| 10^7 | 10^5, 1 | 11.4 s / 9.9 s | 342 s (122 s) / 338 s (83 s) | 245 ms, 10^5 / 6 ms, 2 | 1.05 / 0.90 us | 167 s |

- **The per-row test** is 0.7 to 1.05 us, flat from 10^4 to 10^6 folders and from 10^6 to 10^7 files. The jsonb key lookup doesn't grow with the set.
- **The walk** grows with the folders the caller passes: about 4 us a folder, so 4 s for a caller who passes all of 10^6. One who passes few costs milliseconds at any size.
- **Filing:** 10^5 new folders and 10^6 files took 17.6 s, against 722 s before. Of the 10^7-file runs' 168 s, most is the files.
- **The share map** walks the catalog by folder, and probes `annex_file` for each file in that order, where the flat count reads it in file order. That probe costs about 2 to 3 us a file at 10^6 and more at 10^7, where the index no longer fits in 1 GB of buffers. Without the NAS's folders the same query takes 1.3 to 1.5 s at 10^6 and 45 to 122 s at 10^7, so at 10^7 it is past its 3 s live limit either way.

### Tried and dropped

- **A function call a row:** `catalog.can_read_file` with the walk passed in cost 4.8 us a row, and the same test inline cost 1.5 us. So `readable_sql` writes the test inline.
- **A join form for the share map:** `annex_file` and `annex_folder` left-joined, so they could be hashed. It changed nothing: the planner estimates the recursive folder set at 41 rows and keeps probing a row at a time, 4.48 s against 4.49 s. I reverted it.
- **A column on `catalog.file`** (its folder): it would raise `entity_version` on every annexed file at each move and folder change (`file_bump`). The feed doesn't watch the column, but the version is the file's ETag. Not done.

## What changed

- **Nothing is derived from folder ACLs any more.** 0055 drops `catalog.read_gate`, the recompute, its trigger on `nas_folder_acl`, and the per-filer lock. `annex_dir_gate` is now only each annex folder's place in the export, made when a file first lands there.
- **`catalog.passable_dirs(user, groups)`** works out, once per query:
  - the folder ACLs the caller passes: those whose search set names the caller, a group of theirs or Everyone (a GIN index on `search_sids`), and of those the ones `acl_can_read` lets through;
  - the filers whose answer admits the caller (decision 65), once a filer;
  - the walk from each filer's export root, down through those folders only;
  - the annex folders reached, as a jsonb object keyed by `dir_id`.
- **The walk is bounded.** A folder's parent is its path less a name (a generated column, `parent_path`), so the walk can't loop or follow a corrupt link, and its depth is capped at 2048 anyway.
- **`readable_sql`** writes the test inline: the captured ACL, then the row's annex folder (an annexed file's, or a catalog folder's parent's) looked up in the walk. The walk is a sub-select that doesn't refer to the row, so it is evaluated once per query, and it appears once in the fragment. A query that asks more than once, as the share map does three times, walks once with `PASS_CTE`.
- **`catalog.can_read_file`, for one file,** checks only that file's chain of folders, which is O(depth) with no walk. `readable_sql_pos` and `readable_by_sql` use it: a placement's or share copy's file, and Teams' recipients.
- **The race of the re-review of #83 goes away:** with nothing derived, a folder change not yet committed can't leave a wide answer behind.

## The guarantees, and their tests

Every guarantee #83 to #85 had still has its test. All of them pass on 0055:

- **Fail closed:**
  - a folder not read, read with an error, or with an unknown search set keeps everyone out: `test_an_unreadable_folder_above_the_annex_hides_its_files_and_says_so` and the unmapped-group tests;
  - folders above the annex root count: the same test, and the property test's export root;
  - an annexed file with no folder recorded is nobody's: `test_a_file_moved_takes_its_new_folders_gate`, which now checks the one-file function too.
- **The filer's answer (65):** `test_a_share_that_lets_in_only_some_groups_keeps_everyone_else_out`, and the property test's random answers.
- **The flag (64):** unchanged, `test_annex_placements.py`.
- **Never wider than the captured ACL, and equal to the Python model:** the property test, 150 random worlds. It now also checks that the listing's inline form (the walk) and the one-file function (the chain) agree.
- **The lint:** `test_read_set_lint.py`, with `passable_dirs` added to the predicate's own functions.
- **One walk per query** (new): `test_a_listing_walks_the_folders_once_however_many_rows_and_times_it_asks` reads the plan. It requires one `passable_dirs` evaluated once, for a flat count and for the share map.

**Deleted:** `test_an_annex_batch_takes_its_filers_gate_lock_before_anything_else`. Its promise was that annex batches can't deadlock over the gates' lock, and the lock is gone. `test_a_gate_is_never_worked_out_from_a_folder_change_not_yet_committed` keeps its promise: after a root closed in an uncommitted transaction commits, the file in a new folder is hidden. It no longer asserts that the second transaction waited, since nothing makes it wait now.

## Undoings: 8, each caught

1. the walk starts at the export's root (3 tests);
2. the walk keeps the filer's answer;
3. the walk asks `acl_can_read`, not only the index;
4. a listing: an annexed file with no folder is nobody's;
5. a listing narrows catalog folders;
6. one file: the filer's answer;
7. one file: each folder must let them through;
8. the share map walks once.

**Not counted, equivalent:** "one file: an annexed file with no folder is nobody's" (the outer default). With no folder rows, `filer_admits` of a NULL filer is already false.

## The review of #88: one item and a page don't walk

The review found that a route loading one annexed file paid for the whole walk, about 4 s at 10^6 folders on a NAS Everyone may traverse. That covered `load_scope` (the file page, a recall, rewind and tiers) and the share's root. Browse paid twice.

**What a query takes now** (`catalog/read_set.py`):

| Form | For | Cost |
|---|---|---|
| `readable_row_sql` | One item: `load_scope`'s chain of ancestors, the share's root, a file's versions, a returned file, placements, Teams' recipients | Each row's own chain (`catalog.dir_passes`), 140 to 210 us, and no walk |
| `readable_in_folder_sql` | One folder's children: the browse listing and its as-of form | The folder's NAS folder, found among its first 64 children, and its chain checked once; each row compares its own folder with it, and one that differs checks its own |
| `readable_page_sql` | Pages from many folders: the annex's files and events, removals, the audit log, the live feed's names | The walk, capped at `PAGE_WALK_MAX`; past the cap, each row's own chain |
| `readable_sql` | Whole-share counts: the share map, a node's files, tiers' descendants | The walk, whatever it takes |

**The cut-off,** `PAGE_WALK_MAX` = 10,000 folder ACLs or folders. A walk costs about 4 us a folder, so 19 to 20 ms at 10^4. A row's chain costs 140 to 210 us, so a page of about 100 rows checked on its own costs about the same. Past the cap a page checks its rows; under it, the walk.

**Measured at 10^6 passable folders** (10^6 files, a folder each; best of three). The walk column is the form #88 used; the now column is the form above.

| Request | Passes all: walk / now | Passes few: walk / now |
|---|---|---|
| A file's page (`load_scope`) | 6.5 s / 1 ms | 1 ms / 1 ms |
| A browse page of 101 (the home folders) | 6.2 s / 2 ms | 1 ms / 1 ms |
| A browse page of a folder hidden from the caller | 6.2 s / 20 ms | 1 ms / 19 ms |
| A page of 101 of the annex's files | 6.2 s / 98 ms | 1.38 s / 1.35 s |

At 10^6 files in 10 folders (10^5 a folder), every form takes 1 ms, except the annex's files at 80 to 87 ms and a hidden folder for the caller who passes few at 111 to 116 ms, either way. When 1 file in 10^6 is visible, the annex's Files tab costs 1.35 s either way, because filling a page of them means reading them all.

**Tried first:**
- Each row's own chain on a page: about 140 us a row, and a page of rows hidden from the caller reads them all, 2.1 s for 10^4.
- A lateral join, which the planner didn't memoize: 508 ms for the same.
- The first folder lookup scanned every child: 0.2 to 0.9 s in a folder of 10^6. It is now bounded to 64.

**Tests:**
- `test_one_item_and_a_folders_page_never_walk`: `load_scope`'s and a browse page's plans have no `passable_dirs`, and the item routes (`access`, `routes_browse`, `routes_shares`, `routes_annex_returns`) don't use the walk form.
- `test_a_child_in_another_folder_than_its_siblings_checks_its_own`.
- The property test now checks all five forms on every world: the walk, the page under and past its cap, a folder's children, and one item.
- The lint allows `dir_passes` as one of the predicate's own functions.

**Undoings: 7, each caught:**
1. `load_scope` checks one item's chain;
2. the share's root checks its chain;
3. browse checks its folder's chain;
4. a child in another folder checks its own;
5. a page past the cap checks each row;
6. a walk past the cap gives up;
7. one folder's chain keeps the filer's answer.

The first form of 6 broke the migration's SQL, so it was redone as `WHEN false`.

## Lanes

| Lane | Result |
|---|---|
| Service | 2706 passed, 51 skipped (the scale bench skips), at fc3107af; 2704 at b8098d6e |
| Ruff | check and format clean |
| Annex exit test on omnifile-acl | `annex_a1_test.sh` 308 passed, 0 failed, in 9928 s, at fc3107af (and 9931 s at b8098d6e); run again because the placement and share-copy gates go through `catalog.can_read_file`, whose body changed |
| Edge | unchanged |
| Portal | unchanged |

**Open:** the share map at 10^7 files is past its 3 s limit with the captured ACL alone, so it needs its own work, such as counts kept per folder.
