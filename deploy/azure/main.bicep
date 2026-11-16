// OmniFile service: Azure infrastructure for one client deployment, in the client's subscription (decision 17).
//
// Deploys at resource-group scope. Creates: VNet; log workspace; two Container Apps environments (an INTERNAL one for
// api / worker / verifier, and a small EXTERNAL one for the Teams relay, the only public endpoint); PostgreSQL Flexible
// Server (private, Entra-only); storage accounts for the non-WORM layout and per WORM share; Key Vault for the edge CA key;
// managed identities and the role assignments they need.
//
// Private endpoints for storage and Key Vault are the CUSTOMER's to create (decision 22). They are off by default here.
//
// The six image parameters are required: deploy/images/build.sh builds the images and records their digests in
// deploy/images/images.lock.json. Each app runs its own command (`appCommands`), whatever the image's ENTRYPOINT.

targetScope = 'resourceGroup'

// ---------------------------------------------------------------- types

@description('One WORM share. Each gets its own LRS/GRS account with versioning and version-level immutability. The name becomes part of the account name and cannot be changed later; use 2-5 lowercase letters or digits.')
type wormShare = {
  @minLength(2)
  @maxLength(5)
  name: string
  redundancy: ('Standard_LRS' | 'Standard_GRS')?
  @description('Containers to pre-create. The recall container (decision 31) is always created in addition.')
  containers: string[]?
  @description('Default retention in days for new versions in the data containers, created UNLOCKED (the service locks it, decision 31). Omit once the service has locked it. 0 or omitted = the service sets the policy.')
  retentionDays: int?
}

// ---------------------------------------------------------------- general

@description('Short lower-case prefix used in resource names.')
@minLength(2)
@maxLength(8)
param namePrefix string = 'omni'

param location string = resourceGroup().location
param tags object = {
  product: 'omnifile-gateway'
}

@description('How big the deployment is (the `sizes` table below). standard: the production sizing. small: the cheapest that keeps every constraint (private networking with no public fallback, the relay the only public endpoint, per-app identities and database roles, closed allow-lists, api\'s direct TLS): a Burstable B1ms PostgreSQL server, Consumption-only apps at 0.25 to 0.5 vCPU, web and enrol scaling to zero, a capped log workspace and alerts every 15 minutes. Prices: docs/milestones/results/wp16.md. A parameter set below (apiMinReplicas, postgresSkuName, ...) overrides its size\'s value.')
@allowed([ 'small', 'standard' ])
param deploymentSize string = 'standard'

@description('Empty = the size\'s (90 days standard, 30 small).')
@minValue(30)
@maxValue(730)
param logRetentionDays int?
@description('The log workspace\'s daily ingestion cap in GB, as a decimal string (\'0.5\'); \'-1\' = no cap. Empty = the size\'s (none standard, 0.5 small). Past the cap the workspace drops data until the next UTC day, metrics and alert data included.')
param logDailyCapGb string?
@description('Service metrics to Application Insights (decision 37; service metrics.py, OMNIFILE_METRICS_EXPORTER).')
param metricsEnabled bool = true
@description('Alert rules on the service and edge metrics (deploy/azure/modules/app-metrics.bicep).')
param alertsEnabled bool = true
@description('Action group the alerts notify (resource id). Empty: alerts fire in the portal only.')
param alertActionGroupId string = ''
@description('An Azure Monitor Private Link Scope for metric ingestion. Changes DNS for every Azure Monitor endpoint the VNet resolves.')
param monitorPrivateLink bool = false
@description('Resource ids of Arc-connected edges (Microsoft.HybridCompute/machines) that ship their journal to the workspace.')
param edgeArcMachineIds array = []

// ---------------------------------------------------------------- network

param vnetAddressPrefix string = '10.60.0.0/16'
@description('Internal Container Apps environment subnet. Workload profiles need /27 or larger; /26 leaves room to scale.')
param caeInternalSubnetPrefix string = '10.60.0.0/26'
@description('External (relay) Container Apps environment subnet, /27 or larger.')
param caeRelaySubnetPrefix string = '10.60.0.64/27'
@description('PostgreSQL delegated subnet, /28 or larger.')
param postgresSubnetPrefix string = '10.60.0.96/27'
@description('Where the customer puts the storage and Key Vault private endpoints.')
param privateEndpointSubnetPrefix string = '10.60.0.128/27'
@description('Required, closed by default (threat model T1). Source ranges allowed to reach the relay (ingress IP restrictions; NSGs cannot restrict an external workload-profile environment). To admit only Teams, take the AzureBotService service tag\'s IPv4 prefixes at deploy time with relay-ranges.sh (decision 49; az network list-service-tags), which the parameters file loads; VERIFY that Teams calls always come from them, and that they fit the ingress rule limit. The repo does not record the ranges, so there is no default. [\'0.0.0.0/0\'] opens it to the internet on purpose, the relay\'s own checks being the only gate.')
@minLength(1)
param relayAllowedCidrs array
@description('Ranges (for example a jump-host subnet) allowed to reach PostgreSQL on 5432 so the Entra administrator can create database roles.')
param postgresAdminSourcePrefixes array = []
@description('Required, closed by default (threat model T1). Ranges allowed to call the enrolment app: the edge appliance networks. The internal environment subnet is added for api\'s calls. Never a range that covers caeRelaySubnetPrefix.')
@minLength(1)
param enrolAllowedCidrs array
@description('Required, closed by default (threat model T1). Ranges allowed to reach the portal and partner API (web): the users\' and Prizmanyx\'s networks. Never list a range that covers caeRelaySubnetPrefix: a compromised relay must not reach web.')
@minLength(1)
param webAllowedCidrs array
@description('The administrators\' workstation ranges: added to web\'s and enrol\'s allow-lists, so an admin reaches the portal and enrol\'s health check from their own machine. Never a range that covers caeRelaySubnetPrefix (T1). PostgreSQL is separate (postgresAdminSourcePrefixes). Empty = none beyond the lists above.')
param adminAllowedCidrs array = []

// T1, enforced (review of #34): a range in web's or enrol's allow-lists that covers the relay subnet would let a
// compromised relay call them, so the deployment fails instead. Two IPv4 blocks overlap exactly when they agree at the
// shorter of their two prefixes. IPv6 ranges can't hold the (IPv4) relay subnet; a bare address is its /32.
var callerCidrs = map(
  filter(union(enrolAllowedCidrs, webAllowedCidrs, adminAllowedCidrs), c => !contains(c, ':')),
  c => contains(c, '/') ? c : '${c}/32'
)
var relayCidr = parseCidr(caeRelaySubnetPrefix)
var cidrsOverRelay = filter(
  callerCidrs,
  c =>
    parseCidr('${split(c, '/')[0]}/${min(int(split(c, '/')[1]), relayCidr.cidr)}').network == parseCidr('${relayCidr.network}/${min(int(split(c, '/')[1]), relayCidr.cidr)}').network
)
var callerCidrsChecked = empty(cidrsOverRelay)
  ? true
  : fail('enrolAllowedCidrs, webAllowedCidrs and adminAllowedCidrs must not cover caeRelaySubnetPrefix (threat model T1): ${join(cidrsOverRelay, ', ')}')
@description('Source range(s) of the internal environment\'s ingress proxy as the enrol and web containers see it: X-Forwarded-For is believed only from these peers (rate limits, audit addresses). Never 0.0.0.0/0. Empty = the internal environment subnet. VERIFY the range after the first deployment. It is never used for api\'s client certificates.')
param ingressSourceCidrs array = []

// ---------------------------------------------------------------- container apps

@description('Required. The images, by digest (<registry>/<repository>@sha256:...): deploy/images/build.sh records them in deploy/images/images.lock.json. api, enrol, worker and the verifier use the service image (omnifile-service), web the web image, the relay the relay image.')
@minLength(1)
param apiImage string
@minLength(1)
param workerImage string
@minLength(1)
param verifierImage string
@minLength(1)
param relayImage string
@minLength(1)
param webImage string
@minLength(1)
param enrolImage string
param enrolTargetPort int = 8080
param webTargetPort int = 8080
param relayTargetPort int = 8080
@description('Registry login server, for example myregistry.azurecr.io. Empty = images are public.')
param registryServer string = ''
@description('Resource id of the Azure Container Registry, so the identities get AcrPull. Empty = no assignment.')
param registryResourceId string = ''

@description('How edges and the relay reach api (threat model T5). tcp, the only value: a TCP ingress passes the connection through and api terminates mutual TLS itself (OMNIFILE_API_CLIENT_AUTH=direct_tls), with a server certificate from the edge CA, issued by enrol through Key Vault; edges pin that CA from their enrolment answer. An HTTP ingress with forwarded client certificates is not offered: api runs in the shared internal environment, whose ingress proxy is in the subnet every other app runs in, so no proxy range would be safe. It can return when api has an environment of its own.')
@allowed([ 'tcp' ])
param apiIngress string = 'tcp'
@description('apiIngress = tcp: the port api listens on and the environment exposes (unique in the environment; never 80, 443 or 36985).')
@minValue(1024)
@maxValue(65535)
param apiTcpPort int = 8443
@description('SHA-256 (hex) of the CA certificate that issued the enrol app\'s TLS certificate: new edges pin it (the enrolment code carries it). Empty: the portal can\'t add edges until it is set (deploy/azure/README.md, step 11).')
param enrolServerCaSha256 string = ''
@description('Deploy the `enrol` app: edge enrolment only (enrolment token, CSR, client certificate). Internal, no client certificates, and the only app that can sign with the CA key.')
param deployEnrol bool = true
@description('Deploy the `web` app: portal backend and partner API, client certificates optional. It has no storage and no Key Vault access (catalog only).')
param deployWeb bool = true
@description('Empty = the size\'s (2 standard, 1 small).')
param apiMinReplicas int?
@description('Empty = the size\'s (10 standard, 2 small).')
param apiMaxReplicas int?
param workerMinReplicas int = 1
@description('Empty = the size\'s (4 standard, 1 small).')
param workerMaxReplicas int?
param healthProbePath string = ''
param relayHealthProbePath string = '/healthz'

