# Annex: OmniFile as a storage sync for an existing NAS

**Status: DECIDED (2026-11-04).** WP10, design only. Owner requirement (2026-11-04): "OmniFile should only act as a storage sync. It will read the filer." The owner decided the 13 open questions the same day (§9.2). Sources and raw findings: `docs/research/2026-11-04-annex.md` (read 2026-11-03).

**Decision 51 (2026-11-04), for DECISIONS.md:**

> **An Annex watches a directory on an existing NAS and acts on its files, without sitting in the users' data path.**
> - It works with any NFS or SMB server. **NetApp ONTAP and Dell PowerScale are treated as generic NFS/SMB servers:** scheduled scans plus reconciliation, with no vendor-specific integration. FPolicy and PowerScale changelists are possible later additions.
> - Users keep using the NAS directly.
> - **The edge** next to the NAS reads the directory and copies chosen files to Azure as plain blobs. It is the copier, not Storage Mover; decision 20 still covers restored S3 objects.
> - Under the annex's rules it may later remove a file from the NAS, but only after the file's copy is verified in full (CRC64-NVME, decision 24) and the file is unchanged since.
>   - Removals are audited, throttled, can be paused, and start only after an ArchiveAdmin approves the rule set.
>   - A new removing annex first runs 7 days as a dry-run.
> - Three outbound modes (**Copy**, **Archive after N days**, **Move now**) and three return options for recalled files (**back where it came from**, **a new folder**, **an OmniFile share**) are templates in one rules system. It is built from existing objects: NAS folders, OmniFile share folders, storage tiers and edges.
>   - Within each trigger, the first matching rule wins.
>   - Every proposal names a person who has access to the annex.
> - **Pointer files** are on by default for Archive after N days, and off for Move now.
> - **Deletes on the NAS** in Copy mode are reported, never mirrored. The copy is kept until an ArchiveAdmin purges it.
> - **Destination in v1:** Azure only.
> - **A recalled file:**
>   - It lands back where it came from by default, or in a restore folder outside the annex for Move now. A `-restored` name is used on a clash, and a 14-day hold applies.
>   - The requester may pick another target only from those the rules list, and only where they have write access.
>   - It keeps its own captured ACL wherever it lands.
>   - It overwrites a file on a NAS only when the replaced file is annexed and already verified in Azure, and only at its own name in its original folder (decision 82).
>   - It is written under a temporary name, verified by a CRC64-NVME read on the target, and only then renamed into place.
> - **A recall into an OmniFile share** is a server-side copy into the share's dataset, filled on the edge. Admins can delete such recalled copies at any time, one by one or in bulk by filter. The delete removes only the recalled copy, never the archived original, is audited, and respects WORM and holds.
> - OmniFile never stubs a NAS file. It never uses FabricPool, CloudPools or SmartLink, never emulates a vendor's API, and never replicates with SnapMirror or SyncIQ.

---

## 1. What an Annex is

An **Annex** is a watched directory on someone else's NAS, for example `\\ontap01\geo\long-term-storage` or `isilon:/ifs/geo/incoming`. OmniFile reads it and acts on its files. It does not serve it: users keep opening files on the NAS as they do today.

This is different from an OmniFile share, where the edge *is* the filer, and from decision 35's `serve` mode, where the NAS is a backend behind an OmniFile share. Nothing in the user's read or write path changes, so nothing in it can fail closed or open because of us.

Each annex has one **mode**, chosen at creation:

| Mode | What happens | The NAS |
|---|---|---|
| **Copy** | Every selected file is copied to Azure, and copied again when it changes. An archival backup. | Never changed by the copying. Mounted read-only unless the annex's return rules may put recalled files back there (§4.6). |
| **Archive after N days** | Copy, and a file that has been in the directory longer than N days is removed from the NAS once its copy is verified. | Files leave after N days. |
| **Move now** | Copy, and remove from the NAS as soon as the copy is verified. A drop folder. | Files leave within minutes. |
| **Custom rules** | Any mix of the above, per path, type, size, age or owner (§4). | As the rules say. |

The three modes are built-in rule templates (§4.4), so "Custom" is the same engine with the rules visible. The same rules also decide where a recalled file lands: back where it came from, in a new folder, or in an OmniFile share (§3.6, §4.6).

Once a file is in Azure, it is an ordinary OmniFile catalog file:
- it follows a tier policy (Hot → Cool → Cold → Archive, or Smart plus an archive date);
- it shows in the portal with its tier track;
- it can be recalled, and versioned (decision 30).

What the annex adds is where the file came from (`\\ontap01\geo\...`), when it **arrived** and when it **left** the NAS, and which rule moved it.

## 2. Seeing changes

Every annex keeps the same per-file record, whatever the source: first seen, last seen, gone since, size, mtime, ctime, and the file id (inode, or SMB file id). Hints such as SMB change notify, and any later vendor feed, only make that record fresher. **A periodic full reconciliation scan backs every source**, so a missed or dropped event can delay an action but never lose a file (§2.3).

### 2.1 NetApp ONTAP and Dell PowerScale: generic in v1

**The owner decided (2026-11-04): no vendor-specific integration for now.** An ONTAP SVM or a PowerScale cluster is annexed like any NFS or SMB server (§2.2): scans plus reconciliation. This keeps v1 to one change-detection path, tested once, with no partner agreement and no vendor API account to look after.

What the research found, kept for later (details in the research note):
- **ONTAP FPolicy** is the natural ONTAP feed: events pushed to an external server, asynchronous and non-mandatory so users never wait on us, with a persistent store from 9.14.1. **Its wire protocol isn't in ONTAP's public documentation**, and community posts say NetApp gives it to approved partners. It is a possible later addition, through NetApp's partner programme, never by reverse-engineering.
- **ONTAP native auditing** can fill a NAS's staging volume and then block its users, and needs audit ACEs on the tree. Not used.
- **ONTAP SnapDiff** is for licensed backup partners, and its licence reportedly forbids moving data to non-NetApp storage [S]. Not used.
- **PowerScale snapshot changelists** are a public Platform API (`ChangelistCreate`, then `GET /platform/10/snapshot/changelists/{id}/entries`). They give exact arrivals and departures between two snapshots, need SnapshotIQ, and are what SyncIQ uses. This is the strongest later addition, because it needs no partner agreement. **PowerScale protocol audit events** (syslog or CEE, with a forwarder that resumes after an outage) could be a fast hint beside it.

The catalog keeps `event_source` an open enum (§7), so either feed is an addition, not a change.

### 2.2 Any NFS or SMB server (every NAS in v1)

- **Scheduled scans:**
  - a **quick scan** often (default hourly), which reads only directories whose mtime changed and so finds arrivals, departures and renames;
  - a **full scan** less often (default daily), which stats every file, because a file rewritten in place doesn't change its directory's mtime.
- **Cost.** Storage Mover's own tests are the best public numbers: about 2,860 items/s over NFS with a 4-core agent, and 21–23 hours just to *scan* 117 million files over SMB.
  - At 10^6 files a full scan takes minutes.
  - At 10^7 it takes about an hour over NFS.
  - At 10^8 it takes about 10 hours over NFS and about a day over SMB.
  - Our walker runs directories in parallel and must be measured (§10). At 10^8, a daily full scan over SMB is not realistic; it becomes weekly, and quick scans carry the day.
- **SMB CHANGE_NOTIFY** (watch a subtree) is a cheap hint on any SMB server: it triggers a quick scan of the directories it names. It overflows under load ("rescan the directory"), so it is never the record.
- **Arrival and departure without events.**
  - **Arrived** = the first scan that saw the file. The window is the scan interval, narrowed by the creation time where the protocol gives one (SMB always; NFSv4 `time_create` where the server has it).
  - **Left** = between the last scan that saw it and the first that didn't (`last_seen` → `gone_since`).
  - A rename is told from a delete plus an arrival by the file id (inode, or SMB file id), which stays the same.
