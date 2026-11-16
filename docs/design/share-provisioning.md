# Adding datasets and shares (M6)

**Status: BUILT (2026-10-16).** `docs/BUILD-PLAN.md` M6 asks for shares to be added in the portal, with the edge rendering them. Before this, datasets and shares were made only by SQL in tests and fixtures, and the e2e lanes rendered each share's Samba stanza and Ganesha block by hand with `omniacl render`.

## The flow

1. An ArchiveAdmin presses **Add share** on the portal's Shares page. Plain questions come first:
   - the name;
   - which edge and which storage (a new dataset), or an existing dataset with no share;
   - who connects (Windows SMB, NFSv4, NFSv3);
   - how NFS clients prove who they are: Kerberos, with the throughput note, or named hosts with `sec=sys`;
   - the Owners, Writers and Readers AD groups;
   - what happens when someone opens an archived file.

   Every other knob is one click down: SMB encryption, rewind depth, storage layout, key prefix and the host-trust override.
2. The portal calls `POST /v1/datasets` (when the dataset is new), then `POST /v1/shares` (`portal-api.md` §5.16).
   - The service checks every rule the edge will check again (`access-control.md` §3), so a share it accepts is one the edge serves.
   - It creates the dataset with its root folder, and the share on the dataset's root, in the dataset's edge. Both are audited.
   - If the share fails after its dataset was made, the portal keeps that dataset for the retry.
3. The catalog's triggers put a `dataset.upsert`, a `file.upsert` for the root, and a `share.upsert` in the edge's feed, in that order.
4. The edge's replica takes them. The daemon makes the root folder under its data root, named after the share (`<data root>/<share name>`).
5. The daemon's share loop (`omnifile_edge::shares`, `[shares] render = true`):
   - maps each `share.upsert` row to an `omnifile_acl::ShareConfig`;
   - validates it, and leaves out (and logs) a share it can't serve;
   - renders `[shares] smb_conf` and `ganesha_conf` (by default `/var/lib/omnifile/shares/smb.conf` and `ganesha.conf`), shares in name order;
   - replaces each file atomically, and only when its content changed;
   - when either changed, runs `[shares] reload`: `systemctl try-reload-or-restart smbd.service nfs-ganesha-omni.service`. Both units reload on SIGHUP, and a stopped unit stays stopped. A failed reload is retried. It also reloads once at every daemon start: the servers outlive the daemon, so a reload its predecessor owed (it wrote the files and crashed, or the reload failed) is never lost;
   - when an existing export's rewind area changes (`Snap_Dir`, from a rewind depth set or cleared), runs `[shares] restart` (`systemctl try-restart nfs-ganesha-omni.service`) instead. `FSAL_OMNI` keeps the `Snap_Dir` an export was made with, because lookups read it without a lock, so a reload doesn't change it; NFS clients ride the restart's grace period. The restart owed is recorded beside the file before the file changes, so a crash still restarts;
   - gives the share root its groups' ACL (`omnifile_acl::apply_root`). When the groups change, the old groups' entries are replaced and Explorer's own edits are kept.
     - Owners must map, through winbind, for anything to be applied.
     - A Writers or Readers group winbind can't map yet is left out and retried. It never holds up the rest: the group it replaces loses its entries at once, so the root gets fewer rights, never more.
     - The gids applied are kept in `share-acls.json` beside the replica. The record is trusted only while the root still has them: every pass checks the root itself (`check_root`, and that its group is Owners), so a root made again, or one whose ACL or group was changed behind the daemon's back, is given them again.
     - A changed root is reported in the next ACL scan, and the rewind trees take its new ACL.
   - The materialiser keeps a dataset root's owning group, because the edge sets it to the share's Owners group and the catalog doesn't carry it. Every other folder follows the catalog.

   The loop runs at start and whenever a share row's version changes. It retries every 30 s while a step is left: a root not yet on disk, a group winbind can't map yet, or a failed reload.

## Includes

