# Manual tiers, storage versioning and WORM

**Status: ACCEPTED (2026-10-05), decisions 29–31.** Owner requirements:
- set a file's tier from the gateway's web page;
- "storage versioning": rewind to an earlier date to see a file, back to a depth the share's owners choose;
- treat the solution as a WORM location, with WORM configurable per share in the gateway.

The owner chose versioned WORM (overwrites make new versions), compliance-locked retention, and depth set by share Owners.

The Azure facts below were read on 2026-10-05 from Microsoft's immutable-storage overview (updated 2026-08-25) and smart-tier page (updated 2026-08-12).

## Azure facts that shape the design

| Fact | Consequence |
|---|---|
| Under a time-based retention policy or legal hold, blobs "can be created and read, but not modified or deleted". | The current Smart → Archive move (copy to the archive account, then **delete** the original) **can't run on WORM data** until retention expires. |
| "All blob access tiers support immutable storage. You can change the access tier of a blob with the Set Blob Tier operation." | WORM data **tiers in place** with Set Blob Tier, including to Archive. |
| "For a write to succeed, you must either have versioning enabled or have neither a legal hold nor a time-based retention policy." | With **versioning on**, an overwrite under retention succeeds as a new version, and the old version stays immutable. This is native versioned WORM. |
| Version-level WORM needs versioning, and supports per-version retention and legal holds. A locked policy can be extended but never shortened or deleted. | Matches the owner's choices: per-file legal holds (already in `AGENTS.md`) and compliance-locked retention. |
| Version-level immutability blocks **Blob Inventory** on that account. | Nightly reconcile for WORM accounts uses the change feed and listings, not Inventory. |
| Immutability is **incompatible with point-in-time restore and last-access tracking.** | We don't use either; our `clock = last_read` comes from edge and catalog reads. |
| Smart tier needs ZRS, has no Archive, and blobs given an explicit tier "can't be tiered back to smart". | Manual tier changes take a blob out of Smart permanently. WORM data that must reach Archive can't use Smart. |
| Whether Smart tier works on accounts with immutability or versioning is **not stated**. | VERIFY in pilot 4. Until then, WORM shares use scheduled tiers. |
| Immutable storage is assessed (Cohasset) for SEC 17a-4(f), FINRA 4511 and CFTC 1.31. The policy must be **locked** to count. | Compliance mode locks the policy after a short test window. |

## Decision 29: set the tier from the gateway page

- **Who:** members of the share's **Owners** AD group, and `DataSteward`s (decision 28).
- **Where:** in the portal file browser, select files or folders and choose **Hot / Cool / Cold / Archive**, or **Keep online until \<date\>**. (The Teams "Keep online" button is the same action for one file.)
- **How it works:** the choice is stored as a **tier pin** (tier, optional until-date, who, why) that overrides the policy schedule while it is active. When it expires, the policy takes over again.
  - The **archive date stays respected**: a pin to a warmer tier never pushes a file past its archive day unless an Owner explicitly says so.
  - Pins **flow through the backend**: `set_class()`, with the S3 equivalents for S3-resident files during migration.
- **What the screen warns before applying,** using `capabilities()`:
  - early-deletion and minimum-duration fees;
  - "this removes the file from Smart tier permanently";
  - Archive recall time, with no promises above 10 GB;
  - the cost change per month.
- **On the edge:** a pin to Archive makes the file a stub, per the share's `stub_at` rule. A pin to Hot from Archive starts a recall.

## Decision 30: storage versioning and rewind

- **What a version is:** each **committed** write is a version. Committed means the share's quiet period passed after close (decision 67: 60 s by default on a WORM share, `edge-metadata.md`). Intermediate writes before commit aren't versions.
  - Overwrite → new version.
  - Delete → tombstone (the file disappears from the live view, and its versions remain).
  - Rename → a new path that keeps the version history.
- **Depth:** set per share by the **Owners** group (`rewind_depth_days`), within a maximum the `ArchiveAdmin` sets.
  - Versions older than the depth are pruned automatically by a lifecycle rule plus the catalog.
  - On a WORM share, versions are kept at least until their retention ends, whatever the depth.
- **Storage:** Azure **blob versioning** keeps every version under the file's own blob name, so the blobs stay plain. The **catalog** keeps each path's version history (when, size, hashes, version id, tier, location), because a file's versions can sit in different tiers and, for non-WORM shares, different accounts. The catalog answers "this folder as of date T".
  - Native-features rule: native versioning is used for storage. The as-of view lives in the catalog because native versioning can't follow a blob across our two accounts or into S3.
