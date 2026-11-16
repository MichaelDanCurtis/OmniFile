# Migration namespace: one NFS view over S3 and Azure

**Status: ACCEPTED — in v1 (decision 19, 2026-09-26).** Decisions 14–16 and 19–20 in `docs/DECISIONS.md` answered all five open questions at the end of this doc.

> **Reframed the same day:** OmniFile is provider-agnostic (decision 13), so this feature is a special case of the core design. A dataset is bound to a backend, and during a migration that binding changes from S3 to Azure, prefix by prefix, under a share that never moves. The S3 backend built for this feature is the same one S3-resident clients use permanently.

## The idea

While Prizmanyx moves a client's files from S3 to Azure Blob with Azure Storage Mover, OmniFile Gateway presents **one NFS share** over the whole library. When someone reads a file, OmniFile checks where its bytes are *right now*:
- still in S3 → stream it from S3;
- moved and verified in Azure → stream it from Azure.

If the file is archived on either side (S3 Glacier / Deep Archive, or Azure Archive), the gateway uses the same behaviour as the core product: NFS "wait" or fail fast, a note file, and a Teams card.

## Verdict: yes — and it is the strongest reason to install OmniFile *before* cutover

- **Users mount one path on day one and never re-point anything.** Cutover stops being an event users experience. For each prefix it becomes a catalog update ("this prefix now reads from Azure").
- **It reuses nearly everything the core product needs:** catalog, identity chain, Teams recall cards, policies, portal.
- **One restore can serve two purposes.** A user reading a Glacier object triggers an S3 restore. If that restore is registered with Prizmanyx's restore plan, Storage Mover can copy the object while the restored copy is live.

## How it would work

