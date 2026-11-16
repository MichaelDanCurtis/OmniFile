# NFS grace reclaim: 2026-11-04T05:01:47Z

server 127.0.0.1 (clients reach it as 10.231.0.1, bridge), restart: `systemctl restart nfs-ganesha-omni`

Dev VM. The appliance's base config as committed; Ganesha restarted cleanly. (rev b07042a)

client kernel 7.0.0-42-generic, script 02eb8f8284bbefe0bc66770cbf7bc3eb68d20f6449adcbb7a89ae739094fe9bc; v4 /e2e/wait, v3 /srv/omnifile/data/wait; A holds 8 v4.1 locks, 4 read opens, 4 NLM locks (NLM on); A cut off 20s after the restart

| result | check |
|---|---|
| PASS | clients a, b, c up (10.231.0.11/24, 10.231.0.12/24, 10.231.0.13/24), each with its own rpcbind and rpc.statd |
| PASS | the server answers NFS from a client (rpcinfo -T tcp 10.231.0.1 nfs 4) |
| PASS | the server registers NLM (nlockmgr) with its rpcbind |
| PASS | each client mounts the share over NFSv4.1 and NFSv3 (vers=3,proto=tcp,hard,timeo=50) |
| PASS | A holds 8 NFSv4.1 opens with fcntl write locks and 4 NFSv3 (NLM) locks, and 4 read-only opens |
| PASS | before the restart, B is refused every one of A's 12 locks (EAGAIN) |
| PASS | the restart command succeeded (exit 0) |
| PASS | the server is back 23 s after the restart began |
| PASS | and registers NLM again |
| PASS | while A could not reclaim (20 s), B was granted none of A's locks (got 0) |
| PASS | A's 16 fds all work after the restart (pwrite+fsync+pread inside each lock; a lost lock gives EIO) |
| PASS | every in-grace attempt by B ended refused, none granted (answered 92 s after the server was back) |
| PASS | after grace, B is still refused every lock A reclaimed |
| PASS | once A unlocks, B is granted each of the 12 locks (the server's lock state is A's, not stuck) |
| PASS | rider v3-write (NFSv3, a new open, pwrite, fsync and read back each cycle) rode the restart: 488 cycles, no error, no wrong byte; longest cycle 21.1 s |
| PASS | rider v3-trunc (NFSv3, open(O_TRUNC), write, fsync, close and read back each cycle (a truncate: Ganesha answers JUKEBOX until grace ends)) rode the restart: 35 cycles, no error, no wrong byte; longest cycle 113.25 s |
| PASS | rider v41-held (NFSv4.1, one open fd held across the restart, pwrite, fsync and read back each cycle) rode the restart: 488 cycles, no error, no wrong byte; longest cycle 21.1 s |
| PASS | A reclaimed every NFSv4.1 open it held (12 of 12) |
| PASS | A reclaimed every NFSv4.1 lock it held (8 of 8) |
| PASS | B's in-grace LOCKs were answered GRACE by the server (96 times), never granted |
| PASS | A's first reclaim came after it rejoined (+43.2 s), after B's first GRACE answer (+23.2 s) |
| info | the server granted no delegation for A's 4 read-only opens, so there was none to reclaim (Ganesha grants them only where the FSAL supports leases: FSAL_VFS, and FSAL_OMNI over it, does not; and NFSV4 Delegations defaults to false) |
| PASS | the restarted server told A it had restarted (SM_NOTIFY reached A's statd 1 time(s)), so A's lockd reclaimed |
| PASS | no 'lockd: failed to reclaim' in the kernel log (0) |
| info | server log: 'NFS Server Now IN GRACE, duration 90' then 'NFS Server Now NOT IN GRACE' |

| count | value |
|---|---|
| rider v3-write: cycles / errors / longest cycle (s) | 488 / 0 / 21.1 |
| rider v3-trunc: cycles / errors / longest cycle (s) | 35 / 0 / 113.25 |
| rider v41-held: cycles / errors / longest cycle (s) | 488 / 0 / 21.1 |
| NFSv4.1 opens A held / reclaimed (nfs4_open_reclaim, error=0) | 12 / 12 |
| NFSv4.1 locks A held / lock states it reclaimed (nfs4_state_lock_reclaim) / reclaim LOCKs answered OK (nfs4_set_lock) | 8 / 8 / 8 |
| B's LOCK requests answered NFS4ERR_GRACE / NFS4ERR_DENIED after the restart | 96 / 16 |
| delegations the server granted A / A reclaimed | 0 / 0 |
| SM_NOTIFY from the server received by A's statd | 1 |
| B's NLM LOCK requests after the restart, final answer DENIED / GRANTED (nlmclnt_lock; the GRANTED ones after A let go) | 8 / 4 |
| server grace (its log): began / ended, seconds after the restart began; length | 21 / 111; 90 s |
| seconds after the restart began: server back / A rejoined / B's in-grace attempts answered | 23 / 43 / 115 |

23 passed, 0 failed, 2 notes
