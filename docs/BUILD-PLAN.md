# OmniFile Gateway v1 — build plan

Written 2026-10-05, after the research phase (decisions 1–26) and the local filer spike (`docs/pilots/filer-spike-results.md`). `AGENTS.md` is the anchor and `docs/ARCHITECTURE.md` the component reference; this file says **in what order we build v1, and what "done" means for each step**.

## What v1 is

- **Edge filer.** An Ubuntu 26.04 appliance serving NFS (v3, 4.x) and SMB, joined to AD.
- **Azure path.** Files go to Azure Blob as plain blobs and tier down to Archive on the client's schedule, or by Smart tier plus an archive date. They come back on request, with the user told in Teams. A portal lets admins set policy.
- **Migration path.** During a Prizmanyx S3 → Azure migration, the same NFS share presents S3-resident files (decision 19). Storage Mover moves the bytes, and OmniFile verifies them before cutover (decisions 20, 24).

Out of v1: Azure → S3, GCP, an SMB view of S3-resident files, and Secure Boot.

## Ground rules

1. **Vertical slice first.** The first milestone runs one file end to end through every layer, so the interfaces are fixed before anything is widened.
2. **Spikes are reference, not code.** `spikes/` stays throwaway. Product code is written fresh in `edge/`, `fsal/` and `service/`. The spike **tests** are ported as regression tests, because they pin the kernel and Ganesha behaviour we depend on.
3. **Fail closed is a test, not a hope.** Every milestone that touches stubs keeps the fail-closed suite green: daemon kill, group release, queue flood, pre-armed fds, and daemon-down FSAL reads. No reader may ever see zeros.
4. **Pilots feed numbers in; they don't block the start.** Each milestone says which pilot result it needs, and by when.
5. **Done** means merged to `main`, with:
   - tests in CI;
   - `LICENSES.md` updated for new dependencies;
   - the docs (`ARCHITECTURE.md`, and `AGENTS.md` if a decision moved) matching the code.

## Test strategy: prove it before Azure

Most testing happens before anything is deployed to Azure. There are three layers, from cheapest to most faithful.

1. **Simulated backend ("sim"), where most testing happens.** It is an implementation of the backend contract (`ARCHITECTURE.md` §3) that obeys Azure's and S3's rules, as recorded in `docs/research/2026-09-26-verification.md` and `docs/design/tiers-versioning-worm.md`:
   - logical tiers, Smart, and "an explicit tier can never return to Smart";
   - Archive with a configurable rehydrate delay, plus the 10 GiB/h priority cap;
   - Copy-from-URL;
   - versioning, and WORM retention and legal hold (deletes blocked, overwrites become versions);
   - an ordered change feed;
   - CRC64-NVME per block;
   - S3 restore with `Days` and expiry.

   It runs on a virtual clock, so a 15-hour recall takes seconds, and it injects faults: slow or failed recalls, throttling, outages, partial copies. With sim, the whole system runs on one Mac (edge VM, service, Postgres, sim), and it is the backend for the end-to-end, fail-closed, recall, Teams and permission-matrix suites in CI.
2. **Floci, for SDK plumbing only.** Floci is a family of local cloud emulators (MIT licence). A smoke lane checks that our real Azure and AWS client code makes valid calls where Floci is faithful:
   - `floci-az`: blob CRUD, SAS and user delegation keys;
   - `floci` (AWS): S3 versioning and Object Lock, SQS, EventBridge.

   Per their READMEs, `floci-az` has no tiers, Archive, Smart, rehydrate, Copy-from-URL, versioning, immutability, change feed, index tags, CRC64 or Entra bearer auth. Glacier restore, S3 Batch Operations and IAM Roles Anywhere aren't listed for the AWS emulator either. So Floci never stands in for those. Confirm its coverage by running the images before relying on it.
3. **A real Azure dev subscription, for what no emulator has:** tiers and Smart, Archive and rehydrate, immutability and versioning, the change feed, CRC64, Entra auth and private endpoints. It runs on a schedule against short-lived resources torn down after each run. It is the pilot 4 kit made repeatable, and it **checks sim against the real thing**: the same contract tests run against sim and real Azure, and a difference is a bug in sim.

Before any real deployment, a Bicep `what-if` against the target subscription shows exactly what will be created.

### CI cost

