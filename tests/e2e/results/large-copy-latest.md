# #54 large-copy test, 2026-11-06T18:11:14Z

- rev: 03419fb; 4096 MiB per copy, a Ganesha restart at 50%; quiet_s 3
- pauses at 25%: NFSv4.1 10 s (one version); NFSv3 4 s (one version) and 30 s (the extra partial version of docs/design/edge-metadata.md, "The NFSv3 limit")
- 16 passed, 0 failed

| | check |
|---|---|
| PASS | share roots materialised |
| PASS | Ganesha up, FSAL_OMNI connected |
| PASS | the FSAL negotiated OPEN_STATE (feature bit 5) |
| PASS | v4.1: the copy finished through the restart (writer exit 0) |
| PASS | v4.1: wait/big-v41.segy committed with the whole file's CRC-64/NVME (713b1d6c9c49fdef, 4294967296 bytes) |
| PASS | v4.1, a 10 s pause: exactly one committed content version (no partial one): {"versions": [{"version_id": 6, "edge_op_id": "1-2", "size": 4294967296, "crc64nvme": "713b1d6c9c49fdef"}]} |
| PASS | v4.1: its blob is the source, byte for byte |
| PASS | v3: the copy finished through the restart (writer exit 0) |
| PASS | v3: wait/big-v3.segy committed with the whole file's CRC-64/NVME (9df5f2e1cb430535, 4294967296 bytes) |
| PASS | v3, a 4 s pause: exactly one committed content version (no partial one): {"versions": [{"version_id": 9, "edge_op_id": "1-4", "size": 4294967296, "crc64nvme": "9df5f2e1cb430535"}]} |
| PASS | v3: its blob is the source, byte for byte |
| PASS | v3: the copy finished through the restart (writer exit 0) |
| PASS | v3: wait/big-v3-long.segy committed with the whole file's CRC-64/NVME (3702a89c403d0230, 4294967296 bytes) |
| PASS | v3, a 30 s pause (over 2 x quiet_s = 6 s): the documented extra partial version (docs/design/edge-metadata.md, "The NFSv3 limit"), then the whole file: {"versions": [{"version_id": 12, "edge_op_id": "1-6", "size": 1073741824, "crc64nvme": "e73847097bda6149"}, {"version_id": 14, "edge_op_id": "1-7", "size": 4294967296, "crc64nvme": "3702a89c403d0230"}]} |
| PASS | v3: its blob is the source, byte for byte |
| PASS | no panic in the daemon journal |
