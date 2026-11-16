# M1 exit test results

- When: 2026-10-06T01:24:37Z, 207 s
- Where: edge1, Ubuntu 26.04 LTS, kernel 7.0.0-28-generic, 4 CPUs
- Repository: HEAD 7bbec7a103db5f682b899024d7803efbc64ec826 (m2-review)
- Test script: tests/e2e/exit_test.sh sha256 6323e036cd6666e3242feca03e13328d415f60d99b649aca805c44bec8547f0d
- Ganesha: NFS-Ganesha Release = V15.6; FSAL_OMNI sha256 3ac683062176e539
- Daemon: omnifile-edge 0.0.0, sha256 fafaa8d62f8c77ba
- Postgres: 17.11 (Ubuntu 17.11-1.pgdg26.04+2)
- Files: 11 (largest 256 MiB, others 96 or 48 MiB), seeded pseudo-random
- Result: **83 passed, 0 failed**

| | Check |
|---|---|
| PASS | enrolment over TLS with the pinned CA: enrolled: edge_id=1 api_url=https://127.0.0.1:8443 identity=/var/lib/omnifile/identity |
| PASS | omnifile-edge.service active (startup checks: xfs, CAP_SYS_ADMIN) |
| PASS | share roots materialised from the feed (wait, failfast) |
| PASS | share roots carry the catalog's owner and mode (20002:10000 0755) |
| PASS | nfs-ganesha-omni.service active |
| PASS | Ganesha initialised |
| PASS | FSAL_OMNI connected to the daemon |
| PASS | Ganesha runs with the unit's capability set: CAP_SYS_ADMIN effective; no CAP_SYS_MODULE/RAWIO/PTRACE in its bounding set (CapEff=00000000182004df CapBnd=00000000192004df); not --privileged |
| PASS | no CRIT in the Ganesha log at startup (FSAL_OMNI has CAP_SYS_ADMIN; D-Bus policy installed) |
| PASS | NFSv3, v4.1 and v4.2 mounts of both exports |
| PASS | wrote wait/line_v3.segy (96 MiB) over NFSv3 in 0.11 s |
| PASS | wrote wait/line_v41.segy (96 MiB) over NFSv4.1 in 0.15 s |
| PASS | wrote wait/line_v42.segy (256 MiB) over NFSv4.2 in 0.32 s |
| PASS | wrote failfast/line_v3.segy (96 MiB) over NFSv3 in 0.12 s |
| PASS | wrote failfast/line_v41.segy (96 MiB) over NFSv4.1 in 0.10 s |
| PASS | wrote failfast/line_v42.segy (96 MiB) over NFSv4.2 in 0.10 s |
| PASS | wrote wait/kill.segy (96 MiB) over NFSv4.2 in 0.09 s |
| PASS | wrote wait/fence-fail.segy (48 MiB) over NFSv4.2 in 0.09 s |
| PASS | wrote wait/fence-kill.segy (48 MiB) over NFSv4.2 in 0.08 s |
| PASS | wrote failfast/outage.segy (48 MiB) over NFSv4.2 in 0.31 s |
| PASS | wait/line_v3.segy committed in 2 s after the copy (file_id 3, version 2, verified_by azure_read_crc) |
| PASS | wait/line_v41.segy committed in 1 s after the copy (file_id 4, version 4, verified_by azure_read_crc) |
| PASS | wait/line_v42.segy committed in 1 s after the copy (file_id 5, version 6, verified_by azure_read_crc) |
| PASS | failfast/line_v3.segy committed in 0 s after the copy (file_id 6, version 11, verified_by azure_read_crc) |
| PASS | failfast/line_v41.segy committed in 1 s after the copy (file_id 7, version 13, verified_by azure_read_crc) |
| PASS | failfast/line_v42.segy committed in 0 s after the copy (file_id 8, version 15, verified_by azure_read_crc) |
| PASS | wait/kill.segy committed in 1 s after the copy (file_id 9, version 17, verified_by azure_read_crc) |
| PASS | wait/fence-fail.segy committed in 1 s after the copy (file_id 10, version 19, verified_by azure_read_crc) |
| PASS | wait/fence-kill.segy committed in 0 s after the copy (file_id 11, version 21, verified_by azure_read_crc) |
| PASS | failfast/outage.segy committed in 1 s after the copy (file_id 12, version 23, verified_by azure_read_crc) |
| PASS | wait/line_v3.segy: blob 3/2 in sim, 100663296 bytes, CRC64-NVME f6622cd1188ff4d8 = source = catalog, SHA-256 equal (plain bytes) |
| PASS | POSIX metadata in the edge format (file_owner=20002 file_group=10000 file_permissions=0640: written 0644, 'other' normalised away by the edge, access-control.md 1.1); no explicit access tier sent (tier Smart, inferred None) |
| PASS | wait/line_v41.segy: blob 4/4 in sim, 100663296 bytes, CRC64-NVME cac4a7adfa2e4eed = source = catalog, SHA-256 equal (plain bytes) |
| PASS | wait/line_v42.segy: blob 5/6 in sim, 268435456 bytes, CRC64-NVME 25c827543239fa0b = source = catalog, SHA-256 equal (plain bytes) |
| PASS | failfast/line_v3.segy: blob 6/11 in sim, 100663296 bytes, CRC64-NVME 44a0e7d0bcb018d6 = source = catalog, SHA-256 equal (plain bytes) |
| PASS | failfast/line_v41.segy: blob 7/13 in sim, 100663296 bytes, CRC64-NVME 4d79c18a1eadb18b = source = catalog, SHA-256 equal (plain bytes) |
| PASS | failfast/line_v42.segy: blob 8/15 in sim, 100663296 bytes, CRC64-NVME 833d3cae7ff8205f = source = catalog, SHA-256 equal (plain bytes) |
| PASS | wait/kill.segy: blob 9/17 in sim, 100663296 bytes, CRC64-NVME 5cea032c58b1ae52 = source = catalog, SHA-256 equal (plain bytes) |
| PASS | wait/fence-fail.segy: blob 10/19 in sim, 50331648 bytes, CRC64-NVME 9be14bc96bfe1143 = source = catalog, SHA-256 equal (plain bytes) |
| PASS | wait/fence-kill.segy: blob 11/21 in sim, 50331648 bytes, CRC64-NVME e3a778e149a2a607 = source = catalog, SHA-256 equal (plain bytes) |
| PASS | failfast/outage.segy: blob 12/23 in sim, 50331648 bytes, CRC64-NVME c96eaaf29061b42a = source = catalog, SHA-256 equal (plain bytes) |
| PASS | wrote wait/outbox.segy (48 MiB) over NFSv4.2 in 0.07 s |
| PASS | upload of wait/outbox.segy held in the outbox while Blob refuses Put Block List (op 1-22 recorded, clone kept: 1 clone(s)) |
| PASS | after the restart the outbox replayed op 1-22: wait/outbox.segy committed as exactly one version (34), marked resident |
| PASS | wait/outbox.segy: replayed blob has the source's size and CRC64-NVME |
| PASS | outbox drained and commit clones deleted |
| PASS | wait/line_v3.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 100663296 bytes, catalog state stub |
| PASS | wait/line_v41.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 100663296 bytes, catalog state stub |
| PASS | wait/line_v42.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 268435456 bytes, catalog state stub |
| PASS | failfast/line_v3.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 100663296 bytes, catalog state stub |
| PASS | failfast/line_v41.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 100663296 bytes, catalog state stub |
| PASS | failfast/line_v42.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 100663296 bytes, catalog state stub |
| PASS | wait/kill.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 100663296 bytes, catalog state stub |
| PASS | wait/fence-fail.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 50331648 bytes, catalog state stub |
| PASS | wait/fence-kill.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 50331648 bytes, catalog state stub |
| PASS | failfast/outage.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 50331648 bytes, catalog state stub |
| PASS | wait/outbox.segy stubbed: marker PARTIAL, resident 1052672 (headers + 1 MiB, page-aligned), holes from 1052672 to EOF (no data extent after it), disk usage dropped to 1052672 of 50331648 bytes, catalog state stub |
| PASS | wait/line_v3.segy read back over NFSv3 (wait policy): byte-identical, 100663296 bytes in 0.328 s (recall fill inside the read) |
| PASS | wait/line_v41.segy read back over NFSv4.1 (wait policy): byte-identical, 100663296 bytes in 0.229 s (recall fill inside the read) |
| PASS | wait/line_v42.segy read back over NFSv4.2 (wait policy): byte-identical, 268435456 bytes in 0.717 s (recall fill inside the read) |
| PASS | failfast/line_v3.segy read back over NFSv3 (failfast policy): byte-identical, 100663296 bytes in 0.316 s (recall fill inside the read) |
| PASS | failfast/line_v41.segy read back over NFSv4.1 (failfast policy): byte-identical, 100663296 bytes in 0.285 s (recall fill inside the read) |
| PASS | failfast/line_v42.segy read back over NFSv4.2 (failfast policy): byte-identical, 100663296 bytes in 0.301 s (recall fill inside the read) |
| PASS | truncate -s 0 over NFSv4.2 of wait/line_v42.segy right after its fill: 0.12 s (no lingering DELAY) |
| PASS | cp of new bytes over the just-filled wait/line_v41.segy (NFSv4.1): 0.13 s, exactly the new bytes on disk |
| PASS | fill of wait/kill.segy under way: 68161536 of 100663296 bytes local |
| PASS | systemd restarted the daemon (pid 49554 -> 50195, RestartMode=direct) |
| PASS | Ganesha stayed up through the daemon crash (same pid 48748; BindsTo does not fire on a direct restart) |
| PASS | reader 1 (NFSv4.2) across the daemon kill: byte-identical, no zero block, no error (10.977 s) |
| PASS | reader 2 (NFSv3) across the daemon kill: byte-identical, no zero block, no error (10.981 s) |
| PASS | no reader saw zeros while the daemon was dead (fail closed) |
| PASS | the restarted daemon took the FSAL connection back (fsal hello after the kill) |
| PASS | read of wait/fence-fail.segy during a backend outage failed with EIO after 43 s (fill FAILED; wait share), no zeros |
| PASS | truncate -s 0 of wait/fence-fail.segy after its fill failed: 0.11 s (fence lifted by FAILED) |
| PASS | failfast read of failfast/outage.segy during the outage: EIO after 43 s, no zeros |
| PASS | failfast read of failfast/outage.segy after the outage: byte-identical (25.906 s) |
| PASS | fill of wait/fence-kill.segy under way |
| PASS | truncate -s 0 of wait/fence-kill.segy after a daemon kill mid-fill: 0.11 s after the restart (resync dropped FETCHING) |
| PASS | wait/fence-kill.segy is empty on disk after the truncate |
| PASS | disk usage of wait/outbox.segy stayed at the resident prefix: allocated 1052672 of 50331648 bytes; volume used 1985015808 bytes |
| PASS | no panic in the daemon journal |
| PASS | no malformed FSAL message in the daemon journal (NS_HINT and friends decode) |
| PASS | Ganesha still active at the end |

## Notes

- **failfast on hot data waits.** M1 has no Archive tier, so every stub is ONLINE (readable by range GET). The daemon answers DELAY (or parks the read) for ONLINE data even on failfast shares, because a fetch takes seconds (crates/omnifile-edge/src/core.rs, on_read_intent); failfast's immediate EIO is for archived data (M3). EIO on failfast is exercised here through a backend outage (step 12): the fill fails after the Blob client's retries (about 40 s), the read gets EIO, and the same read succeeds once Blob is back.
- **Ganesha runs with MDCACHE Close_Fast** (dev/edge/ganesha-base.conf). Without it Ganesha's fd cache keeps an NFSv3 writer's fd open, the stub handshake's write-lease check refuses the file, and files written over NFSv3 never stub.
- **One machine.** The service (Postgres 17, sim Blob facade, enrol, api, worker) runs on the edge machine itself, over 127.0.0.1; shares are sec=sys with root squashed, files are written as uid 20002.
- **Recall here means an online fetch.** M1 reads hot data back by range GET into the stub (no rehydrate); the catalog keeps the file in state stub after an online fetch, since the service is not told (recall_filled is for archive recalls).
