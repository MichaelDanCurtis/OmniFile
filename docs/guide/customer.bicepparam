using '../../deploy/azure/main.bicep'

// The customer guide's parameters (docs/guide/README.md). Don't edit this file: every value comes
// from omnifile.env (your values) or omnifile.state.env (what the guide's steps recorded), which
// each guide step loads before it runs. docs/guide/check.sh builds this file offline.

// ---- your values (omnifile.env)
param namePrefix = readEnvironmentVariable('OMNI_PREFIX')
param location = readEnvironmentVariable('OMNI_LOCATION')
// The size (main.bicep `sizes`; prices in docs/milestones/results/wp16.md): standard, or small. It sets the
// database server, each app's CPU, memory, replicas and connection pool, the log cap and retention and the alerts'
// frequency. The test values further down are null for a client, so the size's own apply.
var sizeChoice = readEnvironmentVariable('OMNI_DEPLOYMENT_SIZE', '')
param deploymentSize = empty(sizeChoice) ? 'standard' : sizeChoice
param postgresEntraAdminName = readEnvironmentVariable('OMNI_PG_ADMIN_GROUP')
param postgresEntraAdminObjectId = readEnvironmentVariable('OMNI_PG_ADMIN_GROUP_ID')
param enrolAllowedCidrs = split(readEnvironmentVariable('OMNI_EDGE_CIDRS'), ',')
param webAllowedCidrs = split(readEnvironmentVariable('OMNI_USER_CIDRS'), ',')
param postgresAdminSourcePrefixes = split(readEnvironmentVariable('OMNI_ADMIN_CIDRS'), ',')
// Your workstation reaches the portal and enrol too (never the relay).
param adminAllowedCidrs = split(readEnvironmentVariable('OMNI_ADMIN_CIDRS'), ',')
param vnetAddressPrefix = readEnvironmentVariable('OMNI_VNET_PREFIX')
param caeInternalSubnetPrefix = readEnvironmentVariable('OMNI_SUBNET_CAE')
param caeRelaySubnetPrefix = readEnvironmentVariable('OMNI_SUBNET_RELAY')
param postgresSubnetPrefix = readEnvironmentVariable('OMNI_SUBNET_PG')
param privateEndpointSubnetPrefix = readEnvironmentVariable('OMNI_SUBNET_PE')
param alertActionGroupId = readEnvironmentVariable('OMNI_ALERT_ACTION_GROUP_ID', '')
param notifySender = readEnvironmentVariable('OMNI_NOTIFY_SENDER')

// The relay's allow-list: the AzureBotService service tag, taken by relay-ranges.sh (step 13).
param relayAllowedCidrs = loadJsonContent('../../deploy/azure/relay-allowed-cidrs.json', '$.prefixes')

// ---- the images (section 4): your registry, and the digests in the release's lock file
var registry = '${readEnvironmentVariable('OMNI_ACR')}.azurecr.io'
var lock = loadJsonContent('../../deploy/images/images.lock.json')
param registryServer = registry
param registryResourceId = readEnvironmentVariable('OMNI_ACR_ID')
param apiImage = '${registry}/${lock.images[lock.apps.api].repository}@${lock.images[lock.apps.api].digest}'
param enrolImage = '${registry}/${lock.images[lock.apps.enrol].repository}@${lock.images[lock.apps.enrol].digest}'
param webImage = '${registry}/${lock.images[lock.apps.web].repository}@${lock.images[lock.apps.web].digest}'
param workerImage = '${registry}/${lock.images[lock.apps.worker].repository}@${lock.images[lock.apps.worker].digest}'
param verifierImage = '${registry}/${lock.images[lock.apps.verifier].repository}@${lock.images[lock.apps.verifier].digest}'
param relayImage = '${registry}/${lock.images[lock.apps.relay].repository}@${lock.images[lock.apps.relay].digest}'
param healthProbePath = '/healthz'

// ---- fixed choices of this guide
// The edges reach api over TCP with its own TLS (threat model T5).
param apiIngress = 'tcp'
// Private endpoints are yours to create (decision 22): section 6.
param createPrivateEndpoints = false
param createPrivateDnsZones = false
// You, to upload the phase 2 secrets to Key Vault (section 9.2); from phase 2 on, nobody (step 43
// then removes the grant phase 1 made).
param keyVaultUploaderObjectId = phase2 ? '' : readEnvironmentVariable('OMNI_ADMIN_OBJECT_ID')
param keyVaultUploaderType = 'User'

