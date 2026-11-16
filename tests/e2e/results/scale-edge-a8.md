# A8 at scale (smb-guard drain rate, risk R6): 2026-11-04T07:07:58Z

kernel `7.0.0-42-generic`, 8 vCPUs, 46 GiB; the A-suite's a08_flood.sh, unchanged (run.sh --only a08, SG_FLOOD=N), binaries from /home/michaelcurtis.guest/.cache/omnifile/edge-target/release; its volume an xfs image on tmpfs.

Limits before: pid_max 4194304, threads-max 353945, max_map_count 1048576, nr_open 2147483584, nofile 1024/524288, nproc 176972, MemTotal 46 GiB.

Limits raised to: pid_max 4194304, threads-max 353945, max_map_count 1048576, nofile 1048576/1048576, nproc unlimited.

| readers | A8 | zeros while the guard was stopped | zeros after the drain | completed OK | errors | drain (s) | answers/s | A8b (3,000 readers, LimitNOFILE=1024) | MemAvailable min (MiB) | run (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 4000 | PASS | 0 | 0 | 4000 | 0 | 2 | 2000 | PASS | 44179 | 15 |
| 8000 | PASS | 0 | 0 | 8000 | 0 | 2 | 4000 | PASS | 43747 | 15 |
| 17000 | PASS | 0 | 0 | 17000 | 0 | 3 | 5667 | PASS | 43251 | 15 |

The drain is timed by a08_flood.sh from SIGCONT of the guard to the flood tool's exit, to the second.