**No workflow runs on its own** (owner's choice, 2026-10-20). The repo has no GitHub-hosted runners, so every workflow is manual (`workflow_dispatch`) and the lanes run locally instead: service `pytest`, portal `npm test`, edge `cargo test` and `clippy`, and the e2e scripts. Their results go in the PR.

If runners come back, a workflow is started from the Actions tab. They were split by cost:
- **Light:** `ci.yml` (Rust and Python lint and unit tests), `fsal.yml`, `relay.yml`, `floci.yml` and `portal.yml`.
- **Heavy:** `edge-linux.yml`, `e2e.yml` (the M1, M2 and M5 exit tests), `smbguard.yml` (the A-suite, about 75 minutes), `kernel-canary.yml` and `appliance.yml`. They skip draft PRs when triggered by one.
- **The kernel canary** used to run weekly to catch runner kernel updates. Run it by hand after a kernel update instead.

Work in a draft PR, and merge stacked PRs by fast-forward.

## Milestones

Sizes are relative (S, M, L, XL), not dates.

### M0 — Foundations (S)

- **CI:**
  - Rust: `cargo fmt`, `clippy -D warnings`, `test`.
  - Python: `ruff`, `pytest`.
  - C: a containerised build of `FSAL_OMNI` against a **pinned Ganesha V15.6 source tree** built with `-ljemalloc`. The spike found no distribution ships Ganesha headers, and the module must match Ganesha's exact build.
- **Kernel test lane.** The fanotify and FSAL suites need Ubuntu 26.04's 7.0 kernel; GitHub-hosted runners can't provide it (VERIFY the current runner kernels). Use a self-hosted runner that boots the Lima/QEMU image from `spikes/` as code, or a cloud VM. Run the suite on every PR that touches `edge/` or `fsal/`.
- **Contracts, written down before any code that uses them:**
  - The **edge ⇄ service control API**: mTLS, private IP; enrolment, SAS/credential minting, stub and recall instructions, events.
  - The **FSAL ⇄ daemon socket protocol**: framing, versioning, timeouts, fail-closed defaults.
  - The **catalog schema v0** (Postgres), with **PGMQ** installed as SQL by our migrations (decision 27), and the **backend contract** (`ARCHITECTURE.md` §3) as Python protocols.
  - The **service hosting layout** (`ARCHITECTURE.md` §1): Container Apps (`api`, `worker`, verifier jobs, a public relay) plus Postgres Flexible Server. Settle where the relay's public ingress lives.
- **Simulated backend (`sim`)** and its contract-test suite, per the test strategy above. The same tests later run against the real Azure and S3 backends. The **Floci smoke lane** follows once real backend code exists.
- **Dev environment:**
  - a one-command Lima VM (Ubuntu 26.04, xfs volumes, Ganesha, Samba);
  - a dev Azure subscription with a ZRS/Smart live account and an LRS archive account (the scripts in `spikes/azure-test/`);
  - a Samba AD domain controller VM, which stands in for the client's AD.

**Exit:** CI is green on all three languages plus the kernel lane; the three contracts are reviewed; `sim` passes the backend contract-test suite.

### M1 — Vertical slice: NFS write → upload → stub → read back (L)

Azure only, one NFS share, hot/cool tiers only (no Archive yet).

- **Edge daemon:**
  - `detector`: `FAN_CLOSE_WRITE` plus a quiet period;
  - `hasher`: BLAKE3, plus CRC64-NVME per block;
  - `uploader`: block upload with per-block `x-ms-content-crc64`, resumable journal, **no explicit tier** (decision 26 / Smart rule);
  - `stubber`: punch holes, xattrs, resident region = headers **+ 1 MiB** (readahead finding);
  - `recall-fill`: range-get from Blob into place;
  - `control client`: mTLS, with the key in a file for now; TPM comes in M6.
- **FSAL v0 (production):**
  - stacked over FSAL_VFS for local files, as in the spike, but with the namespace design from risk R1 decided first;
  - a **pending-recall table inside the FSAL**: answer `DELAY` from memory and never block a Ganesha worker on the daemon;
  - **xattr reads without an `O_RDWR` open** (the `chattr +i` bug);
  - fail closed on any socket error.
- **Service:**
  - the catalog;
  - the edge API (mTLS), which mints per-blob user-delegation SAS;
  - the Azure backend `put`, `get_range` and `head`;
  - a trivial policy: "stub after N minutes".
- **Packaging:** systemd units for the daemon and Ganesha, on the dev VM.

**Exit:**
- A file copied to the NFS share appears in Blob with a matching CRC64, becomes a stub, and reads back byte-identical on NFSv3, 4.1 and 4.2 under `wait` and `failfast`.
- The ported B1–B5 and fail-closed tests pass.

**Needs from pilots:** nothing.

### M2 — SMB, identity and the stub guard (L)

- **Samba and winbind** joined to the dev AD with `idmap_ad`. The same uids are seen by Ganesha and smbd.
- **AD RBAC on the filer** (decision 28, `docs/design/access-control.md`):
  - POSIX ACLs naming AD groups are the single enforced model for both protocols, with no `vfs_acl_xattr`;
  - Ganesha `Manage_Gids = true`, so groups come from AD and not the client;
  - share Owners/Writers/Readers groups applied as root and default ACLs, plus SMB `valid users` and access-based enumeration;
  - share trust levels (proven vs host-trusted);
  - ACL snapshots reported to the catalog;
  - appliance admin through an AD group;
  - audit of opens, recalls and ACL changes.
- **`smb-guard`, production version of the spike's design:**
  - the fanotify group held in the systemd fd store;
  - `FAN_UNLIMITED_QUEUE`;
  - an event journal with orphan replay and a deny-sweep;
  - `RestartMode=direct`;
  - smbd and Ganesha bound to the guard unit;
  - a private fill mount;
  - ignore marks on filled inodes;
  - `(0,0)` events treated as whole-file;
  - a size cap for huge ranges;
  - never exit on `EMFILE`;
  - guard ops interlocked: refuse `disable`, `clean` or uninstall while servers run.
- **`O_TRUNC` watcher** (`FAN_MODIFY` / generation check), so overwritten stubs lose their stub state.
- **Identity:**
  - NFS: uid → SID via winbind;
  - SMB: event pid → smbd session → SID, read while the request is pending (`FAN_REPORT_PIDFD` against pid reuse);
  - `claimed` vs `proven` flags;
  - Kerberos per share (`sec=krb5|krb5i|krb5p`) for Ganesha and Samba.
- **`.RECALLING.txt` notes** next to stubs.

**Exit:**
- An SMB read of a stub waits or fails per policy and is attributed to the right AD user.
- The whole A-suite passes, including kill -9 loops and the 17,000-reader flood.
- A Kerberos NFS mount works against the dev AD.
- The **permission matrix** from `docs/design/access-control.md` passes: Readers/Writers/no-group users × NFS `sys`+`Manage_Gids` / NFS `krb5` / SMB × resident / stub / recalling. An Explorer ACL edit is enforced over NFS, and group removal revokes access.

**Needs from pilots:** the on-site part of pilot 1 (Windows clients, STATUS_PENDING handling, Explorer behaviour, real AD), before M7.

### M3 — Tiering and Archive on Azure (L)

- **Policy engine and scheduler:**
  - `scheduled` (warm, cold and archive days) and `smart` plus an archive day;
  - `clock = last_read | written`;
  - `stub_at`.
- **Smart → Archive:** on the archive day, a server-side copy into the LRS archive account with `x-ms-access-tier=Archive`, then delete the original.
- **Recall by copy** (never Set Blob Tier):
  - Standard and High priority;
  - a **10 GiB/h per-account budget** for priority;
  - ETAs from `capabilities()`, with no promises above 10 GB.
- **Change feed reader:** the catalog-sync and recall-status stream. Get Blob Properties is used only for the one file a user is waiting on.
- **Holds:** index tags for state, and version-level WORM legal hold set at upload. Nightly reconcile from Blob Inventory on non-WORM accounts, and from the change feed plus listings on WORM accounts.
- **Versioning and WORM storage** (decisions 30, 31):
  - blob versioning on every account;
  - `file_versions` in the catalog with as-of queries;
  - depth pruning by a lifecycle rule plus the catalog;
  - WORM shares in one LRS/GRS account with version-level WORM, auto-lock after the test window, and in-place Set Blob Tier;
  - recall copies into a non-WORM recall container;
  - legal holds per version;
  - the edge keeps any committed version until it is uploaded, even if it is overwritten first.
- **Tier pins** (decision 29) honoured by the scheduler, with the archive date still respected.
- **`waitquick:<minutes>`** decided on the backend's ETA.

**Exit:**
- A file goes hot → archive on schedule and its stub reads trigger a real rehydrate.
- The reader waits or fails per policy, and the bytes come back verified.
- Change-feed lag is measured.

**Needs from pilots:** pilot 4's real recall times (8 GB, 184 GB, 1.2 TB), which set the ETA table before M4's portal shows times.

### M4 — Teams, portal and policy editor (L)

- **Teams:**
  - a proactive bot with Adaptive Cards and `Action.Execute` buttons (Recall, Keep online, Track);
  - cards that update through requested → rehydrating → downloading → available.
- **Public relay** (the only public endpoint): validates the Bot Framework JWT, checks the clicker is the intended recipient, and checks the intent is signed and unexpired.
- **Identity lookup:** Graph `onPremisesSecurityIdentifier`, cached. Email fallback through Graph `sendMail`.
- **Gateway page tier and version controls:**
  - set the tier on files and folders, or "keep online until", with fee, Smart-exit and recall-time warnings (decision 29);
  - the **rewind** browser: a folder as of a date, then open, download or restore (decision 30);
  - share settings for rewind depth (Owners) and **WORM** (ArchiveAdmin, one-way, typed confirmation, lock deadline shown) (decision 31).
- **Portal:**
  - Entra OIDC sign-in, with roles `ArchiveAdmin`, `DataSteward` and `User`;
  - a file browser showing tier and archive date;
  - recall ahead of time;
  - the four-step recall view.
- **Policy editor:**
  - the four plain questions, plus "More settings";
  - a live timeline;
  - first-year cost per TB;
  - checks: minimum durations, ordering, recall time, Smart caveats, "nobody is told";
  - the Kerberos throughput note;
  - everything driven by `capabilities()`.
- **Policy proposals** (from Prizmanyx or any AI) need Archive Admin approval in the portal (anti-pattern 7).
- **Access in the portal** (decision 28):
  - listings filtered by the user's AD groups against the catalog's ACL snapshots;
  - portal, Teams and API recalls allowed only for users who can read the file;
  - a share access screen to set Owners/Writers/Readers groups and see each share's trust level.

**Exit:**
- A real Teams user in the dev tenant gets a card for their own stub read.
- Clicking Recall reaches the relay and queues a recall.
- An admin edits a policy and sees the cost update.

**Needs from pilots:** pilot 4's Teams check (proactive install under the client's app policies).

