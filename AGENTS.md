# OmniFile Gateway

**A storage gateway in the spirit of Azure Data Box Gateway, with Archive and migrations built in.** A Linux virtual appliance serves NFS/SMB shares locally, keeps a cache, and ships files to the client's cloud storage as plain objects, managed from a service in the client's cloud. Azure Blob is the home that is built; Amazon S3 and Google Cloud Storage are to be homes too, and the service is to run on any of the three (decisions 68, 69; `docs/design/multicloud.md`). Unlike Data Box Gateway, Archive is first-class: files tier down on a client-set schedule and come back on request, with users told in Teams. It can also present data that still lives in another provider (Amazon S3 first) while that data migrates, in either direction, so users keep one share throughout. And it can sync an existing NAS directory to the cloud without sitting in the users' path (an **Annex**, decision 51).

Clients whose data lives only in S3 or in Google Cloud Storage are **in scope** since decision 68 (2026-11-11), which reversed decision 18. Nothing is built for them yet.

The product, in the owner's words (decision 73): a software-based filer with migration capabilities and the archive, that does migrations easily and quickly and then takes over as the file sync service. It is not a NetApp replacement.

It is a leave-behind product: Prizmanyx (the AWS→Azure migration tool) migrates an estate, and OmniFile Gateway stays with the client afterwards. Prizmanyx talks to it through a partner API.

This file is the anchor document. Read it before doing anything else in this repo. It records the decisions made during the research phase (2026-09-26); the evidence lives in `docs/research/`. When a decision changes, change it here — not just the prose around it.

---

## What it does (the product in one scenario)

1. A user copies `line_1042.segy` (184 GB) into `/long-term-storage/dataset` on the OmniFile share (NFS or SMB).
2. The edge filer notices the write is complete (quiet period + SEG-Y structure check), hashes it, and uploads it to Azure Blob as a **plain block blob** — readable by any Azure tool.
3. The file moves Hot → Cool → Cold → **Archive** on the schedule the client configured (or Smart tier + a fixed archive date). The local copy becomes a stub so disk is freed; the first few MB (SEG-Y headers) stay resident.
4. Day 45: someone opens the file. OmniFile knows **who** (AD identity), starts a recall, and — per the share's policy — either makes the read wait (batch clusters) or fails fast with a clear note file. The person gets a **Teams card** with Recall / Keep online / Track buttons, and the card updates as the recall progresses.
5. A web portal shows every file's tier, lets users recall ahead of time, and lets admins edit policies.

A second pillar ships in v1 (see `docs/design/migration-namespace.md`): during a Prizmanyx migration, the same NFS frontend presents files that still live in **S3** and serves them from S3 or Azure — wherever the bytes are at that moment — with the same archive/recall behaviour for S3 Glacier.

## Owner decisions (closed — do not relitigate)

Recorded 2026-09-26. Full list with context: `docs/DECISIONS.md`.

