# Verification pass: Azure, AWS and Google Cloud in every direction

Checked on 2026-11-11 against each vendor's own documentation, for `docs/design/multicloud.md`. It follows the form of `2026-09-26-verification.md` and does not repeat what that pass settled; `[V]` below points there.

How to read the marks:

- **VERIFIED (page):** I opened the vendor's page today and read the claim on it.
- **VERIFIED (excerpt):** a search result quoted the claim from the vendor's page. I did not open the page. Read these with more care, and open the page before building on one.
- **UNVERIFIED:** no vendor source found today, or the sources don't say.
- **CONTRADICTED:** the vendor's documentation says otherwise. The correction follows.

Nothing here was tested against a real account. Every tool behaviour below is what the documentation says, not what a run showed.

Sections 5 to 7 were added the same day, after the owner's decisions 68 to 76: what the public-endpoint path needs (decision 71), who is billed for what, and server-side copies into a transfer location (decision 76).

## 1. The plan given to the owner, claim by claim

| # | Claim | Mark | What the documentation says |
|---|---|---|---|
| 1 | Each cloud's native tool pulls into its own cloud | **CONTRADICTED** in part | True of Storage Mover and Storage Transfer Service. **DataSync goes both ways:** an Azure Blob location "can be used as a transfer source or destination", as long as the other end is an AWS storage service. [8] So AWS's tool can also push S3 → Azure Blob. It cannot do GCS ↔ Azure, since neither end is AWS. |
| 2 | Into Azure: Storage Mover; S3 is GA | VERIFIED | `[V]`, and the supported-sources table still lists "AWS S3 → Azure blob container" with no preview label. [1] |
| 3 | Private transfers from S3 are in preview | VERIFIED (excerpt) | The portal labels it "Private connections (Preview)". It rests on Private Link Service Direct Connect, which is itself Public Preview in a limited set of regions. [4][5][6] The date "March 2026" is UNVERIFIED. |
| 4 | GCS as a Storage Mover source is in preview | VERIFIED (page) | "GCS S3 (Preview) → Azure blob container". [1] |
| 5 | No Microsoft Learn page confirms it | **CONTRADICTED** | There is one: "Get started with Google-to-Azure migrations in Azure Storage Mover". [3] It reads GCS through GCS's **S3-compatible API with HMAC keys**, held in Azure Key Vault. See section 2.2. |
| 6 | Into AWS: DataSync Enhanced mode, Azure Blob → S3 with no agent | VERIFIED (page) | "Transfers between Azure Blob and Amazon S3 using Enhanced mode do not require an agent." [8] |
| 7 | It needs a SAS token | **CONTRADICTED** | A SAS is one option. Since November 2026, Enhanced mode also takes **federated identity (OIDC)** through Microsoft Entra workload identity federation, with no stored credential. [8][11] See section 2.3. |
| 8 | The agentless path uses Azure's public endpoints | VERIFIED (excerpt) | "Since transfers from other cloud storage typically happen over the public internet…" [9] No page says the agentless path can reach a private endpoint. |
| 9 | Enhanced mode has supported agents for Azure Blob since July 2026 | VERIFIED (excerpt) | 2026-07-28: Enhanced mode "supports agent-based data transfers with HDFS, Microsoft Azure Blob Storage, and self-managed object storage", and Enhanced agents run on Hyper-V. [12][18] |
| 10 | GCS as a DataSync source | VERIFIED (excerpt) | Yes, as an object storage location, agentless in Enhanced mode since 2025-05-29. HMAC keys, or federated identity in Enhanced mode. [9][10][11] |
| 11 | Into GCP: Storage Transfer Service, from S3 and Azure Blob, agentless | VERIFIED (excerpt) | [19][20] |
| 12 | Customer-managed and Google-managed private-network options since December 2025 | **CONTRADICTED** in part | The **Google-managed** private network is for **S3 only**, and dates from 2024-05-17. The **customer-managed** one, for S3 **and Azure**, dates from 2025-12-02. [21][22][23] So Azure → GCS has one private option, and it needs an interconnect. |
| 13 | GCS Archive needs no restore step and has a 365-day minimum | VERIFIED (excerpt) | 365 days; all classes, Archive included, are read with millisecond latency; retrieval fees apply. [31] |
| 14 | GCS has no CRC64-NVME; it has CRC32C | VERIFIED (excerpt) | The documentation lists CRC32C and MD5 only. That CRC64 is absent is what the pages don't list, not a sentence on them. [34][35] |

## 2. The six directions

### 2.1 Summary

| Direction | Native tool | Status | Agent | Private path | Tool's credential to the source | Can a job take an object list? |
|---|---|---|---|---|---|---|
| S3 → Azure | Azure Storage Mover | GA | None | Preview (PLS Direct Connect to an S3 interface endpoint) | Arc multicloud connector, a role from a CloudFormation stack: no key | No: one subpath `[V]` |
| GCS → Azure | Azure Storage Mover | **Preview** | None | Preview ("private buckets require private connections") | **HMAC key in Key Vault** | No |
| Azure → S3 | AWS DataSync, Enhanced mode | GA | None, or an Enhanced agent | Agentless: public. With an agent in the VNet: private | SAS (account or container), or **Entra federation, no key** | Yes: manifest |
| GCS → S3 | AWS DataSync, Enhanced mode | GA | None, or an agent | Agentless: public. "For private cloud connectivity … you need a DataSync agent" | HMAC key, or **Google federation, no key** | Yes: manifest |
| S3 → GCS | Google Storage Transfer Service | GA | None | Google-managed (select AWS regions), or customer-managed over an interconnect | Access key, or **role ARN by web identity, no key** | Yes: manifest |
| Azure → GCS | Google Storage Transfer Service | GA | None | Customer-managed over an interconnect only | SAS, shared key, or **Entra federation, no key** | Yes: manifest |

