# Chaos soak: 2026-11-04T15:35:21Z

seed **3125631393**, 22500 s planned, ran 24320 s; 96 faults planned (12 of each); label: 6h-wp9

Replay: `sudo tests/chaos/soak.sh --seed 3125631393 --duration 22500 --each 12`

| fault | injected | skipped | new main pid after (s, max) | every unit active after (s, max) |
|---|---|---|---|---|
| kill_edge | 12 | 0 | 1.3 | 0.0 |
| kill_ganesha | 12 | 0 | 2.4 | 0.0 |
| kill_smbd | 12 | 0 | - | 0.0 |
| kill_worker | 12 | 0 | 2.1 | 0.0 |
| crash_postgres | 12 | 0 | - | 0.0 |
| fill_disk | 12 | 0 | - | 0.0 |
| drop_network | 12 | 0 | - | 0.0 |
| restart_mid_fill | 8 | 4 | - | 0.0 |

| invariant | violations |
|---|---|
| zeros | 0 |
| wrong_bytes | 0 |
| enoent | 0 |
| lost_commit | 0 |
| recall | 4 |
| recovery | 0 |

Of the lost_commit violations, 0 had the edge daemon killed or restarted within the commit quiet period after the file's close (the detector gap: `edge/crates/omnifile-edge/src/nsync.rs` reconcile_dir leaves a new regular file to the detector, whose close event dies with the process).

| load | count |
|---|---|
| archive_error | 4 |
| archived | 468 |
| catalog_sync_error | 15 |
| delete_ok:nfs-v3 | 269 |
| delete_ok:nfs-v41 | 273 |
| delete_ok:nfs-v42 | 276 |
| list_err:smb | 4 |
| listings | 2634 |
| read_err:smb:NT_STATUS_CONNECTION_DISCONNECTED | 1 |
| read_err:smb:NT_STATUS_CONNECTION_REFUSED | 48 |
| read_err:smb:NT_STATUS_IO_DEVICE_ERROR | 16 |
| read_err:v3:EIO | 22 |
| read_err:v41:EIO | 17 |
| read_err:v42:EIO | 18 |
| read_ok:smb:archived | 2378 |
| read_ok:smb:filled | 4383 |
| read_ok:smb:unfilled | 363 |
| read_ok:v3:archived | 1575 |
| read_ok:v3:filled | 3684 |
| read_ok:v3:unfilled | 639 |
| read_ok:v41:archived | 1631 |
| read_ok:v41:filled | 3622 |
| read_ok:v41:unfilled | 736 |
| read_ok:v42:archived | 1606 |
| read_ok:v42:filled | 3684 |
| read_ok:v42:unfilled | 629 |
| recalls_seen | 613 |
| write_err:nfs-v3:ENOSPC | 6 |
| write_err:nfs-v41:ENOSPC | 8 |
| write_err:nfs-v42:ENOSPC | 5 |
| write_err:smb:NT_STATUS_CONNECTION_REFUSED | 4 |
| write_err:smb:NT_STATUS_DISK_FULL | 4 |
| write_ok:nfs-v3 | 541 |
| write_ok:nfs-v41 | 486 |
| write_ok:nfs-v42 | 540 |
| write_ok:smb | 250 |

| at the end | count |
|---|---|
| committed_and_hashed | 322 |
| deleted_stays_deleted | 989 |
| excluded:failed | 9 |
| placeholder_present | 322 |
| recall:available | 609 |
| recall:downloading | 4 |

settle: `{"settled_s": null, "pending": [], "n_pending": 0, "active_recalls": [235, 273, 274, 598]}`

Errors by the fault active when they happened:

