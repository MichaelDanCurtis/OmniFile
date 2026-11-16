# Research findings (2026-09-26)

These findings were distilled from the research conversation. Claims marked **verified** were read at the primary source during that session. Everything else comes from research subagents, with sources attached. **UNVERIFIED** marks claims that had no primary source.

## Azure Blob tiers and recall

- **Archive is offline.** Rehydration takes "up to 15 hours … for objects under 10 GB" at Standard priority. High priority "might complete in less than one hour for objects under 10 GB". **Priority retrieval is capped at 10 GiB per hour per storage account**, and bulk timelines don't scale linearly. *(verified)* https://learn.microsoft.com/en-us/azure/storage/blobs/archive-rehydrate-overview
- **Copy Blob out of Archive avoids the early-deletion fee.** Set Blob Tier before day 180 incurs it. Completion raises `Microsoft.Storage.BlobTierChanged`. *(verified, same page)*
- **Minimum durations:** Cool 30 days, Cold 90, Archive 180. Archive is supported on LRS, GRS and RA-GRS only, not ZRS or GZRS. https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-overview
- **Smart tier (GA):**
  - Moves blobs Hot → Cool after 30 days without access, → Cold after 90, and back to Hot on access.
  - No early-deletion or retrieval fees; a per-object monitoring fee applies above 128 KiB.
  - Requires ZRS, GZRS or RA-GZRS. **Does not support Archive.**
  - A blob moved to an explicit tier can't return to smart.
  
  *(verified)* https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-smart
- **Upload straight into Archive.** Put Block List accepts `x-ms-access-tier` (Hot, Cool, Cold, Archive, Smart). Copying a Hot, Cool or Cold source into an Archive destination across accounts in the same region is supported. https://learn.microsoft.com/en-us/rest/api/storageservices/put-block-list
- **Lifecycle rules:**
  - Max 10 prefixes per rule; changes take effect within 24 hours.
  - Last-access tracking updates at most once per 24 hours.
  - Add `daysAfterLastTierChangeGreaterThan` to `tierToArchive`, or lifecycle rules re-archive rehydrated blobs.
  
  https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview
- **List prices, East US, fetched 2026-09-26** (Azure Retail Prices API), per GB-month:

  | Tier | LRS | ZRS |
  |---|---|---|
  | Hot | 0.0208 | 0.026 |
  | Cool | 0.0152 | 0.019 |
  | Cold | 0.0036 | 0.004 |
  | Archive | 0.00099 | — |

  - Write operations per 10k (LRS): Hot 0.05, Cool 0.10, Cold 0.18, Archive 0.10.
  - Archive retrieval: Standard $0.02/GB, High $0.10/GB.

## Networking

- **ExpressRoute gateway throughput by SKU:** ErGw1Az 1 Gbps, ErGw2Az 2 Gbps, ErGw3Az 10 Gbps; ErGwScale 1 Gbps per unit. *(verified)* https://learn.microsoft.com/en-us/azure/expressroute/expressroute-about-virtual-network-gateways
- **Private endpoint throughput through the gateway "might be reduced by half compared to connectivity to non-private endpoint resources."** *(verified, same page)*
- **Packet limit:** max TCP/UDP packet size 1,400 bytes, and gateways don't support fragmentation. *(verified, same page)*
- **FastPath for Private Link:** ExpressRoute Direct only, limited GA with enrollment. https://learn.microsoft.com/en-us/azure/expressroute/about-fastpath
- **Private Link data processing:** about $0.01/GB for the first PB (Retail Prices API).
- **Event Grid push delivery to private endpoints is not supported.** Pull delivery via Event Grid namespaces is. https://learn.microsoft.com/en-us/azure/event-grid/consume-private-endpoints
- **Cross-account Copy Blob:** the source must be authorized with a SAS (a user-delegation SAS is fine). https://learn.microsoft.com/en-us/rest/api/storageservices/copy-blob

## Linux interception and NFS semantics

- **fanotify denials:** `FAN_DENY_ERRNO` exists since Linux 6.13. Permitted errnos: EPERM, EIO, EBUSY, ETXTBSY, EAGAIN, ENOSPC, EDQUOT. **Upon close(2), outstanding permission events are set to allowed**, so the design fails open. *(verified)* https://man7.org/linux/man-pages/man7/fanotify.7.html
- **Pre-content events:**
  - `FAN_PRE_ACCESS` is in the Linux 6.14 uapi headers; ext4, xfs and btrfs opt in.
  - The mmap page-fault hook was reverted.
  - Restartable events had not merged as of mid-2026.
  
  https://lwn.net/Articles/1075829/ and https://github.com/amir73il/fsnotify-utils/wiki/Hierarchical-Storage-Management-API