"GA" for DataSync and Storage Transfer Service means no page marks the path as preview. I did not find a sentence saying "generally available" for each, so read it as VERIFIED (excerpt) at best.

### 2.2 Azure Storage Mover (into Azure)

From S3, unchanged since `[V]`: agentless, the Arc multicloud connector, 500 million objects per job, 10 concurrent jobs per subscription, Glacier and Deep Archive objects "must be restored before migration", one `sourceSubpath` and `targetSubpath`, Additive or Mirror, no destination tier, no object list. [1][2]

New or changed:

- **GCS source (VERIFIED, excerpt).** [3]
  - Source type "GCS Object Storage - S3". The source URL is `https://storage.googleapis.com/<bucket>/`, with an optional prefix.
  - **Credential: an HMAC access key and secret,** made for a Google service account, stored as two Key Vault secrets. The endpoint's managed identity reads them (Key Vault Secrets User). Microsoft suggests a key lifetime of at least a week.
  - No multicloud connector and no discovery of buckets.
  - The source must speak HTTPS and AWS Signature Version 4. 500 million objects per job.
  - "You must restore objects in archival storage classes before migration." [1] GCS has no restore step (section 3.1), so what this means for GCS Archive objects is UNVERIFIED.
  - The REST API has an S3-with-HMAC endpoint type that lists GCS as a source. [7]
- **Private connections (VERIFIED, excerpt).** [4][5][6]
  - Preview. Built on a Private Link Service Direct Connect, created outside Storage Mover and then approved. It must be in the Storage Mover's region.
  - At most 10 private connections per subscription per region.
  - For S3 it points at the S3 interface endpoint's private IP; the bucket policy must allow `aws:SourceVpce`.
  - The cloud-to-cloud page has left-over text about Google Private Service Connect, so a private GCS path is intended. What it needs is UNVERIFIED.
- **Target account behind a firewall: documented now, untested.** `[V]` found Storage Mover absent from Azure Storage's trusted-services list. It is on it today. See section 5.1.
- **What it verifies itself: UNVERIFIED** for cloud-to-cloud jobs, as in `[V]`. The guide's own "post-migration validation" is a list of manual steps. [2]
- **Versions:** nothing says noncurrent S3 or GCS versions are copied. UNVERIFIED; assume current versions only.
- **Out of Azure:** every target is Azure `[V]`. Unchanged. [1]

### 2.3 AWS DataSync (into AWS, and out of S3)

- **Azure Blob location (VERIFIED, page).** [8]
  - **SAS:** account-level or container-level only. "You can't provide DataSync a user delegation SAS token." A SAS that expires mid-transfer stops the task.
  - **Federated identity (OIDC):** DataSync assumes an IAM role, gets a signed JWT from AWS STS (`GetWebIdentityToken`), and exchanges it at the Entra token endpoint for an access token. The Azure side is a user-assigned managed identity or app registration with a federated credential (issuer: the AWS account's outbound issuer URL; subject: the role's ARN; audience `api://AzureADTokenExchange`) and a role on the container, such as Storage Blob Data Contributor. **Enhanced mode only.** A location takes federation or a SAS, not both.
  - **Tiers read:** hot and cool. Archive blobs must be rehydrated first. **"DataSync doesn't support the cold access tier."**
  - **Tiers written:** hot, cool or archive. Copies written to archive can't be verified by the "verify all data" option.
  - **Limits to S3:** no blob over 5 TB, none with more than 2 KB of metadata, none with more than 2 KB of tags. Tags are not copied from hierarchical-namespace accounts.
  - **Blob types read:** block, page and append. Written: block only.
  - **Folder scope:** a `Subdirectory` on the location limits a task to a virtual directory.
- **Google Cloud Storage location (VERIFIED, excerpt).** [9][10][11]
  - Through GCS's XML API, as an object storage location. Basic mode needs HMAC keys and an agent.
  - Enhanced mode: agentless, with HMAC keys or **federated identity** through a Google Workload Identity Pool OIDC provider (announced with the Azure one, November 2026).
  - GCS Archive objects are read directly, with Google's retrieval fees.
  - Metadata (ContentType, ContentEncoding and the like) is copied "on a best-effort basis". GCS tags are not read through this interface.
- **Enhanced mode (VERIFIED, excerpt).** [13]
  - No object-count limit. Lists, prepares, transfers and verifies in parallel.
  - **Verifies only what it transferred** (`ONLY_FILES_TRANSFERRED`). The point-in-time "verify everything at the destination" mode is Basic only.
  - A task's mode can't be changed after it is created.