@description('Create the Azure Bot for Teams, as a user-assigned managed-identity bot on the worker\'s identity (teams.md 6.1): its Microsoft App ID is that identity\'s client id (output teamsBotAppId), its messaging endpoint the relay, with the Teams channel. VERIFY that a managed-identity bot works for Bot Connector tokens from Container Apps (real-tenant-checks.md).')
param deployTeamsBot bool = true
@allowed([ 'F0', 'S1' ])
param teamsBotSku string = 'F0'
@description('Only with deployTeamsBot = false: the Microsoft App ID of a bot registered elsewhere (the JWT audience the relay checks, and the worker\'s OMNIFILE_TEAMS_BOT_APP_ID).')
param relayBotAppId string = ''
@description('The Microsoft 365 tenant whose Teams users click the cards. Empty = the tenant of this deployment; set it when the Azure subscription and the Teams tenant differ, or every click is refused.')
param teamsTenantId string = ''
@description('JSON {kid: Ed25519 public key} that verifies card intents (public keys only, not secret). The relay and api both check them.')
param relayIntentPublicKeys string = '{}'
@description('The Teams app\'s id in the organisation\'s app catalog (not the manifest id), for proactive install: deploy/entra/register.sh --teams-app-zip prints it. Empty: the worker sends no cards.')
param teamsAppCatalogId string = ''
@description('SHA-256 fingerprints (hex) of the relay\'s client certificates api accepts on the intents path (`python -m omnifile_service.enrol issue-relay-cert` prints one). Empty: api answers 503 to every click.')
param relayCertSha256 array = []
@description('The key id the worker signs card intents with (a key of relayIntentPublicKeys).')
param teamsIntentKid string = 'intent'
@description('Second phase. Once the Key Vault secret teams-intent-signing-key exists (the Ed25519 private key, PKCS#8 PEM), set true and redeploy: the worker gets read access to that one secret and mounts it. Until then cards have no buttons.')
param teamsSecretsProvisioned bool = false
@description('The mailbox (UPN) the worker sends fallback email from. Empty: no email.')
param notifySender string = ''
@description('The portal\'s origin as users reach it (https://host). Empty = web\'s own FQDN.')
param webOrigin string = ''
@description('The portal\'s Entra app registration client id (deploy/entra/register.sh). Empty: web waits until it is set.')
param webClientId string = ''
@description('Bearer audiences web accepts on the partner API: the registration\'s application ID URI and client id (register.sh). Empty: partner tokens are refused.')
param webApiAudiences array = []
@description('Source address range of the environment ingress as the relay container sees it, so per-IP rate limiting uses real client addresses (uvicorn forwarded-allow-ips). Never *. Empty = the relay subnet. VERIFY the range after the first deployment.')
param relayForwardedAllowIps string = ''
@description('Second phase. After the CA has issued the relay\'s client certificate and the three secrets exist in Key Vault (relay-mtls-cert, relay-mtls-key, relay-ca-bundle), set true and redeploy: the relay identity then gets read access to those three secrets only, and the relay mounts them.')
param relaySecretsProvisioned bool = false
@description('The edge CA certificate (PEM; public), made once by `python -m omnifile_service.enrol init-kv-ca` with the vault key (README step 7). api trusts edge certificates it issued, and enrol checks it is the vault key\'s. Until it is set, api and enrol wait for it and edges can\'t enrol.')
param edgeCaCertPem string = ''
@description('Second phase. Once the Key Vault secrets api-lease-key and web-cursor-key exist (each the base64 of 32 random bytes; deploy/azure/README.md), set true and redeploy: api and web each read their own one only, as a Container Apps secret reference, never as plain env.')
param appSecretsProvisioned bool = false

@description('Verifier job trigger. Schedule (default): an execution every verifierCron, each replica one pass over the verify queue. Manual: only when started (az containerapp job start, or the worker through its job-starter role). Event: KEDA starts them from PGMQ queue depth (VERIFY).')
@allowed([ 'Schedule', 'Manual', 'Event' ])
param verifierTrigger string = 'Schedule'
@description('verifierTrigger = Schedule: when executions start (cron, UTC). A pass stops reading after OMNIFILE_VERIFIER_MAX_SECONDS (840 s), before the next run.')
param verifierCron string = '*/15 * * * *'
@description('A dedicated workload profile type (for example D8) for the verifier job. Empty = Consumption. Standard size only: a dedicated profile bills the plan management fee ($0.10 an hour) on top of its instances.')
param verifierDedicatedProfileType string = ''
param caeZoneRedundant bool = false

@description('Extra environment variables for every app. Array of { name, value }.')
param extraEnv array = []

// ---------------------------------------------------------------- postgres

@allowed([ '17', '18' ])
param postgresVersion string = '18'
@description('Empty = the size\'s (Standard_D4ds_v5 standard, Standard_B1ms small).')
param postgresSkuName string?
@description('Empty = the size\'s (GeneralPurpose standard, Burstable small).')
@allowed([ 'Burstable', 'GeneralPurpose', 'MemoryOptimized' ])
param postgresSkuTier string?
@description('Empty = the size\'s (128 standard, 32 small).')
param postgresStorageSizeGB int?
@description('Optional high availability. ZoneRedundant needs a region with availability zones. Burstable servers have none, so the deployment fails if it is set with one.')
@allowed([ 'Disabled', 'SameZone', 'ZoneRedundant' ])
param postgresHighAvailability string = 'Disabled'
param postgresGeoRedundantBackup bool = false
param postgresBackupRetentionDays int = 14
@description('Object id of the Entra group (or user) that administers PostgreSQL. It creates the database roles for the service identities.')
@minLength(36)
@maxLength(36)
param postgresEntraAdminObjectId string
param postgresEntraAdminName string
@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param postgresEntraAdminType string = 'Group'

// ---------------------------------------------------------------- storage

@description('Non-WORM layout: a ZRS live account (default tier Smart) plus an LRS/GRS archive account.')
param deployNonWormStorage bool = true
@allowed([ 'Standard_ZRS', 'Standard_GZRS', 'Standard_RAGZRS' ])
param liveRedundancy string = 'Standard_ZRS'
@allowed([ 'Standard_LRS', 'Standard_GRS' ])
param archiveRedundancy string = 'Standard_LRS'
@description('WORM shares. One account each. WORM accounts can never be switched back to non-WORM.')
param wormShares wormShare[] = []
@description('Containers to pre-create on the live and archive accounts.')
param nonWormContainers array = [
  'data'
]
param storageVersioning bool = true
param changeFeedRetentionDays int = 14
param blobSoftDeleteDays int = 7
@description('Daily Blob Inventory on the non-WORM accounts, for reconcile.')
param blobInventory bool = true
@description('The ArchiveAdmin maximum rewind depth, as a lifecycle rule on non-WORM accounts. 0 = no rule (the service prunes).')
param versionMaxAgeDays int = 0
@allowed([ 'Enabled', 'Disabled' ])
param storagePublicNetworkAccess string = 'Disabled'

// ---------------------------------------------------------------- key vault

@description('Deploy a Key Vault for the OmniFile CA key that signs edge certificates. Recommended.')
param deployKeyVault bool = true
@allowed([ 'standard', 'premium' ])
@description('premium keeps the CA key in an HSM. Recommended.')
param keyVaultSku string = 'premium'
param createCaKey bool = true
@allowed([ 'Enabled', 'Disabled' ])
param keyVaultPublicNetworkAccess string = 'Disabled'
@description('Purge protection on the vault. Keep it on in production: it can never be turned off again. A short-lived test deployment sets false so its vault can be purged at teardown.')
param keyVaultPurgeProtection bool = true
@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteDays int = 90
@description('Object id of the admin group (or user) that uploads the second-phase secrets (relay, app and Teams keys): Key Vault Secrets Officer on the vault. Never on the CA key, which no human can use. Empty = nobody.')
param keyVaultUploaderObjectId string = ''
@allowed([ 'User', 'Group' ])
param keyVaultUploaderType string = 'Group'

// ---------------------------------------------------------------- private endpoints (customer's job by default)

@description('Create storage and Key Vault private endpoints here. Default false: the customer creates them (decision 22).')
param createPrivateEndpoints bool = false
@description('Create the privatelink DNS zones and link them to the VNet. Forced on when createPrivateEndpoints is true.')
param createPrivateDnsZones bool = false

// ---------------------------------------------------------------- access

@description('Object id of the break-glass admin group that may use the storage data plane. Empty = nobody (decision 28 section 7).')
param breakGlassAdminGroupObjectId string = ''
@description('Owner is not offered: Storage Blob Data Owner includes the immutability override and version delete actions.')
@allowed([ 'Reader', 'Contributor' ])
param breakGlassBlobRole string = 'Reader'
@description('Also give the worker the role that holds immutableStorage runAsSuperUser, which per-version retention changes need. Off until pilot 4 shows it is required.')
param grantRetentionOfficerToWorker bool = false
@description('CanNotDelete resource locks on WORM storage accounts.')
param lockWormAccounts bool = true

// ---------------------------------------------------------------- migration

@description('The migration engine\'s Storage Mover (docs/design/migration-engine.md), as the worker\'s OMNIFILE_STORAGE_MOVER JSON: subscription_id, resource_group, storage_mover, project, source_endpoints, target_endpoints. The customer creates the Storage Mover, its project and endpoints in this resource group (setup guide). Empty = no copies are started.')
param storageMoverConfig string = ''
@description('The service\'s AWS accounts (OMNIFILE_AWS_ACCOUNTS JSON, backends/s3/config.py), for S3 sources and Roles Anywhere CRLs: api and the worker. Empty = no AWS.')
param awsAccounts string = ''
@description('The audience of the web-identity token the service presents to AWS (OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE, decision 43). Empty = the service default.')
param awsWebIdentityAudience string = ''
@description('Connect AWS\'s one-click link (docs/design/cloud-connect.md 2.2): the https URL in S3 where the release publishes deploy/aws/cloudformation/omnifile-connect.yaml (OMNIFILE_AWS_CONNECT_TEMPLATE_URL, web). Empty = the page offers the template for upload instead.')
param awsConnectTemplateUrl string = ''

