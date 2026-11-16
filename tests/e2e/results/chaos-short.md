# Chaos soak: 2026-11-04T06:03:55Z

seed **2013365296**, 1800 s planned, ran 2711 s; 16 faults planned (2 of each); label: short

Replay: `sudo tests/chaos/soak.sh --seed 2013365296 --duration 1800 --each 2`

| fault | injected | skipped | recovered (s, max) |
|---|---|---|---|
| kill_edge | 2 | 0 | 2.0 |
| kill_ganesha | 2 | 0 | 3.1 |
| kill_smbd | 2 | 0 | 0.0 |
| kill_worker | 2 | 0 | 2.1 |
| crash_postgres | 2 | 0 | 0.0 |
| fill_disk | 2 | 0 | 0.0 |
| drop_network | 2 | 0 | 0.1 |
| restart_mid_fill | 2 | 0 | 0.0 |

| invariant | violations |
|---|---|
| zeros | 0 |
| wrong_bytes | 0 |
| enoent | 0 |
| lost_commit | 1 |
| recall | 0 |
| recovery | 0 |

| load | count |
|---|---|
| archived | 36 |
| catalog_sync_error | 3 |
| delete_ok:nfs-v3 | 7 |
| delete_ok:nfs-v41 | 6 |
| delete_ok:nfs-v42 | 9 |
| listings | 209 |
| read_err:smb:NT_STATUS_CONNECTION_REFUSED) | 7 |
| read_err:smb:NT_STATUS_IO_DEVICE_ERROR | 11 |
| read_err:v3:EIO | 6 |
| read_err:v41:EIO | 3 |
| read_err:v42:EIO | 5 |
| read_ok:smb:archived | 34 |
| read_ok:smb:filled | 308 |
| read_ok:smb:unfilled | 38 |
| read_ok:v3:archived | 52 |
| read_ok:v3:filled | 221 |
| read_ok:v3:unfilled | 99 |
| read_ok:v41:archived | 32 |
| read_ok:v41:filled | 209 |
| read_ok:v41:unfilled | 80 |
| read_ok:v42:archived | 41 |
| read_ok:v42:filled | 214 |
| read_ok:v42:unfilled | 78 |
| recalls_seen | 29 |
| write_err:nfs-v3:ENOSPC | 2 |
| write_err:nfs-v41:ENOSPC | 2 |
| write_err:nfs-v42:ENOSPC | 3 |
| write_err:smb:NT_STATUS_DISK_FULL | 3 |
| write_ok:nfs-v3 | 73 |
| write_ok:nfs-v41 | 111 |
| write_ok:nfs-v42 | 70 |
| write_ok:smb | 33 |

| at the end | count |
|---|---|
| committed_and_hashed | 238 |
| deleted_stays_deleted | 26 |
| excluded:failed | 3 |
| placeholder_present | 238 |
| recall:available | 29 |

settle: `{"settled_s": null, "pending": ["wait/soak/d2/f00162.bin"], "n_pending": 1, "active_recalls": []}`

Errors by the fault active when they happened:

- fill_disk:read_err:smb:NT_STATUS_IO_DEVICE_ERROR: 11
- fill_disk:read_err:v3:EIO: 6
- fill_disk:read_err:v41:EIO: 2
- fill_disk:read_err:v42:EIO: 5
- fill_disk:write_err:ENOSPC: 7
- fill_disk:write_err:NT_STATUS_DISK_FULL: 3
- kill_smbd:read_err:smb:NT_STATUS_CONNECTION_REFUSED): 6
- none:read_err:v41:EIO: 1
- restart_mid_fill:read_err:smb:NT_STATUS_CONNECTION_REFUSED): 1

Faults:

- `{"n": 1, "fault": "crash_postgres", "at_s": 240.2, "took_s": 30.1, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 28.0, "start_rc": 0}`
- `{"n": 2, "fault": "kill_ganesha", "at_s": 336.5, "took_s": 0.0, "recovered_s": 3.1}`
- `{"n": 3, "fault": "fill_disk", "at_s": 410.8, "took_s": 85.3, "recovered_s": 0.0, "hold_s": 85.3, "filled_gib": 62.46, "left_mib": 2}`
- `{"n": 4, "fault": "kill_edge", "at_s": 496.2, "took_s": 0.0, "recovered_s": 2.0}`
- `{"n": 5, "fault": "restart_mid_fill", "at_s": 543.3, "took_s": 9.8, "recovered_s": 0.0, "file": "wait/soak/big/b002.bin", "filled_before_restart_mib": 18, "restart_after_s": 4.3, "readers": {"v3": {"ok": true, "err": null}, "v41": {"ok": true, "err": null}}}`
- `{"n": 6, "fault": "kill_ganesha", "at_s": 621.4, "took_s": 0.0, "recovered_s": 3.0}`
- `{"n": 7, "fault": "kill_edge", "at_s": 729.8, "took_s": 0.0, "recovered_s": 0.0}`
- `{"n": 8, "fault": "drop_network", "at_s": 798.1, "took_s": 71.4, "recovered_s": 0.0, "hold_s": 42.5, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 9, "fault": "kill_smbd", "at_s": 894.6, "took_s": 17.8, "recovered_s": 0.0, "hold_s": 17.7, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 10, "fault": "drop_network", "at_s": 1013.7, "took_s": 61.4, "recovered_s": 0.1, "hold_s": 30.7, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 11, "fault": "kill_worker", "at_s": 1082.3, "took_s": 0.0, "recovered_s": 2.1}`
- `{"n": 12, "fault": "restart_mid_fill", "at_s": 1208.7, "took_s": 11.9, "recovered_s": 0.0, "file": "wait/soak/big/b004.bin", "filled_before_restart_mib": 18, "restart_after_s": 3.0, "readers": {"v41": {"ok": true, "err": null}, "v3": {"ok": true, "err": null}}}`
- `{"n": 13, "fault": "fill_disk", "at_s": 1333.0, "took_s": 65.3, "recovered_s": 0.0, "hold_s": 65.3, "filled_gib": 62.33, "left_mib": 2}`
- `{"n": 14, "fault": "crash_postgres", "at_s": 1441.6, "took_s": 0.0, "recovered_s": 0.0, "mode": "one backend SIGKILL (Postgres's own crash restart)", "pid": "72638"}`
- `{"n": 15, "fault": "kill_smbd", "at_s": 1524.9, "took_s": 10.3, "recovered_s": 0.0, "hold_s": 10.2, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 16, "fault": "kill_worker", "at_s": 1634.6, "took_s": 0.0, "recovered_s": 2.1}`

Violations (1):

- `{"kind": "lost_commit", "dt": 2709.5, "fault": "none", "key": "wait/soak/d2/f00162.bin", "why": "not committed", "row": null}`

**RED**: 1 violations, every fault at least 2 times.