- **Manifests (VERIFIED, excerpt).** A task execution can take a manifest, a list of objects, in Enhanced mode; DataSync then lists and verifies only what it names. [14][18]
- **Agents (VERIFIED, page and excerpt).** An Enhanced agent can run as a Hyper-V image on an Azure VM, or on EC2. It talks to DataSync through public, FIPS or **VPC** service endpoints. [8][12] Whether federated identity works for a task that uses an agent is UNVERIFIED: the page says "Enhanced mode only" and no more.
- **API (VERIFIED, excerpt).** `CreateLocationAzureBlob`, `CreateLocationObjectStorage`, `CreateTask`, `StartTaskExecution`, `DescribeTaskExecution`. [14]
  - Status: `QUEUED`, `CANCELLING`, `LAUNCHING`, `PREPARING`, `TRANSFERRING`, `VERIFYING`, `SUCCESS`, `ERROR`.
  - Counters: `FilesListed`, `FilesPrepared`, `FilesTransferred`, `FilesSkipped`, and `FilesFailed` split into prepare, transfer, verify and delete.
  - A task report can be written to an S3 bucket, listing transferred, verified, skipped and deleted objects (`TaskReportConfig`).
- **Versions:** UNVERIFIED. Nothing found says noncurrent blob versions or GCS generations are copied.
- **Deletes at the destination** are an option (`PreserveDeletedFiles`). Its default is UNVERIFIED; a driver must set it explicitly.
- **The checksum S3 holds for an object DataSync wrote:** UNVERIFIED. S3 adds a full-object CRC64NVME to uploads that carry none `[V]`, but what DataSync's uploads carry is not documented.

### 2.4 Google Storage Transfer Service (into Google Cloud)

- **S3 source credential (VERIFIED, excerpt).** An access key, or a **role ARN** that Google's service identity assumes by `AssumeRoleWithWebIdentity`. [19][26]
- **Azure source credential (VERIFIED, excerpt).** A SAS token, a shared key, or **federated identity**: an Entra application with a federated credential, named in the job by client id and tenant id (`federatedIdentityConfig`). [20][26]
- **Private paths (VERIFIED, excerpt).** [21][22][23]
  - **Google-managed private network:** S3 only, a boolean on the job. Select AWS regions, not GovCloud. "This network is shared between other users of Storage Transfer Service." No AWS egress fee; Google bills a flat rate per GiB instead.
  - **Customer-managed private network:** S3 or Azure Blob, over Cross-Cloud Interconnect or Partner Interconnect. For S3 it needs an S3 VPC endpoint, a regional internal proxy Network Load Balancer, and a Service Directory service the job names. The Azure side's steps were not read.
- **Archived source objects (VERIFIED, page).** [24]
  - **Glacier Flexible Retrieval objects "are skipped regardless of restore status."** The API reference says a project can be allow-listed to copy them (excerpt). [27]
  - Glacier Deep Archive objects "are transferred only if they are restored."
  - Glacier Instant Retrieval objects are transferred.
  - A job can filter source objects by storage class (2026-09-21), in the console and REST API only.
  - Counters name both cases: `unrestoredDeepArchiveObjectsSkippedCount` and `unsupportedS3GlacierObjectsSkippedCount`. [27]
  - **Azure archive and cold blobs:** UNVERIFIED. No page read today mentions them.
- **Limits (VERIFIED, page).** [25]
  - 5 TiB per object. Names up to 1,024 bytes, the destination prefix included.
  - "Storage Transfer Service does not lock source files during a transfer." An object that changes mid-transfer fails and isn't copied.
  - A job can **delete source objects after transfer**. OmniFile must never set it.
- **Manifests (VERIFIED, page).** A CSV of object names, with an optional generation, in any Cloud Storage bucket, no size limit. Supported "from AWS S3, S3-compatible storage, Azure Blob, or Cloud Storage to Cloud Storage". [28]
- **What it verifies (VERIFIED, excerpt).** [29]
  - It uses the source's checksum where one exists, and fails the object on a mismatch. Otherwise it "may compute a CRC32C on the fly" and compare it with what Cloud Storage reports.
  - For S3-compatible sources it expects the ETag to be an MD5; multipart objects have none, and are checked "by file size instead". Whether that weaker check also applies to Amazon S3 itself is UNVERIFIED.
- **Metadata (VERIFIED, excerpt).** Custom metadata is kept from S3 and Azure Blob. The destination storage class can be set per job. [30]
- **API (VERIFIED, excerpt).** `transferJobs.create`, `transferJobs.run`, and `transferOperations` with counters. Pub/Sub notifications on completion exist in the API; not read today.
- **Versions:** UNVERIFIED for S3 and Azure sources. Assume current versions only.
- **Out of Google Cloud to S3 or Azure:** Storage Transfer Service writes object data only to Cloud Storage (and to file systems through agents). There is no native Google tool that pushes to S3 or Azure Blob. UNVERIFIED as a negative, but nothing found says otherwise.

## 3. Google Cloud Storage as a backend

### 3.1 Classes, Autoclass, retrieval