- **How users rewind:**
  1. **Portal (M4):** browse any folder "as of" a date and time inside the depth. Open, download or **restore** a version. Restoring creates a new current version, or writes to another path.
  2. **NFS and SMB (M5, with the R1 namespace):** read-only point-in-time trees, as NFS `.snapshots/<date>/…` and Windows **Previous Versions** using Samba's `@GMT-` token convention. The exact mechanism is part of `docs/design/fsal-namespace.md`.
  - Old versions usually sit in cool or archive tiers, so opening one can start a recall, with the same wait/fail policy, note file and Teams card.
- **Access:** rewind respects decision 28. A user sees an old version only if the ACL **at that time** let them read it (from the ACL snapshot history), **and** they can read the folder now.
- **Cost:** the policy editor shows the extra storage for the chosen depth, from change rate × size. Versions are billed at full size.

## Decision 31: WORM, configurable per share in the gateway

- **Switching it on:** an `ArchiveAdmin` turns WORM on for a share in the portal and sets **retention days**.
  - It is **one-way**: once the policy is locked, the share can't go back to non-WORM. A new share is needed for mutable data.
  - The portal says this plainly, and requires typing the share name to confirm.
- **What WORM means here:**
  - Every committed version is immutable for `retention_days` from its commit.
  - Overwrites and deletes are allowed, but only add a new version or a tombstone (decision 30). Nothing committed is destroyed before retention ends.
  - **Legal hold** per file or folder keeps versions beyond retention until it is cleared.
- **Compliance mode:**
  - The Azure policy is created unlocked for a short validation window (hours, at most 24 h, as Microsoft recommends), then **locked automatically**.
  - Once locked, retention can be extended but never shortened, and nobody can delete early.
  - The lock deadline is shown in the portal.
- **Storage layout for WORM shares:**
  - One storage account for the share's data, **LRS or GRS** (Archive isn't allowed on ZRS), with versioning and **version-level WORM**: a policy on the container, plus per-version retention and holds.
  - Tiering runs **in place** with Set Blob Tier, never by copy-and-delete. A file that leaves a shared blob for its own (below) copies, and deletes nothing: its old row stays as a retained secondary until retention ends.
  - **Smart tier isn't offered** for WORM shares until pilot 4 confirms Smart works with immutability, and even then it can't be combined with Archive.
  - The policy editor enforces this.
- **Recall:** a rehydrate copies the archived version into a separate, non-WORM **recall container**. The archived original is never touched. The recall copy is deleted after the user's window, which WORM allows because that container has no policy.
- **The edge:**
  - The cloud copy is the compliance record; the edge is a cache.
  - The edge must never lose a committed version. An overwrite that arrives before the previous version finished uploading still uploads the previous version first: the landing zone keeps it until it is uploaded and verified.
  - Local deletes and overwrites just change the live view.
  - The stubber may punch holes in committed files, because the bytes live in the immutable cloud version. So the edge does **not** use `chattr +i` on WORM shares.
- **Audit:** every WORM enable, lock, retention extension, legal hold set or clear, and tier pin is recorded with the AD identity. Azure's own policy audit log covers the container policy.

## Decision 66: files that share a blob and want different tiers

Whole-file dedupe gives two files one blob, and one blob can be in one tier. Built in `worker/tiering.py` and `policy/engine.py`.

- **When two files want the same tier.** A file goes along when another moves the blob to a tier its own plan (schedule, pin and archive date) has it in now. For a colder move, also:
  - a file with no pin in force whose schedule reaches that Cool or Cold step within **7 days**. It goes up to that much early. That costs a dearer read on a file still online, and nothing else, while a copy would cost a blob for good;
  - a file with no pin in force whose **archive date is today (UTC)**. A file is never archived on a date before its own, because that takes it offline.

  A pin says what it wants now, however soon it ends. A move is never put off to agree with another file's plan. A file with no tiering policy wants nothing, so it never causes a copy.
