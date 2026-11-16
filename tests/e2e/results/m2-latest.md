# M2 exit test results

- When: 2026-10-06T03:19:33Z, 205 s
- Where: edge1, Ubuntu 26.04 LTS, kernel 7.0.0-28-generic, 4 CPUs
- Repository: HEAD 7b942f6f6828d5f1629dd521f05212de051efed3 (7b942f6)
- Test script: tests/e2e/m2_exit_test.sh sha256 2e4933c6b406ac31390a1adc5a8e12976db1ec9c62819906f28d3d7539cb4a18
- Daemon: omnifile-edge 0.0.0, sha256 451630fe1a7972b0
- Samba: Version 4.23.6-Ubuntu-4.23.6+dfsg-1ubuntu2.2; Ganesha: NFS-Ganesha Release = V15.6
- AD: realm OMNI.TEST (omnifile-dc); files 32 MiB, seeded pseudo-random; flood 2000 readers
- Result: **111 passed, 0 failed**

| | Check |
|---|---|
| PASS | winbind resolves the share groups and users (Proj-Owners S-1-5-21-3190051465-787089565-3716134449-1105, geo-reader S-1-5-21-3190051465-787089565-3716134449-1108) |
| PASS | enrolment: enrolled: edge_id=1 api_url=https://127.0.0.1:8443 identity=/var/lib/omnifile/identity |
| PASS | omnifile-edge active (Type=notify: READY only once the guard is armed) |
| PASS | the guard's fanotify group is in the unit's fd store (was 2) |
| PASS | guard armed on the data volume, private fill mount verified, bound to the daemon |
| PASS | daemon confined: CapBnd=000000001028001f (SYS_ADMIN, DAC_*, FOWNER, CHOWN, FSETID, SYS_PTRACE, LEASE; no KILL/SYS_MODULE/RAWIO/SETUID/SETGID/NET_ADMIN/MKNOD), NoNewPrivs=1, seccomp filter on (no @mount/@debug/@module/@raw-io) |
| PASS | share roots materialised (wait failfast smbwait smbfail krb) |
| PASS | AD-group ACL on the proven shares' roots (Owners rwx, Writers rwx, Readers r-x, default ACL) |
| PASS | every export's FSAL Policy comes from the catalog's on_open (failfast: Policy = "failfast") |
| PASS | smb.conf parses and loads no vfs_acl_xattr |
| PASS | nfs-ganesha-omni and smbd active |
| PASS | smbd and Ganesha BindsTo= the daemon |
| PASS | FSAL_OMNI connected |
| PASS | wrote smbfail/a.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote smbwait/a.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote smbwait/b.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote smbwait/kill.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote smbwait/hold.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote smbwait/flood.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote smbfail/nested.segy over SMB as geo-writer (smbclient, NTLMv2 against the DC) |
| PASS | wrote failfast/ff.segy over NFSv4.2 as geo-writer |
| PASS | wrote wait/w.segy over NFSv4.2 as geo-writer |
| PASS | wrote wait/hold.segy over NFSv4.2 as geo-writer |
| PASS | smbfail/a.segy committed; verified by the service (azure_read_crc) |
| PASS | smbwait/a.segy committed; verified by the service (azure_read_crc) |
| PASS | smbwait/b.segy committed; verified by the service (azure_read_crc) |
| PASS | smbwait/kill.segy committed; verified by the service (azure_read_crc) |
| PASS | smbwait/hold.segy committed; verified by the service (azure_read_crc) |
| PASS | smbwait/flood.segy committed; verified by the service (azure_read_crc) |
| PASS | smbfail/nested.segy committed; verified by the service (azure_read_crc) |
| PASS | failfast/ff.segy committed; verified by the service (azure_read_crc) |
| PASS | wait/w.segy committed; verified by the service (azure_read_crc) |
| PASS | wait/hold.segy committed; verified by the service (azure_read_crc) |
| PASS | SMB-written files carry the share's AD-group ACL, other::--- (normalised) |
| PASS | smbfail/a.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbwait/a.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbwait/b.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbwait/kill.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbwait/hold.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbwait/flood.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbfail/nested.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | failfast/ff.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | wait/w.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | wait/hold.segy stubbed (PARTIAL, 1052672 bytes resident) |
| PASS | smbfail/a.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | smbwait/a.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | smbwait/b.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | smbwait/flood.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | smbfail/nested.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | failfast/ff.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | wait/w.segy archived: marker ARCHIVED, not ONLINE (from the feed) |
| PASS | SMB read of archived smbfail/a.segy as geo-reader failed in 0.11 s: NT_STATUS_IO_DEVICE_ERROR (failfast) |
| PASS | recall of smbfail/a.segy attributed to geo-reader (S-1-5-21-3190051465-787089565-3716134449-1108), via smb, proven |
| PASS | a .RECALLING.txt note next to smbfail/a.segy |
| PASS | geo-none (no share group) is denied over SMB: NT_STATUS_ACCESS_DENIED |
| PASS | geo-none started no recall and is not a requester |
| PASS | CIFS mount of smbfail as geo-nested (sec=krb5, nested group Proj-Readers-Nested) |
| PASS | CIFS (krb5) read of archived smbfail/nested.segy as geo-nested: EIO after 0.13 s, no zeros |
| PASS | recall of smbfail/nested.segy attributed to geo-nested, via smb, proven |
| PASS | both SMB readers (smbclient, CIFS krb5) still waiting after 8 s (wait policy: the read is held, no error, no zeros) |
| PASS | recall of smbwait/a.segy attributed to geo-reader, via smb, proven |
| PASS | recall of smbwait/b.segy attributed to geo-reader (CIFS krb5), via smb, proven |
| PASS | smbclient get of smbwait/a.segy completed with the source's bytes after the fill |
| PASS | CIFS (krb5) read of smbwait/b.segy as geo-reader: byte-identical after the fill (8.561 s) |
| PASS | recall of smbwait/a.segy available (the edge reported the fill) |
| PASS | catalog says resident for smbwait/a.segy after the recall fill |
| PASS | before the edit: geo-none is not in smbwait/a.segy's catalog ACL |
| PASS | geo-writer (the file's owner: POSIX lets only the owner change an ACL) granted geo-none READ on smbwait/a.segy with smbcacls (Explorer's path) |
| PASS | the POSIX ACL now names uid 20003 (no security.NTACL) |
| PASS | the edge's ACL scan reported the edit: catalog ACL be58ecdac6c2 names geo-none (user, access, r-x) after 4 s; read_sids include it: yes |
| PASS | an acl-scan was recorded for the dataset |
| PASS | NFSv3 read of archived failfast/ff.segy: EIO in 0.12 s, no zeros |
| PASS | NFSv4.1 read of archived failfast/ff.segy: EIO in 0.12 s, no zeros |
| PASS | NFSv4.2 read of archived failfast/ff.segy: EIO in 0.12 s, no zeros |
| PASS | NFSv3, 4.1 and 4.2 readers of archived wait/w.segy all still waiting after 8 s |
| PASS | NFSv3 wait read of wait/w.segy byte-identical after the fill |
| PASS | NFSv4.1 wait read of wait/w.segy byte-identical after the fill |
| PASS | NFSv4.2 wait read of wait/w.segy byte-identical after the fill |
| PASS | the SMB readers' online fetch of smbwait/kill.segy started (smb-guard asked the daemon) |
| PASS | both SMB readers (CIFS krb5, smbclient) still waiting on the fill when the first kill lands |
| PASS | kill 1: daemon (with the guard) restarted, pid 7102 -> 9273; group reused from the fd store |
| PASS | kill 2: daemon (with the guard) restarted, pid 9273 -> 9364; group reused from the fd store |
| PASS | kill 3: daemon (with the guard) restarted, pid 9364 -> 9453; group reused from the fd store |
| PASS | smbd kept its pid 7308 through three daemon kills (RestartMode=direct; BindsTo not fired) |
| PASS | Ganesha kept its pid through the kills |
| PASS | CIFS reader across three daemon kills: byte-identical, no zero block (17.088 s) |
| PASS | smbclient reader across three daemon kills: byte-identical |
| PASS | journal: ORPHAN events from a killed instance were answered by the next (or none were pending) |
| PASS | after the online fetch the catalog says resident (files/{id}/filled) |
| PASS | daemon killed and held down (unit activating/start-pre, no daemon process) |
| PASS | smbd and Ganesha stayed up while the daemon was held down |
| PASS | 5 of 5 readers (CIFS, local, NFSv3, 4.1, 4.2) blocked for 12 s with no daemon (none read zeros) |
| PASS | daemon back after the hold (12 s held) |
| PASS | hold-cifs after the hold: byte-identical (12.461 s) |
| PASS | hold-local after the hold: byte-identical (12.46 s) |
| PASS | hold-nfs-3 after the hold: byte-identical (17.215 s) |
| PASS | hold-nfs-4.1 after the hold: byte-identical (12.405 s) |
| PASS | hold-nfs-4.2 after the hold: byte-identical (12.408 s) |
| PASS | NFSv4.2 mount of /krb with sec=krb5p (nfs/edge1.omni.test from the AD keytab) |
| PASS | geo-writer wrote krb/k.segy over NFS krb5p |
| PASS | geo-reader reads krb/k.segy over NFS krb5p: byte-identical |
| PASS | geo-none denied over NFS krb5p (EACCES) |
| PASS | krb/k.segy committed |
| PASS | krb/k.segy stubbed |
| PASS | recall of krb/k.segy attributed to geo-reader, via nfs, sec krb5p, proven |
| PASS | krb5p wait read of archived krb/k.segy: byte-identical after the fill |
| PASS | local readers' recall reported via=local (process on the edge), claimed |
| PASS | all 2000 readers held while the recall is pending |
| PASS | flood: 2000 of 2000 readers read the source's bytes, 0 zeros, 0 errors; drained 19 s after the recall moved on |
| PASS | no panic in the daemon journal |
| PASS | smbd still active, guard still armed |

## Notes

- **Recall progress is moved by hand.** The worker's restore (M3) is not exercised: the test sets the recall to `downloading` in the catalog, as the worker would once the restore completes. Everything after that (feed, read credential, fill through the private mount, `filled`, the held reads completing) is the product's.
- **Archive is the catalog's tier only.** The sim keeps serving the bytes; the marker's ARCHIVED flag and the recall flow are real.
