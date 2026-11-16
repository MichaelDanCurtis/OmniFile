# Vendor-agnostic OmniFile: Azure, AWS and Google Cloud

**Status: a design for decisions 68 to 81 (2026-11-11). Almost none of it is built.** The decisions are in `docs/DECISIONS.md`. Where this note goes beyond them, it says "proposed". Section 17 lists what the owner still has to decide.

The decisions, in short:

| # | Decision |
|---|---|
| 68 | Any of Azure Blob, Amazon S3 or Google Cloud Storage can be a dataset's permanent home. Reverses decision 18. |
| 69 | The service runs as containers on any of the three clouds. Entra and Teams stay. Amends decision 17. |
| 70 | The destination's native tool does every bulk cloud-to-cloud copy. OmniFile is never in the data path. |
| 71 | Those copies run over the providers' public endpoints, encrypted in transit. OmniFile builds no private path between clouds. Amends decision 22, which is unchanged for the edge and the service. |
| 72 | OmniFile owns the on-premises agent that archives to the cloud: shares, and the Annex. Both must work with any home. |
| 73 | The product: a software filer with migrations and the archive, that migrates easily and then stays as the file sync service. Not a NetApp replacement. |
| 74 | OmniFile keeps starting and watching each copy job, restoring archived objects first, verifying every file before cutover, and showing jobs and agents. |
| 75 | AWS comes first after Azure, as a home and as a host. |
| 76 | A copy tool is fed through a **transfer location** by default; reading the client's storage directly is the option. OmniFile opens and closes a transfer location's network rules itself. Amends decision 24. |
| 77 to 80 | By delegation: "all networks" on a transfer account only while a run reads it; a CRC32C beside every CRC64-NVME; an estimate of the source side's bill before a migration starts; a deployment on another host keeps Entra, Teams and, if needed, one Azure resource. |
| 81 | Old copies in a home a dataset has left are removed only by a clean-up an ArchiveAdmin starts, never by the migration. |

The evidence is `docs/research/2026-11-11-multicloud-verification.md`, cited as `[R]` with its section. It marks what was read on a vendor's page, what was only seen in a search excerpt, and what could not be confirmed. `[V]` is September's pass, `2026-09-26-verification.md`. **Nothing was tested on a real account.**

## 1. What "vendor-agnostic" means here, and what it doesn't

Four things, all in scope since decisions 68 to 70. They are built in stages (section 15).

| Level | Meaning | Today |
|---|---|---|
| A. Any direction | A dataset's bytes move between any two of Azure Blob, S3 and GCS, behind one share, copied by the destination's tool and verified by OmniFile | S3 → Azure only |
| B. A third backend | GCS implements the backend contract | Azure Blob and S3 |
| C. Any home | A dataset lives on any of the three for good, with tiering, Archive, versioning and WORM there | Azure only |
| D. Any host | The service itself runs in any of the three | Azure only |

What it does not mean:

- **Not a storage format of our own.** Plain objects only, readable by each cloud's own tools.
- **Not a copier of our own,** and not a network of our own between clouds (decisions 70, 71).
- **Not a lowest common denominator.** Policies stay in logical tiers. Each backend says what it can't do, and the policy editor warns in that provider's terms.
- **Not independence from Microsoft identity.** Every client has Active Directory synced to Entra ID, and tells users through Teams. A home or a host on another cloud changes where bytes and compute are, not who signs in.
- **Not a NetApp replacement** (decision 73). OmniFile works with a NAS (decision 35, the Annex) and doesn't replace one.

**What OmniFile owns (decision 72)** is unchanged by any of this: the appliance that serves NFS and SMB and archives itself to the cloud, and the Annex that reaches into another filer and archives its files. Neither changes here. What changes is that "the cloud" each archives to can be any of the three, so the edge's upload, fill, stub and Annex paths must work against S3 and GCS as they do against Azure (section 12).

**Who does what in a migration (decisions 70 and 74):**

| Step | Who | Bytes through OmniFile? |
|---|---|---|
| Copy the bytes | The destination cloud's tool | No |
| Start, watch and re-run the job; show jobs and agents | OmniFile | No: API calls |
| Restore archived objects the tool won't read | OmniFile asks the source provider | No: the provider restores in place |
| Put each batch in the transfer location (the default, decision 76) | OmniFile asks the source provider for a server-side copy | No |
| Verify every file | OmniFile | Only where a cloud stores no checksum: a read inside that cloud (section 5) |
| Stamp tier, metadata, retention | OmniFile | No |
| Switch users over | OmniFile, when verified | No |

## 2. The direction matrix

| Direction | Native tool | Status | The catches |
|---|---|---|---|
| S3 → Azure | Azure Storage Mover | GA. Built (M5). | One subpath per job, no object list, no destination tier. `[R 2.2]` |
| Azure → S3 | AWS DataSync, Enhanced mode | GA | Can't read the **Cold** tier. Archive blobs must be rehydrated first. No blob over 5 TB. Publishes no source addresses, so a firewalled account can't name it. `[R 2.3, 5.1]` |
| S3 → GCS | Google Storage Transfer Service | GA | **Skips Glacier Flexible Retrieval objects even when restored.** Deep Archive is copied once restored. 5 TiB per object. `[R 2.4]` |
| Azure → GCS | Google Storage Transfer Service | GA | Whether it reads Cold and Archive blobs is unconfirmed. `[R 2.4]` |
| GCS → S3 | AWS DataSync, Enhanced mode | GA | Reads GCS through its XML API. Metadata is copied "best effort". `[R 2.3]` |
| GCS → Azure | Azure Storage Mover | **Preview** | Reads GCS through its S3-compatible API with an **HMAC key stored in Key Vault**. `[R 2.2]` |

"GA" means the vendor's pages don't mark the path as preview `[R 2.1]`.

- **By default no tool reads the client's storage.** Each batch is copied inside the source cloud into a transfer location OmniFile owns, and the tool reads that (decision 76, section 7). The catches above about what a tool can't read or name then fall on OmniFile's staging, not on the client. Reading the client's storage directly is the option (section 9.3).
- **Five directions have a GA tool that needs no stored credential.** DataSync is AWS's equivalent of Storage Mover, and since November 2026 it federates to Entra and to Google with no SAS and no key.
- **Google's tool only pulls in.** Nothing native pushes out of Google Cloud, so the two directions out of GCS rest on AWS's and Microsoft's tools.
- **DataSync is the one tool that goes both ways.** It can also write S3 → Azure Blob, and it takes object lists. Decision 20 names Storage Mover for that direction, and this note doesn't change it.
- **GCS → Azure is the weak direction.** Until its tool is GA and keyless, it ships with `copy: external`: the client or Prizmanyx runs any copier, and OmniFile watches the target, verifies every file and cuts over (`migration-engine.md` section 9.1). A route on GA tools alone also exists: GCS → S3 with DataSync, then S3 → Azure with Storage Mover. It pays egress twice. Section 17, question 1.

## 3. The copier interface

