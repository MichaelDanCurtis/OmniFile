# OmniFile service: Azure infrastructure (Bicep)

Infrastructure for one client deployment of the OmniFile service, in the client's subscription (decision 17). It has been **compiled and linted only**: nothing in it has been deployed, and no Azure resource has been created from it. Anything not yet checked against a real deployment is marked `// VERIFY:` in the source, and every one is listed in "What a real deployment must confirm" below.

```
deploy/azure/check.sh             # build main.bicep and every .bicepparam, lint main.bicep and each module, check the allow-lists are required, check test.bicepparam and the two sizes, test relay-ranges.sh and whatif.sh
deploy/azure/test-sizes.py MAIN.json [--evaluate]   # just the size checks (deploymentSize), on a compiled main.bicep
deploy/azure/test-relay-ranges.sh # just the offline tests of relay-ranges.sh
deploy/azure/test-whatif.sh       # just the offline tests of whatif.sh (a fake az)
```

It uses the Bicep CLI (`brew install azure/bicep/bicep`), or `az bicep` when only the Azure CLI is there; it never signs in. **Last run 2026-11-03, Bicep CLI 0.48.1:** `main.bicep`, `main.example.bicepparam` and `test.bicepparam` build with no errors and no warnings, `main.bicep` and all 17 modules lint clean, and a parameters file without the three ingress allow-lists, or with them empty, fails to build. The parameters files are built in a copy of this folder whose `relay-allowed-cidrs.json` `relay-ranges.sh` made from the fixture; the built `relayAllowedCidrs` is exactly its list, the example does not build without the file, and the script's 19 offline tests pass. `test.bicepparam` is built with stand-in environment values and the real image lock, and its values are checked. `whatif.sh`'s 16 offline tests pass. **2026-11-04:** `small.bicepparam` and `standard.bicepparam` build too, and `test-sizes.py` passes (`deploymentSize`: standard is exactly the sizing before the parameter, small is what `docs/milestones/results/wp16.md` prices, a set parameter overrides its size, and small refuses a dedicated verifier profile and high availability on its Burstable server, evaluated with the service's ARM evaluator). `service/tests/test_deploy_env.py` compiles the template and checks every app's command and settings (it fails on the template before this change, whose apps all ran `api`).

Deploy at resource-group scope. Take the relay's ranges, preview, then deploy what was previewed:

```
deploy/azure/relay-ranges.sh --location <region> --out deploy/azure/relay-allowed-cidrs.json
deploy/azure/whatif.sh -g <rg> -p main.example.bicepparam            # read-only; prints the changes and "digest: sha256:..."
deploy/azure/whatif.sh -g <rg> -p main.example.bicepparam --deploy --approve sha256:...   # what-if again; deploys only if unchanged
```

`whatif.sh` compiles the template and the parameters itself and gives az the compiled files, so the digest covers exactly what is deployed. `test.bicepparam` is a small (`deploymentSize = 'small'`), removable test deployment (Burstable PostgreSQL, one WORM share with a 1-day unlocked policy and no lock, a purgeable vault, run tags), which reads every account-specific value from the environment (its header lists them) and the images from `deploy/images/images.lock.json`.

## What it creates

| Piece | Resources |
|---|---|
| Network | One VNet and four subnets, **each with its own NSG**: `snet-cae-internal`, `snet-cae-relay`, `snet-postgres`, `snet-private-endpoints`. See "Network isolation". |
| Internal environment | Container Apps environment (workload profiles, **internal**) with apps `api` (edge, mutual TLS on a TCP ingress, port 8443), `enrol` (edge enrolment, no client certificates), `web` (portal and partner API, no client certificates), `worker`, and the verifier **job**. Each runs its own command (`appCommands`: `python -m omnifile_service.<app>`, the verifier the worker's), whatever the image's own. A private DNS zone for the environment's default domain. |
| Relay | A second, small **external** Container Apps environment with the `relay` app. The only public endpoint. |
| Teams bot | An Azure Bot (`deployTeamsBot`), single tenant, whose identity is the worker's managed identity (`UserAssignedMSI`), its messaging endpoint the relay's `/api/messages`, with the Teams channel. |
| Database | PostgreSQL Flexible Server (PG 18 by default, 17 allowed). Private access through a delegated subnet. Entra authentication only, so no password administrator. HA optional. `azure.extensions` allows `PG_CRON`, `PGCRYPTO`, `PG_TRGM`, `BTREE_GIST`, and `pg_cron` is preloaded. PGMQ is not in the list: the service's migrations install it as plain SQL (decision 27). |
| Storage | Non-WORM: a **live** account (ZRS by default, default tier **Smart**) and an **archive** account (LRS or GRS). WORM: one LRS or GRS account **per WORM share**, with versioning and version-level immutability. See "Storage layouts". |
| Key Vault | One vault (RBAC, purge protection) with a non-exportable EC P-384 CA key (HSM-backed on the premium SKU). |
| Identities | Six user-assigned managed identities, one per workload: `api`, `enrol`, `web`, `worker`, `verifier`, `relay`. |
| Roles | Data-plane and control-plane roles, listed below. Four custom roles scoped to the resource group. |
| Monitoring | A Log Analytics workspace. Diagnostic settings on every storage account, PostgreSQL and Key Vault. Container Apps logs go to the same workspace. Application Insights on that workspace for the service's metrics, with alert rules (decision 37; `modules/app-metrics.bicep`). Optionally, a data collection rule and the Azure Monitor Agent on Arc-connected edges (`modules/edge-monitoring*.bicep`). |

## Decision: where the relay's public ingress lives

**A second, external Container Apps environment, in its own subnet of the same VNet.** One environment cannot hold both an internal-only `api` and a public relay.

What the Container Apps docs say, which this follows:
- The accessibility level is set **on the environment**, at creation, and cannot be changed afterwards. An internal environment "has no public endpoints" and is reached through an internal load balancer (<https://learn.microsoft.com/en-us/azure/container-apps/networking#accessibility-level>).
- An app's `ingress.external` only says whether it is published at the environment's boundary. On an internal environment, `external: true` means "reachable from the virtual network", not from the internet. `external: false` means "only other apps in the same environment", and other clients in the VNet get a 404 (<https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview#how-ingress-visibility-interacts-with-the-environment-type>).
- So `api` uses `external: true` on the internal environment (that is how the edge appliances, over ExpressRoute or VPN, reach it), and the relay lives in an environment created external.

How the relay reaches `api` privately: both environments are in the same VNet, and `modules/dns-zone-for-env.bicep` creates a private DNS zone named after the internal environment's default domain, linked to that VNet, with `*` and `@` records pointing at the environment's internal IP. The relay calls `https://<api fqdn>` and the traffic never leaves the VNet. The Container Apps docs say NSG or firewall inbound lockdown is not supported on an external workload-profile environment, because public traffic enters through an IP in the managed resource group (<https://learn.microsoft.com/en-us/azure/container-apps/firewall-integration>). So inbound filtering is done with the relay app's **ingress IP restrictions** (`relayAllowedCidrs`, required: see "Closed by default"). The NSG on the relay subnet controls what the relay may reach (next section).

Why not App Service with VNet integration: it would work, but it adds a second hosting model, a second deployment pipeline and an always-on plan for one small app. The external environment uses the same images, identity model and logging as everything else, and costs little on the Consumption profile.

## Apps and mutual TLS (`ARCHITECTURE.md` section 1)

**`api` terminates mutual TLS itself (threat model T5).** With `apiIngress = tcp` (the default) `api` has a TCP ingress on `apiTcpPort` (8443), which passes connections through. `api` (`OMNIFILE_API_CLIENT_AUTH=direct_tls`) requires a client certificate from the edge CA in its own handshake and refuses an `X-Forwarded-Client-Cert` header. Its server certificate comes from the same CA: each replica makes a key in memory and asks `enrol` to sign it (`purpose: api_server`, for `api`'s FQDN, which `enrol` gets as `OMNIFILE_ENROL_API_SERVER_NAMES`), then renews it in place. The enrolment answer tells each edge to pin the edge CA for `api` (`api_server_ca_sha256`). Before, `api` sat behind the HTTP ingress and believed the forwarded certificate from the whole internal subnet, so any other app's replica could name any edge.

An HTTP ingress with forwarded client certificates is **not offered** (`apiIngress` allows only `tcp`; review of #34): `api` runs in the shared internal environment, whose ingress proxy is in the subnet every other app runs in, so no proxy range would be safe. It can return when `api` has an environment of its own. The code keeps forwarded mode for other hosting, and fails closed twice: it needs `OMNIFILE_API_TRUSTED_PROXIES` and refuses a range wider than /16, and it refuses to start when a proxy range overlaps `OMNIFILE_API_APP_SUBNETS`, the subnets the other apps run in, which the template passes.

`clientCertificateMode` (HTTP ingress only) is a property of the app's ingress (`properties.configuration.ingress.clientCertificateMode`, checked by the compiler against `Microsoft.App/containerApps@2025-07-01`; Microsoft's example is at <https://learn.microsoft.com/en-us/azure/container-apps/client-certificate-authorization>). It applies to **every request** to that app, and the platform does **not** validate the chain: the app checks `X-Forwarded-Client-Cert` against the OmniFile CA. So the hosting has one app per trust model:

| App | Callers | Client certificate | Identity can reach |
|---|---|---|---|
| `api` | edge appliances, and the relay | required by `api` itself (TCP ingress); `require` at an HTTP ingress | catalog; blobs (mints SAS); Key Vault: its own lease key only (phase 2) |
| `enrol` | a new edge that has no certificate yet | `ignore` (limit callers with `enrolAllowedCidrs`) | catalog; Key Vault: **Crypto User on the CA key only** |
| `web` | browsers (portal), Prizmanyx (partner API) | `ignore` (Entra sign-in in the app; limit callers with `webAllowedCidrs`) | catalog; no storage role; Key Vault: its own cursor key only (phase 2) |
| `worker` | nobody (no ingress) | none | catalog; blobs through a custom role |
| verifier job | started by the worker | none | catalog; blobs read-only |
| `relay` (other environment) | Teams | `ignore` (Bot Framework JWT) | the internal `api`, and its own three Key Vault secrets |

**Certificate renewal flow.** An edge renews at `api` (mutual TLS). `api` has no sign right on the CA key, so it forwards the CSR to `POST /internal/v0/sign` on `enrol`, over the internal environment (same environment, so no NSG or ingress rule blocks it: the internal NSG allows intra-subnet traffic), authenticated with an Entra token that `api` requests with its managed identity for the audience in `OMNIFILE_ENROL_AUDIENCE`. `enrol` accepts it only from `api`'s object id (`OMNIFILE_ENROL_ALLOWED_CALLER_OID`), so `enrol` stays the only signer. Edges reach `enrol` for enrolment but must be refused on `/internal/*`: Container Apps cannot filter by path per app, so `enrol` enforces that itself. The internal subnet is always added to `enrolAllowedCidrs` so `api` is not locked out (VERIFY the source address of app-to-app calls).

`web` and `enrol` can each be switched off (`deployWeb`, `deployEnrol`).

**Before the second phase** an app whose settings aren't there yet starts **not ready** instead of exiting: `api` and `enrol` until the CA exists (step 7), `web` until `webClientId` is set (step 6), the relay until its certificate and intent keys are there (steps 7 and 8). Each logs one line saying what is missing, and `/healthz` and every other path answer 503. The readiness probe is `/healthz` and the liveness probe the port (`modules/app.bicep`), so such a replica takes no traffic and is not restarted; `az containerapp exec` into `enrol` works for the bootstrap (VERIFY that exec reaches a replica that isn't ready).

## Network isolation

| Subnet | Inbound | Outbound |
|---|---|---|
| internal environment | 443, 31443 and `apiTcpPort` (8443, with TCP ingress) from the VNet (the edge over ExpressRoute or VPN, the relay, admins), load-balancer probes, itself. Everything else denied. | default (the service needs PostgreSQL, the private endpoints, Teams, Graph, Entra, and AWS during migrations). Tighten with Azure Firewall if the client wants FQDN egress rules. |
| relay (external environment) | cannot be filtered by NSG (see above); use `relayAllowedCidrs` | the platform dependencies, DNS, the internet on 443 (Bot Framework keys, registry), and the **internal environment on `apiTcpPort` only** (`api`; not 443, so not `web` or `enrol`). **Denied: PostgreSQL, the private endpoints, on-premises, every other private address.** |
| PostgreSQL | 5432 and 6432 from the internal environment, itself, and `postgresAdminSourcePrefixes`. Everything else denied. | default |
| private endpoints | 443 from the VNet only (NSG policies enabled on the subnet) | default |

Narrowing the relay's internet egress to `login.botframework.com` needs Azure Firewall with an FQDN rule; an NSG cannot filter by name. Rules follow the Container Apps NSG guidance (<https://learn.microsoft.com/en-us/azure/container-apps/firewall-integration>); VERIFY them in a first deployment.

### Closed by default (threat model T1)

The three ingress allow-lists have **no default**: a parameters file must give each at least one range, or the deployment fails validation (`bicep build-params` refuses it, as does ARM; `check.sh` tests both a missing and an empty list).

| Parameter | What to list |
|---|---|
| `webAllowedCidrs` | The users' and Prizmanyx's networks. Never a range that covers `caeRelaySubnetPrefix`: a compromised relay must not reach `web`. The deployment fails if one does (this list, `enrolAllowedCidrs` or `adminAllowedCidrs`), naming the range. |
| `enrolAllowedCidrs` | The edge appliances' networks. The internal environment subnet is added for `api`'s signing calls. Never a range that covers the relay subnet (the deployment fails). |
| `relayAllowedCidrs` | Microsoft's Bot Service ranges: the `AzureBotService` service tag's IPv4 prefixes, taken at deployment time by `relay-ranges.sh` ("The relay's ranges" below) and refreshed with each deployment. The repo does not record them, and that every Teams call comes from them is a real-tenant check (`docs/milestones/real-tenant-checks.md`), so there is no default. VERIFY that the list fits the ingress rule limit. |

To open one on purpose, list `0.0.0.0/0`. For the relay that leaves its own checks (the Bot Framework JWT, the signed intent) as the only gate. For `web` and `enrol` it admits the relay's subnet, which T1 rules out, so the deployment fails.

### The relay's ranges (decision 49)

Nobody types the relay's ranges. Before every deployment:

```
deploy/azure/relay-ranges.sh --location <region> --out deploy/azure/relay-allowed-cidrs.json
```

It runs `az network list-service-tags --location <region>` (read-only, with the operator's own `az` sign-in; the script never signs in) and writes `relay-allowed-cidrs.json`. The parameters file reads it, so `relayAllowedCidrs` is exactly that list:

```
param relayAllowedCidrs = loadJsonContent('relay-allowed-cidrs.json', '$.prefixes')
```

- **Which tag.** By default the global `AzureBotService` tag, because Bot Connector's calls are not tied to the relay's region (`--location` picks the cloud; the list covers every region). `--regional` takes only `AzureBotService.<Region>`; use it only once a real tenant shows every call comes from there.
- **IPv4 only.** Container Apps ingress IP restrictions take IPv4, so IPv6 prefixes are left out (the script counts them).
- **Fails loudly, writing nothing,** when the tag is missing, appears twice, has no IPv4 prefix, or holds a malformed one, or when `az` fails. `--max-rules N` also fails above N prefixes, once the ingress rule limit is known (VERIFY).
- **Changes.** A rerun prints the prefixes added and removed since the file it replaces, and the file records the tag's change number and when it was taken. It is not committed (`.gitignore`).
- **Tests.** `test-relay-ranges.sh` runs it offline against `testdata/service-tags.json`, which has the REST API's response shape with documentation addresses (not Microsoft's), and through a fake `az` for the live path; `check.sh` runs those tests and builds the example from the script's output.

## Storage layouts

All accounts: shared-key access **off** (Entra only), public network access **off** (parameter), TLS 1.2 minimum, no anonymous access, copy scope **`PrivateLink`** whenever public access is off (decision 22; `AAD` only if you turn public access on for development; the live to archive copy then needs private endpoints on both accounts in the same VNet, VERIFY in pilot 4), change feed on (default retention 14 days), blob and container soft delete (7 days), diagnostic logs to Log Analytics. No point-in-time restore and no last-access tracking, because immutability does not allow them.

| Layout | Redundancy | Default tier | Versioning | Inventory | Notes |
|---|---|---|---|---|---|
| `live` (non-WORM) | ZRS, GZRS or RA-GZRS | **Smart** (`accessTier: 'Smart'`, API `2025-08-01`) | on | daily, Parquet | Uploads must not set an explicit tier or the blob never enters Smart. |
| `archive` (non-WORM) | LRS or GRS | Cool | on | daily | The service copies files here on their archive date with `x-ms-access-tier=Archive`. |
| `worm` (one per share) | LRS or GRS | Hot | on | **none** (version-level immutability disables it) | `immutableStorageWithVersioning.enabled: true`; a **`CanNotDelete` lock** on the account (`lockWormAccounts`); an optional **default retention policy on the data containers, created unlocked** (`retentionDays`). The service locks it after the validation window (decision 31). The `recall` container never gets a policy, so recall copies stay deletable. Scheduled tiers only, no Smart. |

Facts checked: Smart tier needs a minimum REST API of `2025-08-01`, a GPv2 account and zone-redundant storage (<https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-smart>). `immutableStorageWithVersioning.enabled` "can only be set to true at account creation time" (<https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/2025-08-01/storageaccounts>). **WORM accounts must never be deleted and recreated casually**, and the template can never turn the setting off.

Once the service has **locked** a container policy, remove `retentionDays` from the parameters before the next deployment: do not re-send a policy that is already locked. A policy can be extended, never shortened or deleted.

The optional `versionMaxAgeDays` adds a lifecycle rule that deletes non-current versions on non-WORM accounts (the ArchiveAdmin maximum rewind depth). It is off by default because the service prunes by share.

The live and WORM accounts also get an **`omnifile-exports`** container for bulk snapshot exports (control API §6): compressed catalog parts an edge downloads when it has to re-read a large dataset. It has no immutability policy. The service deletes an export after 2 days. A lifecycle rule (`expire-snapshot-exports`, `exportMaxAgeDays`, default 3) deletes anything older, as a backstop. **VERIFY** in pilot 4 that the rule runs on a WORM account. The `worker` identity writes there with its existing data role, and the `api` identity signs the read SAS as it does for files.

## Access (decision 28, section 7)

| Identity | Storage (live, archive, WORM) | Key Vault | Other |
|---|---|---|---|
| `api` | custom **OmniFile SAS Minter**: blob read and write, and user-delegation keys, to mint per-blob SAS for the edge. **No delete of any kind**, no container writes, no tags; a SAS can't grant more than its signer holds | Secrets User on `api-lease-key` only (phase 2) | |
| `enrol` | none | **Key Vault Crypto User on the CA key only**: it can sign, never read the key | |
| `web` | **none** | Secrets User on `web-cursor-key` only (phase 2) | catalog only |
| `worker` | custom **OmniFile Blob Worker**: read, write, delete, move, tags, legal hold (`containers/write`), create container, and user-delegation keys (the portal's download links). **Excluded: `runAsSuperUser`, `immutableStorage/runAsSuperUser`, `deleteBlobVersion`, `permanentDelete`, ACL and ownership changes, container delete.** WORM accounts also get custom **OmniFile WORM Policy Manager** (create, extend, lock container policies; **no delete**, no account changes, no keys) | none | custom **OmniFile Job Starter** on the verifier job |
| `verifier` | Storage Blob Data Reader | none | |
| `relay` | none | **Key Vault Secrets User on its own three secrets only** (`relay-mtls-cert`, `relay-mtls-key`, `relay-ca-bundle`), in phase 2 | |
| break-glass group | `breakGlassAdminGroupObjectId`, role `Reader` or `Contributor`. Empty = nobody. **Owner is not offered.** | none | |

Why the worker is not Storage Blob Data Owner: that built-in role's `blobs/*` carries the immutability override and version delete actions, which is how WORM would be bypassed. Setting a per-version retention policy needs `immutableStorage/runAsSuperUser` and nothing less (<https://learn.microsoft.com/en-us/rest/api/storageservices/set-blob-immutability-policy>), so the template grants it to no one by default. Container default policies and legal holds work without it. If pilot 4 shows per-version extension is required, set `grantRetentionOfficerToWorker = true`, which assigns a custom role holding that single action to the worker.

Nobody else gets Blob data access. End users never do. Creating role assignments needs Owner or User Access Administrator on the resource group for whoever deploys.

If you give an ACR (`registryResourceId`), the six identities get AcrPull.

Key and secret level assignments are used on purpose here, though Microsoft recommends vault-level in general; the Key Vault ABAC alternative (limit a role by secret name) is still in preview.

### Key Vault: recommendation

Use it, on the premium SKU. The CA key that signs edge-appliance certificates is the most valuable secret in the product. Held as a non-exportable HSM key, the service can ask the vault to sign but nobody can ever copy the key, and every use is in the audit log. Without a vault the key would sit in a file or database. The template creates the key, `omnifile-edge-ca` (EC P-384, `sign` and `verify` only), and only the `enrol` identity can use it. The root certificate is created once at bootstrap, by signing with that key. If a customer wants no vault, set `deployKeyVault = false` and the service needs another home for the key.

## Parameters you will change

Everything has a default except the PostgreSQL Entra administrator, the six images and the three ingress allow-lists ("Closed by default").

| Parameter | Default | Meaning |
|---|---|---|
| `postgresEntraAdminObjectId`, `postgresEntraAdminName` | **required** | Entra group that administers PostgreSQL. |
| `deploymentSize` | `standard` | `standard` (production) or `small` (the cheapest that keeps every constraint: a site with a few edges, or a test run). The `sizes` table in `main.bicep` sets PostgreSQL's server and storage, each app's CPU, memory and replicas, each app's catalog connections at small (`OMNIFILE_PG_POOL_MAX`: api 4, web 4, enrol 2, the worker 6, at most 25 in all against B1ms's 35; on a B1ms the deployment fails if the replicas, the verifier trigger or the server let the apps open more than 25), the verifier's size, the log workspace's retention and daily cap, and how often the alerts run. Any of `postgresSkuName`, `postgresSkuTier`, `postgresStorageSizeGB`, `apiMinReplicas`, `apiMaxReplicas`, `workerMaxReplicas`, `logRetentionDays` and `logDailyCapGb` that is set overrides its size. `small.bicepparam` and `standard.bicepparam` start each; "Rough monthly cost" below has the prices. |
| `namePrefix` | `omni` | 2 to 8 lowercase characters used in names. |
| `apiImage`, `enrolImage`, `webImage`, `workerImage`, `verifierImage`, `relayImage` | **required** | `<registry>/<repository>@<digest>`, from `deploy/images/images.lock.json` (`deploy/images/build.sh`): `omnifile-service` for api, enrol, worker and the verifier, `omnifile-web`, `omnifile-relay`. Each app's command is fixed in the template. |
| `registryServer`, `registryResourceId` | empty | Registry and AcrPull. |
| `apiIngress` | `tcp` | The only value: `api` terminates mutual TLS itself behind a TCP ingress, with a server certificate from the edge CA (threat model T5). See "Apps and mutual TLS" for why there is no `http`. |
| `apiTcpPort` | `8443` | `api`'s port with TCP ingress: unique in the environment, never 80, 443 or 36985. Edges and the relay call `https://<api fqdn>:8443` (output `apiUrl`). |
| `deployEnrol`, `deployWeb` | `true`, `true` | The enrolment and portal/partner apps. |
| `enrolAllowedCidrs` | **required** | Edge networks allowed to enrol. |
| `webAllowedCidrs` | **required** | Networks allowed to reach the portal and partner API: the users' and Prizmanyx's. Neither this list nor `enrolAllowedCidrs` may cover `caeRelaySubnetPrefix` (threat model T1). |
| `adminAllowedCidrs` | `[]` | The administrators' workstations: added to `web`'s and `enrol`'s allow-lists (the portal, enrol's health check). Never a range covering the relay subnet (the deployment fails). PostgreSQL access stays `postgresAdminSourcePrefixes`. |
| `deployTeamsBot`, `teamsBotSku` | `true`, `F0` | The Azure Bot, a managed-identity bot on the worker's identity (output `teamsBotAppId`). |
| `relayBotAppId` | empty | Only with `deployTeamsBot = false`: a bot registered elsewhere. |
| `relayIntentPublicKeys` | `{}` | `{kid: base64url Ed25519 public key}`: the relay and `api` both verify card intents with it. |
| `teamsIntentKid`, `teamsSecretsProvisioned` | `intent`, `false` | The key id the worker signs with; phase 2, once the secret `teams-intent-signing-key` (PKCS#8 PEM) is in the vault: the worker reads that one secret. |
| `teamsAppCatalogId` | empty | The Teams app's catalog id (`deploy/entra/register.sh --teams-app-zip`). Empty: the worker sends no cards. |
| `relayCertSha256` | `[]` | SHA-256s of the relay certificates `api` accepts (`issue-relay-cert`, step 7). Empty: `api` answers 503 to clicks. |
| `notifySender` | empty | The mailbox the worker sends fallback email from (scoped by Exchange RBAC; `register.sh --mail-sender`). |
| `teamsTenantId` | empty (this deployment's tenant) | The Microsoft 365 tenant whose users click the cards, when it isn't the subscription's. Also `web`'s sign-in tenant. |
| `webOrigin`, `webClientId`, `webApiAudiences` | `https://<web fqdn>`, empty, `[]` | The portal's origin, its app registration and the partner API's audiences (`deploy/entra/register.sh` writes the last two). `web` doesn't start until `webClientId` is set. |
| `awsAccounts`, `awsWebIdentityAudience` | empty | `OMNIFILE_AWS_ACCOUNTS` and `OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE` for `api` and the worker (S3 sources, Roles Anywhere CRLs; decision 43). |
| `awsConnectTemplateUrl` | empty | `OMNIFILE_AWS_CONNECT_TEMPLATE_URL` for `web`: the S3 URL of `deploy/aws/cloudformation/omnifile-connect.yaml`, for Settings, AWS's one-click Launch in AWS (`docs/design/cloud-connect.md` 2.2). Empty = the page offers the template for upload. |
| `relayAllowedCidrs`, `relayForwardedAllowIps` | **required**, relay subnet | Who may call the relay (the Bot Service ranges, from `relay-ranges.sh`); the ingress proxy range for uvicorn `forwarded-allow-ips` (never `*`; VERIFY the range). |
| `ingressSourceCidrs` | `[]` = internal environment subnet | The internal ingress proxy's source range: `enrol` and `web` believe `X-Forwarded-For` (rate limits, audit addresses) only from it. Never used for `api`'s certificates. enrol's `/internal/v0/sign` also requires a caller in the internal subnet (`OMNIFILE_ENROL_SIGN_SOURCES`). VERIFY the range after the first deployment. |
| `keyVaultPurgeProtection`, `keyVaultSoftDeleteDays` | `true`, `90` | Keep both in production. A test deployment turns purge protection off (it can never be turned off once on) so teardown can purge the vault. |
| `keyVaultUploaderObjectId`, `keyVaultUploaderType` | empty, `Group` | Who uploads the phase-2 secrets: Key Vault Secrets Officer on the vault (no key rights). Empty: nobody. |
| `edgeCaCertPem` | empty | The CA certificate from step 7 (public). |
| `relaySecretsProvisioned` | `false` | Phase 2 switch; see the customer steps. |
| `appSecretsProvisioned` | `false` | Phase 2 switch for `api-lease-key` and `web-cursor-key`; see the customer steps. |
| `wormShares[].retentionDays` | none | Default retention on the data containers, created unlocked. |
| `lockWormAccounts`, `grantRetentionOfficerToWorker` | `true`, `false` | WORM resource locks; the retention officer role. |
| `postgresAdminSourcePrefixes` | `[]` | Ranges (jump host) allowed to reach PostgreSQL. |
| `deployNonWormStorage`, `liveRedundancy`, `archiveRedundancy` | `true`, `Standard_ZRS`, `Standard_LRS` | Non-WORM layout. |
| `wormShares` | `[]` | `[{ name: 'fin', redundancy: 'Standard_GRS', containers: ['data'] }]`. The name (2 to 5 lowercase letters or digits) is part of the account name and cannot be changed. |
| `postgresVersion`, `postgresSkuName`, `postgresHighAvailability`, `postgresGeoRedundantBackup` | `18`, the size's (`Standard_D4ds_v5` standard, `Standard_B1ms` small), `Disabled`, `false` | Database size and resilience. A Burstable server has no high availability: the deployment fails if it is asked for. |
| `verifierTrigger`, `verifierCron`, `verifierDedicatedProfileType` | `Schedule`, `*/15 * * * *`, empty | The verifier job: each execution is one replica and one pass over the `verify` queue, one message at a time (`python -m omnifile_service.worker.verifier`, exit 0 or 1), on the schedule by default. A pass holds a Postgres advisory lock, so executions never overlap; the worker reads the same queue with the same 2-hour visibility. `Manual`: only when started. `Event`: a PGMQ queue-depth KEDA rule (VERIFY). |
| `breakGlassAdminGroupObjectId`, `breakGlassBlobRole` | empty, `Reader` | Decision 28 section 7 (`Reader` or `Contributor`; `Contributor` includes blob and container delete). |
| `createPrivateEndpoints`, `createPrivateDnsZones` | `false`, `false` | See below. |
| `metricsEnabled`, `alertsEnabled` | `true`, `true` | Service metrics to Application Insights with the apps' managed identities, and the alert rules (`docs/design/observability.md`). |
| `alertActionGroupId` | empty | The action group alerts notify. Empty: they fire in the portal only. |
| `monitorPrivateLink` | `false` | An Azure Monitor Private Link Scope for metric ingestion. It changes DNS for every Azure Monitor endpoint the VNet resolves, so check other Azure Monitor users of the VNet first. |
| `edgeArcMachineIds` | `[]` | Arc machine resource ids of edges whose journal goes to the workspace (the Azure Monitor Agent extension and a data collection rule). |
| `vnetAddressPrefix` and the four subnet prefixes | `10.60.0.0/16` and `/26`, `/27`, `/27`, `/27` | Change them if they collide with on-premises ranges. |

## What the customer does (decision 22)

The template does **not** create private endpoints by default. Storage and Key Vault come up with public network access disabled and cannot be used until these steps are done.

1. **Private endpoints.** For each entry in the `privateEndpointTargets` deployment output, create a private endpoint in `privateEndpointSubnetId`: sub-resource `blob` for each storage account and `vault` for the Key Vault. (Setting `createPrivateEndpoints = true` makes the template do this and the next step.)
2. **DNS.** Link these private DNS zones (the `requiredPrivateDnsZones` output) to the VNet and attach each endpoint to its zone through a zone group: `privatelink.blob.core.windows.net`, and `privatelink.vaultcore.azure.net` if the vault is deployed. If the customer already has these zones in a hub, they use theirs. (`createPrivateDnsZones = true` creates and links them here.)
3. **Reach the VNet from the edge.** Connect this VNet to the ExpressRoute or VPN gateway (peer it to the hub, or place a gateway in it). The template does not create gateways or peering.
4. **On-premises DNS.** Add conditional forwarders so the edge resolves the internal environment's default domain (output `internalEnvironmentDefaultDomain`, internal IP in `internalEnvironmentStaticIp`) and the two `privatelink` zones, through the VNet's resolver.
5. **PostgreSQL roles.** Sign in as the Entra administrator (from an allowed range, `postgresAdminSourcePrefixes`) and create a database role for each identity that uses the catalog (`pgaadauth_create_principal`) named exactly after it (`id-<prefix>-api`, `-enrol`, `-web`, `-worker`, `-verifier`; the relay has none), then run the service migrations (which install PGMQ) as the administrator, naming each app's login:
   ```
   PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv) \
   OMNIFILE_DATABASE_URL=postgresql://<admin>@<server>/<db>?sslmode=verify-full \
   OMNIFILE_DB_MEMBERS=api=id-<prefix>-api,enrol=id-<prefix>-enrol,web=id-<prefix>-web,worker=id-<prefix>-worker,worker=id-<prefix>-verifier \
     python -m omnifile_service.catalog.migrate
   ```
   Every migration run ends by granting each app's group role (`omnifile_api`, `omnifile_enrol`, `omnifile_web`, `omnifile_worker`) exactly its privileges, and makes each login a member (`service/src/omnifile_service/catalog/privileges.py`). The administrator owns every table; no app can change the schema, truncate a table, turn a trigger off, write the feed (except the api's conflict events), or touch the audit log beyond adding to it. The verifier job runs the worker's code and is a member of `omnifile_worker`. Run the migrations the same way after every upgrade, so new tables get their grants, and **before** moving the apps to the new images: a new image expects its migrations to be there, and the older apps keep working on a newer catalog, since the migrations so far only add. So: migrate with the new code, then re-deploy with the new image digests. A migration that can't get a table's lock within 5 s gives up, lets queued readers past, and tries again, six times in about three minutes. It then stops with a message naming the file and the sessions in the way, and nothing is recorded. A worker in a long provider call ends by itself, so run it again later. The token in `PGPASSWORD` is the administrator's own Entra token (it lasts about an hour); the apps sign in by themselves ("The catalog connection").
6. **Entra and Teams.** The template creates the Azure Bot (a managed-identity bot on the worker's identity, messaging endpoint the relay). Then, as an admin with the roles its header names, run `deploy/entra/register.sh` with the deployment's outputs (`identityPrincipalIds.web` and `.worker`, `webOrigin`): it registers the portal app (roles, sign-in, claims, a federated credential for `web`'s identity) and gives the worker's identity its Graph application roles. Build the Teams app with `deploy/teams/build.sh --bot-app-id <teamsBotAppId> --portal-url <webOrigin>` and publish it (`register.sh --teams-app-zip`, or the Teams admin centre). Re-deploy with `webClientId`, `webApiAudiences` and `teamsAppCatalogId` from `register.sh`'s output. Assigning the app roles to AD groups, and the AD group sync, are the client's.
7. **CA bootstrap.** Only `enrol`'s identity can sign with the vault key, so make the root certificate inside its container (it is running, waiting for the CA): `az containerapp exec -n <prefix>-enrol -g <rg> --command "python -m omnifile_service.enrol init-kv-ca /tmp/ca.pem"`, then print `/tmp/ca.pem` the same way. It is self-signed by the vault key (ECDSA P-384 with SHA-384), lasts ten years (`--days`), and is public. Re-deploy with `edgeCaCertPem` set to it: `api` takes it as its trust anchor for edge certificates and gets its own server certificate from `enrol`, and `enrol` checks at start that the vault key is its key and refuses to start otherwise. Every certificate `enrol` issues is verified against it before it is handed out. Until `edgeCaCertPem` is set, `api` and `enrol` wait for it and no edge can enrol. A CA key in a file (`OMNIFILE_DEV_CA_KEY`) is refused outside dev mode.
   Then issue the relay's client certificate. Make an ECDSA P-256 key and CSR where you will keep the key (`openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout relay.key -subj /CN=omnifile-relay -out relay.csr`), and sign it inside `enrol`: `az containerapp exec -n <prefix>-enrol -g <rg> --command "python -m omnifile_service.enrol issue-relay-cert --csr-b64 $(base64 < relay.csr | tr -d '\n')"`. It prints the certificate (CN `omnifile-relay`, SAN `urn:omnifile:relay`, clientAuth, 180 days by default, `--days`) and `SHA-256: <hex>`: re-deploy with `relayCertSha256` set to that hash, and renew before it expires.
8. **Relay secrets, phase 2.** Upload `relay-mtls-cert` (the certificate from step 7), `relay-mtls-key` and `relay-ca-bundle` (the names are in the `relaySecretNamesToUpload` output) to the vault, then re-deploy with `relaySecretsProvisioned = true`. With TCP ingress (the default) `relay-ca-bundle` is the edge CA certificate (`edgeCaCertPem`), which issues `api`'s server certificate. Never a broad public-CA bundle. The relay identity then gets read access to those three secrets, and the relay mounts them. (Two phases because a role assignment on a secret needs the secret to exist, and this template never writes secret values; `keyVaultUploaderObjectId` gives the uploader Secrets Officer on the vault.)
   The same goes for the keys every replica of an app shares: put `api-lease-key` (stub leases) and `web-cursor-key` (portal cursors), each the base64 of 32 random bytes (`openssl rand -base64 32`), in the vault, and re-deploy with `appSecretsProvisioned = true`. Each app then reads its own key as a secret reference, never as a plain environment variable that anyone with read access to the app could see. Until then, each replica makes its own key, so leases and cursors work only on the replica that made them.
9. **Images.** `deploy/images/build.sh` builds the three linux/amd64 images and records their digests in `deploy/images/images.lock.json`; `deploy/images/smoke.sh` runs each with its app's command against Postgres. Push them to the registry (a release step; the digests stay the same) and set the six image parameters to `<registry>/<repository>@<digest>`.
10. **Migrations from S3 (M5, optional).** Create an Azure Storage Mover and a project in this resource group, an Azure Arc multicloud connector to the AWS account (its CloudFormation stack runs on the AWS side), a source endpoint per S3 bucket (and one for the S3 backend's `staging_bucket`, if urgent recalls are staged), and a target endpoint per storage container the copies land in, with the endpoints' managed identity given Storage Blob Data Contributor on that container. Then re-deploy with `storageMoverConfig` naming them (the worker's `OMNIFILE_STORAGE_MOVER`, `docs/design/migration-engine.md`); the worker gets the custom "OmniFile Mover Operator" role on this resource group. Without it, migrations can still be copied by Prizmanyx (`copy: external`) and are verified and cut over by the service. The portal can do the first part instead: Settings, Azure grants the worker a Mover Deployer role on a resource group you choose, and Settings, Storage Mover creates the Storage Mover and its project there and walks you through the agent (`docs/design/cloud-connect.md` 3). The endpoints and `storageMoverConfig` stay this step.

11. **Enrolment codes for new edges.** The portal hands out one-time enrolment codes (Edges, Add edge; `portal-api.md` §5.15). Each code names the `enrol` URL, which the template passes to `web`, and the CA whose certificate the edge pins when it enrols. Read the certificate chain `enrol` presents from a machine on the edge's network (`openssl s_client -connect <enrol fqdn>:443 -showcerts`). Take the SHA-256 of the issuing CA's DER (`openssl x509 -outform der | sha256sum`), and re-deploy with `enrolServerCaSha256` set to it. Until then, Add edge answers that enrolment isn't configured. **VERIFY** how long the internal environment's certificate issuer stays the same: when it changes, codes issued before carry the old pin. The setup wizard on the appliance takes the code (`docs/design/setup-wizard.md`). This pin is for `enrol` only: with TCP ingress the enrolment answer then tells the edge to pin the edge CA for `api` (`api_server_ca_sha256`), so `api`'s certificate never depends on the platform's issuer.

12. **Edges reading S3 (M5, optional; decision 50).** On the AWS side, register the edge CA certificate from step 7 as an IAM Roles Anywhere trust anchor, and create the edges' role and profile, following `deploy/aws/README.md`. Give the service's web-identity role the CRL permissions there (`deploy/aws/iam/service-crl-policy.json`), and name the trust anchor in the account's `OMNIFILE_AWS_ACCOUNTS` entry (`roles_anywhere_trust_anchor_arn`, and `endpoints.rolesanywhere`). A revoked edge's certificates then reach Roles Anywhere's CRL: the worker gets the CRL from `enrol` (`OMNIFILE_ENROL_CRL_URL`; enrol admits only the worker's identity, `OMNIFILE_ENROL_CRL_CALLER_OID`) and uploads it.

## Catalog backup and restore (M6)

The catalog is the Azure Database for PostgreSQL server: files, versions, locations, policies, audit, and the PGMQ work queues (decision 27). The blobs and S3 objects are the data; the catalog is what makes them a filesystem.

**Backups** are the server's own (no OmniFile job):
- automatic full and incremental backups, kept `postgresBackupRetentionDays` (7 to 35, default 14);
- geo-redundant with `postgresGeoRedundantBackup = true`, for a restore in the paired region (set at creation; it can't be turned on later, VERIFY);
- point-in-time restore to any second inside the retention, always to a **new** server.

**Restore** (a mistaken bulk change, a corrupted catalog, a lost region):

1. **Stop the writers.** Scale `api`, `web`, `worker` and the verifier job to zero, so nothing writes to either server. Edges keep serving what they hold. Writes on the edge queue locally, and reads of non-resident data wait or fail fast by share policy, as when the service is unreachable.
2. **Restore** to a new server at the chosen time (`az postgres flexible-server restore`, or `geo-restore` into the paired region). Bring it into the VNet as the template does (delegated subnet, private DNS zone), recreate the Entra administrator and the database roles if needed (step 5 above), and re-deploy with the new server's name.
3. **Start a new feed incarnation.** As the Entra administrator, before anything else connects: `SELECT catalog.feed_restored();`. The restored server hands out transaction ids again, which feed cursors are made of. Without this, an edge or partner whose cursor is from after the restore point would skip new events without noticing. With it, every edge gets `reset_required` and every partner cursor `410 cursor_expired`, so each reads the state again (`control-api.md` §5). It also moves every catalog sequence 2^40 ahead, so a file id an edge holds for a file of the lost timeline is never given to a new one.
4. **Start the apps.** Queued work restored with the catalog runs again; handlers are idempotent.
5. **Reconcile** every container now (`reconcile/run.py`, on demand). Blobs written after the restore point are reported as `unexpected` drift, and catalog rows for blobs deleted since as `missing`. Review them before acting: reconcile reports and never repairs.
6. **Edges re-snapshot** on `reset_required`, in place (`docs/design/edge-metadata.md` §5.2). Inodes stay, and the restored catalog's rows win. Local bytes the restored catalog doesn't know are committed again, like a write made while the service was unreachable: a file created after the restore point becomes a new file, and a newer local version becomes a new version. A stub of a lost version answers `EIO`, and its blob shows in step 5's report. Large datasets load from a bulk snapshot export (`control-api.md` §6), with paging as the fallback.

What is lost: catalog changes after the restore point. That covers policy edits, recall history and audit rows, and the catalog records of uploads (the blobs themselves stay, found by step 5).

### Restore drill (local)

```
deploy/azure/restore-drill.sh            # Postgres 17; PG_IMAGE=postgres:18 for the default server version
```

It starts a throwaway Postgres container of its own (a unique name, a free port on 127.0.0.1), removes it on exit, and touches nothing in Azure. It runs `service/tests/api/test_restore_drill.py` (skipped in the ordinary suite) and then the edge's re-snapshot unit test:

1. seeds a catalog through the `api` app: an enrolled edge, a dataset and share, two files and a folder; the edge reads the feed and acknowledges its cursor;
2. backs it up with `pg_dump --format=custom`, then creates one more file (the timeline the restore loses);
3. restores into a new database with `pg_restore --exit-on-error`, and serves the `api` app on it;
4. shows that without step 3 of the runbook nothing tells the edge: its old cursor still reads on;
5. runs `SELECT catalog.feed_restored()` (returns incarnation 1);
6. checks the service's half of every edge's re-snapshot: the old cursor gets `reset_required` with no events; a full snapshot has `as_of` in incarnation 1 and exactly the restored rows (not the lost file); a new file's id is more than 2^40 past the lost one; the feed reads on from the snapshot's cursor, and the edge can acknowledge it; the source catalog keeps incarnation 0.

The edge's half, re-snapshotting in place, is `omnifile-replica`'s `a_resnapshot_after_a_restore_applies_wound_back_rows_and_finds_lost_ones` (run by the script) and `omnifile-edge`'s Linux daemon tests `a_restored_catalog_is_resnapshotted_in_place` and `a_restored_catalog_is_loaded_from_a_bulk_export` (the edge-linux lane).

**Results, 2026-10-21** (Postgres 17.11 in Docker): the drill passes (1 passed, 1.6 s), and the replica test passes. With `feed_restored()` left out, the drill fails at the `reset_required` check, so it tests the step. The whole api suite passes on the same throwaway server (131 passed, 1 skipped). Not covered here: Azure's own point-in-time restore to a new server, the Entra roles on it, and partner cursors (`410 cursor_expired`, `service/tests/web/test_partner_reads.py`).

### Feed and audit partitions

The feed (`catalog.feed_event`, weekly) and the audit log (`catalog.audit_log`, monthly) are partitioned by time. Once a day the worker creates partitions 8 weeks and 6 months ahead, and drops feed partitions older than `OMNIFILE_FEED_RETENTION_DAYS` (default 35, at least 7). An edge or partner whose cursor is older gets `reset_required` or `410` and reads the state again. Audit partitions are never dropped automatically.

A row in a DEFAULT partition means its partition was missing when the row was written: the worker was down for weeks, or its sweep fails. The alert `default-partition-rows` fires on any. Creating the partition for that range fails while the rows are there, so, as the Entra administrator, in one transaction: copy the DEFAULT partition's rows for the range to a temporary table, delete them, `SELECT catalog.ensure_time_partitions(...)`, and insert them back. Audit rows can't be deleted (an append-only trigger): for those, `ALTER TABLE ... DISABLE TRIGGER audit_log_no_update` on `catalog.audit_log_default` for that transaction only.

## Dead letters

The `dead-letters` alert fires while any `<queue>_dlq` queue holds a message: one the worker tried five times, or one that can never succeed. Run these as the Entra administrator, with the same connection as the migrations (step 5):

```
python -m omnifile_service.admin dead-letters list [--queue restores] [--json]
python -m omnifile_service.admin dead-letters resend --queue restores --msg-id 123   # or --all --yes
python -m omnifile_service.admin dead-letters drop --queue restores --msg-id 123     # or --all --yes; --force: see below
```

`list` shows each message, its error, how long it has been there, and whether the worker has given up on it. Read the error before anything else, along with the worker's log for that time. Every `resend` and `drop` is audited (`dead_letter.resend`, `dead_letter.drop`, with who signed in), and the message is kept in `pgmq.a_<queue>_dlq`.

**What resending means.** The message runs again, with all its tries. If it fails every time, it comes back here after about a minute and a half, having repeated whatever it does at the provider on each try, and that can bill. So find out why it failed first. Resending a message for work that has ended since (a failed recall, say) does nothing, and `drop` clears it.

**What dropping means.** `drop` clears the alert, so it refuses a message nothing would send again, says what would be stranded, and changes nothing. Add `--force` only once you accept that:

- an upload's `verify`: the file stays unverified and never becomes a stub, and the edge keeps its copy;
- `delete_blob`: the row stays `deleting`, with no sweep, and its bytes stay, and are billed;
- `annex_purge`: the purge never runs, since `purge_due` skips one already requested;
- `migrate`, `migrate_verify`, `seed`, `snapshot_export`, `portal`, `cloud_task`, `settings_test`: that work stops where it is.

`--force` names what it stranded in its output and in the audit row. `--all`, on `resend` or `drop`, needs `--yes`.

**The worker handles some of these by itself.** The sweep `dead_letter_redrive` sends these queues' messages again after 5, 10, 20 and 40 minutes, then each hour, six times in all, and once more when the provider answers, so a message that isn't marked as given up may need nothing from you. If every try fails, a recall or a read-back ends about 5 h 25 min after its first failure, and a delete after about 4 h 25 min (the table in `docs/milestones/results/wp23.md` shows how):

| Queue | While it waits | At the end |
|---|---|---|
| `restores` | The recall stays active, and its promised time is cleared. If the provider answers nothing at all, the recall keeps waiting, for up to 3 days. | The recall fails, and its user sees why in the portal and in Teams and can recall again. The letter stays until you `drop` it. |
| `verify` | The file stays unverified: nothing is stubbed, and the edge keeps its bytes. If the provider answers nothing at all, the read-back keeps waiting, for up to 3 days. | A `location.verify` deny is recorded. An archive copy is dropped and the original kept. An upload stays on the edge, its file shows "Not checked" in the portal, and `resend` tries the read-back again. |
| `retention` | The location stays `deleting`, and its bytes stay, and are billed. An annex purge stays requested. | An audit row, `retention.gave_up`. Find out why the delete fails, then `resend`. |

Being throttled uses up no tries for an hour, since real throttling is over in seconds or minutes. After that a message counts as failing like any other, because Azure also answers a gateway's 503 as throttling. So a throttled message reaches this alert within about an hour, and a recall throttled that long ends at about 6.5 hours.

**Many recalls failing at once.** If the provider answers a HEAD but refuses every restore (a role lost its permission, say), that isn't taken for an outage. Recalls fail after about 5 h 25 min, at up to 100 a minute, and each user gets a card. Fix the cause first. A failed recall has ended, so `resend` does nothing for it. To bring them back, use `POST /v1/recall-batches` for the dataset, or the users can open their files again. `dead-letters list --queue restores --json` lists what failed and why, and `drop --all --yes` then clears the alert.

The `worm_policy` sweep repeats its own work, so its dead letters are only cleared. Other queues aren't sent again by this sweep. `tiering`, `notify`, stubs and the CRL have sweeps that derive their work again, so `drop` is safe once you have read the error. For `migrate`, `migrate_verify`, `seed`, `snapshot_export`, `portal`, `reconcile`, `settings_test`, `cloud_task` and `identity_resolve`, see `docs/milestones/results/wp23.md`, which says what a lost message loses in each. Resend those once the cause is fixed.

## Rough monthly cost

List prices in USD from the Azure Retail Prices API for **West US 3** on 2026-11-04, before any agreement or reservation, for 730 hours. `docs/milestones/results/wp16.md` has every resource with its meter, and how each figure is worked out. Use the Azure pricing calculator for a quote. Not included: storage capacity and transactions (they scale with the data), private-link and load-balancer data processed (about $0.01 and $0.005 per GB), the container registry (Basic is about $5 a month), ExpressRoute or VPN, S3 egress during migrations, and Entra, Teams and Microsoft 365 licences.

| | `standard` | `small` |
|---|---|---|
| PostgreSQL (compute and storage) | $274.60 (D4ds_v5, 128 GB) | $16.09 (B1ms, 32 GB) |
| Container Apps (apps and the verifier job, after the free grant) | $281.05 | $82.42 |
| The two environments' load balancers and public IPs (Microsoft bills these; VERIFY on the first bill) | $47.45 | $47.45 |
| Private endpoints (live, archive, vault; $7.30 more per WORM share) | $21.90 | $21.90 |
| Log Analytics and Application Insights (estimate) | about $26 | about $2 (at most $23 under the cap) |
| Log search alerts (12 rules) | $18.00 | $6.00 |
| Key Vault (the HSM-backed CA key) | $5.00 | $5.00 |
| Private DNS zones (4) | $2.00 | $2.00 |
| **Total** | **about $676** | **about $183** |

- **Storage capacity is what grows.** The live account is billed at Hot, Cool or Cold rates by Smart tier (Hot ZRS $0.023 per GB-month in West US 3), plus a monitoring fee of $0.04 per 10,000 objects. The archive account is cheap per GB ($0.00099 Archive LRS) but has 180-day early-deletion fees and rehydration costs. Versions are billed at full size, so rewind depth and change rate multiply storage.
- **PostgreSQL** is the largest fixed cost at `standard`; zone-redundant high availability doubles it. `small`'s B1ms has 35 user connections; `OMNIFILE_PG_POOL_MAX` keeps the apps to 25 at their maximum replicas (wp16.md has the budget). `postgresSkuName = 'Standard_B2s'` (414 connections, about $50 more) if a run still needs more.
- **Container Apps** assumes `api` and the worker are billed at the active rate all month and the warm `web`, `enrol` and relay replicas at the idle rate. Every replica idle would bring `small`'s apps down to about $30. No plan management fee: it is billed only with a dedicated workload profile, an environment private endpoint or planned maintenance, and the template sets none.
- **Log Analytics.** About $2.30 per GB after the first 5 GB a month. Logging storage reads, writes and deletes on busy accounts can ingest a lot: check this first if the bill surprises. `small` caps ingestion at 0.5 GB a day (`logDailyCapGb`); past the cap the workspace drops data, metrics included, until the next UTC day.
- **Key Vault premium.** $5 a month for the EC P-384 HSM key (an advanced key type; an RSA 2048 key would be $1), plus tiny operation charges. Both sizes keep it.

## Not done here

- Private endpoints, ExpressRoute or VPN, and peering by default (customer).
- Entra app registration, roles and Graph permissions: `deploy/entra/register.sh`, run by an admin (step 6). Assigning the app roles to AD groups is the client's.
- Pushing the images: `deploy/images/build.sh` builds them and records their digests; a release step pushes them.
- Azure Storage Mover resources for the S3 migration (decision 20); they belong with the migration work.
- Locking WORM policies: the service does that after the validation window, never infrastructure code.
- The restore drill on real Azure (`az postgres flexible-server restore` to a new server, the Entra roles, re-deploy). The local drill above covers the dump, the restore and the service's part.
- A real deployment. Before M6 exit, run `what-if` and a deployment in a clean subscription and resolve every row of the next table.

## The catalog connection

The server allows Microsoft Entra sign-in only, so each app signs in as its own managed identity, with an access token as the password (`service/src/omnifile_service/catalog/connect.py`). The template sets, for `api`, `enrol`, `web`, `worker` and the verifier job (the relay has no catalog access):

| Setting | Value |
|---|---|
| `OMNIFILE_PG_HOST`, `OMNIFILE_PG_DATABASE` | the server's FQDN, `omnifile` |
| `OMNIFILE_PG_SSLMODE` | `verify-full` (the server's certificate is checked against the system CAs; `OMNIFILE_PG_SSLROOTCERT` names another bundle) |
| `OMNIFILE_PG_USER` | the app's identity name, `id-<prefix>-<app>`: its PostgreSQL principal (step 5) |
| `AZURE_CLIENT_ID` | the same identity's client id: `ManagedIdentityCredential` gets the token for `https://ossrdbms-aad.database.windows.net/.default` |

Each new connection, pooled or not, gets a token with at least five minutes left; one token serves until then. The server checks a token only at sign-in, so an open connection outlives it. Every session acts as its app's group role (`SET ROLE` through the connection's options: `omnifile_api`, `omnifile_enrol`, `omnifile_web`, `omnifile_worker`; the verifier is a worker), the per-app grants of step 5, so a missing membership fails at once. Tokens are never logged.

In dev and tests, `OMNIFILE_DATABASE_URL` (a libpq connection string) is used instead, as before; setting both is refused. Storage accounts are not app settings: the service reads them from `catalog.backend` rows (the `storageAccounts` output names them). `service/tests/test_deploy_env.py` checks that every setting the template passes is one the service or relay reads, and that each catalog app gets its own identity's settings.

## What a real deployment must confirm (`VERIFY`)

Every `VERIFY` in `deploy/azure`, in one place. Each needs a real subscription (or a real tenant, or pilot 4's storage accounts); none can be settled by compiling. Tick them off in `docs/milestones/real-azure-checks.md` and `real-tenant-checks.md`.

| Where | What to confirm | Needs |
|---|---|---|
| `main.bicep` `relayAllowedCidrs`; `relay-ranges.sh` | Every Teams call to the relay comes from the `AzureBotService` ranges (and whether the regional tag alone is enough), the list fits the ingress rule limit, and real `list-service-tags` output has the fields the script reads | a real tenant |
| `main.bicep` `ingressSourceCidrs` | The internal ingress proxy's source range as `enrol` and `web` see it | a deployment |
| `main.bicep` `apiIngress = tcp`; `modules/app.bicep` | The TCP ingress on `apiTcpPort` reaches `api` from the edge networks and from the relay environment (NSG rules on both subnets); a replica in the internal subnet sending a forged `X-Forwarded-Client-Cert` is refused (threat model T5); whether ingress IP restrictions apply to TCP ingress; the TCP startup probe (10 + 10 x 30 s) keeps `api` from being restarted while it waits for its certificate, and probe limits allow those values | a deployment |
| `main.bicep` `apiFqdn` | An app with external ingress on an internal environment is `<name>.<default domain>`, the name `api`'s certificate is issued for | a deployment |
| `main.bicep` `teamsBot` | A `UserAssignedMSI` Azure Bot on the worker's identity gets Bot Connector tokens from Container Apps, and the relay sees the identity's client id as the token audience; whether it works when `teamsTenantId` differs from the deployment's tenant | a real tenant |
| `main.bicep` `relayForwardedAllowIps` | The relay environment's ingress source range, for uvicorn `forwarded-allow-ips` | a deployment |
| `main.bicep` enrol module | The source address app-to-app calls from `api` show at `enrol` (inside the internal subnet, or add its range) | a deployment |
| `main.bicep` `OMNIFILE_ENROL_AUDIENCE` | `enrol`'s client id works as the audience of the token `api` gets with its managed identity | a deployment |
| `main.bicep` `verifierTrigger`, the verifier job's scale rule; `modules/job.bicep` | The KEDA PostgreSQL scaler with Entra authentication on a Container Apps job, and the PGMQ queue name (`pgmq.q_<name>`); else keep `Manual` | a deployment |
| `main.bicep`, the worker's scale rule | Scaling on PGMQ queue depth with the KEDA PostgreSQL scaler | a deployment |
| `main.bicep` Mover Operator assignment | The Storage Mover is in this resource group (else assign the role where it is) | a deployment with Storage Mover |
| `modules/roles.bicep` Mover Operator | The action names | a deployment with Storage Mover |
| `main.bicep` relay secrets | Container Apps resolves Key Vault secret references through the private endpoint when the vault's bypass is `None` (else `AzureServices`, or `az containerapp secret set`) | a deployment |
| `modules/cae.bicep` peer encryption | Generally available on workload-profile environments, and its throughput cost | a deployment |
| "Network isolation" | The NSG rules against the Container Apps guidance | a deployment |
| step 11 | How long the internal environment's certificate issuer stays the same (codes carry its pin) | a deployment |
| "Catalog backup and restore" | Geo-redundant backup can only be chosen at server creation | a deployment |
| "The catalog connection" | Each app signs in with its managed identity's token, acts as its group role, and `verify-full` passes with the image's CA bundle | a deployment |
| `modules/roles.bicep` WORM Policy Manager | Its actions are enough to create, extend and lock container policies | pilot 4 |
| `modules/storage-account.bicep` versioning | Smart tier works on an account with versioning (and, for WORM, immutability) | pilot 4 |
| `modules/storage-account.bicep` copy scope | With `PrivateLink`, the live to archive copy works with both accounts' private endpoints in the same VNet | pilot 4 |
| `modules/storage-account.bicep` WORM policies | The resource shape for version-level immutability policies on containers | pilot 4 |
| `modules/storage-account.bicep` exports | The `expire-snapshot-exports` lifecycle rule runs on a WORM account | pilot 4 |
| `modules/storage-account.bicep` inventory | The `schemaFields` against a real Blob Inventory report (the reconcile reader for it waits on one) | pilot 4 |