- **Separate product, separate repo.** Not a Prizmanyx pipeline stage. Exposes an API Prizmanyx understands.
- **Linux only** for the edge filer.
- **Languages:** Rust (edge daemon), C (the NFS-Ganesha FSAL module), Python (the OmniFile service — control plane and backends). The portal front end is TypeScript (React + Vite), browser only (decision 32).
- **Archive is core.** Tier timing is configuration: the client sets when files go Hot → Cool ("warm") → Cold → Archive, *or* chooses **Smart** (Microsoft moves files among Hot/Cool/Cold by use) **plus an archive date that is always respected**.
- **Protocols:** NFSv4 preferred; **NFSv3, NFSv4.x and SMB are all supported.** Some processing clusters may be told to wait.
- **Identity:** all target clients run on-prem **Active Directory** (synced to Microsoft Entra ID — formerly Azure AD). AD integration is mandatory and must be easy to set up. **The filer enforces AD RBAC** on every path: NFS, SMB, portal, Teams, API, and S3-resident files. Access is managed through AD groups (decision 28; `docs/design/access-control.md`). **The portal's roles are held by AD groups or by named people, chosen at install;** with people, every AD user in the tenant is a User, so Entra ID P1 is needed only for groups (decision 89).
- **Kerberos is configurable per share**, and the UI must show a note about its throughput cost when enabling it.
- **Notifications:** Microsoft Teams (proactive bot + Adaptive Cards with buttons). Email is the fallback.
- **Networking:** must work over **Azure Private Endpoints**. **One public HTTPS endpoint is acceptable** — only for Teams card callbacks (a thin relay). The customer creates the private endpoints, following our setup guide. When the setup wizard selects private networking, OmniFile always reaches storage through them and never falls back to a public path. The Azure control plane (ARM) and Entra stay public, and that is fine (decision 22). **Cloud-to-cloud migration copies are the exception:** they run over the providers' public endpoints, encrypted in transit, and OmniFile builds no private path between clouds (decision 71).
- **Policy UI:** "incredibly easy to use, but incredibly detailed" — plain-language questions up front, every knob one click down.
- **Migrations run in any direction; only S3 → Azure is built** (decision 23). Every direction among Azure, AWS and Google Cloud is in scope, each copied by the destination's native tool (decisions 68, 70). **While migrating, all new writes go to the destination.** OmniFile may always request archive restores (S3 `RestoreObject`, Azure rehydrate), and a restore can automatically trigger migration of what it restored. Egress on user reads is accepted; waivers are administrative, outside this project.
- **Control plane runs in Azure today:** a container app plus Azure Database for PostgreSQL, VNet-injected. It avoids Event Grid push, which cannot deliver into a private network. **It is to run as containers on any of the three clouds;** Entra and Teams stay on every host (decision 69; not built).
- **Backend-pluggable; any of the three clouds can be a dataset's home** (decision 68, reversing decision 18). Each dataset (share or prefix) is bound to a **storage backend**. Azure Blob is the home backend that is built; Amazon S3 is built as a migration source only; Google Cloud Storage is not built. The backend decides tiers, restore mechanics, credentials and private networking. Everything user-facing — shares, identity, policies, Teams, portal, API — is the same whichever provider holds the bytes. A dataset's backend can change (for example S3 → Azure during a Prizmanyx migration) while the share stays put.
- **The migration namespace is in v1** (decision 19), so `FSAL_OMNI` is built as a virtual filesystem over local, Azure and S3 backends from the start.
- **Azure Storage Mover copies restored archive objects;** OmniFile hands the copy to a Storage Mover job and monitors it. No home-grown copier (decision 20). The same holds for every pair of clouds: the destination's native tool copies, and OmniFile is never in the data path (decision 70). OmniFile still starts and watches each job, restores archived objects first, and verifies every file before cutover (decision 74). **By default the tool is to read a transfer location OmniFile owns in the source cloud, never the client's storage;** reading it directly becomes the option (decision 76; not built: today only urgent recalls are staged). Bulk prefixes rerun in Additive mode after each restore wave; urgent recalls go through an S3 staging prefix. Every copied file is verified in full before cutover: an in-Azure CRC64-NVME read is compared with S3's Batch Compute checksum (decision 24).
- **Backends: Azure Blob and Amazon S3 are built; Google Cloud Storage is in scope,** in both directions and as a home (decisions 21, 68).
- **Edge OS: Ubuntu 26.04 LTS, minimized** (decision 26).
- **Tiers can be set by hand in the gateway page** (Owners and DataStewards), as a pin that overrides the policy schedule until it expires (decision 29).
- **Any reader can press "Keep online" on a Teams card:** a 14-day audited hold with no tier change (decision 33).
- **Restore an old version** as an overwrite (the previous current is kept as a version) or as `<name>-restored` / `<name>-restored-<UTC time>`; shares show only current versions (decision 34).
- **Next version: on-prem NAS targets** (NetApp ONTAP, Qumulo), per dataset either served behind OmniFile or handed off after a verified copy. v1 keeps API enums open and the M5 partner API backend-neutral so this is additive (decision 35; `docs/design/nas-targets.md`).
- **Storage versioning:** every committed write is a version, and deletes are tombstones. Users rewind a folder to an earlier date, within a depth set by the share's Owners (capped by ArchiveAdmin). This is in the portal first, then as read-only NFS/SMB point-in-time trees (decision 30).
- **WORM is configurable per share in the gateway, one-way.** Overwrites and deletes create versions and tombstones, and nothing committed is destroyed before retention ends. Compliance-locked Azure version-level WORM, with legal holds per file (decision 31). Design: `docs/design/tiers-versioning-worm.md`.
- **Service queues: PGMQ in the catalog Postgres,** installed as plain SQL because Azure's managed Postgres doesn't offer the extension. Enqueue in the same transaction as the catalog change (decision 27).
- **Licence: Apache License 2.0** (decision 86, replacing decision 25's BSL 1.1): free to use, change and ship at any size of organisation, as long as the copyright and attribution notices, `NOTICE` included, are kept. `fsal/` stays LGPL-3.0 and `samba-vfs/` GPL-3.0-or-later. Outside contributions are accepted with a DCO sign-off on every commit, and no agreement (decision 87).
- **The public repository is `MichaelDanCurtis/OmniFile`:** one scrubbed snapshot per publish, no history (`release/public_snapshot.py`). This repository stays the private working one, and the appliance update feed stays on it for now (decision 88).
- **A folder restricted after its `sec=sys` share was made is a warning, not a refusal** (decision 39): the portal flags the share and an alert fires until an ArchiveAdmin switches it to Kerberos or records a host-trust override; the edge keeps serving.
- **One GPL-3 Samba VFS module ships with the edge,** in `samba-vfs/`: `vfs_omnifile_prevver` fills a file opened directly in an unfilled rewind label. It runs inside smbd only, is never linked into our binaries, and talks to no OmniFile process (decision 36).
- **Migration deletes are reported, never applied to the S3 source** (decision 40). OmniFile holds no `s3:DeleteObject` on a source and never writes to one; the cutover report lists the tombstones. Old copies in a home a dataset has left are removed only by a separate clean-up an ArchiveAdmin starts, never by the migration (decision 81; not built).
- **Point-in-time trees are `.snapshots/YYYY-MM-DDTHHMMZ/`** over NFS and Previous Versions over SMB (decision 41).
- **Hard links are refused on shares;** copies work. A hard link in a seeded or migrating source becomes separate files (decision 42).
- **AWS credentials have no keys anywhere:** the service uses web identity from its Azure managed identity, the edge uses IAM Roles Anywhere with its key in the TPM, and presigned URLs are only a fallback (decision 43).
- **The edges' Roles Anywhere certificates come from OmniFile's own per-deployment edge CA by default,** which the client registers as the trust anchor; the client's own CA stays an option. The edge requests and renews its certificate over mTLS, and a revocation uploads the CA's CRL to Roles Anywhere (decision 50; `deploy/aws/README.md`).
- **Chunk-range reads gate M5:** the SSD chunk cache and resident-header cache serve a large remote file's ranges without fetching the whole file first (decision 44).
- **No GitHub CI:** every workflow is manual-only, for runner cost (decision 45).
- **The appliance's GPL source is published in a public GitHub repo,** `omnifile-gateway-sources` (the owner creates it at the first release): one GitHub Release per appliance release with the exact archives and manifest from `appliance/source-offer/`, and requests through that repo's issues, which the written offer names (decision 46).
- **The update feed is on GitHub Releases,** where the appliance's `systemd-sysupdate` downloads it. Its signing key is in Azure Key Vault Premium (HSM, non-exportable), and only the release script signs, never a developer machine (decision 47).
- **Secure Boot is optional in v1:** the UKIs are signed with OmniFile's own key, also in Key Vault Premium, and the docs show a client how to enrol it (MOK or `db`); Microsoft (shim) signing only if a client requires it (decision 48).
- **The relay's allow-list comes from the `AzureBotService` service tag at deploy time** (`deploy/azure/relay-ranges.sh`), never typed by hand; to be confirmed on the first real Teams tenant (decision 49).
- **An Annex syncs an existing NAS directory to Azure without sitting in the users' path** (decision 51; `docs/design/annex.md`). Any NFS or SMB server, ONTAP and PowerScale included, read by the edge with scans and reconciliation. Modes: Copy, Archive after N days, Move now. A file leaves the NAS only after its copy is verified in full, under rules an ArchiveAdmin approved, after a 7-day dry run. NAS deletes are reported, never mirrored. OmniFile never stubs a NAS file.
- **A returning file replaces a file on a NAS only at its own name in its original folder** (decision 82), and only when that file is exactly the verified copy OmniFile holds; an older version may do so with `overwrite_keep_version` (83). The old file is hidden, the grace period waited, then the new one placed; a late change keeps the old file, under its own name or as `<name>-replaced-<UTC time>` (84). No switch: the approval page marks such a rule (85). Built in A3.4 (`a34.md`).
- **The UI says "API",** never a product name, **and every API client has a named owner who has access** (decision 52).
- **OmniFile API keys are offered alongside Entra app identities:** hashed, expiring, revocable, never logged (decision 53).
- **NFSv3 locking (NLM) is on,** with fixed ports behind the appliance's firewall. The cost is a full 90 s grace at every Ganesha restart (decision 54).
- **No recall limits for now** (decision 55).
- **DataStewards see a share map's whole-share totals, as counts only** (decision 56). File names stay limited to what the caller can read.
- **A migration's tombstone list goes to registered API clients with `Archive.Adopt` and to ArchiveAdmins,** besides the app that started it; every read is audited (decision 57).
- **The guide's AWS names take an optional, test-only suffix** (`OMNI_AWS_NAME_SUFFIX`), empty for customers (decision 58).
- **Annex removals over SMB treat a file as in use when the server refuses its rename** (a sharing violation), plus a 15-minute grace after the rename. **Removals never start by themselves:** after the dry run, an ArchiveAdmin who signed in within the last few minutes confirms what it would remove, never the proposer or anyone answerable for its app (decision 59).
- **The small deployment size is the default for test runs and small sites,** in West US 3 on Azure and us-west-2 on AWS; `standard` stays the template's default (decision 60). No size relaxes private networking, the one public endpoint, per-app identities or the HSM key.
- **The portal's look is octave-style soft cards and layout, with Nothing-style type and dot-matrix motion** that stops when hidden and under reduced motion (decision 61).
- **Who sees prices is set per role by an ArchiveAdmin,** who also overrides the rates; a hidden caller gets no amount in any answer (decision 62).
- **An annexed file's read set takes its parent folders into account** (decision 63), up to the export's root, strictly on SMB, and a file is hidden until its folders are read. **A filer that doesn't show its ACLs is flagged at each mount,** and group-readable placements and share copies are refused there until an ArchiveAdmin acknowledges it (decision 64). **Filer setup asks whether a share or export limits access beyond file permissions** (decision 65). 63 is built (`catalog/read_set.py`), 65 on top of it (the filer's answer, `access`), and 64 on top of that (`acls_shown` at every mount, the filer's flag and its acknowledgement). All three must land before a first client.
- **When files sharing a blob want different tiers, the one whose plan differs gets its own copy** (decision 66; built in #77, `docs/design/tiers-versioning-worm.md`). The moving file copies and the others keep the original, so a split bills no early deletion while another file stays on the blob. A Cool or Cold step within 7 days, or an Archive date on the same day, isn't worth a copy, and a blob in Archive is never copied. A split never holds up an archive date for more than about 10 minutes, and an overdue or late archive is alerted on. If a copy can't be made, only an archive date that has come may move the blob over a file that differs, and it tells the pin's setter; any other move, a pin to Archive included, waits and tries again daily.
- **The NFSv3 quiet period is set per share,** by its Owners or an ArchiveAdmin, 10 to 600 s, defaulting to 60 s on WORM shares and today's 10 s elsewhere (decision 67; built in #80).
- **What OmniFile owns is the on-premises agent that archives to the cloud, in two modes:** the NFS and SMB server that archives itself (shares), and the endpoint that reaches into another filer and archives its files (the Annex). Both must work with any of the three clouds as home (decision 72; only Azure is built).

## Backends

A backend implements one contract: put, ranged get, list, tier change, restore (start + status), delete, and capability metadata. The capability metadata covers tiers, minimum durations, restore times and limits, and private-network options. Policies use **logical tiers** (hot, warm, cold, archive, smart). The backend maps them to its own classes and reports its constraints, so the policy editor can warn in the right terms.

| Logical | Azure Blob | Amazon S3 |
|---|---|---|
| hot | Hot | Standard |
| warm | Cool (30-day minimum) | Standard-IA (30-day minimum; 128 KB minimum billable size) |
| cold | Cold (90-day minimum) | Glacier Instant Retrieval (90-day minimum; 128 KB minimum billable size; no restore needed) |
| archive | Archive (180-day minimum; rehydrate by copy; ≤ 10 GiB/h priority per account) | Glacier Flexible Retrieval (90-day minimum) or Deep Archive (180-day minimum; no Expedited restore); +40 KB billed per object; `RestoreObject` creates a temporary copy for N days, billed on top of the archived copy |
| smart | Smart tier (ZRS only, no Archive) + OmniFile's archive date | Intelligent-Tiering (objects under 128 KB are never tiered; restores from its opt-in archive tiers take no `Days` and never expire), + OmniFile's archive date |
| private network | Private Endpoint | PrivateLink interface endpoint for S3, reachable over Direct Connect or VPN; TLS 1.2 only |
| edge credentials | Arc managed identity, or service-minted user-delegation SAS | IAM Roles Anywhere (X.509 → temporary credentials; certificate from OmniFile's edge CA, decision 50), or service-minted presigned URLs |

The S3 column was checked against AWS documentation on 2026-09-26 (`docs/research/2026-09-26-verification.md`). Presigned URLs were not re-checked. Decision 43: the presigned-URL entry is a fallback only; the service uses web identity from its Azure managed identity, and the edge uses Roles Anywhere.

## Architecture (summary — detail in `docs/ARCHITECTURE.md`)

```
Users (NFSv3 / NFSv4.x / SMB)           Browser + Teams
        │                                     │
┌───────▼──────── Edge filer (client DC, Linux) ───────────┐
│ NFS-Ganesha + FSAL_OMNI (C)   Samba (separate process)    │
│ omnifile-edge daemon (Rust): landing zone, upload,        │
│   hashing/whole-file dedupe, stub guard, recall fill      │
│ Joined to AD with winbind (single ID source)              │
└───────┬──────────────────────── private IP, mTLS ─────────┘
        │  ExpressRoute / VPN → Blob private endpoints
┌───────▼──────── OmniFile service (client's Azure, Python) ┐
│ Catalog (Postgres) · policy engine · tiering scheduler     │
│ Recall orchestrator · portal · partner API · Teams bot     │
│ Backends: Azure Blob · Amazon S3 · (more later)            │
│ Teams relay = the ONLY public endpoint                     │
└──────────────┬──────────────────────────────┬──────────────┘
      Azure Blob (private endpoint)    Amazon S3 (PrivateLink)
      live acct (ZRS/Smart) +          Standard … Glacier /
      archive acct (LRS/GRS)           Deep Archive
```

Key choices and why:

- **The edge namespace is always a real xfs tree** (R1, `docs/design/fsal-namespace.md`). Every catalog object exists on the edge as a sparse placeholder: local files, stubs, and objects that were never local (S3- or Azure-resident). `FSAL_OMNI` stays a thin guard stacked over FSAL_VFS. So LOOKUP, GETATTR and READDIR never leave the box, and one set of POSIX ACLs and one fail-closed guard cover everything. The cost is about 1.1 KB of xfs per object.
  - **The stub marker is one binary `trusted.omnifile` xattr, never `user.*`.** NFSv4.2 SETXATTR maps to `user.*`, so a file's owner could delete a `user.*` marker and read holes as zeros.
  - The daemon keeps the edge's metadata replica in SQLite (R2, `docs/design/edge-metadata.md`). The FSAL talks to the daemon over `SOCK_SEQPACKET` (`docs/contracts/fsal-socket.md`).
- **NFS-Ganesha, not the kernel NFS server.** knfsd reads arrive from kernel threads, so nothing downstream can tell *which user* opened a stub. Ganesha's FSAL sees the caller's uid and client address on every read, and can return `NFS4ERR_DELAY` / `NFS3ERR_JUKEBOX` ("wait") or an error ("fail fast") itself.
- **Samba for SMB, as a separate GPL-3 process.** Its reads hit the local filesystem, so SMB stub reads are guarded by fanotify pre-content events (Linux ≥ 6.14; ext4/xfs/btrfs only; a `mmap()` must be filled for its whole range up front) in the Rust daemon. The edge OS is **Ubuntu 26.04 LTS, minimized** (kernel 7.0; decision 26). RHEL 10 and Flatcar are on 6.12 and don't have these events.
- **winbind, not SSSD,** as the one identity source for both NFS and SMB (Red Hat does not support SSSD for Samba file servers). Prefer `idmap_ad` (AD POSIX attributes) so Linux clients agree on uids.
- **The edge holds no Azure secrets.** It authenticates to the service over mTLS; the service mints short-lived per-blob user-delegation SAS tokens. The box itself can use an Azure Arc managed identity.
- **Azure backend: two storage accounts per region.** Smart tier requires zone-redundant storage; Archive is forbidden on ZRS. On a file's archive date the service copies it server-side into the archive account with `x-ms-access-tier=Archive`, then deletes the original. Smart is the live account's *default* tier and is GA in public regions: uploads must not set an explicit tier, or the blob never enters Smart. **This applies to non-WORM shares only.** Immutability forbids the delete, so **WORM shares** use one LRS/GRS account with versioning and version-level WORM, and tier **in place** with Set Blob Tier (decision 31). Smart isn't offered for them until pilot 4 confirms it works with immutability.
- **Azure recall = Copy Blob out of Archive** (avoids the 180-day early-deletion fee that Set Blob Tier would incur), respecting the **10 GiB/hour per-account priority cap**.
- **No Event Grid dependency: read the Blob change feed.** Event Grid cannot push into private endpoints. The change feed is an ordered, durable log read as ordinary blobs over the private endpoint (records within minutes). From schema v4 it includes `BlobTierChanged` and `BlobAsyncOperationInitiated`. It is the catalog-sync and recall-status stream. Get Blob Properties is kept only for the single file a user is waiting on.
- **S3 recall: Batch Operations Restore waves, with EventBridge → SQS for completion.** The control plane pulls restore-completed and restore-expired events from an SQS queue in the client's AWS account, through an SQS interface endpoint. That event can trigger migration of the restored objects (decision 15). The restored copy is temporary for `Days`, so the service tracks expiry. The 1,000 restore requests/s per account is shared by user recalls and waves; budget it.
- **Integrity: CRC64-NVME end to end.** Azure's CRC64 and S3's default CRC64NVME are the same algorithm. S3 Batch **Compute checksum** produces full-object checksums, including MD5, for objects at rest in any storage class, Glacier included, without restoring them. Copies are verified with per-block `x-ms-content-crc64` and no extra read pass.
- **Credentials without secrets:** the Arc managed identity for Azure, and IAM Roles Anywhere (X.509, key in a TPM) for S3 through an S3 interface endpoint. Interface endpoints reject TLS 1.3, so the Rust client must allow TLS 1.2.
- **Holds and state in index tags.** Tags can be set on Archive-tier blobs; metadata cannot. Legal holds use version-level WORM, set at upload. Accounts with version-level immutability can't run Blob Inventory, so they reconcile from the change feed and listings.
- **POSIX attributes as `x-amz-meta-file-owner/-group/-permissions`** on S3 (File Gateway compatible). On Azure they are blob metadata `file_owner`, `file_group` and `file_permissions`: Azure metadata names must be C# identifiers, so hyphens are rejected. The backend layer maps logical names to each provider's spelling.

## The "file is archived" experience

- NFSv4.0 has exactly one "not yet" signal (`NFS4ERR_DELAY`); Linux clients retry it silently forever, so the program just waits. NFSv4.2's `offline` attribute (RFC 9754) is not implemented in Linux yet — adopt it when it is.
- Therefore each share has a policy: **wait** (batch clusters), **fail fast** (people: immediate `EIO`), or **wait only if quick** (threshold in minutes).
- Always, regardless of policy: a `<file>.RECALLING.txt` note appears next to the stub; a Teams card goes to the person who opened it (identity chain below); the portal shows the recall's four steps.
- **Never return "file not found"** for an archived file — it contradicts `ls`, and sync/cleanup tools treat it as a delete.

## Identity chain (AD → Teams)

uid seen by FSAL (or the SMB session user) → winbind → AD SID → Microsoft Graph `GET /users?$filter=onPremisesSecurityIdentifier eq '<SID>'` → Entra objectId → proactive Teams bot message with `Action.Execute` buttons → the click arrives at the public relay → relay validates the Bot Framework JWT, that the clicker is the intended recipient, and that the intent is signed and unexpired → recall queued. With NFS `sec=sys`, the uid is only *claimed* by the client; record it as such. With Kerberos it is proven.

## Critical constraints

- **License hygiene: no AGPL at runtime.** Our own code is Apache-2.0 (decision 86), so every dependency must allow use, change and redistribution, commercial included. GPL components (Samba, SSSD/winbind, lsyncd, fswatch) run as separate processes and are never linked. NFS-Ganesha is LGPL-3: our FSAL module is LGPL and stays small, talking to the Rust daemon over a socket. Maintain `LICENSES.md` for every direct dependency.
- **Plain blobs only.** No proprietary chunk store in Azure. Dedupe is whole-file (hash match → reference or server-side copy), never a chunk repository.
- **Stubs must fail closed.** fanotify fails *open*: if the daemon dies, pending reads are allowed and a hole-punched stub reads as zeros — silent corruption of seismic data. Every stub design must guarantee a read of an unfilled region errors when the daemon is not there. The local filer spike (`docs/pilots/filer-spike-results.md`) showed how:
  - the fanotify group lives in the systemd fd store, so it outlives the daemon;
  - it uses `FAN_UNLIMITED_QUEUE`;
  - the daemon journals every event, so a restarted daemon can answer its predecessor's;
  - smbd and Ganesha are bound to the guard unit;
  - the FSAL fails closed and never blocks Ganesha threads on the daemon.
  
  Without these measures, the spike reproduced zeros.
- **Archive recall is slow for big files.** Microsoft's "up to 15 h" applies only under 10 GB; priority retrieval is capped at 10 GiB/h per storage account. Never promise a time above 10 GB.
- **Private networking caveats.** Over ExpressRoute, private-endpoint traffic crosses the ExpressRoute gateway (1/2/10 Gbps by SKU) and "might be reduced by half" (Microsoft). Private Link charges ≈ $0.01/GB processed. Size the gateway; offer the optional relay mode only when a pilot proves it helps.
- **Kerberos cost.** Microsoft measured krb5 ≈ −53% IOPS and krb5p ≈ −77% on NFS; the UI must say so when an admin enables it.

## Research rule: native features first

Owner direction (2026-09-26): on both Azure and AWS, look first for **native platform features that make the product easier**, and use them instead of building our own. Examples: S3 Batch Operations for bulk restores, S3 Metadata / Inventory for the catalog, Azure lifecycle rules as a tiering backstop, Blob Inventory for reconciliation, managed identities / IAM Roles Anywhere for credentials, Data Box / Snowball for seeding. A native feature is rejected only for a recorded reason: it breaks private networking, it can't express a customer's policy, or it locks a control-plane feature to one cloud.

## Anti-patterns (do NOT do these)

1. Don't use the kernel NFS server for stub-backed exports — it hides the requesting user.
2. Don't return `ENOENT` for archived files.
3. Don't store data in Azure in any format other than the original file bytes.
4. Don't rely on Event Grid delivery into the private network.
5. Don't promise recall times above 10 GB, and don't offer High priority where it cannot be faster.
6. Don't link GPL code into our binaries; talk to it over sockets/D-Bus/NSS. The one GPL module we write (`samba-vfs/`) lives inside smbd and is never linked by us.
7. Don't let a policy proposal from Prizmanyx (or any AI) apply itself — an Archive Admin approves it in the portal.

## Repo layout

- `edge/` — Rust workspace. `crates/omnifile-edge` is the edge daemon.
- `fsal/` — the NFS-Ganesha FSAL module (C, LGPL-3). Built against a Ganesha source tree.
- `appliance/` — the edge appliance image: the mkosi definition, `build.sh`, `convert.sh` (qcow2, VHDX, OVA) and the qemu tests (decision 38).
- `samba-vfs/` — the Samba VFS module `vfs_omnifile_prevver` (C, GPL-3). Built inside the matching Samba release by `samba-vfs/build.sh`.
- `service/` — the OmniFile service: control plane, catalog, policy engine, backends (Python 3.12+).
- `relay/` — the Teams relay, the one public endpoint (Python).
- `portal/` — the portal front end (TypeScript, React + Vite, decision 32); `portal/e2e/` holds the Playwright tests.
- `deploy/` — `azure/` (Bicep, `check.sh`, the what-if gate, the restore drill), `aws/` (the client's Roles Anywhere setup and IAM policies, and `cloudformation/omnifile-connect.yaml`, the service's role that Settings, AWS launches), `images/` (container builds and smoke), `teams/` (the app package), `entra/` (the app registration).
- `release/` — one version, SBOMs, the licence check, the vulnerability scan, release notes and the runbook (`docs/release/README.md`).
- `ci/kernel-canary/` — the fanotify kernel canary (risk R3).
- `dev/` — the local dev stacks and the Lima VM definitions.
- `tests/` — lanes that span components:
  - `e2e/`: the M1–M5 exit tests, the Annex A1 exit test and the NFS grace test, run on the dev VMs (`results/` keeps their latest results);
  - `chaos/`: the chaos soak (`soak.sh`, `soak.py`) and its offline tests (`test_soak.py`);
  - `scale-edge/`: A8 at 17,000 readers, and the local line-rate ceiling;
  - `acl/`: the permission matrix;
  - `windows/`: the Windows client kit (Pester), for the matrix on site;
  - `real/`: the real-cloud harness (`run.sh`), fakes-only until the owner supplies accounts; `real/aws/` holds the AWS run's policy and roles template for the owner to review.
- `docs/` — `ARCHITECTURE.md`, `DECISIONS.md`, `PILOTS.md`, `BUILD-PLAN.md`, `contracts/`, `design/`, `research/`, `milestones/` (with `results/` per work package), `guide/` (the customer guide and its lint), `legal/` (drafts for counsel), `release/`.

**No GitHub CI (owner's choice, for cost).** The repo has no GitHub-hosted runners, so every workflow is manual (`workflow_dispatch`) and nothing runs on a push or PR. Don't re-enable triggers, and don't watch, re-run or try to fix checks. Run the lanes locally instead (service `pytest`, portal `npm test`, edge `cargo test` and `clippy`, the e2e scripts) and report their results in the PR.

## Current state and what's next

**M0–M4 are built and pass their exit tests against the simulated backend** (M0–M3 merged 2026-10-06 from PRs #3–#6; M4 in PR #8; `docs/BUILD-PLAN.md`, `docs/milestones/`):
- **M1:** NFS write → upload → stub → read back, fail-closed.
- **M2:** SMB, the production fail-closed guard, AD identity and POSIX-ACL RBAC.
- **M3:** tiering, Archive, versioning and WORM.
- **M4:** Teams cards and the relay click path, the portal (React SPA over `web`), the policy editor with live preview, restore choices (decision 34). Exit test 15/15 service checks and 7/7 Playwright; a dev-VM run with real SMB stub reads by an AD user.

Final regression at 7b942f6: M1 83/83, M2 111/111, M3 110/110, permission matrix 130/0, smb-guard A-suite 10/10. Each milestone had an independent review, and its fixes were re-verified.

**Where M5–M7 stand (2026-11-05):** M5 is done against sim and the fakes (below); what only real AWS can confirm remains. M6 and M7 are built as far as they can be without accounts or a client; their exits are not proven, as each needs a real subscription, tenant or site. Twenty PRs, #33 to #52, merged on 2026-11-03 and 11-04 PST (`docs/milestones/results/overnight-2026-11-04.md`, and one `wpN.md` per work package); #58 folded them into these documents. Twelve more, #53 to #65 (#58 aside), merged by 2026-11-05. Each merged after its local lanes passed and its independent review's findings were fixed (#65 is #64's follow-up); Codex was out of credits, so it still has to be run on them. **Not yet done anywhere:** anything on real Azure (`docs/milestones/real-azure-checks.md`), real AWS (`real-aws-checks.md`) or a real Teams tenant (`real-tenant-checks.md`), and Windows clients.

**Regression lanes, on the branches before merge** (the omnifile-edge dev VM, Ubuntu 26.04, kernel 7.0). None has run on main since #52 merged, and #54 and #62 have since changed the FSAL, the commit path and fills, so a rerun on main is next:
- M1 `tests/e2e/exit_test.sh` 83/83, M2 `m2_exit_test.sh` 111/111 and M5 `m5_exit_test.sh` 139/139 at #52's head (436a678), on the branch before merge; #49 also got M1 83/83 and M2 111/111 at 0041a26 (`wp15.md`);
- M3 `m3_exit_test.sh` 110/110, at #48's head merged with main at 20a8d21;
- the permission matrix (`tests/acl/matrix.sh`) 130 pass, 0 fail (#35);
- the smb-guard A-suite 13/13 on the appliance's own kernel (#44).

On main at 2794aeb (#65), run for this summary: service `uv run pytest -q` on its own Postgres 17, 1788 passed and 50 skipped; portal `npm test` 299 passed in 38 files.

M2 had dropped to 107/111 after the fill-fence work of 2026-10-20: a restarted daemon fenced a held SMB fill before Ganesha's FSAL reconnected, so the readers got EIO (failing closed). #49 makes the fence wait, bounded, for the FSALs to reconnect.

**M5 is done** (2026-10-21, `docs/milestones/M5.md`). Everything below is built on the sim and fakes:
- the S3 backend, which passes the shared backend contract suite over an in-memory S3/SQS fake;
- catalog seeding from S3 Inventory or a listing, kept current from S3 events (`docs/design/catalog-seeding.md`);
- the partner API;
- the migration engine (`docs/design/migration-engine.md`): Azure Storage Mover copies, restore waves, Batch Compute checksum jobs, staging for urgent recalls, per-file CRC64 verification and cutover;
- edge reads of S3-resident files through presigned URLs, exposed only after the whole-file CRC matches;
- rewind trees: the as-of listing, plus the edge daemon's label directories, `POPULATE_DIR`, reads of old versions and eviction;
- the reconciliation digest (control API §6): computed on demand by the service, checked daily per dataset by the edge, with race guards; divergence triggers a re-snapshot;
- edge audit records (control API §4.7): reads the edge refuses itself;
- bulk snapshot exports for 10^8-row datasets (control API §6): zstd NDJSON parts written by the worker, loaded by the edge on a re-snapshot after a SHA-256 check, with paging as the fallback;
- the FSAL's `.snapshots`: the share's rewind area in its root, `ROFS` below it, and `DELAY` with `POPULATE_DIR` on unpopulated directories (`fsal/tests/t_snapshots.sh`, NFSv3 and v4 with a userspace client);
- a service-side exit test.

Independent reviews of the migration engine and the edge's S3 and rewind code (2026-10-15) found 4 blocking and 9 should-fix issues. All are fixed, each with a test that fails without its fix.

The NFS half of the exit gate is built (`tests/e2e/m5_exit_test.sh`, in the M1 lane), with SMB Previous Versions steps. Its first runs found four bugs, all fixed (`docs/milestones/M5.md`). The worst was that seeded S3 files could not be read: their CRC is now learned from S3.

Previous Versions over SMB are built in full:
- **Folders:** the smb-guard fills an unfilled rewind directory when smbd opens it.
- **A file opened directly:** `vfs_omnifile_prevver` (`samba-vfs/`, decision 36) opens the file's folders first.
- **Tests:** A-suite x02 and x03, and steps 7 and 8 of the M5 e2e test.

PR #10's work (line B) is ported, decisions 40–44, in PRs #17 to #25 and the exit-test port (`claude/m5-exit-finish`): the source read-only guard (the service never writes to a source), the FSAL resume-race fix, held/WORM cutover, chunk-range reads (SSD chunk cache, resident-header cache) with no-keys AWS credentials, hard-link refusal, the user as actor on deletes and renames in the cutover report, ETag pinning, S3 Metadata seeding (behind a per-source choice until the spike on real AWS), the partner API shapes, and B's exit-test checks.

**The M5 exit test passes 139/139 on the omnifile-edge dev VM** at 6d0b46e (Ubuntu 26.04, kernel 7.0, joined to the dev AD; `tests/e2e/results/m5-latest.md`): S3-resident files served over NFSv3 and v4, Glacier restores, SMB Previous Versions, shares from the catalog, users' renames and deletes, a control-API outage, network loss, a daemon killed mid-fill, range reads that fetch only the chunks they touch (step 14), a migration in which a user's recall is staged and used by the cutover, an fd held across it and the deleter named in the cutover report (step 15), and no write to a source bucket in the whole run (step 16); one check, step 10's local chown, is intermittent under heavy host load (`M5.md`). The M4 exit test still passes (15/15 service-side checks and 10/10 Playwright, on the same VM). The VM run found one product bug: an edge op on a file whose version is dated ahead of the catalog's clock was refused, so a user's delete of a freshly seeded S3 file never reached the catalog. Migration 0031 fixes it. The rest were the VM's setup (the Samba module unbuilt, no conntrack, a stale FSAL module) and test helpers the product had moved past.

Still open after M5: everything only real AWS can confirm (`docs/milestones/real-aws-checks.md`), and S3 Metadata tables as the default.

**M6 is built, its exit unproven** (`docs/milestones/M6.md`). The exit is a fresh OVA plus the Bicep deploy reaching the M1–M4 behaviour in a clean subscription, by following the guide only, which needs one. The real-cloud harness (below) runs that exit once the owner supplies accounts.

Built on 2026-11-05 (lane counts from each PR and its results file):
- **The annex in the appliance image** (#53, `wp14-a2-appliance.md`): the first build and boot with the Annex copier. `smoke.sh` 70/70 on arm64 (67/67 in the PR, plus 3 for `rpc.gssd` after review). It fixed the builder's missing CA store, the annex supervisor restart-looping before setup, and a missing `hwdb.bin`, and added licence rows for cifs-utils, nfs-common and nftables with a fresh test manifest: `release/tests` 108 OK (the two fixture failures #58 reported are gone), `appliance/tests` 37 OK, `cargo test -p omnifile-setup` 53.
- **The harness follows the whole guide** (#56, `wp5.md`): a dry run takes all 82 steps of `docs/guide` through fake az models that refuse what Azure refuses, Bicep runs behind a dead proxy, and an az guard on every step's PATH refuses any deploy but whatif.sh's own granted create. The AWS run's inline policy and roles template are in `tests/real/aws/`, and `test_aws_policy.py` fails on each escalation route the review found; `--scope cheap` is about $0.57 an hour. `tests/real` 197 passed in the PR and 312 after merging main at 8e43ab6. Dry runs, exit 0, no residue: azure 18 pass, 1 partial, 23 manual, 0 fail; aws 7 pass, 11 partial, 14 manual, 0 fail; tenant 13 pass, 25 manual, 5 pending, 0 fail.
- **Two deployment sizes** (#59, `wp16.md`; decision 60): small is about $183 a month and standard about $676 at West US 3 list prices, before storage. `deploy/azure/check.sh` 34 ok, `test-sizes.py --evaluate` 55 ok, `docs/guide/check.sh` ok (82 steps), `docs/guide/test.sh` 63 OK, service 1710 passed and 50 skipped, 158 passed with the worker's pool on, `tests/real` 185 passed.
- **The portal's new look** (#60, `wp17.md`; decision 61): visual only. Portal `npm test` 266 passed in 31 files, mock Playwright 2 passed, main chunk 518 kB (budget 600), no CSP violation under `web`'s header; the dot layer takes 3.5 % of the main thread idle and 0.01 % when hidden.
- **Live Activity, Add edge, prices and Filers** (#63, `wp18.md`; decision 62): a long-poll feed (`GET /v1/activity/live`) that names only files the caller's AD groups may read and shows an app only its client owner's datasets; the Add edge wizard; price visibility and rate overrides; the Filers flow. `catalog/migrate.py` gained a `-- migrate: no-transaction` mode, so migration 0043 builds its indexes `CONCURRENTLY` without blocking edge commits, and the runner polls for its lock with a deadline. Service 1738 passed, 50 skipped (0042 copied in before #61 merged); portal 282 passed in 36 files; mock Playwright 5 passed.
- **Cloud connect** (#64, #65; `wp19.md`, `docs/design/cloud-connect.md`; threat model T11): Connect Azure (a delegated ARM sign-in, two custom read-mostly roles, discovery that confirms only privately reachable accounts), Connect AWS (a CloudFormation template launched from the portal, a read-only fetch, and the `OMNIFILE_AWS_ACCOUNTS` entry for the operator to set), Storage Mover deployed by the worker with the agent's steps and live status, and Scan source in the New migration wizard. #65 aborts incomplete uploads on the ops bucket and checks private DNS per account. At #65: service 1788 passed, 50 skipped; portal 299 passed in 38 files; `deploy/azure/check.sh` 34 ok; `cfn-lint` 1.34.2 clean; `tests/real` 313 passed in 14 files. At #64 also `deploy/entra/test-register.sh` 17/17.

Built on 2026-11-03 and 11-04 (PST):
- **A deployable service** (#34, `wp1.md`). Each app has its own entrypoint in the Bicep; before, `enrol`, the worker and the verifier would all have run `api`. T5 is closed: `api` sits behind a TCP ingress and terminates mutual TLS itself, with a server certificate from the edge CA that edges pin from their enrolment answer, and a forged `X-Forwarded-Client-Cert` is refused. Also the image builds (smoke 14/14), a what-if gated by a digest (`deploy/azure/test-whatif.sh` 16/16), the Teams package (11/11) and the Entra registration (17/17), each against a fake `az`. `deploy/azure/check.sh`: 29 ok.
- **The appliance gate** (#44, #50; `wp2.md`):
  - the full R3 gate passes on arm64 under HVF (canary 5/5, A-suite 13/13) and on x86-64 under TCG in 19 minutes; A8 holds 17,000 of 17,000 readers with 0 zeros;
  - builds are byte-identical, whole disk, on both architectures (snapshot 20261023, for ca-certificates USN-8944-1);
  - qcow2, VHDX, VMDK, a fixed VHD for Azure Gen2 and the OVA are checked and booted (`formats.sh` 17/17 arm64, 16/16 x86-64);
  - NFSv3 locking with fixed ports, and a fail-closed nftables firewall: no firewall means no network and no listeners;
  - per-box SSH host keys (every box had shared one set), re-keyed once on update; Ganesha's recovery directory off the read-only `/usr`;
  - smbd, nmbd, winbind and Ganesha restart after a crash and stay bound to the guard (#50; `smoke.sh` 57/57 on arm64);
  - update round 16/16, also from a pre-#44 image; Secure Boot key rotation 11/11; the update feed through a proxy 11/11; `setup.sh --dc` 11/11 against the dev AD.
- **The setup wizard can be rerun** after a failed AD join, and never formats a partition of the system disk; `python -m omnifile_service.admin backend` registers storage backends (#37; `cargo test -p omnifile-setup` 53, `tests/admin` 51).
- **Settings and API clients** (#43, `wp11.md`): Settings connect and check storage, AWS, identity, Teams and Storage Mover, and store no secret. API clients have named owners, and OmniFile API keys sit beside Entra (decisions 52, 53). The security review closed self-approval through a minted key, re-checks at approval, refused-key flooding and `web`'s write rights on backends.
- **Migrations in the portal** (#41, #45; `wp12.md`): the New migration wizard, the migration page, and the operations console (agents, needs-attention items with their fixes, staged waves on a schedule, a Gantt view). Portal 194/194, mock Playwright 2/2.
- **Visual views** (#40, #46; `wp13.md`): the file journey, the share map and a draggable policy timeline, in light and dark. The portal loads them, Settings and Migrations as lazy chunks; the main chunk is 509 kB at #45 (it was 636).
- **Release engineering** (#38, `wp6.md`): one version (0.1.0), SBOMs, a licence check, a vulnerability scan, release notes and a runbook that publishes the sources first and the feed last. The dry run installs an update through a fake GitHub. A real release fails on purpose until its waiver for ca-certificates USN-8944-1 is gone (it is dry-run only).
- **The customer guide** (#33, `wp7.md`): 82 steps, linted against the Bicep and the repo (`docs/guide/check.sh` ok, `docs/guide/test.sh` 61), the Windows client kit (Pester 64/64 at its last run), and CLA and DCO drafts for counsel.
- **The real-cloud harness** (#36, `wp5.md`): `tests/real/run.sh` plans, deploys, runs the exits and checks, collects and tears down, with an allow-list, an approval per gate and a teardown that handles WORM first. Its dry runs against fakes exit 0 with no residue for `azure`, `aws` and `tenant`; its own suite passed 185.

Built before:
- the portal's Activity, Migrations and Edges pages;
- observability (decision 37): service metrics to Application Insights, alert rules, and edge logs through Arc;
- the edge heartbeat, now running, with certificate renewal (it never ran before);
- the appliance image (decision 38; `appliance/`, spike results in `docs/design/appliance-image.md`). mkosi builds it, with a verity-protected `/usr` and A/B updates through `systemd-sysupdate`. A version is blessed only once the smb-guard kernel check passes at boot. Smoke and update-round tests run in qemu; Hyper-V, vSphere and Arc wait for a client site.
- the setup wizard (`omnifile-setup`, `docs/design/setup-wizard.md`):
  - plain-language questions on the console, or unattended from a systemd credential;
  - network, the xfs data disk, the AD join and admin group, a sealed and audited break-glass account, enrolment, private networking (now enforced by the edge itself), and the update feed and restart window;
  - the portal issues the enrolment codes (Edges, Add edge);
  - tested with a real Samba AD DC and on the appliance in qemu.
- adding datasets and shares in the portal (`docs/design/share-provisioning.md`): the service checks every rule first, and the edge daemon renders each share's Samba and Ganesha config, reloads both servers, and gives the share root its groups' ACL. Folders can be shared once their dataset's root is, and removing a share keeps its data (never a WORM share);
- TPM-held keys: the edge's mTLS key, and a pending CSR's key, are sealed with `systemd-creds` (TPM 2.0 plus the host key), never on disk in the clear. They were tested inside the daemon's own sandbox on the appliance, with a vTPM.
- the service deployment (`deploy/azure/README.md`): the Bicep builds and lints clean (`deploy/azure/check.sh`), and every `VERIFY` left needs a real deployment, a tenant or pilot 4 (one table in the README); the ingress allow-lists of `web`, `enrol` and the relay are required, so a deployment is closed by default, and the relay's is taken from the `AzureBotService` service tag at deploy time (`deploy/azure/relay-ranges.sh`, decision 49); a catalog restore drill runs locally (`deploy/azure/restore-drill.sh`).
- every app signs in to PostgreSQL with Entra as its own managed identity and group role (`catalog/connect.py`; README, "The catalog connection"), which the template's settings now match; before, no app could have reached the catalog as deployed.

Still open in M6 (L: local; P: needs an account; O: the owner):
- P: the exit itself, through `tests/real/run.sh`, and every line of `real-azure-checks.md`, `real-aws-checks.md` and `real-tenant-checks.md`, cloud connect's RA-43 to RA-52 and RW-33 to RW-40 among them;
- P: the signed x86-64 gate under KVM, the fixed VHD on an Azure Gen2 VM, and Arc connecting;
- P: B1ms with the pools under a real run, the live feed's long-poll behind the real ingress, and the annex's Kerberos mounts on the appliance (whether `cifs.upcall` finds the unit's ticket under `PrivateTmp=yes`, and whether `rpc.gssd` gets one for `sec=krb5`);
- L: the local rehearsal (`tests/real/rehearsal/m6-local.sh`); the verifier's own database role; `cifs.spnego.conf` is in `/etc`, which an A/B update from a pre-annex image wouldn't add (no such image has shipped);
- O: the accounts and `tests/real/allow-list.local`, the registry and where clients download the appliance (`OMNIFILE_APPLIANCE_IMAGES_URL`), where each release publishes the AWS connect template (`OMNIFILE_AWS_CONNECT_TEMPLATE_URL`), the two Key Vault Premium keys, the public feed and sources repositories, and the Hyper-V and vSphere imports at a client site.

**M7 is built as far as it can be without a client, its exit unproven**, starting with the threat model and a security review (`docs/design/threat-model.md`). Three independent reviews (the public relay; the service and its Azure deployment; the edge's privileged paths) found one blocking issue, a Writer taking over a folder share's root, and eleven should-fix issues. All are fixed, each with a test that fails without it where one can be written.

Built on 2026-11-05:
- **No partial versions** (#54): a file an NFSv4 client holds open for write isn't committed, through a new FSAL socket query (`OPEN_STATE`, feature bit 5; `fsal-socket.md` §5.5), and an NFSv3 file that went quiet with no close must hold its size and mtime for a further quiet period. This fixes the half-written version WP3 saw committed during a 10 GiB NFSv4.1 copy. Lanes in the PR, before its review fixes: `fsal/tests/t_open_state.sh` 7/7 (all 7 fail on the old FSAL); the FSAL guard suite 335 passed and 3 failed on its first full run, all in `t_chunk_map` (two feature-mask checks, updated for the new bit, and one kill -9 case), then `t_chunk_map` and `t_open_state` 53/53; edge `omnifile-edge` lib 85, chunks 10, daemon_linux 28, lost_commit 14, nsync 21, path_safety 1, review_m1 12, `fsal-proto` 35, `omnifile-detect` 22, clippy clean. Not run yet: the large-copy check on the dev VM.
- **A recall whose edge is gone fails** (#55): a worker sweep fails a `downloading` recall when its edge has been silent, or has reported no fill running, for 60 minutes (the defaults), never on elapsed time: a 2 TB fill whose edge keeps heartbeating stays `downloading` for days. Silence counts only inside the service's reachable window (migration 0040), so a service outage fails nothing; users are told on the card; migration 0039 adds `last_fill_at`. Service 1640 passed, 50 skipped (in the PR, before its review fixes).
- **A full volume during a fill is resumed** (#62): ENOSPC and EDQUOT are interruptions, resumed with the backoff up to 8 times in 30 minutes, then reported. The 6-hour soak rerun on main before it (seed 597205368) had 0 zeros, 0 wrong bytes, 0 ENOENT and 0 lost commits, and was red only on 4 recalls that failed for ENOSPC during a `fill_disk` fault. Edge lib 109, chunks 10, daemon_linux 27 (one skipped: POSIX ACLs on this kernel's xfs), lost_commit 14, nsync 21, path_safety 1, review_m1 12; clippy clean.
- **Annex phases A2.1 and A2.2** (#57, #61; `wp14-a2.md`; decision 59): if-then rules with a visual builder, dry runs, and proposals an ArchiveAdmin approves (#57, nothing written to a NAS); then removals (#61), the first code that writes to a filer, all in `annex/remover.rs`. Its four writes (hide by rename, delete after the grace, put back, pointer file) each run only on a per-file ticket that the service checks again before the write, and a journal means a restart never deletes twice. Migrations 0041 and 0042. After merging main: the annex exit test on omnifile-acl 52/52 at #57 and 130/130 at #61 (A1's 52 plus the removals stage); edge Linux 252 then 293 passed, clippy clean; service 1687 then 1732 passed, 50 skipped; portal 210 then 268 passed.

Built on 2026-11-03 and 11-04 (PST):
- **Catalog scale** (#35, #47; `wp4.md`): a bulk seed path merges each batch in one owner-run statement. It seeds about 16,500 files/s, flat from 10^5 to 10^7 (it was 1,378/s), so 10^8 takes about 1.7 hours per worker. Also the Blob Inventory reader, container-level immutability, and a copy check before any original is deleted. #47 made migration 0033 work without a superuser, as on Azure, and the service suite now migrates as a non-superuser, so that class of bug fails the suite.
- **Chaos, grace and edge scale** (#48, `wp3.md`):
  - NFS grace: 23/23 across a Ganesha restart and a SIGKILL. All 12 opens and 8 NFSv4.1 locks are reclaimed, NFSv3 locks are reclaimed through SM_NOTIFY, and a second client is never granted a held lock;
  - A8 on the dev VM: 17,000 of 17,000 readers, 0 zeros, drained in 3 s;
  - a local line-rate ceiling (virtiofs and the Python sim, not line rate): write 386 MB/s, upload 169, verification 516, recall fill 214;
  - a 6.75-hour chaos soak, 8 fault types 12 times each (seed 3125631393): 0 zeros, 0 wrong bytes and 0 ENOENT in 24,930 good reads, and 0 lost commits in 1,817 writes. It was red on 4 recalls stuck in `downloading`, which #52 fixes. The harness now also counts zeros in reads that then fail; the soak reruns on main next.
- **Lost commits** (#42, `wp9.md`): the soak lost files closed just before a daemon kill or clean restart. Now a sweep commits files no close was seen for, the detector's fanotify group lives in the systemd fd store, and a durable log of pending closes is replayed at start. All 8 `lost_commit_linux` tests fail on the old main.
- **Recalls resume** (#52): a failed fill is reported and fails the recall, an interrupted one is resumed with a backoff, and a read that joins a `downloading` recall starts its fill. That explains the soak's 4 stuck recalls and its 31 NFS and 6 SMB EIO reads with no fault active.
- **Annex phase A1** (#51, `wp14.md`; decision 51): a Copy annex on any NFS or SMB server. The edge mounts it read-only and copies verified plain blobs, NAS deletes are tombstones, and ACLs are captured as the NAS holds them, failing closed. Exit test `tests/e2e/annex_a1_test.sh` 50/50 on omnifile-acl; the edge and service reviews found 4 blocking issues, all fixed.

Still open before production:
- P: T5's check on a real deployment (the direct-replica line in `real-azure-checks.md`), and that every Teams call reaches the relay from the `AzureBotService` ranges (a real tenant);
- P: 10^8 seeding on Azure Database for PostgreSQL, and the Annex against real ONTAP and PowerScale, with a separate remove-rights account, an SMB open's sharing violation (the lab's Linux cifs client can't hold one) and how those servers move ctime on a rename;
- the source offer is complete for a whole build (#38), and its first real publish is a release task; signing is built (decisions 47 and 48, `docs/design/secure-boot.md`), and the keys are the owner's;
- O: the Windows client matrix and 200 GB SEG-Y at line rate (pilots 1 and 2, on site), counsel's review of `docs/legal/`, and a first client deployment.

Built before:
- the Key Vault CA signer: `enrol` signs edge certificates with an HSM key it never holds;
- the namespace sync: users' renames, moves, deletes and chowns reach the catalog (`docs/design/namespace-sync.md`);
- per-app database privileges (`catalog/privileges.py`), applied by every migration run and exercised by every service test;
- the worker's daily partition upkeep for the feed and audit log, which nothing ran before.

Scale and chaos before the bulk seed path (`docs/milestones/M7.md`): the row-by-row seeder ran at about 1,400 files/s, and steps 11 to 13 of the M5 test check that a control-API outage, a loss of the edge's network and a daemon killed mid-fill from S3 fail closed and recover (the fill resumes where it recorded its progress). For Windows clients (risk R4), stubs are marked offline to SMB clients and Offline Files is disabled per share; the client matrix itself runs on site (`docs/pilots/windows-client-matrix.md`). Independent reviews of the namespace sync and the database privileges found four blocking issues, and a re-review one more; all are fixed, each with a test.

Next, locally: M1, M2, M3 and M5 again on main, with #54's large-copy check, and the chaos soak again on main to confirm #62; Codex on the merged PRs; the verifier's own database role (it connects as the worker's, `wp1.md`); "Pause and restore" for an annex's hidden files, which A2.2 left unbuilt; and the next Annex phases (`docs/design/annex.md`).

**Research phase complete. Pilot 1 (filer spike) passed locally** (`docs/pilots/filer-spike-results.md`): NFS through `FSAL_OMNI`, and SMB through the fail-closed fanotify guard. Its on-prem part (AD/Kerberos, Windows clients, remote backends) and pilots 2–4 run at a client site (`docs/pilots/remote-pilots.md`). The full pilot plan is in `docs/PILOTS.md` (four parallel spikes, ~4 weeks): filer spike (Ganesha FSAL + stubs + identity + fail-closed), network throughput test through the client's ExpressRoute gateway, data test (SEG-Y compression ratio and duplicate share), Azure test (smart→archive copy, real recall times, a real Teams card).

The questions open with the owner are the four in `docs/design/multicloud.md` section 17, all about Google Cloud and waiting for that stage. They are left by the decisions of 2026-11-11, 68 to 81 (any cloud as a home, the service on any cloud, native tools for every cloud-to-cloud copy over public endpoints, what OmniFile owns and keeps doing, AWS first, the transfer location as the default, an admin-started clean-up of a home a dataset has left; 77 to 80 by delegation). Nothing else is open. The owner's decisions of 2026-11-06 are 63 to 67 (the read set, shared blobs, the NFSv3 quiet period), and those of 2026-11-04 are 51 to 58 (the Annex, API naming and owners, API keys, NFSv3 locking, no recall limits, share map totals, the tombstone list, the test-only AWS name suffix), then 59 (the Annex's removal rules), and 60 to 62, recorded 2026-11-05 (the small size and its regions, the portal's look, who sees prices). Decision 36 (2026-10-15) settled how SMB fills a file opened directly in an unfilled label, with a GPL-3 Samba VFS module. Decisions 19–26 closed the research-phase questions. The code is Apache-2.0 since decision 86 (2026-11-16), which retired the BSL and the legal review of its grant; outside contributions are taken with a DCO sign-off (decision 87, 2026-11-19), and the public repository is `OmniFile` with the update feed kept private for now (decision 88). Native-feature research (`docs/research/2026-09-26-native-features.md`) and the verification pass (`docs/research/2026-09-26-verification.md`) are done.

## Provenance