### 3.1 What the engine needs from any copier

The engine already drives a port, not Storage Mover itself (`migrate/mover.py`): `ensure_job`, `start_run`, `run_status`, `running_run`. The sim mover implements it for tests. So this is an extension, not a rewrite.

| The engine needs | Storage Mover today | DataSync | Storage Transfer Service |
|---|---|---|---|
| A job for one prefix | `PUT jobDefinitions/{name}` with a source and target subpath | Two locations (the source one with a `Subdirectory`) and `CreateTask`, Enhanced mode | `transferJobs.create` with a source path and a sink path |
| Copy only what is new, delete nothing | `copyMode: Additive`, never Mirror | Transfer changed data only; keep deleted files | Overwrite when different; never delete at the sink |
| Start a run | `POST startJob` | `StartTaskExecution` | `transferJobs.run` |
| Status and counters | `GET jobRuns/{run}` | `DescribeTaskExecution` | `transferOperations.get` |
| Find a run a crashed step started | The job definition names its latest run | List the task's executions | List the job's operations |
| A stable name, so a retry makes no second job | The job definition's name | A task's ARN is assigned by AWS, so the driver tags the task with its name and looks it up | The job's name is the caller's |
| Agents, for the console | `list_agents` | None in this design (no agents) | None |

Field and option names for DataSync and Storage Transfer Service are as search results quoted their API references `[R 2.3, 2.4]`. Each is confirmed against the reference when its driver is written, as `storage_mover.py` marks its own.

### 3.2 What the port gains (proposed)

- **Capabilities.** A copier says what it can do, as a backend does. The engine reads them instead of knowing which tool it has.

  | Capability | Storage Mover | DataSync | Storage Transfer Service |
  |---|---|---|---|
  | Takes an object list | No | Yes (manifest) | Yes (manifest) |
  | Source classes it can't read | Unrestored Glacier, Deep Archive | Azure Cold; Azure Archive | Glacier Flexible Retrieval at all; unrestored Deep Archive |
  | Sets the destination class | No | Yes | Yes |
  | Largest object | Not stated | 5 TB | 5 TiB |
  | Publishes its source addresses | No | No | Yes |
  | Options that delete, forced off | Mirror | Remove deleted files | Delete at source after transfer; delete at sink |

- **A source that is a transfer location.** By default a job's source is the migration's prefix in the transfer location, which holds exactly the batch to copy. Every tool can do that, Storage Mover included: it is one subpath. The job's target is the prefix's real path, as the urgent-recall staging does today.
- **A run over a list,** for the direct option. `start_run` may take the objects to copy. A driver that takes a list writes a manifest in OmniFile's own operations bucket and runs over it. Storage Mover takes none, so direct S3 → Azure reruns the whole prefix in Additive mode, which is what M5 built.

The engine's steps don't change: copy, find the copies, verify, stamp, cut over. Its names do: `storage_mover_job` becomes the copier's job, its code and messages say "source" where they say "S3", and `posix_metadata` comes from the target backend, not from `backends/azure`.

### 3.3 What each driver does differently

- **Storage Mover.** As built. The customer makes the endpoints; the driver makes job definitions and runs.
- **DataSync.**
  - The driver creates the locations and the task. A location carries the federation parameters (an IAM role, a tenant id, a client id), which are names, not secrets.
  - A task's mode is fixed when it is created, so tasks are always Enhanced.
  - It can land objects in a chosen S3 class. The proposal keeps today's order: land in Standard, verify, then stamp the tier.
  - Blobs it can't read (Cold, Archive) reach it through the transfer location, hot. A blob over 5 TB can't go to S3 by DataSync at all, and is reported before the run, from the catalog.
- **Storage Transfer Service.**
  - Jobs live in the client's Google Cloud project and run as Google's service agent for that project.
  - The two delete options are never set, and the role the service agent is given at the source can't delete anyway (section 11).
  - Glacier Flexible Retrieval objects reach it through the transfer location, as Standard. In the direct option they are staged all the same, unless Google allow-lists the client's project.
  - It reports the two kinds of skipped Glacier object in their own counters, which map onto the port's `unsupported`.

## 4. Native features not used, and why

The native-features rule: a native tool is rejected only for a recorded reason.

| Native feature | Verdict | Reason |
|---|---|---|
| Storage Mover Mirror; DataSync "remove deleted files"; Storage Transfer Service delete options | Rejected | They delete. A migration never deletes (decision 40). |
| DataSync agents; Storage Mover private connections; Storage Transfer Service over a customer's interconnect | Not built | Decision 71. Each also has its own catch: an agent is a VM to run, Storage Mover's connections are preview, an interconnect is the client's network project. |
| Each tool's key or SAS route | Not offered | No stored secrets (decision 43). |
| Autoclass with Archive as its terminal class, as the whole of "smart + archive date" | Rejected for that use | Can't express the policy: it archives on access patterns, never on a date (section 6). |
| Azure "Put Block From URL" pulling from a GCS signed URL, for GCS → Azure | Rejected | OmniFile would be driving a copy per object, which is a copier by another name (decision 70). |
| Storage Mover's GCS source | Not relied on | Preview, and it stores a key. Section 17, question 1. |

Where a native tool has a gap, OmniFile fills it with another native feature, as decision 24 did: a server-side copy into a staging area, a manifest, or a checksum the provider computes.

## 5. Verification across clouds

Decision 24's rule, kept by decision 74: **every file, full content, never sampled, and a prefix never flips on a copier's word.** Its method is not general. It compares S3's CRC64-NVME with a CRC64-NVME read in Azure, and GCS has no CRC64-NVME `[R 3.2]`.

Three facts decide the method for each pair:

- **GCS stores a CRC32C for every object,** composite ones included. It is metadata: reading it reads no data.
- **S3 can produce a full-object CRC32C or CRC64-NVME for any object at rest,** in any class, with no restore, through Batch Operations Compute checksum.
- **Azure stores no whole-blob checksum.** But for every blob OmniFile wrote, the catalog holds the CRC64-NVME it was verified with.

| Pair | Expected: the source's value | Actual: the copy's value | Bytes OmniFile reads | Egress |
|---|---|---|---|---|
| S3 → Azure | S3's CRC64-NVME (HEAD, or a Batch job) | A read of the copy inside Azure | The copy, once, in Azure | None |
| GCS → Azure | GCS's CRC32C (metadata) | A read of the copy inside Azure, computing CRC32C, and CRC64-NVME in the same pass for the catalog | The copy, once, in Azure | None |
| Azure → S3 | The catalog's CRC64-NVME | S3's CRC64-NVME (HEAD if the object carries one, else a Batch job on the destination) | None | None |
| Azure → GCS | The catalog's CRC32C, if recorded. Otherwise a read of the source inside Azure, computing CRC32C and checking CRC64-NVME against the catalog. | GCS's CRC32C (metadata) | None, or the source once, in Azure | None |
| S3 → GCS | S3's full-object CRC32C (HEAD, or a Batch job) | GCS's CRC32C (metadata) | None | None |
| GCS → S3 | GCS's CRC32C (metadata) | S3's CRC32C (HEAD, or a Batch job on the destination) | None | None |