// ================================================================ sizes (deploymentSize)
//
// standard is the production sizing, the same as before deploymentSize existed. small is the cheapest that keeps
// every constraint; the price of each resource at both sizes is in docs/milestones/results/wp16.md, and
// deploy/azure/check.sh pins both columns. What does not change with the size: private endpoints for every storage
// account and the vault, the two environments (the relay's is the only public one), one identity and one database
// role per app, the required allow-lists, api's TCP ingress with its own TLS, and the HSM-backed CA key.
//   - cpu and memory are Consumption-profile sizes (0.25 vCPU per 0.5 GiB). small runs Consumption only.
//   - min 0 scales to zero, for web and enrol only: their callers retry (api asks enrol for its server certificate
//     with backoff, the worker retries the CRL, edges and browsers retry), and the first call after an idle spell
//     waits for a replica to start.
//   - The relay stays warm at both sizes: Teams expects a quick answer to a card click, and a cold start (the image,
//     Python, the Bot Framework signing keys) can take longer than that.
//   - small's PostgreSQL (B1ms) has 35 user connections. `pool` is OMNIFILE_PG_POOL_MAX, the most connections one
//     replica holds (the worker's shared by every queue and sweep, service worker/pool.py). At small's maximum
//     replicas: api 2 x 4, web 4, enrol 2, worker 6, and the verifier's 2 (a lock and its queue) = 22, plus at most
//     3 short ones (api's start-up read per replica, web's Settings storage run) = 25, under 30 with 10 to spare for
//     an administrator and the migrations. The worker's 6 are in lanes: 2 reserved for restores, edge instructions
//     and notify, and of the other 4 at most 2 for the long holders (read-back, migration, exports, seeding,
//     reconcile). standard sets no pool: api, web and enrol keep 10 per replica, and the worker a connection per
//     queue. On a B1ms, `sizeChecked` refuses settings that could open more (catalogConnections).
var sizes = {
  standard: {
    api: {
      cpu: '1'
      memory: '2Gi'
      min: 2
      max: 10
    }
    worker: {
      cpu: '1'
      memory: '2Gi'
      max: 4
    }
    // web's max follows api's.
    web: {
      cpu: '0.5'
      memory: '1Gi'
      min: 2
    }
    enrol: {
      cpu: '0.5'
      memory: '1Gi'
      min: 1
      max: 3
    }
    relay: {
      cpu: '0.5'
      memory: '1Gi'
      min: 1
      max: 3
    }
    // eventMax: executions at once with verifierTrigger = Event (each a lock and its queue: 2 connections).
    verifier: {
      cpu: '1'
      memory: '2Gi'
      eventMax: 10
    }
    postgres: {
      skuName: 'Standard_D4ds_v5'
      skuTier: 'GeneralPurpose'
      storageSizeGB: 128
    }
    logRetentionDays: 90
    logDailyCapGb: '-1'
    alertFrequency: 'PT5M'
  }
  small: {
    api: {
      cpu: '0.5'
      memory: '1Gi'
      min: 1
      max: 2
      pool: 4
    }
    worker: {
      cpu: '0.5'
      memory: '1Gi'
      max: 1
      pool: 6
    }
    web: {
      cpu: '0.25'
      memory: '0.5Gi'
      min: 0
      max: 1
      pool: 4
    }
    enrol: {
      cpu: '0.25'
      memory: '0.5Gi'
      min: 0
      max: 1
      pool: 2
    }
    relay: {
      cpu: '0.25'
      memory: '0.5Gi'
      min: 1
      max: 2
    }
    verifier: {
      cpu: '0.5'
      memory: '1Gi'
      eventMax: 1
    }
    postgres: {
      skuName: 'Standard_B1ms'
      skuTier: 'Burstable'
      storageSizeGB: 32
    }
    logRetentionDays: 30
    logDailyCapGb: '0.5'
    alertFrequency: 'PT15M'
  }
}
var size = sizes[deploymentSize]
var apiMin = apiMinReplicas ?? size.api.min
var apiMax = apiMaxReplicas ?? size.api.max
var workerMax = workerMaxReplicas ?? size.worker.max
var webMax = size.web.?max ?? apiMax
var pgSkuName = postgresSkuName ?? size.postgres.skuName
var pgSkuTier = postgresSkuTier ?? size.postgres.skuTier
var pgStorageSizeGB = postgresStorageSizeGB ?? size.postgres.storageSizeGB
var logRetention = logRetentionDays ?? size.logRetentionDays
var logCap = logDailyCapGb ?? size.logDailyCapGb
// OMNIFILE_PG_POOL_MAX for each app that pools, where its size sets one (small only).
var poolEnv = {
  api: contains(size.api, 'pool') ? [
    {
      name: 'OMNIFILE_PG_POOL_MAX'
      value: string(size.api.?pool)
    }
  ] : []
  worker: contains(size.worker, 'pool') ? [
    {
      name: 'OMNIFILE_PG_POOL_MAX'
      value: string(size.worker.?pool)
    }
  ] : []
  web: contains(size.web, 'pool') ? [
    {
      name: 'OMNIFILE_PG_POOL_MAX'
      value: string(size.web.?pool)
    }
  ] : []
  enrol: contains(size.enrol, 'pool') ? [
    {
      name: 'OMNIFILE_PG_POOL_MAX'
      value: string(size.enrol.?pool)
    }
  ] : []
}
// The catalog connections every app can hold at its maximum replicas (docs/milestones/results/wp16.md): each pooled
// app's replicas times its pool (10 when its size sets none), the worker's budget (without one it keeps a
// connection per queue and opens one per running sweep: up to about 37), the verifier's executions (a lock and its
// queue each), and the short ones (api's start-up read per replica, web's Settings storage run).
var catalogPools = {
  api: size.api.?pool ?? 10
  web: size.web.?pool ?? 10
  enrol: size.enrol.?pool ?? 10
  worker: size.worker.?pool ?? 37
}
var catalogConnectionsBy = {
  api: apiMax * catalogPools.api
  web: deployWeb ? webMax * catalogPools.web : 0
  enrol: deployEnrol ? size.enrol.max * catalogPools.enrol : 0
  worker: workerMax * catalogPools.worker
  verifier: 2 * (verifierTrigger == 'Event' ? size.verifier.eventMax : 1)
  short: apiMax + (deployWeb ? 1 : 0)
}
var catalogConnections = catalogConnectionsBy.api + catalogConnectionsBy.web + catalogConnectionsBy.enrol + catalogConnectionsBy.worker + catalogConnectionsBy.verifier + catalogConnectionsBy.short
// A B1ms has 35 user connections: the apps may take 30 of them, with 5 more to spare at that.
var b1msConnectionLimit = 30
var b1msConnectionMargin = 5
// Refused at deployment: small is Consumption only, a Burstable server has no high availability, and a B1ms can't
// take more connections than the apps can open (an override of the replicas or the server, or standard on a B1ms).
var sizeChecked = deploymentSize == 'small' && !empty(verifierDedicatedProfileType)
  ? fail('deploymentSize small runs on the Consumption profile only: leave verifierDedicatedProfileType empty')
  : pgSkuTier == 'Burstable' && postgresHighAvailability != 'Disabled'
      ? fail('a Burstable PostgreSQL server has no high availability: set postgresHighAvailability to Disabled, or a GeneralPurpose server')
      : pgSkuName == 'Standard_B1ms' && catalogConnections + b1msConnectionMargin > b1msConnectionLimit
          ? fail('a Standard_B1ms PostgreSQL server has 35 user connections, and these settings let the apps open ${catalogConnections} (with ${b1msConnectionMargin} to spare, at most ${b1msConnectionLimit}): use deploymentSize small with its replicas and verifier trigger, or a bigger server such as Standard_B2s')
          : true

// ================================================================ names

var uniq = take(uniqueString(resourceGroup().id), 10)
var shortPrefix = take(namePrefix, 6)
var liveName = '${shortPrefix}live${uniq}'
var archiveName = '${shortPrefix}arch${uniq}'
var wormNames = [for s in wormShares: '${shortPrefix}w${s.name}${uniq}']
var postgresName = '${namePrefix}-pg-${uniq}'
var keyVaultName = take('${namePrefix}-kv-${uniq}', 24)
var caKeyName = 'omnifile-edge-ca'
// The relay's mutual-TLS material, uploaded to Key Vault after the CA has issued the relay's client certificate.
var relaySecretNames = [
  'relay-mtls-cert'
  'relay-mtls-key'
  'relay-ca-bundle'
]
// Keys every replica of an app shares (stub leases, opaque cursors), read from Key Vault in the second phase.
var apiLeaseKeySecret = 'api-lease-key'
var webCursorKeySecret = 'web-cursor-key'
var appKeysEnabled = deployKeyVault && appSecretsProvisioned
// The edge CA certificate, mounted as a file into api (its trust anchor) and enrol (checked against the vault key).
var caCertEnabled = !empty(edgeCaCertPem)
var caCertSecrets = caCertEnabled ? [
  {
    name: 'edge-ca-cert'
    value: edgeCaCertPem
  }
] : []
var caCertVolumes = caCertEnabled ? [
  {
    name: 'edge-ca'
    storageType: 'Secret'
    secrets: [
      {
        secretRef: 'edge-ca-cert'
        path: 'ca.pem'
      }
    ]
  }
] : []
var caCertMounts = caCertEnabled ? [
  {
    volumeName: 'edge-ca'
    mountPath: '/mnt/edge-ca'
  }
] : []
var caCertEnv = caCertEnabled ? [
  {
    name: 'OMNIFILE_CA_CERT'
    value: '/mnt/edge-ca/ca.pem'
  }
] : []
var pgDatabase = 'omnifile'