1. **Seed the catalog from S3.** Use the S3 Metadata live inventory table (Iceberg, read with PyIceberg/DuckDB — see Prizmanyx's storage-profiler research), or S3 Inventory Parquet. Each row gets a path, size, ETag, storage class, last-modified time and `location = s3`.
   - The S3 Metadata **journal** table keeps the catalog current with near-real-time changes.
   - Never LIST S3 on a directory read. Listings come from the catalog.
2. **Move the bytes.** Prizmanyx runs Storage Mover jobs per prefix. Storage Mover reports only counters, not per-object completion events. So **Prizmanyx verifies each prefix** (list the Azure container, compare size and MD5 where comparable, and check the S3 journal for changes made after the copy). It then calls OmniFile's API to set that prefix's `location = azure (verified)`. Until that call, reads go to S3.
3. **Read path.** A new FSAL (`FSAL_OMNI`) resolves each path through the catalog to a backend.
   - It streams byte ranges: S3 `GetObject` with `Range`, or Azure `Get Blob` with `x-ms-range`.
   - It reads ahead and keeps a local SSD chunk cache. Sequential SEG-Y reads never need a full download first.
   - Headers are served from a resident-header cache (the first few MB of each file), so tools that scan thousands of files don't cause thousands of downloads.
4. **Archived data, either side.**
   - **S3:** GLACIER, DEEP_ARCHIVE and the Intelligent-Tiering archive tiers need `RestoreObject`. Glacier Flexible takes 3–5 h Standard or 5–12 h Bulk; Deep Archive takes ≤ 12 h Standard or ≤ 48 h Bulk. Glacier Instant Retrieval needs no restore.
   - **Azure:** Archive needs a Copy Blob rehydrate.
   - Both use the same per-share wait / fail-fast / wait-if-quick policy, the same note file and the same Teams card.
5. **Writes.** Proposal for v1, detailed in the next section: data still in S3 is read-only through the gateway, and new files go to Azure only.

## Write semantics (decided 2026-09-26)

A dataset is in one of three states: **steady** (one backend), **migrating** (source → destination), or **cut over**. Migration can run in either direction.

While a dataset is **migrating**, every new write goes to the **destination**:

| Operation | File still at the source | File already at the destination |
|---|---|---|
| Create | Lands locally, then goes to the destination | Normal |
| Overwrite | A new version is written at the destination; the catalog points there; the source copy is left for reconcile | Normal |
| Delete | Hidden in the catalog (tombstone); reported for the decommission, never applied to the source (decision 40), which is read-only to OmniFile (`catalog-seeding.md` section 10) | Normal |
| Rename | Metadata-only in the catalog, materialised at the destination when the file moves (v1 may refuse it instead) | Server-side copy + delete |

## Restores trigger migration (decided 2026-09-26)

OmniFile always holds permission to request archive restores (S3 `RestoreObject`; Azure rehydrate). When a restore completes, or when one is scheduled, OmniFile can trigger migration of the restored objects, so one restore serves both the user who asked and the move.

**Who copies the restored object: Azure Storage Mover (decision 20).** OmniFile creates the Storage Mover job and monitors it. The copy must finish before the restored copy expires (`Days`). The verification pass (`docs/research/2026-09-26-verification.md`) found that Storage Mover copies restored Glacier objects. Decision 24 covers the gaps it found:
- **Feeding jobs.** Jobs take one prefix, not a list of objects.
  - A bulk migration reruns each prefix in Additive mode after every restore wave. Unrestored objects show up as failed or unsupported until they are restored.
  - A user-triggered recall that can't wait for the next rerun is copied into an S3 staging prefix first (S3 Batch Copy), and Storage Mover runs on that prefix.
- **Verification, per file, before cutover.**
  1. Storage Mover's job counters only say the job ran.
  2. For each file, the service reads the new blob inside Azure (no egress; still hot, so no retrieval fee) and computes CRC64-NVME.
  3. It compares that with S3's full-object CRC64NVME from Batch Compute checksum, which works at rest, even on Glacier.
  4. A match stamps the tier (only when the policy is not Smart), POSIX metadata, index tags and the catalog checksum.
  5. A mismatch requeues the file and alerts.
  6. A prefix flips to `location = azure (verified)` only when every file in it has matched and the S3 journal shows no change since the copy.
- **Status.** Storage Mover reports per job; per-object completion comes from the Blob change feed and the verifier.
- **Private networking.** The customer creates the storage account's private endpoint (decision 22). The pilot checks whether Storage Mover, a Microsoft service rather than traffic through that endpoint, can still write when public network access is disabled.
- **Direction.** v1 is S3 → Azure only (decision 23), so Storage Mover's inability to copy Azure → S3 doesn't matter yet.

## Hard parts

- **Consistency.** A file can change in S3 after Storage Mover copied it. S3 stays authoritative until a prefix is verified. Verification must check the S3 journal for changes made after the copy started.
- **Egress costs.** On-prem reads from S3 pay AWS internet egress (~$0.09/GB for the first 10 TB a month); reads from Azure pay Azure egress. The chunk cache matters.
  - Whether AWS's free-egress credits for leaving AWS cover user reads during the migration window is **UNVERIFIED**.
  - Once a prefix is verified in Azure, prefer the cheaper side.
- **AWS credentials on-prem.** Use **IAM Roles Anywhere** (X.509 certificate → temporary credentials), not static keys. Reads need `s3:GetObject`. Triggering restores needs `s3:RestoreObject`, which is a write in the client's AWS account, so it needs explicit consent. *Superseded by decision 43: no keys anywhere. The service uses web identity from its Azure managed identity; the edge uses Roles Anywhere with its key in the TPM.*
- **POSIX metadata.** S3 objects carry no uid, gid or mode. Use per-dataset defaults from policy (owner group, mode), or read them from object metadata if an earlier tool stored them. Permission checks are enforced at the gateway.
- **Latency.** S3 first-byte latency from on-prem is tens of milliseconds. Sequential reads are fine with read-ahead. Many small random reads (header scans) rely on the resident-header cache, which costs a little egress to warm.
- **SMB.** Samba needs a local filesystem. A virtual S3/Azure namespace over SMB would need a Samba VFS module (GPL) or a FUSE layer. The owner has scoped this feature to **NFS first**.

## Architectural consequence: decide early

This feature moves the centre of the design. `FSAL_OMNI` becomes a **virtual filesystem over three backends** (local landing zone, Azure Blob, S3) with a chunk cache, rather than a stub guard on top of local xfs. fanotify remains only for SMB and local-filesystem stubs.

It is cheaper to build the FSAL this way from the start than to retrofit it. The filer spike in `docs/PILOTS.md` should test a backend-agnostic FSAL.

## Division of labour with Prizmanyx

- **Prizmanyx owns** movement (Storage Mover), verification and the restore plan.
- **OmniFile owns** presentation (the namespace), user identity, notifications and recalls on the user's behalf.
- **API additions:**
  - `POST /v1/sources`: register an S3 bucket or prefix and its inventory source.
  - `POST /v1/locations`: flip verified prefixes to Azure.
  - `GET /v1/restores`: user-triggered restores, so Prizmanyx can fold them into its plan.

## Reserved: migration mode and `handed_off` (decision 35)

A later version moves S3 or Azure data to an on-premises NAS (NetApp ONTAP, Qumulo) too. Nothing NAS-specific is built yet. These names are reserved now, so the API and catalog don't freeze around S3 → Azure:

- **Per-migration `mode`:**
  - `serve` (today's only behaviour): the destination becomes the backend behind the same OmniFile share. Users keep the share; OmniFile keeps tiering, recalls and versioning.
  - `hand_off`: copy, verify in full (decision 24), then the destination serves users directly and OmniFile steps aside.
- **Terminal dataset state `handed_off`:**
  - the share is released (removed from the edge, after a notice period);
  - the catalog keeps the dataset read-only, for audit and for "where did my file go";
  - retained versions, WORM retention and legal holds stay owned where they live. A hand-off never shortens or drops them; versions under retention stay in their backend until it ends.
- **Catalog:** `dataset.migration_state` gains `handed_off`, and the migration record gains `mode`, when this is built. The CHECK values are added then, by a new migration; nothing is added now.
- **API:** `Dataset.migration_state` and `backend_kind` are open enums in the portal API (portal-api.md §1). The M5 drafts of `POST /v1/sources`, `POST /v1/locations` `{…, mode}` and the lifecycle events are in portal-api.md §11.

## Suggested phasing

1. **v1a:** a read-only namespace over S3 + Azure, with archive handling on both sides and Teams cards.
2. **v1b:** writes land in Azure; tombstones for deletes. v1 migrates S3 → Azure only (decision 23).
3. **v2:** SMB, rename for S3-resident files, and possibly Azure → S3 with AWS tools.
4. **v3 (maybe):** GCP in both directions.

## Open questions for the owner

1. ~~Writes during migration~~ — decided: all new writes go to the destination (decision 14).
2. ~~Who triggers S3 restores~~ — decided: OmniFile, always permitted, and restores can trigger migration (decision 15).
3. ~~S3 egress on user reads~~ — decided: accepted; waivers are administrative (decision 16).
4. ~~In scope for v1?~~ — decided: yes (decision 19).
5. ~~Who copies restored archive objects?~~ — decided: Azure Storage Mover; OmniFile hands off and monitors the job (decision 20).
