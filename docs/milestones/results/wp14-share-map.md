# The share map from a roll-up kept by the worker

Branch `claude/share-map-rollup`, off main at 9a04d39c (#90 merged). Migrations `0056_share_map_rollup.sql` and `0057_file_subfolders.sql`.

## Why

At 10^7 files the share map's live count took 211 to 342 s (`wp14-read-set-scale.md`), and 45 to 122 s with the captured ACL alone. That is past its 3 s limit. Its walk of a share's folders read every file in each folder to find the subfolders, and then it counted every file. The owner chose a roll-up refreshed by the worker, with the read check exact at query time, and minutes-old counts said plainly ("as of").

## What was built

- **The roll-up** (`catalog.share_map_group`) has one row per group of a catalog folder's live files. A group shares:
  - the captured ACL;
  - whether the files are annexed, and their annex folder;
  - the state;
  - the primary copy's backend, tier and class.

  Each row holds the files, their bytes and the bytes on the edge. Nothing about who may read is stored.
- **The map sums it** (`web/routes_visual.py`):
  - ArchiveAdmins and DataStewards get every folder under the share's root (decision 56, counts only).
  - Everyone else gets only the folders they can read, as before, with each group put through `readable_group_sql`, the same predicate its files would go through (`catalog/read_set.py`).
  - For everyone else, a folder with a change waiting is not summed from the roll-up: its files are counted live through the same read predicate (the review's should-fix, and Codex's P1). So any change to who may read a file shows at once, whether or not the worker is running: the file's own ACL, a move between folders, or a change to an annexed file's annex folder or annex row. Past `QUEUED_LIVE_FILES` (20,000) files in such folders under the share, at their last count, the map says `counting` rather than show a stale total; a folder that grew since its count is still bounded by the 3 s limit, past which the map has no totals (`basis: none`). That makes the map fail closed.
  - Recalls in flight stay live.
- **What marks a folder for recount** (triggers in 0056). Each appends to `catalog.share_map_dirty`, so an edge's commit only inserts and never waits on the worker:
  - a file inserted or deleted, or changed in anything the map counts (folder, kind, state, size, bytes on the edge, ACL, primary copy, deletion): its folder, before and after a move;
  - a folder moved, renamed or deleted: itself;
  - a location's state, backend, tier or class (`catalog.location`, not `catalog.file`): the folder of the file it is the primary copy of;
  - an annexed file's annex folder, or its annex row going: its catalog folder;
  - a folder made: queued for its first count.
- **The worker** (`usage/share_map.py`, the `share_map` sweep each minute):
  - It recounts the queued folders, oldest first, at most 500 a transaction and 40 transactions or 20 s a run.
  - Each batch reads at most 2,000 of the oldest queue entries, by the queue's primary key (`seq`), so a big backlog doesn't make each batch scan the whole queue. A batch whose recount meets a concurrent change is skipped for the rest of the run, and the run goes on to other folders.
  - Each batch runs at REPEATABLE READ: its groups are deleted and counted again, its folders stamped, and only the queue entries its snapshot saw are taken off. A change committed meanwhile stays queued.
  - The queue is the cursor. The folders made before 0056 are queued 20,000 a run through a cursor in `catalog.share_map_state`, so the first count resumes too.
  - No transaction spans batches, and none holds a lock an edge waits on.
- **"As of"** (`computed_at`):
  - a folder with nothing queued is true as of now;
  - a folder with a change queued, as of its count;
  - a folder made since the roll-up began and not counted yet, as of when it was made (it was empty then, and its files are queued).

  The map is true as of the oldest of these. A worker that falls behind makes the time older, and never makes a wrong total look current. A reader's map counts its folders with a change waiting live, so its time is now.

  Left as is (review item 3): a change to a file the caller can't read, in a folder they can, makes a whole-share map's "as of" go back to that folder's count, although none of the caller's counts move. For a reader's map that folder is now counted live, so this only affects ArchiveAdmins and DataStewards, and the effect is only on the time shown, never on a count.
- **"Counting":** a share with a folder from before the roll-up that hasn't been counted, or with no folder counted at all, shows `basis: counting`: no totals, never a total of zero. The portal says "Counting this share's files. The totals show once the service has counted them.", and otherwise "As of <date and time>".
- **Age:** the gauge `omnifile.share_map.age` is the oldest queued change's age, or how long the first count has been running. The alert `share-map-stale` fires past two hours.
- **The subfolder index** (`file_subfolders`, 0057): `catalog.file (parent_id, name) WHERE kind = 'dir' AND deleted_at IS NULL`, built CONCURRENTLY. The walk of a share's folders no longer reads their files.

**Decision 56:** ArchiveAdmins and DataStewards see whole-share totals, counts only, and file names stay limited to what the caller can read (`/map/files` is unchanged). **Decision 62:** the map shows no price, so nothing changes there.

## The hourly count, and its readers

`catalog.share_map_count` and `share_map_run` (0036) are dropped in 0056. Their readers:

| Reader | What happened |
|---|---|
| `web/routes_visual.py` (the map, `scope: all`) | moved to the roll-up |
| `usage/share_map.py` (`snapshot`, `count_share`) | replaced by the roll-up's refresh |
| `worker/usage_jobs.py` (the hourly `share_map` sweep) | now the roll-up's sweep, each minute |
| `tests/web/test_visual.py` | moved to the roll-up; the per-share transaction test went with the hourly count |
| The console, alerts, the partner API, metrics | none read it |

## Numbers

Best of three; `service/tests/api/test_read_set_scale.py` with `OMNIFILE_READ_SET_BENCH_ONLY=rollup`. Each of 10^5 NAS folders has an ACL of its own, which makes 10^5 groups.

| Files | Caller | The map | The live count it replaced |
|---|---|---|---|
| 10^6 | passes all | 967 ms | 5.1 s |
| 10^6 | passes few | 440 ms | 4.0 s |
| 10^6 | whole share (ArchiveAdmin, DataSteward) | 316 ms | (the hourly count) |
| 10^7 | passes all | 998 ms | 211 to 342 s |
| 10^7 | passes few | 494 ms | 211 to 342 s |
| 10^7 | whole share | 360 ms | (the hourly count) |

| Roll-up upkeep | 10^6 files | 10^7 files |
|---|---|---|
| First count (100,003 folders, 10^5 groups) | 9.6 s | 30.6 s, in batches of 500 folders |
| One folder recounted | 10 ms | 16 ms |

At 10^6 the map's totals equal the live count for both callers, checked by the bench.

A reader's map with folders waiting, counted live (after the review; the cap lifted to time the live part; best of three):

| Files | Folders waiting (their files) | Passes all | Passes few |
|---|---|---|---|
| 10^6 | none | 951 ms | 452 ms |
| 10^6 | 2,000 (20,000) | 1,024 ms | 501 ms |
| 10^7 | none | 1,052 ms | 505 ms |
| 10^7 | 100 (10,000) | 1,049 ms | 535 ms |
| 10^7 | 200 (20,000) | 1,123 ms | 568 ms |
| 10^7 | 2,000 (200,000) | 9,094 ms | 7,026 ms |

A cap counted in folders does not bound the cost: at 10^7, 2,000 folders hold 200,000 files, which is past the 3 s limit. So the cap is counted in the files those folders held at their last count: `QUEUED_LIVE_FILES` = 20,000.

## Tests

- **Property:** `test_the_maps_sums_equal_a_live_count_on_random_worlds`, 25 random worlds, each checked twice:
  - the worlds have folders with open, owner-only and deny ACLs, files of random state, tier and size, and recalls;
  - each world is checked after its first count, and again after random moves, state changes, tier changes on the location, deletes, ACL changes and folder renames, followed by a recount;
  - after each round of changes, and before the recount, the three callers' maps are checked again (no recount: a reader's map counts the changed folders live);
  - every check compares the map's sums with the live count it replaced (kept in the test), for the whole share, a reader, an owner and someone in no group.