// Each app's command: Container Apps' `command` replaces the image's ENTRYPOINT and drops its CMD, so an app runs
// exactly this whatever the image says (the service image serves four apps). service/tests/test_deploy_env.py
// checks each against the code, and deploy/images/smoke.sh runs each image with it.
var appCommands = {
  api: [ 'python', '-m', 'omnifile_service.api' ]
  enrol: [ 'python', '-m', 'omnifile_service.enrol' ]
  web: [ 'python', '-m', 'omnifile_service.web' ]
  worker: [ 'python', '-m', 'omnifile_service.worker' ]
  // The verifier job runs one pass over the worker's verify queue and exits (worker/verifier.py; its role is
  // omnifile_worker, catalog/privileges.py).
  verifier: [ 'python', '-m', 'omnifile_service.worker.verifier' ]
  relay: [ 'omnifile-relay' ]
}

// The Teams tenant: whose users click the cards (api, worker, web and the relay all check it).
var teamsTenant = empty(teamsTenantId) ? tenant().tenantId : teamsTenantId
// The bot's Microsoft App ID: the worker's identity when this template creates the bot (a managed-identity bot).
var botAppId = deployTeamsBot ? idWorker.properties.clientId : relayBotAppId
var teamsIntentSecret = 'teams-intent-signing-key'
var teamsKeyEnabled = deployKeyVault && teamsSecretsProvisioned
var hasIntentKeys = !empty(relayIntentPublicKeys) && relayIntentPublicKeys != '{}'
// AWS (decision 43): api reads S3 sources' settings for edge credentials, the worker for everything else.
var awsEnv = concat(empty(awsAccounts) ? [] : [
  {
    name: 'OMNIFILE_AWS_ACCOUNTS'
    value: awsAccounts
  }
], empty(awsWebIdentityAudience) ? [] : [
  {
    name: 'OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE'
    value: awsWebIdentityAudience
  }
])

// ---- built-in role definition ids
var roleStorageBlobDataContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var roleStorageBlobDataReader = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
var roleKeyVaultCryptoUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '12338af0-0e69-4776-bea7-57ae8d297424')
var roleKeyVaultSecretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var roleAcrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var breakGlassRoleId = breakGlassBlobRole == 'Reader' ? roleStorageBlobDataReader : roleStorageBlobDataContributor

// ================================================================ monitoring and identities

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    name: '${namePrefix}-law-${uniq}'
    location: location
    tags: tags
    retentionInDays: logRetention
    dailyQuotaGb: logCap
  }
}

// One identity per workload, so each gets only what it needs and can be revoked on its own.
resource idApi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-api'
  location: location
  tags: tags
}

resource idEnrol 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-enrol'
  location: location
  tags: tags
}

resource idWeb 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-web'
  location: location
  tags: tags
}

resource idWorker 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-worker'
  location: location
  tags: tags
}

resource idVerifier 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-verifier'
  location: location
  tags: tags
}

resource idRelay 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-relay'
  location: location
  tags: tags
}

// ================================================================ metrics and alerts (decision 37)

module appMetrics 'modules/app-metrics.bicep' = if (metricsEnabled) {
  name: 'app-metrics'
  params: {
    namePrefix: namePrefix
    uniq: uniq
    location: location
    tags: tags
    workspaceId: monitoring.outputs.id
    publisherPrincipalIds: [
      idApi.properties.principalId
      idWeb.properties.principalId
      idWorker.properties.principalId
    ]
    actionGroupId: alertActionGroupId
    alertsEnabled: alertsEnabled
    alertEvaluationFrequency: size.alertFrequency
    privateLink: monitorPrivateLink
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    vnetId: network.outputs.vnetId
  }
}

var metricsEnv = metricsEnabled ? [
  {
    name: 'OMNIFILE_METRICS_EXPORTER'
    value: 'azuremonitor'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appMetrics!.outputs.connectionString
  }
] : []

module edgeMonitoring 'modules/edge-monitoring.bicep' = if (!empty(edgeArcMachineIds)) {
  name: 'edge-monitoring'
  params: {
    namePrefix: namePrefix
    uniq: uniq
    location: location
    tags: tags
    workspaceId: monitoring.outputs.id
  }
}

module edgeMachines 'modules/edge-monitoring-machine.bicep' = [for (m, i) in edgeArcMachineIds: {
  name: 'edge-monitoring-${i}'
  scope: resourceGroup(split(m, '/')[2], split(m, '/')[4])
  params: {
    machineName: last(split(m, '/'))
    location: location
    dcrId: edgeMonitoring!.outputs.id
  }
}]

// ================================================================ network

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    name: '${namePrefix}-vnet'
    location: location
    tags: tags
    addressPrefix: vnetAddressPrefix
    caeInternalSubnetPrefix: caeInternalSubnetPrefix
    caeRelaySubnetPrefix: caeRelaySubnetPrefix
    postgresSubnetPrefix: postgresSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    postgresAdminSourcePrefixes: postgresAdminSourcePrefixes
    // api's TCP port, for the edges, admins and the relay (apiIngress = tcp).
    extraIngressPorts: apiIngress == 'tcp' ? [ string(apiTcpPort) ] : []
    // The relay calls api only, on its TCP port (review of #34: it had every ingress port).
    relayToInternalPorts: [ string(apiTcpPort) ]
  }
}

// ================================================================ postgres

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    name: postgresName
    location: location
    tags: tags
    delegatedSubnetId: network.outputs.postgresSubnetId
    vnetId: network.outputs.vnetId
    workspaceId: monitoring.outputs.id
    version: postgresVersion
    skuName: pgSkuName
    skuTier: pgSkuTier
    storageSizeGB: pgStorageSizeGB
    highAvailabilityMode: sizeChecked ? postgresHighAvailability : 'Disabled'
    geoRedundantBackup: postgresGeoRedundantBackup
    backupRetentionDays: postgresBackupRetentionDays
    databaseName: pgDatabase
    entraAdminObjectId: postgresEntraAdminObjectId
    entraAdminName: postgresEntraAdminName
    entraAdminType: postgresEntraAdminType
  }
}

// ================================================================ storage

module storageLive 'modules/storage-account.bicep' = if (deployNonWormStorage) {
  name: 'storage-live'
  params: {
    name: liveName
    location: location
    tags: tags
    workspaceId: monitoring.outputs.id
    layout: 'live'
    skuName: liveRedundancy
    versioningEnabled: storageVersioning
    changeFeedRetentionDays: changeFeedRetentionDays
    softDeleteDays: blobSoftDeleteDays
    publicNetworkAccess: storagePublicNetworkAccess
    containers: nonWormContainers
    inventoryEnabled: blobInventory
    versionMaxAgeDays: versionMaxAgeDays
  }
}

module storageArchive 'modules/storage-account.bicep' = if (deployNonWormStorage) {
  name: 'storage-archive'
  params: {
    name: archiveName
    location: location
    tags: tags
    workspaceId: monitoring.outputs.id
    layout: 'archive'
    skuName: archiveRedundancy
    versioningEnabled: storageVersioning
    changeFeedRetentionDays: changeFeedRetentionDays
    softDeleteDays: blobSoftDeleteDays
    publicNetworkAccess: storagePublicNetworkAccess
    containers: nonWormContainers
    inventoryEnabled: blobInventory
    versionMaxAgeDays: versionMaxAgeDays
  }
}

module storageWorm 'modules/storage-account.bicep' = [for (s, i) in wormShares: {
  name: 'storage-worm-${s.name}'
  params: {
    name: wormNames[i]
    location: location
    tags: union(tags, {
      'omnifile-share': s.name
    })
    workspaceId: monitoring.outputs.id
    layout: 'worm'
    skuName: s.?redundancy ?? 'Standard_LRS'
    versioningEnabled: true
    changeFeedRetentionDays: changeFeedRetentionDays
    softDeleteDays: blobSoftDeleteDays
    publicNetworkAccess: storagePublicNetworkAccess
    containers: union(s.?containers ?? [ 'data' ], [ 'recall' ])
    // The default policy covers the data containers only. The recall container carries no policy so that recall copies can be deleted.
    policyContainers: s.?containers ?? [ 'data' ]
    wormRetentionDays: s.?retentionDays ?? 0
    lockAccount: lockWormAccounts
    inventoryEnabled: false
  }
}]

// ================================================================ key vault

module keyVault 'modules/keyvault.bicep' = if (deployKeyVault) {
  name: 'keyvault'
  params: {
    name: keyVaultName
    location: location
    tags: tags
    workspaceId: monitoring.outputs.id
    skuName: keyVaultSku
    publicNetworkAccess: keyVaultPublicNetworkAccess
    createCaKey: createCaKey
    caKeyName: caKeyName
    purgeProtection: keyVaultPurgeProtection
    softDeleteRetentionInDays: keyVaultSoftDeleteDays
  }
}

// ================================================================ roles and assignments

module roles 'modules/roles.bicep' = {
  name: 'roles'
}

var apiPrincipal = {
  principalId: idApi.properties.principalId
  principalType: 'ServicePrincipal'
}
var workerPrincipal = {
  principalId: idWorker.properties.principalId
  principalType: 'ServicePrincipal'
}
var verifierPrincipal = {
  principalId: idVerifier.properties.principalId
  principalType: 'ServicePrincipal'
}
var relayPrincipal = {
  principalId: idRelay.properties.principalId
  principalType: 'ServicePrincipal'
}
var webPrincipal = {
  principalId: idWeb.properties.principalId
  principalType: 'ServicePrincipal'
}
var enrolPrincipal = {
  principalId: idEnrol.properties.principalId
  principalType: 'ServicePrincipal'
}
// idWeb has no data role anywhere: the web app reads and writes the catalog only. It has no storage data role and no
// delegator role; in Key Vault it reads its own cursor key only. Its one control-plane role is the read-only
// "OmniFile Settings Reader" on this resource group (webSettingsReader below), for the Settings checks.