- drop_network:read_err:v41:EIO: 1
- fill_disk:read_err:smb:NT_STATUS_IO_DEVICE_ERROR: 8
- fill_disk:read_err:v3:EIO: 9
- fill_disk:read_err:v41:EIO: 5
- fill_disk:read_err:v42:EIO: 11
- fill_disk:write_err:ENOSPC: 19
- fill_disk:write_err:NT_STATUS_DISK_FULL: 4
- kill_edge:read_err:smb:NT_STATUS_IO_DEVICE_ERROR: 1
- kill_ganesha:read_err:smb:NT_STATUS_IO_DEVICE_ERROR: 1
- kill_smbd:read_err:smb:NT_STATUS_CONNECTION_REFUSED: 45
- kill_smbd:write_err:NT_STATUS_CONNECTION_REFUSED: 4
- none:read_err:smb:NT_STATUS_IO_DEVICE_ERROR: 6
- none:read_err:v3:EIO: 13
- none:read_err:v41:EIO: 11
- none:read_err:v42:EIO: 7
- restart_mid_fill:read_err:smb:NT_STATUS_CONNECTION_DISCONNECTED: 1
- restart_mid_fill:read_err:smb:NT_STATUS_CONNECTION_REFUSED: 3

Faults:

- `{"n": 1, "fault": "kill_ganesha", "at_s": 300.7, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "43263", "new_pid": "45934", "restarted_s": 2.1, "kill_t": 1793782502.401}`
- `{"n": 2, "fault": "kill_ganesha", "at_s": 519.3, "took_s": 2.3, "recovered_s": 0.0, "old_pid": "45934", "new_pid": "46851", "restarted_s": 2.3, "kill_t": 1793782721.061}`
- `{"n": 3, "fault": "kill_ganesha", "at_s": 681.0, "took_s": 2.3, "recovered_s": 0.0, "old_pid": "46851", "new_pid": "47571", "restarted_s": 2.3, "kill_t": 1793782882.728}`
- `{"n": 4, "fault": "kill_ganesha", "at_s": 951.0, "took_s": 2.3, "recovered_s": 0.0, "old_pid": "47571", "new_pid": "48817", "restarted_s": 2.3, "kill_t": 1793783152.767}`
- `{"n": 5, "fault": "kill_edge", "at_s": 1106.8, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "43015", "new_pid": "49462", "restarted_s": 1.3, "kill_t": 1793783308.488}`
- `{"n": 6, "fault": "crash_postgres", "at_s": 1367.6, "took_s": 29.3, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 27.1, "start_rc": 0}`
- `{"n": 7, "fault": "crash_postgres", "at_s": 1555.4, "took_s": 10.7, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 8.6, "start_rc": 0}`
- `{"n": 8, "fault": "restart_mid_fill", "at_s": 1721.7, "took_s": 9.6, "recovered_s": 0.0, "file": "wait/soak/big/b003.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.8, "restart_t": 1793783924.826, "readers": {"v41": {"ok": true, "err": null}, "v3": {"ok": true, "err": null}}}`
- `{"n": 9, "fault": "kill_edge", "at_s": 2010.9, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "52327", "new_pid": "53656", "restarted_s": 1.3, "kill_t": 1793784212.624}`
- `{"n": 10, "fault": "kill_edge", "at_s": 2303.2, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "53656", "new_pid": "55058", "restarted_s": 1.3, "kill_t": 1793784504.966}`
- `{"n": 11, "fault": "kill_ganesha", "at_s": 2466.9, "took_s": 2.3, "recovered_s": 0.0, "old_pid": "52380", "new_pid": "55918", "restarted_s": 2.3, "kill_t": 1793784668.618}`
- `{"n": 12, "fault": "kill_worker", "at_s": 2601.7, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "43258", "new_pid": "56491", "restarted_s": 2.1, "kill_t": 1793784803.44}`
- `{"n": 13, "fault": "crash_postgres", "at_s": 2886.7, "took_s": 11.2, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 9.1, "start_rc": 0}`
- `{"n": 14, "fault": "fill_disk", "at_s": 3160.0, "took_s": 49.1, "recovered_s": 0.0, "hold_s": 49.1, "filled_gib": 62.23, "left_mib": 2}`
- `{"n": 15, "fault": "kill_edge", "at_s": 3374.9, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "55058", "new_pid": "60049", "restarted_s": 1.3, "kill_t": 1793785576.603}`
- `{"n": 16, "fault": "kill_smbd", "at_s": 3679.4, "took_s": 11.9, "recovered_s": 0.0, "hold_s": 11.8, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 17, "fault": "drop_network", "at_s": 3810.8, "took_s": 53.3, "recovered_s": 0.0, "hold_s": 53.2, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 18, "fault": "crash_postgres", "at_s": 4110.1, "took_s": 0.0, "recovered_s": 0.0, "mode": "one backend SIGKILL (Postgres's own crash restart)", "pid": "57858"}`
- `{"n": 19, "fault": "restart_mid_fill", "at_s": 4397.4, "took_s": 9.6, "recovered_s": 0.0, "file": "wait/soak/big/b001.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.8, "restart_t": 1793786600.565, "readers": {"v3": {"ok": true, "err": null}, "v41": {"ok": true, "err": null}}}`
- `{"n": 20, "fault": "restart_mid_fill", "at_s": 4670.1, "took_s": 9.6, "recovered_s": 0.0, "file": "wait/soak/big/b003.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.8, "restart_t": 1793786873.287, "readers": {"v3": {"ok": true, "err": null}, "v41": {"ok": true, "err": null}}}`
- `{"n": 21, "fault": "kill_worker", "at_s": 4808.4, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "56491", "new_pid": "66691", "restarted_s": 2.1, "kill_t": 1793787010.112}`
- `{"n": 22, "fault": "drop_network", "at_s": 5084.8, "took_s": 52.0, "recovered_s": 0.0, "hold_s": 51.9, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 23, "fault": "drop_network", "at_s": 5328.6, "took_s": 107.3, "recovered_s": 0.0, "hold_s": 53.7, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 24, "fault": "fill_disk", "at_s": 5491.2, "took_s": 75.9, "recovered_s": 0.0, "hold_s": 75.9, "filled_gib": 62.09, "left_mib": 2}`
- `{"n": 25, "fault": "drop_network", "at_s": 5692.7, "took_s": 53.8, "recovered_s": 0.0, "hold_s": 53.8, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 26, "fault": "fill_disk", "at_s": 5942.4, "took_s": 53.3, "recovered_s": 0.0, "hold_s": 53.3, "filled_gib": 62.1, "left_mib": 2}`
- `{"n": 27, "fault": "kill_worker", "at_s": 6217.6, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "66691", "new_pid": "72881", "restarted_s": 2.1, "kill_t": 1793788419.366}`
- `{"n": 28, "fault": "restart_mid_fill", "at_s": 6498.9, "took_s": 9.9, "recovered_s": 0.0, "file": "wait/soak/big/b004.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.8, "restart_t": 1793788702.045, "readers": {"v41": {"ok": true, "err": null}, "v3": {"ok": true, "err": null}}}`
- `{"n": 29, "fault": "restart_mid_fill", "at_s": 6759.9, "took_s": 10.0, "recovered_s": 0.0, "file": "wait/soak/big/b004.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.9, "restart_t": 1793788963.055, "readers": {"v41": {"ok": true, "err": null}, "v3": {"ok": true, "err": null}}}`
- `{"n": 30, "fault": "fill_disk", "at_s": 7024.2, "took_s": 75.7, "recovered_s": 0.0, "hold_s": 75.7, "filled_gib": 62.0, "left_mib": 2}`
- `{"n": 31, "fault": "fill_disk", "at_s": 7318.8, "took_s": 69.9, "recovered_s": 0.0, "hold_s": 69.8, "filled_gib": 62.02, "left_mib": 2}`
- `{"n": 32, "fault": "crash_postgres", "at_s": 7608.6, "took_s": 27.2, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 25.0, "start_rc": 0}`
- `{"n": 33, "fault": "drop_network", "at_s": 7811.6, "took_s": 94.5, "recovered_s": 0.0, "hold_s": 43.3, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 34, "fault": "kill_ganesha", "at_s": 7960.4, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "75453", "new_pid": "80649", "restarted_s": 2.1, "kill_t": 1793790162.114}`
- `{"n": 35, "fault": "kill_worker", "at_s": 8134.3, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "72881", "new_pid": "81437", "restarted_s": 2.1, "kill_t": 1793790336.009}`
- `{"n": 36, "fault": "drop_network", "at_s": 8298.1, "took_s": 28.4, "recovered_s": 0.0, "hold_s": 28.4, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 37, "fault": "crash_postgres", "at_s": 8587.6, "took_s": 16.4, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 14.2, "start_rc": 0}`
- `{"n": 38, "fault": "kill_edge", "at_s": 8722.6, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "75394", "new_pid": "84039", "restarted_s": 1.3, "kill_t": 1793790924.304}`
- `{"n": 39, "fault": "restart_mid_fill", "at_s": 8877.5, "took_s": 9.6, "recovered_s": 0.0, "file": "wait/soak/big/b001.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.8, "restart_t": 1793791080.685, "readers": {"v3": {"ok": true, "err": null}, "v41": {"ok": true, "err": null}}}`
- `{"n": 40, "fault": "crash_postgres", "at_s": 9178.4, "took_s": 0.0, "recovered_s": 0.0, "mode": "one backend SIGKILL (Postgres's own crash restart)", "pid": "83485"}`
- `{"n": 41, "fault": "kill_ganesha", "at_s": 9458.6, "took_s": 2.4, "recovered_s": 0.0, "old_pid": "84863", "new_pid": "87443", "restarted_s": 2.4, "kill_t": 1793791660.305}`
- `{"n": 42, "fault": "fill_disk", "at_s": 9751.2, "took_s": 35.2, "recovered_s": 0.0, "hold_s": 35.2, "filled_gib": 61.87, "left_mib": 2}`
- `{"n": 43, "fault": "kill_edge", "at_s": 9945.1, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "84811", "new_pid": "89629", "restarted_s": 1.3, "kill_t": 1793792146.797}`
- `{"n": 44, "fault": "drop_network", "at_s": 10199.4, "took_s": 49.2, "recovered_s": 0.0, "hold_s": 23.8, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 45, "fault": "kill_smbd", "at_s": 10379.2, "took_s": 14.2, "recovered_s": 0.0, "hold_s": 14.2, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 46, "fault": "kill_smbd", "at_s": 10651.5, "took_s": 21.8, "recovered_s": 0.0, "hold_s": 21.7, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 47, "fault": "kill_worker", "at_s": 10895.2, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "81437", "new_pid": "93953", "restarted_s": 2.1, "kill_t": 1793793096.982}`
- `{"n": 48, "fault": "kill_worker", "at_s": 11122.4, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "93953", "new_pid": "94988", "restarted_s": 2.1, "kill_t": 1793793324.176}`
- `{"n": 49, "fault": "crash_postgres", "at_s": 11389.7, "took_s": 11.3, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 9.1, "start_rc": 0}`
- `{"n": 50, "fault": "restart_mid_fill", "at_s": 11620.9, "took_s": 9.7, "recovered_s": 0.0, "file": "wait/soak/big/b002.bin", "filled_before_restart_mib": 18, "restart_after_s": 1.8, "restart_t": 1793793824.052, "readers": {"v41": {"ok": true, "err": null}, "v3": {"ok": true, "err": null}}}`
- `{"n": 51, "fault": "kill_smbd", "at_s": 11860.6, "took_s": 22.5, "recovered_s": 0.0, "hold_s": 22.4, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 52, "fault": "kill_ganesha", "at_s": 12056.0, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "97318", "new_pid": "99275", "restarted_s": 2.1, "kill_t": 1793794257.687}`
- `{"n": 53, "fault": "kill_edge", "at_s": 12275.0, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "97266", "new_pid": "100205", "restarted_s": 1.3, "kill_t": 1793794476.721}`
- `{"n": 54, "fault": "fill_disk", "at_s": 12564.6, "took_s": 56.3, "recovered_s": 0.0, "hold_s": 56.3, "filled_gib": 61.85, "left_mib": 2}`
- `{"n": 55, "fault": "drop_network", "at_s": 12860.1, "took_s": 37.7, "recovered_s": 0.0, "hold_s": 37.7, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 56, "fault": "crash_postgres", "at_s": 13107.7, "took_s": 15.9, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 13.7, "start_rc": 0}`
- `{"n": 57, "fault": "drop_network", "at_s": 13286.3, "took_s": 25.8, "recovered_s": 0.0, "hold_s": 25.8, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 58, "fault": "kill_ganesha", "at_s": 13550.2, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "99275", "new_pid": "105879", "restarted_s": 2.1, "kill_t": 1793795751.916}`
- `{"n": 59, "fault": "kill_edge", "at_s": 13748.2, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "100205", "new_pid": "106728", "restarted_s": 1.3, "kill_t": 1793795949.947}`
- `{"n": 60, "fault": "fill_disk", "at_s": 14051.8, "took_s": 84.8, "recovered_s": 0.0, "hold_s": 84.8, "filled_gib": 61.85, "left_mib": 2}`
- `{"n": 61, "fault": "fill_disk", "at_s": 14265.2, "took_s": 89.6, "recovered_s": 0.0, "hold_s": 89.6, "filled_gib": 61.85, "left_mib": 2}`
- `{"n": 62, "fault": "fill_disk", "at_s": 14479.4, "took_s": 81.7, "recovered_s": 0.0, "hold_s": 81.7, "filled_gib": 61.86, "left_mib": 2}`
- `{"n": 63, "fault": "kill_ganesha", "at_s": 14748.8, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "105879", "new_pid": "111254", "restarted_s": 2.1, "kill_t": 1793796950.566}`
- `{"n": 64, "fault": "kill_edge", "at_s": 14902.6, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "106728", "new_pid": "111905", "restarted_s": 1.3, "kill_t": 1793797104.343}`
- `{"n": 65, "fault": "kill_smbd", "at_s": 15169.9, "took_s": 25.0, "recovered_s": 0.0, "hold_s": 24.9, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 66, "fault": "kill_worker", "at_s": 15347.6, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "94988", "new_pid": "113980", "restarted_s": 2.1, "kill_t": 1793797549.33}`
- `{"n": 67, "fault": "drop_network", "at_s": 15644.0, "took_s": 91.0, "recovered_s": 0.0, "hold_s": 49.7, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 68, "fault": "kill_worker", "at_s": 15815.5, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "113980", "new_pid": "116021", "restarted_s": 2.1, "kill_t": 1793798017.21}`
- `{"n": 69, "fault": "restart_mid_fill", "at_s": 16019.5, "took_s": 9.5, "recovered_s": 0.0, "file": "wait/soak/big/b002.bin", "filled_before_restart_mib": 18, "restart_after_s": 2.0, "restart_t": 1793798222.62, "readers": {"v41": {"ok": true, "err": null}, "v3": {"ok": true, "err": null}}}`
- `{"n": 70, "fault": "kill_ganesha", "at_s": 16294.3, "took_s": 2.3, "recovered_s": 0.0, "old_pid": "117039", "new_pid": "118251", "restarted_s": 2.3, "kill_t": 1793798496.025}`
- `{"n": 71, "fault": "crash_postgres", "at_s": 16573.9, "took_s": 0.0, "recovered_s": 0.0, "mode": "one backend SIGKILL (Postgres's own crash restart)", "pid": "116043"}`
- `{"n": 72, "fault": "kill_smbd", "at_s": 16706.5, "took_s": 15.3, "recovered_s": 0.0, "hold_s": 15.2, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 73, "fault": "kill_edge", "at_s": 16886.6, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "116986", "new_pid": "120891", "restarted_s": 1.3, "kill_t": 1793799088.341}`
- `{"n": 74, "fault": "restart_mid_fill", "at_s": 17124.8, "took_s": 300.2, "recovered_s": 0.0, "skipped": "no big stub ready in 300 s"}`
- `{"n": 75, "fault": "crash_postgres", "at_s": 17425.1, "took_s": 31.6, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 29.4, "start_rc": 0}`
- `{"n": 76, "fault": "fill_disk", "at_s": 17590.3, "took_s": 63.7, "recovered_s": 0.0, "hold_s": 63.6, "filled_gib": 61.8, "left_mib": 2}`
- `{"n": 77, "fault": "kill_smbd", "at_s": 17852.8, "took_s": 22.9, "recovered_s": 0.0, "hold_s": 22.8, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 78, "fault": "restart_mid_fill", "at_s": 18072.6, "took_s": 300.3, "recovered_s": 0.0, "skipped": "no big stub ready in 300 s"}`
- `{"n": 79, "fault": "kill_worker", "at_s": 18372.9, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "116021", "new_pid": "127459", "restarted_s": 2.1, "kill_t": 1793800574.639}`
- `{"n": 80, "fault": "restart_mid_fill", "at_s": 18381.1, "took_s": 300.3, "recovered_s": 0.0, "skipped": "no big stub ready in 300 s"}`
- `{"n": 81, "fault": "drop_network", "at_s": 18681.4, "took_s": 93.9, "recovered_s": 0.0, "hold_s": 41.3, "forgotten_flows": true, "cgroup": "system.slice/omnifile-edge.service"}`
- `{"n": 82, "fault": "kill_worker", "at_s": 18775.4, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "127459", "new_pid": "129253", "restarted_s": 2.1, "kill_t": 1793800977.116}`
- `{"n": 83, "fault": "kill_worker", "at_s": 19014.6, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "129253", "new_pid": "130378", "restarted_s": 2.1, "kill_t": 1793801216.318}`
- `{"n": 84, "fault": "kill_smbd", "at_s": 19224.7, "took_s": 10.0, "recovered_s": 0.0, "hold_s": 9.9, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 85, "fault": "kill_worker", "at_s": 19520.7, "took_s": 2.2, "recovered_s": 0.0, "old_pid": "130378", "new_pid": "132650", "restarted_s": 2.1, "kill_t": 1793801722.462}`
- `{"n": 86, "fault": "kill_smbd", "at_s": 19713.8, "took_s": 13.3, "recovered_s": 0.0, "hold_s": 13.2, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 87, "fault": "fill_disk", "at_s": 20007.3, "took_s": 69.4, "recovered_s": 0.0, "hold_s": 69.4, "filled_gib": 61.83, "left_mib": 2}`
- `{"n": 88, "fault": "kill_ganesha", "at_s": 20254.5, "took_s": 2.1, "recovered_s": 0.0, "old_pid": "118251", "new_pid": "136004", "restarted_s": 2.1, "kill_t": 1793802456.248}`
- `{"n": 89, "fault": "kill_smbd", "at_s": 20498.5, "took_s": 23.4, "recovered_s": 0.0, "hold_s": 23.3, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 90, "fault": "kill_edge", "at_s": 20732.9, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "120891", "new_pid": "138060", "restarted_s": 1.3, "kill_t": 1793802934.614}`
- `{"n": 91, "fault": "crash_postgres", "at_s": 20882.8, "took_s": 21.1, "recovered_s": 0.0, "mode": "cluster SIGKILL, started again", "hold_s": 18.9, "start_rc": 0}`
- `{"n": 92, "fault": "restart_mid_fill", "at_s": 21174.0, "took_s": 300.2, "recovered_s": 0.0, "skipped": "no big stub ready in 300 s"}`
- `{"n": 93, "fault": "kill_edge", "at_s": 21474.3, "took_s": 1.3, "recovered_s": 0.0, "old_pid": "138060", "new_pid": "141538", "restarted_s": 1.3, "kill_t": 1793803676.006}`
- `{"n": 94, "fault": "kill_smbd", "at_s": 21767.7, "took_s": 7.6, "recovered_s": 0.0, "hold_s": 7.5, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 95, "fault": "kill_smbd", "at_s": 21949.0, "took_s": 20.6, "recovered_s": 0.0, "hold_s": 20.5, "restarted_by": "the soak (smbd.service has no Restart=)", "start_rc": 0}`
- `{"n": 96, "fault": "drop_network", "at_s": 22091.1, "took_s": 60.0, "recovered_s": 0.0, "hold_s": 59.9, "forgotten_flows": false, "cgroup": "system.slice/omnifile-edge.service"}`

Violations (4):

- `{"kind": "recall", "dt": 24319.8, "fault": "none", "recall_id": 235, "why": "still downloading"}`
- `{"kind": "recall", "dt": 24319.8, "fault": "none", "recall_id": 273, "why": "still downloading"}`
- `{"kind": "recall", "dt": 24319.8, "fault": "none", "recall_id": 274, "why": "still downloading"}`
- `{"kind": "recall", "dt": 24319.8, "fault": "none", "recall_id": 598, "why": "still downloading"}`

**RED**: 4 violations, every fault NOT injected enough.