- **Marking:** each change marks the folders it touches:
  - a bare insert;
  - a move (both folders);
  - a tier change on the location;
  - a folder renamed, moved or deleted;
  - and what the map doesn't count marks nothing.
- **Folder changes show at once:** a folder renamed or deleted is reflected immediately, before any recount.
- **"Counting":** a new share says counting until its first count, and a folder from before the roll-up says counting until the backfill counts it.
- **"As of":** a folder with nothing waiting is true now; with a change waiting, it is as of its count.
- **The refresh:** bounded per run and resumed by the next; a change committed during a recount stays queued. A batch reads at most `SCAN` queue entries (from `EXPLAIN ANALYZE`, 20 of 5,000), and a batch whose recount meets a concurrent change isn't retried in the same run, while the others go on.
- **Authorization changes with no recount, for a reader:** a file's ACL narrowed; a file moved into a closed folder (an owner still sees it); an annexed file's annex folder changed to one the reader can't read; an annex row deleted marks its folder. Past `QUEUED_LIVE_FILES` files in the folders waiting (one folder of three files is past a cap of two), a reader's map says counting, while the whole share's sums the roll-up.
- **The age gauge.**
- **Annexed files** (`test_the_share_maps_roll_up_judges_annexed_files_as_files_are`):
  - a group keeps its annex folder;
  - a NAS folder closed shows at once;
  - an annex folder change marks the catalog folder;
  - an annexed file with no annex folder counts for nobody, equal to the live count throughout.
- **Portal:** "counting" and "As of".

**Undoings: 20, each caught** (the first 12 before the review, rerun after it; 13 to 20 the review's fixes):
1. a new file marks its folder (caught once a bare insert was tested: every test's file had also been updated);
2. a moved file marks the folder it left;
3. a folder moved, renamed or deleted marks itself;
4. a location's tier change marks its file's folder;
5. an annex folder change marks its file's folder;
6. a recount takes off only what its snapshot saw;
7. a folder not counted makes the map counting;
8. a folder with a change waiting is as of its count;
9. an annexed group with no annex folder counts for nobody;
10. a run is bounded;
11. the backfill counts folders from before;
12. the whole share counts every group (decision 56);
13. a reader's map counts a folder with a change waiting live (5 tests fail: the ACL, move and annex tests and the property test);
14. the live part goes through the read predicate;
15. a folder counted live isn't also summed from the roll-up;
16. too many folders waiting make a reader's map say counting;
17. an annex row going marks its file's folder;
18. a batch reads the oldest entries by `seq` (the old `GROUP BY` over the whole queue);
19. a failed recount's folders wait for the next run;
20. the cap counts the waiting folders' files, not the folders.

## Lanes

After the review's fixes, at 7b1747a9:

| Lane | Result |
|---|---|
| Service | 2722 passed, 51 skipped |
| Ruff | check and format clean |
| Portal (unchanged) | 335 passed in 39 files; typecheck, lint and `check:client` clean |
| M4 exit test (port base 20400) | 15/15 service checks, 10/10 Playwright |
| Annex exit test | not run: the map isn't on its path |

Before the review, at 6391e481: service 2716 passed and 51 skipped, portal 335 in 39 files, M4 15/15 and 10/10.
