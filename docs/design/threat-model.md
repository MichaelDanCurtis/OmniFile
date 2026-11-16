# Threat model (M7)

**Status: first version, 2026-10-16.** `docs/BUILD-PLAN.md` M7 starts with "threat model and security review, the public relay above all". This note records what each boundary protects, how, and what is still open. It is built from three independent reviews of the code (the relay; the service's surfaces and its Azure deployment; the edge's privileged paths), each of which read the code end to end and confirmed the mitigations below at the cited files. Their findings are fixed in commits 87143b3, 80d6e93, 7dc6806, b8dbf58, 4249b5c and ae79144, each with a test that fails without it unless noted.

Keep this file current: a change to a boundary, a credential or a check changes its row here. T11 (cloud connect) and Open items 8 to 10 were added on 2026-11-05, from `cloud-connect.md` section 5, `results/wp5.md` and `results/wp14-a2.md`.

## What is protected

- **The data**, wherever it is: on the edge's xfs volume, in Azure Blob (plain blobs), in S3.
- **Who may read it**: AD RBAC on every path (decision 28). A role never grants read.
- **Its integrity**: a stub never reads as zeros (fail closed), every fill is checked against a whole-file CRC64-NVME, and nothing committed is destroyed on a WORM share (decision 31).
- **The credentials**: edge mTLS keys, the edge CA key, SAS and presigned URLs, enrolment codes, intent signing keys, the break-glass account.

## Who might attack

| Actor | Can | Wants |
|---|---|---|
| Anyone on the internet | Reach the relay, the one public endpoint | Act as a user, crash it, reach the private network |
| A user with an AD account | NFS/SMB to shares, the portal, Teams | Read what their groups can't; keep access after removal |
| A Writer of a share | Rename, create and link inside it | Take over a share or file they don't own |
| An NFS client with `sec=sys` | Claim any uid | Act as another user (`access-control.md` §3: such a share is host-trusted, and says so) |
| A compromised workload in the service (`web`, `worker`, `enrol`, `verifier`, `relay`) | What its identity and network allow | Reach data or credentials beyond its job |
| A compromised service or catalog row | Send the edge anything in its feed | Run code on the edge or on NFS clients |
| A thief of an appliance | The disk, the TPM-bound keys inside | Keep talking to the service |
| An ArchiveAdmin | Every admin route | Read file names without being in the groups (refused, T3) |

## Boundaries, threats and mitigations

### T1. Internet → relay (`relay/`)

The only public endpoint (decision 22). It holds no private key and can't send messages.

- **Bot Framework JWT** (`botauth.py`): only RS256/384/512, narrowed by the metadata; `kid` required; key from the cached JWKS only (never `jku`, `x5u` or `jwk`); `iss`, `aud`, `exp` required, `nbf`/`iat` checked with 300 s skew. The JWKS is fetched only over HTTPS from `login.botframework.com`, with no redirects, a size cap and no proxy from the environment, refreshed every 6 h and early for an unknown `kid` at most every 300 s. Stale keys serve at most 48 h, then it fails closed.
- **Claims bound to the activity**: the `serviceUrl` claim equals the activity's, and the host is on an exact allowlist; the channel is `msteams` and endorsed by the key; the tenant (claim, conversation and channel data) is ours. The relay never calls `serviceUrl`.
- **The intent** (`intent.py`): Ed25519, `alg`, `typ`, `kid`, `iss`, `aud` pinned, lifetime capped. The clicker must be the intent's `sub`, the button's verb its `act`, and the file its `fid`.
- **Replay**: reserved per replica until expiry; the service is the authority (the intent id is consumed once under a row lock). A full store now says it is unavailable rather than "already received" (fixed).
- **Floods**: 64 KiB body, 2 s to read it, JSON content type, deep-nesting guard, `limit_concurrency` 200. Per-address limits for unauthenticated traffic; authenticated traffic is limited per user, for every activity kind (fixed: one user's messages no longer use up everyone's clicks).
- **Logs**: an allowlist of fields; no tokens, object ids, addresses or `serviceUrl`.
- **Container**: non-root, image by digest, nothing written at runtime; its own external environment; its NSG denies Postgres and other private ranges.
- **Accepted**: the activity body isn't signed by Bot Framework, so `from.aadObjectId` rests on TLS from Bot Connector. Inherent to the platform.
- **Ingress is closed by default** (fixed, 2026-10-21: all three lists were empty, which meant open). `relayAllowedCidrs`, `webAllowedCidrs` and `enrolAllowedCidrs` have no default and must each name at least one range, or the deployment fails validation (`deploy/azure/README.md`, "Closed by default"; `deploy/azure/check.sh` tests it). `web` and `enrol` take the users', Prizmanyx's and edges' networks, which leave the relay's subnet out, so a compromised relay can't reach them through the shared internal ingress (they still authenticate every caller). The relay takes the `AzureBotService` ranges: the repo records no copy of Microsoft's ranges; `deploy/azure/relay-ranges.sh` takes them from the published service tag at each deployment (decision 49) and fails if the tag is missing or empty, so nobody types them. `0.0.0.0/0` opens a list on purpose.
- **Open**: that every Teams call comes from the `AzureBotService` ranges (a real tenant).