- **knfsd identity:** knfsd maps `-EAGAIN` to `nfserr_jukebox` (`fs/nfsd/vfs.c`, read in source, not run). Events carry an nfsd kernel-thread pid and no uid, so **knfsd hides the requesting user**.
- **NFS-Ganesha (LGPL-3):**
  - FSAL code sees `op_ctx->creds` (uid, gid, groups) and `op_ctx->caller_addr`.
  - `ERR_FSAL_DELAY` becomes NFS4ERR_DELAY or NFS3ERR_JUKEBOX.
  
  https://github.com/nfs-ganesha/nfs-ganesha
- **Linux NFS client behaviour:** it retries `NFS4ERR_DELAY` with backoff from 100 ms to 15 s, with no limit (NFSv3 JUKEBOX: every 5 s). Applications block (killable). (Source-read.)
- **Offline attribute:** RFC 9754 (March 2025, Haynes & Myklebust) defines `fattr4_offline` (attribute 83) for **NFSv4.2 only**; not in Linux yet. *(verified)* https://www.rfc-editor.org/rfc/rfc9754.html
- **Kerberos cost:** measured by Microsoft on Azure NetApp Files, krb5 −53% IOPS, krb5i −55%, krb5p −77%. https://learn.microsoft.com/en-us/azure/azure-netapp-files/performance-impact-kerberos

## Identity and Teams

- **Naming:** Azure AD was renamed Microsoft Entra ID in 2023. https://learn.microsoft.com/en-us/entra/fundamentals/new-name
- **SSSD and Samba:** Red Hat does not support SSSD for Samba file servers; use winbind. https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/configuring_and_using_network_file_services/assembly_using-samba-as-a-server_configuring-and-using-network-file-services
- **SID → Entra lookup:** Graph user `onPremisesSecurityIdentifier` supports `$filter eq`. https://learn.microsoft.com/en-us/graph/api/resources/user?view=graph-rest-1.0
- **Proactive Teams bot:**
  - Install for a user via Graph (`TeamsAppInstallation.ReadWriteSelfForUser.All`), then message them 1:1.
  - `Action.Execute` buttons send an invoke to the bot.
  - Sending a chat message app-only via Graph is not supported, except for migration.
  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages
- **The bot needs a public endpoint.** Disabling public access on the Azure Bot unconfigures the Teams channel, so the messaging endpoint must be publicly reachable. https://learn.microsoft.com/en-us/azure/bot-service/dl-network-isolation-concept?view=azure-bot-service-4.0
- **SDKs:** the Teams SDK for Python went GA in May 2026; Bot Framework SDK long-term support ended December 2025.
- **Edge credentials:** Arc-enabled servers get a managed identity. The Arc private link scope does not cover Entra token traffic, which still needs outbound 443. https://learn.microsoft.com/en-us/azure/azure-arc/servers/managed-identity-authentication

## Speed: compression and dedupe

- **SEG-Y layout:** 3200-byte text header, 400-byte binary header, 240-byte trace headers. Samples dominate the bytes. https://library.seg.org/pb-assets/technical-standards/seg_y_rev1-1686080991247.pdf
- **Lossless float compression:** about a 1.41× geometric mean on single-precision science data (ASPLOS '25). Planning figure for SEG-Y is 1.1–1.5×, **UNVERIFIED for seismic**. https://userweb.cs.txstate.edu/~burtscher/papers/asplos25.pdf
- **Dedupe:** no published seismic dedupe ratios were found. Reprocessing rewrites every sample, so whole-file dedupe is the only expected win.
- **AzCopy tuning:** concurrency defaults to 16 × CPUs, capped at 300. https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azcopy-optimize
- **Data Box next-gen:** 120 or 525 TB usable. https://learn.microsoft.com/en-us/azure/databox/data-box-overview

## Market scan (why build)

Nothing surveyed combines plain objects, the Archive tier, NFS recall signalling, user notification and a self-service recall portal.
- **Closest:**
  - Spectra StorCycle: Azure Archive, native formats, HTML/symlink stubs, restore users, completion email.
  - Komprise: Microsoft-validated, works with any NAS including Isilon, native objects, Cool → Archive policies.
- **Hot/Cool/Cold only:** Nasuni, CTERA and Panzura (not plain objects), NetApp FabricPool, Isilon CloudPools, Azure NetApp Files cool access.
- **No Archive at all:** Azure File Sync, which targets Azure Files, not Blob.
- **Azure Data Box Gateway (the product's model):** a virtual appliance serving NFS/SMB, writing native blobs and evicting its local cache. Archive is possible only through lifecycle rules, and refreshing a share does not rehydrate. https://learn.microsoft.com/en-us/azure/databox-gateway/data-box-gateway-overview

## To research next (S3 backend)

Needs the same primary-source pass the Azure side had:
- PrivateLink interface endpoints for S3 reachable from on-prem over Direct Connect or VPN (DNS, throughput).
- IAM Roles Anywhere setup and limits.
- S3 `RestoreObject` limits: 1,000 requests/s per account and 1–2 PB/day are known from the Prizmanyx research; recheck.
- Intelligent-Tiering archive access tiers as the "smart" mapping.
- S3 list prices for the policy editor's cost preview.