// api: mints per-blob user-delegation SAS for the edge, so it needs read/write and the right to request a delegation key,
//   and nothing more: the custom "OmniFile SAS Minter" role has no delete of any kind (a SAS is capped by its signer's role).
// worker: tier changes, server-side copies, restores, index tags, legal holds. The custom "OmniFile Blob Worker" role has no
//   immutability override (runAsSuperUser), no version delete and no permanent delete. Storage Blob Data Owner is NOT used.
// verifier: reads only.
var serviceBlobAssignments = [
  union(apiPrincipal, { roleDefinitionId: roles.outputs.sasMinterRoleId })
  union(workerPrincipal, { roleDefinitionId: roles.outputs.blobWorkerRoleId })
  union(verifierPrincipal, { roleDefinitionId: roleStorageBlobDataReader })
]
var breakGlassAssignments = empty(breakGlassAdminGroupObjectId) ? [] : [
  {
    principalId: breakGlassAdminGroupObjectId
    principalType: 'Group'
    roleDefinitionId: breakGlassRoleId
  }
]
var nonWormAssignments = concat(serviceBlobAssignments, breakGlassAssignments)
// WORM accounts: the worker also manages container policies (create, extend, lock; no delete). Per-version retention changes
// need immutableStorage runAsSuperUser, granted only if grantRetentionOfficerToWorker is set.
var wormAssignments = concat(nonWormAssignments, [
  union(workerPrincipal, { roleDefinitionId: roles.outputs.wormManagerRoleId })
], grantRetentionOfficerToWorker ? [
  union(workerPrincipal, { roleDefinitionId: roles.outputs.retentionOfficerRoleId })
] : [])

module rbacLive 'modules/rbac-storage.bicep' = if (deployNonWormStorage) {
  name: 'rbac-live'
  params: {
    storageAccountName: liveName
    assignments: nonWormAssignments
  }
  dependsOn: [
    storageLive
  ]
}

module rbacArchive 'modules/rbac-storage.bicep' = if (deployNonWormStorage) {
  name: 'rbac-archive'
  params: {
    storageAccountName: archiveName
    assignments: nonWormAssignments
  }
  dependsOn: [
    storageArchive
  ]
}

module rbacWorm 'modules/rbac-storage.bicep' = [for (s, i) in wormShares: {
  name: 'rbac-worm-${s.name}'
  params: {
    storageAccountName: wormNames[i]
    assignments: wormAssignments
  }
  dependsOn: [
    storageWorm[i]
  ]
}]

// Key Vault, one object at a time (no vault-wide data role for any workload):
//   enrol: Crypto User on the CA key only. It is the only identity that can sign with it (edge certificates).
//   relay: Secrets User on its own three secrets only, and only in the second phase (relaySecretsProvisioned).
//   api, web: Secrets User on their own shared key only (api-lease-key, web-cursor-key), in the second phase
//     (appSecretsProvisioned). Before it, the keys come from extraEnv, or each replica makes its own.
//   worker: Secrets User on the Teams intent signing key only, in the second phase (teamsSecretsProvisioned).
//   verifier: nothing in Key Vault.
module rbacCaKey 'modules/rbac-scope.bicep' = if (deployKeyVault && createCaKey && deployEnrol) {
  name: 'rbac-ca-key'
  params: {
    kind: 'key'
    resourceName: keyVaultName
    childName: caKeyName
    assignments: [
      union(enrolPrincipal, { roleDefinitionId: roleKeyVaultCryptoUser })
    ]
  }
  dependsOn: [
    keyVault
  ]
}

module rbacRelaySecrets 'modules/rbac-scope.bicep' = [for n in relaySecretNames: if (deployKeyVault && relaySecretsProvisioned) {
  name: 'rbac-relay-${n}'
  params: {
    kind: 'secret'
    resourceName: keyVaultName
    childName: n
    assignments: [
      union(relayPrincipal, { roleDefinitionId: roleKeyVaultSecretsUser })
    ]
  }
  dependsOn: [
    keyVault
  ]
}]

module rbacApiLeaseKey 'modules/rbac-scope.bicep' = if (appKeysEnabled) {
  name: 'rbac-api-lease-key'
  params: {
    kind: 'secret'
    resourceName: keyVaultName
    childName: apiLeaseKeySecret
    assignments: [
      union(apiPrincipal, { roleDefinitionId: roleKeyVaultSecretsUser })
    ]
  }
  dependsOn: [
    keyVault
  ]
}

module rbacWebCursorKey 'modules/rbac-scope.bicep' = if (appKeysEnabled && deployWeb) {
  name: 'rbac-web-cursor-key'
  params: {
    kind: 'secret'
    resourceName: keyVaultName
    childName: webCursorKeySecret
    assignments: [
      union(webPrincipal, { roleDefinitionId: roleKeyVaultSecretsUser })
    ]
  }
  dependsOn: [
    keyVault
  ]
}

// The admins who upload the second-phase secrets. Secrets only: Secrets Officer carries no key operations.
var roleKeyVaultSecretsOfficer = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
module rbacSecretsOfficer 'modules/rbac-scope.bicep' = if (deployKeyVault && !empty(keyVaultUploaderObjectId)) {
  name: 'rbac-secrets-officer'
  params: {
    kind: 'keyvault'
    resourceName: keyVaultName
    assignments: [
      {
        principalId: keyVaultUploaderObjectId
        principalType: keyVaultUploaderType
        roleDefinitionId: roleKeyVaultSecretsOfficer
      }
    ]
  }
  dependsOn: [
    keyVault
  ]
}

module rbacTeamsIntentKey 'modules/rbac-scope.bicep' = if (teamsKeyEnabled) {
  name: 'rbac-teams-intent-key'
  params: {
    kind: 'secret'
    resourceName: keyVaultName
    childName: teamsIntentSecret
    assignments: [
      union(workerPrincipal, { roleDefinitionId: roleKeyVaultSecretsUser })
    ]
  }
  dependsOn: [
    keyVault
  ]
}

var registryParts = split(registryResourceId, '/')
module rbacRegistry 'modules/rbac-scope.bicep' = if (!empty(registryResourceId)) {
  name: 'rbac-registry'
  scope: resourceGroup(registryParts[2], registryParts[4])
  params: {
    kind: 'registry'
    resourceName: last(registryParts)
    assignments: [
      union(apiPrincipal, { roleDefinitionId: roleAcrPull })
      union(enrolPrincipal, { roleDefinitionId: roleAcrPull })
      union(webPrincipal, { roleDefinitionId: roleAcrPull })
      union(workerPrincipal, { roleDefinitionId: roleAcrPull })
      union(verifierPrincipal, { roleDefinitionId: roleAcrPull })
      union(relayPrincipal, { roleDefinitionId: roleAcrPull })
    ]
  }
}

// ================================================================ private endpoints (optional) and DNS zones

#disable-next-line no-hardcoded-env-urls
var vaultZone = 'privatelink.vaultcore.azure.net'
var blobZone = 'privatelink.blob.${environment().suffixes.storage}'
var zoneNames = concat([ blobZone ], deployKeyVault ? [ vaultZone ] : [])
var withZones = createPrivateDnsZones || createPrivateEndpoints

module privateDnsZones 'modules/private-dns-zones.bicep' = if (withZones) {
  name: 'private-dns-zones'
  params: {
    tags: tags
    vnetId: network.outputs.vnetId
    zoneNames: zoneNames
  }
}

// Names are known at the start of the deployment, so the endpoint targets are built from names, not module outputs.
var wormTargets = [for n in wormNames: { name: n }]
var storageTargets = concat(deployNonWormStorage ? [
  { name: liveName }
  { name: archiveName }
] : [], wormTargets)

module storagePrivateEndpoints 'modules/private-endpoint.bicep' = [for t in storageTargets: if (createPrivateEndpoints) {
  name: 'pe-${t.name}'
  params: {
    name: 'pe-${t.name}-blob'
    location: location
    tags: tags
    subnetId: network.outputs.privateEndpointSubnetId
    targetResourceId: resourceId('Microsoft.Storage/storageAccounts', t.name)
    groupId: 'blob'
    privateDnsZoneId: resourceId('Microsoft.Network/privateDnsZones', blobZone)
  }
  dependsOn: [
    privateDnsZones
    storageLive
    storageArchive
    storageWorm
  ]
}]

module keyVaultPrivateEndpoint 'modules/private-endpoint.bicep' = if (createPrivateEndpoints && deployKeyVault) {
  name: 'pe-keyvault'
  params: {
    name: 'pe-${keyVaultName}-vault'
    location: location
    tags: tags
    subnetId: network.outputs.privateEndpointSubnetId
    targetResourceId: resourceId('Microsoft.KeyVault/vaults', keyVaultName)
    groupId: 'vault'
    privateDnsZoneId: resourceId('Microsoft.Network/privateDnsZones', vaultZone)
  }
  dependsOn: [
    privateDnsZones
    keyVault
  ]
}

// ================================================================ container apps: internal environment

module caeInternal 'modules/cae.bicep' = {
  name: 'cae-internal'
  params: {
    name: 'cae-${namePrefix}-internal'
    location: location
    tags: tags
    subnetId: network.outputs.caeInternalSubnetId
    internal: true
    workspaceName: '${namePrefix}-law-${uniq}'
    zoneRedundant: caeZoneRedundant
    dedicatedProfileType: sizeChecked ? verifierDedicatedProfileType : ''
  }
  dependsOn: [
    monitoring
  ]
}

// Resolve <app>.<internal default domain> inside the VNet. On-prem edge appliances need a conditional forwarder to it.
module caeInternalDns 'modules/dns-zone-for-env.bicep' = {
  name: 'cae-internal-dns'
  params: {
    tags: tags
    defaultDomain: caeInternal.outputs.defaultDomain
    staticIp: caeInternal.outputs.staticIp
    vnetId: network.outputs.vnetId
  }
}

// Each app gets only the settings it needs. The web app gets the catalog and nothing about storage or Key Vault.
// The catalog (service catalog/connect.py; README "The catalog connection"): every app signs in to PostgreSQL with
// Entra as its own managed identity (AZURE_CLIENT_ID; OMNIFILE_PG_USER is that identity's name, its PostgreSQL
// principal), with an access token as the password, and acts as its app's group role. Storage accounts are not app
// settings: the service reads them from catalog.backend rows (the storageAccounts output names them).
var pgEnv = [
  {
    name: 'OMNIFILE_PG_HOST'
    value: postgres.outputs.fqdn
  }
  {
    name: 'OMNIFILE_PG_DATABASE'
    value: pgDatabase
  }
  {
    name: 'OMNIFILE_PG_SSLMODE'
    value: 'verify-full'
  }
]
var keyVaultUri = deployKeyVault ? keyVault.?outputs.uri ?? '' : ''

