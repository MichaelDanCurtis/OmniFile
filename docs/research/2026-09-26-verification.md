# Verification pass: open items from the research phase

Read at the primary source on 2026-09-26, after the owner closed decisions 19–21. **VERIFIED** means the claim was read on the cited page. **INFERRED** means it follows from what the page says but is not stated outright. **UNVERIFIED** means no primary source was found.

## Amazon S3 backend column

The tier mapping in `AGENTS.md` holds. Corrections and additions:

- **Minimum sizes (VERIFIED).** Standard-IA, One Zone-IA and Glacier Instant Retrieval bill at least 128 KB per object. Flexible Retrieval and Deep Archive add 40 KB per object: 32 KB at the archive rate and 8 KB at the Standard rate. [1]
- **Restores (VERIFIED).** [2][3]
  - **Expedited (1–5 min):** only for objects under 250 MB. It covers Flexible Retrieval and Intelligent-Tiering Archive Access, **not Deep Archive**. Without provisioned capacity, a request "might not be accepted" when demand is high.
  - **Standard:** 3–5 h for Flexible Retrieval; within 12 h for Deep Archive. Through Batch Operations: minutes to 5 h, and 9–12 h.
  - **Bulk:** 5–12 h for Flexible Retrieval; within 48 h for Deep Archive.
  - **Throughput:** 1–2 PB/day per account. Objects over 5 TB take longer.
  - **Billing:** the restored temporary copy is billed on top of the archived copy.
- **Restore rate (VERIFIED; sharing INFERRED).** 1,000 restore requests/s per account, returning `ThrottlingException` above that. The docs say Batch Operations "fully utilizes" the account's restore rate, which implies one pool shared with user recalls. Keep the token bucket. [2]
- **Intelligent-Tiering (VERIFIED).** [1][3]
  - Objects under 128 KB are never monitored or tiered.
  - Restores from its archive tiers take no `Days`: the object returns to Frequent Access and has no expiry.
  - A Batch restore job cannot mix Intelligent-Tiering objects with Flexible Retrieval or Deep Archive objects.
  - "Object Restore Expired" is defined only for Flexible Retrieval and Deep Archive, so none fires for Intelligent-Tiering (INFERRED). [5]
- **Private networking (VERIFIED).** S3 interface endpoints are reachable from on-prem over Direct Connect or Site-to-Site VPN. [4] For DNS, there are two options:
  - use the endpoint-specific names (`bucket.vpce-….s3.<region>.vpce.amazonaws.com`), which resolve publicly, so on-prem DNS needs no changes; or
  - use private DNS through a Route 53 Resolver inbound endpoint. The default "private DNS only for inbound endpoints" setting also needs an S3 gateway endpoint in the VPC.
- **TLS (VERIFIED).** S3 interface endpoints do not support TLS 1.3 or hybrid post-quantum TLS. The S3 encryption page says TLS 1.3 is supported "in all S3 endpoints, except for AWS PrivateLink for Amazon S3 and Multi-Region Access Points". The Rust client must negotiate TLS 1.2. [4][8]
- **IAM Roles Anywhere (VERIFIED).** [6][7]
  - The trust anchor is AWS Private CA or an external CA.
  - The credential helper accepts TPM 2.0 keys (Linux since v1.3.0) and PKCS#11.
  - Sessions last 900–43,200 s.
  - `serve` mode offers a local endpoint compatible with the EC2 instance metadata service (IMDSv2) and refreshes credentials 5 minutes before expiry. That suits the Rust daemon.
  - Roles Anywhere has its own interface endpoint (from a search excerpt of [9]).
- **CRC64 (VERIFIED, partly).** [10][11][12]
  - S3 attaches a full-object CRC64NVME checksum when an upload carries none. Whether current SDKs always choose CRC64NVME is UNVERIFIED.
  - For multipart uploads, CRC64NVME supports only the full-object type.
  - Azure's CRC64 is the same CRC64-NVME (polynomial `0xad93d23594c93659`, reflected input/output, init and xorout all-ones; the reflected table constant is `0x9a6c9329ac4bc9b5`; check value for `123456789` is `0xAE8B14860A799888`).
  - **Azure checks it per request; nothing says Azure stores a whole-blob CRC64.** The design already combines the per-block CRCs itself and compares the result with S3's full-object value.
- **Batch Compute checksum (VERIFIED).** It works on every storage class without a restore. It produces full-object checksums, MD5 included. [13][14] Caveats:
  - jobs over Flexible Retrieval or Deep Archive objects can take up to a week;
  - SSE-C objects are not supported;
  - results come back in the completion report and are not stored on the objects.
- **EventBridge (VERIFIED).** The events are "Object Restore Initiated", "Object Restore Completed" and "Object Restore Expired". [5]
- **Presigned URLs** as an edge credential were not re-checked.