// ---- recorded by the guide's steps (omnifile.state.env); empty until then
// Phase 2 (section 9): the edge CA, the enrolment pin, the portal's app registration, Teams.
var phase2 = readEnvironmentVariable('OMNI_PHASE', '1') == '2'
param edgeCaCertPem = readEnvironmentVariable('OMNI_EDGE_CA_PEM', '')
param enrolServerCaSha256 = readEnvironmentVariable('OMNI_ENROL_SERVER_CA_SHA256', '')
param webClientId = readEnvironmentVariable('OMNI_WEB_CLIENT_ID', '')
// Who holds the portal's roles (section 1.5, decision 89): groups or people.
var roleHoldersChoice = readEnvironmentVariable('OMNI_ROLE_HOLDERS', '')
param webRoleHolders = empty(roleHoldersChoice) ? 'groups' : roleHoldersChoice
param webApiAudiences = json(readEnvironmentVariable('OMNI_WEB_API_AUDIENCES', '[]'))
param teamsAppCatalogId = readEnvironmentVariable('OMNI_TEAMS_APP_CATALOG_ID', '')
param teamsIntentKid = readEnvironmentVariable('OMNI_INTENT_KID', '')
param relayIntentPublicKeys = readEnvironmentVariable('OMNI_INTENT_PUBLIC_KEYS', '{}')
param relayCertSha256 = empty(readEnvironmentVariable('OMNI_RELAY_CERT_SHA256', '')) ? [] : [
  readEnvironmentVariable('OMNI_RELAY_CERT_SHA256', '')
]
param appSecretsProvisioned = phase2
param relaySecretsProvisioned = phase2
param teamsSecretsProvisioned = phase2

// Migration clients only (section 17): the AWS accounts and the web-identity audience.
param awsAccounts = readEnvironmentVariable('OMNI_AWS_ACCOUNTS_JSON', '')
param awsWebIdentityAudience = readEnvironmentVariable('OMNI_AWS_AUDIENCE', '')

// ---- test runs only (omnifile.env.example, "Test runs only"): each empty value means the client default,
// which is main.bicep's own default (docs/guide/check.sh checks they agree).
var testRunId = readEnvironmentVariable('OMNI_TEST_RUN_ID', '')
param tags = empty(testRunId) ? {
  product: 'omnifile-gateway'
} : {
  product: 'omnifile-gateway'
  'omnifile-run': testRunId
  'omnifile-expires': readEnvironmentVariable('OMNI_TEST_RUN_EXPIRES', '')
  'omnifile-harness': 'tests-real'
}
var testPgTier = readEnvironmentVariable('OMNI_TEST_PG_SKU_TIER', '')
param postgresSkuTier = empty(testPgTier) ? null : testPgTier
var testPgName = readEnvironmentVariable('OMNI_TEST_PG_SKU_NAME', '')
param postgresSkuName = empty(testPgName) ? null : testPgName
var testPgGb = readEnvironmentVariable('OMNI_TEST_PG_STORAGE_GB', '')
param postgresStorageSizeGB = empty(testPgGb) ? null : int(testPgGb)
var testKvPurge = readEnvironmentVariable('OMNI_TEST_KV_PURGE_PROTECTION', '')
param keyVaultPurgeProtection = empty(testKvPurge) ? true : bool(testKvPurge)
var testKvDays = readEnvironmentVariable('OMNI_TEST_KV_SOFT_DELETE_DAYS', '')
param keyVaultSoftDeleteDays = empty(testKvDays) ? 90 : int(testKvDays)
var testApiMin = readEnvironmentVariable('OMNI_TEST_API_MIN_REPLICAS', '')
param apiMinReplicas = empty(testApiMin) ? null : int(testApiMin)
var testApiMax = readEnvironmentVariable('OMNI_TEST_API_MAX_REPLICAS', '')
param apiMaxReplicas = empty(testApiMax) ? null : int(testApiMax)
var testWorkerMax = readEnvironmentVariable('OMNI_TEST_WORKER_MAX_REPLICAS', '')
param workerMaxReplicas = empty(testWorkerMax) ? null : int(testWorkerMax)
var testWormDays = readEnvironmentVariable('OMNI_TEST_WORM_RETENTION_DAYS', '')
param wormShares = empty(testWormDays) ? [] : [
  {
    name: 'tst'
    redundancy: 'Standard_LRS'
    retentionDays: int(testWormDays)
  }
]
var testLockWorm = readEnvironmentVariable('OMNI_TEST_LOCK_WORM', '')
param lockWormAccounts = empty(testLockWorm) ? true : bool(testLockWorm)
var testBlobDays = readEnvironmentVariable('OMNI_TEST_BLOB_SOFT_DELETE_DAYS', '')
param blobSoftDeleteDays = empty(testBlobDays) ? 7 : int(testBlobDays)
var testAlerts = readEnvironmentVariable('OMNI_TEST_ALERTS', '')
param alertsEnabled = empty(testAlerts) ? true : bool(testAlerts)
