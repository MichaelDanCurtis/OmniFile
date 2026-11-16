# Who made each namespace change, and hard links (namespace-sync.md)

- When: 2026-10-21T17:30:44Z, 72 s; edge1, kernel 7.0.0-38-generic
- Daemon: sha256 279cd895e85cdcba
- Result: **40 passed, 0 failed**

| | Check |
|---|---|
| PASS | winbind names geo-writer (S-1-5-21-3190051465-787089565-3716134449-1109): the DC is up |
| PASS | NFSv3 mkdir: nfs3-1792603772's create says via=nfs, uid 20002, geo-writer's SID, claimed (sec=sys) |
| PASS | NFSv3 rename: the same file (19) at nfs3-1792603772/g.txt |
| PASS | NFSv3 rename: via=nfs with geo-writer's SID |
| PASS | NFSv3 ln (hard link) answers not supported and links nothing (ln: failed to create hard link '/mnt/omnifile-ns/v3/nfs3-1792603772/g.txt' => '/mnt/omnifile-ns/v3/nfs3-1792603772/h.txt': Unknown error 524) |
| PASS | NFSv3 unlink: a tombstone with via=nfs and geo-writer's SID |
| PASS | NFSv3 rmdir: a tombstone with via=nfs and geo-writer's SID |
| PASS | NFSv4.2 mkdir: nfs42-1792603772's create says via=nfs, uid 20002, geo-writer's SID, claimed (sec=sys) |
| PASS | NFSv4.2 rename: the same file (21) at nfs42-1792603772/g.txt |
| PASS | NFSv4.2 rename: via=nfs with geo-writer's SID |
| PASS | NFSv4.2 ln (hard link) answers not supported and links nothing (ln: failed to create hard link '/mnt/omnifile-ns/v4.2/nfs42-1792603772/g.txt' => '/mnt/omnifile-ns/v4.2/nfs42-1792603772/h.txt': Unknown error 524) |
| PASS | NFSv4.2 unlink: a tombstone with via=nfs and geo-writer's SID |
| PASS | NFSv4.2 rmdir: a tombstone with via=nfs and geo-writer's SID |
| PASS | a local hard link to a local file is split: the new name has its own inode |
| PASS | and is committed as a separate catalog file (22 keeps the first name) |
| PASS | with the same bytes |
| PASS | the new S3 object is a placeholder with no local bytes |
| PASS | a local hard link to an S3-resident placeholder is removed (decision 42) |
| PASS | and the catalogued name keeps its inode, marker and file id (25), nothing tombstoned |
| PASS | SMB mkdir: via=smb with the session's SID (Try "help" to get a list of possible commands.) |
| PASS | SMB rename: smbd-1792603772-2 is the same folder (Try "help" to get a list of possible commands.) |
| PASS | SMB rename: via=smb with the session's SID |
| PASS | SMB rmdir (Try "help" to get a list of possible commands.) |
| PASS | SMB rmdir: a tombstone with via=smb and the session's SID |
| PASS | an SMB hard link to a local file is split into its own inode (Try "help" to get a list of possible commands.) |
| PASS | and committed as a separate catalog file |
| PASS | SMB delete (Try "help" to get a list of possible commands.) |
| PASS | SMB delete: a tombstone with via=smb and the session's SID |
| PASS | an SMB hard link to the S3-resident placeholder is removed (Try "help" to get a list of possible commands.) |
| PASS | and the catalogued name keeps its inode, marker and file id, nothing tombstoned |
| PASS | the placeholder whose links were removed still reads the S3 object's bytes over NFSv4.2 |
| PASS | omnifile-edge back after mv mvx-1792603772 mvy-1792603772; mkdir mvx-1792603772 |
| PASS | mv mvx-1792603772 mvy-1792603772 while the daemon was down: mvy-1792603772 is the old directory and its contents keep their file_ids |
| PASS | mkdir mvx-1792603772 (the old name reused): a new catalog directory |
| PASS | the moved directory's last version is a rename via=unknown, never a tombstone |
| PASS | the new mvx-1792603772 is created via=unknown |
| PASS | nothing under mvy-1792603772 was tombstoned (after 10 s more) |
| PASS | the S3-resident gl.segy's tombstone says via=nfs, geo-writer |
| PASS | the cutover report lists gl.segy, deleted by geo-writer's identity, proof claimed (sec=sys) |
| PASS | and that identity is geo-writer's SID |
