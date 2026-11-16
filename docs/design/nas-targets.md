# On-prem NAS targets (NetApp ONTAP, Qumulo): next version

Decision 35. Owner requirement (2026-10-06): move data from Amazon S3 or Azure Blob to an on-prem NAS, NetApp ONTAP or Qumulo. It ships in the next version. v1 makes only the foundation changes that would otherwise break later (§5).

## 1. Two modes, chosen per dataset

- **`serve`:** the NAS is a backend behind the OmniFile share, like Azure or S3. Users keep the same share, Teams notices, portal and AD access rules.
  - A NAS has no Archive tier. Archive needs cross-backend tiering (hot on the NAS, archive in Azure), or the dataset has no archive day.
  - NAS volumes must be reachable only by the edge's service account, as direct Blob access is limited to the service identity (`access-control.md`). Otherwise direct NAS mounts bypass OmniFile's RBAC.
  - ONTAP S3 **NAS buckets** (9.12.1+), whose objects are visible as NFS/SMB files, support neither versioning nor Object Lock. Ordinary ONTAP S3 buckets have versioning (9.11.1+) and Object Lock (9.14.1+, no legal hold), but their objects aren't visible as files. Qumulo S3 has versioning (7.1.2) and Object Lock in compliance mode with legal hold, over the same namespace; its version copies sit in a hidden `.s3-versioning~` directory that NFS and SMB can see.
  - So **serve mode is non-WORM**, and versioned datasets in serve mode need ONTAP non-NAS buckets or Qumulo.
- **`hand_off`:** OmniFile copies the dataset to the NAS, verifies every file, stamps ACLs, then releases the share. Users mount the NAS directly. The dataset ends in `handed_off`: the catalog is read-only, and retained versions and retention stay owned where they live.
  - **WORM shares (decision 31) hand off only to ONTAP SnapLock.**
  - **Writes during a hand-off (owner, 2026-10-06):** writes stay at the source until a short read-only freeze, then a final delta copy, verify, and switch. This amends decision 14 ("new writes go to the destination") for hand-off migrations only, because writing to the NAS through OmniFile is serve mode's data path.
  - Redirecting clients after the hand-off (DFS-N for SMB; NFSv4 referrals in Ganesha) is still to be chosen.

The next version builds `hand_off` first: native copiers move the bytes, and nothing new is needed in the FSAL or on the user data path.

## 2. Copiers (native first)

Checked against vendor docs on 2026-10-06; sources in §6.

| Path | Native copier |
|---|---|
| S3 → ONTAP (NFS/SMB) | NetApp Copy and Sync (data broker in AWS or on-prem), or AWS DataSync with an on-prem agent |
| S3 → Qumulo | Qumulo Shift-From (Qumulo Core 4.2.3+), or Copy and Sync / DataSync to Qumulo as a generic NFS/SMB server |
| Azure Blob → ONTAP or Qumulo | NetApp Copy and Sync only (Azure Blob → ONTAP, or → any NFS/SMB server) |
| FSx for ONTAP → on-prem ONTAP | SnapMirror |
| Fallback, any target | Copy through the OmniFile share: XCP (NFS) or robocopy `/COPYALL` (SMB). It reuses OmniFile's recall flow and carries ACLs. |

Not usable: Azure Storage Mover (writes only into Azure), DataSync with Azure Blob as source (it pairs Azure Blob only with AWS storage), Azure NetApp Files → on-prem SnapMirror (not documented). Azure Data Box export needs Archive-tier blobs rehydrated first and exports no ACLs or metadata; Snowball Edge is closed to new customers.

Archived sources are restored first (S3 `RestoreObject`, Azure rehydrate; decision 15). DataSync's "verify all data" isn't available for Glacier Flexible Retrieval or Deep Archive sources.

## 3. Verification (decision 24 applies)

No native tool gives a per-file checksum to compare with the source. Copy and Sync compares path, size and name (plus optional mtime, uid, gid, mode). DataSync doesn't name its checksum. ONTAP S3 documents only Content-MD5. Qumulo S3 accepts CRC64-NVME trailers on upload but stores no per-object checksum.

So the **edge** reads each NAS copy on the LAN (no egress, no WAN) and computes CRC64-NVME with `omnifile-integrity`. The result is compared with the source's: S3 Batch Compute checksum, or the catalog's CRC64 for Azure. A match sets `verified_by = 'edge_nas_read_crc'` and closes `location_verified`, whose `crc64nvme` requirement stays as it is. In a hand-off, `xcp verify` between the OmniFile export and the NAS is a second, independent check of content and ACLs.

`worker/verify.py` reads through `backend.get_range` in Azure; for a NAS that would drag every byte over ExpressRoute, so NAS verification is an edge instruction.