### M5 — S3 backend and the migration namespace (XL, the critical path)

- **S3 backend:**
  - IAM Roles Anywhere, with the key in a file now and the TPM in M6;
  - PrivateLink endpoints with **TLS 1.2**;
  - logical-tier map and `capabilities()` (the verified S3 column);
  - Batch Operations restore waves;
  - EventBridge → SQS completion, pulled through an SQS interface endpoint;
  - restored-copy expiry tracking;
  - a token bucket for the 1,000 restores/s account limit.
- **Catalog seeding** from S3 Metadata tables or Inventory, kept current from the journal. Never LIST S3 on a directory read.
- **FSAL namespace over remote backends:**
  - S3- and Azure-resident files appear in the export, served by range get through an SSD **chunk cache** and a **resident-header cache**;
  - archived objects on either side use the same wait/fail flow.

  This is the largest single piece of work (risk R1).
- **Point-in-time trees over NFS and SMB** (decision 30): read-only `.snapshots/<date>/` and Windows Previous Versions (`@GMT-` tokens), on the R1 namespace.
- **Write semantics while migrating:** new writes go to the destination (Azure), deletes become tombstones, and rename may be refused in v1.
- **Storage Mover orchestration (decisions 20, 24):**
  - Additive reruns after restore waves, and an S3 staging prefix for urgent recalls;
  - the **per-file verifier**: an in-Azure CRC64-NVME read compared with S3 Batch Compute checksum;
  - then stamp the tier (unless Smart), POSIX metadata and tags;
  - a prefix flips to `azure (verified)` only when every file has matched and the journal shows no change since the copy.