- **Classes (VERIFIED, excerpt).** Standard (no minimum), Nearline (30 days), Coldline (90 days), Archive (365 days). Retrieval fees apply to the last three. Every class is read with millisecond latency. **There is no restore step, in any class.** [31]
- **Autoclass (VERIFIED, excerpt),** the nearest thing to our "smart" tier. [32]
  - It is a bucket setting. Objects move to Nearline after 30 days without access, Coldline after 90, Archive after 365.
  - The terminal class is Nearline by default, and can be set to Archive.
  - Objects under 128 KiB stay in Standard for good.
  - **A read of an object's data moves it back to Standard.** Reading or editing its metadata does not.
  - **An explicit class is ignored:** a rewrite or copy that names a class succeeds, and the object is set to Standard.
  - `SetStorageClass` lifecycle rules, and the `matchesStorageClass` condition, are not allowed on an Autoclass bucket.
  - Operations are billed at the Standard rate. A management fee applies per 1,000 objects.
  - **"No retrieval fees and no early-deletion fees under Autoclass" is UNVERIFIED.** The pricing page's extract was ambiguous. [33]
- **So Autoclass cannot hold a fixed archive date.** It moves on access only, and nothing can place an object in Archive on a given day inside an Autoclass bucket.
- **Changing a class outside Autoclass:** by a rewrite, or by a lifecycle `SetStorageClass` rule, which "changes the storage class of an object and updates the object's modification time". [45] Whether either makes a new generation, and whether either is allowed on an object under retention, is UNVERIFIED.

### 3.2 Checksums

- **CRC32C is the one checksum every object has (VERIFIED, excerpt).** "The CRC32C is computed by the Cloud Storage service for all written objects." [34]
- **MD5 is missing from composite objects** and can't validate XML multipart uploads: "composite objects do not have an MD5 hash metadata field." [34][35]
- **No CRC64-NVME.** Decision 24's bridge (S3's CRC64NVME equals Azure's CRC64) does not reach GCS.
- **Amazon S3 can produce a full-object CRC32C for an object at rest (VERIFIED, excerpt).** Batch Operations Compute checksum lists CRC32C, and "all supported checksum algorithms support both the full object and composite checksum types, except CRC-64/NVME", which is full-object only. [15][16] `[V]` established that the job works on every class without a restore.
- **Azure stores no whole-blob checksum** `[V]`. Unchanged, and it matters more now: see `multicloud.md` section 5.

### 3.3 Versioning, WORM, holds

- **Versioning (VERIFIED, excerpt).** A replaced or deleted live object becomes a noncurrent version, addressed by generation number. [38]
- **Object Retention Lock (VERIFIED, excerpt),** the match for version-level WORM. [36]
  - A retain-until time per object: "prior to this time, the object cannot be deleted or replaced." An object under retention "can still be made noncurrent", so an overwrite on a versioned bucket is allowed and the old version is kept.
  - Modes: **Unlocked** (Governance in the XML API), which authorised users can change, and **Locked**, which "cannot be changed, and the retention period can only be increased".
  - It must be enabled on the bucket. **An existing bucket can enable it only in the Google Cloud console,** and it can never be disabled. Enabling it places a best-effort lien on deleting the project.
- **Bucket Lock (VERIFIED, excerpt).** A retention period for a whole bucket; once locked it can't be removed or shortened. An object under both is kept until both are satisfied. [37]
- **Holds (VERIFIED, excerpt).** Temporary holds and event-based holds. A temporary hold can sit beside a retention configuration; an event-based hold can't be set in the same request as one. [36] A temporary hold is the match for a legal hold.
- **S3, for the same table (VERIFIED, excerpt).** Object Lock needs versioning, protects only the version named, has a Compliance mode nobody can shorten, and a legal hold with no end date. It can be enabled on an existing bucket. [17]
- **Soft delete:** GCS keeps deleted objects for a retention window by default. The default length is UNVERIFIED today. It matters for cost and for what "deleted" means.

### 3.4 Changes, inventory, batch work

- **Pub/Sub notifications (VERIFIED, excerpt).** `OBJECT_FINALIZE`, `OBJECT_METADATA_UPDATE`, `OBJECT_DELETE`, `OBJECT_ARCHIVE` (a live version became noncurrent). An overwrite sends two events, linked by `overwroteGeneration` and `overwrittenByGeneration`. The payload is the object's resource. [39] Delivery guarantees were not read; treat it as at-least-once and unordered until checked.
- **There is no ordered change log** like Azure's change feed. Notifications plus inventory is the pair, as EventBridge plus S3 Inventory is on AWS.
- **Inventory:** Storage Insights inventory reports exist. [44] Their fields and formats were not read today: UNVERIFIED.
- **Storage batch operations (VERIFIED, excerpt).** Holds, deletes, metadata and context updates, rewrites, ACLs, over a prefix list or a CSV manifest, one bucket per job. **"Available only if you've configured Storage Intelligence,"** a paid subscription. [43] Whether a batch rewrite can change a storage class is UNVERIFIED.
- **Nothing to compute a checksum in bulk is needed:** the CRC32C is on every object.

### 3.5 Metadata and tags

- **Custom metadata (VERIFIED, excerpt).** 8 KiB per object for all keys and values together. [41] Enough for `file_owner`, `file_group` and `file_permissions`.
- **Object contexts (VERIFIED, excerpt).** Up to 50 key-value pairs per object, 256 bytes per key and per value, billed per million. [42] The nearest thing to index tags. Whether a listing can filter by them, and their release status, are UNVERIFIED.
- **Metadata on an Archive object:** editing metadata does not move an Autoclass object. [32] Whether it is free of early-deletion charges outside Autoclass is UNVERIFIED.

