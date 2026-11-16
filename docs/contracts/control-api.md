# Edge ⇄ service control API, v0

**Status: DRAFT for M0 review (2026-10-05).** The machine-readable form is `service/openapi/control-api.yaml` (OpenAPI 3.1); when the two disagree, fix both. The catalog it reads and writes is in `service/src/omnifile_service/catalog/migrations/`. The edge's local replica of the catalog is designed in `docs/design/edge-metadata.md` (m0-design); section 12 lists what this contract assumes about it.

This is the private API between the edge daemon (`omnifile-edge`) and the OmniFile service. The partner API for Prizmanyx (`ARCHITECTURE.md` §6) and the portal API are separate.

## 1. Shape

- **Transport:** HTTPS over the private network (ExpressRoute or VPN), with mutual TLS. No call crosses the public internet.
- **Base URL:** `https://<service>/v0/edges/{edge_id}/…`. The `edge_id` in the path must match the client certificate (section 2), or the call fails with `403 edge_mismatch`.
- **Bodies:** JSON (`application/json`), UTF-8. Errors use `application/problem+json` (section 9).
- **Identifiers** (`file_id`, `version_id`, `acl_id`, `recall_id`, …) are 64-bit integers sent as **decimal strings**, so no client loses precision. They are never reused.
- **Times** are RFC 3339 UTC. File times are integers in nanoseconds since the epoch (`mtime_ns`), because NFS needs more than microseconds.
- **Hashes** are lowercase hex: BLAKE3 (64 hex digits) and CRC64-NVME (16 hex digits, the big-endian value).
- **Every mutating call carries `Idempotency-Key`** (section 8).
- **`version_id`** in this API always means the file's **content version**: the version holding its bytes. A rename or attribute change makes a new catalog version but keeps the content version, so it never invalidates stubs, credentials, recalls or the edge's `trusted.omnifile` marker.
- **One edge per dataset in v1.** Every share of a dataset is on the same edge (a catalog constraint), and a file's local state (`state`, `resident_bytes`, stub) is that edge's. Several edges serving one dataset needs per-(edge, file) state; that is a v2 item.

## 2. Enrolment and mutual TLS

### Who trusts whom

- The service runs an **OmniFile CA**. Its key lives in Azure Key Vault (Premium, HSM), reached through the vault's private endpoint, and is used only through Key Vault `sign`. The CA certificate is the trust anchor for edge certificates.
- **Only the `enrol` app signs.** Its managed identity is the only one with `sign` on the CA key. The `api` app never touches the key: for renewals it authenticates the edge and forwards the CSR to `enrol`'s internal signing endpoint (below).
- `api`'s TLS server certificate is issued **by the edge CA** (threat model T5). `api` sits behind a TCP ingress and terminates mutual TLS itself; each replica makes a key in memory and has `enrol` sign its certificate (`purpose: api_server`, below) for `api`'s private DNS name. `enrol` presents the ingress's certificate, whose CA the enrolment bundle pins. The edge learns which CA to pin for `api` from the enrolment answer (`api_server_ca_sha256`), so it never relies on public CAs over the private path.
- Behind an HTTP ingress (forwarded mode, which `deploy/azure/main.bicep` no longer offers, T5), `api` would present the ingress's certificate too, the answer would carry no `api_server_ca_sha256`, and the edge pins the bundle's CA for both.

### Enrolment (once per appliance)

1. An `ArchiveAdmin` adds the edge in the portal. The service creates an `edge` row (`state = pending`) and a one-time **enrolment bundle**, shown once:
   `ofe1.<base64url(JSON{"url": "https://…", "server_ca_sha256": "…", "token": "<256-bit random>"})>`
   The catalog keeps only the token's SHA-256 and an expiry (24 h by default). Issuing a new bundle replaces the old one.
2. In the setup wizard, the operator pastes the bundle. The edge generates an ECDSA P-256 key. In M1 it stored the key in a file (`0600`, root). From M6 the key is sealed with systemd credentials, bound to the TPM (`docs/design/setup-wizard.md`, "Keys"). It then builds a CSR.
3. The edge calls `POST /v0/enrol` (no client certificate) with `{token, csr_pem, facts: {hostname, os, kernel, version}}`. It checks the server certificate against `server_ca_sha256` from the bundle.
4. The service hashes the token and, in one statement, moves the matching `pending` row whose token has not expired to `active`. A token works once. The service ignores the CSR's subject and issues a certificate with:
   - `CN = omnifile-edge-<edge_id>`;
   - SAN `URI:urn:omnifile:edge:<edge_id>`;
   - EKU clientAuth;
   - a lifetime of **30 days**.