### T2. Relay → `api`

- mTLS: the relay's certificate is from the OmniFile CA, with SAN `urn:omnifile:relay`, its SHA-256 pinned (mandatory outside dev), and refused on every edge path.
- The service checks everything again: the intent's signature, its row (action, file, recipient, not revoked, unused), fresh AD groups (else 503 and a refresh), and read access. Every refusal and acceptance is audited.
- The relay trusts only the chain `api`'s ingress presents (`relay/README.md`; a real-Azure check).

### T3. Browser and partner apps → `web`

- **Entra tokens** (`web/auth.py`): RS256, key by `kid`; `iss`, `aud`, `exp`, `iat` required; the tenant must match. Partner tokens must be app tokens; roles are filtered to the known ones. A rejected token's reason is logged, not returned (fixed).
- **Sessions**: PKCE S256, a state cookie compared in constant time, a nonce; a fresh session id at every sign-in (only its hash is kept); `__Host-` cookies, `SameSite=Strict`; CSRF token plus exact `Origin` on every unsafe request; no CORS; CSP `frame-ancestors 'none'`.
- **Read (decision 28)**: every item is checked with `acl_can_read` against the user's SID and groups; unreadable is 404. Listings, versions, rewind, recalls, pins, holds, restores and downloads are filtered. A role never grants read: the partner reads that name files (`files`, `restores`, `events`) are for apps only (fixed), and the pin preview counts only what the caller can read (fixed).
- **Groups follow AD**: a session's groups older than 15 minutes are refreshed through the worker, and a newer Graph resolution replaces the token's (fixed; it was up to 10 hours).
- **Admin actions**: provisioning, edges and bundles are for a signed-in ArchiveAdmin only, never an app. WORM, legal holds and edge revocation need a sign-in within 10 minutes. A policy proposal can't be approved by its proposer (anti-pattern 7).
- **The caller's address** is the ingress's last `X-Forwarded-For` hop, believed only from the ingress (fixed: the sign-in limit was one bucket for everyone, and audit rows recorded the ingress).
- **Enrolment codes** are credentials: shown once, kept only as a hash, never replayed from the idempotency store (fixed: the store kept them for 24 h).
- **Registered API clients** (`apiclients/`, migration 0035; `portal-api.md` §2.2, §5.21): a valid app token is accepted only from an application an ArchiveAdmin registered and left enabled, and its roles are cut to the client's scopes. Each client names an owner; a proposal is refused unless the owner can see the dataset (ArchiveAdmin or DataSteward seen at a sign-in within 30 days, or an Owner of one of its shares), and approval checks the client and owner again (fixed after review). Nobody answerable for a client approves its proposals: not its owner, who registered it, or who made the key it came with; the catalog's `proposal_client_guard` refuses it too (fixed after review: an ArchiveAdmin could own a client, mint a key, propose and approve alone). A revoked client's pending proposals show as stale. Disabling or revoking takes effect on the next call.
- **OmniFile API keys** (a new credential type, owner decision 2026-11-04; `apiclients/keys.py`). Entra app credentials stay the recommended path. A key is `ofk1.<16-hex id>.<32 random bytes>`, made by a re-authenticated ArchiveAdmin for a registered client, with scopes within the client's and an expiry (90 days by default, at most 365). Only the secret's SHA-256 is stored, compared in constant time; the key is in the creating response only, never in the idempotency store. It is accepted only as `Authorization: Bearer`; a key in a query string is refused, and every log line, uvicorn's access log included, is redacted. The audit names only the key's id; refusals (unknown, wrong secret, expired, revoked, disabled client) are audited. Rate limits are per key. Rotation keeps the old key for an overlap (7 days by default); revoke is immediate. Threats it adds: a leaked key works until it expires or is revoked (unlike a certificate in the client's own Entra registration, which OmniFile never holds), and it can't be bound to a client's network. Mitigations: short default life, last use (time and address) shown per key, instant revoke, and the owner rule on every proposal. Refused keys are throttled per address before any catalog work, and their audit rows aggregated (one per address, reason and key per minute, with a count), so junk keys can't load the catalog or grow the audit log without bound (fixed after review). A percent-encoded key is redacted too, and uvicorn's access lines lose their query strings (fixed after review).
- **Settings** (`settings/`, `portal-api.md` §5.20) store no secret. Adding or editing storage goes through the admin CLI's checks as a re-authenticated ArchiveAdmin, then the owner-run `backend_register` (T6). Live checks call out only with `OMNIFILE_SETTINGS_PROBES=1`, read-only, as `web`'s own managed identity. Test sends (a card, an email, an AWS role assumption) run in the worker, to the asker only, and record no credential.

### T4. Edge → `api` and `enrol`

- **Identity**: the client certificate's CA signature, validity, non-CA flag and clientAuth EKU are checked; its SHA-256 maps to an active edge, its SAN names that edge, and the path's edge id must match. Every handler is scoped to the caller's edge and datasets.
- **Renewal** needs a proof of possession signed with the current key. Certificates last 30 days.
- **Enrolment**: a 256-bit single-use code with an expiry, kept as SHA-256; failed attempts rate-limited and audited; only an ECDSA P-256 CSR, with the subject and SAN set by the server.
- **Revocation**: `POST /v1/edges/{id}/revoke` (fixed: nothing could revoke an edge). The state is read on every request, so it takes effect at once.
- **Credentials handed out**: user-delegation SAS only, for one blob or version, HTTPS, read 60 min, write (`cw`) 15 min. A SAS can grant no more than its signer's role (T6).
- **S3 credentials (decision 43):** two modes.
  - **Default, IAM Roles Anywhere:** the edge's X.509 key is sealed in its TPM (as its mTLS key is), and `aws_signing_helper` serves temporary credentials to the daemon on loopback only. The Roles Anywhere role and its profile must be restricted to:
    - `s3:GetObject` and `s3:GetObjectVersion` on the registered source buckets and prefixes only, with an explicit deny of every write, delete, tagging, restore and ACL action (decision 40);
    - `aws:SourceVpce` equal to the client's S3 interface endpoint, so the credentials are useless off the private network;
    - a session lifetime of at most 1 h (`DurationSeconds` on the profile), so a copied session ends within the hour;
    - a trust anchor whose CA issues each edge's Roles Anywhere certificate, with the trust policy's certificate conditions naming that edge's subject, so one edge's certificate can't act for another. **Decision 50:** by default that CA is OmniFile's own edge CA (the trust anchor is its certificate; `deploy/aws/README.md`). The edge asks `api` for its certificate over mTLS (`POST …/aws-certificate`, `control-api.md` §2): the CSR's self-signature proves the TPM-sealed AWS key, a proof by the mTLS key proves the request is the edge's, the AWS key must differ from the mTLS key and is pinned at the first certificate, and a revoked edge is refused. `enrol` signs it with its own profile: `OU=omnifile-roles-anywhere, CN=omnifile-edge-<id>`, clientAuth, 30 days, renewed with the mTLS certificate. The trust policy conditions on that OU and CN (and `aws:SourceArn` = the trust anchor): the OU keeps an mTLS certificate, from the same CA, from getting AWS credentials. The client's own CA stays an option (`[aws] issuer = "client"`, `setup-wizard.md` "Keys"); it then revokes certificates itself.
  - **Blast radius of a compromised edge in this mode:** read access to every object in its sources (more than one presigned URL gives), for at most the session lifetime after the edge is revoked. **Revocation path (decision 50):** `POST /v1/edges/{id}/revoke` refuses the edge's mTLS certificate at once (T4 above) and, in the same transaction, queues the edge CA's CRL. The worker gets it from `enrol` (built from the catalog: every unexpired certificate of a revoked edge, or, once all of those have expired, the one last certificate of the edge revoked most recently, since Roles Anywhere refuses a CRL that revokes nothing and the weekly refresh must not stop, and it accepts one whose only entry is a stale serial (both verified on real AWS, 2026-11-14, the second on run rr-202611150718-62e9); signed with the CA key) and uploads it to each trust anchor in the operator's settings (`ImportCrl`, then `UpdateCrl`), with the service's web-identity session, scoped to CRLs tagged with that anchor. Failures retry with backoff, and `roles-anywhere-crl-failing` alerts after 15 minutes. From the upload on, the edge gets **no new session**; a session issued before ends when it expires, **at most 1 h later** (the profile's `DurationSeconds`). So the window is the upload's delay plus at most an hour. The service can't end a live session (no `iam:` rights, by design); an operator who must cut it at once adds a deny on the role for sessions issued before now (`aws:TokenIssueTime`, `deploy/aws/README.md`).
  - **Fallback, presigned GETs:** one object version per URL, minted by the service from its own web-identity session (decision 43), lifetime capped by that session. Used only when Roles Anywhere isn't set up.

### T5. Inside the internal environment — **fixed** (TCP ingress, 2026-11-03)

**The gap:** `api` ran behind the Container Apps HTTP ingress in forwarded mode. It read the edge's certificate from `X-Forwarded-Client-Cert` and believed it from `OMNIFILE_API_TRUSTED_PROXIES`, which defaulted to the whole internal subnet. `web`, `worker`, `enrol` and the verifier also run there. The header carries a certificate, not proof of its key, and certificates aren't secret. So a foothold in any of those apps that could reach an `api` replica directly could act as any edge and mint read SAS for its files.

**The fix (the default, `apiIngress = tcp` in `deploy/azure/main.bicep`):**
- `api` sits behind a **TCP ingress**, which passes the connection through, and terminates mutual TLS itself (`OMNIFILE_API_CLIENT_AUTH=direct_tls`): the handshake requires a client certificate from the edge CA, so a caller must hold an edge's (or the relay's) private key to get in. `DirectTls` refuses any `X-Forwarded-Client-Cert` header: nothing in front sets one, so a caller did.
- `api`'s **server certificate** comes from the edge CA. Each replica makes a P-256 key in memory and asks `enrol` to sign it (`POST /internal/v0/sign`, `purpose: api_server`, with `api`'s managed-identity token, from the internal subnet only). `enrol` alone holds the CA key, in Key Vault. The names (`OMNIFILE_ENROL_API_SERVER_NAMES`, `api`'s FQDN) are `enrol`'s, never the CSR's. The certificate has EKU serverAuth only, and edge certificates clientAuth only, so neither can stand in for the other. It lasts 30 days, and `api` renews it in place from day 20. The key never touches a disk (a memfd).
- **Edges pin it.** The enrolment answer carries `api_server_ca_sha256`, the edge CA's SHA-256, over the channel the enrolment bundle pinned (`enrol`'s ingress CA). The edge refuses an answer whose pin names no CA it sent, stores the edge CA as the only root for `api` (`server-ca.pem`), and checks at every start that the file is still the pinned CA (`omnifile-control`, `identity.rs`). The relay's CA bundle for `api` is the edge CA too.
- **No forwarded mode in the template** (review of #34). `api` always runs in the shared internal environment, whose ingress is in the subnet every app runs in, so no proxy range would have been safe; `apiIngress` allows only `tcp`. The code keeps forwarded mode for other hosting and fails closed: it needs the proxy's range, refuses one wider than /16 (/48 for IPv6; `0.0.0.0/1,128.0.0.0/1` was every address without a /0), believes the header only when the TCP peer is in it, and refuses to start when the range overlaps `OMNIFILE_API_APP_SUBNETS` (the subnets the other apps run in, which the template passes).

**Proof** (`service/tests/api/test_t5.py`):
- the deployed default, evaluated from the compiled Bicep, builds a direct-TLS source, and a forged header from inside the subnet authenticates nobody; against origin/main's template the same header authenticated as the victim edge (run recorded in `docs/milestones/results/wp1.md`);
- through a real TCP passthrough to `api`'s own serving code, with its certificate from `enrol`: the victim edge gets in; another edge's key with the victim's certificate in the header gets `401 certificate_invalid` (as itself too); no client certificate fails the handshake;
- forwarded mode with no proxy range doesn't start, and with one believes only that peer;
- the edge's side: `edge/crates/omnifile-control/tests/pin_api_server.rs`.

**Left for a real deployment:** that the TCP ingress and its NSG rule (`apiTcpPort`, 8443) reach `api` from the edge networks and the relay environment, and that ingress IP restrictions apply to TCP (`real-azure-checks.md`, "Direct replica access" becomes: a replica in the subnet that sends a forged header to `api` is refused).

### T6. The service → storage (Azure RBAC; `access-control.md` §7)

Blob access bypasses AD RBAC, so the data plane is locked down:

- Storage accounts: shared keys off, OAuth by default, no anonymous access, public network off with `bypass: None`, TLS 1.2, copies only over Private Link, no cross-tenant replication.
- Roles: `api` has the custom **SAS Minter** role (blob read and write and user-delegation keys; no delete of any kind; fixed: it held Data Contributor on every account, WORM included). The worker's custom role has no immutability override, version delete, permanent delete or container delete, and now has user-delegation keys for download links (fixed: downloads would have failed). The verifier reads only. `web` and the relay have no storage role. The break-glass group defaults to Reader (fixed).
- Key Vault: RBAC, purge protection, public access off. `enrol` alone can sign with the CA key, which never leaves the vault. The relay reads its three secrets; `api` and `web` read only their own shared key (fixed: they were plain environment variables).
- PostgreSQL: Entra authentication only, no public access, `verify-full`.
- **The CA key** never leaves Key Vault: `enrol` signs through the vault's `sign` operation (`enrol/kvsigner.py`), checks at start that the vault key is the CA certificate's, and verifies every certificate against the CA before handing it out. A CA key in a file is refused outside dev mode (fixed: `enrol` could only sign with a file).
- **Database privileges per app** (fixed, was open: every app's role could write every table). `migrate` ends every run by granting group roles `omnifile_api`, `omnifile_enrol`, `omnifile_web` and `omnifile_worker` exactly what each app uses (`catalog/privileges.py`), down to columns where an app changes only a few (web's legal holds and retention extensions on versions). The administrator owns every table, so no app can change the schema, truncate, or turn a trigger off. Catalog triggers run as their owner, and no role may EXECUTE them or (the worker aside) make temporary tables to attach them to (fixed after review: otherwise any app could run one as the owner on a table of its own); so only the triggers write the feed, which edges act on (the api adds its conflict events through an owner-run function that writes nothing else); the audit log takes inserts only. Partition upkeep and the seeder's ANALYZE go through owner-run functions only the worker may call. Every service test runs each app's connections under its role.
- **Storage backends** (migration 0035, review of #43): no app may INSERT or UPDATE `catalog.backend`. The portal's Settings, Storage writes only through the owner-run `backend_register` (EXECUTE for `web` alone, fixed `search_path`): a signed-in person's change, always private, found by name, audited. The catalog's `backend_guard` holds the CLI's rules for every writer: an app's session never writes a public row (its group role set, or a login with membership in one, so `SET ROLE NONE` doesn't evade it; fixed after re-review), private networking never turns off, a kind never changes, and once a dataset, source or migration uses a row no key changes but an allow-list shared with the CLI (S3 buckets added, an inventory rule, a display name, an Azure row's region; fixed after re-review: versioning or the archive class could change under data). `web`'s Azure role is the read-only "OmniFile Settings Reader" (no data actions, no keys) for the Settings checks.

### T7. Catalog and service → edge

The edge validates everything it renders or applies, and never trusts the service alone, with one exception noted below.

- **Shares rendered into smbd and Ganesha** (`omnifile_acl::ShareConfig::validate`): names `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`, not `global`, `homes` or `printers`; paths absolute with no control characters, `"`, `\`, `;`, `%` or a trailing space (fixed: `%` is a Samba substitution and smbd trims spaces); clients from a small alphabet; a host-trusted share needs named clients no wider than /8 or /32 (fixed) and root squash; SIDs digits and dashes. Ganesha's lexer has no other escape. The SMB stanza is a fixed template. The service checks the same rules first.
- **Modes**: a file never gets set-uid or set-gid from the catalog (fixed: a root-owned 04755 row would have put a root executable on NFS clients mounting without `nosuid`).
- **Paths**: feed names refuse empty, `.`, `..`, `/` and NUL. Every daemon operation resolves with `RESOLVE_BENEATH|NO_SYMLINKS|NO_MAGICLINKS|NO_XDEV` and acts through `*at` calls, `O_NOFOLLOW` and fds.
- **Fetches**: HTTPS only (HTTP only for the sim), no redirects. Under private networking, names resolve to private addresses only and public or loopback literals are refused (fixed: loopback reached the appliance's own services).
- **Fills**: no fill without a whole-file CRC; S3 bytes are exposed only after it matches.
- **The exception**: the edge renders shares with `restricted = false`. Whether a host-trusted share covers restricted folders is the service's to judge. (Fixed, decision 39: it was checked only when the share was made or changed, so a folder an Owner restricted later went unnoticed. The share's checks and the list of shares now read the ACLs as they are now, and the gauge `omnifile.shares.host_trusted_restricted` drives the `share-host-trusted-restricted` alert, until an ArchiveAdmin switches the share to Kerberos or records an override. A recorded override now silences the check, as the rule always said.)
  - **Why not enforce it on the edge:** the edge could tell from its replica's ACLs, but refusing to serve the share would let any user who may tighten one folder's ACL take a whole `sec=sys` share offline for everyone.
  - **The owner chose a warning** (decision 39, 2026-10-16), built as above.

- **Annex placements (phase A3.3; `wp14-a3.md`, "What A3 writes to a NAS").** The placement mount is the most powerful handle the edge holds. Over SMB it is mounted with the restore-rights account's ticket (`cruid`) and backup intent (`backupuid`) for the daemon's own opens. So that account's `SeRestorePrivilege` lets those opens bypass the NAS's ACLs, and it can set another owner. The mitigations:
  - **Scope.** Backup intent is only on the placement mount, never on the read-only or removal mounts. The mount exists only while a placement has work due. One that is only reporting, stuck, paused or backing off doesn't hold it.
  - **One module.** Only `annex/placer.rs` holds a handle to it.
  - **Tickets and journal.** Every write is ticketed, journalled and audited. Before the link that names the file, the service is asked again (`go`), with:
    - the folder's ACL as it is now (the requester must still be able to add a file there);
    - the staged ACL as it will be with the file's own owner and group, whose read set must be within the file's catalog read set (decision 51).
  - **The staging folder.** One per annex, with a random token in its name, journalled before the `mkdir`. Its protected DACL names only the placement and read accounts. It is never adopted: only a folder whose successful `mkdir` the journal recorded is used. It is checked on the same fd the creates use, and each file in it gets its own lock, read back, before a byte is written. The scan and the reconciliation skip it at the top of the export.
  - **The owner step.** P5 sets an owner, restores setuid or writes a descriptor only on an fd that is still the inode, size and mtime `secured` saw. A file a user changed or swapped after the link keeps what it has, and never gets another owner's setuid. Over SMB the kernel's cifs client applies a descriptor by path, not by the fd, so a very short window remains between the check and that write. A user who can replace that file and wins the race gets the original's owner and DACL on their replacement, through the restore right, which that user otherwise lacks: that can make their file look like its owner's, and lets its readers read it. They gain no read of the original's bytes, and nothing of this needs more than replacing a file they already may.
  - **What `go` proves.** The staged group is taken literally, and the NFS edge compares the real gid first. An NT owner is swapped for the file's only when it is the filer's registered placement SID, set by an ArchiveAdmin with a fresh sign-in, and only to what that filer's own edge currently reports. A lying edge gains nothing from that report. Its only use is at `go` on that edge's own filers (`fl.edge_id` is checked on every placement step), where the same edge already supplies the staged ACL the service judges, so it could send any ACL without lying about the account; and it already holds the restore right on that NAS, so it can write any descriptor there. It can't report for another edge's filer, because the edge comes from its client certificate, and no route moves a filer to another edge. The report writes nothing else: not `catalog.identity`, and not `placement_sid` itself. What it could do is put a wrong SID in front of the ArchiveAdmin, which only harms that edge's own filer. `WRITE_DAC` and `WRITE_OWNER` are refused for anyone not already holding them or reading the file, and always for a principal with no SID. Whatever can't be proved is a mismatch (decision 51).
  - **Ending a placement.** An ArchiveAdmin may abandon one stuck after its `go`, after 24 hours with no report. Ended placements refuse every later step, so the worst case is a recopy.
  - **Accounts.** A restore or remove account has a ticket cache of its own, gone with the mount. In root's cache it would become what the read-only mount reconnects as, so that is refused. A Copy annex never gets the remove-rights account.

### T8. NFS and SMB users → the edge

- **Stubs fail closed** (`CLAUDE.md`, "Critical constraints"): the fanotify group outlives the daemon, events are journalled, and smbd and Ganesha are bound to the guard. The FSAL fails closed and never blocks Ganesha on the daemon.
- **Markers** are one `trusted.omnifile` xattr. NFSv4.2 xattr ops aren't wired in `FSAL_OMNI` (and would map to `user.*`); 9P is off.
- **Fills land in the right file**: keyed by file, version and placeholder inode; an FSAL fence; `still_ours` checked before every write.
- **Share roots** (fixed, was blocking): a share is served only while its path is the directory the edge made (inode and generation, no symlink), checked every 5 s. A Writer of a parent share who renames a shared folder away and puts their own directory or a symlink in its place gets a share that stops being served, and their directory gets neither the Owners group nor the ACL: every ACL write is pinned to the edge's inode.
- **Attribute changes** from the feed are checked and applied through one fd on the object (fixed: a hard link swapped in between could take another file's owner and mode).
- **Rewind trees**: root-owned 0555, set-id stripped, read-only through the FSAL (`ROFS`) and `shadow_copy2`.
- **The FSAL socket**: 0600, peers checked by uid and `/proc/<pid>/exe`, record lengths checked both ways.
- **Renames, deletes and owner changes reach the catalog** (fixed, was open): a fanotify watcher in directory-FID mode marks changed folders, a pass against the replica turns them into `rename`, `unlink` and `setattr` ops, and a deleted file is tombstoned so a re-snapshot can't bring it back (`namespace-sync.md`).

### T9. Administering the appliance

- Shell and sudo for an AD group only; SSH limited by `AllowGroups`.
- The setup wizard runs commands as argv with no shell, passes the join password in the environment and the recovery password on stdin, and checks sudoers and sshd files before keeping them.
- The break-glass account is console only, locked until sealed, and every session is logged at `auth.alert`, reported by the heartbeat, and shown in the portal until sealed again. An unattended hash must be exactly one crypt(3) hash (fixed: a second line would have set root's password).

### T10. Keys at rest

- The edge's mTLS key and any pending CSR key are sealed with `systemd-creds` (TPM 2.0 plus the host key), name-bound, never in the clear; no PCRs, so firmware updates don't lock it out. A stolen disk without its TPM yields nothing; a stolen appliance is revoked (T4).
- Temporary files are `O_EXCL`, random, with their mode set at creation.

### T11. The portal → the client's Azure and AWS (cloud connect, #64 and #65; `cloud-connect.md`)

Settings, Azure and Settings, AWS make the service's grants and roles from the portal. The review of #64 found no escalation; its fixes are below, each with a test (`results/wp19.md`).

- **Delegated ARM sign-in** (T-CC1 to T-CC3, T-CC5). Auth code with PKCE through `web`, on `/auth/azure/callback`, scope `user_impersonation`.
  - The state is in a `__Host-` cookie and in the operation's row. The ID token's nonce and tenant are checked, and its `oid` must be the admin who started the operation, so a link someone else made fails at the cookie, and another tenant's token is refused.
  - No stored token: the access token lives for that one request, no refresh token is asked for, nothing is cached, and the PKCE verifier is deleted when the code is redeemed. A database leak gives no Azure access.
  - The scope carries all of the admin's Azure rights, so the callback makes only the calls of the plan stored when the operation began: GETs while listing, and in a grant only PUTs of the listed roles and assignments. A test checks the exact calls.
  - A grant or a removal needs a second, fresh sign-in (`max_age=600`), and the callback first reads that the admin holds `roleDefinitions/write` and `roleAssignments/write` on every scope. So a stolen ArchiveAdmin session can grant these fixed roles only where the admin's own Azure account may already assign roles.
- **Custom roles** (T-CC4). Two per resource group: the Discovery Reader for `web` (reads of storage accounts, networks, private endpoints and their DNS zone groups, DNS zones and their links, role assignments and definitions, and Storage Mover) and the Mover Deployer for the worker (Storage Mover writes in one resource group, and the migration engine's job actions). No `dataActions`, no keys, no deletes and no `Microsoft.Authorization/*/write`, so a compromised `web` or worker can't widen its own access.
  - A lost answer in the middle of a grant is still recorded and audited (`uncertain`), so nothing made in Azure is left unremovable (fixed after review).
  - Discovery calls an account private only when public access is closed, it has an approved blob private endpoint, and that endpoint registers in a `privatelink.blob` zone linked to the service's VNet (fixed after review, then per account in #65). Only those can be confirmed for use (decision 22).
- **The AWS connect template's trust** (T-CC6; `deploy/aws/cloudformation/omnifile-connect.yaml`). No keys (decision 43); the admin's AWS credentials stay in their own browser.
  - The service role trusts only `sts:AssumeRoleWithWebIdentity` from the tenant's OIDC provider, for one audience and the listed subjects. A test checks it equals the trust policy the page shows.
  - It denies `s3:Put*`, `s3:Delete*`, `s3:Replicate*`, `s3:Bypass*`, `s3:AbortMultipartUpload` and `s3:ObjectOwnerOverrideToBucketOwner` everywhere but the ops bucket (`NotResource`). So a write through an access point is denied too, and the deny wins over any policy added to the role later (decision 40; fixed after review: it was a list scoped to bucket ARNs). `s3:RestoreObject` is allowed.
  - The Batch role trusts S3 Batch Operations for this account only (`aws:SourceAccount`). The interface endpoints have least-privilege policies and a security group open to the service's VNet range only. The ops bucket is TLS-only and aborts incomplete multipart uploads after a day (#65).
  - The fetch uses the admin's account id, region and endpoints once, checked as an operator setting, for reads only. The worst a forged fetch can do is send the worker's web-identity token to an AWS STS endpoint, which honours it only for a role that trusts this tenant, audience and subject.
  - The confirmed entry is not where the S3 backend reads: an operator sets `OMNIFILE_AWS_ACCOUNTS` (the M5 review's B1), so a stolen session can't point the backend at another account.
- **The scan** (T-CC7; `POST /v1/source-scans`). ArchiveAdmins only, under the backend's role, through the seed's readers, which only list and read the inventory's own bucket; a test checks that every call was a list or a get (decision 40). Capped at 2,000,000 objects and 300 s by default, and at most 10,000,000 and 1,800 s. Its results hold key prefixes, which ArchiveAdmins already see on the migration pages, and are kept 7 days.
- **Open:** the VERIFY lines RA-43 to RA-52 and RW-33 to RW-40 (consent, the permissions answer, role replication, the API versions, the template in S3 and the quick-create link, and the deny against an added full-access policy).

## Fixed in this review

| Finding | Severity | Commit |
|---|---|---|
| A Writer could take over a folder share's root (rename and replace, or a symlink) | blocking | 80d6e93 |
| Enrolment codes kept in the clear in the idempotency store | should-fix | 87143b3 |
| ArchiveAdmin could list every file name through partner reads | should-fix | 87143b3 |
| `api` could delete containers (Data Contributor everywhere) | should-fix | 87143b3 |
| The worker couldn't mint download links on real Azure | should-fix | 87143b3 |
| No way to revoke an edge | should-fix | b8dbf58 |
| `web` saw the ingress, not the caller (global sign-in limit, audit addresses) | should-fix | 7dc6806 |
| Portal groups frozen for up to 10 hours | should-fix | 7dc6806 |
| Set-uid files from the catalog | should-fix | 80d6e93 |
| Materialiser attribute race (hard link swap) | should-fix | 80d6e93 |
| Pin preview counted unreadable files; token errors returned; host-trusted /1 networks; `%` and trailing spaces in share paths; recovery hash lines; loopback under private networking; break-glass default role; keys as plain env; relay per-user limits, replay saturation, tenant, CA bundle | nits | 80d6e93, 7dc6806, 4249b5c, ae79144, 87143b3 |

## Open

1. ~~**T5**, direct replica access with a forwarded client certificate~~: fixed by TCP ingress and direct TLS by default; the network checks are in `real-azure-checks.md`.
1a. **Edge CA rotation** (review of #34): there is no overlap in which an old and a new edge CA both work, so rotating it, after a suspected compromise of the CA key or before the CA certificate's ten years end, means re-enrolling every edge. The procedure is in `control-api.md` §2 ("Known limitation"). The key never leaves the HSM, which makes a forced rotation unlikely; building an overlap (api trusting two CAs, edges pinning both) is left for later.
2. ~~The Key Vault CA signer for `enrol`~~: built (`enrol/kvsigner.py`); the real-vault check is in `real-azure-checks.md`.
3. ~~Per-app database grants~~ (T6): built (`catalog/privileges.py`); the check on Azure's PostgreSQL is in `real-azure-checks.md`.
4. ~~Edge reports of renames, deletes and attribute changes~~ (T8): built (`namespace-sync.md`), checked end to end by step 10 of the M5 test.
5. **Relay exposure** (T1): the Bot Service ranges on its ingress need confirming on a real tenant. The three ingress allow-lists are required, so a deployment is closed until they are set; the relay's comes from the `AzureBotService` service tag at deploy time (`deploy/azure/relay-ranges.sh`, decision 49; `deploy/azure/README.md`, "Closed by default").
6. ~~**Restricted folders** checked only when a share is made or changed (T7)~~: a warning in the portal and an alert (decision 39).
7. Everything only a real deployment can confirm: `docs/milestones/real-azure-checks.md` and `real-tenant-checks.md`.
8. **The real-cloud harness's az guard is a safety net, not a boundary** (`tests/real/harness/azguard.py`, `results/wp5.md`). It refuses every deploy but whatif.sh's own create under a one-shot grant for the approved digest. A guide step runs as the harness's own user, so it could reach the real az or ARM if it set out to. The allow-list, the approval per gate and the guide's lint stay the controls.
9. **The AWS harness policy's Roles Anywhere tagging trade-off** (`tests/real/aws/README.md`, #56). A create that carries tags is also checked as `rolesanywhere:TagResource`, and IAM can't tell that from tagging an existing trust anchor, profile or CRL. So the harness's user could tag an owner's Roles Anywhere resource and then read or delete it, or replace its CRL (which could un-revoke a certificate), though never reach the owner's roles. `ImportCrl` can also attach a CRL to an owner's anchor. The README says to attach the policy only in an account with no Roles Anywhere resources of its own, and gives the simulator commands that show it. This is test tooling: no customer gets this policy.
10. **Annex removals and ctime** (#61, `results/wp14-a2.md`). W2 relies on the ctime that W1's rename left, journalled right after W1, to catch a same-size rewrite with its mtime put back. A POSIX client can't set a ctime. An SMB client can set ChangeTime, so over SMB the edge also re-reads the hidden file's CRC-64/NVME before W2. This ran only against the lab's servers, the SMB one Samba. How a real NAS (ONTAP, PowerScale) moves ctime on a rename, and whether it lets SMB clients set it, is to confirm on site.
