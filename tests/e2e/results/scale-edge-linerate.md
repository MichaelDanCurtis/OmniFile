# Line-rate ceiling, 10 GiB: 2026-11-04T07:43Z

kernel `7.0.0-42-generic`, 8 vCPUs, 46 GiB; NFS client and server on the same VM (127.0.0.1); the data volume a loop file on /work/dev/.artifacts/wp3/data.img (virtiofs to the Mac's external SSD); the backend the dev sim (Python, holding each blob in RAM). `tests/scale-edge/line_rate.sh --gib 10`, second run.

| stage | seconds | MB/s | path |
|---|---|---|---|
| NFSv4.1 write, to the last write() returning | 13.8 | 778 | client-side, the page cache absorbing |
| NFSv4.1 write, through fsync | 27.8 | 386 | on the edge's data volume |
| upload: fsync to committed in the catalog (quiet period 3 s, hash, upload, commit) | 63.6 | 169 | edge to the sim |
| verified by the worker (the sim's bytes read back) | 20.8 | 516 | service side |
| recall: NFSv4.1 read of the stub (O_DIRECT), the fill from the sim behind it | 50.1 | 214 | sim to the edge to the client |

- **Content:** the sim's blob had the file's SHA-256. The stub kept 1,052,672 bytes resident (headers and 1 MiB).
- **Stopped early:** the run was stopped after the recall read, because the Mac's swap passed 8 GiB. The sim held the 10 GiB blob in the VM's RAM, about twice during the commit. Two steps did not run: the filled file's SHA-256 check and the resident re-read. The cleanup deleted the file.
- **A partial version in the first run:** that run's committed content versions were 4,682,940,416 bytes, at 23:35:22 while the client was still writing (23:35:12–23:35:47), then the full 10,737,418,240 bytes, 7 s after the fsync. The script now waits for the full-size version. The second run had only the full version.
