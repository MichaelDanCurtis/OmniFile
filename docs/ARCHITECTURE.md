# OmniFile Gateway — architecture

`AGENTS.md` holds the summary and the closed decisions. This file is the working reference for building. Everything here is a **design proposal**, pending the pilots in `docs/PILOTS.md`.

## 1. Components

### Edge filer (client data centre, Linux, kernel ≥ 6.14, xfs — Ubuntu 26.04 LTS minimized, kernel 7.0; decision 26)

| Part | Language | Job |
|---|---|---|
| NFS-Ganesha + `FSAL_OMNI` | C (LGPL-3) | Serves NFSv3 and NFSv4.x. Resolves every path through the catalog to a backing location: local landing zone, local cache, or a remote backend. On a read of data that is not local, it answers per share policy: `NFS4ERR_DELAY` / `NFS3ERR_JUKEBOX` (wait) or `EIO` (fail fast). It reports `{uid, client IP, path, offset, sec flavour}` to the daemon over a Unix socket. |
| Samba | (GPL-3, separate process) | Serves SMB to Windows users, joined to AD with winbind. Its reads hit the local filesystem. |
| `omnifile-edge` daemon | Rust | See the module list below. |
| winbind | (GPL, system service) | The single AD identity source for NFS and SMB. Use `idmap_ad` when AD carries POSIX attributes, otherwise `idmap_rid`. |

`omnifile-edge` modules:
- **detector:** fanotify `FAN_CLOSE_WRITE` on the landing filesystem, then the share's quiet period (decision 67) and an optional SEG-Y structure check.
- **hasher:** streaming BLAKE3 for whole-file dedupe, plus per-block checksums for upload integrity.
- **uploader:** parallel blocks sized at least max(64 MiB, size ÷ 50,000), with 64–300 streams. It keeps a resumable journal. It **never sets an explicit tier** on the Smart live account, or the blob never enters Smart (decision 26 verification).
- **stubber:** punches holes, keeps the first N MB resident, and marks the file as a stub. Stubs **must fail closed**.
- **smb-guard:** fanotify `FAN_PRE_ACCESS` for SMB and local reads of stubs. It is the fallback path; NFS reads never reach it because the FSAL handles them.
- **cache:** an SSD chunk cache for range reads from remote backends. Needed for the migration namespace and for files already stubbed.
- **recall-fill:** writes returned data back into place and removes the stub marker.
- **control client:** mTLS to the service. Fetches per-object upload and download credentials. Holds no long-lived cloud secrets.

### OmniFile service (client's Azure, Python 3.12+)

- **API:** edge control API (mTLS, private IP) in `api`; the portal and partner JSON API (`docs/contracts/portal-api.md`) in `web`.
- **Catalog:** Postgres. Stores datasets, files, locations (backend + object key + class), hashes and duplicate references, holds, recalls, policies, shares, an identity cache, and notifications.
- **Policy engine and tiering scheduler:** evaluates policies against the catalog. Issues tier changes, server-side archive copies (Azure Smart → archive account), and stub instructions to the edge.
- **Recall orchestrator:** starts and tracks restores per backend, respects provider limits, watches restored-copy expiry (S3), and signals the edge to fill files back in.
- **Backends:** Azure Blob and Amazon S3, behind one contract (section 3).
- **Notifier** (in the worker; `docs/contracts/teams.md`): resolves the requester's SID to an Entra user and groups through Graph, installs the Teams app for them, sends the Adaptive Card through the Bot Connector and updates it in place as the recall moves. Every button carries an Ed25519-signed intent; only the worker holds that key. Email through Graph `sendMail` when Teams is blocked or keeps failing.
- **Teams relay:** the only public endpoint. Validates the Bot Framework JWT, the tenant, the signed intent and the clicker, then forwards over mutual TLS (a pinned relay certificate) to `api` `POST /internal/v1/teams/intents`. `api` re-checks the intent, consumes it once, checks the clicker can still read the file, acts in one transaction and answers with a buttonless card; the worker sends the full card about 6 s later.
- **Portal** (decision 32): a React + TypeScript single-page app (`portal/`), built into the `web` image and served by `web` as static files under a strict CSP. `web` is a backend-for-frontend: Entra OIDC (auth code + PKCE, a client assertion from its managed identity, no secret), server-side sessions in the catalog, CSRF header plus Origin check. App roles `ArchiveAdmin`, `DataSteward` and `User`, assigned to AD groups; file access always by AD group SIDs (decision 28), never by role. Partners call the same API with Entra app tokens. Dev and tests use a dev OIDC issuer mounted in `web` (dev mode only).
- **Queues: PGMQ in the catalog database** (decision 27). Work is enqueued in the same transaction as the catalog change that caused it. Queued work includes restores, Storage Mover jobs, verification, notifications and edge instructions.