Sources:
[1] https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html
[2] https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects-retrieval-options.html
[3] https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects.html
[4] https://docs.aws.amazon.com/AmazonS3/latest/userguide/privatelink-interface-endpoints.html
[5] https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventBridge.html
[6] https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html
[7] https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html
[8] https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingEncryptionInTransit.html
[9] https://docs.aws.amazon.com/rolesanywhere/latest/userguide/vpc-interface-endpoints.html
[10] https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html
[11] https://learn.microsoft.com/en-us/rest/api/storageservices/structured-body-format
[12] https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-upload
[13] https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-compute-checksums.html
[14] https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html

## Azure Storage Mover as the copier for restored objects (decision 20)

**Verdict: holds for bulk, prefix-wide copies; has gaps for restore-triggered copies of specific objects, and for private-only targets.**

What is confirmed (VERIFIED):
- S3 → Azure Blob cloud-to-cloud copy has been **GA since 2025-10-23**. It is agentless and the service is free; AWS egress still applies. [15]
- It connects to AWS through an Azure Arc multicloud connector, set up on the AWS side by a CloudFormation stack. [16][17]
  - The connector runs in only 13 Azure regions and scans 15 AWS regions. [18]
  - The exact IAM actions the stack grants are not published (UNVERIFIED).
- **Glacier:** "doesn't support automatic rehydration of archived objects. Data stored in AWS Glacier or Deep Archive must be restored before migration." [19] So restored objects can be copied.
  - Whether unrestored objects in the same prefix are skipped or counted as failed is not documented.
  - Glacier Instant Retrieval and the Intelligent-Tiering archive tiers are not mentioned.
- **Scoping:** a job definition takes one `sourceSubpath` and one `targetSubpath`. Copy mode is Additive (copy only new files) or Mirror. There is **no object list, manifest or include/exclude filter** (API 2026-05-01). [20]
- **Target:** the target is written with the endpoint's managed identity (Storage Blob Data Contributor), so no shared key is needed. [16] But:
  - private networking is documented only for the **source**, through Private Link Service Direct Connect to an S3 interface endpoint, which needs a portal flight link, with a limit of 10 private connections per subscription per region [21];
  - Storage Mover is **not** on the Azure Storage trusted-services list [22];
  - a target account with public network access disabled is **undocumented**.
- **Tier, metadata, checksums:**
  - there is no destination access-tier setting;
  - integrity checking is MD5 (`dataIntegrityValidation`), and whether it applies to cloud-to-cloud jobs is unstated;
  - how `x-amz-meta-*` maps to blob metadata is undocumented. [20]
- **Automation:**
  - ARM REST and the Python SDK (`azure-mgmt-storagemover`) cover create, start, stop, get and list. [23]
  - A job run reports status, scan status, item and byte counters (scanned, transferred, failed, excluded, unsupported, noTransferNeeded), errors and warnings. [24]
  - Per-object logs reach Log Analytics **only for failures**. [25]
  - No Event Grid events are documented.
- **Limits:** 500 million objects per job; 10 concurrent jobs per subscription (can be raised by support request). The published private-path examples reach 4.5–5.6 Gbps. [19]
- **Reverse direction (Azure → S3): not supported;** every target is Azure. [19] AWS DataSync can do Azure Blob → S3, but:
  - it authenticates **only with an account or container SAS**; "You can't provide DataSync a user delegation SAS token" [26];
  - Archive blobs must be rehydrated first, and the Cold tier is unsupported;
  - blobs over 5 TB, or with more than 2 KB of metadata, are not transferred.

Gaps against our design:
1. **A restore wave cannot be a job.** Restored objects sit among unrestored ones under the same prefix. Options:
   - S3 Batch Copy the restored objects to a staging prefix first, then point Storage Mover at that prefix and map `targetSubpath` back to the real path; or
   - accept whole-prefix rescans, with unrestored objects reported as failed or unsupported.
2. **Private-only, Entra-only target account:** unproven. It conflicts with decision 10 until a pilot shows it works.
3. **Tier, metadata and CRC64 need a second pass** after Storage Mover finishes: set the tier, stamp POSIX metadata, and verify.
4. **Completion is per job, not per object.** Recall status has to come from our own reconciliation, for example the Blob change feed.
5. **Azure → S3 (decision 14) has no copier that fits "no shared keys".** DataSync needs an account-key SAS, and a DataSync agent VM inside the VNet for a private-only account.

**Resolution (2026-09-26):** decisions 22–24 answer these gaps. The customer creates private endpoints, and the pilot tests the Storage Mover write path. v1 is S3 → Azure only. Jobs are fed by Additive reruns and staging prefixes. A per-file CRC64 verifier runs before cutover.