- **No pair needs bytes to leave a cloud to be verified.** Two pairs need no read at all.
- **Every read happens in Azure,** the one cloud with no stored checksum. So the verifier is a container that runs in the client's Azure whenever Azure is one end, wherever the service is hosted. A client with data in Azure has an Azure subscription to run it in. A deployment with no Azure storage runs no verifier reads.
- **A CRC32C is recorded beside the CRC64-NVME for every new write** (decision 78), computed by the edge in the same pass, from stage 1. It saves a full read of every file in a later move to GCS. Files already stored get theirs from the in-Azure read when they migrate.
- **Reading GCS metadata does not move an Autoclass object** back to Standard `[R 3.1]`. A data read would, so verifying by metadata also keeps tiers where they are.
- **Objects no provider can checksum** hold up their prefix, as SSE-C objects do today. The same may apply to GCS objects under customer-supplied keys; unconfirmed.
- **A foreign Azure source** (blobs OmniFile never wrote) has no catalog checksum, so its expected value is a read inside Azure.
- **The copier's own verification is never the answer.** DataSync's Enhanced mode checks only what it transferred, and Storage Transfer Service may fall back to size for some objects `[R 2.3, 2.4]`.

The catalog's `verified_by` gains a value per method, and `Capabilities.checksum` (already a field, `crc64nvme` today) says which algorithm a backend vouches for.

## 6. The GCS backend against the contract

### 6.1 The tier table

A third column for `AGENTS.md`'s table, to be added there when the backend is built. Marks are in `[R 3]`.

| Logical | Google Cloud Storage |
|---|---|
| hot | Standard |
| warm | Nearline (30-day minimum) |
| cold | Coldline (90-day minimum) |
| archive | Archive (365-day minimum; **no restore: read in milliseconds**; retrieval fee) |
| smart | Autoclass on the bucket (objects under 128 KiB stay Standard; a data read returns an object to Standard; an explicit class is ignored) + OmniFile's archive date, by a copy into a second bucket |
| private network | Private Service Connect endpoint for Google APIs, reachable over Cloud Interconnect or VPN; or Private Google Access |
| edge credentials | Workload Identity Federation with an X.509 certificate from OmniFile's edge CA, or service-minted signed URLs |

### 6.2 What is different from Azure and S3

- **No restore.** Recalls from GCS are a fill, not a wait: no recall card, no four steps, no wave. The 365-day minimum is the price: an archive date set too early is charged for the year. Section 17, question 3.
- **Smart plus an archive date needs two buckets,** as Azure needs two accounts. Autoclass moves objects on access only, ignores an explicit class and forbids class-changing lifecycle rules. So on a file's archive date the service copies it into a non-Autoclass bucket as Archive, verifies the copy by its CRC32C, and deletes the original. Section 17, question 2.
- **Tiering outside Autoclass** is a rewrite, or a lifecycle rule. Whether either makes a new generation is unconfirmed, and it decides whether WORM shares can tier in place (section 10).
- **Ranged reads carry no checksum of the range,** as on S3. The edge exposes a GCS-resident file as it does an S3-resident one (`fsal-namespace.md` section 5): the whole file after its checksum matches, or by the chunk path. How the chunk path gets a checksum GCS vouches for is a design item.
- **Changes come from Pub/Sub,** pulled by the service, as S3's come from SQS. There is no ordered log; reconciliation uses inventory reports.
- **Tags:** object contexts (up to 50 per object) are the nearest thing to index tags. Holds and state can live there, or in custom metadata, which can be edited in any class.
- **POSIX attributes** as custom metadata: `file_owner`, `file_group`, `file_permissions`, well inside 8 KiB.
- **Bulk jobs** (storage batch operations) need Google's Storage Intelligence subscription. The backend should not depend on them.

### 6.3 The contract, method by method

| Contract | GCS |
|---|---|
| `put` | Resumable upload carrying a CRC32C, which GCS checks |
| `get_range` | Ranged GET |
| `head`, `list`, `list_versions` | Object metadata (size, CRC32C, class, generation, retention); listings; generations |
| `set_class` | Rewrite or lifecycle rule; not available in an Autoclass bucket |
| `copy_object` | Rewrite, server-side |
| `restore`, `restore_status` | Not needed, in any class |
| `delete` | Refused on a source (section 11) |
| `set_retention`, `set_legal_hold` | Object Retention Lock, Locked mode; a temporary hold |
| `set_tags`, `get_tags`, `set_metadata` | Object contexts; custom metadata |
| `edge_credential` | A pointer for the edge's certificate federation, or a signed URL |
| `read_changes` | A Pub/Sub pull subscription |
| `capabilities` | `checksum: crc32c`, no restore options, the two-bucket flag for smart, 50 tags |

`BackendKind` and `CredentialMode` are open enums already (decision 35), so a new value breaks no API client.

## 7. The transfer location, restores and batches

### 7.1 One concept for every source cloud (decision 76)

A **transfer location** is storage OmniFile owns in the source cloud: the S3 staging bucket that exists today, an Azure transfer account, later a Google bucket. No dataset reads it, and it is empty between batches.

For each batch:

1. **Restore** what is archived, where the provider needs it (7.3).
2. **Copy the batch in,** inside the source cloud, by the provider's own server-side copy (7.2). The client's storage is read, never written (decision 40).
3. **The destination's tool copies the batch out,** over the public endpoint, to the prefix's real path.
4. **Verify every file** against the original object's checksum, not the staged copy's (section 5).
5. **Empty** the transfer location of each file once it is verified.

- **This is the default, for S3 → Azure too.** M5 stages only urgent recalls, and Storage Mover reads the client's bucket for everything else. That becomes the option (section 9.3). Decision 76 amends decision 24 here.
- **Batches overlap.** While the tool copies one batch, the next is being staged, so only the first batch's staging is a wait. After that the pace is the slower of staging and the tool.
- **A batch is sized by bytes,** so the temporary storage has a ceiling the plan shows. An ArchiveAdmin can change it.
- **A staged copy pins what it copied:** the object's version, or its ETag and size where the key is unversioned, as urgent-recall staging does today. An object overwritten after staging is found by the journal gate and goes in a later batch.
- **A staged copy keeps the source's checksum where the provider gives one.** S3 computes a full-object CRC64-NVME for the copy, which must equal the original's: a check for free, before the tool runs. A composite checksum is not comparable across a copy, so only full-object values are used. `[R 7]`

### 7.2 How a batch is copied in

