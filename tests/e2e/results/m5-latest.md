# M5 exit test (NFS half): 2026-10-21T20:46:37Z

rev 6d0b46e, kernel 7.0.0-38-generic, script 4425de5d83de127f92d74b444cf2c8fc18e92b6a14ed1dc192491b514b9fe996

daemon 279cd895e85cdcba, FSAL_OMNI e33480513b323cb9, Version 4.23.6-Ubuntu-4.23.6+dfsg-1ubuntu2.2

| result | check |
|---|---|
| PASS | omnifile-edge.service active |
| PASS | share root materialised from the feed |
| PASS | Ganesha up, FSAL_OMNI connected to the daemon |
| PASS | the worker seeds the source from a listing |
| PASS | std.segy in the catalog at the share root, its primary copy on S3 (backend 2) |
| PASS | std.segy: one copy, the S3 object (nothing in Azure) |
| PASS | gl.segy in the catalog at the share root, its primary copy on S3 (backend 2) |
| PASS | gl.segy: one copy, the S3 object (nothing in Azure) |
| PASS | both materialised as placeholders |
| PASS | std.segy: full size, a stub marker, no data on disk |
| PASS | NFSv3 lists both ({"ok": true, "names": ["gl.segy", "std.segy"]}) |
| PASS | NFSv4 lists both ({"ok": true, "names": ["gl.segy", "std.segy"]}) |
| PASS | NFSv3 read: the S3 object's bytes, byte-identical, no zero blocks |
| PASS | the first read returned them (no error answer before) |
| PASS | NFSv4 read: the same bytes |
| PASS | an online object needs no restore: no recall |
| PASS | std.segy is a whole local copy: the object's CRC-64/NVME |
| PASS | the catalog saw no upload: the primary copy is still the S3 object |
| PASS | after the read, still one copy, on S3, with the CRC S3 stores for it |
| PASS | gl.segy (archived): NFSv3 says 'not yet' (JUKEBOX), not ENOENT |
| PASS | an S3 restore started (recall rehydrating) |
| PASS | NFSv4 says 'not yet' (DELAY) while it restores |
| PASS | after the restore, NFSv3 reads gl.segy's bytes |
| PASS | and NFSv4 |
| PASS | deep/d.txt committed |
| PASS | the edge made today's label (2026-10-21T0000Z), unfilled |
| PASS | the rendered stanza serves the rewind area (shadow:snapdir) |
| PASS | smbd answers |
| PASS | the previous version's folder lists both files |
| PASS | a placeholder in the label is offline to SMB clients (attributes Ao) |
| PASS | and a filled file is not (std.segy: A) |
| PASS | the label was filled for that open (no longer unfilled) |
| PASS | by the smb-guard, for smbd's open |
| PASS | a file read from the previous version has its bytes |
| PASS | yesterday's label (2026-10-20T0000Z) is unfilled |
| PASS | without the module, opening it directly is NOT_FOUND (the gap) |
| PASS | and the label stays unfilled |
| PASS | omniacl names the installed module before shadow_copy2 |
| PASS | smbd answers (Ubuntu's smbd, with the module) |
| PASS | opened directly, the file has its bytes from that version |
| PASS | the label was filled for it |
| PASS | and its subfolder deep |
| PASS | by the smb-guard, for smbd's opens (2 fills) |
| PASS | omnifile-edge.service active with rendering on |
| PASS | the seeded share is rendered by the daemon |
| PASS | Ganesha up with the daemon's exports |
| PASS | the rendered export serves the seeded share (v4) |
| PASS | a new share is rendered with its export id |
| PASS | the daemon reloaded Ganesha for it |
| PASS | Ganesha serves the new share over NFSv4 |
| PASS | and over NFSv3 |
| PASS | the seeded share kept serving through the reload |
| PASS | a removed share is no longer rendered |
| PASS | Ganesha no longer serves it |
| PASS | its folder stays on the edge |
| PASS | the seeded share still serves |
| PASS | Ganesha stayed up through the reloads |
| PASS | ns/a.txt committed |
| PASS | NFS rename ({"ok": true}): the catalog has ns/b.txt, the same file (9) |
| PASS | and no ns/a.txt |
| PASS | NFS move to another folder ({"ok": true}): ns2/b.txt, the same file |
| PASS | a folder renamed ({"ok": true}): ns3/b.txt, the same file |
| PASS | and no ns2 |
| PASS | a local chown: the catalog has its new owner |
| PASS | NFS unlink ({"ok": true}): ns3/b.txt is deleted in the catalog |
| PASS | NFS rmdir ({"ok": true}): ns3 is deleted too |
| PASS | ns/c.txt committed |
| PASS | a folder removed with its file from a shell: both deleted in the catalog |
| PASS | nothing deleted comes back on the edge (the tombstones' echoes are skipped) |
| PASS | every namespace op acknowledged: none queued, sent or refused |
| PASS | the worker seeds the second source |
| PASS | chaos/c.segy is a placeholder on the edge |
| PASS | the control API is down |
| PASS | while the API is down, every read answers "not yet" (never bytes, ENOENT or another error) |
| PASS | and the placeholder holds no data (nothing was written) |
| PASS | the control API is back |
| PASS | once it is back, the same read returns the object's bytes, no zero blocks |
| PASS | and over NFSv4 |
| PASS | the edge daemon stayed up through the outage |
| PASS | nft is installed |
| PASS | chaos/n.segy is a placeholder on the edge |
| PASS | the edge's packets to the API and the sim are dropped |
| PASS | the API still answers others (only the edge is cut off) |
| PASS | a write over NFS succeeds while the link is down |
| PASS | while the link is down, every read answers "not yet" (never bytes, ENOENT or another error) |
| PASS | and every NFS call answers within Park_Ms + 2 s (nothing waits on a hung connection) |
| PASS | and the placeholder holds no data |
| PASS | and nl-w.txt is not committed yet (nothing reached the service) |
| PASS | the outage held some of the edge's flows (the firewall has something to forget) |
| PASS | only the flows from the outage are still dropped |
| PASS | once the link is back, the same read returns the object's bytes, no zero blocks (32 s) |
| PASS | the file written meanwhile is uploaded and committed (32 s after) |
| PASS | a catalog change made after the outage still reaches the edge (the feed resumed) |
| PASS | the edge daemon was never restarted (same process throughout) |
| PASS | chaos/k.segy is a placeholder on the edge |
| PASS | k.segy (archived): the readers started an S3 restore (recall rehydrating) |
| PASS | the restore completed and the fill from S3 recorded its first 64 MiB |
| PASS | unverified bytes are never readable: 64 MiB on disk, the marker still says 0 resident |
| PASS | systemd restarted the daemon |
| PASS | Ganesha stayed up through the daemon kill (same pid 152506) |
| PASS | NFSv3 reader across the kill: the object's bytes, byte-identical, no zero block |
| PASS | and every answer before it was "not yet" (never bytes early, ENOENT or another error) |
| PASS | NFSv4 reader across the kill: the object's bytes, byte-identical, no zero block |
| PASS | and every answer before it was "not yet" (never bytes early, ENOENT or another error) |
| PASS | the fill resumed where it recorded its progress, not from 0 (140 MiB fetched for a 132 MiB object) |
| PASS | k.segy is a whole local copy now, its progress record gone |
| PASS | chaos/r.segy is a placeholder on the edge |
| PASS | 10 ranges at random offsets of a 160 MiB S3-resident object over NFSv4: byte-identical |
| PASS | only the chunks they touch came from S3: 44 MiB of 160 MiB (11 chunks of 4 MiB, and the header prefix) |
| PASS | r.segy stays a placeholder with a chunk map (12 chunks local), never filled whole |
| PASS | the same ranges over NFSv3: the same bytes, from the chunks now local (no S3 bytes) |
| PASS | systemd restarted the daemon after a SIGKILL |
| PASS | after the kill, the same ranges read the same bytes from the chunks on disk (no S3 bytes) |
| PASS | Ganesha stayed up through the kill (same pid 152506) |
| PASS | the worker seeds the third source (e2e-mig/mig/ at the share's mig/) |
| PASS | its four objects are placeholders on the edge |
| PASS | a user deletes the S3-resident mig/del.segy over NFSv4 ({"ok": true}): a tombstone in the catalog |
| PASS | an NFSv4 reader holds mig/big.segy open, its first 4 MiB read |
| PASS | migration 1 of mig/: the first run copied the online objects, and verifies them |
| PASS | and g.segy (Glacier) has no copy yet: only its S3 object |
| PASS | a user's read of mig/g.segy: 'not yet' (JUKEBOX), never ENOENT |
| PASS | its S3 restore serves the migration too (a user recall, migrate_on_complete) |
| PASS | once restored, the user's read returns g.segy's bytes, no zero blocks |
| PASS | the restore was staged: an S3 copy of the restored object into OmniFile's staging bucket (e2e-ops), never the source bucket |
| PASS | a staging run copied it into Azure |
| PASS | its copy in Azure is verified (a CRC read in Azure against the S3 object's own) |
| PASS | migration 1 cut over |
| PASS | with no rerun of the whole prefix: the copier's last run is still the first (omnifile-migration-1-run-1) |
| PASS | the cutover used the staged copy: g.segy's primary is its verified copy in Azure, the S3 object secondary |
| PASS | and the staging object was deleted once its copy was verified |
| PASS | the fd opened before the cutover read all of big.segy through it, byte-identical, no zero blocks |
| PASS | and the file kept its inode on the edge (100663389) |
| PASS | an object first read after the cutover comes from Azure: byte-identical, no S3 bytes |
| PASS | the cutover report lists mig/del.segy (its S3 object, never deleted by OmniFile) |
| PASS | and who deleted it: the NFS user's identity (1: uid 20002, S-1-5-21-3190051465-787089565-3716134449-1109, via nfs), claimed (sec=sys) |
| PASS | OmniFile never wrote a source bucket (e2e-bucket, e2e-chaos, e2e-mig: no PUT, COPY, DELETE, tagging, retention or policy request) |
| PASS | its only S3 writes went to its staging bucket: the staged copy in, then deleted ({"COPY": 1, "LIST": 1, "GET": 1, "DELETE": 1}) |
| PASS | and its restores and staging copy read the sources (RESTORE, COPY_FROM on e2e-mig) |
| PASS | Ganesha never restarted |

139 passed, 0 failed
