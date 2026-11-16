# NFS grace reclaim: 2026-11-04T05:06:05Z

server 127.0.0.1 (clients reach it as 10.231.0.1, bridge), restart: `systemctl restart nfs-ganesha-omni`

Dev VM. The appliance's base config with Enable_NLM = false (the comparison); Ganesha restarted cleanly; NFSv4 only. (rev b07042a)

client kernel 7.0.0-42-generic, script 02eb8f8284bbefe0bc66770cbf7bc3eb68d20f6449adcbb7a89ae739094fe9bc; v4 /e2e/wait, v3 /srv/omnifile/data/wait; A holds 8 v4.1 locks, 4 read opens, 4 NLM locks (NLM off); A cut off 20s after the restart

| result | check |
|---|---|
| PASS | clients a, b, c up (10.231.0.11/24, 10.231.0.12/24, 10.231.0.13/24), each with its own rpcbind and rpc.statd |
| PASS | the server answers NFS from a client (rpcinfo -T tcp 10.231.0.1 nfs 4) |
| PASS | each client mounts the share over NFSv4.1 and NFSv3 (vers=3,proto=tcp,hard,timeo=50,nolock) |
| PASS | A holds 8 NFSv4.1 opens with fcntl write locks and 0 NFSv3 (NLM) locks, and 4 read-only opens |
| PASS | before the restart, B is refused every one of A's 8 locks (EAGAIN) |
| PASS | the restart command succeeded (exit 0) |
| PASS | the server is back 2 s after the restart began |
| PASS | while A could not reclaim (20 s), B was granted none of A's locks (got 0) |
| PASS | A's 12 fds all work after the restart (pwrite+fsync+pread inside each lock; a lost lock gives EIO) |
| PASS | every in-grace attempt by B ended refused, none granted (answered 42 s after the server was back) |
| PASS | after grace, B is still refused every lock A reclaimed |
| PASS | once A unlocks, B is granted each of the 8 locks (the server's lock state is A's, not stuck) |
| PASS | rider v3-write (NFSv3, a new open, pwrite, fsync and read back each cycle) rode the restart: 224 cycles, no error, no wrong byte; longest cycle 3.03 s |
| PASS | rider v3-trunc (NFSv3, open(O_TRUNC), write, fsync, close and read back each cycle (a truncate: Ganesha answers JUKEBOX until grace ends)) rode the restart: 73 cycles, no error, no wrong byte; longest cycle 33.74 s |
| PASS | rider v41-held (NFSv4.1, one open fd held across the restart, pwrite, fsync and read back each cycle) rode the restart: 224 cycles, no error, no wrong byte; longest cycle 3.03 s |
| PASS | A reclaimed every NFSv4.1 open it held (12 of 12) |
| PASS | A reclaimed every NFSv4.1 lock it held (8 of 8) |
| PASS | B's in-grace LOCKs were answered GRACE by the server (72 times), never granted |
| PASS | A's first reclaim came after it rejoined (+22.2 s), after B's first GRACE answer (+2.2 s) |
| info | the server granted no delegation for A's 4 read-only opens, so there was none to reclaim (Ganesha grants them only where the FSAL supports leases: FSAL_VFS, and FSAL_OMNI over it, does not; and NFSV4 Delegations defaults to false) |
| info | server log: 'NFS Server Now IN GRACE, duration 90' then 'NFS Server Now NOT IN GRACE' |

| count | value |
|---|---|
| rider v3-write: cycles / errors / longest cycle (s) | 224 / 0 / 3.03 |
| rider v3-trunc: cycles / errors / longest cycle (s) | 73 / 0 / 33.74 |
| rider v41-held: cycles / errors / longest cycle (s) | 224 / 0 / 3.03 |
| NFSv4.1 opens A held / reclaimed (nfs4_open_reclaim, error=0) | 12 / 12 |
| NFSv4.1 locks A held / lock states it reclaimed (nfs4_state_lock_reclaim) / reclaim LOCKs answered OK (nfs4_set_lock) | 8 / 8 / 8 |
| B's LOCK requests answered NFS4ERR_GRACE / NFS4ERR_DENIED after the restart | 72 / 16 |
| delegations the server granted A / A reclaimed | 0 / 0 |
| server grace (its log): began / ended, seconds after the restart began; length | 0 / 30; 30 s |
| seconds after the restart began: server back / A rejoined / B's in-grace attempts answered | 2 / 22 / 44 |

19 passed, 0 failed, 2 notes