## 4. Identity and ACLs

Object copiers carry no ACLs (Copy and Sync's FAQ says so for object → SMB). OmniFile stamps them afterwards from the catalog, whose ACLs are POSIX entries naming SIDs. POSIX → NTFS/NFSv4 is the lossless direction (risk R7 bites the other way): default entries become inheritable ACEs and the mask folds into effective rights. Stamping runs on the AD-joined edge with a NAS privileged account: an ONTAP SVM joined to AD with NTFS or mixed security style, or an AD-joined Qumulo cluster (its ACL REST API to be checked when built).

## 5. What v1 changes now, and what waits

**Now (would otherwise break later):**
- **F1.** Response enums that grow with new backends (`backend_kind`, `migration_state` and similar) are open (`x-extensible-enum`) in `portal-api.yaml`; the SPA renders unknown values.
- **F2.** The M5 partner endpoints are drafted backend-neutral: `POST /v1/sources` takes a backend id or `{kind, config}`; `POST /v1/locations` takes `to_backend_id` and `mode: serve | hand_off`; `/v1/events` carries dataset lifecycle events.
- **F3.** `migration-namespace.md` reserves `mode: serve | hand_off` and the terminal state `handed_off`. The CHECK value is added when it is built.
- **F4.** The edge tolerates an unknown credential `mode` and reports "unsupported backend: upgrade the edge" for that operation.

**Waits (additive later):**
- A NAS backend kind (`backend.kind` CHECK, `verified_by`, the price table's `backend_kind`): one ALTER each. `location.container` holds the SVM/volume or cluster/share, `object_key` the path.
- A NAS backend in `base.py`: hot only, `versioning` per bucket type, `UnsupportedOperation` for restore and tags.
- A generic copier: rename `migration_prefix.storage_mover_job` and the `storage_mover` queue to `copier` / `copier_job_ref` (internal, do it in M5).
- A source-change watermark for Azure as a migration source (`journal_checked_through` is S3-only).
- The edge verify instruction (§3) and ACL stamping (§4).
- `archive_backend_id` for cross-backend tiering in serve mode; `archive_day_required` relaxed by backend capability.
- Name mapping for keys that are invalid on NTFS.
- The client redirect after a hand-off.

## 6. Sources (read 2026-10-06; page dates where shown)

- NetApp Copy and Sync supported relationships (2025-09-29): https://docs.netapp.com/us-en/data-services-copy-sync/reference-supported-relationships.html
- NetApp Copy and Sync FAQ, ACLs from object storage (2026-09-17): https://docs.netapp.com/us-en/data-services-copy-sync/faq.html
- `xcp verify` (2024-04-26): https://docs.netapp.com/us-en/xcp/xcp-nfs-reference-verify.html
- ONTAP S3 multiprotocol (NAS) buckets (2026-09-09): https://docs.netapp.com/us-en/ontap/s3-multiprotocol/index.html
- ONTAP S3 supported actions (2026-09-14): https://docs.netapp.com/us-en/ontap/s3-config/ontap-s3-supported-actions-reference.html
- NetApp Replication, FSx for ONTAP → on-prem (2026-09-17): https://docs.netapp.com/us-en/data-services-replication/concept-replication.html
- Azure NetApp Files migration, into ANF only (2026-07-08): https://learn.microsoft.com/en-us/azure/azure-netapp-files/migrate-volumes
- Qumulo Shift-From (S3 → Qumulo): https://docs.qumulo.com/administrator-guide/replicating-data/shift-from-s3.html
- Qumulo S3 API functionality and limits: https://docs.qumulo.com/administrator-guide/s3-api/supported-s3-functionality-known-limits.html
- AWS DataSync locations: https://docs.aws.amazon.com/datasync/latest/userguide/working-with-locations.html; Azure Blob location: https://docs.aws.amazon.com/datasync/latest/userguide/creating-azure-blob-location.html; verification: https://docs.aws.amazon.com/datasync/latest/userguide/configure-data-verification-options.html
- Azure Storage Mover overview (2026-05-05): https://learn.microsoft.com/en-us/azure/storage-mover/service-overview
- Azure Data Box export (2026-08-03): https://learn.microsoft.com/en-us/azure/databox/data-box-deploy-export-ordered

**Still unverified:** ANF → on-prem ONTAP replication (undocumented, so treat it as no); which checksum DataSync uses; any Copy and Sync checksum mode (none documented); replication between Azure Native Qumulo and on-prem Qumulo (marketing only); Qumulo's ACL REST API; NFSv4 referrals from Ganesha for the hand-off redirect; whether Qumulo Object Lock is an acceptable WORM hand-off target besides SnapLock.