// Certificate renewal: an edge renews at api (mutual TLS). api cannot sign, so it forwards the CSR to enrol's internal
// signing endpoint (/internal/v0/sign) over the internal environment, authenticated with api's managed identity (an Entra token
// for the enrol audience). enrol remains the only signer. Edges must be refused on /internal/*: path filtering is enforced
// by enrol itself, which accepts only a token whose object id is api's (OMNIFILE_ENROL_ALLOWED_CALLER_OID), and only from
// api's subnet (OMNIFILE_ENROL_SIGN_SOURCES).
var enrolUrl = deployEnrol ? 'https://${enrol.?outputs.fqdn ?? ''}' : ''

// The ingress proxy's addresses: the only peers whose X-Forwarded-For enrol and web believe.
var ingressSources = join(empty(ingressSourceCidrs) ? [ caeInternalSubnetPrefix ] : ingressSourceCidrs, ',')

// api's name and URL, built from the environment's domain so api's own settings (its server certificate's name, the
// api_url edges are told) don't depend on the api module. An app with external ingress is <name>.<default domain>.
var apiName = '${namePrefix}-api'
var apiFqdn = '${apiName}.${caeInternal.outputs.defaultDomain}'
var apiTcp = apiIngress == 'tcp'
var apiUrl = apiTcp ? 'https://${apiFqdn}:${apiTcpPort}' : 'https://${apiFqdn}'
var webOriginUrl = empty(webOrigin) ? 'https://${namePrefix}-web.${caeInternal.outputs.defaultDomain}' : webOrigin

// T5 (threat model): api terminates mutual TLS itself; an X-Forwarded-Client-Cert header is refused. Its server
// certificate comes from enrol (the edge CA, through Key Vault) at start, for apiFqdn. OMNIFILE_API_APP_SUBNETS names
// where the other apps run: should api ever be switched to forwarded mode (extraEnv), it refuses a proxy range
// overlapping them.
var apiAuthEnv = [
  {
    name: 'OMNIFILE_API_CLIENT_AUTH'
    value: 'direct_tls'
  }
  {
    name: 'PORT'
    value: string(apiTcpPort)
  }
  {
    name: 'OMNIFILE_API_SERVER_CERT_URL'
    value: deployEnrol ? '${enrolUrl}/internal/v0/sign' : ''
  }
  {
    name: 'OMNIFILE_API_APP_SUBNETS'
    value: '${caeInternalSubnetPrefix},${caeRelaySubnetPrefix}'
  }
]

// The Teams intents api checks (teams.md 4): the relay's certificate pins, the intent keys and the tenant.
var apiTeamsEnv = concat([
  {
    name: 'OMNIFILE_TENANT_ID'
    value: teamsTenant
  }
  {
    name: 'OMNIFILE_PORTAL_URL'
    value: webOriginUrl
  }
  {
    name: 'OMNIFILE_TEAMS_RELAY_CERT_SHA256'
    value: join(relayCertSha256, ',')
  }
], hasIntentKeys ? [
  {
    name: 'OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS'
    value: relayIntentPublicKeys
  }
] : [])

// api: the edge control API, mutual TLS. Reads and writes blobs (mints SAS). No Key Vault, so it cannot sign certificates.
module api 'modules/app.bicep' = {
  name: 'app-api'
  params: {
    name: apiName
    location: location
    tags: tags
    environmentId: caeInternal.outputs.id
    identityId: idApi.id
    image: apiImage
    command: appCommands.api
    registryServer: registryServer
    // external on an INTERNAL environment = published on the internal load balancer, reachable from the VNet and
    // from on-prem over ExpressRoute/VPN, never from the internet.
    ingress: 'external'
    transport: 'tcp'
    targetPort: apiTcpPort
    exposedPort: apiTcpPort
    healthProbePath: healthProbePath
    cpu: size.api.cpu
    memory: size.api.memory
    minReplicas: apiMin
    maxReplicas: apiMax
    env: concat(pgEnv, poolEnv.api, extraEnv, metricsEnv, [
      {
        name: 'AZURE_CLIENT_ID'
        value: idApi.properties.clientId
      }
      {
        name: 'OMNIFILE_PG_USER'
        value: idApi.name
      }
      {
        // Returned to edges as api_url (renewals).
        name: 'OMNIFILE_API_URL'
        value: apiUrl
      }
      {
        name: 'OMNIFILE_ENROL_SIGN_URL'
        value: deployEnrol ? '${enrolUrl}/internal/v0/sign' : ''
      }
      {
        // Audience for the Entra token api requests with its managed identity. VERIFY: the target identity's client id works as the audience.
        name: 'OMNIFILE_ENROL_AUDIENCE'
        value: idEnrol.properties.clientId
      }
    ], apiAuthEnv, apiTeamsEnv, awsEnv, caCertEnv, appKeysEnabled ? [
      {
        name: 'OMNIFILE_API_LEASE_KEY'
        secretRef: apiLeaseKeySecret
      }
    ] : [])
    secrets: concat(caCertSecrets, appKeysEnabled ? [
      {
        name: apiLeaseKeySecret
        keyVaultUrl: '${keyVaultUri}secrets/${apiLeaseKeySecret}'
        identity: idApi.id
      }
    ] : [])
    volumes: caCertVolumes
    volumeMounts: caCertMounts
  }
  dependsOn: [
    rbacApiLeaseKey
  ]
}

// enrol: edge enrolment only (token -> CSR -> client certificate). No client certificates (a new edge has none), small, and
// the only identity with sign rights on the CA key. enrolAllowedCidrs (required) limits who can call it.
module enrol 'modules/app.bicep' = if (deployEnrol) {
  name: 'app-enrol'
  params: {
    name: '${namePrefix}-enrol'
    location: location
    tags: tags
    environmentId: caeInternal.outputs.id
    identityId: idEnrol.id
    image: enrolImage
    command: appCommands.enrol
    registryServer: registryServer
    ingress: 'external'
    targetPort: enrolTargetPort
    clientCertificateMode: 'ignore'
    // api calls enrol from inside the environment, so the allow list also admits the internal subnet.
    // VERIFY: the source address Envoy shows for app-to-app calls; if it is outside the subnet, add its range to enrolAllowedCidrs.
    ingressAllowedCidrs: callerCidrsChecked ? union(enrolAllowedCidrs, adminAllowedCidrs, [ caeInternalSubnetPrefix ]) : []
    healthProbePath: healthProbePath
    cpu: size.enrol.cpu
    memory: size.enrol.memory
    minReplicas: size.enrol.min
    maxReplicas: size.enrol.max
    env: concat(pgEnv, poolEnv.enrol, extraEnv, [
      {
        name: 'AZURE_CLIENT_ID'
        value: idEnrol.properties.clientId
      }
      {
        name: 'OMNIFILE_PG_USER'
        value: idEnrol.name
      }
      {
        name: 'OMNIFILE_KEYVAULT_URI'
        value: keyVaultUri
      }
      {
        name: 'OMNIFILE_CA_KEY_NAME'
        value: caKeyName
      }
      {
        // /internal/v0/sign accepts only an Entra token for this audience whose object id is api's.
        name: 'OMNIFILE_ENROL_AUDIENCE'
        value: idEnrol.properties.clientId
      }
      {
        name: 'OMNIFILE_ENROL_ALLOWED_CALLER_OID'
        value: idApi.properties.principalId
      }
      {
        name: 'OMNIFILE_ENROL_TENANT_ID'
        value: tenant().tenantId
      }
      {
        // X-Forwarded-For (for per-address rate limiting and the sign check) is believed only from the ingress.
        name: 'OMNIFILE_ENROL_TRUSTED_INGRESS'
        value: ingressSources
      }
      {
        // /internal/v0/sign also needs the caller in api's subnet (api's replicas run in the internal environment).
        name: 'OMNIFILE_ENROL_SIGN_SOURCES'
        value: caeInternalSubnetPrefix
      }
      {
        // /internal/v0/crl (decision 50): only the worker's identity, which uploads the CRL to Roles Anywhere.
        name: 'OMNIFILE_ENROL_CRL_CALLER_OID'
        value: idWorker.properties.principalId
      }
      {
        // Returned to edges as api_url at enrolment: where every later call goes.
        name: 'OMNIFILE_API_URL'
        value: apiUrl
      }
    ], apiTcp ? [
      {
        // T5: enrol issues api's server certificate (/internal/v0/sign, purpose api_server) for this name only, and tells
        // edges at enrolment to pin the edge CA for api (api_server_ca_sha256).
        name: 'OMNIFILE_ENROL_API_SERVER_NAMES'
        value: apiFqdn
      }
    ] : [], caCertEnv)
    secrets: caCertSecrets
    volumes: caCertVolumes
    volumeMounts: caCertMounts
  }
}

// The worker's Teams and email settings (teams.md 10). Without OMNIFILE_TEAMS_APP_ID (teamsAppCatalogId) the worker
// runs no notification jobs. Its managed identity is the bot (deployTeamsBot) and holds the Graph application
// permissions (deploy/entra/register.sh).
var workerTeamsEnv = concat([
  {
    name: 'OMNIFILE_TENANT_ID'
    value: teamsTenant
  }
  {
    name: 'OMNIFILE_TEAMS_BOT_APP_ID'
    value: botAppId
  }
  {
    name: 'OMNIFILE_TEAMS_APP_ID'
    value: teamsAppCatalogId
  }
  {
    name: 'OMNIFILE_TEAMS_INTENT_KID'
    value: teamsIntentKid
  }
  {
    name: 'OMNIFILE_PORTAL_URL'
    value: webOriginUrl
  }
], teamsKeyEnabled ? [
  {
    name: 'OMNIFILE_TEAMS_INTENT_SIGNING_KEY'
    value: '/mnt/teams/intent-key.pem'
  }
] : [], empty(notifySender) ? [] : [
  {
    name: 'OMNIFILE_NOTIFY_SENDER'
    value: notifySender
  }
])
var workerSecrets = teamsKeyEnabled ? [
  {
    name: teamsIntentSecret
    keyVaultUrl: '${keyVaultUri}secrets/${teamsIntentSecret}'
    identity: idWorker.id
  }
] : []
var workerVolumes = teamsKeyEnabled ? [
  {
    name: 'teams'
    storageType: 'Secret'
    secrets: [
      {
        secretRef: teamsIntentSecret
        path: 'intent-key.pem'
      }
    ]
  }
] : []
var workerMounts = teamsKeyEnabled ? [
  {
    volumeName: 'teams'
    mountPath: '/mnt/teams'
  }
] : []