- **Partner API** for Prizmanyx (`ARCHITECTURE.md` §6): `adopt`, `sources`, `locations`, `files`, `usage`, `policies/proposals`, `recalls`, `restores`, `events`. Entra app roles; no secrets.
- **ACLs for S3-resident files:** each registered source names its Owners/Writers/Readers groups, which become the default ACL of its folders (decision 28).

**Exit:**
- A test bucket with Standard, Glacier and Deep Archive objects is served through one NFS share.
- A user read of a Glacier object triggers a restore, a Teams card and a Storage Mover copy.
- The verifier passes, and the prefix flips with no client-visible change.

**Needs from pilots:** none, but test S3 data from a Prizmanyx engagement would help.

### M6 — Appliance, deployment and operations (M)

- **Appliance image:**
  - Ubuntu 26.04 minimized, built reproducibly (pick the tool in M0: autoinstall/cloud-init or an image builder);
  - VMware OVA, Hyper-V VHDX and KVM qcow2 outputs.
- **Setup wizard:**
  - network;
  - AD join;
  - service endpoint and enrolment token;
  - data disks (xfs);
  - the "use private endpoints" switch (decision 22: the customer creates the endpoints, and we always use them).
- **Edge enrolment** with a **TPM-held** mTLS key; Roles Anywhere key in the TPM.
- **Updates and reboots:**
  - OS and component updates inside an admin-set maintenance window;
  - NFS clients ride the grace period;
  - **a kernel update is gated by the fanotify regression suite** (risk R3).