Sources:
[15] https://azure.microsoft.com/en-us/blog/fully-managed-cloud-to-cloud-transfers-with-azure-storage-mover/
[16] https://learn.microsoft.com/en-us/azure/storage-mover/cloud-to-cloud-migration
[17] https://learn.microsoft.com/en-us/azure/azure-arc/multicloud-connector/add-public-cloud
[18] https://learn.microsoft.com/en-us/azure/azure-arc/multicloud-connector/overview
[19] https://learn.microsoft.com/en-us/azure/storage-mover/service-overview
[20] https://learn.microsoft.com/en-us/rest/api/storagemover/job-definitions/create-or-update?view=rest-storagemover-2026-05-01
[21] https://learn.microsoft.com/en-us/azure/storage-mover/cloud-to-cloud-private-network-configuration
[22] https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-trusted-azure-services
[23] https://learn.microsoft.com/en-us/python/api/azure-mgmt-storagemover/azure.mgmt.storagemover.operations.jobdefinitionsoperations?view=azure-python
[24] https://learn.microsoft.com/en-us/rest/api/storagemover/job-runs/get?view=rest-storagemover-2026-05-01
[25] https://learn.microsoft.com/en-us/azure/storage-mover/log-monitoring
[26] https://docs.aws.amazon.com/datasync/latest/userguide/creating-azure-blob-location.html

## Azure Smart tier

- **GA (VERIFIED).** The smart-tier overview (updated 2026-08-12) says: "Smart tier is generally available in all public cloud regions with zonal redundancies." It is still preview in Azure Government and 21Vianet (China). The Azure blog announced GA on 2026-04-14. [27][28]
  - The Put Blob and Set Blob Tier REST pages still say "currently in public preview". Both were last updated on 2026-03-26, before the GA date, so they are stale. [29][30]
  - Data-plane `x-ms-access-tier: Smart` needs REST version 2026-02-06 or later.
- **Smart is the account's default access tier.** Two consequences for our design:
  - a blob uploaded with an explicit tier never enters Smart, so **uploads to the live account must not set `x-ms-access-tier`**;
  - once a blob has an explicit tier it cannot go back to Smart. [27]
- **Where it works (VERIFIED).** Standard GPv2 accounts with ZRS, GZRS or RA-GZRS; block blobs only. [27]
- **Billing (VERIFIED).** [27]
  - Capacity is billed at the underlying hot, cool or cold rate.
  - A monitoring fee is charged per 10K objects over 128 KiB. Objects of 128 KiB or less are free and never tier down.
  - There is no charge for transitions within Smart, early deletion, or retrieval.
  - The dollar rate is UNVERIFIED.
- **Archive is not supported on ZRS, GZRS or RA-GZRS (VERIFIED)**, and Smart does not include Archive. The two-account design stands. [31]

## Linux kernel and Azure Arc

- **fanotify pre-content events (`FAN_PRE_ACCESS`) were merged in Linux 6.14 (VERIFIED).** Restrictions: [32][33][34]
  - They work **only on ext4, xfs and btrfs**; other filesystems return `EOPNOTSUPP`.
  - The page-fault hook was reverted before 6.14 shipped and replaced by a hook at `mmap()` time covering the whole mapped range. So the daemon must fill the **entire mmapped range** when `mmap()` is called, not page by page.
- **RHEL 10 cannot run the SMB stub guard.**
  - RHEL 10.0 through 10.2 ship kernel 6.12 (VERIFIED). [35][36]
  - CentOS Stream 10 main (kernel 6.12.0-271, 2026-09-24) has no `FAN_PRE_ACCESS` and no `SB_I_ALLOW_HSM` (VERIFIED); no backport errata were found. That RHEL has no backport is INFERRED.
- **Ubuntu 26.04 LTS ships kernel 7.0 (VERIFIED).** It is the reference OS for the edge filer. [37]
- **Arc supports Ubuntu 26.04 and RHEL 10 on x86-64 only (VERIFIED).** [38]
- **Arc cannot run entirely over Private Link (VERIFIED).** Private Link covers the hybrid identity service (`*.his.arc.azure.com`) and guest configuration. [39][40] These stay on the default internet route:
  - Entra ID (`login.microsoftonline.com`, `*.login.microsoft.com`, `pas.windows.net`);
  - extension notifications (`guestnotificationservice.azure.com`, `*.servicebus.windows.net`);
  - `packages.microsoft.com`;
  - ARM, unless a Resource Management Private Link is added.

  An edge that uses the Arc managed identity therefore needs outbound HTTPS to Entra. An edge that must have no internet path uses the other credential route: mTLS to the service, which mints user-delegation SAS tokens.

Sources:
[27] https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-smart
[28] https://azure.microsoft.com/en-us/blog/optimize-object-storage-costs-automatically-with-smart-tier-now-generally-available/
[29] https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob
[30] https://learn.microsoft.com/en-us/rest/api/storageservices/set-blob-tier
[31] https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-overview
[32] https://github.com/torvalds/linux/commit/8883957b3c9de2087fb6cf9691c1188cccf1ac9c
[33] https://github.com/torvalds/linux/commit/5121711eb8dbcbed70b1db429a4665f413844164
[34] https://github.com/torvalds/linux/commit/066e053fe208a3b83ee89dc5a192146add688861
[35] https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/10.1_release_notes/architectures
[36] https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/10.2_release_notes/architectures
[37] https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/
[38] https://learn.microsoft.com/en-us/azure/azure-arc/servers/prerequisites
[39] https://learn.microsoft.com/en-us/azure/azure-arc/servers/private-link-security
[40] https://learn.microsoft.com/en-us/azure/azure-arc/servers/network-requirements