module worker 'modules/app.bicep' = {
  name: 'app-worker'
  params: {
    name: '${namePrefix}-worker'
    location: location
    tags: tags
    environmentId: caeInternal.outputs.id
    identityId: idWorker.id
    image: workerImage
    command: appCommands.worker
    registryServer: registryServer
    secrets: workerSecrets
    volumes: workerVolumes
    volumeMounts: workerMounts
    ingress: 'none'
    cpu: size.worker.cpu
    memory: size.worker.memory
    minReplicas: workerMinReplicas
    maxReplicas: workerMax
    // VERIFY: scale on PGMQ queue depth with the KEDA PostgreSQL scaler once queue names are fixed.
    scaleOnCpuPercent: 70
    env: concat(pgEnv, poolEnv.worker, extraEnv, metricsEnv, [
      {
        name: 'AZURE_CLIENT_ID'
        value: idWorker.properties.clientId
      }
      {
        name: 'OMNIFILE_PG_USER'
        value: idWorker.name
      }
      {
        // The edge CA's CRL for the Roles Anywhere trust anchors (decision 50), signed by enrol.
        name: 'OMNIFILE_ENROL_CRL_URL'
        value: deployEnrol ? '${enrolUrl}/internal/v0/crl' : ''
      }
      {
        name: 'OMNIFILE_ENROL_AUDIENCE'
        value: idEnrol.properties.clientId
      }
    ], workerTeamsEnv, awsEnv, empty(storageMoverConfig) ? [] : [
      {
        name: 'OMNIFILE_STORAGE_MOVER'
        value: storageMoverConfig
      }
    ])
  }
  dependsOn: [
    rbacTeamsIntentKey
  ]
}