### 3.6 Private access

- **Private Service Connect endpoint for Google APIs (VERIFIED, excerpt).** An internal address in the customer's VPC that reaches `*.googleapis.com` (the `all-apis` bundle), or only the services that support VPC Service Controls (`vpc-sc`). On-premises hosts reach it over Cloud Interconnect VLAN attachments or Cloud VPN. [40]
- **Private Google Access for on-premises hosts (VERIFIED, excerpt).** The `restricted.googleapis.com` range, reached through a VPC over VPN or Interconnect, with DNS pointing `*.googleapis.com` at it. [49]
- Cloud Storage and Pub/Sub are both reachable this way. TLS versions on these endpoints were not read.

### 3.7 Credentials with no keys

- **The service, from an Azure managed identity (VERIFIED, excerpt).** Workload Identity Federation takes Entra tokens: an OIDC provider with issuer `https://sts.windows.net/<tenant>`, the managed identity's object id as subject, exchanged at Google's Security Token Service for a short-lived token. [46] The page is written for Azure VMs. That it works from a Container Apps managed identity is UNVERIFIED but expected: the token is the same.
- **The edge, by certificate (VERIFIED, excerpt).** Workload Identity Federation with X.509 certificates has been generally available since 2025-05-07. The pool trusts registered CAs as trust anchors (at most 3, intermediates allowed); the workload presents its certificate over mTLS. A leaf may live 390 days at most. [47][48] This is the Roles Anywhere equivalent.
  - **TPM-held keys:** UNVERIFIED. The client libraries read a certificate and key by path. A PKCS#11 or TPM route was not found today.
  - **Certificate-based access** (binding a token to the certificate in Access Context Manager) is a separate, allow-listed feature. [47]
- **A fallback like presigned URLs:** V4 signed URLs exist, and can be signed through IAM without a key file. Not read today: UNVERIFIED.

## 4. What I could not confirm, in one place

1. Storage Mover writing to a storage account with public network access disabled: documented as possible (section 5.1), never run.
2. What Storage Mover verifies on a cloud-to-cloud copy.
3. How Storage Mover's private connection reaches GCS, and when either preview ends.
4. Whether DataSync's federated identity works with an Enhanced agent, which a private Azure → S3 path needs.
5. What checksum S3 holds for an object DataSync wrote.
6. Whether Storage Transfer Service reads Azure Cold and Archive blobs.
7. Whether Storage Transfer Service checks S3 multipart objects by size only.
8. Whether any of the three tools copies noncurrent versions.
9. GCS: whether a class change makes a new generation, and whether it is allowed under retention. The WORM design on GCS depends on it.
10. GCS: fees under Autoclass, the soft-delete default, inventory report fields, filtering by object contexts.
11. The edge's Google credential with its key in a TPM.
12. The dates of the Storage Mover previews.

## 5. The public-endpoint path (decision 71)

Cloud-to-cloud copies run over the providers' public endpoints. This section is what each tool then needs from the storage at each end, and what a client whose storage is private-only has to open.

### 5.1 Azure Blob

- **As a destination, for Storage Mover: no opening is needed (VERIFIED, page; untested).** "Azure Storage Mover, `Microsoft.StorageMover/storageMovers`, Enables access to storage accounts" is in the list of trusted access based on a managed identity (page updated 2026-10-15). [50] **This CONTRADICTS `[V]`,** which found it absent in September: it has been added since.
- **The exception survives "public network access: Disabled" (VERIFIED, excerpt).** Existing resource instance rules and exceptions, the trusted-services one included, "remain in effect" after public access is disabled. [51] So a private-only target account can take Storage Mover's writes through a resource instance rule for that Storage Mover, with no public opening.
- **Not under an enforced network security perimeter (VERIFIED, excerpt).** In enforced mode the account's IP rules and its exceptions for Azure services don't apply. [51]
- **As a source, the account must answer the tool's public requests.** That means public network access "Enabled from selected networks" with IP rules for the tool, or "Enabled from all networks".
  - **Storage Transfer Service publishes its ranges (VERIFIED, excerpt).** A JSON file at `https://www.gstatic.com/storage-transfer-service/ipranges.json`; each `ipv4Prefix` goes into the storage firewall. A new range is listed at least seven days before it is used, and Google says to check the file at least weekly, and to keep the trusted-services exception on. **IP restriction is not supported when the job authenticates with a SAS,** so it needs federation. [20]
  - **DataSync publishes none that I found (UNVERIFIED).** The network-requirements page lists the endpoints DataSync needs, not the addresses its agentless transfers come from. [52] Without them an Azure firewall can't name DataSync, and the account must allow all networks while a task runs.
  - **How many IP rules an account takes:** UNVERIFIED today.
- **IP rules can't name a same-region Azure service (VERIFIED, excerpt).** "Services deployed in the same region as the storage account use private Azure IP addresses." [51] Not a problem here: the readers are AWS and Google.

### 5.2 Amazon S3