**Hosting** (decision 17): one deployment per client, in the client's subscription. No VMs.
- **Azure Container Apps environment,** VNet-integrated and internal:
  - `api`: the **edge control API** and the relay's inbound intents. Ingress uses `clientCertificateMode: require`, so every caller presents a client certificate, and the app validates the `X-Forwarded-Client-Cert` chain against the OmniFile CA. The CA key is non-exportable in Key Vault.
  - `enrol`: **edge enrolment only** (enrolment token → CSR → client certificate), internal-only and without client certificates, because a new edge has none yet. It does nothing else.
  - `web`: **portal and partner API**, with Entra sign-in and no client certificates. It is a separate app because Container Apps applies `clientCertificateMode` to every caller of an app. Its image (`service/Containerfile.web`) builds `portal/` in a Node stage and serves `portal/dist` from the Python runtime; no Node at runtime. `web` has no storage role: downloads and version restores are catalog requests the worker carries out.
  - `worker`: scheduler, recall orchestrator, change-feed and SQS readers, Storage Mover orchestration.
  - verifier as **Container Apps jobs**, scaling out for bulk CRC64 checks.
  - The **Teams relay** runs in a **second, small external Container Apps environment**, in its own subnet of the same VNet. An environment's internal or external accessibility is fixed at creation, so one environment can't hold both. The relay reaches `api` over the VNet through a private DNS zone.
- **Azure Database for PostgreSQL Flexible Server,** private access.

## 2. Paths

- **Data:** edge → ExpressRoute/VPN → private endpoint (Azure) or PrivateLink interface endpoint (S3) → object store. Optionally, edge → relay VM (FastPath) → private endpoint, only if a pilot shows the ExpressRoute gateway is the bottleneck.
- **Control:** edge ⇄ service over private IP with mutual TLS.
- **Outbound from the service:** Teams, Graph and Entra (controlled egress).
- **Inbound from the internet:** the Teams relay only.
- **No push events from the cloud.** Event Grid cannot deliver into private endpoints. The service is the only writer, so it polls restores.

## 3. Backend contract

```
put(key, stream, class, metadata) -> ObjectRef        # multipart / block upload, tier set at write
get_range(ref, offset, len) -> bytes
head(ref) -> {size, class, restore_state, checksum?}
list(prefix, cursor) -> page                            # for reconcile only; listings come from the catalog
set_class(ref, class)                                   # non-WORM: Azure Smart → archive account copy; WORM: in-place Set Blob Tier
restore(ref, speed, days?) -> RestoreHandle             # Azure: Copy Blob rehydrate; S3: RestoreObject
restore_status(handle) -> {state, eta?, expires_at?}
delete(ref)                                             # with versioning: current version becomes previous; blocked under retention
list_versions(key) -> [VersionRef]                      # decision 30
set_retention(ref@version, until) / set_legal_hold(ref@version, on)   # decision 31
capabilities() -> {logical tier map, minimum durations, restore speeds and SLAs,
                   throughput caps, private-network options, credential mode}
```

The policy editor reads `capabilities()`, so warnings about minimum durations and recall times are always stated in the chosen provider's terms.

## 4. The "file is archived" flow

1. A read reaches data that is not local.
   - Over NFS, the FSAL sees `{uid, client IP, sec flavour}`.
   - Over SMB, the smb-guard sees the event. The event pid maps to the smbd session user, looked up while the request is pending (filer spike A12).
2. The daemon asks the service. If the file is archived, a recall is started or joined; if it is online in the cloud, the read is served by range get.
3. Per share policy:
   - **wait:** the FSAL returns DELAY/JUKEBOX until the data is local.
   - **fail fast:** the FSAL returns `EIO` at once.
   - **wait if quick:** decided by the backend's ETA against a threshold.
4. A `<file>.RECALLING.txt` note appears next to the file, giving the ETA and a portal link.
5. The notifier resolves the requester:
   - uid → SID via winbind;
   - SID → Entra objectId via Graph `onPremisesSecurityIdentifier`, cached;
   - then sends a Teams card.

   `sec=sys` identities are marked *claimed*; Kerberos identities are *proven*.
6. The card updates through requested → rehydrating → downloading → available. Email is the fallback if Teams returns 403.