// The worker defines and runs Storage Mover jobs (custom "OmniFile Mover Operator", this resource group).
// VERIFY: the Storage Mover sits in this resource group (setup guide); otherwise assign the role there.
resource workerMoverOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(storageMoverConfig)) {
  name: guid(resourceGroup().id, idWorker.id, 'omnifile-mover-operator')
  properties: {
    roleDefinitionId: roles.outputs.moverOperatorRoleId
    principalId: idWorker.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// web's Settings checks (portal-api.md 5.20) read the storage accounts, their blob service settings, the service
// identities' role assignments and the Storage Mover, with the custom read-only "OmniFile Settings Reader" on this
// resource group (modules/roles.bicep). It holds no data action, so web still reads no blob, and no listKeys.
resource webSettingsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployWeb) {
  name: guid(resourceGroup().id, idWeb.id, 'omnifile-settings-reader')
  properties: {
    roleDefinitionId: roles.outputs.settingsReaderRoleId
    principalId: idWeb.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// What web's Settings pages read (web/__main__.py): names and ids, never a secret. The live checks call out
// (OMNIFILE_SETTINGS_PROBES) with web's identity. OMNIFILE_TEAMS_APP_PACKAGE is not set: the zip is built on an
// admin workstation (deploy/teams/build.sh), and the page then shows how to build and upload it.
var webSettingsEnv = concat([
  {
    name: 'OMNIFILE_SETTINGS_PROBES'
    value: '1'
  }
  {
    name: 'OMNIFILE_AZURE_SUBSCRIPTION_ID'
    value: subscription().subscriptionId
  }
  {
    name: 'OMNIFILE_AZURE_RESOURCE_GROUP'
    value: resourceGroup().name
  }
  {
    // Connect Azure checks a privatelink.blob zone is linked to this VNet (decision 22), and Connect AWS gives its
    // range to the AWS interface endpoints' security group (docs/design/cloud-connect.md).
    name: 'OMNIFILE_SERVICE_VNET_ID'
    value: network.outputs.vnetId
  }
  {
    name: 'OMNIFILE_SERVICE_VNET_CIDR'
    value: vnetAddressPrefix
  }
  {
    // Who must hold a data role on each storage account (the checks' rbac_role: worker, api, verifier), and web, which
    // Connect Azure grants its read-only Discovery Reader (docs/design/cloud-connect.md 1.4; web has no data role).
    name: 'OMNIFILE_SERVICE_PRINCIPAL_IDS'
    value: 'worker=${idWorker.properties.principalId},api=${idApi.properties.principalId},verifier=${idVerifier.properties.principalId},web=${idWeb.properties.principalId}'
  }
  {
    // The identities the AWS role's trust policy names (decision 43): the two that call AWS (awsEnv: api, worker).
    name: 'OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS'
    value: '${idWorker.properties.principalId},${idApi.properties.principalId}'
  }
  {
    name: 'OMNIFILE_TENANT_ID'
    value: teamsTenant
  }
  {
    name: 'OMNIFILE_TEAMS_BOT_APP_ID'
    value: botAppId
  }
  {
    name: 'OMNIFILE_TEAMS_APP_ID'
    value: teamsAppCatalogId
  }
], empty(notifySender) ? [] : [
  {
    name: 'OMNIFILE_NOTIFY_SENDER'
    value: notifySender
  }
], awsEnv, empty(storageMoverConfig) ? [] : [
  {
    name: 'OMNIFILE_STORAGE_MOVER'
    value: storageMoverConfig
  }
], empty(awsConnectTemplateUrl) ? [] : [
  {
    name: 'OMNIFILE_AWS_CONNECT_TEMPLATE_URL'
    value: awsConnectTemplateUrl
  }
])

// web: portal backend and API for callers that cannot present a client certificate (browsers, registered API clients).
// Its own identity, with NO storage data role and NO Key Vault role beyond its cursor key: it reads and writes the
// catalog only. Anything that needs blobs (a recall, a restore) goes through the catalog's queue to the worker.
module web 'modules/app.bicep' = if (deployWeb) {
  name: 'app-web'
  params: {
    name: '${namePrefix}-web'
    location: location
    tags: tags
    environmentId: caeInternal.outputs.id
    identityId: idWeb.id
    image: webImage
    command: appCommands.web
    registryServer: registryServer
    ingress: 'external'
    targetPort: webTargetPort
    clientCertificateMode: 'ignore'
    // Nothing inside the environment calls web, so the internal subnet isn't added.
    ingressAllowedCidrs: callerCidrsChecked ? union(webAllowedCidrs, adminAllowedCidrs) : []
    healthProbePath: healthProbePath
    cpu: size.web.cpu
    memory: size.web.memory
    minReplicas: size.web.min
    maxReplicas: webMax
    env: concat(pgEnv, poolEnv.web, extraEnv, metricsEnv, [
      {
        name: 'AZURE_CLIENT_ID'
        value: idWeb.properties.clientId
      }
      {
        name: 'OMNIFILE_PG_USER'
        value: idWeb.name
      }
      {
        // X-Forwarded-For is believed only from the ingress: the sign-in rate limit is per caller, and audit rows
        // record the caller's address, not the ingress's (M7 review).
        name: 'OMNIFILE_WEB_TRUSTED_INGRESS'
        value: ingressSources
      }
      // Enrolment codes for new edges (portal-api.md 5.15): where edges enrol, and the CA they pin.
      {
        name: 'OMNIFILE_ENROL_PUBLIC_URL'
        value: enrolUrl
      }
      {
        name: 'OMNIFILE_ENROL_SERVER_CA_SHA256'
        value: enrolServerCaSha256
      }
      // Entra sign-in (portal-api.md 2.1): web signs in with a client assertion from its managed identity, through
      // the federated credential deploy/entra/register.sh puts on the app registration. No secret.
      {
        name: 'OMNIFILE_WEB_ORIGIN'
        value: webOriginUrl
      }
      {
        name: 'OMNIFILE_WEB_TENANT_ID'
        value: teamsTenant
      }
      {
        name: 'OMNIFILE_WEB_CLIENT_ID'
        value: webClientId
      }
      {
        name: 'OMNIFILE_WEB_API_AUDIENCES'
        value: join(webApiAudiences, ',')
      }
    ], webSettingsEnv, appKeysEnabled ? [
      {
        name: 'OMNIFILE_WEB_CURSOR_KEY'
        secretRef: webCursorKeySecret
      }
    ] : [])
    secrets: appKeysEnabled ? [
      {
        name: webCursorKeySecret
        keyVaultUrl: '${keyVaultUri}secrets/${webCursorKeySecret}'
        identity: idWeb.id
      }
    ] : []
  }
  dependsOn: [
    rbacWebCursorKey
  ]
}

var verifierRules = verifierTrigger == 'Event' ? [
  {
    name: 'pgmq-verify-depth'
    type: 'postgresql'
    identity: idVerifier.id
    // VERIFY: queue name (pgmq.q_<name>) and the scaler's Entra auth parameters.
    metadata: {
      host: postgres.outputs.fqdn
      port: '5432'
      userName: idVerifier.name
      dbName: pgDatabase
      sslmode: 'require'
      query: 'SELECT count(*) FROM pgmq.q_verify'
      targetQueryValue: '1'
      activationTargetQueryValue: '0'
    }
  }
] : []

module verifier 'modules/job.bicep' = {
  name: 'job-verifier'
  params: {
    name: '${namePrefix}-verifier'
    location: location
    tags: tags
    environmentId: caeInternal.outputs.id
    workloadProfileName: caeInternal.outputs.dedicatedProfileName
    identityId: idVerifier.id
    image: verifierImage
    command: appCommands.verifier
    registryServer: registryServer
    triggerType: verifierTrigger
    cronExpression: verifierCron
    // One replica per execution, and a pass holds an advisory lock (worker/verifier.py), so a
    // scheduled execution never overlaps one still reading a big file (review of #34).
    parallelism: 1
    replicaCompletionCount: 1
    cpu: size.verifier.cpu
    memory: size.verifier.memory
    eventMaxExecutions: size.verifier.eventMax
    eventScaleRules: verifierRules
    env: concat(pgEnv, extraEnv, [
      {
        name: 'AZURE_CLIENT_ID'
        value: idVerifier.properties.clientId
      }
      {
        name: 'OMNIFILE_PG_USER'
        value: idVerifier.name
      }
    ])
  }
}

// The worker starts verifier executions (manual trigger) and reads their status.
module rbacVerifierJob 'modules/rbac-scope.bicep' = {
  name: 'rbac-verifier-job'
  params: {
    kind: 'job'
    resourceName: '${namePrefix}-verifier'
    assignments: [
      union(workerPrincipal, { roleDefinitionId: roles.outputs.jobStarterRoleId })
    ]
  }
  dependsOn: [
    verifier
  ]
}

// ================================================================ container apps: external environment (Teams relay only)
//
// Where the relay's public ingress lives: a SECOND, small Container Apps environment, created external, in its own subnet of
// the same VNet. One environment cannot be both: the accessibility level is set per environment at creation and cannot be
// changed, and "internal" environments have no public endpoint at all. The relay reaches the api over the VNet, through the
// internal environment's private DNS zone. App Service with VNet integration would also work, but it adds a second hosting
// model, a second deployment pipeline and its own plan (always-on cost) for one small app.

module caeRelay 'modules/cae.bicep' = {
  name: 'cae-relay'
  params: {
    name: 'cae-${namePrefix}-relay'
    location: location
    tags: tags
    subnetId: network.outputs.caeRelaySubnetId
    internal: false
    workspaceName: '${namePrefix}-law-${uniq}'
    zoneRedundant: false
  }
  dependsOn: [
    monitoring
  ]
}

// The relay's settings are the OMNIFILE_RELAY_* variables in relay/README.md. Its mutual-TLS material comes from three
// named Key Vault secrets, referenced one by one (no vault-wide access), in the second phase only.
var relayMtlsEnabled = deployKeyVault && relaySecretsProvisioned
var relaySecrets = relayMtlsEnabled ? map(relaySecretNames, n => {
  name: n
  keyVaultUrl: '${keyVaultUri}secrets/${n}'
  identity: idRelay.id
}) : []
var relayVolumes = relayMtlsEnabled ? [
  {
    name: 'mtls'
    storageType: 'Secret'
    secrets: [
      {
        secretRef: 'relay-mtls-cert'
        path: 'client.crt'
      }
      {
        secretRef: 'relay-mtls-key'
        path: 'client.key'
      }
      {
        secretRef: 'relay-ca-bundle'
        path: 'ca.pem'
      }
    ]
  }
] : []
var relayVolumeMounts = relayMtlsEnabled ? [
  {
    volumeName: 'mtls'
    mountPath: '/mnt/mtls'
  }
] : []
var relayMtlsEnv = relayMtlsEnabled ? [
  {
    name: 'OMNIFILE_RELAY_SERVICE_CLIENT_CERT'
    value: '/mnt/mtls/client.crt'
  }
  {
    name: 'OMNIFILE_RELAY_SERVICE_CLIENT_KEY'
    value: '/mnt/mtls/client.key'
  }
  {
    name: 'OMNIFILE_RELAY_SERVICE_CA_BUNDLE'
    value: '/mnt/mtls/ca.pem'
  }
] : []

// VERIFY: that Container Apps resolves Key Vault secret references through a private endpoint when the vault's public
// access is disabled and its firewall bypass is None (modules/keyvault.bicep). If not, set the vault's bypass to
// AzureServices or put the three values in the app with `az containerapp secret set` instead.
module relay 'modules/app.bicep' = {
  name: 'app-relay'
  params: {
    name: '${namePrefix}-relay'
    location: location
    tags: tags
    environmentId: caeRelay.outputs.id
    identityId: idRelay.id
    image: relayImage
    command: appCommands.relay
    registryServer: registryServer
    ingress: 'external'
    targetPort: relayTargetPort
    // Bot Framework authenticates itself with a JWT, not a client certificate.
    clientCertificateMode: 'ignore'
    ingressAllowedCidrs: relayAllowedCidrs
    healthProbePath: relayHealthProbePath
    cpu: size.relay.cpu
    memory: size.relay.memory
    // Always warm at every size: Teams expects a quick answer to a button click.
    minReplicas: size.relay.min
    maxReplicas: size.relay.max
    secrets: relaySecrets
    volumes: relayVolumes
    volumeMounts: relayVolumeMounts
    env: concat([
      {
        name: 'OMNIFILE_RELAY_BOT_APP_ID'
        value: botAppId
      }
      {
        name: 'OMNIFILE_RELAY_TENANT_ID'
        value: teamsTenant
      }
      {
        name: 'OMNIFILE_RELAY_INTENT_PUBLIC_KEYS'
        value: relayIntentPublicKeys
      }
      {
        // With TCP ingress, api's port, and api's server certificate is from the edge CA: relay-ca-bundle is then
        // the edge CA certificate (README step 8).
        name: 'OMNIFILE_RELAY_SERVICE_BASE_URL'
        value: api.outputs.port == 443 ? 'https://${api.outputs.fqdn}' : 'https://${api.outputs.fqdn}:${api.outputs.port}'
      }
      {
        // Without this, uvicorn would see only the ingress proxy's address and the per-IP rate limit would count every
        // caller as one. Never '*'.
        name: 'OMNIFILE_RELAY_FORWARDED_ALLOW_IPS'
        value: empty(relayForwardedAllowIps) ? caeRelaySubnetPrefix : relayForwardedAllowIps
      }
      {
        name: 'PORT'
        value: string(relayTargetPort)
      }
    ], relayMtlsEnv)
  }
  dependsOn: [
    caeInternalDns
  ]
}

// ================================================================ the Teams bot (teams.md 6.1)
//
// A single-tenant Azure Bot whose identity is the worker's user-assigned managed identity: the worker gets Bot
// Connector tokens with it (OMNIFILE_TEAMS_BOT_CREDENTIAL defaults to managed_identity), and the relay checks clicks
// against its client id. Teams calls the relay's /api/messages.

resource teamsBot 'Microsoft.BotService/botServices@2022-09-15' = if (deployTeamsBot) {
  name: '${namePrefix}-bot-${uniq}'
  location: 'global'
  tags: tags
  kind: 'azurebot'
  sku: {
    name: teamsBotSku
  }
  properties: {
    displayName: 'OmniFile'
    endpoint: 'https://${relay.outputs.fqdn}/api/messages'
    msaAppId: idWorker.properties.clientId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: idWorker.id
    msaAppTenantId: tenant().tenantId
    // Bot Connector's own endpoint is public; only the messaging endpoint above is ours.
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = if (deployTeamsBot) {
  parent: teamsBot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

// ================================================================ outputs (no secrets)

var peStorageTargets = deployNonWormStorage ? [
  {
    resourceId: resourceId('Microsoft.Storage/storageAccounts', liveName)
    groupId: 'blob'
    privateDnsZone: blobZone
  }
  {
    resourceId: resourceId('Microsoft.Storage/storageAccounts', archiveName)
    groupId: 'blob'
    privateDnsZone: blobZone
  }
] : []
var peWormTargets = [for n in wormNames: {
  resourceId: resourceId('Microsoft.Storage/storageAccounts', n)
  groupId: 'blob'
  privateDnsZone: blobZone
}]
var peVaultTargets = deployKeyVault ? [
  {
    resourceId: resourceId('Microsoft.KeyVault/vaults', keyVaultName)
    groupId: 'vault'
    privateDnsZone: vaultZone
  }
] : []

@description('Where edges and the relay call api: with TCP ingress, its port too.')
output apiUrl string = apiUrl
output apiIngress string = apiIngress
output relayPublicUrl string = 'https://${relay.outputs.fqdn}'
output webOrigin string = webOriginUrl
output webUrl string = deployWeb ? 'https://${web.?outputs.fqdn ?? ''}' : ''
output enrolUrl string = enrolUrl
output keyVaultName string = deployKeyVault ? keyVaultName : ''
@description('The bot\'s Microsoft App ID, for deploy/teams/build.sh --bot-app-id.')
output teamsBotAppId string = botAppId
output internalEnvironmentDefaultDomain string = caeInternal.outputs.defaultDomain
output internalEnvironmentStaticIp string = caeInternal.outputs.staticIp
output postgresHost string = postgres.outputs.fqdn
output postgresDatabase string = pgDatabase
output keyVaultUri string = keyVaultUri
output relaySecretNamesToUpload array = relaySecretNames
output vnetId string = network.outputs.vnetId
output privateEndpointSubnetId string = network.outputs.privateEndpointSubnetId
output logAnalyticsWorkspaceId string = monitoring.outputs.id
output applicationInsightsId string = metricsEnabled ? appMetrics!.outputs.id : ''
output identityPrincipalIds object = {
  api: idApi.properties.principalId
  enrol: idEnrol.properties.principalId
  web: idWeb.properties.principalId
  worker: idWorker.properties.principalId
  verifier: idVerifier.properties.principalId
  relay: idRelay.properties.principalId
}
output storageAccounts object = {
  live: deployNonWormStorage ? liveName : ''
  archive: deployNonWormStorage ? archiveName : ''
  worm: toObject(range(0, length(wormShares)), i => wormShares[i].name, i => wormNames[i])
}
@description('What the customer must create. One blob private endpoint per storage account (plus one for the vault) in privateEndpointSubnetId, each with a DNS zone group for the matching zone.')
output privateEndpointTargets array = concat(peStorageTargets, peWormTargets, peVaultTargets)
output requiredPrivateDnsZones array = zoneNames