| Source | The copy | Limits `[R 7]` |
|---|---|---|
| S3 | `CopyObject` up to 5 GB. Above that, a multipart copy: `UploadPartCopy` for each part, then complete. Built (`S3Backend.copy_object`), with a full-object CRC64-NVME. | Parts of 5 MiB to 5 GiB, 10,000 parts. S3 Batch Operations Copy takes objects only up to 5 GB, so it can't carry seismic files; it is a later speed-up for small objects. |
| Azure | `Copy Blob` into the transfer account, in the same region. For an archived blob the same call is the rehydration. | Across accounts needs service version 2021-02-12 or later and the same region. Microsoft's example authorises the source with a SAS; that it works with Entra alone, from a private-only account, is to confirm. `Put Blob From URL` stops at 5,000 MiB; larger blobs go block by block with `Put Block From URL` (4,000 MiB a block), with Entra authorisation of the source. |
| GCS | A rewrite into the transfer bucket | Not checked |

- **A multipart copy is many calls per object, driven by OmniFile.** The bytes never leave S3. Today's staging copies 10 objects a step, parts in sequence, 512 MiB at a time: a file of a few hundred GB takes hours. Whole batches need parts and objects copied in parallel, within S3's request limits. That is the main work of the batch mode (section 15, stage 2).
- **What a copy can't carry,** it is told to: the engine stamps metadata, tags, tier and retention on the final copy, as today.

### 7.3 Restores, per source

| Source | What must be restored | How | Then |
|---|---|---|---|
| S3 | Glacier Flexible Retrieval, Deep Archive, Intelligent-Tiering's archive tiers | S3 Batch restore; completion by EventBridge to SQS. Built. | The restored objects are a batch: copied into the transfer location as Standard |
| Azure | Archive | The copy into the transfer account is the rehydration (`Copy Blob` to an online tier). Completion from the change feed. | Nothing more: the blob arrives hot |
| GCS | Nothing | No waves | Copied like any other object |

- **Cold blobs in Azure, and Flexible Retrieval objects for Google's tool,** stop being special: each tool reads only hot copies in the transfer location.
- **Leaving Azure Archive is slow.** Priority rehydration is capped at 10 GiB an hour per storage account, and no time is promised above 10 GB (`AGENTS.md`). A migration of a large archive out of Azure is paced by that, not by the copier or the staging. The plan for a prefix must say so before it starts.
- **Urgent recalls** keep their own lane: a user's recall of an object that hasn't moved yet is staged ahead of the batch, as today.

## 8. Credentials with no keys

Decision 43's rule (no keys anywhere) holds on every pair but one, and on every host.

**The service to each cloud, by host.** Its own cloud is always its native identity. The other two are federation from that identity:

| Host | To Azure | To AWS | To Google Cloud |
|---|---|---|---|
| Azure | Managed identity. Built. | Web identity from the managed identity (decision 43). Built. | Workload Identity Federation trusting the Entra tenant `[R 3.7]` |
| AWS | Entra workload identity federation, trusting the AWS account's outbound issuer, as DataSync does `[R 2.3]` | Task role | Workload Identity Federation's AWS provider (not read) |
| Google Cloud | Entra workload identity federation, trusting Google's issuer (not read) | Web identity from the service account (not read) | Service account |

**The edge to each cloud** doesn't depend on the host:

| To | How | Status |
|---|---|---|
| Azure | A service-minted user-delegation SAS. Or an Arc managed identity, where the client has Arc. | Built |
| AWS | IAM Roles Anywhere, certificate from the edge CA, key in the TPM (decisions 43, 50) | Built for reads; writes to a home bucket are new |
| Google Cloud | Workload Identity Federation with X.509: the pool trusts the edge CA, and the edge presents its certificate over mTLS. Signed URLs as the fallback. | GA since May 2025 `[R 3.7]`. **A TPM-held key is unconfirmed.** |

**The copier to the source:**

| Tool → source | How | Stored secret? |
|---|---|---|
| Storage Mover → S3 | Arc multicloud connector, a role made by CloudFormation | No |
| Storage Mover → GCS | HMAC key and secret in Key Vault, read by the endpoint's managed identity | **Yes**, in the client's vault, never held by OmniFile |
| DataSync → Azure | Entra workload identity federation | No (Enhanced mode) |
| DataSync → GCS | Google Workload Identity Federation | No (Enhanced mode) |
| Storage Transfer Service → S3 | A role ARN, assumed by web identity | No |
| Storage Transfer Service → Azure | An Entra application with a federated credential | No |

The client creates the federation on each side, following the guide, as it creates the AWS roles today. The portal's connect flow (`cloud-connect.md`) gains a Google Cloud page.

## 9. Networking

### 9.1 Two paths, two rules

| Path | Who | Rule |
|---|---|---|
| The edge and the service reaching the client's storage: uploads, fills, verification reads, metadata, events | OmniFile | **Unchanged (decision 22).** Private where the wizard selected it, with no public fallback. On an S3 home that is PrivateLink; on a GCS home, Private Service Connect. The client creates them. |
| A cloud-to-cloud copy | The destination's tool | **Public endpoints, encrypted in transit (decision 71).** OmniFile builds no private path between clouds. |

"Public" means the storage endpoint answers requests from the internet. It never means a request needs no credential: every read is by a federated identity that can only read, over HTTPS, and anonymous access and shared keys stay off.

### 9.2 The default: through the transfer location

The tool reads the transfer location, never the client's storage (section 7). So the only storage that answers the internet for a copy is OmniFile's own, holding one batch.

| Source cloud | The transfer location's network | What opens for a run |
|---|---|---|
| S3 | A bucket's endpoint is always public; access is by policy. The transfer bucket's policy admits OmniFile and the tool's role, nothing else. | Nothing. There is no network rule to change. |
| Azure | A transfer account: Entra-only, no shared key, no anonymous access, TLS 1.2 or later. **Closed by default.** | Its network rules, for the run: Storage Transfer Service's published ranges, or all networks for DataSync, which publishes none (decision 77). |
| GCS | A transfer bucket with no IP filter, reachable with credentials | Nothing, unless the client's organisation requires a filter |

**At the destination nothing opens either.** Storage Mover is a trusted service on Azure: a resource instance rule lets it write, even with public network access disabled `[R 5.1]`. DataSync writes inside AWS as its role, and Storage Transfer Service writes as Google's service agent; a bucket policy or filter that would refuse them excepts that one identity.

**An Azure transfer account's opening (decision 76):**

| | |
|---|---|
| Who allows it | An ArchiveAdmin approves the migration in the portal, signed in within the last few minutes. **The approval screen lists the openings it covers:** which account, to which addresses, for how long each, and about how many runs. That one approval covers each run's opening; there is no fresh approval per run. |
| Who does it | OmniFile, by itself, for each run. Its right is to change that one account's network settings, and no other's: none on any account that holds a dataset. |
| For how long | One run. It opens when the run starts and closes when the run ends, or at a deadline (proposed: 24 hours, which an ArchiveAdmin can extend). A batch is sized to fit. |
| What is exposed | One batch, to requests that carry a token for one identity with read on one container. |
| How it closes | The service sets the account back to closed, and deletes each staged blob once its file has a verified copy. A lifecycle rule deletes anything older than a few days, as a backstop. |
| The audit | Every opening and every closing is its own audit record: the migration, the run, the account, what was allowed, when, and the approval it rests on. |
| The proof | The service reads the account's network settings back from Azure and records them in the audit log, with Azure's own change record. The migration page shows "closed, confirmed at". While an account is open the setting is re-read every hour; afterwards daily. An alert fires if it is open with no run, or past its deadline. |