## 5. Policy model (what the editor edits)

Also per share: **tier pins** set by hand (decision 29), **`rewind_depth_days`** (decision 30), **WORM** with retention days and lock state (decision 31), and the **quiet period** before a file counts as finished (decision 67). See `docs/design/tiers-versioning-worm.md`.

- **Which files:** folders (globs), file types, owning group, minimum size. Advanced: excludes, a legal-hold tag, SEG-Y validation, and duplicate handling (reference or copy).
- **When files move:** `mode = scheduled | smart`, and `clock = last_read | written`.
  - Scheduled: optional warm day, optional cold day, and a required archive day.
  - Smart: a required archive day.
  - Stubbing: `stub_at = archive | leaves_hot | never`, with `resident_mb`.
- **What users see:** `on_open = wait | fail | auto(threshold_minutes)`, per share.
- **Who is told:** the requester (Teams), the owner, a watchers group, and email fallback.
- **Security, per share:** `sec = sys | krb5 | krb5i | krb5p`. The UI shows the throughput note: Microsoft measured about −53% IOPS for krb5 and about −77% for krb5p on NFS.
- **Live feedback in the editor:**
  - a timeline;
  - first-year cost per TB, compared with keeping data hot;
  - checks: minimum-duration fees, ordering errors, recall-time warnings, smart-tier caveats, and "nobody is told".

Built in M4 (`portal/src/features/policies/`, `POST /v1/policies/preview`):
- **The editor asks four plain questions** (which files, when they move, what users see, who is told), with every knob under "More settings".
- **"Which files" is the whole dataset in v1.** The M3 engine applies one policy per dataset; folder, type, group and size selectors need engine work and come later (decision 12). The editor says so.
- **The numbers stay on the server.** The editor posts the draft body 250 ms after the last edit; `policy/preview.py`, `cost.py` and `checks.py` (pure functions over the backend's `capabilities()` and a dated price table, every price marked `verify` until checked) return the timeline, the first-year cost per TB against keeping it hot, the recall cost per TB at each speed, and the checks. An error check blocks saving.
- **Versions and approval.** Saving creates a new active version (`base_version` guards concurrent edits) and re-plans the dataset. A proposal (Prizmanyx, any AI, a DataSteward) is stored with its preview and changes nothing until an ArchiveAdmin other than the proposer approves it (anti-pattern 7).
- **"What users see"** is per share (`on_open`), saved through the share settings or applied with an approved proposal's `share_overrides`.
- **"Who is told"** is the policy body's `notify` object: requester (default on), the share's Owners, a watchers group, and email fallback.

A working mockup of this editor is in the research page (see `AGENTS.md` → Provenance).

## 6. Partner API (for Prizmanyx)

Auth: an Entra app registration exposing app roles, called with client credentials or workload identity federation (no secrets). Every request is checked for `tid`, `aud` and `roles`.

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /v1/adopt` | `Archive.Adopt` | Register objects Prizmanyx already moved, keeping their original paths |
| `POST /v1/sources` | `Archive.Adopt` | Register an S3 bucket/prefix and its inventory source (migration namespace) |
| `POST /v1/locations` | `Archive.Adopt` | Flip verified prefixes to their new backend |
| `GET /v1/files?path=` | `Archive.Read` | Catalog rows: tier, archive date, hash, duplicate references, holds |
| `GET /v1/usage` | `Archive.Read` | Bytes and cost by tier, share and dataset |
| `POST /v1/policies/proposals` | `Policy.Propose` | A proposal only; an Archive Admin approves it in the portal |
| `POST /v1/recalls` | `Recall.Request` | Recall paths or datasets by a due date; returns ETA and cost |
| `GET /v1/restores` | `Archive.Read` | Restores users triggered, so Prizmanyx can fold them into its restore plan |
| `GET /v1/events?after=` | `Archive.Read` | Ordered pull feed (works over private networking) |

## 7. Speed levers, ranked

1. **Data Box** (or the provider's equivalent) for the historical estate.
2. **Network path.** ExpressRoute gateway SKU (1/2/10 Gbps), and private-endpoint throughput "might be reduced by half". FastPath for Private Link is available on ExpressRoute Direct only.
3. **Parallel streams and TCP tuning.**
4. **Whole-file dedupe.**
5. **Optional relay mode**, which also enables wire compression. Expect about 1.1–1.5× for float SEG-Y; this is unverified for seismic data.

Chunk-level dedupe is not planned; expected gain is about zero.
