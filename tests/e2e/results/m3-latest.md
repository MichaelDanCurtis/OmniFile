# M3 exit test results

- When: 2026-11-12T00:45:56Z, 274 s wall, 97 h virtual
- Where: edge1, Ubuntu 26.04 LTS, kernel 7.0.0-42-generic, 8 CPUs
- Repository: HEAD 7daa545c (7daa545c)
- Test script: tests/e2e/m3_exit_test.sh sha256 d5b557d0f3bb3a7973a0fe8486e9bb69f68d1b7cbe40885dc80e8e6990b2c61f
- Daemon: omnifile-edge 0.1.0, sha256 d4b4d4e12199c80b
- Samba: Version 4.23.6-Ubuntu-4.23.6+dfsg-1ubuntu2.2; Ganesha: NFS-Ganesha Release = V15.6; Postgres under libfaketime.so.1
- Backends: sim_http two-account (:10000, live ZRS Smart + archive LRS) and WORM (:10001, one LRS account, version-level WORM), one shared virtual clock
- Files: 16 MiB, seeded pseudo-random
- Result: **137 passed, 0 failed**

| | Check |
|---|---|
| PASS | shared clock at +0: sim, api/worker file and Postgres (libfaketime) agree with the wall clock |
| PASS | one /_sim/advance moves everything: +1 h on both facades, the shared file and Postgres now() (db offset 3600.0 s) |
| PASS | winbind resolves the share groups and users |
| PASS | enrolment: enrolled: edge_id=1 api_url=https://127.0.0.1:8443 identity=/var/lib/omnifile/identity |
| PASS | omnifile-edge active |
| PASS | share roots materialised (wait failfast smbwait smbfail smart worm) |
| PASS | nfs-ganesha-omni and smbd active |
| PASS | FSAL_OMNI connected |
| PASS | WORM enabled on share worm: 4-day retention, lock deadline 2026-11-12 02:43:03.617958+00:00 |
| PASS | rewind depth 1 day (Owners, within the ArchiveAdmin maximum); WORM in its validation window |
| PASS | worker active on the dev clock, with the M3 jobs |
| PASS | wrote wait/w.segy over NFSv4.2 as geo-writer |
| PASS | wrote wait/pin.segy over NFSv4.2 as geo-writer |
| PASS | wrote failfast/ff.segy over NFSv4.2 as geo-writer |
| PASS | wrote failfast/ff3.segy over NFSv4.2 as geo-writer |
| PASS | wrote smart/s.segy over NFSv4.2 as geo-writer |
| PASS | wrote smart/pinned.segy over NFSv4.2 as geo-writer |
| PASS | wrote worm/v.segy over NFSv4.2 as geo-writer |
| PASS | wrote wait/sup.segy over NFSv4.2 as geo-writer |
| PASS | wrote wait/del.segy over NFSv4.2 as geo-writer |
| PASS | wrote smbwait/a.segy over SMB as geo-writer (NTLMv2 against the DC) |
| PASS | wrote smbfail/b.segy over SMB as geo-writer (NTLMv2 against the DC) |
| PASS | wait/w.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 7404498076ab96d9) |
| PASS | wait/pin.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 6ebb336bf9090818) |
| PASS | failfast/ff.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 d37313464bcfce8d) |
| PASS | failfast/ff3.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 53bc500371a276de) |
| PASS | smart/s.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 20080ac800c412c7) |
| PASS | smart/pinned.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 28ca0817fecb7d5a) |
| PASS | worm/v.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 9f60f9fcd4c61803) |
| PASS | wait/sup.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 8dbf3acd1c8f0e6f) |
| PASS | wait/del.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 d086d6b3c045f2c9) |
| PASS | smbwait/a.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 6d427277cbc8e573) |
| PASS | smbfail/b.segy committed and verified by the worker's read-back (azure_read_crc, CRC64 d231843e87299f67) |
| PASS | smart/s.segy lands in Smart on the live (ZRS) account: no explicit tier sent at upload |
| PASS | wait/w.segy (scheduled mode) is set Hot explicitly: Smart must not cool it before the warm day |
| PASS | WORM uploads land Hot in the WORM account |
| PASS | overwrite of worm/v.segy committed as a new version ([30, 44]); both blobs kept in sim: [true, true] |
| PASS | every committed version has retention_until = commit + 4 days (["2026-11-15 17:43:18", "2026-11-15 17:43:32"]) |
| PASS | per-version retention stamped on the WORM blobs (worm_stamp sweep: Azure version-level immutability) |
| PASS | the old version 30 can't be destroyed: sim delete -> Immutable; catalog delete, prune, and location delete -> OF002 |
| PASS | pin wait/pin.segy to hot for 84 h, override_archive (Owner's explicit choice past the 72 h archive day) |
| PASS | pin smart/pinned.segy to cool (warm) for 30 h |
| PASS | 10 GiB/h budget: the first 6 GiB High recall is admitted (restore speed high, ETA 18:43:34); the 2nd and 3rd wait in OmniFile's queue (no restore yet) with ETAs ["19:19:34", "19:55:34"] |
| PASS | a 12 GiB recall has no promised time (none above 10 GB), at Standard |
| PASS | the WORM share auto-locked when its validation window ended (one-way from now) |
| PASS | an hour later the pipeline drained: the 2nd 6 GiB recall is admitted at High; the 3rd: requested (speed None) |
| PASS | wait/w.segy is Cool (Set Blob Tier in place on the live account) |
| PASS | smbwait/a.segy (written over SMB) is Cool |
| PASS | worm/v.segy is Cool, in place in the WORM account |
| PASS | smart/s.segy is still Smart (Microsoft moves it; OmniFile waits for the archive day) |
| PASS | smart/pinned.segy is Cool while its pin lasts (explicit tier) |
| PASS | smart/pinned.segy: the scheduler moved it Smart -> Cool for the pin (audit tier.move) and will look again when the pin ends |
| PASS | wait/pin.segy (pinned hot) is Hot, not Cool |
| PASS | wait/pin.segy: evaluated on day 1 and held for the pin (no move off Hot; next look at the pin's end, 84 h) |
| PASS | wait/w.segy is Cold |
| PASS | failfast/ff.segy is Cold |
| PASS | smart/pinned.segy re-evaluated after its pin ended (next look moved past the pin's end, to the archive day) |
| PASS | smart/pinned.segy after its pin expired: still Cool, explicit tier (one way: never back to Smart) |
| PASS | a move back to Smart is refused and audited (tier.move.refused) |
| PASS | smart/pinned.segy still Cool in sim after the refused move |
| PASS | wait/w.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | wait/sup.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | wait/del.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | failfast/ff.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | failfast/ff3.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | smbwait/a.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | smbfail/b.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | smart/s.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | smart/pinned.segy archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim) |
| PASS | worm/v.segy archived in place (Set Blob Tier, same WORM blob; no copy, no delete) |
| PASS | wait/w.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | wait/sup.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | wait/del.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | failfast/ff.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | failfast/ff3.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | smbwait/a.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | smbfail/b.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | smart/s.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | worm/v.segy is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED) |
| PASS | wait/pin.segy on day 3: Hot, resident, not archived (the hot pin overrides the archive day) |
| PASS | the superseded WORM version is beyond rewind depth + grace (1 + 1 days) but still retained, so not pruned |
| PASS | NFSv4.2 read of archived failfast/ff.segy: EIO in 0.14 s, no zeros |
| PASS | NFSv3 read of archived failfast/ff3.segy: EIO in 0.12 s, no zeros |
| PASS | SMB read of archived smbfail/b.segy as geo-reader: NT_STATUS_IO_DEVICE_ERROR in 0.11 s |
| PASS | failfast/ff.segy: recall queued and the worker started a Copy Blob rehydrate (Standard) into live/recall; ETA 2026-11-15 09:14:28 (15 h on the virtual clock) |
| PASS | smbfail/b.segy: recall queued and the worker started a Copy Blob rehydrate (Standard) into live/recall; ETA 2026-11-15 09:14:28 (15 h on the virtual clock) |
| PASS | a .RECALLING.txt note next to failfast/ff.segy |
| PASS | 4 readers (NFSv4.2 wait, smart, WORM, SMB smbclient) all waiting after 10 s: the rehydrate is running, no error, no zeros |
| PASS | wait/w.segy: recall rehydrating (the read started it) |
| PASS | smart/s.segy: recall rehydrating (the read started it) |
| PASS | worm/v.segy: recall rehydrating (the read started it) |
| PASS | smbwait/a.segy: recall rehydrating (the read started it) |
| PASS | the WORM file's rehydrate copies into the non-WORM recall container (worm/recall); the archived original is untouched |
| PASS | wait/sup.segy: recall rehydrating (the read started it) |
| PASS | a .RECALLING.txt note next to wait/sup.segy |
| PASS | wait/del.segy: recall rehydrating (the read started it) |
| PASS | a .RECALLING.txt note next to wait/del.segy |
| PASS | both readers are waiting (NFSv4.2, wait share) |
| PASS | replaced wait/sup.segy from another client (NFSv3) while its recall rehydrates; the write did not wait for the recall (0.21 s) |
| PASS | the waiting reader of wait/sup.segy was released 0.37 s after the replace, with no error and no zeros: after the 1048576 bytes of resident header it had already read (the old version's), every byte is the new file's (240 blocks of 64 KiB) |
| PASS | a new read of wait/sup.segy gets the whole new file, byte-identical |
| PASS | wait/sup.segy: its recall ended superseded (replaced) when the new version was committed, not failed; no failure text; ended by a content version |
| PASS | the .RECALLING.txt note next to wait/sup.segy is gone |
| PASS | the old version's rehydrate is left running (restore in_progress): not aborted, and not polled for this recall any more |
| PASS | wait/sup.segy holds the new version: resident, local CRC64-NVME = the catalog's |
| PASS | deleted wait/del.segy on the edge while its recall rehydrates |
| PASS | wait/del.segy: its recall ended superseded (deleted) with the tombstone, not failed |
| PASS | the .RECALLING.txt note next to where wait/del.segy was is gone |
| PASS | the waiting reader of the deleted file was released 1.33 s after the delete, with an error (cat: /mnt/omnifile-m3/wait/del.segy: Input/output error ) and nothing past the 1052672 bytes of resident header it had already read: not a wait with no end, no zeros |
| PASS | the edge reported no failed fill for either recall |
| PASS | wait/w.segy: the waiting reader got byte-identical bytes after the rehydrate (26.264 s) |
| PASS | smart/s.segy: the waiting reader got byte-identical bytes after the rehydrate (26.264 s) |
| PASS | worm/v.segy: the waiting reader got byte-identical bytes after the rehydrate (26.158 s) |
| PASS | smbwait/a.segy: the waiting SMB reader got byte-identical bytes |
| PASS | wait/sup.segy: the old version's rehydrate finished and the change feed closed it (restore completed, its copy committed for the 7-day window); the recall is still superseded |
| PASS | wait/sup.segy still holds the new file after the old version's rehydrate finished |
| PASS | wait/w.segy back on the edge: resident, local CRC64-NVME 7404498076ab96d9 = the catalog's (verified at upload) |
| PASS | wait/w.segy recall available; restore completed; recall copy committed |
| PASS | smart/s.segy back on the edge: resident, local CRC64-NVME 20080ac800c412c7 = the catalog's (verified at upload) |
| PASS | smart/s.segy recall available; restore completed; recall copy committed |
| PASS | worm/v.segy back on the edge: resident, local CRC64-NVME 489b591bea69a08d = the catalog's (verified at upload) |
| PASS | worm/v.segy recall available; restore completed; recall copy committed |
| PASS | smbwait/a.segy back on the edge: resident, local CRC64-NVME 6d427277cbc8e573 = the catalog's (verified at upload) |
| PASS | smbwait/a.segy recall available; restore completed; recall copy committed |
| PASS | failfast/ff.segy recall available (the edge filled it after the rehydrate) |
| PASS | smbfail/b.segy recall available (the edge filled it after the rehydrate) |
| PASS | NFS read of failfast/ff.segy after the recall: byte-identical |
| PASS | SMB read of smbfail/b.segy after the recall: byte-identical |
| PASS | failfast/ff3.segy (NFSv3 failfast read) also has its recall: available |
| PASS | wait/pin.segy archived after its hot pin expired |
| PASS | wait/pin.segy then stubbed |
| PASS | after retention ended the old version 30 was pruned (catalog: pruned) and its blob deleted from the WORM account; the current version stays (committed) |
| PASS | change-feed sync of live/data caught up; steady-state lag 1.7 s (sim feed_lag 0 + the 2 s sync interval) |
| PASS | change feed followed on 5 containers, 120 records applied, no errors |
| PASS | no panic in the daemon journal |
| PASS | no traceback in the worker or api journals |
| PASS | no dead-lettered worker messages |

## Notes

- Policies: scheduled `{"mode": "scheduled", "clock": "written", "warm_day": 1, "cold_day": 2, "archive_day": 3, "stub_at": "archive"}`; smart `{"mode": "smart", "clock": "written", "archive_day": 3, "stub_at": "archive"}`; WORM share `{"mode": "scheduled", "clock": "written", "warm_day": 1, "archive_day": 3, "stub_at": "archive"}`.
- Budget, before the hour passed: `[(1, 6, 'high', 'rehydrating', 'high', '18:43:34'), (2, 6, 'high', 'requested', None, '19:19:34'), (3, 6, 'high', 'requested', None, '19:55:34'), (4, 12, 'standard', 'rehydrating', 'standard', '')]`
- Change-feed lag per container (samples include virtual-clock jumps, so their max is the jump, not provider lag):
  - archive/data: 50 records in 12 pages, max 82804 s, median page max 1.2 s
  - live/data: 48 records in 13 pages, max 54301 s, median page max 1.5 s
  - live/recall: 26 records in 8 pages, max 82804 s, median page max 32.1 s
  - worm/data: 9 records in 7 pages, max 323 s, median page max 0.8 s
  - worm/recall: 2 records in 2 pages, max 323 s, median page max 162.1 s
  - Steady state after the probe write, live/data: last lag 1.7 s.
- **Virtual time.** One OffsetClock file is the clock of both sim facades, the api and the worker, and (through libfaketime) of Postgres; `/_sim/advance` moves them together. The edge keeps real time. Worker sweeps and restore polls run every 2 s (`OMNIFILE_WORKER_MAX_SWEEP_S`, `OMNIFILE_RECALL_POLL_S`, dev only).
- **Sim, not Azure.** Rehydrate times are Microsoft's stated upper bounds on the virtual clock (Standard 15 h; High 1 h floor at 10 GiB/h per account). Real recall times and real change-feed lag are pilot 4 (docs/milestones/real-azure-checks.md).
