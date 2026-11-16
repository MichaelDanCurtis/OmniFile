# Chaos soak: 2026-11-04T07:32:20Z

seed **2843486889**, 22500 s planned, ran 5146 s (ABORTED); 96 faults planned (12 of each); label: 6h

Replay: `sudo tests/chaos/soak.sh --seed 2843486889 --duration 22500 --each 12`

| fault | injected | skipped | new main pid after (s, max) | every unit active after (s, max) |
|---|---|---|---|---|
| kill_edge | 1 | 0 | 1.3 | 0.1 |
| kill_ganesha | 3 | 0 | 2.6 | 0.2 |
| kill_smbd | 3 | 0 | - | 0.0 |
| kill_worker | 1 | 0 | 2.2 | 0.0 |
| crash_postgres | 2 | 0 | - | 0.0 |
| fill_disk | 0 | 0 | - | - |
| drop_network | 2 | 0 | - | 0.0 |
| restart_mid_fill | 1 | 0 | - | 0.1 |

| invariant | violations |
|---|---|
| zeros | 0 |
| wrong_bytes | 0 |
| enoent | 0 |
| lost_commit | 3 |
| recall | 0 |
| recovery | 0 |

Of the lost_commit violations, 2 had the edge daemon killed or restarted within the commit quiet period after the file's close (the detector gap: `edge/crates/omnifile-edge/src/nsync.rs` reconcile_dir leaves a new regular file to the detector, whose close event dies with the process).

| load | count |
|---|---|
| archived | 69 |
| catalog_sync_error | 3 |
| delete_ok:nfs-v3 | 23 |
| delete_ok:nfs-v41 | 19 |
| delete_ok:nfs-v42 | 20 |
| listings | 379 |
| read_err:smb:NT_STATUS_CONNECTION_REFUSED | 13 |
| read_ok:smb:archived | 148 |
| read_ok:smb:filled | 581 |
| read_ok:smb:unfilled | 67 |
| read_ok:v3:archived | 94 |
| read_ok:v3:filled | 421 |
| read_ok:v3:unfilled | 156 |
| read_ok:v41:archived | 107 |
| read_ok:v41:filled | 464 |
| read_ok:v41:unfilled | 149 |
| read_ok:v42:archived | 121 |
| read_ok:v42:filled | 473 |
| read_ok:v42:unfilled | 146 |
| recalls_seen | 89 |
| write_ok:nfs-v3 | 117 |
| write_ok:nfs-v41 | 124 |
| write_ok:nfs-v42 | 133 |
| write_ok:smb | 66 |

| at the end | count |
|---|---|
| committed_and_hashed | 307 |
| deleted_stays_deleted | 62 |
| placeholder_present | 307 |
| recall:available | 89 |

settle: `{"settled_s": null, "pending": ["wait/soak/d4/f00172.bin", "wait/soak/d7/f00255.bin", "smbwait/soak/s00300.bin"], "n_pending": 3, "active_recalls": []}`

Errors by the fault active when they happened:

- kill_smbd:read_err:smb:NT_STATUS_CONNECTION_REFUSED: 13

Faults:

- `{"n": 1, "fault": "drop_network", "at_s": 301.0, "took_s": 47.7, "recovered_s": 0.0, "hold_s": 47.6, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 2, "fault": "kill_smbd", "at_s": 508.0, "took_s": 23.1, "recovered_s": 0.0, "hold_s": 23.0, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 3, "fault": "kill_smbd", "at_s": 751.7, "took_s": 9.5, "recovered_s": 0.0, "hold_s": 9.4, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 4, "fault": "kill_ganesha", "at_s": 925.8, "took_s": 2.2, "recovered_s": 0.2, "old_pid": "78545", "new_pid": "83825", "restarted_s": 2.2, "kill_t": 1793773319.562}`
- `{"n": 5, "fault": "drop_network", "at_s": 1080.8, "took_s": 55.2, "recovered_s": 0.0, "hold_s": 55.1, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 6, "fault": "kill_ganesha", "at_s": 1273.4, "took_s": 2.3, "recovered_s": 0.0, "old_pid": "83825", "new_pid": "85262", "restarted_s": 2.3, "kill_t": 1793773667.131}`
- `{"n": 7, "fault": "kill_edge", "at_s": 1582.8, "took_s": 1.4, "recovered_s": 0.1, "old_pid": "78304", "new_pid": "86572", "restarted_s": 1.3, "kill_t": 1793773976.527}`
- `{"n": 8, "fault": "kill_worker", "at_s": 1741.7, "took_s": 2.2, "recovered_s": 0.0, "old_pid": "78540", "new_pid": "87365", "restarted_s": 2.2, "kill_t": 1793774135.503}`
- `{"n": 9, "fault": "restart_mid_fill", "at_s": 2057.7, "took_s": 10.5, "recovered_s": 0.1, "file": "wait/soak/big/b002.bin", "filled_before_restart_mib": 18, "restart_after_s": 2.5, "restart_t": 1793774453.161, "readers": {"v3": {"ok": true, "err": null}, "v41": {"ok": true, "err": null}}}`
- `{"n": 10, "fault": "kill_ganesha", "at_s": 2344.1, "took_s": 2.6, "recovered_s": 0.1, "old_pid": "88862", "new_pid": "90068", "restarted_s": 2.6, "kill_t": 1793774737.863}`
- `{"n": 11, "fault": "crash_postgres", "at_s": 2650.7, "took_s": 29.6, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 27.4, "start_rc": 0}`
- `{"n": 12, "fault": "kill_smbd", "at_s": 2914.9, "took_s": 17.9, "recovered_s": 0.0, "hold_s": 17.8, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 13, "fault": "crash_postgres", "at_s": 3177.4, "took_s": 7.5, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 5.3, "start_rc": 0}`

Violations (3):

- `{"kind": "lost_commit", "dt": 5144.6, "fault": "none", "key": "wait/soak/d4/f00172.bin", "why": "catalog CRC or size differs", "want": ["88a022be0a7970f4", 247351], "got": ["f751127d628b71a9", 1460118]}`
- `{"kind": "lost_commit", "dt": 5145.2, "fault": "none", "key": "wait/soak/d7/f00255.bin", "why": "not committed", "row": null, "local": true, "daemon_stopped": "kill_edge 2.7 s after the close"}`
- `{"kind": "lost_commit", "dt": 5145.6, "fault": "none", "key": "smbwait/soak/s00300.bin", "why": "not committed", "row": null, "local": true, "daemon_stopped": "restart_mid_fill 3.5 s after the close"}`

**RED**: 3 violations, every fault NOT injected enough.