- **While a split runs,** other moves of that blob wait, because it decided on what the blob was and its copy is reading it. A move to Archive waits only about 10 minutes, then takes its own copy from the readable blob, so an archive date isn't met by archiving another file's bytes. Any other move waits for the split to end, and writes one audit line saying so. A copy into Archive ends by `OWN_COPY_DEADLINE`, 24 hours of reachable time in the window recalls use (migration 0040), in every phase, throttled or stuck behind `verify` included: past it the blob moves, because the date wins. A copy for any other move carries on, since no date is at stake. A file past its archive date alerts while it is overdue (`tier-archive-overdue`, a bounded count from `file_archive_due`), and any archive that lands after its date is audited (`tier.archive.late`) and alerted on, unless an Owner's pin held it there.
- **Otherwise the moving file gets its own blob, and only its row changes.** The files that stay keep the original, whose tier and clock don't change. So a split bills no early deletion while another file stays on the blob, whichever way the wishes point. Leaving costs one read of the blob's tier: nothing out of Hot, a retrieval out of Cool or Cold. When nobody stays, the first to move copies, and the other has the blob to itself. Two limits: if every other file leaves during a copy, the original's delete bills what moving it in place would have (and a file that finds nobody left before its copy starts just moves in place); and a copy dropped after it reached its tier, which only a version pruned during the split causes, bills its own early deletion.
- **One blob per wish, not per file.** A file about to copy first looks for the same bytes already in the tier it wants (BLAKE3, size and CRC64, the dedupe's test), and joins them. Not a retained version: nothing of it may be retired, and a new reference couldn't be stamped true on a blob whose retention differs.
- **The copy** is Archive's: a server-side copy, landing in Hot; `verify` reads it back in full and compares CRC64-NVME; then Set Blob Tier; then the record, under the blob's lock. The original stays until then. `delete_blob` keeps it while another row uses it.
- **WORM.** The new blob carries the version's retention and legal hold before it becomes the primary (`stamp_worm`). The old row becomes a retained secondary, through the swap a migration's cutover uses (0027), and its blob stays until retention ends. It has no plan of its own, so it asks for no copy and holds nothing back. A copy that fails its read-back on a retained version stays too, unused, because nothing of a retained version is deleted. A daily sweep (`replaced_secondaries`) retires both once retention ends, through `delete_blob`, which keeps the bytes while another row uses them. It also reclaims a copy whose message was lost, a day on. A blob's retention is never shortened: a row whose retention has ended stamps nothing, and a row with an earlier date than the blob's leaves the blob's. Clearing one file's hold leaves another file's on the same blob.
- **A blob in Archive is never copied.** That would be a rehydrate, which only a person starts. A warmer pin on a stub starts the recall it always did, and the blob stays.
- **If the copy can't be made** (no write rights, three failed tries, a bad copy, no CRC to check it against, retention that won't stamp, or the deadline), the audit says why. **Only an archive date that has come may then move the blob over a file that differs,** because a date is always respected. A pin to Archive doesn't: it's a person's choice about their own file, and they may not be able to read the others, so it waits like any other move until the file's own date. Each file a date goes over gets `forced` in its audit row, a counted alert (`tier-forced-archive`), and its pin's setter a Teams card, with email as the fallback. **Any other move changes nothing,** and tries the copy again a day later: a schedule running late with an alert (`tier-held-back`, which keeps firing and says what fixes it) beats a pin going false and a fee billed. Files that agree, and blobs with one file, move as they always did.

- **The sweep behind a split** (`retire_replaced_secondaries`) runs daily, once across replicas, as messages on `retention` that follow each other a page of 500 secondaries at a time by a partial index, so a backlog clears in one go, and its last page records the pass. A secondary's delete heads its version's primary first (`copy_mismatch`): if that's missing or differs, it refuses, audits and keeps the blob, so an automatic delete can't take the last bytes. **Stamps of one blob take turns** on its advisory lock, held at session level so no transaction id crosses a provider call. No row writes `omnifile_hold=0` on a blob another file holds.

## What changes elsewhere

- **The anchor:** the two-account Smart → Archive design applies to **non-WORM** shares only. WORM shares tier in place in one LRS/GRS account. Nightly reconcile on WORM accounts uses the change feed and listings, not Blob Inventory.
- **Backend contract** (`ARCHITECTURE.md` §3) gains:
  - `list_versions(key)`;
  - `get_range(ref@version, …)`;
  - `set_retention(ref@version, until)`;
  - `set_legal_hold(ref@version, on)`;
  - in `capabilities()`: WORM support, in-place archive and a versioning flag.

  S3 Object Lock (compliance mode) is the S3 equivalent, but S3 is only a migration source in v1, so it is noted, not built.
- **Catalog:** tier pins, a `file_versions` table with as-of queries, per-share `rewind_depth_days`, WORM settings and lock state, and per-version `retention_until` and `legal_hold`.
- **Build plan:**
  - M3 builds versioning, WORM storage and in-place tiering.
  - M4 builds the tier controls, the rewind browser, and the WORM and depth settings.
  - M5 builds the NFS and SMB point-in-time trees.

## To verify

- Smart tier on an account with versioning and immutability (pilot 4).
- That Set Blob Tier to Archive and back works on locked version-level WORM blobs as documented, including early-deletion billing when a version leaves Archive within 180 days (pilot 4).
- Samba Previous Versions without a snapshotting filesystem: whether a virtual `@GMT-` tree is enough (M5).