- S3 has no switch for "private-only". A bucket is closed to the internet by a bucket policy that denies requests not arriving through a named VPC endpoint (`aws:SourceVpce`) or from named addresses (`aws:SourceIp`). Not re-read today: UNVERIFIED as wording, standard as behaviour.
- **So opening is a policy change, and it can name a principal, not a network:** the deny gets an exception for the one role the tool uses. That needs no address list. Not re-read today.
- **Storage Mover's addresses toward S3 are not published (UNVERIFIED).** A Microsoft Q&A answer says the service reaches buckets over the public internet and gives no ranges. The exception must be by role.
- **Storage Transfer Service's ranges are the same published file** as for Azure: "if an AWS project limits storage access by IP, the worker ranges must be added". [19]
- **As a destination for DataSync:** DataSync runs inside AWS under a role the client gives it. A bucket policy that denies all but a VPC endpoint blocks it too, until that role is excepted. Not read today: UNVERIFIED.

### 5.3 Google Cloud Storage

- **Bucket IP filtering (VERIFIED, excerpt).** A bucket can allow requests only from listed public ranges (`publicNetworkSource`) and VPC networks (`vpcNetworkSources`). `allowAllServiceAgentAccess` lets Google's own services through without the address check. Google warns that a wrong rule can lock the owner out. [53] Its release status, and whether Storage Transfer Service's service agent is covered by that switch, are UNVERIFIED.
- **As a source for DataSync or Storage Mover,** a filtered bucket must allow their addresses, and neither publishes any (above). The filter has to come off, or allow all public ranges, while a copy runs.
- **VPC Service Controls** perimeters also restrict access to a bucket. How each tool passes one was not read: UNVERIFIED.

### 5.4 Encryption in transit