- **OmniFile can't test from the internet** that an endpoint refuses. The proof is the provider's configuration, read back, and the provider's change log.
- **To confirm on real Azure, before anything else:** that `Copy Blob` into the transfer account works with Entra alone while the home account is private-only. **If it doesn't, an Azure source has only the direct option** (section 9.3), and the transfer location serves S3 and GCS sources. It is stage 0's first check (section 15).

### 9.3 The option: direct

The tool reads the client's storage itself. It saves the second write and the temporary storage (section 14), and it means the client's storage answers the tool over the public endpoint.

**The New migration wizard offers both, the transfer location first,** and explains each in plain words:

- **For the default:** the bytes are written twice in the source cloud, each batch is stored there until it is verified, the copies are requests the source cloud bills, and it takes a little longer because the first batch is staged before anything moves. With figures from the plan, for those who may see prices (decisions 62, 79).
- **For direct:** what the client must do, what it exposes, for how long, and how to close it.

| Source cloud | What the client must do | What that exposes | How it closes |
|---|---|---|---|
| S3 | Usually nothing: a bucket is reachable with credentials unless its policy says otherwise. If the policy denies all but a VPC endpoint or an address range, add an exception for the tool's role. | Nothing new: the role can read, and only it is excepted | Remove the exception |
| Azure | Set the account's public network access to selected networks with the tool's ranges, or to all networks for DataSync. Give the tool's identity read on the container. | The whole account's endpoint answers the internet, to requests with a valid token | Set it back, and remove the role |
| GCS | Lift or widen the bucket's IP filter, if it has one | The bucket answers requests with a valid token from anywhere | Put the filter back |

- **OmniFile makes none of these changes.** It holds no right to change the network settings or the policy of storage that holds a dataset. The client makes them, following the wizard's steps.
- **OmniFile checks before the first run** that the tool can read the source, and refuses to start with a plain reason if it can't.
- **OmniFile checks again after the last run,** and each day until it is closed: it reads the account's network rules or the bucket's policy, and the migration page and an alert say so while the source is still open. That read needs one more permission on the source (to read its network settings or policy), which the guide adds for the direct option only.
- **Where the source is already reachable with credentials, the wizard says so.** Most S3 buckets are. There the default costs a second write and buys no less exposure, and an admin should see that before choosing.
- **A client who wants neither** runs their own copier over their own network (`copy: external`). OmniFile still verifies every file.

### 9.4 Not built, and why

| Path | Why not |
|---|---|
| DataSync agents in the source cloud | Decision 71. A VM per source for the client to run, and federation through an agent is unconfirmed. The transfer location removes the need. |
| Storage Mover private connections | Decision 71. Preview, on a Private Link feature that is itself preview. |
| Storage Transfer Service over the client's interconnect | Decision 71. It needs an interconnect between two clouds, a load balancer and a service directory entry. |
| Storage Transfer Service's Google-managed network | Not a private path OmniFile would build: it is a flag on the job. It is here because of what it does to the bill (section 14). Section 17, question 4. |

A client who needs one of these runs it themselves, as an external copier.

## 10. WORM and holds, per cloud

Decision 31's meaning doesn't change: every committed version is immutable for its retention, overwrites and deletes only add versions and tombstones, compliance mode, and a legal hold per file.