The appliance's server configs include the rendered files. The daemon's unit is read-only outside `/var/lib/omnifile`, so it never writes `/etc`.

| Server | Main file (setup wizard) | Includes | Which includes (daemon) |
|---|---|---|---|
| smbd | `/etc/samba/smb.conf` (`edge/config/samba/smb.conf.tmpl`) | `/etc/samba/omnifile-shares.conf` | `/var/lib/omnifile/shares/smb.conf` |
| Ganesha | `/etc/ganesha/ganesha.conf` | `omnifile-base.conf`, `/etc/ganesha/omnifile-exports.conf` | `/var/lib/omnifile/shares/ganesha.conf` |

The wizard creates both rendered files, empty, so the servers start before the daemon's first pass: Ganesha won't start on an `%include` of a missing file.

## What the catalog carries for it

Migration 0022 adds two columns to `catalog.share`:

- **`nfs_export_id`**: Ganesha's `Export_Id`.
  - NFS clients keep it in their file handles, so it never changes for a share. A trigger refuses an update.
  - It is assigned on insert: the next free id on the edge, from 2 (0 and 1 are Ganesha's), under a per-edge advisory lock. It is unique per edge.
  - Shares from before the migration were numbered in `share_id` order.
- **`smb_encrypt`**: SMB3 encryption required.

Migration 0050 adds **`quiet_s`**: how long a file must sit untouched before it counts as finished, or null for the share's default (decision 67, `edge-metadata.md`). It is changed with the share's settings, not when the share is made.

`share.upsert` now carries `nfs_export_id`, `smb_encrypt`, `host_trust_override` and `quiet_s` (`control-api.md` §5). A change to `quiet_s` doesn't touch the rendered config, so it reloads nothing. An edge whose service doesn't send `nfs_export_id` (an older service) logs the share as not served, rather than inventing an id.

## Choices

- **Share names are unique per edge regardless of case** (migration 0022, `lower(name)`): Samba and Windows match share names regardless of case, so `Data` and `data` would be one SMB share with two definitions. The edge renders only the older of two such names, as a backstop.
- **A reload is not proof that Ganesha took an export.** SIGHUP always succeeds, and Ganesha only logs an export it rejects. The service checks every rule first, and the edge validates each share again, so a rejected export means a bug; checking Ganesha's export list over D-Bus is left for later.
- **A folder is the root of at most one share** (migration 0023). A dataset's root share names the root folder on the edge (`Replica::root_name`), so two shares of one root would fight over it.
- **Shares of folders** (`POST /v1/shares` with `path`, e.g. `/projects/2026`): an existing folder of a dataset, once the dataset's root is shared, because the root share names the folder the others live in. A folder share has its own groups, open policy and rewind; its root gets its groups' ACL like any share root, and the materialiser keeps its owning group (`PendingOp::share_root`). Files under it belong to it, the nearest share above them (`Replica::share_of`): its open policy for SMB reads, its share id in their markers. Over NFS a file follows the export it was reached through, since `FSAL_OMNI`'s policy is per export: reached through the root share, a file in a shared folder takes the root share's policy.
- **Removing a share** (`DELETE /v1/shares/{share_id}`) removes only the share. The dataset, its files and their versions stay, and a new share can serve them again.
  - The edge gets a `share.delete` (migration 0023's trigger), drops the share's stanza and export, and reloads. The folder stays on disk.
  - Never a WORM share: its retention protects what it holds (decision 31).
  - Not a root share while folder shares of the dataset exist, because they need its folder on the edge.
  - The share's daily usage rows go with it.
- **S3 is never a dataset's home** (decision 18). `GET /v1/backends` marks it `can_be_home: false`, and `POST /v1/datasets` refuses it: S3 data comes in as a migration source (§5.10).
- **The edge doesn't know restricted folders.** The service checks them when the share is made, and records the override. The edge renders with `restricted = false`.
- **`vfs_omnifile_prevver`** goes into an SMB stanza only for a share with a rewind area, and only where smbd's modules directory has it **and its build info matches the running Samba** (`[shares] smb_prevver = "auto"`). A module built for another VFS interface is left out and logged as an error every render, so the share still connects; `samba-vfs/README.md` has the load check.
- **Every SMB stanza** sets `store dos attributes = yes`, which carries the offline bit stubs get (`[shares] smb_offline`, on by default; `omnifile_marker::dosattr`), and `csc policy = disable`, so Windows Offline Files never caches a share of stubs.

## Tests

- **Service:** `service/tests/web/test_provision.py`:
  - backends, and where a dataset may live;
  - a dataset with its root;
  - a share, and the feed row the edge gets;
  - export ids, which never move;
  - every rule the edge checks again;
  - the host-trust override;
  - roles.
- **Portal:** `portal/src/features/shares/addShare.test.tsx`:
  - the button is for ArchiveAdmin only;
  - the Kerberos note;
  - `sec=sys` needs hosts;
  - the requests;
  - the retry reuses the dataset;
  - S3 is never offered.
- **Edge:**
  - `shares.rs` unit tests: a row becomes the share the edge serves; what it refuses; SMB-only shares; files replaced only when they change;
  - `tests/daemon_linux.rs::a_share_from_the_feed_is_rendered_reloaded_and_its_root_given_its_groups`, on xfs with the fake service:
    - the share from the feed is rendered and the servers reloaded;
    - the root gets its groups' ACL, with Owners as its group and no rights for the owning group;
    - a pass with no change reloads nothing;
    - new readers and SMB encryption re-render, reload and move the ACL;
    - an edit that leaves the groups alone keeps the root's group;
    - readers replaced by an unmapped group lose their entries at once;
    - a rewind depth from 0 restarts Ganesha;
    - a root that lost its ACL and group is given them again;
    - a share the edge can't serve is left out and the other stays;
    - a restarted daemon reloads once.

    Each step was checked to fail without its fix.
  - `omnifile-stub` `a_share_root_keeps_the_group_the_edge_gave_it` (xfs): the materialiser keeps the group of a dataset's root and of a shared folder, and a folder that isn't shared follows the catalog.
  - The daemon test also shares a folder (rendered at the folder, given its groups), then removes it with `share.delete` (no longer served, reloaded, the folder stays).
- **Service:** `test_a_folder_is_shared_once_the_root_is`, `test_removing_a_share_keeps_its_data_and_tells_the_edge`, `test_a_worm_share_is_never_removed`.
- **Ganesha:** `fsal/tests/t_reload.sh` checks the reload the design relies on, against a running `ganesha.nfsd` V15.6 over NFSv3 and v4 (13 checks): an `EXPORT` added to an included file is served after SIGHUP, one removed is gone after the next, the others keep serving, and Ganesha stays up.
- **End to end** (`tests/e2e/m5_exit_test.sh`, step 9, on the M1 stack): the daemon renders the exports, under its real unit, and Ganesha includes them. The seeded share is served from the rendering. A share added in the catalog (`e2e.py add-share`, the rows `POST /v1/datasets` and `POST /v1/shares` make; the M1 stack runs no portal) is rendered with its export id, reloaded, and served over NFSv3 and v4, and the other share keeps serving. A removed one (`remove-share`) is no longer rendered or served, and its folder stays. Ganesha stays up throughout. 56/56 with the rest of the M5 test (2026-10-16).
- **smbd:** Ubuntu's `smbd.service` reloads with SIGHUP (`ExecReload=/bin/kill -HUP $MAINPID`), which re-reads `smb.conf` and its includes.
- **Wizard:** `omnifile-setup` tests: `edge.toml` turns rendering on, the includes point at the rendered files, and the empty files exist.
- **Appliance** (`appliance/tests/setup.sh`, 36/36): on the real image, both servers include the rendered files, and `smb.conf` with the include parses. The daemon's own unit, sandbox included (`ProtectSystem=strict`, its capabilities and syscall filter), runs the `[shares] reload` command against PID 1: an active unit is restarted, and stopped ones stay stopped.