- **Quiet period before copying.** A file is copied only once size, mtime and ctime are unchanged across two looks at least Q minutes apart (default 10, as the edge's own landing zone does).
  - A SEG-Y file must also pass the structure check the edge already runs.
  - The edge stats the file again after copying; if anything changed while it was read, the copy is thrown away and tried after the next quiet period.

### 2.3 Reconciliation

A full scan runs on a schedule even when events flow: weekly by default, daily for annexes under 10^7 files. It compares the NAS with the annex's record and writes drift rows (`catalog.reconcile_drift`, as the S3 sources do) for:
- missed arrivals;
- missed departures;
- files changed with no event;
- files whose copy no longer matches.

An event feed that went quiet longer than its expected rate raises an alert and triggers an early scan.

## 3. Copying, removing and restoring

### 3.1 Plain blobs only

Copies are the original file bytes as one block blob each. They carry OmniFile's usual metadata (`file_owner`, `file_group`, `file_permissions`), CRC64-NVME and index tags. Any Azure tool can read them (AGENTS.md "plain blobs only").

The vendors' own cloud tiering is ruled out, because neither writes files:
- **FabricPool** packs cold 4 KB WAFL blocks into 4 MB objects; ACLs, directories and metadata stay on the array. On Azure it supports only Hot and Cool, never Archive or blob-level tiering.
- **CloudPools** splits files into 2 MB chunks (cloud data objects), optionally zlib-compressed and AES-encrypted with the cluster's keys. It leaves an 8 KB **SmartLink** stub that is "the sole means to access" the data, and supports only the Hot tier.

Without the array, neither is readable; neither reaches Archive; and both make the array the only way back to the data. That is the opposite of an annex's purpose.

One side effect to watch: annexed files may already sit on FabricPool or CloudPools. Reading them to copy pulls the data back from the cloud tier (cost, time, and with FabricPool `auto`, random reads rewarm blocks). The annex shows a warning when the NAS reports it (OneFS SmartLink files, ONTAP tiering policy), and copies such files at a lower rate.

### 3.2 Who copies: the edge (decided)

| Copier | Fit |
|---|---|
| **The edge, reading the NAS over NFS/SMB and uploading with its own uploader** | Already built (M1): plain blobs, our metadata names, per-block `x-ms-content-crc64`, user-delegation SAS from the service, private endpoints enforced (decision 22). It sits next to the NAS. It copies exactly the files the rules choose, one at a time. |
| Azure Storage Mover | Copies NFS or SMB to Blob, but: <ul><li>it needs its own Hyper-V or VMware VM with "unrestricted internet connectivity";</li><li>its control traffic and logs stay public;</li><li>it copies whole subpaths, with no per-file selection;</li><li>it writes metadata in its own names, which we'd restamp;</li><li>its copies still need our verification.</li></ul> |
| NetApp Copy and Sync, XCP; PowerScale tools | Vendor-specific, and none writes plain blobs with our metadata and checksum. Not needed. |
| AWS DataSync | For an S3 destination only. NFS → S3 keeps uid/gid/mode; SMB → S3 keeps no ACLs. Only relevant if annexes ever target S3, and decisions 18 and 23 keep Azure the home. Later, if at all. |

Storage Mover fails the native-features rule on two recorded grounds: it can't express a customer's policy (per-file rules), and it can't keep its traffic private. Decision 20 still holds for what it covers: copying restored S3 archive objects. Storage Mover stays an option for a one-off **bulk seed** of a huge Copy-mode annex (§10, phase A6).

The annex copier runs in its own unit (`omnifile-annex@<id>.service`) with its own NAS mounts. A hung NAS can then never stall the daemon that serves OmniFile's own shares.

**A NAS that stops answering.** NFS mounts are `hard`, so a call into a NAS that has gone quiet blocks in the kernel, with the unit's loop inside it: nothing is asked or reported, and the bound on a placement never starts. The lab's run shows this (`wp14-a3.md`). `soft` isn't used, because each stat and read would wait out its own timeout, one after another, for every file a tick touches.

So one thread per mount only stats it, because a slow pass isn't a hang. When a stat hasn't returned for `hang_s` (300 seconds by default), the unit logs an ALERT and **tells the service first**, within 20 seconds: a failed `mount` report, which is the filer's health, and a `nas_not_answering` problem. Then it exits, leaving a note only if the service couldn't be told. An admin sees the hung mount within about `hang_s`, whatever the process can do next.

**Whether the exit gets out depends on what it has open.** From real runs of the unit on kernel 7.0, with NFS blocked by a firewall rule and with the server frozen:

| What the unit was doing | NFS | How the NAS went quiet | Service told after | The process |
|---|---|---|---|---|
| A scan | v3 | Blocked | About 90 s | Exits at once (status 75), and the unit restarts |
| A scan | v4 | Blocked | 93 s | Exits at once |
| A scan | v4 | Frozen | 99 s | Exits at once |
| A fill | v3 | Blocked | 98 s | Held in D state (`folio_wait_bit_common`) |
| A fill | v3 | Frozen | 99 s | Held |
| A fill | v4 | Blocked | 96 s | Held |
| A fill | v4 | Frozen | 95 s | Held |

A probe with a small C program on a frozen server also saw NFSv4 with nothing open held in `nfs4_proc_destroy_session`. The real unit didn't do that in these runs, but it shows the table depends on the kernel and on timing, so nothing relies on the exit getting out. A frozen NFSv3 scan wasn't run.

- **When it exits,** the mount namespace goes with it and systemd restarts the unit. The new process can't mount, and says so. While it can't mount it still asks for tickets, sends reports and runs the placements' bound, so a cancel is heard and a placement goes `stuck` as for any outage. A mount that doesn't answer in 180 seconds is given up on (`mount.nfs` over NFSv4 otherwise blocks for good). Before each pass a writable mount is checked to still be a mount, and the root opened from it to be on that mount's device: a helper that outlived its SIGKILL can finish an `umount -l` late, and a plain directory there would take the writes on the edge's own disk and could report them placed. It then counts against the bound, nothing is written, and the mount is made again.
- **When the kernel holds it** (a fill: closing the file waits for its dirty pages), it stays in D state and SIGKILL does nothing. systemd shows `active (running)`, with the same main PID and `Result=success`. Nothing more is reported or bounded, and no cancel is heard, until the NAS answers. It then goes, and the unit restarts by itself.
- **What an operator can do:** bring the NAS back, or restart the unit. `systemctl restart` takes four stop timeouts (`TimeoutStopSec=60`, so about four minutes), going through `deactivating` and `final-sigkill` with `Result=timeout`. systemd then gives up on the old process, which stays in D state until the NAS answers, and starts a new one, which says it can't mount, asks and bounds. **Rebooting the edge also frees it. Never write to `/sys/fs/nfs/server-*/shutdown`:** with hung mounts it panicked a kernel in testing.

When the NAS answers the unit mounts again, and what was in hand resumes from its journal, without waiting out a `stuck` placement's hour. A cancel takes effect in the service at once, and `go` is refused from then on. The edge hears it when the placement next reports (when it sticks at the bound, or at its hourly ask after that), and removes what it made once the NAS answers.

On the appliance (WP14 A2.3, `docs/milestones/results/wp14-a2-appliance.md`) the image carries `omnifile-annex`, both units, and the NAS mounts' tools (`cifs-utils` with `cifs.upcall` and its request-key rule, `nfs-common`, `kinit`). `rpc.gssd`, which NFS `sec=krb5` needs, is a static unit nothing else starts: each annex unit has `Wants=rpc-gssd.service`. The supervisor `omnifile-annex.service` is enabled by the preset, but its appliance drop-in holds it until the box has a config and an identity, and setup's services step starts it. `appliance/tests/smoke.sh` checks each of these. It also checks that an annex unit, as sandboxed, can mount read-only in a namespace no other process sees.

### 3.3 Verification (decision 24)

- The edge computes the file's CRC64-NVME as it reads it from the NAS and uploads it with per-block CRC64s (`x-ms-content-crc64`).
- **At commit (the usual case on Azure):** when every block was put with its CRC and Azure answered with it, Azure checked each block as it stored it, and the service checks that the blocks' CRCs combine to the file's. The copy is `verified` then (`verified_by = 'upload_block_crc'`).
- **Otherwise** (a block without its CRC; the simulated backend of the exit test always takes this path), the `verify` queue reads the blob inside Azure (`worker/verify.read_crc`, no egress) and compares the two (`verified_by = 'azure_read_crc'`), as in the migration engine.
- Not sampled: every file, every byte.
- **A copy the read-back refuses** (other bytes, or no blob at all) stays unverified, so nothing leaves the NAS for it. The annex shows a `copy_refused` problem, once per version, and its edge is asked to reconcile (`reconcile_asked_at` in `GET annexes`). That reconciliation copies again only the files whose records say `refused`, never a copy whose read-back is only pending.
  - **Bounded:** the refusals are counted per file, one per version, and a version that verifies sets the count back to 0 and resolves the file's `copy_mismatch` drift. The first refusal asks at once, the second an hour later (`ASK_BACKOFF`), and at the third (`MAX_REFUSALS`) the service stops asking: an ALERT is logged once, and the gauge `omnifile.annex.copies_refused` keeps an alert up while the file is there. The edge's own reconciliation, every `reconcile_days`, still copies it again, as before.
  - **An ArchiveAdmin decides the rest** (#81's follow-up, migration 0058), per file, with a fresh sign-in, audited, from the annex's Problems:
    - **Stop copying it again, until I say:** the edge's records say `copy_stopped`, so no reconciliation copies the same bytes again (its own included), and a later refusal asks for none. A file changed on the NAS is still copied: that is a new copy. **Resume** undoes it.
    - **Acknowledge** the refusals: the file leaves the gauge, so the alert quiets for it alone. A new refusal ends the acknowledgement unless copying is stopped.
    - **The gauge** counts a file at the bound, or stopped whatever its count, until acknowledged: a file stopped at its first refusal would never reach the bound. So a file with no verified copy is silent only after two audited acts.
    - Both end when a version verifies, a verified version placed back under its name included (`_returned`). A stopped file stays on Problems, however old its refusals, until then or until it is resumed: it is never dropped with the 30 days' events.
  - A refusal that comes before the edge's `copied` event is said by that event. Both take the annex's row, then the location's, then the annex file's, so neither misses the other and they can't deadlock. A read-back the redrive gives up on is counted the same way, and its problem says it couldn't be done rather than that it didn't match.
- **A2's removal gate needs the in-Azure read.** Before anything is removed from the NAS (§3.4), a copy verified only by `upload_block_crc` is read back in Azure first, so the bytes Azure holds now are checked, not only the bytes it was sent.

### 3.4 Removing a file from the NAS

**Nothing is removed from the NAS until its current version's copy is verified in full.** On top of that, a removal must pass every one of these, in order:

1. **The annex allows removals:**
   - its mode or rule removes this file;
   - removals are not paused (a per-annex switch, plus a global one in Settings);
   - the annex isn't in dry-run;
   - the ArchiveAdmin approval of the current rule set is recorded.
2. **Re-check just before removing.** The file's size, mtime, ctime and file id equal those recorded at verification, as does a vendor change id if a later feed gives one. Any difference cancels the removal, and the file is copied again as a new version.
3. **Not in use, and safe to remove:**
   - SMB: the edge opens the file with delete access and no sharing. A sharing violation means someone has it open, and the removal waits for the next pass.
   - NFS has no such signal, so step 4's grace period covers it.
   - A file with more than one hard link is never removed (decision 42: we copy it as separate files, and removing one name frees nothing).
   - A file with SMB alternate data streams is never removed unless the annex copies streams (v1 doesn't).
   - A file on a SnapLock or WORM volume is never removed.
4. **Two-step removal:**
   - The file is renamed in place to a hidden name, `.omnifile-removing-<id>-<name>`. Users stop seeing it, but the bytes are still on the NAS.
   - After a grace period (default 15 minutes) with no change, it is deleted.
   - If it changed, or someone renamed it back, the removal is cancelled and the name restored.
   - The edge journals each step before taking it, so a daemon killed mid-removal resumes and never removes twice or removes an unverified file (the fill journal's pattern).
5. **Throttled:** a per-annex rate (default 1,000 files per hour) and an optional window (for example nights only).

**Audit:** every step writes an audit record:
- scheduled, renamed, deleted, cancelled (and why), paused, resumed;
- each record names the file, its version, the CRC and the rule that chose it.

The portal shows removals pending and done (§6).

**Dry-run:** the annex runs every step except the rename and the delete, and records "would remove". New annexes with a removing mode start in dry-run for a shadow period (default 7 days, §9.2 decision 5).

**Space:** the NAS's own snapshots keep removed files until those snapshots expire. The annex page says so, so nobody expects space back at once.

### 3.5 What is left on the NAS

Two choices: nothing, or a small pointer file `<name>.omnifile.txt` that says where the file went and links to the portal page for it (with a Recall button). A pointer file is a separate, ordinary text file, never a stub. The annex records each pointer it writes and ignores pointers in its own scans.

**Decided (2026-11-04):**
- pointer files **on by default for "Archive after N days"**, where people come back to a folder and wonder where a file went;
- **off by default for "Move now"**, a drop folder where leaving is the point;
- a per-annex switch either way.

### 3.6 Getting a file back

A recall of an annexed file runs through the same rules as the outbound path (§4). The annex's return rules decide where the file lands:
- back where it came from;
- a new directory;
- or an OmniFile share.

The requester may choose another target within what those rules allow. §4.6 covers name conflicts, ACLs, verification, space, partial writes, the pointer file and loops.

Who may recall: anyone the captured ACL lets read the file, checked as for portal recalls (`access-control.md` §5). Where the file may land is a separate check of the requester's write access to the target (§4.6).

**Later (phase A6):** an optional read-only OmniFile share of the annex's whole dataset, for people who want to browse everything that ever left the NAS. It reuses the existing stub and recall machinery.

### 3.7 Changes and deletes on the NAS

- **A file changed on the NAS after it was copied** becomes a new version. It is copied again after the quiet period, and the old version stays (decision 30). Removal always concerns the current version.
- **A file deleted on the NAS in Copy mode: keep the copy, never mirror the delete** (decision 40's "deletes reported, never applied").
  - The catalog marks the file tombstoned with `gone_since`, and the annex page lists it.
  - The copy is kept until an ArchiveAdmin purges it, or until an optional per-annex retention ("purge copies of files deleted on the NAS after N days", off by default) runs out.
  - A purge refused under a legal hold or WORM retention ends there: nothing is deleted, the request is cleared, and `purge_refused_at` and a `purge_refused` problem say so. The file can be returned while the hold lasts, since the hold keeps the bytes and doesn't stop them going back, and it can be asked for again. A retention tries again after 7 days. A purge pending but not refused still counts as taken: no return is offered. A new request, a purge that goes through or the file coming back clears `purge_refused_at`.
  - A retention never touches a file with a placement open, from the moment it is asked for, its recall included, and its request is queued in the same transaction (decision 27), so a crash leaves work the worker still does. Asking again for a request still open queues it again; a second message finds nothing to do.
  - This is the point of an archival backup: a deleted or ransomed file can come back.
- **A user writes a new file where a removed one was:** it is a new arrival and a new version of the same catalog path. The old one stays as an older version.

## 4. The rules: one workflow model for both directions

The owner (2026-11-04): "the same system we use to set up how the files are moved [should] be the same system used to set back". So there is one model. It is built from objects OmniFile already has, and the three outbound modes and the three return options are templates in it.

### 4.1 Primitives

**Locations** name existing catalog objects, never free-form hosts:

| Location | Refers to | Can be |
|---|---|---|
| `nas_folder` | an annex (`annex_id` + a path under its root), or a folder on a registered filer (`filer_id` + path) | a source (outbound) or a target (return) |
| `share_folder` | an OmniFile share (`share_id` + path) | a target |
| `tier` | the annex's dataset in Azure, with a logical tier or tier policy | where copies live |
| `edge` | an enrolled edge (`edge_id`) | who does the work. By default the edge of the annex or share, named only where there is a choice. |

**Triggers:**

| Trigger | Fires when |
|---|---|
| `arrived` | a file appears in a `nas_folder` and has passed its quiet period |
| `changed` | a copied file's content changes on the NAS |
| `age` | a file crosses an age, checked at least daily (age since arrival, mtime or atime) |
| `deleted_at_source` | a file vanishes from the NAS (Copy mode's tombstone) |
| `recall_requested` | someone asks for a removed file from the portal, Teams or the API |
| `recall_completed` | the file's bytes are readable again (rehydrated if they were in Archive) |
| `schedule` | a time, for example nightly (later, if needed) |

**Conditions** (all must match; omitted means "any"):

| Condition | Example |
|---|---|
| `path` glob, relative to the location | `surveys/**/raw/*` |
| `extension` | `segy, sgy, sgy.gz` |
| `size` | `>= 1 GiB` |
| `age_since_arrival` | `> 30 days` |
| `modified_age` (mtime) | `> 90 days` |
| `accessed_age` (atime) | `> 180 days`. The editor warns that many NAS mount with `noatime`/`relatime`. |
| `owner` / `group` (AD SID, picked by name) | `GEO\SeismicTeam` |
| `content_type` (sniff of the first bytes) | `segy` (3,200-byte textual header plus 400-byte binary header), `zip`, `hdf5` |
| `requester_in` (return rules only) | `GEO\Processing` |

**Actions:**

| Action | Effect |
|---|---|
| `exclude` | Do nothing; the file stays where it is. |
| `copy_to: <tier>` | Copy to Azure; recopy on change. |
| `remove_from_source` | Remove from the `nas_folder` it came from, only after its copy is verified, by the §3.4 pipeline. Allowed only after a `copy_to`, now or earlier. |
| `remove_after: N days` | The same, N days after arrival (a shorthand for an `age` rule). |
| `place_at: <location>` | Write the file to a `nas_folder` or `share_folder`, with a conflict choice (§4.6). |
| `policy` / `tier` | The tier policy of the copy in Azure (decision 5), or a start tier. |
| `hold: N days` | Keep the file where it was placed; outbound rules skip it until then (§4.6, loops). |
| `notify: teams` | A Teams card to the requester, the file's owner (SID → Entra, as for recalls) or a named group. |
| `tag: key=value` | An index tag on the blob and a label in the catalog. |

"Move" is `copy_to` plus `remove_from_source`. A rule is **trigger + conditions + actions**, stored as JSON, not as code. There is no scripting, no loops and no network access, so a rule can't do anything the tables don't name.

### 4.2 Order and conflicts

- Rules are grouped by trigger. Within a trigger, **the first matching rule wins**, with all of its actions: one file, one rule per event, which is easy to explain and to preview.
- **Each trigger in use ends with a default rule**, so every file is accounted for.
- **`remove_from_source` needs a `copy_to`**; the editor adds it and won't let it be dropped.
- **Checks block saving** a rule set when:
  - a rule can never match because an earlier one covers it;
  - `remove_after` is shorter than the quiet period;
  - a `place_at` target is a folder that a `remove_from_source` rule covers with no hold (§4.6);
  - a location names an annex, share or edge that doesn't exist or that the proposer can't reach.

### 4.3 Versions, proposals and approval

- A rule set is versioned. Editing makes a new **proposal**, and the running version keeps running until the proposal is approved.
- **An ArchiveAdmin approves** every proposal whose rule set removes anything, or that adds a `place_at` target outside the annex's own folders (anti-pattern 7). Other proposals may be approved by the annex's Owners.
  - A removing proposal that adds or widens a removal starts a fresh 7-day dry run. So does any change to its `arrived`, `changed` or `age` rules: an age rule removes what the arrival rules copied, so copying more removes more.
  - Both are computed again at approval, against the set running then, whatever was stored at proposal.
  - Every proposal moves the annex's ETag, and an approval names its version, so nobody approves a proposal they haven't seen.
- Nobody approves their own proposal (as `catalog.policy_proposal` already enforces).
- **Every proposal names a person who owns it,** including one made through the partner API or by an AI.
  - That person must be an Owner of the annex, or have read access to its root, at proposal time and again at approval time.
  - The API refuses a proposal without one (`422 owner_required`, `403 owner_lacks_access`).
  - This is stricter than today's policy proposals, which allow an application alone.
- Each proposal carries its **dry-run report** (§4.5) as it was when proposed. The approver's page shows the rules, what they change against the running set, and asks for a fresh dry run when it opens.

### 4.4 Templates

The creator picks one outbound and one return template; "Custom" opens the same rules for editing. Every rule set starts with a fixed, hidden rule that excludes OmniFile's own files (`*.omnifile.txt`, `.omnifile-removing-*`, `.omnifile-restoring-*`).

**Outbound:**

| Template | Rules |
|---|---|
| Copy | `arrived` → `copy_to` · `changed` → `copy_to` |
| Archive after N days | as Copy, plus `age > N` → `remove_from_source` |
| Move now | `arrived` → `copy_to`, `remove_from_source` |

**Return** (trigger `recall_completed`):

| Template | Rules |
|---|---|
| Back where it came from | `place_at: <original nas_folder and name>`, conflict `restored_name`, `hold: 14 days`, `notify: requester` |
| Into a restore folder | `place_at: <chosen nas_folder>/<original relative path>`, conflict `restored_name`, `notify: requester` |
| Into an OmniFile share | `place_at: <chosen share_folder>/<original relative path>`, conflict `overwrite_keep_version`, `notify: requester` |

**Default return template per outbound mode:**
- Archive after N days → "Back where it came from".
- Move now → "Into a restore folder" outside the annex, because a drop folder would just send the file out again.
- Copy → "Back where it came from". It matters only when the NAS lost the file.

### 4.5 Dry-run preview

For outbound rules, the edge evaluates the rule set over its latest listing of the real directory and returns:
- per rule, the files and bytes that match;
- what each would do now;
- a removal timeline: how many files and bytes leave the NAS on which days;
- 20 sample paths per rule.

It reads metadata only, so a preview over 10^7 files takes seconds to minutes. The edge already holds the listing (§6.2).
- **No file is read for a preview.** A `content_type` condition needs a file's first 4 KB, and reading them updates its last-access time on the NAS. The copier sniffs the type from the read it makes anyway (the hash pass), and a preview uses only those types. A `content_type` condition on a file not copied yet is unknown in a preview. A real arrival does read the first 4 KB, and the editor says so.
- **Unknowns decide nothing.** A condition the edge can't evaluate now (an ACL it can't read, a type not known, an atime it doesn't have) is neither true nor false. A file whose rule depends on one, at or before the rule that would decide, is **undecided**: not copied by a guess, never removed, and counted apart in the report.
- **The real run decides as the preview does.** A removal is decided again under the running rule set when it is due, never from the rule saved at arrival. Only `arrived` and `age` rules remove; a `changed` rule only copies again or leaves the change.
- **Previews are bounded.** Each runs off the edge's event loop, on its own connection to `annex.db`, within 2 minutes. The edge marks a run as taken (`running`) before it starts; a newer draft from the same person replaces their older ones, and a run not taken in 10 minutes or answered in 15 fails.
- **Counts can name files, so previews are ArchiveAdmins' (and apps') for now.** The edge's listing is unfiltered, and an annex's folders in the catalog carry the Owners' ACL, not the NAS's. So no check can tell which NAS folders an Owner may read, and counts would tell them whether a file exists in one they can't (`smith-salary*.xlsx`). Anyone else gets `403 role_required` for a preview and no report on a proposal. **Capturing the NAS folder ACLs** (as files' ACLs are captured today) is what would reopen previews to Owners, for rules on paths in folders they can read.
- **Age removals trust the clock only when the service agrees with it.** `GET annexes` carries the service's time (`served_at`). While the edge's clock is more than 10 minutes from it, no age-based removal is decided.
- **Limits** (service and edge): 200 rules with unique ids, 100 values per list, globs of 512 characters, 32 segments and at most 4 `**`, never two in a row. The edge's glob matcher is linear in the pattern times the path, whatever the `**`.

For return rules, the preview runs over the annex's removed files: per rule and target, the files that a recall would place where, how many would hit a name conflict today, and how much space each target would need against its free space now.

One implementation, in the edge, evaluates rules both for the preview and for real, so the preview can't drift from what happens.

**Return rules are the service's (A3.1).** Their inputs are the removed file as the catalog knows it and who asks, and both are the service's. So the service evaluates them, in one place (`annex/returns.py`), for the Back preview and for real. The edge skips them and places a file only on a ticket.

### 4.6 The return path

A recall goes through these steps, each journalled on the edge so a crash resumes or cleans up:

1. **Request.** The requester presses Recall in the portal or on a Teams card (or calls the API). The service evaluates the `recall_requested` rules: the default target, plus the other targets those rules allow this requester.
   - The requester may pick any listed target, or "a folder of my choice" where a rule allows one under a named location.
   - The service checks the requester's **write access** to the chosen target against the catalog's ACL snapshot of that folder, because the edge writes with its own account and must not write where the requester couldn't. The ACL rows gain `write_sids`, beside `read_sids`.
   - A target the requester can't write to isn't offered.
2. **Rehydrate** if the blob is in Archive (copy out of Archive, the usual recall), with the usual Teams card and progress steps.
3. **Space check.** Before writing, the edge asks the target for free space (NFS FSSTAT, SMB FS_FULL_SIZE_INFORMATION). This reflects quotas that apply to the edge's account. The file's size plus a margin (default 5%) must fit.
   - A quota that applies to the file's original owner can still refuse the write, or the change of owner, with `EDQUOT`. That is handled as a failure in step 6.
4. **Write to a temporary name** in the target folder, `.omnifile-restoring-<id>-<name>`, so a partial file never shows under the real name. Then:
   - set ownership and the ACL from the catalog (below), and the timestamps;
   - **read the file back from the target and compare its CRC64-NVME** with the catalog's. A mismatch deletes the temporary file and retries once, then fails.
5. **Resolve the name and rename atomically.** If the final name is free, rename onto it, refusing to replace (SMB rename without replace; NFS `link` + `unlink` or `RENAME` after a fresh check). If the name is taken (the file exists again, or was recreated), apply the rule's conflict choice, from decision 34:
   - **`same_content`:** an existing file with the same size and CRC is left alone, the temporary file is deleted, and the requester is told it was already there.
   - **`overwrite_keep_version`:**
     - In an OmniFile share, this is an ordinary new version, and the previous one is kept (decision 34).
     - On a NAS (A3.4, decisions 82 to 85; `docs/milestones/results/a34.md`), only at the file's own name in its original folder, and only when the file there is exactly the annex's current version, verified, read back in Azure within 24 hours, with no legal hold, no open removal and no other placement replacing it. An older version may replace a file still on the NAS. Anything else falls back to `restored_name`, and the outcome says `overwrite_refused_<why>`. The edge checks the file itself first, as before a removal's W1 (forced `statx`, one link, over SMB no stream and its CRC read again), and asks `replace_go`. Then:
       1. R1 hides the old file as `.omnifile-replacing-<placement>-<name>` (the remover's W1). An SMB sharing violation is "in use": no replace. A name too long for its hidden name (over 255 bytes) is never replaced. A file found under the hidden name with another inode is a user's file W1 hid (a save-by-rename just before it): it goes back under its own name, or as `<name>-replaced-<UTC time>` with a Problem, and the replace is given up. A removal's W1 is treated the same way. After a restart between the hide and its journal, the hidden file is read again whole (NFS and SMB) and must be the verified copy's bytes, or it goes back.
       2. R2 waits the annex's grace period, so **the name is empty for that time**, then checks the hidden file with W2's gates and asks `replace_delete_go`. A change or a refusal puts the old file back under its own name (W3), and the new file takes a `-restored` name; a user's file made at the name in the grace keeps it.
       3. R3 links the new file under the name (P4), after a forced `statx` of the staged file.
       4. R4 deletes the hidden file (W2) only where the new file holds the file's own name, as the inode this placement linked: a user's file made at the name in the grace, or a `-restored` name taken instead, means nothing replaced it, and the old file goes back (under its own name if free, else as below). It checks the hidden file again and asks `replace_delete_go` again, naming the new file's name (the service refuses any but the origin's). If a gate fails now, the old file is put back with W3 as `<name>-replaced-<UTC time>` (never `-restored`, which means a version someone asked for), the placement finishes, and the Owners get a Problem naming both files. Nothing is deleted.
       5. The service away, or an answer that isn't definite, is waited out; after the bound the placement is `stuck`, naming the hidden file. A refused W3 is `stuck` too. Every byte is kept.
       - `done` makes the file's head a new version that references the placed version's blob; the replaced version stays in its history ("replaced; the old content is version N"). The scans skip the hidden name, don't report the file's departure while it is hidden, and treat a `-replaced-` file as a new file to copy.
       - No switch of its own: the approval page marks such a rule "replaces files on the NAS", and only an ArchiveAdmin approves a rule set with one, as one that removes (decision 85).
       - The service takes each of the edge's facts only in its order and only after its yes (`replace_hidden` after `replace_go`, `replaced` after the delete phase), and keeps the file protected (no removal, no departure recorded) while a replace may have it hidden, also after its placement ended.
       - **A known limit (inherited from #61's removals).** Where a NAS refuses hard links, W3 renames the old file over a placeholder of ours with a plain rename, since NFS has no no-replace rename. A user's file swapped onto the placeholder between its last check and that rename is overwritten. The window is one round trip, and the placeholder's text says not to touch it. A placeholder left at a name W3 gave up on is removed, if it is still exactly ours.
   - **`restored_name`:** `<name>-restored<ext>`, or `<name>-restored-<YYYY-MM-DD-HHMMSS>Z<ext>` if that is taken too. A taken timestamped name is refused, never overwritten.
6. **Failures** (no space, quota, access denied, NAS unreachable) delete the temporary file. The Teams card says what happened and offers the other allowed targets.
7. **Notify.** The requester's Teams card is updated with where the file landed, as a path they can copy (`\\ontap01\geo\long-term-storage\line_1042-restored.segy`), or as the OmniFile share path, and the portal link.

**ACLs and ownership.** The file gets back its captured owner and ACL: the raw NTFS descriptor over SMB, or the NFSv4 or POSIX ACL over NFS.
- **In its original folder:** exactly what it had.
- **In another NAS folder:** still its own descriptor, not the folder's inherited one, so a restore never lets more people read the file than before. People who could read the folder but not the file still can't read the file.
- **In an OmniFile share:** the catalog ACL, which is already in the share's model (POSIX entries naming SIDs, decision 28).
- Setting another owner needs the restore right on the NAS (SMB SeRestorePrivilege, plus SeTakeOwnershipPrivilege on Samba, or NFS root). That is part of the `OmniFile-Annex-Restore` group's grant (§5). Without it, the file is placed owned by the edge's account, and the card says so.

**The staging folder and its lock** (A3.3; `docs/milestones/results/wp14-a3.md`). A file is written to `.omnifile-staging-<annex_id>-<token>/` at the top of the export before it gets a name (not, as step 4 first said, a temporary name in the target). The token is random and journalled before the `mkdir`. So a crash, a replayed `mkdir` or a journal that failed leaves a name nothing depends on: the next try uses a new one, and no folder is ever adopted.

The lock is what the folder and each staging file must read back as before a byte goes in. It allows the placement account full control and the read account read and traverse, and nobody else anything that touches the data. A server may report a little more than was set, because some derive an ACL from the mode:

- Over NFSv4, `OWNER@` and the read account may hold anything. `GROUP@` and `EVERYONE@` may keep only `READ_ATTRIBUTES` (0x80), `READ_ACL` (0x20000) and `SYNCHRONIZE` (0x100000), together `0x00120080`, none of which reads, writes or executes the file. Any other bit fails the lock, `WRITE_ACL` and `WRITE_OWNER` included.
- A named principal other than the read account gets nothing, even from that mask.
- Over SMB and NFSv3 no such allowance exists: the descriptor as set, or mode 0700 with no named entry.

The mask is in the code as `NFS4_NO_DATA_ACCESS`. NFS-Ganesha's FSAL_VFS reports exactly `0x00120080` for both at mode 0700 (the lab's run, §10.2). knfsd, ONTAP and PowerScale haven't been checked. If one reports more, placements on it stick with `staging_lockdown_failed` (fail closed), and the mask is what to look at.

**What the gate can't prove.** At `go`, the staged ACL's read set must be within the file's catalog read set (decision 51), so:

- The staged group is taken literally, and over NFS the edge compares the file's real group with the final one first (`group_not_set`).
- A staged NT owner counts as the file's own owner only when it is the filer's registered placement account (`PATCH /v1/filers/{id}`, `placement_sid`). **Registering it is a setup step on every SMB filer:** the staged file's owner is whoever placed it, so until then every placement fails `placement_account_not_registered`, with a detail that says this. The edge reports the account, and a person registers it:
  - The edge reports the SID, kind and name from winbind for the restore-rights principal it actually holds, the same account it will place files with, when its unit starts and hourly. An edge reports only for filers it serves: the service takes the edge from its client certificate, and checks the annex and the filer against it.
  - An ArchiveAdmin registers it through `PATCH /v1/filers/{id}`, with If-Match and a fresh sign-in. The report never registers anything by itself: a person approves, for the reason a policy proposal doesn't apply itself.
  - The route takes a SID only if it is what that filer's own edge currently reports, a user or machine account. No report, one over 6 hours old, a different SID, a group or a well-known SID are each refused, and the refusal says which.
- `WRITE_DAC` and `WRITE_OWNER` go to nobody who isn't a reader, the owner, or someone the catalog's ACL already gave them to.
- `WRITE_DAC` or `WRITE_OWNER` for a principal the edge couldn't map always fails `acl_mismatch`. So does read for one, or for a POSIX group with no SID, with two exceptions:
  - The catalog's raw ACL literally lets anyone read the file (`S-1-1-0`, a NULL DACL or POSIX's other class, not Authenticated Users or `BUILTIN\\Users`).
  - At its origin on its own filer, the staged ACL is provably the capture of the version being placed: the same numeric uid and gid, mode and ACL. For NFSv4 that compares raw bytes. For POSIX it needs no named entries, because the catalog keeps no raw copy of those and drops an unmapped one. It also needs the version's uid and gid to be the file's as the catalog knows them now (after a chown or chgrp the capture says nothing about who reads it), and the edge to report that the server shows POSIX ACLs on the staged file (`posix_acls_shown`). A server that hides them looks base-only, and its 0640 may be a hidden mask, so there the proof doesn't apply. If that is all it lacks, the placement fails `acl_hidden`, with a detail saying so; otherwise `acl_mismatch`.
  - Accepted: a uid or gid reused on the same NAS for someone else since the capture passes this. The catalog can't see that, and neither can the NAS.
  - **A chown after the last scan isn't known.** A placement's owner and group are what the catalog last knew for the file, the same source as its read set and ACL. A chown or chgrp on the NAS since the last scan isn't in it, so the file goes back as the scan last saw it. A tombstoned file keeps what it had when it went.

  So a `root:root 0640` file on an NFS export, whose group has no SID, goes back to its own folder, and not elsewhere; with a named entry in its ACL it doesn't go back at all.

**A credential the service can't mint** costs only that placement. The others on the filer get their tickets. It raises a `ticket_no_credential` problem, at most hourly, and the next ask tries again; only a ticket that was given is audited.

**Trouble on the edge has one rule.** A trouble that might clear is tried again with a backoff up to a bound (six tries, about ten minutes), then the placement is `stuck`: an ALERT, reported, and the service asked hourly, so a cancel or an abandon is seen. `stuck` keeps the bytes, and holds no placement mount between its hourly tries. Only a definite refusal, a cancel or an abandon deletes staged bytes; an outage of the service, Blob storage or the NAS never does, however few there are. A blob read is told by its status: 401 and 403 renew the credential; 404 and 409 renew it and read once more before they count, because an archive-date move or an expired rehydrated copy gives them with the data safe in Azure; other 4xx are definite; a CRC failure fetches the range again; and only a network failure, 408, 429 or a 5xx is an outage. At P3 and P5 only `EACCES`, `EPERM`, `EOPNOTSUPP` and `EINVAL` are definite. `EROFS`, `EDQUOT` and `ENOSPC` can clear, so they back off to `stuck`, and a whole staged copy is never deleted for them. Nothing fails after the link: P5 isn't `done` while the owner might yet be set, and only a definite error leaves the placement account as owner, which `done` says. A refusal at P5 (`EACCES`, `EPERM`) gets the bound's tries first, because a NAS's id map can refuse for a moment and this one would leave the owner wrong for good; `EOPNOTSUPP` and `EINVAL` end it at once. An ended placement with its file already under its name (the link taken, then a cancel or an abandon) drops the staging link, which leaves the file with one, and reports a `leftover` naming it. A `stuck` report says where the placement is (`at`, `name`), so an abandon can say what the file was left like, in its audit row and event.

**Ways out.** Before its `go`, a placement can be cancelled by its requester or an ArchiveAdmin. After its `go` the file may be under its name, so only an ArchiveAdmin may abandon it, once the edge has said nothing new for 24 hours (`POST /v1/placements/{id}/abandon`, audited). An abandoned placement refuses every later step, so what the edge reports afterwards is audited (`annex.placement.late_report`), raises a problem event naming the file, and changes nothing; the next scan finds the file, with no hold, and the worst case is a recopy. `placements_paused` on an annex is an ArchiveAdmin's kill switch (audited). Retiring an annex ends its placements, and other annexes' placements for its filer if no annex is left there, with a problem event for each. A staged copy stays in the staging folder, open only to those two accounts, until the NAS's owners remove it, and one that had its `go` may be under its name. A staging file the journal doesn't name is reported once and never deleted. A folder at the export's top named like a staging folder that isn't this annex's is reported once and not scanned, in case it holds another annex's staged bytes.

**Into an OmniFile share.** The blob is copied server-side into the share's dataset as a new catalog file (`origin = 'annex_restore'`, linked to the annex file and its placement). The edge fills it at once and keeps it online for the hold period, so it is there when the requester opens it. It is a plain blob copy inside Azure, with no edge upload. These **recalled copies** are extra copies of something already archived, so admins can clean them up (§8.4).

**The pointer file.**
- If the file is back under its own name in its original folder, its `<name>.omnifile.txt` is deleted.
- If the file was placed anywhere else (a `-restored` name, another folder, a share), the pointer stays and gains a line saying where and when it was restored, and by whom.
- The pointer goes only once the file is back under its own name.

**Loops.** A placed file must not go straight back out:
- **Provenance.** Every placement records the target path, file id and CRC (`annex_placement`, §7). When an annex's scan meets that file, it is a `returned` event, not `arrived`. `arrived` rules don't fire for it.
- **Hold.** Outbound rules skip a placed file until its `hold` ends (default 14 days, as "Keep online", decision 33). After that, its age counts from the placement, so "Archive after 30 days" removes it 30 days after it came back at the earliest.
- **No recopy.** If its content is unchanged, the existing verified copy is reused: a later removal needs no upload. If it changed, it is a new version, as any change is.
- **Save-time check.** A rule set whose return target is covered by a `remove_from_source` rule without a hold is refused (§4.2). That includes a Move-now drop folder.
- **Circuit breaker.** A file placed and removed more than three times in 30 days raises an alert. It is then pinned where it is until an Owner looks.

## 5. Identity and access

- **The account the edge mounts the NAS with.**
  - By default it is the edge's **AD computer account**, which already exists from the setup wizard's AD join. Kerberos for SMB, `sec=krb5` for NFS, so no new password.
  - The NAS admin grants rights through two AD groups the setup guide names:
    - `OmniFile-Annex-Read`: read, list, and read ACLs and attributes;
    - `OmniFile-Annex-Remove`: also delete, rename and create, for removals and pointer files;
    - `OmniFile-Annex-Restore`: create, rename, write ACLs and set owner (SMB SeRestorePrivilege, plus SeTakeOwnershipPrivilege on Samba, or NFS root), for putting recalled files back (§4.6). Where a rule may replace a file (A3.4), also delete on those files: the replace hides the old file by a rename and deletes it, as a removal does. Without it the NAS refuses the hide, nothing is replaced, and the file takes a `-restored` name.
  - **Copy-mode annexes mount read-only and get only the first group,** unless their return rules place files on the NAS. Then they get the restore group too, and still never remove.
  - A dedicated restore account is `restore_principal`, with `restore_uid` (a uid of its own for its ticket cache: root's holds the read account's, and the unit refuses to overwrite it) and a sealed `annex-restore.keytab`. Its SID is also registered on the filer (`placement_sid`, §4.6), or a server that shows it as a staged file's owner fails `go`. Its SID and the read account's are looked up by name through winbind unless `placement_sid` and `read_sid` give them. Over NFSv4 with two accounts, `read_nfs4_who` is the read account's NFSv4 name.
  - Mounting an SMB share with the machine account's Kerberos ticket is to be proven (§10).
  - Fallback: a dedicated service account whose keytab is sealed with `systemd-creds` (TPM plus host key), like the edge's mTLS key.
  - NFS `sec=sys` is a last resort: an export limited to the edge's IP, `ro` for Copy mode, and root access because reading every user's file needs it. The portal labels such an annex "host-trusted".
- **ACLs into the catalog.** For each copied file the edge captures the NAS ACL:
  - the NTFS security descriptor over SMB;
  - the NFSv4 ACL;
  - or POSIX mode and ACL, mapped to SIDs through winbind.
  - The catalog keeps two forms:
    - **The raw descriptor**, kept whole, so a restore to the NAS puts back exactly what was there.
    - **The captured read set** (`catalog.acl.read_sids`): the SIDs the file's own ACL lets read, after deny entries. Deny entries only ever remove a SID, so mapping errors fail toward "can't see it". It stays as it was read, for restores and placements.
    - **The effective write set** (`write_sids`, new) for folders: who may create files there. It decides where a requester may have a recalled file placed (§4.6), and fails toward "can't place it here".
    - **The search set** (`search_sids`, decision 63) for folders: who may pass through to what is below. POSIX search (x); NT and NFSv4 traverse (`FILE_TRAVERSE`, `ACE4_EXECUTE`), `GENERIC_EXECUTE` or `GENERIC_ALL`, deny-first. Unknown keeps everyone out of what is below, and the folder is reported: a deny for a principal the edge couldn't map, or (Codex and the review of #83) a POSIX group the edge couldn't map, named or owning, whose entry after the mask doesn't grant x where `other` does. Such a group's members are kept out on the NAS and who they are isn't known. A write set is unknown the same way, and then nobody places a file there.
    - **A file's read set** is unknown, and nobody may read it, for the same reason: a POSIX file whose owning group has no SID and reads less than `other` (a `0604` file, say).
  - **The folders above a file (decision 63).** Who may read an annexed file is who its captured ACL lets read and every folder above it lets through, up to the export's root. `catalog.can_read_file` is that answer, and `catalog/read_set.py` is the only code that asks it: the portal, Teams, recalls, the live feed, placements and share copies. A lint (`tests/catalog/test_read_set_lint.py`) fails on any other reader of the raw set, and a property test checks that the effective set is never wider than the captured one.
    - **Strict on SMB** (Michael Curtis, the owner, 2026-11-07). Windows grants everyone "bypass traverse checking" by default, and Samba honours it, so a user a folder's DACL keeps out may still open a file below it by its full path. OmniFile removes them anyway: it may under-show, never over-show.
    - **Up to the export's root.** The edge reads every folder of the annex and every folder above its root (`folders.rs`). One it can't read keeps everyone out of what is below it, and the annex shows a `folder_unreadable` problem.
    - **Fail closed until read** (the owner, 2026-11-07). A file is hidden from everyone until every folder above it has been read. After the update, the edge sends every folder's ACL again once, and after each quick scan it reads the folders it hasn't sent yet, so a new folder's files are hidden only until then. It also reads again those whose inode or ctime changed (a chmod, a folder deleted and made again), so a folder closed between full scans is seen at the next quick scan. The annex's Problems say so (`read_set`, and a `read_set_pending` problem for annexes that were there before), until the edge's first complete pass (`folders_read`).
    - **The portal says so.** Beside each annexed file it names the folders above it that don't let everyone through (`folders_narrow`). The annex's Problems say whether its folders have been read, and which can't be. A problem names a NAS folder only to ArchiveAdmins and apps; others see its reason.
    - **Folder names too.** The catalog folders an annex makes carry its Owners ACL, and are shown only to those the NAS folders above them let through (`catalog.annex_folder`), so a subfolder's name doesn't show under a folder the NAS keeps someone out of.
    - **At scale** (migration 0055). Nothing is derived from folder ACLs. A query walks, once, from each filer's export root through only the folders whose ACL lets the caller through (on filers whose answer admits them), and each annexed file's or catalog folder's row is then a key lookup in the set of folders reached. The walk's cost grows with the folders the caller can pass, not with every folder, and a folder change applies at the next query with nothing to recompute. One file is checked by its own chain of folders. Numbers, including a per-user home folder each, in `results/wp14-read-set-scale.md`. (Before 0055, a gate per distinct chain of folder ACLs was kept by triggers, which cost 29 us a row at 10^5 folders.)
    - **The share or export (decision 65).** SMB share permissions and NFS export rules can't be read through a mount, so filer setup asks an ArchiveAdmin whether the share or export lets in fewer people than the files do. If it does, they name the AD groups it lets in, and only members of those can read the filer's annexed files. An export limited by client host or subnet is answered the same way (the owner, 2026-11-08): they name the groups whose people use those hosts; if it doesn't, that answer is kept too. Either way it is recorded with who answered and when (`PATCH /v1/filers/{id}` `access`, audited as `filer.access`), the portal shows it beside the filer with "Confirm again", and until it is answered the files are hidden from everyone. A staged or copied reader must be one of the groups it names.
    - **A folder that goes** keeps its last ACL (`nas_folder_acl.gone_at`), so a file deleted or removed from the NAS is still judged by the folders it was in. Nothing is placed in a gone folder.
    - **A file is judged by the folders it is in now,** for every version: a file moved on the NAS out of a closed folder is read under its new folders, old versions too.
    - **Placements and share copies.** A staged ACL's readers, and whoever it gives control, must each get through every folder above the file by name (a folder that denies anyone, or lets only some through, refuses a SID it doesn't name). A folder whose ACL the target's own folders have too is no obstacle: the placed file sits below the same ACL there. So a file put back where it was goes back; anywhere else under a narrower folder, it is refused (`acl_mismatch`). A share's folders are no cover for the NAS's.
  - The portal, Teams and recalls use the effective read set, as for every catalog file (decision 28): people see in the portal only the annexed files the NAS lets them read.
  - **A blind spot, for a server that hides ACLs.** Over NFS, a server whose client can't read POSIX or NFSv4 ACLs (NFSv3 to a volume with NFSv4 ACLs, say) answers as if a file had only its mode. Then named readers are missed, which fails toward "can't see it", but a hidden mask makes the mode's group bits the mask, not the owning group's. So 0640 may read as the owning group being able to read when it can't, and the read set is wider than the NAS for that group. Placements refuse the proof there, as `acl_hidden` (§4.6). Decision 64 closes it for new grants: the edge reports at every mount whether the server shows the ACLs of its flavour at the annex root (`acls_shown`: the POSIX ACL, NFSv4 ACL or NT descriptor attribute answers, even with "none"). A server that doesn't is flagged on its filer (`acls_hidden_since`), and so is one the edge couldn't look at through a mount that works, or an edge too old to say. The portal shows the flag, and an alert fires (`omnifile.annex.filers_acls_hidden`), until an ArchiveAdmin acknowledges that access there rests on mode bits alone, or a later mount shows the ACLs, which clears both. Until then, placements and share copies of its files that anyone but the owner could read are refused (`acls_hidden_filer`); portal names and recalls go on. It catches a server that says it has no ACL support, not one that answers but withholds entries, or a mixed-style volume, so it is also to confirm on a real NAS.
  - An ACL change on the NAS, seen by the full scan (which compares ACLs by hash) or by an SMB change notify for security, updates the snapshot. Folder ACLs are captured the same way.
- **Annex roles.** An annex has Owners (AD group: edit rules, approve copy-only changes, restore) and is visible to ArchiveAdmins and DataStewards as datasets are.
  - Creating an annex with a removing mode, and approving removals, is ArchiveAdmin only.
  - The blobs are reachable only by the service identity and break-glass admins (`access-control.md` §7).

## 6. Where it runs and how it scales

### 6.1 Placement and network

The annex runs on an **edge appliance near the NAS**: the same appliance as OmniFile shares, or one used only for annexes. Data goes NAS → edge over the LAN, then edge → Azure over the private path (ExpressRoute or VPN to the private endpoints). It needs little local disk: files stream through, with a small spool for retried blocks.

**Throughput is the WAN's.** A 10 Gbps ExpressRoute gateway moves at most about 4.5 TB an hour, and private-endpoint traffic "might be reduced by half", so plan on about 2 TB an hour. A 1 PB first copy then takes about three weeks. The annex page shows the estimate from the dry-run's byte count and the measured rate, with a bandwidth window in its settings. Data Box seeding is the answer for petabytes, and comes later.

### 6.2 State at 10^8 files

- **The edge keeps the full listing** in its SQLite replica's neighbour, `annex.db`: one row per NAS file with path, file id, size, times, first and last seen, change id and state. That is about 200 bytes a row, so 20 GB at 10^8. This is where scans compare and where the dry-run runs.
- **The service catalog holds only files a rule copies.** They are ordinary `file`, `file_version` and `location` rows in the annex's dataset, plus an `annex_file` row each (§7). Excluded files never enter the catalog.
- **The first scan of an existing directory is a baseline, not 10^8 "arrivals".** The annex page shows "N files present when the annex started", and arrivals count from then on.
- **The catalog's write rate is the limit for big first copies.** The real seeder measured about 1,400 files/s per worker, so 10^8 files take about 20 hours per worker, in parallel by subtree. The WAN is slower than that for any file over a few MB.

### 6.3 Fit with the rest

- **Datasets.** An annex binds to a dataset in Azure (new or existing, with its storage layout and tier policy). It is a `catalog.source` of a new kind, `annex`, on a `nas` backend (§7). So seeding, drift, ACL snapshots and the feed work as they do for S3 sources.
- **Policy engine.** Copies follow the dataset's tier policy, or a rule's `policy`. The archive date is respected (decision 5), pins work (decision 29), and versions and rewind work (decision 30).
- **WORM** (decision 31) applies to the copies in Azure if the dataset is WORM. That is a good fit for an archival backup, and removals from the NAS are then backed by an immutable copy.
- **Not used:** FSAL_OMNI, the smb-guard and stubs. An annexed file is never a stub on our xfs, unless the optional read share of §3.6 is turned on.

**Why OmniFile must not stub files on someone else's NAS.** A stub is safe only if every read of it passes a guard that fails closed (AGENTS.md). On a NAS we own no read path. The only hook, FPolicy in synchronous mandatory mode, would make every user read on the array wait on OmniFile, which is exactly what an annex avoids. It would also be HSM on another vendor's array, which their support, snapshots and replication (SnapMirror, SyncIQ) know nothing about. So an annexed file is either whole on the NAS or gone from it, with an optional plain pointer file.

## 7. Catalog and API

New tables, in a migration numbered after WP4's reserved 0033–0035:

| Table | Holds |
|---|---|
| `catalog.filer` | A registered NAS:<ul><li>`vendor` (open: `ontap`, `powerscale`, `generic`);</li><li>host, protocol (`smb`, `nfs3`, `nfs4`), share or export;</li><li>`event_source` (open; v1 has `scan` and `smb_notify`, and later feeds such as `fpolicy` or `onefs_changelist` are additions);</li><li>its edge, and the credential mode (`machine_account`, `service_keytab`, `sec_sys`);</li><li>the last health check.</li></ul> One `backend` row of kind **`nas`** per filer (decision 35 reserved the kind). The service never reads or writes NAS bytes; the edge does. |
| `catalog.source` | Gains `kind = 'annex'`: `(backend_id, container = share, prefix = path)` → dataset folder, as S3 sources do. |
| `catalog.annex` | One per annex, keyed by its source:<ul><li>`mode` (open: `copy`, `archive_after`, `move_now`, `custom`);</li><li>`state` (`registered → baseline → active ⇄ paused → retired`);</li><li>the running rule set version, `removals_paused`, `dry_run_until`;</li><li>removal rate and window, pointer files, quiet minutes, grace minutes;</li><li>scan schedules, and the deleted-file retention.</li></ul> |
| `catalog.annex_ruleset` | Versions: `body` (jsonb), `state` (`proposed`, `approved`, `active`, `superseded`, `rejected`, `withdrawn`), `owner_identity_id` **NOT NULL**, `proposed_via` (`portal`, `api`), `proposed_by_app`, the decider, the dry-run report. |
| `catalog.annex_file` | Per copied file: the file id on the NAS, the size/mtime/ctime/change id at verification, `first_seen`, `last_seen`, `gone_since`, `removed_at`, `remove_after`, `hold_until` (after a placement), the rule that matched, and `nas_state` (open: `present`, `removing`, `removed`, `gone`, `returned`). |
| `catalog.annex_placement` | One per recall that writes a file somewhere (§4.6):<ul><li>file and version, requester, the rule and the target `location` (jsonb, the §4.1 shape) with the requester's override;</li><li>conflict choice and outcome, temporary and final names, bytes, CRC read back;</li><li>`state` (open: `requested → rehydrating → writing → verifying → placed`, or `failed` with a reason such as `no_space`, `quota`, `access_denied`, `crc_mismatch`);</li><li>the Teams card to update;</li><li>for a recall into a share (§8.4): the placed file, the request's note and source (`request_via`), `opens_since`, `distinct_openers`, `last_opened_at`, and `deleted_at` / `deleted_by` / `delete_job`.</li></ul> Scans match placed files by path, file id and CRC to tell `returned` from `arrived`. |
| `catalog.annex_event` | Partitioned by day, kept by the worker's partition upkeep. Kinds (open): `baseline`, `arrived`, `changed`, `left`, `copied`, `verified`, `removal_scheduled`, `renamed_for_removal`, `removed`, `removal_cancelled`, `recall_requested`, `placed`, `placement_failed`, `returned`, `rule_hit`, `skipped_in_use`, `loop_breaker`. |

The rule set `body` holds rules as `{trigger, conditions, actions}` with locations in the §4.1 shape: `{"kind": "nas_folder", "annex_id": 7, "path": "restored/"}`, `{"kind": "share_folder", "share_id": 3, "path": "recalls/"}`, `{"kind": "tier", "policy_id": 12}`, `{"kind": "edge", "edge_id": 2}`. Every id is checked to exist, and to be reachable by the proposer, at proposal and at approval. The kinds are open enums, so a NAS-target location (decision 35) can join later.

`verified_by` gains no value: the copy is verified by `azure_read_crc`, as today. The edge feed gains `annex.upsert` and `annex_ruleset.active`. The control API gains:
- `POST /v0/edges/{id}/annex-events` (batches);
- `POST /v0/edges/{id}/annex-dry-runs/{run}`;
- the removal and placement journals' acknowledgements;
- hourly open counts for files that have a placement (`placement_opens`), from Ganesha opens and Samba's audit of opens;
- `POST /v0/edges/{id}/placements/{p}` progress, and the edge instructions `place` (write, verify, rename) and `stat_target` (free space, name taken).

**Portal and partner API** (`portal-api.yaml`, every new enum `x-extensible-enum`):

| Route | For |
|---|---|
| `GET/POST /v1/filers`, `GET /v1/filers/{id}/health` | Register a NAS; test reach, credentials and event source. |
| `GET/POST /v1/annexes`, `GET/PATCH /v1/annexes/{id}` | Create, list and change settings. A removing mode starts as a pending approval. |
| `POST /v1/annexes/{id}/rulesets` | A proposal. `owner` (a person) required; the API checks their access. |
| `POST /v1/annexes/{id}/rulesets/{v}:approve` / `:reject` | ArchiveAdmin, or Owners for copy-only. |
| `POST /v1/annexes/{id}/dry-runs`, `GET …/dry-runs/{run}` | Preview; also attached to every proposal. |
| `POST /v1/annexes/{id}/removals:pause` / `:resume` | The switch. Also `POST /v1/settings/annex-removals:pause` for all annexes. |
| `GET /v1/annexes/{id}/events`, `…/files`, `…/removals` | Timeline, files, pending and done removals. |
| `GET /v1/files/{id}/return-targets` | The targets the return rules allow this requester for this file, with the default first and each target's free space and name conflict now. |
| `POST /v1/placements` | `place_at` (one of the allowed targets, or a folder under an allowed location), `on_conflict`, `priority` when the bytes are in Archive, `note`. It creates an `annex_placement`, and the usual recall first when needed. (A3.1 made this its own route rather than a field of `POST /v1/recalls`: a placement of an online file has no recall.) |
| `GET /v1/placements/{id}` | Progress of a placement and where the file landed. |
| `GET /v1/recalled-copies` | Recalled copies in OmniFile shares, filtered as in §8.4 (`share_id`, `annex_id`, `recalled_before`, `not_opened_for_days`, `max_opens`, `min_size`, `requester`, `changed`, `original_safe`, `hold_active`), with the §8.4 columns. |
| `POST /v1/recalled-copies:delete` | `{ids}` or `{filter, expected_count}`, plus `dry_run` and `notify`. Refused if the filter's count changed. It returns a job, `GET /v1/recalled-copies/deletions/{id}`, with per-file outcomes. ArchiveAdmin, or the share's Owners. |

Prizmanyx sees annexes through the same partner endpoints, as an application with app roles; its proposals must still name a person (§4.3). `/v1/events` carries annex lifecycle events.

**Portal work in flight:**
- **The New migration wizard (WP12)** gains a source card "A NAS share (Annex)". It hands over to the Create Annex flow below with the filer and path filled in.
- **Settings (WP11)** gains:
  - **Filers**: registered NAS filers, their credential mode, health and event source;
  - **Annex defaults**: quiet period, grace period, removal rate and window, pointer files, scan schedules, the global "pause all removals".

## 8. The portal

### 8.1 "Create an Annex"

Plain questions first; every setting one click down ("More settings"):

1. **Which share?** Pick a registered filer or add one (host, SMB or NFS; the edge tests the mount and reports read and remove rights). Then browse to the folder.
2. **What should happen to files there?** Copy · Archive after [N] days · Move now · Custom rules (opens the rule builder).
3. **When someone recalls a file, where should it go?** Back where it came from · Into this folder [pick] · Into an OmniFile share [pick] · Let the person choose among these. The default follows the mode (§4.4).
4. **Where do copies go?** A new or existing dataset (storage and layout), and its tier policy, shown on the policy timeline WP13 is building.
5. **Who looks after it?** The annex's Owners group. People see files in the portal as the NAS's own permissions allow, which the page says in one line.
6. **Preview.** The dry-run in words, for example: "14,210 files (96 TB) will be copied. From 4 December, 3,002 files (41 TB) will leave the NAS, about 120 a day." It has a per-rule table and sample paths, and for recalls: "Recalled files go back to their own folder; 212 of today's removed files would get a `-restored` name because the name is taken again."
7. **Start.** Copying starts at once. For a removing mode the page says: "Removals wait for an ArchiveAdmin's approval and a 7-day dry-run". The ArchiveAdmin is notified in Teams.

More settings: event source and its health, quiet period, grace period, removal rate and window, pointer files, scan schedules, deleted-file retention, bandwidth window, and the credential mode.

### 8.2 The Annex page

- **Header:** filer and path, mode, state, event source health, last full scan, removals running or paused (the switch is here), and the WAN estimate for what's left.
- **Timeline:** arrivals and departures per day (a bar chart, one colour each), with copies and removals overlaid. Clicking a day lists its files.
- **Rules:** the running rule set as the flow of §8.3, with hits per rule over the last 7 and 30 days, and any pending proposal with its dry-run.
- **Removals:** pending (file, rule, due date, "hold" button) and done (file, time, CRC, by which rule, "Recall"), plus cancelled ones and why.
- **Recalls:** in progress (the four recall steps plus writing and verifying on the target) and done (where each file landed, under which name, and who asked), plus failures with their reason (no space, quota, access denied).
- **Files:** search by path; each file shows its NAS state and the usual tier track, with a NAS stop first: NAS → Hot → Cool → Cold → Archive.
- **Problems:** drift from reconciliation, files skipped as in use, unreadable or hard-linked, and FabricPool/CloudPools warnings.

The **Shares** page doesn't list annexes, because they aren't OmniFile shares. The **Migrations** page lists them under their own heading next to S3 migrations, with copied, verified, removed and remaining.

### 8.3 The visual rule builder

One canvas shows both directions: **Out** on top, read left to right from the NAS to Azure, and **Back** below, read right to left from Azure to where a recalled file lands.

```
OUT   Files land in \\ontap01\geo\long-term-storage                      14,210 files · 96 TB
   ├─ 1. *.segy, ≥ 1 GiB, older than 30 days ─▶ copy, Archive policy ─▶ leaves NAS on day 30 ─▶ Hot → Cool → Cold → Archive
   │       9,880 files · 88 TB
   ├─ 2. surveys/**/scratch/* ────────────────▶ exclude ──────────────▶ stays on NAS only
   │       1,328 files · 2 TB
   └─ 3. everything else ─────────────────────▶ copy ─────────────────▶ NAS + Azure (Cool)
           3,002 files · 6 TB

BACK  When a removed file is recalled                                    (8,410 removed so far)
   ├─ 1. requester in GEO\Processing, ≥ 100 GiB ◀─ place in OmniFile share \\omnifile01\scratch\recalls ◀─ Archive
   │       hold 14 days · conflict: new version
   └─ 2. everyone else ◀─ back where it came from · conflict: -restored name · hold 14 days
           today: 212 names taken, 41 TB needed, 63 TB free on the NAS
```

- **Columns (Out):** "Where files land" → "If" (conditions as plain chips: "SEG-Y", "≥ 1 GiB", "older than 30 days") → "Then" (actions) → "Ends up" (NAS only, NAS + Azure, or Azure only). Tiers are drawn with WP13's tier-track component and policy timeline, so they look the same everywhere.
- **Columns (Back):** "From" (the tier the file is in, with its recall time) → "If" (requester, size, path) → "Place at" (a NAS folder, an OmniFile share folder, or "let the person choose" with the list), with the conflict choice and hold as chips.
- **Locations** are picked from lists of what exists (annexes, registered filers' folders, OmniFile shares, edges), never typed as free paths. A share the proposer can't write to isn't offered.
- **Live counts.** Each branch shows files and bytes from the dry-run, refreshed a second after an edit. A branch whose count drops to zero is greyed with "no files match". Back branches show today's name conflicts and the space needed against the target's free space.
- **Editing:** click a chip to change it; add a rule with "+ rule". Order matters and is shown by number.
- **Accessibility.** The diagram is drawn *from* an ordered list, which is the real editor:
  - every rule is a row with form fields;
  - "Move up" and "Move down" buttons reorder rows, as do Alt+↑ / Alt+↓;
  - drag is an extra, never the only way;
  - the diagram has a text equivalent (the rule table with counts);
  - count changes are announced politely (`aria-live`);
  - colour never carries meaning alone: removal branches also say "leaves NAS", and return branches say "placed at".
- **Safety in the builder:** removal branches are marked "leaves NAS on day N", the §4.2 checks show inline (including a return target that an outbound rule would empty again), and saving makes a proposal; it never applies anything.

### 8.4 Recalled copies in OmniFile shares: admin cleanup

A recall into an OmniFile share (§4.6) leaves a **recalled copy**: an extra copy of a file whose original is archived in the annex's dataset. The owner (2026-11-04): an admin can delete a recalled copy at any time, and the portal must show enough to decide when.

**Where and who.** A "Recalled copies" tab on each share in the Shares page, and the same list across shares under each Annex page's Recalls. ArchiveAdmins can delete any recalled copy; a share's Owners can delete those in their share.

**What each row shows:**

| Column | From |
|---|---|
| Recalled by, when, and why | The placement: the requester (by name, from their SID), the time, where the request came from (portal, Teams, an API app), and the request's note. The recall dialog gains an optional "Why do you need it?" field. |
| Source | The annex, the file's original NAS path, and the version recalled. |
| Use since the recall | Last opened, opens since the recall, and how many different people opened it. The edge counts opens (not bytes) of files that have a placement, over NFS and SMB, and reports hourly totals. |
| Changed since the recall | Yes if anyone wrote a new version in the share. It is then someone's work, not just a copy. |
| Size and tier | The copy's size, where its bytes are now (on the edge, or a stub with its Azure tier), and its monthly cost. |
| Is the original safe? | The archived original's state in the annex dataset: verified (CRC64-NVME), its tier, and WORM (locked until a date) or a legal hold. A red "not safe" means unverified, purged or a failed check. |
| Holds and pins | On the copy: the 14-day placement hold (until when), "Keep online", a legal hold, a tier pin, or the share's WORM retention. |

**Filters** combine, and a filter can be saved:
- recalled before or after a date;
- not opened for N days, or never opened since the recall;
- opens at most N;
- size at least N;
- annex, requester, changed since recall (yes or no), original safe (yes or no), hold active (yes or no).

The example the owner gave is one saved filter: "recalled more than 30 days ago and not opened since".

**Bulk delete:**
1. "Select all that match" selects every match, not just the page.
2. A preview shows how many files and bytes would be deleted, and how many are skipped, grouped by reason.
3. The admin confirms with the number shown. If the filter now matches a different set, the delete is refused, and the admin is asked to look again.
4. The delete runs as a throttled job with progress, and ends with a report (CSV) of every file and its outcome.

A single file has a Delete button with the same checks.

**What a delete does:**
- **It removes only the recalled copy:** the file in the share, its data on the edge, and its blob in the share's dataset.
  - The archived original in the annex's dataset is never touched.
  - The API takes only recalled copies (`origin = 'annex_restore'`), so it can't be pointed at an original.
- **The copy's blob is purged, not kept as a rewind version,** because the original is the same bytes (same CRC64-NVME).
  - The copy's catalog history keeps a link to the original.
  - Rewind views of the share from before the delete still list the file. Opening one reads the archived original, recalled the usual way.
- **It is skipped, with the reason, when:**
  - a hold, legal hold or pin is active on the copy. An admin can release the hold first, as its own audited action;
  - the share is WORM and the copy's retention hasn't ended (decision 31);
  - the copy changed since the recall. A bulk delete never removes someone's work. A single delete of a changed copy is an ordinary share delete (tombstone, versions kept), after a warning;
  - the original isn't safe: the recalled copy may be the last good copy;
  - the file is open on the edge right now. It is retried later in the job.
- **It is audited:** who deleted, when, under which filter or job, and per file the CRC and the original's location.
- **The requester is told** by Teams card: "Your recalled copy of line_1042.segy was removed by an admin. The archived original is safe; recall it again any time." This is on by default, with a switch per delete job.

## 9. Risks and owner decisions

### 9.1 Risks

| Risk | Mitigation |
|---|---|
| A wrong removal deletes the only copy of real data | Removal only after full verification, re-check at removal, rename-then-delete with a grace period, dry-run first, approval, throttle, pause switch, audit. The NAS's snapshots are a further net. |
| A file changes between verification and removal | Re-check of size, times, file id and change id. The rename step moves ctime, so the grace period watches for further change. |
| A scan or hint misses a change | Hints (SMB change notify) only trigger scans. Reconciliation scans are the record, and an injected missed change is part of A1's exit test. |
| 10^8-file scans over SMB take a day or more | Quick scans of changed directories, weekly full scans, parallel walkers, and a measured rate shown in the portal. Vendor feeds (A5) can cut this later. |
| Reading FabricPool or CloudPools data pulls it back | Detect and warn, and copy those files at a lower rate. |
| NTFS ACLs don't map to POSIX | Keep the raw descriptor for restores. The read set fails closed. |
| Removing files breaks users' work | Pointer files, Recall in the portal and on Teams cards, Teams notices from `notify` rules, and the 14-day hold after a placement. |
| A recalled file goes straight back out (a loop) | Provenance records, the hold, reuse of the verified copy, the save-time check and a circuit breaker (§4.6). |
| A placement lets a requester write where they couldn't | The service checks the requester's write access to the target against the catalog's folder ACLs; the file keeps its own descriptor, so it is never readable by more people than before. |
| A recalled file overwrites someone's newer work | Default conflict choice is a `-restored` name. Overwrite on a NAS only replaces an annexed file whose current version is already verified in Azure. |
| A partial or corrupt file appears under the real name | Temporary name, CRC64-NVME read back on the target, then an atomic rename that refuses to replace. |
| The NAS is full or over quota | Free-space check before writing; `ENOSPC`/`EDQUOT` cleans up and offers the other targets. |
| An admin deletes a recalled copy that is the last good copy | The delete is skipped unless the archived original is verified and safe (§8.4). |
| A hung NAS mount stalls the edge | Annex copiers run in their own unit with their own mounts. Inside the unit, a watchdog tells the service after `hang_s` and exits; in a fill the kernel may hold the process until the NAS answers (§3.2). |
| Vendor licence or trademark trouble | v1 uses only NFS and SMB, and says "works with NetApp ONTAP / Dell PowerScale" in plain words, never implying endorsement. No SnapMirror/SyncIQ target, no API emulation, no reverse-engineered protocol (§9.3). |

### 9.2 Owner decisions (2026-11-04)

The owner decided all 13 questions on 2026-11-04:

| # | Question | Decision |
|---|---|---|
| 1 | Who copies | **The edge**, as recommended. Storage Mover isn't used for annexes; decision 20 still covers restored S3 objects. |
| 2 | ONTAP's FPolicy | **No ONTAP-specific integration for now:** no FPolicy, no partner programme, no audit-log path. ONTAP is a generic NFS/SMB server, scanned and reconciled. FPolicy is a possible later addition. PowerScale is generic too in v1, with changelists a later addition (§2.1). |
| 3 | Pointer files | On by default for Archive after N days, off for Move now, switchable per annex. |
| 4 | Where a recalled file lands by default | Back where it came from (Copy, Archive after N days); a restore folder outside the annex (Move now). A `-restored` name on a clash; a 14-day hold. |
| 5 | Caution at the start | An ArchiveAdmin approves every removing rule set; a 7-day dry-run first; 1,000 removals an hour; 15-minute grace. |
| 6 | Deletes on the NAS in Copy mode | Keep the copy until an ArchiveAdmin purges it, with an optional per-annex retention, off by default. Reported, never mirrored. |
| 7 | Rule conflicts | First match wins within each trigger, with a default rule at the end of each. |
| 8 | Destination in v1 | Azure only. |
| 9 | API and AI proposals | Every proposal names a person who has access, checked at proposal and at approval; removing changes need an ArchiveAdmin. |
| 10 | Requester's choice of target | Only among the targets the rules list, with the requester's write access checked. |
| 11 | ACLs on a file placed elsewhere | It keeps its own captured descriptor and owner; in an OmniFile share, its catalog ACL. |
| 12 | Overwrite on a NAS | Only where the replaced file is annexed and its current version is verified in Azure; otherwise `-restored` names. |
| 13 | Recall into an OmniFile share | A server-side copy into the share's dataset, filled on the edge, **plus admin cleanup**: admins can delete recalled copies at any time, singly or in bulk by filter. The portal shows what they need to decide (§8.4). |

### 9.3 "Compatible with ONTAP and PowerScale": the honest answer

- **What it can mean:** OmniFile works *alongside* ONTAP and PowerScale, through NFS and SMB and their documented event features. It copies their files to Azure as plain blobs and can thin the arrays by rule. Users and the arrays' own tools are untouched.
- **What it can't mean:**
  - OmniFile isn't a SnapMirror or SyncIQ target;
  - it doesn't speak FabricPool's or CloudPools' formats;
  - it doesn't emulate ONTAP's or OneFS's management APIs.

  Those are proprietary. Emulating them invites licence and trademark trouble, and a vendor can change them under us at any time. An FPolicy server is a partner role: if we ever add it, we join NetApp's programme properly, or not at all.

## 10. Build plan and local tests

### 10.1 Phases

| Phase | Builds | Exit |
|---|---|---|
| **A1: Copy mode** | See the list below. | A Copy annex on a test Samba server and a test NFS server copies, recopies on change, never writes to the NAS, records deletes as tombstones, and finds an injected missed change by reconciliation. |
| **A2: Removals** | Archive after N days and Move now; the removal pipeline (§3.4) with dry-run, the 7-day shadow period, pause, throttle, audit and journal; pointer files; ArchiveAdmin approval | The removal race tests below pass. A killed daemon never removes twice or removes unverified. |
| **A3: Return path and recalled copies** | The return path (§4.6): the three return templates, placements, the requester's choice of target with the write check, conflict choices, the space check, read-back verification, ACL and owner restore, loop protection, and Teams cards. Recall into an OmniFile share, and the admin cleanup of recalled copies (§8.4): open counts from the edge, the Recalled copies tab, filters, bulk delete. | The placement race tests pass, and a killed daemon never leaves a partial file under a real name. A bulk delete never touches an original, a changed copy, a held copy or a WORM copy. |
| **A4: Rules** | The workflow model's full rule language (all triggers, locations and actions), its checks, proposals with owners, the dry-run preview for both directions, and the visual builder with Out and Back | Previews match actions on the same listing; API proposals without an owner with access are refused |
| **A5: Vendor feeds (later, owner's call)** | PowerScale changelists (and audit syslog as a hint); an FPolicy async server, only if the owner later joins NetApp's partner programme | Feeds reduce scan work; reconciliation still finds an injected missed event |
| **A6: Extras** | Read-only OmniFile share of a whole annex; a Storage Mover bulk seed; S3 destinations via DataSync if asked | Owner's call |

**A1, exactly.** Copy mode only, on any NFS or SMB server (ONTAP and PowerScale included, as generic servers). Nothing is ever written to the NAS.
- **Catalog:**
  - `catalog.filer`, the `nas` backend kind, `source.kind = 'annex'`;
  - `catalog.annex` (mode `copy` only), `catalog.annex_file`, `catalog.annex_event` with its daily partitions;
  - `catalog.annex_ruleset` holding only the fixed Copy template, approved by the annex's Owners. There is no rule editor yet.
- **Edge:**
  - the `omnifile-annex@` unit, with read-only NAS mounts using the machine account (SMB Kerberos, NFS `krb5`), and NFS `sec=sys` as the labelled fallback;
  - `annex.db`: the baseline scan, quick and full scans, and the SMB change-notify hint;
  - the quiet period with SEG-Y structure check;
  - copying through the existing uploader, re-stat after read, recopy on change as a new version;
  - ACL capture (raw descriptor and `read_sids`);
  - weekly reconciliation with drift rows;
  - events batched to the service.
- **Service:** verification by the in-Azure CRC read; tombstones for files deleted on the NAS, kept until an ArchiveAdmin purges them; the control-API routes for annex events.
- **API:** `/v1/filers` (register, health), `/v1/annexes` (create in Copy mode, settings), `/v1/annexes/{id}/events` and `…/files`. Every enum is open.
- **Portal:**
  - Create an Annex with steps 1, 2 (Copy only), 4, 5 and 7 (§8.1);
  - the Annex page header, timeline, files (with the NAS stop on the tier track) and problems;
  - the WP12 "A NAS share (Annex)" source card;
  - WP11 Settings: Filers, and Annex defaults for quiet period and scan schedules;
  - a recalled-copies list isn't needed yet.
- **Not in A1:** removals, pointer files, recalls back to the NAS or into shares, the `write_sids` set, the rule editor and dry-run preview, and any vendor feed. Recall of a Copy-mode file that the NAS lost comes with A3.

### 10.2 What can be tested locally

On a Lima VM acting as "the NAS": Samba joined to `omnifile-dc` for SMB and NTFS ACLs, and a kernel NFS server for NFSv3/v4. The kernel server is fine here because it is the customer's NAS, not our export. The edge runs in its own VM.

- Arrivals, departures and renames found by quick and full scans. Renames are told apart by file id.
- Quiet period: a slow writer is never copied mid-write. A file changed during its read is thrown away and recopied.
- Copy mode never writes: the share is mounted read-only, and a hash of the NAS tree is unchanged after a run.
- Removal races, each refused:
  - a file modified between verification and removal;
  - a file open over SMB;
  - a file renamed back during the grace period;
  - a hard-linked file;
  - a file with an alternate data stream;
  - a paused annex;
  - a dry-run annex.
- A daemon killed at each removal step resumes correctly from its journal.
- NTFS ACLs captured over SMB give the right `read_sids` and `write_sids` (allow minus deny). A placement puts the exact descriptor and owner back.
- Placements:
  - each conflict choice when the name is taken (same content, different content, recreated file);
  - a placement to a folder the requester can't write to is refused;
  - a full share or a quota (Samba `max disk size`, or an xfs project quota on the NFS server) fails cleanly with nothing left behind;
  - a corrupted temporary file (flipped byte) is caught by the read-back CRC;
  - a daemon killed mid-write leaves only a temporary name, which is cleaned up on resume.
- Loops: a file placed back into an Archive-after-N annex is a `returned` event, is held, and isn't recopied; a Move-now folder as a return target is refused at save; the circuit breaker fires on a forced loop.
- Recalled-copy cleanup: open counts arrive from both NFS and SMB; a bulk delete by "recalled more than 30 days ago and not opened since" removes exactly the matching copies, and skips held, WORM, changed copies and copies whose original isn't safe; the archived original's blob and catalog rows are unchanged; a rewind view from before the delete still opens the file from the original; a count change between preview and confirm is refused.
- Pointer files are written, ignored by scans, updated when a file is placed elsewhere, and removed when it is back under its own name.
- **Placements over NFSv4, partly.** The exit test's NFSv4 stage runs on NFS-Ganesha with FSAL_VFS and `Disable_ACL = false`: a real NFSv4.2 mount, `system.nfs4_acl` read and set, and the lock read back. That server derives its ACLs from the mode and any POSIX ACL, so it doesn't store an NFSv4 ACL as written. **Still for a real NAS** (with knfsd, ONTAP and PowerScale each): whether the lock reads back within `NFS4_NO_DATA_ACCESS`, whether a named `read_nfs4_who` is kept, whether inherited entries reach a new staging file, and `sec=krb5` on the placement mount.
- SMB change notify triggers a quick scan; an overflow falls back to a scan.
- Machine-account Kerberos mounts for SMB and NFS `krb5` against the test DC.
- Scale: scan rate at 10^6 files on the VM, to calibrate the 10^8 estimate.
- (A5, later) Vendor feeds against fakes only: a fake OneFS Platform API serving changelist pages in the SDK's documented shapes.

**Needs a client site:** real ONTAP and PowerScale exports (scan rates at 10^7+, FabricPool and CloudPools behaviour, machine-account mounts), and WAN throughput over the client's ExpressRoute.