5. The response is `{edge_id, cert_pem, chain_pem, ca_pem, not_after, api_url, api_server_ca_sha256?}`. The service records the certificate's SHA-256 (DER) in `edge.cert_sha256`.
   - `chain_pem` holds the intermediates between `cert_pem` and `ca_pem` (empty when the CA signs directly). The edge presents `cert_pem` followed by `chain_pem`, and trusts `ca_pem`.
   - `api_url` is where every later call goes (`OMNIFILE_API_URL` on `enrol`; with TCP ingress it names `api`'s port, `https://<api fqdn>:8443`).
   - `api_server_ca_sha256` (hex SHA-256 of a CA certificate's DER) is present when `api`'s server certificate is the edge CA's (`OMNIFILE_ENROL_API_SERVER_NAMES` set on `enrol`). The edge then trusts that CA, and only it, for `api`. It must be the SHA-256 of `ca_pem` (or of the CA the bundle pinned): an answer naming any other CA fails the enrolment. Absent, the edge trusts the bundle's CA for `api` as for `enrol`.
   - A bad, used or expired token gets `401 enrolment_token_invalid`. A CSR that is not a self-signed ECDSA P-256 request gets `400 invalid_request`.

Failed token attempts (a wrong token, not a malformed request) are rate-limited per source address and audited. The source address is the TCP peer, or, only when the peer is the ingress (`OMNIFILE_ENROL_TRUSTED_INGRESS`), the last `X-Forwarded-For` entry, which the ingress appended.

**Hosting consequence:** `api` accepts no connection without a client certificate (its own TLS listener requires one; with an HTTP ingress, `clientCertificateMode: require`). Enrolment therefore lives on a second, internal-only container app, **`enrol`**: the same image with a different command (`python -m omnifile_service.enrol`), on an HTTP ingress with `clientCertificateMode: ignore`. It exposes three routes:
- `POST /v0/enrol`, for edges, authenticated by the one-time token;
- `POST /internal/v0/sign`, for `api` only (below). Never exposed to edges: the ingress must not route this path from edge networks, the app accepts it only with an Entra token issued to `api`'s managed identity, **and** only from a caller address inside `OMNIFILE_ENROL_SIGN_SOURCES` (api's subnet; set it in production).
- `POST /internal/v0/crl`, for the worker only (decision 50, below).

Before the edge CA exists (a fresh deployment, `deploy/azure/README.md` step 7), `api` and `enrol` start in a waiting mode instead of exiting: they log what is missing, and `/healthz` and every other path answer `503 unavailable` (not ready, but alive: the liveness probe is the port), so the CA can be made inside `enrol`'s container.

### The internal signing endpoint

`POST /internal/v0/sign`, called by `api`, with `Authorization: Bearer <Entra token for api's managed identity>`. `enrol` checks the caller's address against `OMNIFILE_ENROL_SIGN_SOURCES` (when set), the token's audience (its own app ID), and that the caller's object ID is `api`'s managed identity. Any other caller gets `403 caller_not_allowed`. The dev shared token (`OMNIFILE_ENROL_DEV_TOKEN`) is refused unless `OMNIFILE_DEV_MODE=1`.

```json
{"edge_id": "…", "csr_pem": "…", "purpose": "renewal",
 "current_cert_sha256": "…", "request_id": "…"}
```

- `enrol` re-checks the catalog: the edge must be `active`, and `current_cert_sha256` must be its `cert_sha256` or its unexpired `prev_cert_sha256`.
- It issues the certificate exactly as at enrolment (the same subject, SAN, EKU and 30-day lifetime; the CSR's subject is ignored).
- In one transaction, it records the new fingerprint and moves the old one to `prev_cert_sha256`.
- It returns `{cert_pem, chain_pem, ca_pem, not_after}`.
- `request_id` makes it idempotent: a repeated request returns the same certificate.
- Every signature is audited.
- `purpose: "roles_anywhere"` (decision 50) signs the edge's IAM Roles Anywhere certificate instead (below). The same checks apply; the edge's mTLS fingerprints are not touched, and the response adds `renew_after`.
- `purpose: "api_server"` (threat model T5) signs `api`'s own TLS server certificate: the body is `{csr_pem, purpose, request_id}` only (no `edge_id` or `current_cert_sha256`; either is `400 invalid_request`). The caller and address checks are the same. The CSR must be ECDSA P-256 and self-signed; its subject and SANs are ignored. The certificate: `CN` and SAN `DNS` names from `OMNIFILE_ENROL_API_SERVER_NAMES` (`api`'s FQDN), EKU serverAuth only, keyUsage digitalSignature, not a CA, **30 days**. The response is `{cert_pem, chain_pem, ca_pem, not_after, renew_after}` (`renew_after`: day 20). An `enrol` with no names configured answers `403 caller_not_allowed`. Each is audited (`api.server_certificate.sign`). `api` checks the certificate is the edge CA's, for its key, before presenting it, and renews it in place.

### The edge's IAM Roles Anywhere certificate (decision 50)

By default the edge's Roles Anywhere certificate comes from the same edge CA, which the client registers as a Roles Anywhere trust anchor (`deploy/aws/README.md`). The client's own CA stays an option (`docs/design/setup-wizard.md`, "Keys").

- The edge calls `POST /v0/edges/{id}/aws-certificate` on `api` with `{csr_pem, proof: {signed_at, signature}}`, authenticated by its mTLS certificate as for every call. The CSR is for its AWS key, which the setup wizard sealed to the TPM; its self-signature proves the edge holds that key.
- `proof` is signed exactly as for a renewal, by the presented certificate's key, over `omnifile-roles-anywhere-v1` LF `<edge_id>` LF `<signed_at>` LF `<hex SHA-256 of the CSR's DER>`, within 5 minutes. The context differs from the renewal's, so neither proof can be replayed as the other. A forwarded certificate alone (public data) can't get one.
- `api` refuses a CSR whose key is the mTLS key (`400 invalid_request`): the AWS key is a key of its own. It forwards the CSR to `enrol`'s `POST /internal/v0/sign` with `purpose: "roles_anywhere"`.
- `enrol` re-checks that the edge is `active` and the presented certificate is its own. The first certificate **pins the key** (`edge.ra_key_sha256`, the SHA-256 of its SubjectPublicKeyInfo); a CSR for another key gets `409 aws_key_mismatch`. The wizard keeps the key across reruns, so a new key means a new enrolment, as a cleared TPM does.
- The certificate: subject `OU=omnifile-roles-anywhere, CN=omnifile-edge-<edge_id>`, SAN `URI:urn:omnifile:edge:<edge_id>`, EKU clientAuth, keyUsage digitalSignature, not a CA, **30 days**, as the mTLS certificate. The OU sets it apart: the Roles Anywhere trust policy conditions on it and on the CN (threat model T4), so an mTLS certificate from the same CA gets no AWS credentials. Conversely, the Roles Anywhere certificate isn't an edge's `cert_sha256`, so it opens no control call.
- The response is `{edge_id, cert_pem, chain_pem, ca_pem, not_after, renew_after}` (`renew_after`: day 20). The edge renews it with its mTLS certificate (the same heartbeat), or from `renew_after`, with the same CSR. Earlier certificates stay valid until they expire.
- Every certificate is recorded (`catalog.edge_ra_certificate`) and audited (`edge.roles_anywhere.sign`). A revoked edge is refused (`403 edge_revoked`) by `api` and again by `enrol`.

**Revocation to AWS.** Revoking an edge (`POST /v1/edges/{id}/revoke`) also queues `roles_anywhere_crl`, in the same transaction. The worker asks `enrol` for the edge CA's CRL (`POST /internal/v0/crl {}`, with an Entra token for the worker's managed identity, `OMNIFILE_ENROL_CRL_CALLER_OID`, from `OMNIFILE_ENROL_SIGN_SOURCES`). `enrol` builds it from the catalog, never from the request: every unexpired Roles Anywhere and mTLS certificate of a revoked edge, numbered by its build time, valid 30 days, signed with the CA key. Once an edge has been revoked it is never empty: when none of those certificates is unexpired, it lists one serial, the last certificate of the edge revoked most recently. Roles Anywhere refuses a CRL that revokes nothing (`ImportCrl` and `UpdateCrl`, real AWS, 2026-11-14), and the worker never uploads one. It answers `{crl_pem, crl_number, this_update, next_update, revoked, through}`. The worker uploads it, with its web-identity AWS session (decision 43), to each trust anchor in the operator's settings (`OMNIFILE_AWS_ACCOUNTS`: an account's `roles_anywhere_trust_anchor_arn`, through its `endpoints.rolesanywhere` interface endpoint). It uses `ImportCrl` the first time (tagged `omnifile-trust-anchor=<id>`) and `UpdateCrl` after; a lost CRL id is found again with `ListCrls`. One worker replica publishes to an anchor at a time (an advisory lock held from reading its state, through fetching the CRL and uploading it, to recording it), and a CRL whose number isn't above the last one uploaded is never uploaded, so an older CRL never replaces a newer one. Failures back off from 30 s to 15 min, and a minute sweep retries them; it also refreshes the CRL weekly. The alert `roles-anywhere-crl-failing` fires after 15 minutes of failures (`worker/crl_jobs.py`). Sessions already issued end when they expire, at most an hour later (T4).

### Every other call

- **Default (`apiIngress = tcp`): direct TLS.** A TCP ingress passes the connection through; `api` terminates TLS with a client certificate required from the edge CA (`OMNIFILE_API_CLIENT_AUTH=direct_tls`) and reads it from the handshake. A request carrying `X-Forwarded-Client-Cert` is refused (`401 certificate_invalid`): nothing in front of `api` sets it, so the caller did (threat model T5).
- **Forwarded mode** (not offered by the template, T5; for other hosting): an ingress terminates TLS, requires a client certificate, and forwards it to the app in **`X-Forwarded-Client-Cert`**.
  - Format (Microsoft Learn, "Configure client certificate authentication in Azure Container Apps", read 2026-10-05): `Hash=<SHA-256 hex>;Cert="<PEM>";Chain="<PEM…>";`. The documented example elides the PEM's encoding, so the app accepts raw PEM (with or without line breaks), URL-encoded PEM and bare base64 DER, and checks `Hash` against the certificate. The platform does not validate the chain.
  - The app refuses the header unless it is configured to trust the ingress **and** the TCP peer is one of the proxy's addresses (`OMNIFILE_API_TRUSTED_PROXIES`). `api` refuses to start in forwarded mode without them, with a range wider than /16 (/48 for IPv6), or with one overlapping `OMNIFILE_API_APP_SUBNETS` (the subnets the other apps run in): the range must hold only the proxy (T5). VERIFY on a real environment that the ingress replaces a copy of the header a client sends itself.
- The app then:
  1. takes the certificate (from the handshake, or by parsing the header), verifies the chain against the OmniFile CA, and checks validity dates;
  2. maps the certificate's SHA-256 to an `edge` row with `state = active`, accepting `cert_sha256` or, until it expires, `prev_cert_sha256`;
  3. checks that the row's `edge_id` equals the path's `{edge_id}`.
- **Revocation is a catalog state, not a CRL:** setting an edge to `revoked` rejects its next call. Revocation is audited.
- **Renewal:** at two thirds of the lifetime (day 20), the edge calls `POST /v0/edges/{id}/certificate` on `api` with `{csr_pem, proof: {signed_at, signature}}`, authenticated by its current certificate.
  - **Proof of possession.** A forwarded certificate is public data, so it alone is not enough. `signature` is base64 of a DER ECDSA P-256/SHA-256 signature, by the **current** certificate's private key, over the UTF-8 bytes `omnifile-renewal-v1` LF `<edge_id>` LF `<signed_at exactly as sent>` LF `<lowercase hex SHA-256 of the CSR's DER>`. `signed_at` (RFC 3339) must be within 5 minutes of the service's clock. `api` verifies it against the certificate the edge presented; otherwise `401 certificate_invalid` (`400 invalid_request` if missing or unreadable).
  - `api` authenticates the edge as for every call. It then forwards the CSR to `enrol`'s `POST /internal/v0/sign`, with the fingerprint of the certificate the edge presented and the idempotency key as `request_id`.
  - `enrol` signs, records the new fingerprint and keeps the old one as `prev_cert_sha256`. `api` returns the result to the edge.
  - If the response is lost, the old certificate still works and the edge retries with the same `Idempotency-Key` **and the identical body** (the same proof). The service then returns the same new certificate.
- **An expired certificate cannot renew.** The edge needs a new enrolment bundle from the portal; the edge's identity and catalog rows are kept.
- **Known limitation: rotating the edge CA re-enrols every edge.** One CA is the trust anchor for edge certificates and, with TCP ingress, the issuer of `api`'s server certificate, which each edge pins (`api_server_ca_sha256`). A renewal keeps the pin it had (`omnifile-control`, `client.rs`), and `api` trusts one CA (`OMNIFILE_CA_CERT`), so there is no overlap in which both CAs work. Until rotation is built, the procedure is:
  1. Make a second key in the vault (EC-HSM, P-384, `sign` and `verify`) and its certificate with `init-kv-ca` run against it. The template's key name is fixed today (`omnifile-edge-ca`), so this needs a parameter for it.
  2. Issue a new enrolment bundle from the portal for every edge.
  3. For Roles Anywhere (decision 50), register the new CA as a second trust anchor first.
  4. Re-deploy with the new key and `edgeCaCertPem`: `api`, `enrol` and the relay's CA bundle change at once, and every edge loses `api` until it is enrolled again with its new bundle (setup wizard). Edge identities and catalog rows are kept.
  5. Issue the relay a new certificate (`issue-relay-cert`), set `relayCertSha256` and the relay secrets, then remove the old trust anchor.

## 3. Credentials for data

The edge holds no cloud account secrets.

| Backend | How the edge reaches bytes | From the service |
|---|---|---|
| Azure Blob | A **user-delegation SAS** for one blob (or one blob version) | `POST …/credentials` returns a URL with the SAS |
| Amazon S3 | **IAM Roles Anywhere on the edge** (the default, decision 43, M5): X.509 key sealed to the TPM, its certificate from OmniFile's edge CA by default (decision 50, §2) → temporary credentials through `aws_signing_helper serve` (IMDSv2 on loopback); SigV4; PrivateLink interface endpoint, TLS 1.2. Read only | Only a pointer, no secret (`mode = s3_roles_anywhere`). A **presigned GetObject URL** for one object version (`s3_presigned_url`) only when the operator sets `edge_credentials = presigned` for the account (fallback) |

`POST /v0/edges/{id}/credentials` takes `{file_id, version_id, access: read|write}` and returns:

```json
{"mode": "azure_user_delegation_sas", "url": "https://acct.privatelink…/c/key?…&sig=…",
 "expires_at": "…", "cache_until": "…"}
{"mode": "s3_presigned_url", "url": "https://bkt.bucket.vpce-….s3.eu-west-1.vpce.amazonaws.com/key?versionId=…&X-Amz-Signature=…",
 "expires_at": "…", "cache_until": "…", "crc64nvme": "<16 hex digits>"}
{"mode": "s3_roles_anywhere", "bucket": "…", "key": "…", "version_id": "…",
 "etag": "\"…\"", "crc64nvme": "<16 hex digits>",
 "region": "eu-west-1", "endpoint": "bucket.vpce-….s3.eu-west-1.vpce.amazonaws.com",
 "addressing": "virtual_host", "tls_max_version": "1.2",
 "requester_pays": false, "private_network": true}
```

- **The pointer's fields (M5).** `version_id` is omitted for an unversioned object. `etag` is the location's ETag, double-quoted as S3 gives it: the edge sends it as `If-Match` when there is no `version_id`, treats `412` as "the source changed", and refuses a read with neither. `crc64nvme` follows the same rule as for presigned URLs (below). `endpoint` is a host only, from the operator's account settings, never from a catalog row. `addressing`: `virtual_host` puts the bucket in front of the host; `path` puts it in the path; an unknown value is an unsupported credential. `tls_max_version: "1.2"` (the default when absent) pins TLS 1.2, which interface endpoints require. `requester_pays` sends `x-amz-request-payer: requester`. `private_network` (default true): only `*.vpce.amazonaws.com` hosts, checked after the URL is parsed (no backslash or userinfo bypass), resolved through the edge's private-only resolver; without it any valid DNS name on 443 is accepted. The edge also refuses a public host whenever its own `[network] private_only` is on.
- **Presigned fallback, pinned (review on #23).** A `s3_presigned_url` credential carries `if_match` (the location's ETag, quoted) whenever the catalog knows the ETag: the URL is signed with `if-match` as a signed header (`X-Amz-SignedHeaders`), so the edge sends exactly that value as `If-Match` on every GET, and an object replaced since then answers `412` (the source changed: narrow, `remote.source_changed`, fail closed). A presigned URL that names neither a `versionId` nor an `if_match` pins nothing: the edge never reads it chunk by chunk, only by a whole-file fill verified against the CRC.
- **Signing on the edge (M5).** The edge signs each ranged GET with SigV4 (`omnifile-blob::s3`), the response must carry the pointer's `x-amz-version-id`, and redirects are never followed. Credentials come from `aws_signing_helper serve` on loopback (`[s3] imds_url`, a loopback `http://` URL only; `edge/systemd/omnifile-aws-signing-helper.service`), refreshed before they expire and once more when S3 says the session expired. The helper's key is sealed with `systemd-creds` like the edge's mTLS key (`docs/design/setup-wizard.md`, "Keys").
- **Service credentials (M5).** The service itself holds no AWS key: it calls STS `AssumeRoleWithWebIdentity` with its managed identity's Entra token for the deployment's fixed `api://` audience. Endpoints, roles, the audience, the CA, queues, staging, private networking and the edge mode are operator settings (`OMNIFILE_AWS_ACCOUNTS`, `OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE`, `OMNIFILE_AWS_PRIVATE_NETWORK`); a backend row names only the account, region, buckets, prefix, requester pays, versioning and archive class, so a partner's source can never point the service or the edge elsewhere.

- **S3 reads are whole-file verified.** An S3 ranged GET carries no checksum of the range, so the edge can't check a range on arrival as it does Azure's `x-ms-content-crc64`. A whole-file fill from S3 therefore exposes nothing until the whole file matches the catalog's CRC-64/NVME: the marker is widened once, at the end (`docs/design/fsal-namespace.md` §5.3). Each range is still checked for length and, when the URL names a version, for `x-amz-version-id`. **M5: chunk-range reads** (§4.4, "Remote objects") expose each chunk once it is durable, and still make a file `RESIDENT` only after the whole-file CRC matched.
- **The CRC an S3 read is checked against.** A file seeded from a listing or an inventory has no CRC in the catalog: neither gives one. So an S3 read credential carries `crc64nvme`, the location's, or, when it has none, the one S3 stores for the object, read by HEAD with checksum mode. A learned CRC is recorded on the location and on the file, so the feed carries it to the edge too. The edge uses the replica's CRC, else the credential's. An object S3 stores no CRC for (written before S3 added them) gets a credential without one, and the edge refuses to fill it. It becomes readable once the migration's Compute checksum job records its CRC (`migration-engine.md` §5), or once it has been copied to Azure.
- **S3 writes:** `access: write` on an S3-resident file is refused (`unavailable`): new writes go to the dataset's own backend (decision 14).

- **Unknown modes.** A newer service may add credential modes (for example for an on-prem NAS backend). An edge that does not know a `mode` still parses the response; it reports that one operation as `unsupported backend: upgrade the edge` and never reads zeros or skips silently. Unknown `state` and `eta_confidence` values in recall responses are tolerated the same way.
- **SAS scope:**
  - read: `sp=r` on that blob, or on that version (`versionid`);
  - write: `sp=cw` on that blob.
- **Lifetimes:** read SAS 60 minutes; write SAS **15 minutes**, renewed by calling again with the same `upload_id` (so a leaked write SAS is short-lived).
- **Caching:** the edge may cache a SAS and reuse it until `cache_until` (expiry minus 5 minutes). This is what lets reads of cloud-resident files continue for up to an hour while the service is unreachable (section 11).
- **Behind a SAS:** the service holds a user-delegation key (valid up to 7 days) obtained with its managed identity and signs SAS tokens locally, so minting costs no Azure call.
- **Private networking:** if the backend has `private_network = true` (decision 22), the URL always names the private-endpoint host. There is never a public fallback.

## 4. File lifecycle

### 4.1 Namespace changes from the edge

`POST /v0/edges/{id}/changes` sends an ordered batch (up to 1,000 ops) from the edge's local journal. Each op has an `op_id` (unique per edge, for example the journal sequence number), which also serves as its idempotency key.

| `op` | Fields | Effect in the catalog |
|---|---|---|
| `create` | `parent` (file ref), `name`, `kind` (`file`, `dir`, `symlink`), `symlink_target`, attrs | New `file` row (`state = landing` for files). A file's content arrives later through an upload |
| `setattr` | `file`, attrs | A `metadata` version |
| `rename` | `file`, `new_parent`, `new_name`, `replace` (bool) | A `rename` version on the file. A directory rename is one op on the directory. Catalog-only: object keys never derive from paths, so S3- and Azure-resident files rename without touching the backend |
| `unlink` | `file` | A `tombstone` version. The live row gets `deleted_at` |
| `acl` | `file`, `acl_sha256` | Points the file at a known ACL (section 4.6) |

- **File refs** are either `{"file_id": "…"}` or, for objects the service hasn't numbered yet, `{"local": {"fsid": "…", "ino": "…", "ino_gen": "…"}}`. The response maps every `local` ref it created to a `file_id`. Later ops in the same batch may use the local ref.
- **Attrs:** `size`, `mtime_ns`, `uid`, `gid`, `mode`, `owner_sid`, `acl_sha256`.
- **`actor`** (optional, any op; ported from line B, PR #10): who made the change on the edge, shaped like a recall's `requester` (§4.4): `{via, uid, sid, sec, proof, client_addr}`. `via` is `nfs` (from `FSAL_OMNI`'s `NS_HINT`, `fsal-socket.md` §5.4), `smb` or `local` (the daemon's namespace watcher: the smbd session behind the event's pid, or the local process's uid), or `unknown`: the edge's reconcile pass found the change after the fact (made while the daemon was down, or an event it lost), or it could not name the person (no other field then; any sent is ignored). The edge never names a person for its own operations. The versions the op writes record it (`file_version.actor_*`, migration 0030), with the catalog identity of its SID (added as for requesters) or of its uid. `proof` is decided by the service exactly as for requesters (the edge's value can only lower it): `proven` only for Kerberos NFS on Kerberos-only shares, or SMB on `proven` shares; `claimed` for `sec=sys` and local processes; `unknown` stays `unknown`. So a tombstone says who deleted the file: the cutover report's `deleted_by` (`portal-api.md` §5.18). Ops without `actor` (older edges) record nothing.
- **Per-op results:**
  - `applied`, with the assigned `file_id`, `version_id` and `entity_version`;
  - `duplicate`: the op was seen before; same result as the first time;
  - `conflict`: the catalog changed in a way the op can't be applied over. The result says why, and a `file.conflict` feed event carries it too;
  - `rejected`: a rule refused it. Error codes as in section 9. Examples:
    - `directory_not_empty`: unlink a directory that still has live children (children go first; there is no recursive delete);
    - `name_invalid`: not UTF-8, or `.`/`..`/`/`.
- **Ordering:** ops apply in order. A `rejected` op does not stop later ops, unless a later op depends on it (a local ref it would have created). Those fail with `dependency_failed`.
- **WORM shares** (decision 31): overwrite, rename and unlink are allowed, because they only add versions or tombstones. Nothing committed is destroyed. The edge does not need to refuse them locally.

### 4.2 Upload: intent, then complete

The edge outbox's `commit` op (`edge-metadata.md` §5.4) is sent as an upload intent rather than as a `changes` op, because it needs a write credential back. It carries the same `op_id`, so a replay finds the version already created (`file_version.edge_op_id`).

1. The detector sees a write finish (close-write plus the quiet period). The daemon reflinks the file into its commit clone (`.omni/commit/`). The hasher computes BLAKE3 and per-block CRC64-NVME over the clone.
2. **Intent:** `POST /v0/edges/{id}/uploads`:
   ```json
   {"op_id": "…", "file": {"file_id": "…"}, "size": 197568495616, "mtime_ns": "…",
    "blake3": "…", "crc64nvme": "…", "block_size": 268435456,
    "base_version_id": "…"}
   ```
   `base_version_id` is the version the edge believes is current. If it is stale, the service answers `409 version_conflict`, and the edge resolves it from the feed.

   The response is one of:
   - `{"action": "upload", "upload_id", "version_id", "target": <credential (write)>, "block_size", "set_tier": false}`.
     - `upload_id` is the pending version's id, so it equals `version_id`. Replaying the same `op_id` returns this upload with a fresh credential while it is pending, and `dedupe` once it is committed.
     - A new intent for a file drops that file's older pending upload.
     - The service creates a `pending` version and a `pending` primary location.
     - `set_tier: false` means **don't send `x-ms-access-tier`**: on a Smart account, a blob with an explicit tier never enters Smart.
   - `{"action": "dedupe", "version_id"}`.
     - BLAKE3 and size match a committed version in the same dataset. The service records a reference, or makes a server-side copy, and commits the version itself.
     - The edge does not upload.
3. The edge uploads blocks with `x-ms-content-crc64` on each, and keeps a resumable journal. Azure verifies each block's CRC on receipt. The edge then commits the block list.
4. **Complete:** `POST /v0/edges/{id}/uploads/{upload_id}/complete`:
   ```json
   {"crc64nvme": "…", "blake3": "…", "size": …,
    "blocks": [{"id": "<block id>", "length": 268435456, "crc64nvme": "…"}, …],
    "etag": "…", "backend_version_id": "…"}
   ```
   - `crc64nvme` is the whole-file value combined from the block CRCs.
   - `blocks` is the committed block list in order: each block's id, length, and the CRC64-NVME sent as its `x-ms-content-crc64` (which Azure checked against the body). Required.
   - `backend_version_id` is `x-ms-version-id` from Put Block List.
   - The service checks, without reading the bytes back:
     - the block lengths add up to `size`, and the block CRCs combine to `crc64nvme`;
     - the blob version (`backend_version_id`, else the current one) exists with that size (HEAD);
     - its committed block list (Get Block List) is exactly the declared ids and lengths. A block list committed by anyone else holding the write SAS fails here.
     A failure is `400 invalid_request`.
   - It then commits the version (`state = committed`), recording the ETag and **version id it checked**; reads, credentials and later operations use that version, so a later overwrite through a still-valid write SAS changes nothing the catalog points at. It moves the file to `resident`. **`verified` is never set from the edge's CRCs alone:** if the backend's block list carries per-block CRCs (the sim) and they match, the location is `verified` (`upload_block_crc`); Azure's Get Block List has none, so the location is committed with `verified = false` and a `verify` job (PGMQ, same transaction) reads the pinned version back in Azure and computes its CRC64-NVME. Only a match sets `verified` (`azure_read_crc`); a mismatch is audited and the copy stays unverified. Stubbing (section 4.3) requires `verified`.
   - The commit supersedes the previous version (decision 30). On a WORM share it stamps `retention_until = committed_at + retention_days` (decision 31).
   - Returns `{version_id, committed_at, retention_until}`.
5. **Abort:** `POST …/uploads/{upload_id}/abort` drops the pending version and location. An upload left pending for 7 days is aborted by the service.

**WORM rule (decision 31):** the edge never loses a committed version. The commit clone keeps a version's bytes until `complete` returns, so an overwrite or delete of the live file can't lose them. The edge deletes the clone only after that.

### 4.3 Stubbing: desired state, then a two-step handshake

- **Desired state:** the service decides when a file may become a stub (policy, tier pin, `stub_at`). It sets `stub` on the file, as `{version_id, resident_bytes}`, which reaches the edge through the feed (`file.upsert`).
- **The handshake:**
  1. `POST /v0/edges/{id}/files/{file_id}/stub/prepare` `{version_id}`.
     - The service re-checks, in one transaction:
       - `version_id` is still the file's content version (a rename or chmod since doesn't matter);
       - its primary location is `verified`;
       - no `keep_online` hold or warmer tier pin applies.
     - It returns `{lease_id, resident_bytes, lease_expires_at}`, a 10-minute lease. `lease_id` is an opaque string (`<expiry>.<128-bit MAC hex>`); the edge sends it back unchanged. Otherwise it returns `409 stub_not_allowed` with the reason.
  2. The edge stubs as `fsal-namespace.md` §5.1 specifies. It checks the content version, removes the smb-guard ignore mark, and narrows the `trusted.omnifile` marker. It then waits for every connected FSAL to acknowledge an INVALIDATE, and punches holes past `resident_bytes` only if all have. Ownership, mode and ACLs are untouched. If any step fails, it reports `skip` (below).
  3. `POST …/stub/commit` `{lease_id, version_id, resident_bytes}` moves the file to `stub`.
- **Skipping:** if the edge can't or won't stub (its local version differs, the file is open for write, or a lease was refused), it reports `POST …/stub/skip` `{version_id, reason}`. The service clears or reschedules the desired state.
- **Safety:** the edge never punches a file whose local committed version differs from `version_id`, or without a valid lease. A crash between steps 2 and 3 is safe, because the bytes are in the cloud. On restart the edge replays the commit from its journal.

### 4.4 Reads of non-local data, and recalls

When a read reaches bytes that are not local, the FSAL or smb-guard asks the daemon. Unless the replica already knows the answer, the daemon calls `POST /v0/edges/{id}/recalls`:

```json
{"file_id": "…", "version_id": "…", "offset": 0, "length": 1048576,
 "requester": {"uid": 41023, "sid": "S-1-5-21-…", "via": "nfs",
               "sec": "sys", "proof": "claimed", "client_addr": "10.1.2.3"},
 "on_open": "wait"}
```

- **`proof` (decision 28, identity chain)** is decided by the service, never taken from the edge. The edge may still send `proof`, but it can only lower the result:
  - `proven` for NFS only with `sec` = `krb5`, `krb5i` or `krb5p`, and only if every NFS share of the file's dataset on that edge is Kerberos-only (`trust_level = proven`);
  - `proven` for SMB only if the dataset has an SMB share on that edge and every such share is `proven`;
  - `claimed` otherwise: NFS `sec=sys`, `via = local`, or a `sec` that doesn't fit the protocol.
  The service records it on the recall and in the audit log (with the edge's value when it differed).
- **Requester SIDs.** A SID the catalog hasn't seen becomes a catalog identity (for its Teams card) only if it is a domain account (`S-1-5-21-a-b-c-RID`, RID 500 or ≥ 1000) in a domain this edge's shares use, and the edge is within 30 new SIDs per 10 minutes. Otherwise the recall still proceeds, but no identity is created and no notification is queued; the audit log records why (`sid_rejected`).
- **`pinned`** (optional, default false): the reader asked for this version itself, not for whatever the file holds. The edge sets it for every read of a rewind placeholder (`.snapshots`, Previous Versions). A pinned recall is not ended when the file is replaced or deleted (below). Joining pinned pins a recall that wasn't.
- **`via`:** `nfs` (from `FSAL_OMNI`), `smb` (an smbd session, caught by the smb-guard), or `local`: a process on the edge itself, such as an admin shell or a backup agent, read the stub directly through the data volume. The smb-guard catches those reads too. A `local` requester has the process's uid and, when it maps to AD, its SID.
- **The response is one of:**
  - `{"status": "online", "credential": …}`: the bytes are readable now (hot, cool, cold, Smart, Glacier Instant Retrieval, or a live restored copy). The edge range-gets them into its cache.
  - `{"status": "recall", "recall_id", "state", "joined", "eta", "eta_confidence", "decision", "note"}`:
    - a recall was started, or joined: one active recall per content version (a rewind read of an old version is a separate recall);
    - `decision` is `wait` or `fail`, from the share's `on_open` (`wait`, `fail`, or `auto` against the ETA; see the vocabulary table below);
    - `note` is the text for `<file>.RECALLING.txt`. The file name in it has control characters escaped (`\xNN`, `\uNNNN` for Unicode line separators and bidi controls) and is capped at 255 characters, so the note never carries raw control characters;
    - `eta` is null for files over 10 GB on Azure, because no time is promised above that (`AGENTS.md`).
- **Progress:** recall state changes reach the edge as `recall.state` feed events. The edge does not poll.
- **States.** The catalog's recall state is what the portal and Teams show: `requested → rehydrating → copying → downloading → available`, or `failed` / `cancelled` / `superseded`. `copying` exists only when a restored object must be copied first (Storage Mover for S3-resident files, decision 20, or the WORM recall container); cards show it as part of "rehydrating". How these map to `base.RestoreState` and to the FSAL's states is defined once, in `fsal-socket.md` §5.5 ("State mapping").
- **Open policy.** The catalog and the feed use the share's terms (`on_open` = `wait`, `fail`, or `auto` with `wait_threshold_minutes`). The daemon translates them for the FSAL as `fsal-socket.md` §10 defines: `auto` becomes `waitquick` with `quick_ms` = minutes × 60,000.
- **Fill:** when a recall reaches `downloading`, its event carries a read credential for the restored copy. On a WORM share that copy is in the non-WORM recall container (decision 31). The edge fills the file through its private mount, then calls `POST …/recalls/{recall_id}/filled` `{version_id, resident_bytes}`. The service moves the recall to `available` and updates the Teams card.
- **A file replaced or deleted while it is recalled** (migration 0059). A recall of a file's current content is for those bytes under that name. When a new content version of the file is committed, or the file is deleted, the file's active recalls of what was its current version end **in that same transaction**, as `superseded`, with why (`replaced` or `deleted`) and the version that did it. That is not a failure, and the next read is of the new file.
  - **Which recalls.** Only recalls of the then-current content version, and not one someone asked for by version. A recall of an older version is of that version (a rewind read, a portal version restore) and is never reached. Two things keep a recall of the current version: `pinned`, set by a read in `.snapshots` or Previous Versions (the edge says so), which stays; and `held_by_jobs`, the number of unfinished portal version restores (decision 34) and annex placements waiting on it, which the catalog keeps by triggers on those tables. When the last such job ends, the recall is an ordinary one again, and the next replace or delete ends it. A rename or an attribute change moves no bytes and ends nothing.
  - **A kept recall whose file moves on** is marked with the version that would have ended it (`moved_on_version_id`), and goes on for whoever kept it. No edge fills it: its file is deleted or holds another version. So it ends with its last job:
    - **the job had its bytes** (a restore `done`, a placement `placed`): the job closes the recall as `available`. A restore does so from any active state: it reads the bytes as soon as the recall's copy is recorded, which can be before the recall is marked `downloading`. A placement's recall is `downloading` by then;
    - **the job let go** (it failed, was cancelled, or its row was deleted) and the recall is not `pinned`: the recall ends `superseded`, in the job's own transaction, with the reason and the version of the commit that passed it by, and `notify` is queued. A deleted file's recall of the content it still names ends as `deleted` this way even with no mark (it was asked for after the delete, or before migration 0059): the idle rule would otherwise fail it.
    - A recall of a version that was already an old one when it was asked for has no mark, and is left as before: nothing judges it.
  - **Who is told.** The commit queues `notify` itself. The card and the portal say "replaced" or "deleted while it was being recalled", never "failed" (`teams.md` §3.2, `portal-api.md` §5.5). They name who did it only to someone who can read the file, and only when the superseding version records an actor the edge proved (Kerberos NFS, or SMB on a proven share). Today that is a delete: an upload's content version records no actor.
  - **The provider's restore is left to finish, not aborted.** The worker stops polling it for the recall, and starts none for a recall that has already ended. A restore already running stays on record: the change feed closes it and commits its copy (Azure's rehydrated copy; S3's temporary copy expires after its `Days`), a later recall of that version joins it or reads its copy, and the recall-copy expiry deletes the copy after its 7-day window. So nothing is orphaned, and the cost is what the recall had already asked for: the retrieval, and the copy as Hot for up to 7 days. Azure's Abort Copy Blob is not used: the version it would stop is still a kept version, and the one a user is most likely to ask for next (a rewind, or a restore of what was just overwritten).
  - **The edge.** The feed carries `superseded` to an edge that says it knows the state (`recall_superseded` in its heartbeat, §4.7), and `cancelled` to one that doesn't: edges are updated after the service, and an older one reads an unknown state as nothing ended, so a reader of a `wait` share could wait for good. To an edge the two mean the same. The edge stops a fill of that recall, schedules no resume, ends what it kept for the recall and removes the `.RECALLING.txt` note (for a deleted file, where it wrote it). A fill that finds the live file is no longer the recall's version reports nothing, whether or not the change is committed yet: the replica says the file is deleted or holds another version, or on disk another inode has its name, it has no marker (the FSAL removes the marker when a writer truncates a stub), or its marker names another version. `…/failed` on a `superseded` recall answers 204 and changes nothing.
  - **A reader that was waiting** (`wait`, or `auto` within its threshold) is released, never left waiting on a recall that has ended. If the file was replaced in place, the entry ends `CANCELLED`, and the reader's next read finds the new marker (or none) and gets the new bytes. A reader that was part-way through keeps what it had already read: the stub's resident header is the old version's, and what follows is the new file's, as with any file replaced while it is being read. If the inode the reader holds is no longer the file (the file was deleted, or another was renamed over it, and the reader still has the old one open), its bytes can never come: the entry ends `FAILED` and the reader gets `EIO`. A deleted file's recall row used to be applied to nothing on the edge, so such a reader waited on. The same holds when a kept recall is closed `available` for a file that moved on: nothing was filled on the edge, so a reader still waiting there is released in the same two ways (`CANCELLED` if its inode is still the file, `FAILED` and `EIO` if not). An `available` row for an entry the edge's own fill already ended changes nothing.
  - **Holds and pins are the file's.** A `keep_online` hold (decision 33) and a tier pin (decision 29) are not touched: they stay as they were on the replaced file, and on the deleted one.
  - **The one limit.** Between the write on the edge and its commit (the quiet period and the upload), the recall is still `downloading` in the catalog with no fill. The commit ends it. If the commit takes longer than `OMNIFILE_RECALL_NO_FILL_MINUTES` (a very large upload, or a writer that holds the file open), the idle rule below fails it first, as `edge not filling:`.
- **A fill that fails** (the bytes failing their CRC, a placeholder whose marker can't be read, or an interruption that outlasts the edge's resume window) is reported with `POST …/recalls/{recall_id}/failed` `{version_id, reason}`. The recall becomes `failed` with that reason, users are told, and the next read starts a new recall. A fill only interrupted (the daemon stopping, an FSAL not yet reconnected, the network, the edge's data volume full) is not reported: the edge resumes it, with a backoff (5 s doubling to 5 min), up to 8 times in 30 minutes, and reports it failed only once those are used up. A full volume is often transient, but nothing on the edge frees it: the edge never stubs files on its own, and the chunk cache evicts only within its own budget. Users deleting files, an admin growing the volume, or the action the low-disk alert asks for is what frees it. A read of the file while a resume is scheduled waits for it rather than starting a fill of its own. It does so at start for every recall its replica has in `downloading`, and whenever a read is answered with a `downloading` recall that no local fill is working on. So a recall never stays `downloading` without a fill that will end it. A failure report is dropped if a newer fill of the same recall is running, the replica no longer says `downloading`, or the file is no longer the recall's version (above); and if the bytes arrive after all, `…/filled` on a recall the edge reported failed makes it `available` (the catalog then says `resident`). A rewind placeholder's fill is never resumed on a timer or reported failed (its recall is the live file's too): the reader's next open starts it again.
- **An edge that stops.** A recall of a file's current content stays `downloading` only while its edge works on it. The worker fails it when the edge has sent no heartbeat for `OMNIFILE_RECALL_EDGE_SILENT_MINUTES` (default 60), with a reason that starts `edge unreachable:`. It also fails it when the edge heartbeats and reports `fills_running`, has caught up on its feed (`feed_lag_events` 0), and has reported no fill running for `OMNIFILE_RECALL_NO_FILL_MINUTES` (default 60), with a reason that starts `edge not filling:`. Both count from when the recall reached `downloading` too, and 0 turns a rule off. Silence and idleness count only within the service's current reachable window: the time since heartbeats last started arriving from the edges with no gap over 3 minutes (`catalog.heartbeat_window`). An outage of the service or of the edges' way to it (ingress, allow-list, certificates, the api losing the catalog) closes the window and fails nothing; after it every edge has the whole limit again. A site with a single edge cannot tell that edge being gone from such an outage, so its recalls are failed only by the idle rule. Never on elapsed time: a fill that runs for days while its edge heartbeats with a fill running is left alone. A recall of an older version (one a portal version restore made, which the restore closes as `available` when it is done, or one the file has moved past) is not judged. Users are told as for any failed recall. The feed carries `failed` to the edge, which stops any fill of that recall it still runs. An edge that has the bytes after all still reports `…/filled`, which makes the recall `available` (`worker/recall_timeout.py`).
- **M5: chunk-range reads (edge-local cache).** An `ONLINE` read through an FSAL that negotiated `CHUNK_MAP`, or the smb-guard, is not a recall: the edge reads the version's credential from `POST …/credentials` and fetches the chunks the read touches into the placeholder (`fsal-namespace.md` §5.4). The service is not told about these fills or their eviction. Eviction narrows by the stub handshake without a `stub/prepare` lease, and never below the prefix the catalog counts (a stub's). When every byte is local and the whole-file CRC matches, the edge reports `POST …/files/{file_id}/filled` with `resident_bytes = size`, as an online fetch does; without a catalog CRC the file stays `PARTIAL`, served chunk by chunk. A read that needs the whole file (write, truncate), an archived object, or an FSAL that negotiated v0.1 keeps the recall and whole-file fill path. Edge-local fetches that escalate to a recall send the edge's own requester (`uid` 0, `via = local`, claimed).
- **Online fetch:** bytes that were readable by range GET (no recall) are fetched into the stub the same way. The edge then calls `POST …/files/{file_id}/filled` `{version_id, resident_bytes}`, and the catalog shows the file `resident` again once every byte is local. Repeats are harmless.

### 4.5 Point-in-time reads (decision 30)

The edge doesn't replicate version history. For a `.snapshots/<date>/…` or `@GMT-…` lookup it calls:

`GET /v0/edges/{id}/asof?dir_id=…&t=<RFC 3339>&after=<name>&limit=…`

This returns that directory's entries as they were at `t`, in the same row shape as `file.upsert`. Each row carries the `version_id` valid at `t`, and the location of that version.

- If `t` is older than the share's `rewind_depth_days`, the answer is `410 beyond_rewind_depth`. A `t` in the future is `400 invalid_request`; a `dir_id` that isn't a directory of this edge's datasets is `404`.
- Entries are paged by name (bytewise): `after` is the last name of the previous page, and `more` says whether another page follows.
- Rows are always `state: remote`, with `resident_bytes: 0` and no `stub`: the edge decides whether a placeholder can be a reflink of the live file (the same content version, resident). `acl_id` is the ACL at `t`. `entity_version` is the id of the version row valid at `t`; rewind rows never enter the edge's live replica.
- Reading an old version uses `POST …/credentials` with that `version_id`.
- An old version in Archive goes through `POST …/recalls`, with the same wait/fail flow.

### 4.6 ACLs (decision 28)

ACLs are content-addressed per dataset. The edge computes `acl_sha256` over a canonical form: the JSON of `{owner_sid, group_sid, mode, entries}`, with entries sorted and no whitespace.

- `POST /v0/edges/{id}/acls` `{dataset_id, acls: [{sha256, owner_sid, group_sid, mode, entries: [{scope: access|default, tag, sid, perms}]}]}` stores any ACLs the service doesn't know yet. It returns `{acl_ids: {sha256 → acl_id}}`. Sending a known ACL again is harmless.
  - The canonical form is exactly `AclSnapshot::canonical_json` in `edge/crates/omnifile-acl/src/snapshot.rs`: entries sorted by their own canonical text (`{"perms","scope","sid"?,"tag"}`, keys sorted), then `group_sid` (if any), `mode`, `owner_sid` (if any). The service recomputes `sha256` and refuses a body whose hash differs (`400`), so one ACL can never be filed under another's hash.
  - `user` and `group` entries carry a `sid`; the others must not. `tag` is one of `owner`, `owning_group`, `user`, `group`, `mask`, `other`.
  - The service derives `read_sids` and `deny_sids` itself, in POSIX.1e access-check order: the owner entry decides for the owner; a named-user entry decides for that user even when it denies; else any matching group entry (owning or named) granting `r` grants and a matching one without `r` denies; else `other` (Everyone, `S-1-1-0`). The mask limits named users and all group entries. `catalog.acl_can_read(read_sids, deny_sids, user_sid, group_sids)` (migration 0007) gives the answer; `read_sids` alone is only the index prefilter.
  - Each folder and file row points at one ACL (`file.acl_id`): a folder's snapshot and a file that differs from it are both pointers.
  - `dataset_id` must be one of this edge's datasets (`404 not_found` otherwise); at most 1,000 ACLs per call.
- Files point at ACLs through `changes` ops (`acl` op, or `acl_sha256` in attrs). An ACL the service hasn't seen fails that op with `acl_unknown`.
- **Scans:** a nightly full scan, and on-change batches, are bracketed by:
  - `POST …/acl-scans` `{dataset_id, kind: full|on_change}`, which returns `scan_id`;
  - `POST …/acl-scans/{scan_id}/complete` `{entries_seen, entries_changed}` (`entries_changed ≤ entries_seen`). It is idempotent by `scan_id`: the same counts again answer `204`, different ones `409 version_conflict`. Only the edge that started a scan can complete it.

  The portal shows the last full scan's time.

### 4.7 Reads, audit and heartbeat

- `POST /v0/edges/{id}/reads` reports reads for the policy's `clock = last_read`. Reads of resident files never reach the service otherwise.
  - Body: `{reads: [{file_id, last_read_at}]}`, up to 10,000 entries.
  - The edge coalesces to **one entry per file per hour**: the latest read time in that hour. It sends a batch at least hourly, and holds batches while the service is unreachable.
  - The service keeps the later of the stored and reported times (`file.last_read_at`), so late or repeated batches are harmless. These updates touch only service-side clocks, so they produce no feed events.
  - Response: `{accepted, unknown_file_ids}`.

- `POST /v0/edges/{id}/audit` sends batched audit records:
  - opens of stubs;
  - denies;
  - recalls triggered by reads;
  - local admin actions.
  Each record has `{at, action, decision, sid?, uid?, proof, trust_level, file_id?, client_addr?, details}`. The edge buffers them while the service is unreachable.

  Built:
  - **Service:**
    - Records are stored in `catalog.audit_log` as actor `edge:<id>`, 5,000 per call. The batch is refused whole if any record is malformed, or dated more than 10 minutes ahead.
    - The dataset and share come from `file_id`. A file that isn't this edge's is never put on the row; it goes into `details.claimed_file_id`, so an edge cannot write into another dataset's history.
    - `uid` goes into `details`.
  - **Edge:**
    - **M5:** `remote.source_changed` (`info`, `file_id`, `details: {version_id, bucket, key, etag}`): a chunk read pinned by `If-Match` (or a version) got `412` from S3, so the source object changed under the catalog. The edge has already narrowed the file to the catalog's prefix and fails reads of it closed for a while. Recorded through the replica's audit queue, so it survives an unreachable service.
    - `read.refused` (`deny`): a read the edge answers with an error itself, over NFS or SMB. `details.reason` is one of `failfast`, `recall_failed`, `recall_cancelled` (a `superseded` recall counts as cancelled here), `unknown_file` or `shutting_down`, with `via`, `version_id`, `on_open` and `sec`.
    - Recalls are not repeated: the service records `recall.request` itself.
    - One record per file, requester and reason per 10 minutes, however often a client retries.
    - Records go into the replica's `audit` table every 5 seconds, and from there to the service. The table keeps at most 100,000; past that it drops the oldest and records `audit.dropped`.
    - A clean shutdown flushes them.
- `POST /v0/edges/{id}/heartbeat` sends:
  - software version;
  - kernel;
  - guard state;
  - landing-zone and cache usage;
  - queue depths;
  - feed cursor;
  - `recovery_used`: 1 while the break-glass account has been used since it was last sealed (`docs/design/setup-wizard.md`).
  - `quiet_default_s` and `quiet_worm_default_s`: this edge's quiet periods for a share with none of its own (`[commit] quiet_s` and `worm_quiet_s`, decision 67). Sent at all, they say the edge applies each share's own period; the portal shows them.
  - `quiet_stale_s`: how long the edge has failed to read its shares' quiet periods, from the first failure; 0 while reads work. From 300 the edge needs attention on the Edges page, and an alert fires.
  - `recall_superseded`: 1 from an edge that knows the recall state `superseded` (§4.4). The feed sends `cancelled` in its place to an edge that has not said so.

  It returns:
  - `server_time`;
  - `min_edge_version`;
  - `feed_lag_events`;
  - `config_version`;
  - `certificate_renew_after`.

  Every 30 seconds.

### 4.8 Annexes (annex.md, phases A1 and A2.1)

An annex is a watched directory on someone else's NAS (`docs/design/annex.md`). The edge next to the NAS runs one `omnifile-annex@<annex_id>` unit per annex. It mounts the NAS read-only, keeps its own listing in `annex.db`, and copies files through the usual upload path (4.2). The service never reads NAS bytes, and an annex's dataset is never in the edge's feed: the edge holds no placeholders for annexed files.

- `GET /v0/edges/{id}/annexes`: the live annexes of this edge (registered, baseline, active, paused), each with its filer (host, protocol, export, credential mode, event source), its settings (quiet period and scan schedules) and its running rule set (`{version, body}`, the if-then rules of annex.md section 4.1). The edge evaluates the rules itself, for the preview and for real, and stops copying if it can't read them (a `skipped` event with reason `rules_unreadable`, path `.`). An annex whose rule set waits for a person's approval isn't listed. The unit polls it every minute; a retired annex's unit stops. Each annex carries `served_at`, the service's clock: the unit decides no age-based removal while its own clock is more than 10 minutes from it. `reconcile_asked_at`, when set, is the service asking for a reconciliation now (a copy Azure's read-back refused, `annex.md` section 3.3): the unit runs one when it sees a value it hasn't answered. After a second refusal of the same file the ask is an hour later, and it isn't shown until then; after a third the service stops asking for that file.
- `POST /v0/edges/{id}/annex-events`: `{annex_id, events: [...]}`, up to 1,000 events of one annex. They are applied in order, each in its own savepoint, and the response has one result per event: `{index, status: applied | rejected, code?, detail?, ...}`. Kinds (open; an unknown kind is rejected for that event only):
  - `mount`: the edge's mount check of the filer (`details`: ok, read_only, readable, sec, error, and `hung: true` when the watchdog sends it), stored as the filer's health. With `hung`, it also raises a `nas_not_answering` problem.
  - `scan`: a quick, full or reconcile scan finished (`details.scan`, counts).
  - `baseline`: the first scan's count (`details: {files, bytes}`). The annex becomes `active`. Files present then are sent as `arrived` with `baseline: true`, which makes their catalog files without an arrival on the timeline.
  - `arrived`, `changed`: `{path, nas_file_id, size, mtime_ns, ctime_ns, btime_ns?, uid?, gid?, mode?, acl, rule?, tags?}`. `rule` is the id of the rule that chose to copy the file (a file a rule excludes is never sent). The service makes the file in the annex's dataset (folders as needed, with the Owners group's ACL), as `origin = 'annex'` and `state = 'remote'`, and its `annex_file` row. The result is `{file_id, content_version_id, copy}`; `copy: true` means upload it through `uploads` with that `file_id` (and `base_version_id`). The ACL is the NAS's: an NT security descriptor (parsed by the service), an NFSv4 ACL with the edge's SID mapping, or POSIX mode and entries. NT and NFSv4 ACLs are evaluated deny-first. An ACL the service can't read leaves the file unreadable in the portal, and records a `problem`.
  - `copied`: `{path, file_id, version_id, size, mtime_ns, ctime_ns}` after `uploads/complete`. The upload is verified by the `verify` queue's in-Azure CRC read (decision 24), which records `verified` on the annex file.
  - `left`: `{path, nas_file_id?}`. The file is gone from the NAS: the catalog file is tombstoned and its copy kept until an ArchiveAdmin purges it (decisions 40 and 51).
  - `renamed`: `{path, old_path, nas_file_id}`: the same NAS file at a new path. The catalog file moves.
  - `drift`: `{path, details: {drift, expected, observed}}` from the weekly reconciliation: `missed_arrival`, `missed_departure`, `changed_no_event` or `copy_mismatch`, as a `catalog.reconcile_drift` row (source `annex`). The event that fixes it marks the row with `resolved_by`.
  - `skipped`, `copy_failed`: `{path, reason, detail?}`, for the annex's Problems list.
  - `would_remove`: `{path, file_id, version_id, rule, reason}`: a removing rule chose a copied file (Move now after its copy, or an `age` rule or `remove_after_days` on its day). Phase A2.1 records it, with whether that version is the copy and is verified, and removes nothing; the removal pipeline (A2.2) acts on tickets the service issues instead.
  - `folder_acl` (phase A3.1): `{path, acl}`, `{path, error}` or `{path, gone: true}`, after a full scan, for each folder that changed, and after a quick scan for each folder not sent yet. `path` is relative to the filer's export: the annex's own tree, a folder above its root up to the export's root (`""`, decision 63), or a folder below one of its return rules' `nas_folder` locations on this filer (anything else is `rejected: out_of_scope`). The service keeps it in `catalog.nas_folder_acl`, with who may add a file to the folder (`write_sids`, deny-first; an unmapped deny, or an ACL it can't read, lets nobody: the requester's write check for a return target then fails) and who may pass through it to what is below (`search_sids`, decision 63: an unmapped deny, or an ACL it can't read, hides every annexed file below it, and the annex shows a `folder_unreadable` problem). A gone folder keeps its last ACL (`gone_at`) for the files that were in it, and takes no placements. An older report never replaces a newer one.
  - `mount` (decision 64): its details always carry `acls_shown`: whether the server shows the ACLs of the annex's flavour at its root (the POSIX ACL, NFSv4 ACL or NT descriptor attribute answers, even with "none"), or `null` when the edge couldn't look. `false` flags the filer (`acls_hidden_since`) and raises an `acls_hidden` problem once; `true` clears the flag and its acknowledgement. Fail closed: a report without the field (an edge from before decision 64), or `null` from a mount that is `ok` (the annex root wouldn't open), flags it too; only `null` from a failed mount changes nothing.
  - `folders_read` (decision 63): `{details: {folders, unreadable, ancestors, complete}}` after a folder pass that read every folder of the annex and every one above its root, sent when something changed and once after the update that brings it (when the unit also sends every folder again). The service keeps the first and latest in `annex.folders_read_at`; until the first, every annexed file is hidden.
  - `target_space` (phase A3.1): `{path, free_bytes, total_bytes}` for the annex root and each return location, as seen through the read-only mount, for previews only.
- `GET /v0/edges/{id}/annex-dry-runs?annex_id=`: previews the portal asked for (`{run_id, annex_id, body}`), up to 20. The unit polls every 2 seconds and evaluates `body` over its listing, read-only (annex.md section 4.5).
- `POST /v0/edges/{id}/annex-dry-runs/{run_id}`: first `{state: running}` when the unit takes a run (`409 version_conflict` if it no longer waits: a newer draft replaced it, or it expired), then `{state: done, report}` or `{state: failed, error}`. Answered once: a later answer leaves the first, and the response gives the state now. A run not taken within 10 minutes, or not answered within 15 of being taken, fails by itself. The report counts files whose rules can't be decided now apart (`undecided`, and `removals.undecided`).
- **Removals (phase A2.2; decisions 51 and 59).** `GET annexes` gives each annex `removals`: `{live, why_not, dry_run, grace_minutes, pointer_files, rate_per_hour}`. Live means all of these: the running rule set removes, approved by an ArchiveAdmin (`decided_as_archive_admin`; no one else may approve a removing set); its dry run has ended; an ArchiveAdmin confirmed that dry run for the running version; and neither the annex nor Settings pauses removals. The unit mounts the NAS read-write only while it is live. The edge never decides a removal alone:
  - `POST /v0/edges/{id}/annex-removal-tickets` `{annex_id, candidates: [{file_id, version_id, path, nas_file_id, size, mtime_ns, ctime_ns, rule}]}`, up to 100: one result per candidate, `ticket` (`removal`, with `expect` and `expires_at`, 10 minutes) or `not_yet` / `refused` with a `reason`. A ticket needs: removals live; the candidate's `rule` one of the running set's removing rules (`arrived` or `age`; else `refused: rule_not_removing`); no legal hold on the file or a folder above it (`refused: legal_hold`); the file present, its current version the copy, with the NAS attributes it was copied with; that copy verified and **read back in Azure within 24 hours** (otherwise a read is queued, `not_yet: needs_azure_read`); and the window and the rate (`removal_rate_per_hour` tickets an hour) allowing it. A file has one removal in flight: asking again returns it. The candidate's file must be the annex's (`refused: not_in_annex`), at the catalog's path (`refused: path_mismatch`), and decided under the running rule set version (`rules_version`; else `not_yet: rule_set_changed`). `expect.crc64` is the verified copy's CRC-64/NVME, which the edge compares with the file read again before the delete over SMB.
  - `POST /v0/edges/{id}/annex-removals/{removal_id}` `{step}`: `hide` before renaming the file to its hidden name and `delete` before deleting it. Each checks every gate again and answers `{proceed, reason}`. A `delete` refused for a pause or the window (`paused`, `outside_window`) leaves the removal `hidden`: the file waits, and its grace starts again. Any other refusal cancels the removal, and the edge renames the file back. The edge asks each question with its own idempotency key, so a delete asked again after a pause is answered afresh. Every gate is checked on every ask, a delete asked again from `deleting` included. The service keeps the grace itself: a delete before `hidden_at + grace_minutes` is held (`grace`). A ticket of another rule set version is refused (`rule_set_changed`). The edge reports `stuck` (`hidden_name`, `reason`) for a hidden file it can't put back, which becomes a `removal_stuck` event (the annex's Problems) and an alert. Every step, holds included, is an audit row with the verified CRC. The edge reports `hidden` (`hidden_name`), `deleted`, `pointer` (`written`), `done` and `cancelled` (`reason`) after taking them. A file under the hidden name with another inode is a user's file W1 hid (a save-by-rename just before it): the edge puts it back and reports `cancelled` with `reason: swapped_at_hide`; when its own name is taken it is kept as `<name>-replaced-<UTC time>`, sent as `kept_as`, which raises a `removal_kept_aside` problem. The removal gate also refuses while a replace at the origin is open for the file (`replace_open`). Each step is an `annex_event` (`removal_scheduled`, `renamed_for_removal`, `removed`, `removal_cancelled`) and an audit row. A step asked again after a restart is answered as it is, so nothing is taken twice.
- **Placements on a NAS (phase A3.3; `wp14-a3.md`, "What A3 writes to a NAS").** The edge never places a file by itself.
  - The `placement_account` annex event (`details`: `{sid, kind, name}` from winbind, or `{error}`): which account this edge places files on the annex's filer with, the restore-rights principal it actually holds. Sent on an SMB filer when the unit starts and hourly. The service keeps the last one on the filer, for `PATCH /v1/filers/{id}` to check against, and it registers nothing. Only the filer's own edge can write it: the edge is the client certificate's, the annex must be that edge's, and the filer must be too (else `rejected: not_this_edges_filer`). No field in the body names any of them.
  - `POST /v0/edges/{id}/annex-placement-tickets` `{annex_id, in_hand?, renew?}` → `{tickets, failed}`. Tickets for the `ready` placements whose target is a folder of this annex's filer: the placement's own annex if that is its filer, else the filer's first live annex. At most 10, each after every gate, and then `writing`.
    - A ticket carries the folder (relative to the export), the file's own `name`, the names it may take in order (its own, `-restored`, then `-restored-<UTC time>`; an older version at its origin never takes its own), `origin`, `same_content` and `protect` (inherited entries made explicit and the DACL protected, in any folder but its own). It also carries the captured descriptor (`ntfs_sd` or `nfs4` raw, or `posix` mode), the version's size, CRC64-NVME and mtime, and a read credential. `uid` and `gid` are the NAS's own numeric ids, sent only when the target is on the file's own filer.
    - A placement that no longer passes its gates is `failed`: lost read access, a target the rules no longer allow, the origin of a file back on the NAS, or a descriptor this filer can't take (`acl_unsupported`, including a POSIX ACL with named entries in v1). A paused one waits (a pause on any annex of the target's filer counts), and so does one whose bytes aren't readable yet.
    - One already under way is handed over again only if the edge doesn't list it in `in_hand`, or lists it in `renew` because its read credential is running out (a placement stuck before its `go` may ask this too). Only then is a new credential minted, and it is audited. Otherwise its gates are checked and nothing is sent.
  - `POST /v0/edges/{id}/annex-placements/{placement_id}` `{step, ...}`:
    - `go`, before the link that names the file: every gate again, plus the target folder's ACL as the edge reads it now (the requester may still add a file there) and the staged ACL (decision 51; else `acl_mismatch`): its read set within the file's catalog read set, an NT entry for its owner counting as the file's own owner only when that owner is the filer's registered `placement_sid`, and its group taken literally; every catalog deny in it, with the same evaluation order; WRITE_DAC and WRITE_OWNER only for readers, the owner, or whoever the catalog's ACL gave them to; and no allow of read or of those for a principal the edge couldn't map. The read-back's `crc64` must be the version's (`crc_mismatch`). The answer is `{proceed, reason}`; anything but a pause fails the placement. It may be asked again from `placing` after a restart, with every gate checked again.
      - **Unmapped principals.** `WRITE_DAC` or `WRITE_OWNER` for a principal the edge couldn't map is always `acl_mismatch`. An allow of read for one, or a POSIX owning group with no SID whose bits read, is too, with two exceptions. (1) The catalog's raw ACL literally lets anyone read the file: `S-1-1-0`, a NULL DACL or POSIX's other class, not Authenticated Users or `BUILTIN\\Users`, which the read sets normalise to Everyone. (2) At the file's origin on its own filer, the staged ACL is provably the capture of the version being placed: `uid` and `gid` (sent with `go`) are the version's and the file's as the catalog has them now, the mode is the same, and the ACL is the same (raw bytes for NFSv4; for POSIX, no named entry on either side, because the catalog keeps no raw copy of those, and `posix_acls_shown: true` in `go`, the edge's word that this server can show them: a server that hides them looks base-only; if that is all it lacks, `go` answers `acl_hidden`, with a detail). (2) covers control bits too. A staged `S-1-1-0` needs a catalog ACL that literally has it.
      - **Over SMB**, a staged owner that is neither the file's nor the filer's registered `placement_sid` answers `placement_account_not_registered` rather than `acl_mismatch`, and the failure's detail names the fix.
      - A purge asked for and still pending, or done, answers `file_purged`, at tickets too. One a legal hold refused has ended, and doesn't.
    - Reports: `filled`, `read_back` (`crc64`, which must be the version's or the placement fails `crc_mismatch`), `linked`, `owned`, `failed` (`reason`), `stuck` (`reason`, and where it is: `at` and `name`, kept for an abandon to say what the file was left like), `leftover` (`detail`: something a failed placement couldn't clean up, a `problem` event) and `done` (`outcome`, `name`, `attrs`, as journalled at P5).
    - The order is enforced: `go` needs a ticket and `done` needs a `go`. A report out of order changes nothing and answers `reason: not_in_order`. A stuck placement goes back to the state it came from at its next report.
    - `done` reports a fact and always lands: what it can't claim is noted on its event (`not_the_copy`, `not_claimed`, `not_annexed`, `outcome_reported`, `name_reported`), including attributes no column can hold, an `outcome` this service doesn't know or a `name` that can't be one (an edge of another version), and a purge asked for since `go`. Only a step out of order or a placement that isn't this edge's answers otherwise. A hold is never shortened. `already_there` at the origin is the file back, held, as `done` below.
    - `done` back under its own name at its origin makes the annex's file present again, as the copy it has (no recopy), held until `hold_until` (removal tickets answer `not_yet: held`), its age counted from now, with an annex event `returned`. A tombstoned file comes back too, unless its name has been taken since. If `attrs.size` isn't the version's, the file isn't that copy: nothing is claimed, and the next scan sees a change.
    - `done` under any other name, in a folder an annex covers, makes a new annexed file, held. In the file's own dataset it references the version's blob, verified, so it isn't copied again.
    - Until `done`, an `arrived` or `changed` for a path the placement may be making is `rejected: placement_in_progress`; the edge reports it again later.
    - **A replace at the origin (A3.4, decisions 82 to 85; `annex/replace.py`).** With `overwrite_keep_version`, at the file's own name in its original folder, a ticket may carry `replace`: what the file there must still be (`version_id`, `nas_file_id`, `size`, `mtime_ns`, `ctime_ns`, `crc64`, `hidden_name`). It does only when that file is the annex's current version as copied, verified, read back in Azure within 24 hours (else a read is queued and the ticket waits up to 30 minutes, then goes without it), with no legal hold, no open removal and no other placement replacing it. The file there already holding the placed bytes is `same`: the ticket says `same_content`. The edge asks `replace_go` before it hides the old file, and `replace_delete_go` with `phase: place` after the grace (before the new file's link) and `phase: delete` before the delete; each checks every gate again and answers `{proceed, reason, hold}`. A `hold` (paused, the grace, a read under way) is waited out; any other refusal is definite, and the old file is put back. A gate of the placement itself that fails there ends it, as at `go`. The delete phase also sends `name`, the new file's final name; any but the file's own is refused (`not_origin_name`), and the edge keeps the old file. A question is refused when the running rule no longer says `overwrite_keep_version` for the file and target (`rule_changed`). The delete phase needs the place phase's yes first. The edge reports `replace_hidden` (`hidden_name`), `replace_put_back` (`reason`), `replace_aside` (`name`, `reason`: the old file kept as `<name>-replaced-<UTC time>`, with a `replace_kept_aside` problem naming both), `replace_gone` (the old file went by a user's hand; the edge deleted nothing) and `replaced`; these are recorded also after the placement ended, each once and only in its order: `replace_hidden` after a `replace_go` yes, `replaced` after a delete-phase yes, `replace_put_back`, `replace_aside` and `replace_gone` from the offer or the hide. A replay is a no-op; a fact out of order answers `reason: not_in_order` and changes nothing. After a `replace_go` yes, a refusal (of `replace_go` asked again, or of the ticket's offer) records only why: the edge may have the file hidden, and its fact closes the replace. A question reads the running rule under a shared lock an approval takes exclusively, so an approval waits for the answer. While a replace may have the file hidden (hidden, or offered and either under way or given a `replace_go` yes), its removals are refused (`replace_open`) and its departures rejected, also after the placement ended. A `done` saying `replaced` without the edge's `replaced` or `replace_gone` is stored as `replace_unconfirmed` and claims nothing. Every question and report is audited with the replaced version's id and CRC. `done` outcomes gain `replaced` and `replaced_old_kept`; a `restored_name` after a refused replace is stored as `overwrite_refused_<why>`. An older version under the file's own name is a new head version referencing the placed version's blob, the replaced one kept. While the old file is hidden, a `left` for it is `rejected: placement_in_progress`, and the edge doesn't send one.
    - A file brought back to its origin more than three times in 30 days is pinned (`refused: loop_breaker`, audited) until an Owner clears it (portal API). Copies into a share or under another name don't count; a replace at the origin does (A3.4).
    - A placement that ended answers every step with `proceed: false` and its `state`. The edge then cleans up what it made. `done` always lands while the placement is open: a name the ticket no longer covers, or a catalog clash, is noted and nothing is claimed for the file.
- **Return rules (phase A3.1).** A rule set may hold `recall_completed` rules (where a recalled file is placed). They are the service's to evaluate; the edge skips them, keeping their ids unique, and refuses any other trigger it doesn't know. A placement reaches the edge only as a ticket (phase A3.3).
- `GET /v0/edges/{id}/annexes/{annex_id}/records?after=&limit=`: the catalog's record of the annex's live files in path order (NAS file id, attributes as last seen and as copied, verified, whether the copy's location is committed and verified (`copy_ok`), and `refused`: the read-back refused this copy and the service wants another), for the reconciliation. A reconciliation the service asked for copies again only the `refused` records; one on the unit's schedule, or asked for by an operator (SIGUSR2), also copies those that aren't `copy_ok`, as before. None copies a record that says `copy_stopped`: an ArchiveAdmin stopped copies of those bytes again (portal-api.md §5.23), until a version verifies or they resume it; a file changed on the NAS is still copied, as a new copy.

## 5. The event feed

The edge keeps its local metadata replica current by pulling one ordered feed.

`GET /v0/edges/{id}/feed?after=<cursor>&limit=<1..1000>&wait=<0..30>`

```json
{"events": [ … ], "next": "<cursor>", "reset_required": false}
```

- **Cursor:** an opaque string. Start with `"0.0"`. Always continue from `next`, even when `events` is empty: `next` can advance past events this edge doesn't receive.
- **Long poll:** with `wait > 0` and nothing new, the service holds the request up to `wait` seconds.
- **Scope:** events for datasets that this edge's shares present, plus events addressed to this edge.
- **Order:** total, and stable. The service orders by `(transaction id, event id)` and only serves events whose transaction is below the oldest running transaction. Therefore:
  - a transaction that commits late can never be skipped;
  - an event, once served, is never followed by an earlier one.

  The order is by transaction, not by causality across transactions. A child's create can arrive before its parent's, and a directory's tombstone before the tombstones of children removed in other transactions. So the edge:
  - **parks** an upsert whose parent it doesn't have yet, and applies it when the parent arrives (or drops it if a tombstone for it arrives first);
  - on a directory tombstone that fails with `ENOTEMPTY`, keeps the directory and **retries** the `rmdir` as the remaining children's tombstones arrive.

  The cost: one long-running transaction in the catalog delays the feed. The service keeps transactions short and alerts on lag.
- **Delivery:** at least once. Every event carries `entity_version`, which grows for each entity. An edge that already has that version or a newer one ignores the event. Events are thin in the catalog. The service fills in the **current** state of the entity when it serves them, so an old event may arrive with newer data, and that is fine.
- **Echoes:** an event caused by this edge's own `changes` op carries `origin: {edge_id, op_id}`, so the edge can skip it.
- **Ack:** `POST …/feed/ack {"cursor": "…"}` records progress for monitoring and retention. The edge's own durable cursor is authoritative.
- **Retention:** at least **7 days** (weekly partitions, created eight weeks ahead; two weeks kept). A cursor older than the retained range gets `reset_required: true`, and the edge re-bootstraps (section 6).
- **Restored catalog:** cursors are transaction ids, and a point-in-time restore hands out transaction ids again. So after a restore the service starts a new feed **incarnation** (`catalog.feed_restored()`, migration 0019). Cursors from then on have a third part (`"<txid>.<event>.<incarnation>"`), and a cursor from another incarnation gets `reset_required: true`, like one beyond retention. `"0.0"` stays valid.

### Event types

| `type` | Key | Payload (current state) |
|---|---|---|
| `file.upsert` | `file_id` | `file_id, parent_id, name, kind, symlink_target, size, mtime_ns, uid, gid, mode, owner_sid, acl_id, state, resident_bytes, version_id (the content version), crc64nvme, location {backend_id, backend_kind, container, object_key, backend_version_id, logical_tier, storage_class, archived, restore_state, restore_expires_at}, stub {version_id, resident_bytes} \| null, dataset_id, entity_version`. Tier moves and restore progress bump the file's `entity_version`, so `archived` and `restore_state` never go stale |
| `file.rename` | `file_id` | as `file.upsert`; `parent_id` and `name` changed. A directory rename is one event on the directory |
| `file.tombstone` | `file_id` | `file_id, parent_id, name, kind, entity_version`. The catalog refuses to tombstone a non-empty directory, but across transactions a directory's tombstone can arrive before its children's (see Order above) |
| `file.conflict` | `file_id` | `op_id, reason, current` (the catalog's state). The edge's change lost |
| `acl.upsert` | `acl_id` | `acl_id, dataset_id, sha256, owner_sid, group_sid, mode, entries` |
| `share.upsert` | `share_id` | `share_id, dataset_id, name, root_dir_id, protocols, nfs_sec, nfs_clients, root_squash, trust_level, host_trust_override, nfs_export_id, smb_encrypt, owners/writers/readers_group_sid, on_open, wait_threshold_minutes, quiet_s, rewind {depth_days, max_days, labels}, worm {enabled, mode, retention_days, locked_at, lock_deadline}, entity_version`. `quiet_s` is the share's quiet period, 10 to 600 s; null or absent means the edge's default for the share (decision 67) |
| `share.delete` | `share_id` | `share_id`. The share was removed: the edge stops serving it. Its dataset and files stay (M6) |
| `dataset.upsert` | `dataset_id` | `dataset_id, name, backend {kind, private_network}, storage_layout, migration_state, entity_version` |
| `recall.state` | `recall_id` | `recall_id, file_id, version_id, state, eta, requesters [{sid, via, proof}], error, read credential (when downloading), entity_version` |

**Bytes and the feed:** the feed carries metadata, never file content.

## 6. Bootstrap and re-bootstrap

- **Small datasets:** page through `GET /v0/edges/{id}/snapshot?dataset_id=…[&dir_id=…]&after_file_id=…&limit=…`. Rows have the `file.upsert` shape, in `file_id` order. Parents may come after their children. The first page carries `as_of`, a feed cursor. After the last page, the edge reads the feed from `as_of`. Events already reflected are no-ops, thanks to `entity_version`.
- **Large datasets (10⁸ rows):**
  1. `POST /v0/edges/{id}/snapshot-exports {dataset_id}` starts an export job (`202`, with `export_id`, `state: running` and `as_of`). A worker writes zstd-compressed NDJSON parts (about 1M rows each) to a blob container.
  2. `GET …/snapshot-exports/{export_id}` returns `{state, as_of, parts: [{url (read SAS, 24 h), rows, bytes, sha256}]}`. `state` is `running`, `ready` (with `parts`), `failed` or `expired`.
  3. The edge downloads each part, checks `bytes` and `sha256`, loads it, then reads the feed from `as_of`.

  Details (built; `service/src/omnifile_service/exports.py`):
  - **Lines:** each is one object with one key: `{"dataset": …}`, then `{"share": …}` lines, then `{"acl": …}` lines, then `{"file": …}` lines in `file_id` order. The rows have the paged snapshot's shapes. The first three kinds appear in the first part only. `rows` counts a part's lines.
  - **`as_of`** is taken when the export starts, before any row is read.
  - **Where:** the `omnifile-exports` container of the dataset's primary account, never its data container (whose change feed is the catalog's). A dataset on an S3 backend has no export area: `POST` answers `409 layout_conflict`, and the edge pages through `snapshot`.
  - **Kept** 48 hours. Then the worker deletes the parts and the export reads `expired`.
- ACLs, shares and the dataset row come first in both forms, so that file rows can refer to them.
- The edge loads the snapshot into a fresh replica and, on a live edge, diffs it against the current one, so existing inodes and NFS handles survive (`edge-metadata.md` §5.2).

### Reconciliation digest

`GET /v0/edges/{id}/digest?dataset_id=…[&dir_id=…]` returns `{as_of, live_rows, digest}`:
- `digest` is the XOR, over live rows (`deleted_at IS NULL`), of the first 8 bytes of SHA-256(`"<file_id>:<entity_version>"`), as 16 hex digits. It is order-independent, so the edge computes the same value from its replica.
- With `dir_id`, it covers that directory's direct children; without, the whole dataset.
- It is computed when asked, and `as_of`, the feed cursor, is taken first.
- Service-side clocks (`last_read_at`) bump no `entity_version`, so they leave the digest alone.

**Cursors are ordered within an incarnation.** `<txid>.<event>` compares as a pair of numbers. Cursors of different incarnations never compare. An edge may use this to tell whether it has applied the feed up to an `as_of`.

**Built (2026-10-15).** The edge checks each dataset every `[feed] reconcile_s` (default a day; `feed_loop::reconcile_once`). Writes racing the check change the service's digest, so a difference counts only when all of these hold:
- two service digests `reconcile_quiet_s` apart (default 60 s) agree;
- the edge has applied the feed past the second one's `as_of`;
- a third service digest, read after the edge computed its own, still agrees.

Then nothing was written while the edge caught up, and a difference is divergence. A busy dataset is skipped until the next round.

On divergence the feed loop re-snapshots before its next page, never alongside one (section 6 above: a bulk export, or pages). Narrowing by directory first (`dir_id`) is a later optimisation. The whole-dataset digest is a full scan of the dataset's rows, so the edge asks for it at most three times per check, once a day by default.

## 7. Versioning of the API

- The path carries the major version (`/v0`). Within a major version, changes are additive only:
  - new optional request fields;
  - new response fields;
  - new event types;
  - new error codes.

  Clients ignore what they don't know, including unknown event types (after advancing the cursor).
- Each request carries `OmniFile-Edge-Version: <semver>`.
  - The heartbeat returns `min_edge_version`.
  - Below it, every call except heartbeat, certificate renewal and feed returns `426 edge_upgrade_required`, so an old edge keeps its replica current but stops changing anything.
- The service serves the current and the previous major version, so the service and the edges can be upgraded on separate schedules.
- **v0 is pre-release.** It may change incompatibly until the M1 exit; v1 is frozen at M1 exit.

## 8. Idempotency

- Every `POST` needs `Idempotency-Key`: 16–128 characters. A UUIDv7 is recommended. For `changes`, the batch key, plus each op's own `op_id`.
- The service stores `(principal, key) → (request hash, status, response)` for 24 hours (`catalog.idempotency_key`).
  - **Same key, same body:** the stored response is returned, with `Idempotent-Replayed: true`.
  - **Same key, different body:** `422 idempotency_key_reused`.
  - **Same key while the first request is still running:** `409 request_in_progress`, retry later.
- Upload `complete`, stub `commit` and recall `filled` are also idempotent by their natural keys (`upload_id`, `lease_id`, `recall_id`).

## 9. Errors

RFC 9457 problem details:

```json
{"type": "urn:omnifile:error:stub_not_allowed", "title": "Stub not allowed",
 "status": 409, "code": "stub_not_allowed", "detail": "keep_online hold 812",
 "retryable": false, "retry_after_s": null, "request_id": "…"}
```

| HTTP | `code` | Retry? | Meaning |
|---|---|---|---|
| 400 | `invalid_request` | no | Schema violation |
| 401 | `certificate_required` / `certificate_invalid` | no | No or bad client certificate |
| 401 | `enrolment_token_invalid` | no | `POST /v0/enrol`: the token is unknown, used or expired |
| 403 | `caller_not_allowed` | no | `POST /internal/v0/sign`: the caller is not `api`'s managed identity (`/internal/v0/crl`: not the worker's) |
| 403 | `edge_mismatch` / `edge_revoked` | no | Certificate doesn't match `{edge_id}`, or the edge is revoked |
| 404 | `not_found` | no | Unknown id |
| 409 | `version_conflict` | after a feed catch-up | `base_version_id` or `entity_version` is stale |
| 409 | `directory_not_empty` | no | Catalog error OF001 |
| 409 | `worm_violation` | no | OF002: retention, legal hold or a locked policy |
| 409 | `stub_not_allowed` | no | Stub preconditions failed |
| 409 | `request_in_progress` | yes | Same idempotency key still running |
| 409 | `aws_key_mismatch` | no | `POST …/aws-certificate`: the CSR is not for the edge's pinned Roles Anywhere key (decision 50) |
| 410 | `beyond_rewind_depth` | no | As-of time older than the share's depth |
| 410 | `cursor_expired` | no | Same as `reset_required` |
| 422 | `idempotency_key_reused` | no | Same key, different body |
| 422 | `acl_unknown` / `name_invalid` / `dependency_failed` | no | Rejected op |
| 426 | `edge_upgrade_required` | no | Below `min_edge_version` |
| 429 | `throttled` | yes, after `Retry-After` | Per-edge rate limit |
| 400 / 403 / 409 / 410 / 422 | `intent_mismatch`, `intent_invalid`, `not_intended_recipient`, `forbidden`, `intent_already_used`, `intent_expired`, `action_not_allowed` | no | `POST /internal/v1/teams/intents` only; meanings and the relay's reaction in `teams.md` §4.4 |
| 503 | `unavailable` | yes, with backoff | Catalog or backend unavailable |

Catalog trigger errors (SQLSTATE class `OF`, see migration 0003) map to API codes:

| SQLSTATE | HTTP | `code` |
|---|---|---|
| OF001 | 409 | `directory_not_empty` |
| OF002 | 409 | `worm_violation` |
| OF003 | 409 | `policy_not_approved` |
| OF004 | 409 | `layout_conflict` |
| OF005 | 500 | `actor_required` (a service bug: the actor wasn't set) |
| OF006 | 409 | `explicit_tier_set` |
| OF007 | 409 | `version_conflict` (a rename or attribute change cited bytes that are no longer current) |
| OF008 | 500 | not mapped: a service bug (a delete queued for an object in a migration source, which is read-only to OmniFile; migration 0026, `catalog-seeding.md` section 10) |

The edge retries `retryable` errors with jittered exponential backoff (1 s up to 5 min), reusing the same `Idempotency-Key`.

## 10. Timeouts and limits

| Call | Server time budget | Limits |
|---|---|---|
| `feed` | `wait` + 5 s | 1,000 events per page |
| `changes` | 10 s | 1,000 ops, 4 MiB body |
| `recalls` (create or join) | 2 s | The FSAL never blocks a Ganesha thread on it; the daemon answers DELAY or EIO from its own state meanwhile |
| `credentials` | 1 s | Signed locally, no Azure call |
| everything else | 5 s | 4 MiB body |

## 11. When the service is unreachable

The edge is a cache with a journal. Losing the service must never lose data, and never make a stub read return zeros.

| Activity | Behaviour while the service is down |
|---|---|
| Reads of resident files | Unaffected |
| Reads of cloud-resident (not archived) files | Continue while a cached read SAS is valid (`cache_until`, up to about 1 h). S3 reads continue as long as Roles Anywhere works, since they don't need the service |
| Reads that need a recall | No new recalls can start. Per share policy: `wait` keeps answering DELAY/JUKEBOX, so clients wait; `fail` and `auto` return EIO. The `.RECALLING.txt` note says "service unreachable". Requests are journalled and sent on reconnect |
| Writes | Accepted into the landing zone and journalled. Uploads that already hold a write SAS continue until it expires; new uploads wait. At the landing-zone high-water mark (90% by default) new writes get `ENOSPC`, never a silent drop |
| Stubbing | Stops: `stub/prepare` needs the service, so no new holes are punched |
| Namespace changes, ACL reports, audit | Journalled locally, sent in order on reconnect, made safe to resend by `op_id` and idempotency keys |
| Feed | Resumes from the edge's durable cursor; `reset_required` if the gap exceeds retention |
| Certificate | Renewal is retried from day 20; a 30-day certificate gives 10 days of outage margin before re-enrolment |

**Reconnect order:**
1. heartbeat;
2. feed catch-up to the current horizon;
3. then the journalled `changes`, followed by uploads, recalls and audit.

Catching up first means conflicts are detected against the current catalog, not a stale one.

## 12. Assumptions about the edge replica (for `docs/design/edge-metadata.md`)

1. The replica (SQLite in the daemon) is keyed by `file_id`, with `parent_id` + `name` (hierarchical), and stores `entity_version` per entity. Every catalog object is materialised as a sparse xfs placeholder, marked by one binary `trusted.omnifile` xattr (`fsal-namespace.md`). The feed's correctness depends on the edge ignoring older or equal versions.
2. The edge keeps its own durable feed cursor and its own outbox of outgoing ops, with `op_id` (edge id + monotonic counter) unique per edge and stable across restarts. The cursor is the opaque string `next`.
3. There is no recursive delete event. The edge parks orphan upserts until their parent arrives, and retries `rmdir` on `ENOTEMPTY` (section 5, Order).
4. The edge does not replicate version history; it uses `asof` and version-specific credentials on demand.
5. `share_id`, `dataset_id`, `file_id` and `version_id` are 64-bit in the catalog; the marker and the socket protocol carry them as u64.
6. The edge materialises placeholders from `file.upsert`. For S3- and Azure-resident files with no local copy (`state = remote`), it uses the uid, gid, mode and ACL the service supplies. The service fills these from the source registration (decision 28), and the edge never invents them.
7. Names that are not valid UTF-8 stay local-only on the edge: they are never uploaded or stubbed, and the edge reports them as `name_invalid`.
8. `mtime_ns` is the only time the edge needs from the catalog. The policy clocks (`last_read_at`, `written_at`) are service-side.
9. The edge reports reads for the `last_read` clock through `POST …/reads`, coalesced to one entry per file per hour (section 4.7; `edge-metadata.md` §12 item 15).

## 13. Teams button intents (relay → service)

This is not an edge call, but it uses the same service and the same catalog, so it is specified here. **`docs/contracts/teams.md` §4 is authoritative**; this is a summary. The Teams relay is the only public endpoint (`AGENTS.md`). It forwards a button click inward over the private network:

`POST /internal/v1/teams/intents`, with mutual TLS. The relay's client certificate comes from the OmniFile CA, with SAN `urn:omnifile:relay`. Its SHA-256 fingerprint must be one of `OMNIFILE_TEAMS_RELAY_CERT_SHA256` (pinned; required outside dev mode). That certificate may call this path and nothing else (`403 caller_not_allowed` elsewhere). Edge certificates, and missing or invalid ones, get `403 caller_not_allowed` here, never 401. Headers: `Idempotency-Key: <intent_id>`.

```json
{"intent": "<compact JWS, as it was on the card>",
 "intent_id": "<uuid>", "action": "recall",
 "clicker_aad_object_id": "…", "tenant_id": "…",
 "activity_time": "…", "received_at": "…"}
```

- `intent_id` and `action` are copied from the intent so the relay can log them. The service re-verifies the JWS, and refuses the call (`400 intent_mismatch`) unless both match the JWS claims.
- `activity_time` comes from the Teams activity; `received_at` is when the relay received it. Both must be within 10 minutes of the service's clock (`400 invalid_request`).

- **What the relay has already checked:** the Bot Framework JWT on the incoming activity, the tenant, the intent and the clicker. It forwards the signed intent exactly as the card carried it, and never builds one itself.
- **What the service checks, in order:**
  1. the body and its times;
  2. the JWS: **Ed25519** (`alg: EdDSA`, `typ: omnifile-intent+jwt`), against public keys only (`OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS`; the private key is a Key Vault secret only the worker reads). A bad signature, unknown `kid`, or wrong `typ`/`iss`/`aud` is `403 intent_invalid`; expired is `410 intent_expired`;
  3. `intent_id` and `action` match the JWS (`400 intent_mismatch`);
  4. the tenant is the client's tenant (`403 intent_invalid`);
  5. the clicker is the card's recipient (`403 not_intended_recipient`);
  6. a `catalog.teams_intent` row matches, not revoked by a newer card (`403 intent_invalid`);
  7. the intent is consumed once: `UPDATE … WHERE used_at IS NULL`. A repeat gets `409 intent_already_used`, even with a valid signature;
  8. the recipient can still read the file (decision 28; `403 forbidden`).

  Steps 1-5 run before the idempotency store and are never stored; only steps 6-9 are.
- **Then** it does the action (recall, keep online, track) in the transaction of step 7, sends `notify` with a 6-second delay, and returns `200 {"result": "queued" | "done", "recall_id"?, "card"}`. `card` is an Adaptive Card without `Action.Execute` buttons, which the relay shows in place of the clicked card; the worker sends the full card afterwards. `422 action_not_allowed` when the action no longer applies (Track on a finished recall, Recall when the file is online).
- **Never 401:** the relay treats 401 as transient. `429 throttled` above 10 clicks per minute per recipient.
- `Idempotency-Key` is the `intent_id`, and the stored reply is keyed on the `intent` field only, so a second relay replica forwarding the same click gets the first reply.
- Every outcome, allowed or refused, is audited with the clicker's identity (`teams.intent.accepted`, `teams.intent.refused`).

## 14. Endpoint index

All edge paths are below `/v0/edges/{edge_id}`, except enrolment and the relay call. A test (`service/tests/test_control_api_contract.py`) checks this list against the OpenAPI file.

| Method and path | Section |
|---|---|
| `POST /v0/enrol` | 2 |
| `POST /v0/edges/{edge_id}/certificate` | 2 |
| `POST /v0/edges/{edge_id}/aws-certificate` | 2 |
| `POST /v0/edges/{edge_id}/heartbeat` | 4.7 |
| `GET /v0/edges/{edge_id}/feed` | 5 |
| `POST /v0/edges/{edge_id}/feed/ack` | 5 |
| `GET /v0/edges/{edge_id}/snapshot` | 6 |
| `POST /v0/edges/{edge_id}/snapshot-exports` | 6 |
| `GET /v0/edges/{edge_id}/snapshot-exports/{export_id}` | 6 |
| `GET /v0/edges/{edge_id}/digest` | 6 |
| `GET /v0/edges/{edge_id}/asof` | 4.5 |
| `POST /v0/edges/{edge_id}/changes` | 4.1 |
| `POST /v0/edges/{edge_id}/uploads` | 4.2 |
| `POST /v0/edges/{edge_id}/uploads/{upload_id}/complete` | 4.2 |
| `POST /v0/edges/{edge_id}/uploads/{upload_id}/abort` | 4.2 |
| `POST /v0/edges/{edge_id}/credentials` | 3 |
| `POST /v0/edges/{edge_id}/files/{file_id}/stub/prepare` | 4.3 |
| `POST /v0/edges/{edge_id}/files/{file_id}/stub/commit` | 4.3 |
| `POST /v0/edges/{edge_id}/files/{file_id}/stub/skip` | 4.3 |
| `POST /v0/edges/{edge_id}/recalls` | 4.4 |
| `POST /v0/edges/{edge_id}/files/{file_id}/filled` | 4.4 |
| `POST /v0/edges/{edge_id}/recalls/{recall_id}/filled` | 4.4 |
| `POST /v0/edges/{edge_id}/recalls/{recall_id}/failed` | 4.4 |
| `POST /v0/edges/{edge_id}/acls` | 4.6 |
| `POST /v0/edges/{edge_id}/acl-scans` | 4.6 |
| `POST /v0/edges/{edge_id}/acl-scans/{scan_id}/complete` | 4.6 |
| `POST /v0/edges/{edge_id}/reads` | 4.7 |
| `POST /v0/edges/{edge_id}/audit` | 4.7 |
| `GET /v0/edges/{edge_id}/annexes` | 4.8 |
| `POST /v0/edges/{edge_id}/annex-events` | 4.8 |
| `GET /v0/edges/{edge_id}/annexes/{annex_id}/records` | 4.8 |
| `GET /v0/edges/{edge_id}/annex-dry-runs` | 4.8 |
| `POST /v0/edges/{edge_id}/annex-dry-runs/{run_id}` | 4.8 |
| `POST /v0/edges/{edge_id}/annex-removal-tickets` | 4.8 |
| `POST /v0/edges/{edge_id}/annex-removals/{removal_id}` | 4.8 |
| `POST /internal/v1/teams/intents` | 13 (relay, not edge) |
| `POST /internal/v0/sign` | 2 (`api` → `enrol`, not edge: renewals, Roles Anywhere certificates, and `api`'s own server certificate) |
| `POST /internal/v0/crl` | 2 (`worker` → `enrol`, not edge) |