- Storage Mover's S3-compatible sources must use HTTPS. [4][5]
- DataSync's Enhanced mode "connects directly to your source storage over HTTPS". [9]
- Storage Transfer Service's TLS version was not read. The minimum TLS version each storage service enforces is the client's setting (Azure: the account's minimum TLS version; S3: a bucket policy on `aws:SecureTransport`), not the tool's.

### 5.5 One fact for hosting the service on Google Cloud

- **Google's Application Load Balancers end TLS themselves (VERIFIED, excerpt).** They support mutual TLS, and pass the client certificate's details to the backend in request headers. [60] OmniFile's `api` ends mutual TLS itself and refuses a forwarded certificate header (threat model T5), so it needs a pass-through path.
- Google's proxy Network Load Balancers "do not support client certificate-based authentication". [60]
- Whether Cloud Run can receive a raw TCP connection, or a client certificate, was not found: UNVERIFIED.
- Nothing else in `multicloud.md` section 13's AWS and Google columns was checked against a vendor page.

## 6. Who is billed for what

Checked against the vendors' pricing pages where marked. **Figures are list prices for one region and date, and are here to show the shape, not to quote.** The repo's own rate table (decision 62) is where prices live.

### 6.1 The tools

| Tool | What the tool itself costs | Mark |
|---|---|---|
| Azure Storage Mover | Nothing. "The service is free; AWS egress still applies." | `[V]`, the Azure blog; not re-read today |
| AWS DataSync, Enhanced mode | A fee per GB copied, and a fee per task execution, both "charged at the source Region" of the task. AWS's example uses $0.015 per GB and $0.55 per execution. | VERIFIED (excerpt) [54] |
| Google Storage Transfer Service, agentless | Nothing: "transfers from other cloud providers are free of charge". The Google-managed private network costs $0.03 to $0.08 per GiB by region, in place of S3 egress. | VERIFIED (excerpt) [55] |

None of the three needs an agent or a VM for a cloud-to-cloud copy over public endpoints, so there is no compute to pay for.

### 6.2 The clouds

| Cloud | Data coming in | Data going out to another cloud | Mark |
|---|---|---|---|
| Azure | Free | Billed to the storage account's subscription. The first 100 GB a month is free; then about $0.087 per GB for the next 10 TB from North America and Europe. | VERIFIED (excerpt) [56] |
| AWS | Free (not re-read) | Billed to the bucket's account, at the internet data-transfer-out rate. The one figure found, $0.09 per GB, is from an old page. | The rate is UNVERIFIED today |
| Google Cloud | Free | Billed to the bucket's project: from $0.12 per GiB for the first 10 TiB a month, falling with volume. | VERIFIED (excerpt) [57] |

The source side also pays, in every direction:

- **Requests.** Every object copied is at least one read and one list entry at the source, billed per thousand, and one or more writes at the destination. "AWS still bills request charges such as LIST and GET" even on Google's managed network. [55]
- **Retrieval from cold classes.** S3 charges per GB and per request to restore from Glacier classes, and bills the restored temporary copy on top of the archived one `[V]`. Azure charges to rehydrate from Archive and to read from Cool and Cold. GCS charges a retrieval fee per GiB read from Nearline, Coldline and Archive, on the bytes actually read. [57] Amounts were not read today.
- **Early deletion** applies only if something is deleted or re-tiered before its minimum. A migration deletes nothing at a source (decision 40), so it adds none there. A staged copy is in a hot class and adds none.
- **Staging.** A staged copy is stored twice for a while, and is billed as storage and requests in the source cloud.
- **Bulk jobs.** S3 Batch Operations (restore waves, Compute checksum) bills per job and per object. Amounts not read today.

### 6.3 Exit waivers

All three are for a customer leaving a provider altogether. None covers moving one dataset while the account stays.

| Provider | Offer | Conditions | Mark |
|---|---|---|---|
| AWS | "Free data transfer out to internet when moving out of AWS", as a credit | Ask AWS Support before starting. It is for moving **all** data off AWS, or all of one service's. Sources differ on the window: 60 days in the S3 FAQ, 180 days in a later update to the blog for customers outside the EU. Remaining data must then be deleted or the account closed. | VERIFIED (excerpt) [58] |
| Azure | "Free data transfer out to internet when leaving Azure", as a credit on the invoice | A support request with dates and volume. The transfer must finish within 60 days. **All Azure subscriptions on the account must be cancelled** before the credit is given. | VERIFIED (excerpt) [59] |
| Google Cloud | An exit offer exists from memory | Not found today | UNVERIFIED |

So the owner's understanding holds for the tool and the destination, and not for the source: **with Storage Mover pulling S3 into Azure, Azure bills nothing for the tool, for an agent or for the data arriving, and AWS bills the bucket's account for the data leaving and for the requests and restores.** A waiver is the client's to arrange with the provider, and outside this project (`AGENTS.md`, decision 16).

## 7. Server-side copies into a transfer location (decision 76)

A batch is copied inside the source cloud before the destination's tool reads it. This is what each provider's own copy can carry.

### 7.1 Amazon S3

- **`CopyObject` copies up to 5 GB in one call (VERIFIED, excerpt).** "To copy an object greater than 5 GB, you must use the multipart upload Upload Part - Copy (UploadPartCopy) API." [61]
- **A multipart copy:** parts of 5 MiB to 5 GiB, at most 10,000 parts. The largest object is 48.8 TiB; other AWS pages say 50 TB. [62] (DataSync and Storage Transfer Service still stop at 5 TB and 5 TiB, section 2.)
- **S3 Batch Operations Copy takes objects only up to 5 GB (VERIFIED, excerpt).** "Objects to be copied can be up to 5 GB in size." AWS's own way round it is a Lambda function doing a multipart copy, limited by Lambda's 15 minutes. [63][64] So Batch Copy can't carry seismic files.
- **Does a full-object CRC64NVME survive a copy?**
  - **A composite checksum does not (VERIFIED, excerpt).** "The checksum value for the object might change if you copy it": a copy reports a checksum over the whole object, where a multipart upload's composite one was calculated from its parts. [16]
  - **A full-object CRC64NVME is a function of the bytes,** so an equal copy has an equal value. The `CopyObject` result carries a CRC64NVME when the source had one, or had none and S3 added the default, and a checksum type of `COMPOSITE` or `FULL_OBJECT` to tell them apart. [61] That the value returned equals the source's for every kind of copy is what follows from the definition, not a sentence read: **UNVERIFIED as a statement, and a real-AWS check.**
  - **`UploadPartCopy` returns a part's checksum only when the multipart upload was created with that algorithm.** [65] The repo's copy creates it with `CRC64NVME` and `FULL_OBJECT`, so S3 computes the full-object value on completion.
- **So the rule for the design:** compare full-object values only; take the expected value from the original object, never from the staged one; and treat "the staged copy's value equals the original's" as an extra check, not the proof.

### 7.2 Azure Blob

- **`Put Blob From URL` copies a whole blob of up to 5,000 MiB (VERIFIED, excerpt).** A larger source fails with 409. It can't copy a range. [66]
- **`Put Block From URL` takes blocks of up to 4,000 MiB** in service version 2020-04-08 and later. The page's introduction still says 100 MiB, which is the older limit. Entra authorisation of the copy's source is supported from version 2020-10-02, as a bearer token. [67]
- **An archived blob can be rehydrated by a copy straight into another account (VERIFIED, excerpt).** "Service version 2021-02-12 and later supports rehydration by copying an archived blob to a different storage account in the same region." The destination must be an online tier. The source stays archived, and both blobs are billed. It can take up to 15 hours (under 10 GB, as `AGENTS.md` notes). [68]
- **Microsoft's CLI example for that copy uses a SAS on the source.** Whether the same copy works with Entra authorisation alone, and from a source account whose public network access is disabled, is UNVERIFIED. It decides whether a private-only Azure account can use the transfer location at all, and is a real-Azure check.
- **Azure returns no whole-blob checksum for a copy** `[V]`. The staged blob is not checked by itself; the final copy is, against the catalog's value.

### 7.3 Google Cloud Storage

Not read. A rewrite is the server-side copy, and the CRC32C is on every object (section 3.2).

## Sources

All read or searched on 2026-11-11.

Microsoft:
- [1] https://learn.microsoft.com/en-us/azure/storage-mover/service-overview (page; updated 2026-05-05)
- [2] https://learn.microsoft.com/en-us/azure/storage-mover/cloud-to-cloud-migration (page; updated 2026-06-30)
- [3] https://learn.microsoft.com/en-us/azure/storage-mover/google-migration
- [4] https://learn.microsoft.com/en-us/azure/storage-mover/migrations-requiring-private-connections
- [5] https://learn.microsoft.com/en-us/azure/storage-mover/cloud-to-cloud-private-network-configuration
- [6] https://learn.microsoft.com/en-us/azure/private-link/availability
- [7] https://learn.microsoft.com/en-us/rest/api/storagemover/endpoints/list?view=rest-storagemover-2026-05-01

AWS:
- [8] https://docs.aws.amazon.com/datasync/latest/userguide/creating-azure-blob-location.html (page)
- [9] https://docs.aws.amazon.com/datasync/latest/userguide/third-party-cloud-transfer-considerations.html
- [10] https://docs.aws.amazon.com/datasync/latest/userguide/tutorial_transfer-google-cloud-storage.html
- [11] https://aws.amazon.com/about-aws/whats-new/2026/11/datasync-enhanced-oidc-google-azure/
- [12] https://aws.amazon.com/about-aws/whats-new/2026/07/aws-datasync-hdfs-azure-blob-hyper-v/
- [13] https://docs.aws.amazon.com/datasync/latest/userguide/choosing-task-mode.html
- [14] https://docs.aws.amazon.com/datasync/latest/userguide/API_DescribeTaskExecution.html
- [15] https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-compute-checksums.html
- [16] https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html
- [17] https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html
- [18] https://docs.aws.amazon.com/datasync/latest/userguide/doc-history.html

Google:
- [19] https://docs.cloud.google.com/storage-transfer/docs/source-amazon-s3
- [20] https://docs.cloud.google.com/storage-transfer/docs/source-microsoft-azure
- [21] https://docs.cloud.google.com/storage-transfer/docs/create-transfers/agentless/managed-private-network
- [22] https://docs.cloud.google.com/storage-transfer/docs/create-transfers/agentless/customer-managed-private-network
- [23] https://docs.cloud.google.com/storage-transfer/docs/release-notes
- [24] https://docs.cloud.google.com/storage-transfer/docs/filtering-by-storage-class (page)
- [25] https://docs.cloud.google.com/storage-transfer/docs/known-limitations-transfer (page)
- [26] https://docs.cloud.google.com/storage-transfer/docs/reference/rest/v1/TransferSpec
- [27] https://docs.cloud.google.com/storage-transfer/docs/reference/rpc/google.storagetransfer.v1
- [28] https://docs.cloud.google.com/storage-transfer/docs/manifest (page)
- [29] https://docs.cloud.google.com/storage-transfer/docs/data-integrity
- [30] https://cloud.google.com/storage-transfer/docs/metadata-preservation
- [31] https://docs.cloud.google.com/storage/docs/storage-classes
- [32] https://docs.cloud.google.com/storage/docs/autoclass
- [33] https://cloud.google.com/storage/pricing
- [34] https://docs.cloud.google.com/storage/docs/data-validation
- [35] https://docs.cloud.google.com/storage/docs/composite-objects
- [36] https://docs.cloud.google.com/storage/docs/object-lock
- [37] https://docs.cloud.google.com/storage/docs/bucket-lock
- [38] https://docs.cloud.google.com/storage/docs/object-versioning
- [39] https://docs.cloud.google.com/storage/docs/pubsub-notifications
- [40] https://docs.cloud.google.com/vpc/docs/about-accessing-google-apis-endpoints
- [41] https://docs.cloud.google.com/storage/quotas
- [42] https://cloud.google.com/storage/docs/object-contexts
- [43] https://docs.cloud.google.com/storage/docs/batch-operations/overview
- [44] https://docs.cloud.google.com/storage/docs/insights/inventory-reports
- [45] https://docs.cloud.google.com/storage/docs/lifecycle
- [46] https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds
- [47] https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-x509-certificates
- [48] https://docs.cloud.google.com/iam/docs/release-notes
- [49] https://docs.cloud.google.com/vpc-service-controls/docs/set-up-private-connectivity

Added for sections 5 and 6:
- [50] https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-trusted-azure-services (page; updated 2026-10-15)
- [51] https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-limitations
- [52] https://docs.aws.amazon.com/datasync/latest/userguide/datasync-network.html
- [53] https://docs.cloud.google.com/storage/docs/update-ip-filter
- [54] https://aws.amazon.com/datasync/pricing/
- [55] https://cloud.google.com/storage-transfer/pricing
- [56] https://azure.microsoft.com/en-us/pricing/details/bandwidth/
- [57] https://cloud.google.com/storage/pricing
- [58] https://aws.amazon.com/blogs/aws/free-data-transfer-out-to-internet-when-moving-out-of-aws/
- [59] https://azure.microsoft.com/en-us/updates/now-available-free-data-transfer-out-to-internet-when-leaving-azure/
- [60] https://docs.cloud.google.com/load-balancing/docs/mtls

Added for section 7:
- [61] https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html
- [62] https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html
- [63] https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-copy-object.html
- [64] https://aws.amazon.com/blogs/storage/copying-objects-greater-than-5-gb-with-amazon-s3-batch-operations/
- [65] https://docs.aws.amazon.com/AmazonS3/latest/API/API_UploadPartCopy.html
- [66] https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob-from-url
- [67] https://learn.microsoft.com/en-us/rest/api/storageservices/put-block-from-url
- [68] https://learn.microsoft.com/en-us/azure/storage/blobs/archive-rehydrate-overview