| | Azure | Amazon S3 | Google Cloud Storage |
|---|---|---|---|
| Per-version retention | Version-level immutability | Object Lock, retain-until per version | Object Retention Lock, retain-until per object generation |
| Compliance (can't be shortened) | A locked policy | Compliance mode | Locked mode |
| Legal hold | Per version | Per version, no end date | A temporary hold |
| Needs versioning | Yes | Yes | For overwrites to keep the old version |
| Turned on for | The account and container | The bucket; possible on an existing one | The bucket; an existing one only in the console; never off again |
| Tiering under retention | Set Blob Tier in place | Not checked | **Unconfirmed** |

- **A copy carries its retention before it flips.** The engine already waits until every verified copy of a retained or held version carries that retention and hold (`migration-engine.md` section 7). No copier carries retention; OmniFile stamps it, on any backend.
- **The source stays retained.** After a cutover the old object is a retained secondary that OmniFile never writes or deletes.
- **WORM on S3 and GCS homes waits on one check each:** that an object under retention can change class without being replaced. If it can't, a WORM share there has one class for a version's whole retention, and the policy editor must say so.

## 11. Deletes at a source

Decision 40 holds for every source, in every direction: **deletes made during a migration are reported, never applied to the source,** and OmniFile holds no permission to delete or write there.

| Source | What OmniFile and the copier may hold | What they may not |
|---|---|---|
| S3 | Read, list, `s3:RestoreObject`. An explicit deny of every write outside the operations bucket. Built. | Any delete or put on a source bucket |
| Azure | Storage Blob Data Reader on the source container; write on the staging and transfer containers only | Storage Blob Data Contributor on a source. AWS's own example grants it; the guide must not. |
| GCS | Storage Object Viewer on the source bucket | Storage Object Admin on a source. AWS's own example grants it; the guide must not. |

- **Each tool has an option that deletes.** Storage Transfer Service can delete source objects after a transfer. The drivers never set these, and the roles above mean a wrong option fails instead of deleting.
- **The cutover report** (`GET /v1/migrations/{id}/tombstones`) is already backend-neutral.
- **A home a dataset has left is cleaned up separately (decision 81).** When a dataset moves away from a home OmniFile manages, the old copies are in storage the client keeps paying for. The migration still deletes nothing. Afterwards:

  | | |
  |---|---|
  | Who starts it | An ArchiveAdmin, in the portal, signed in within the last few minutes. Never the migration, and never by itself. |
  | When | After a waiting period from the cutover (proposed: 30 days, which an ArchiveAdmin can lengthen). |
  | What it checks first | For each file, again, at that moment: a verified copy in the new home, for the file's current content and for every older version still within its rewind depth. A file without one is left, and named in the report. |
  | What it leaves | **WORM-locked and held copies, until their retention ends or their hold is cleared.** The provider would refuse the delete anyway; the clean-up doesn't try. It can be run again later for those. |
  | What it never touches | A client's own estate that was only ever a source. Its deletes stay a report (decision 40). |
  | The record | Each run is audited with who started it and what it removed and left; each object's removal is in the catalog, as any delete is. |

## 12. A home on S3 or on GCS

Decision 68. A migration end only has to hold objects. A home has to take the edge's writes and run the whole archive life on that cloud.

### 12.1 The design, per cloud

| | Azure (built) | Amazon S3 | Google Cloud Storage |
|---|---|---|---|
| Layout | Two accounts: a live one (ZRS, Smart) and an archive one. WORM: one account. | One bucket per dataset. WORM: a bucket with Object Lock. | Scheduled tiers and WORM: one bucket. Smart: a live Autoclass bucket and an archive bucket. |
| Uploads from the edge | Blocks, each checked by CRC64 | A put or multipart upload carrying a full-object CRC64-NVME, which S3 checks `[V]` | A resumable upload carrying a CRC32C |
| Scheduled tiering | Set Blob Tier | A class change by lifecycle rule or by a copy onto itself | A rewrite, or a lifecycle rule |
| Smart | Smart tier | Intelligent-Tiering (objects under 128 KB never tier) | Autoclass |
| The archive date | A copy into the archive account, then a delete | A move to Glacier Flexible Retrieval or Deep Archive | A copy into the archive bucket, then a delete (smart); a class change (scheduled) |
| Recall | Copy out of Archive, into a recall container. Capped at 10 GiB an hour at priority. | `RestoreObject`: a temporary copy for N days, in place, billed on top. No Expedited from Deep Archive. 1,000 requests a second per account. | None. An archived file opens at once, with a retrieval fee. |
| "Keep online" | A hold, no tier change | Extend the restored copy's days | Nothing to do |
| Versions and rewind | Blob versions | Object versions | Generations |
| WORM and holds | Section 10 | Section 10 | Section 10 |
| Events | The change feed | EventBridge to SQS, pulled | Pub/Sub, pulled |
| Reconciliation | Blob Inventory, or listings | S3 Metadata tables or Inventory | Storage Insights inventory reports |
| Holds and state | Index tags (10) | Object tags (10) | Object contexts (50), or custom metadata |
| The service's credential | Section 8 | Section 8 | Section 8 |
| The edge's credential | Section 8 | Roles Anywhere, now with write | Certificate federation, or signed URLs |

- The S3 column's tiers, restores and limits are `[V]` and `AGENTS.md`'s table. How a class is best changed in a versioned or locked bucket was not re-checked for this note, and is a stage 0 check. A lifecycle rule runs on S3's schedule, not ours, so an archive date would be met within about a day; a copy onto itself is immediate but makes a new version.
- The GCS column is section 6.
- **The "file is archived" experience differs by home, and the share's policy still decides it.** On Azure and S3 a read of an archived file waits or fails fast, with a note file and a Teams card. On GCS there is nothing to wait for.

### 12.2 What changes in the code

The backend contract already carries most of the difference:

| Already there | What it gives |
|---|---|
| `Capabilities.archive_needs_separate_account`, `in_place_archive`, `explicit_tier_leaves_smart` | The tiering worker chooses copy-then-delete or a class change from the backend, not from its kind |
| `Capabilities.checksum`, `restore_options`, `throughput_caps`, `tiers` | The verifier's algorithm, the recall choices and the policy editor's warnings |
| `restore`, `restore_status`, `read_changes`, `set_retention`, `set_legal_hold` | The S3 backend implements them today for a source; a home uses the same calls |
| Open enums (`BackendKind`, `CredentialMode`) | New values break no client |

What is missing:

- **Writes to S3 from OmniFile.** The S3 backend refuses every write outside OmniFile's own areas (`WriteRefused`), because a source is the client's. A home bucket is OmniFile's own area. The guard stays, keyed on whether a bucket is a source.
- **The edge's write path** to S3 and GCS: credentials with write, the upload, and its check.
- **The recall orchestrator** on S3: a restored copy that expires, where Azure's is a copy that stays. The migration engine tracks expiry already.
- **The tiering worker's moves** on S3 and GCS, and decision 66's split of a shared object.
- **Rewind** over S3 versions and GCS generations.
- **The WORM stamp sweep** on Object Lock and Object Retention Lock.
- **The Annex's copies** landing on S3 and GCS, with the same verification.
- **Prices and checks** per provider in the policy editor (decision 62).
- **Setup:** the storage-side template, the guide and a connect page per cloud.

### 12.3 Size

- **S3 as a home: large,** about M3 again. The backend, its fake, restores, events, credentials and the real-AWS check list exist.
- **GCS as a home: very large.** The backend itself is new (a contract suite and a fake first), then the same work as S3.

## 13. The service on any of the three clouds

Decision 69: containers, on Azure, AWS or Google Cloud. A host's own services may be used. Entra and Teams stay.

### 13.1 Each part, per host

The AWS and Google columns are engineering equivalents. **They were not checked against the vendors' pages for this note,** except where marked, and each is a stage 0 check.

| Part | Azure (built) | AWS | Google Cloud | What is common |
|---|---|---|---|---|
| The six apps (`api`, `enrol`, `web`, `worker`, `verifier`, `relay`) | Container Apps, internal environment | ECS on Fargate | GKE | The images |
| `api`'s ingress | TCP ingress; `api` ends mutual TLS itself (threat model T5) | A network load balancer, TCP | A pass-through network load balancer. **Not** an application load balancer: Google's ends TLS and passes the certificate in a header `[R 5.5]`, which T5 refuses. | `api` |
| Catalog and queues | Azure Database for PostgreSQL | RDS for PostgreSQL | Cloud SQL for PostgreSQL | PostgreSQL 16 or later; PGMQ as plain SQL (decision 27); the migrations, which already run without a superuser |
| Each app's database sign-in | An Entra token for its managed identity | An IAM database token for its role | An IAM database token for its service account | One role per app and its privileges (`catalog/privileges.py`) |
| App identities | Six user-assigned managed identities | Six task roles | Six service accounts | One identity per app |
| The key that signs edge certificates | Key Vault Premium (HSM, non-exportable); `enrol` asks it to sign | KMS, an asymmetric signing key | Cloud KMS, an HSM-protected asymmetric key | The edge CA's profile and issuance rules (decision 50) |
| The keys that sign releases | Key Vault Premium | Stay where they are | Stay where they are | They are OmniFile's own, not a deployment's (decisions 47, 48). A client's host doesn't move them. |
| Metrics and alerts | The Azure Monitor exporter; alert rules in Bicep | OTLP to CloudWatch; alarms | OTLP to Cloud Monitoring; alerting policies | The OpenTelemetry instruments. Proposed: alerts defined once as data, rendered for each host. |
| Service logs | Log Analytics | CloudWatch Logs | Cloud Logging | Structured logs on stdout |
| Edge logs | The Azure Monitor Agent on Arc | To design | To design | Open: Arc is Azure's. An edge of a deployment with no Azure needs another route. |
| Private networking of the service | VNet injection and private endpoints | A VPC and interface endpoints | A VPC and Private Service Connect | Decision 22's rule |
| Deployment | Bicep, `check.sh`, the what-if gate, the restore drill | CloudFormation with a change-set gate (`deploy/aws` already uses it) | Terraform with a plan gate | Proposed: one written contract of what every host must provide (apps, settings, identities, roles), which each template's lint checks, as the guide's lint checks the Bicep |
| The one public endpoint (the relay) | A container app; its allow-list from the `AzureBotService` service tag (decision 49) | A load balancer with an address allow-list | A load balancer with a Cloud Armor allow-list | The relay, and the tag's ranges. `relay-ranges.sh` asks the Azure CLI today; other hosts need Microsoft's published file. |
| The bot registration | An Azure Bot resource | The same Azure resource, if one is needed (decision 80) | The same | To check: whether a Teams bot can be registered without an Azure subscription |
| Sign-in, Graph, Teams | Entra | Entra | Entra | All of it |
| The image registry | Container Registry | ECR | Artifact Registry | The build |
| Verification reads | A job in Azure | A job in the client's Azure, only if Azure is one end | The same | Section 5 |

### 13.2 What the code needs

Three things in the service know they are on Azure, each in one place today: the database sign-in (`catalog/connect.py`), the edge CA's signer in `enrol`, and the metrics exporter. Each becomes a small port with one implementation per host, chosen by a setting. Everything else in the service already speaks to Postgres, to backends and to copiers through their ports.

Not in scope of the port work: the edge, the FSAL, the appliance, the portal and the partner API. None of them knows the host.

### 13.3 How one code base is tested on three

| Lane | Runs | What it proves |
|---|---|---|
| Host-neutral lanes | Once: service `pytest` on Postgres, portal, edge | The product, on fakes |
| Port contract suites | Once, over every implementation, against fakes | A host's sign-in, signer and exporter behave as the others do. The backend contract suite is the model. |
| Template lanes | Per host: lint, the plan gate, the image smoke | A host's template provides what the contract says |
| The real-cloud harness | Per host: `tests/real/run.sh` follows that host's guide from nothing, then runs the M1 to M4 exits | A fresh deployment by the guide alone |

Three hosts and three homes are nine combinations, and six directions on top.

- **On fakes: all of them.** The host is a setting and the home is a backend, so the service suite runs each exit against each home, and the port suites cover each host.
- **On real accounts: the diagonal first.** Azure host with Azure home (today), AWS host with S3 home, Google host with GCS home. Those are what a single-cloud client deploys.
- **Then the Azure host with each other home,** because existing clients are there and migrations start there.
- **Each direction once,** on the Azure host.
- The other host-and-home pairs are allowed, and are tested when a client needs one.

### 13.4 Size

- **The ports, with Azure as the only implementation: medium.** It changes no behaviour.
- **AWS as a host: large.** Mostly the template, the guide, the gate and the harness.
- **Google Cloud as a host: large,** and a little more: GKE in place of a managed container service, because of `api`'s ingress.

## 14. Who is billed for what

Checked against the vendors' pricing pages where `[R 6]` says so. Figures there are list prices, to show the shape.

| Direction | The tool | The source side pays | The destination side pays |
|---|---|---|---|
| S3 → Azure | Storage Mover: nothing, and no agent | **AWS bills the bucket's account** for data leaving S3, for the read requests, and for Glacier restores | Azure: nothing for data arriving; write requests and storage |
| GCS → Azure | Storage Mover: nothing | Google bills the bucket's project for data leaving, read requests, and retrieval from cold classes | The same |
| Azure → S3 | DataSync: a fee per GB and per task run, on the AWS account | Azure bills the account for data leaving, read requests, and rehydration from Archive | AWS: nothing for data arriving; write requests and storage |
| GCS → S3 | The same | As GCS above | The same |
| S3 → GCS | Storage Transfer Service: nothing | As S3 above. With Google's managed network: no AWS egress, a lower Google rate per GiB instead; AWS still bills requests. | Google: nothing for data arriving; write requests and storage |
| Azure → GCS | Storage Transfer Service: nothing | As Azure above | The same |

- **The owner's understanding is right for the tool and the destination.** With Storage Mover pulling S3 into Azure, nobody pays Azure for the tool, for an agent or for the bandwidth in.
- **It is not right for the source.** The data leaving S3 is billed by AWS to the bucket's account, with the requests and any restores. That is true of every direction: the cloud being left charges for the leaving.
- **Staging and restores add to the source's bill:** a staged or restored copy is stored twice for a while.
- **OmniFile's own cost is small:** API calls, checksum jobs, and, where Azure is one end, a read of each file inside Azure, which has no egress.
- **The transfer location adds to the source side's bill,** and the wizard says so before it is chosen (section 9.3):

  | The default adds | Billed by |
  |---|---|
  | A second write of every byte, inside the source cloud. No bandwidth charge: it stays in one region. | The source cloud, as copy and write requests |
  | Storage for each batch until its files are verified | The source cloud, at the hot rate, for days |
  | For archived data: nothing extra on S3, where a restore was needed anyway. On Azure the rehydrating copy is the staging. | |
  | A little time: the first batch is staged before anything moves | |

  The direct option adds none of these. Amounts were not read from the pricing pages `[R 6.2]`; the plan computes them from the rate table.
- **Exit waivers exist at AWS and at Azure, and both are for leaving the provider altogether.** AWS credits egress for a customer moving all its data off AWS, after a request to support. Azure credits it after every subscription on the account is cancelled. Neither covers moving one dataset while the account stays. Google's was not found today. `[R 6.3]`
- **A waiver is the client's to arrange,** and outside this project (`AGENTS.md`, decision 16).
- **Before a migration starts, the plan shows the bytes and objects that will leave each cloud,** to everyone who can start one, and an estimate of the source side's bill from the rate table to those who may see prices (decisions 62, 79).

## 15. A staged plan

Sizes: small is one work package; medium is a few; large is a milestone like M5; very large is more than one.

| Stage | Ships | Size | Exit test |
|---|---|---|---|
| 0. Checks | Answers to the open items in `[R 4]`, `[R 5]` and `[R 7]` that the next stage rests on, as lines in the real-cloud check lists | Small | Each line answered, with the run that answered it |
| 1. The seams | Capabilities and transfer-location sources on the copier port; "source" for "S3" in the engine; the three host ports with Azure as their one implementation; a CRC32C beside each CRC64-NVME (decision 78) | Medium | Nothing behaves differently: M1 to M5, the Annex and the permission matrix pass as before, and the port suites pass |
| 2. Whole batches through the transfer location, for S3 → Azure | The batch mode as the default (decision 76); direct as the option, with its checks; the wizard's two explanations and the plan's estimate (decision 79) | Medium | M5's exit test passes in both modes. In the default, Storage Mover never reads the source bucket (the fake S3 shows no request from it there), every file is verified against the original's checksum, and the transfer location ends empty. In direct, a source still open after the last run is reported. |
| 3. S3 as a home | Section 12 for S3 | Large | M1 to M4's exit tests with the dataset's home on S3, on the fake and then on real AWS |
| 4. Azure → S3 | The DataSync driver; the Azure transfer account, its opening and its proof | Large | A dataset moves Azure → S3 with archived and Cold blobs, a held WORM file and a user's recall mid-migration, and **keeps working on S3 afterwards.** Deletes are reported, nothing is written to the source, every opening is shown closed, and M5 still passes. |
| 5. AWS as a host | Section 13 for AWS | Large | A fresh deployment by the guide alone, on AWS with an S3 home, with no Azure storage, passes M1 to M4 |
| 6. GCS | The backend and a home on it; the Storage Transfer Service driver (S3 → GCS, Azure → GCS); GCS → S3 through the DataSync driver; GCS → Azure by external copier; the Google connect page | Very large | The contract suite; M1 to M4 with a GCS home; each of the four directions, verified with no data read out of a cloud |
| 7. Google Cloud as a host | Section 13 for Google Cloud | Large | As stage 5, on Google Cloud with a GCS home |

**Stage 0 starts with two checks, each a go or a no-go:**

| # | The check, on real accounts | Go | No-go |
|---|---|---|---|
| 1 | **Azure:** can an archived blob, and a hot one, be copied into another storage account in the same region with Entra authorisation alone, no SAS and no account key, while the source account's public network access is disabled? | An Azure source uses the transfer location, as designed. | **An Azure source has only the direct option:** the client opens their own account for each run, as section 9.3 describes, or runs their own copier. The transfer location is then for S3 and GCS sources, and the wizard says so for Azure. Stages 2 and 3 are unaffected; stage 4 ships direct-only. |
| 2 | **S3:** does a multipart copy, made with a full-object CRC64-NVME, end with the same value as the original object's, for an original uploaded in one part, in many parts, and with a composite checksum? | The staged copy's value is a free check before the tool runs. | Nothing is lost in safety: the final copy is always verified against the original's value (section 5), never the staged one's. The extra check is dropped, and a bad staging copy is caught one step later, after the tool has copied it. |

Both are in `[R 7]`, where neither could be confirmed from the vendors' pages.

**Why this order, from the code:**

- **The seams come first because they are cheap now and expensive later.** The port already exists, and three modules know the host. Every stage after this adds a backend, a driver or a host, and each would otherwise copy an Azure assumption.
- **The batch mode is its own stage, second, because v1 gets it.** Most of it exists: the staging bucket, the copy with its checksum, a Storage Mover job over the staging subpath, verification by name against the original, and the clean-up. What is new is scale and choice:
  - a batch planner that fills batches by bytes and keeps one staged ahead;
  - copies in parallel, across objects and across the parts of a large one, where today's go ten objects a step with parts in sequence;
  - the temporary storage counted and shown;
  - the wizard's choice, its two explanations and the estimate;
  - the direct option's checks before and after.

  It is medium, not small, because of the parallel copies and their failure cases: a part that fails, a copy cut off by a restart, an object overwritten mid-copy.
- **S3 as a home comes before Azure → S3.** While a dataset migrates, every new write goes to the destination (decision 14), and the engine only accepts the dataset's own backend as a target. So the edge must be able to write to S3 on the first day of a migration to S3, and after the cutover the share lives there.
- **AWS before Google Cloud** (decision 75), which is also what the code says: the S3 backend, its fake, its restores, events, credentials and check lists exist, and GCS starts from nothing.
- **AWS as a host right after S3 as a home.** Together they are a whole product for a client who has only AWS. The host work depends on stage 1, not on GCS.
- **Each host follows its own home,** so a host is never tested only against another cloud's storage.

Stages 3 and 5 could run side by side after stage 1, and stage 6 beside either, with more people.

**If a preview never reaches GA:** only GCS → Azure rests on one. It ships as an external copier, and Storage Mover's GCS source becomes an option the day it is GA and keyless, with no engine change, because it is a driver.

## 16. Risks

| Risk | Why it matters | What holds it |
|---|---|---|
| A tool behaves differently from its documentation | Nothing here was run. September's pass found the same for Storage Mover. | Stage 0, and the real-cloud harness for each direction before a client |
| DataSync's source addresses stay unpublished | An Azure transfer account allows all networks while a task runs | It holds one batch, for one run, readable by one identity (decision 77). The driver narrows it if AWS publishes ranges. |
| The server-side copy into a transfer account doesn't work from a private-only account | Only the direct option would be left for Azure sources | Stage 0 checks it first |
| Staging large objects is slow | A multipart copy is thousands of calls for one seismic file, and it sets the pace of a migration | Parts and objects in parallel (stage 2); the plan shows the pace; direct is there for a client who prefers speed |
| The default doubles the source's write requests and holds a batch twice | A cost the client didn't expect | The wizard says so, with figures, before it is chosen (decisions 76, 79) |
| A transfer account is left open | The exposure the design exists to bound | It opens per run, closes at a deadline, is re-read hourly and alerted on (section 9.2) |
| A source opened for the direct option is left open | The client's own storage stays reachable | OmniFile can't close it. It reads the setting daily, and the page and an alert say so until it is closed (section 9.3). |
| Rehydration paces a move out of Azure Archive | A large archive leaves at Azure's rehydration rate, not the copier's. Priority is capped at 10 GiB an hour per account, and this repo has no figure for standard priority. | Say it in the plan, before the migration starts. Measure it in stage 0. |
| Tiering under retention is not in place on S3 or GCS | WORM shares there couldn't tier | Stage 0; the editor's rule if it fails |
| The edge's Google key can't live in the TPM | It would be sealed but not hardware-bound, or the edge would use signed URLs only | Signed URLs from the service, as presigned URLs are the fallback on S3 |
| Chunk reads from GCS have no checksum per range | Large GCS-resident files would be whole-file fills | The S3 chunk path's design, repeated for GCS |
| Autoclass moves an object on any data read | A check that read data would undo tiering | Verify by metadata only (section 5) |
| Three hosts, three homes, six directions | More combinations than can be run on real accounts | The ports and their contract suites; the diagonal on real accounts (section 13.3) |
| Three template languages | Three places for a setting to be wrong | One written contract, checked by each template's lint |
| The source side's bill surprises a client | Egress on a petabyte is large | The plan shows it before the start (section 14) |

## 17. Open questions for the owner

Decisions 68 to 81 settled the rest. Four remain. All are about Google Cloud, and wait for that stage. Each has a recommendation.

1. **GCS → Azure: may Storage Mover hold a Google HMAC key in the client's Key Vault?** OmniFile would never see it, but it is a stored secret, which decision 43 rules out for AWS, and the tool is in preview.
   Recommendation: no. Ship that direction with an external copier until Microsoft offers a keyless, GA source.

2. **Smart on GCS needs two buckets and a copy on the archive date.** The alternative is Autoclass alone, which can't honour a date.
   Recommendation: two buckets. "An archive date that is always respected" is the product's rule.

3. **A file in GCS Archive opens at once and bills a retrieval fee.** Should a share on GCS be able to demand a recall step anyway, to keep that fee under someone's control?
   Recommendation: no. Show the fee in the policy editor. Add a guard only if a client asks.

4. **S3 → GCS: offer Google's managed network?** It is a flag on the job, not a path OmniFile builds. It replaces AWS's egress charge with a lower Google rate, in some AWS regions.
   Recommendation: yes, as a cost choice, named for what it is.