- **Service deployment:**
  - Bicep for the Container App, Postgres Flexible Server and VNet integration;
  - a setup guide for private endpoints and DNS;
  - catalog backup and restore.
- **Observability:** metrics and logs from edge and service, with recall and upload dashboards in the portal.

**Exit:** a fresh appliance OVA plus the Bicep deploy reach the M1–M4 behaviour in a clean subscription, by following the guide only.

**Needs from pilots:** pilot 2 (gateway throughput, default concurrency and block size) and pilot 3 (whether to compress, and dedupe savings).

**Status (2026-11-05): built, exit unproven** (`docs/milestones/M6.md`). Built and tested locally, against fakes only:
- the appliance image: reproducible (byte-identical builds on arm64 and x86-64), every output format checked and booted, the R3 gate on both architectures (canary 5/5, A-suite 13/13), A/B updates in the maintenance window (`update-round.sh` 16/16), NFSv3 locking with fixed ports behind a fail-closed firewall, servers that restart after a crash bound to the guard, and the Annex copier in the image (`smoke.sh` 70/70 on arm64, #53);
- the setup wizard, with a TPM-held mTLS key and Roles Anywhere key, a real AD join against the dev DC (`setup.sh --dc` 11/11), and reruns that never format the wrong disk;
- NFS clients ride the grace period: `tests/e2e/nfs_grace_test.sh` 23/23 across a Ganesha restart and a SIGKILL (on the dev VM; through the appliance's own reboot and update, `grace.sh` 6/6 checks the paths, and the full test there is still to run);
- the service deployment: per-app entrypoints, T5 closed by a TCP ingress, images, a what-if gate, the Teams package and the Entra registration; a small and a standard size, about $183 and $676 a month at West US 3 list prices (#59, decision 60; `deploy/azure/check.sh` 34 ok); a catalog restore drill;
- observability, the portal's Activity (now with a live feed), Edges (with an Add edge wizard), Settings and Migrations pages, in the new look (#60, #63; decisions 61 and 62), and the customer guide (82 steps, linted against the Bicep);
- connecting the client's Azure and AWS from the portal, deploying Storage Mover, and scanning a migration's source (#64, #65; `docs/design/cloud-connect.md`);
- the real-cloud harness that runs this exit (`tests/real/run.sh`): a dry run follows the whole guide against fakes (#56; `tests/real` 312 passed), and the first AWS run's policy and roles template wait for the owner's review.

Still needed: the exit in a clean subscription, with cloud connect's VERIFY lines (P); the owner's accounts, keys, registry, public repositories and where images and the AWS connect template are published (O); and the Hyper-V and vSphere imports and Arc at a client site (O). Pilots 2 and 3 are on site.

### M7 — Hardening and release (M)

- Threat model and security review, the public relay above all. Lock down Azure RBAC on the storage accounts (service identity and break-glass group only), because direct Blob access bypasses AD (decision 28).
- Scale tests: catalog at 10⁸ files; thousands of concurrent waiters (the drain-rate finding); 200 GB SEG-Y at line rate.
- Chaos tests: daemon kill during fills, a service outage (reads keep failing closed, and recalls resume), and network loss.
- Windows client matrix (from the pilot).
- Licence: Apache-2.0 since decision 86; outside contributions with a DCO sign-off (decision 87).
- Before the first release: move the update feed (`appliance/release.conf` `RELEASE_REPO`) to a repository appliances can download from without a token (decision 88).
- A first client deployment alongside a Prizmanyx engagement.

**Status (2026-11-05): built as far as it can be without a client** (`docs/milestones/M7.md`):
- the threat model (T1 to T11, T11 being cloud connect), with independent reviews of the relay, the service and its deployment, the edge's privileged paths, and each new package since; every finding is fixed, and T5 is closed by default (#34);
- scale: the catalog seeds about 16,500 files/s, flat from 10^5 to 10^7, so 10^8 is about 1.7 hours per worker (10^8 itself is P, on Azure Database for PostgreSQL); 17,000 concurrent waiters with 0 zeros, drained in 3 s on the dev VM; a local throughput ceiling (200 GB SEG-Y at line rate is pilot 2, O);
- chaos: daemon kills during fills, a service outage and network loss in the M1 and M5 exit tests, and a 6.75-hour soak with 0 zeros, 0 wrong bytes, 0 ENOENT and 0 lost commits; the bugs it found (lost commits, recalls that never resumed, smbd never restarting) are fixed. Its 6-hour rerun on main was red only on 4 recalls failed by a full volume, which are now resumed (#62); versions wait for writers to finish (#54), and a recall whose edge is gone fails (#55). Another rerun on main, and M1 to M5 on main, are next (L);
- the Annex's rules, dry runs and removals from the NAS (#57, #61; decision 59): annex exit test 130/130 on omnifile-acl; real ONTAP and PowerScale are P;
- the Windows client kit is built; the matrix runs on site (O);
- licences: the automatic licence check and vulnerability scan, the full GPL source offer, and CLA and DCO drafts for counsel; the legal review is O;
- the first client deployment is O.

## Order and parallel work

```
M0 ─► M1 ─┬─► M2 ─────────────┐
          ├─► M3 ─► M4 ───────┼─► M7 ─► v1
          ├─► M5 (start R1 design in M0) ─┤
          └─► M6 ─────────────┘
```

- **M1 is the only strictly serial step.** After it:
  - M2, M3 and M6 can run in parallel;
  - M4 needs M3's recall states;
  - M5 needs M1's FSAL and M3's backend contract.
- **M5 is the critical path.** Begin its namespace design (R1) during M0, so M1's FSAL is built namespace-ready, as decision 19 requires.

## Open design risks to settle early

| # | Risk | Settle by |
|---|---|---|
| R1 | **FSAL namespace for remote-only files.** How directories, attributes, file handles and readdir work for objects with no local inode: a stacked FSAL plus virtual entries, or our own FSAL with VFS as a delegate. | A design note and prototype in M0, decided before M1's FSAL is written |
| R2 | **Path resolution latency.** "Resolve every path through the catalog" can't mean a WAN round trip to Postgres in Azure. The edge needs a local metadata replica (for example SQLite/RocksDB), fed from the service's event feed. | Design in M0; built in M1 |
| R3 | **Kernel drift.** Pre-content events are new, and Ubuntu updates could change them. | The fanotify regression suite gates every kernel update (M0 lane, M6 policy) |
| R4 | **Windows behaviour** with long waits (STATUS_PENDING), and Explorer thumbnailing triggering recalls. Stubs are now marked offline to SMB clients and Offline Files is disabled per share; the matrix is `docs/pilots/windows-client-matrix.md`. | Pilot 1 on-site, before M7 |
| R5 | **Teams proactive install** blocked by tenant app policies. | Pilot 4, before M4 exit |
| R7 | **Windows ↔ POSIX ACL fidelity.** Deny entries and some inheritance flags don't map, and NFSv4 SETACL doesn't round-trip onto POSIX ACLs. | Permission-matrix tests in M2; document the limits in the portal |
| R6 | **Drain rate with thousands of waiters** (about 45 answers/s at 17,000 in the spike). | Measure under M2, cap waiters per share, prefer failfast for interactive shares |

## First steps

1. M0 CI and the kernel test lane.
2. Draft the three contracts: control API, socket protocol, catalog schema.
3. Write the R1/R2 design note, which decides the FSAL's shape before M1 starts.
