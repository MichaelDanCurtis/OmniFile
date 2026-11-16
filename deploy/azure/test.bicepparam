using './main.bicep'

// A short-lived test deployment (tests/real/run.sh): small, cheap, and removable by its tags.
// Nothing account-specific is committed: every such value comes from the environment.
//
// Required environment:
//   OMNIFILE_RUN_ID              the run's id: tag omnifile-run on every resource
//   OMNIFILE_RUN_EXPIRES         when the run may be torn down (ISO 8601 UTC): tag omnifile-expires
//   OMNIFILE_TEST_PG_ADMIN_OID   the Entra group (or user) that administers PostgreSQL
//   OMNIFILE_TEST_PG_ADMIN_NAME  its display name
//   OMNIFILE_TEST_REGISTRY       the registry login server the images were pushed to
//   OMNIFILE_TEST_ENROL_CIDRS    the edge networks allowed to enrol (comma-separated)
//   OMNIFILE_TEST_WEB_CIDRS      the networks allowed to reach the portal (comma-separated)
// Optional environment (empty when unset):
//   OMNIFILE_TEST_PREFIX         the deployment's namePrefix (default oftest): a run's own, so global names
//                                (storage accounts, the vault, the bot) never collide between runs
//   OMNIFILE_TEST_PG_ADMIN_TYPE  Group (default), User or ServicePrincipal
//   OMNIFILE_TEST_REGISTRY_ID    the registry's resource id, for AcrPull
//   OMNIFILE_TEST_PG_ADMIN_CIDRS ranges allowed to reach PostgreSQL (a jump host)
//   OMNIFILE_TEST_ADMIN_CIDRS    admin workstations allowed to reach web and enrol (comma-separated)
//   OMNIFILE_TEST_UPLOADER_OID   who uploads the second-phase secrets (Key Vault Secrets Officer)
//   OMNIFILE_TEST_WEB_CLIENT_ID  the portal's app registration (deploy/entra/register.sh)
//   OMNIFILE_TEST_EDGE_CA_PEM    the edge CA certificate (README step 7)
//   OMNIFILE_TEST_RELAY_CERT_SHA256  the relay certificate's SHA-256 (issue-relay-cert)
//   OMNIFILE_TEST_TEAMS_APP_ID   the Teams app's catalog id (register.sh --teams-app-zip)
// The images are the ones deploy/images/build.sh recorded in deploy/images/images.lock.json.
// relayAllowedCidrs is read from relay-allowed-cidrs.json, as in the example
// (deploy/azure/relay-ranges.sh writes it).

var runId = readEnvironmentVariable('OMNIFILE_RUN_ID')
var registry = readEnvironmentVariable('OMNIFILE_TEST_REGISTRY')
var images = loadJsonContent('../images/images.lock.json')
var serviceImage = '${registry}/${images.images[images.apps.api].repository}@${images.images[images.apps.api].digest}'
var pgAdminCidrs = readEnvironmentVariable('OMNIFILE_TEST_PG_ADMIN_CIDRS', '')
var adminCidrs = readEnvironmentVariable('OMNIFILE_TEST_ADMIN_CIDRS', '')
var edgeCaPem = readEnvironmentVariable('OMNIFILE_TEST_EDGE_CA_PEM', '')
var relayPin = readEnvironmentVariable('OMNIFILE_TEST_RELAY_CERT_SHA256', '')

param namePrefix = readEnvironmentVariable('OMNIFILE_TEST_PREFIX', 'oftest')
param tags = {
  product: 'omnifile-gateway'
  'omnifile-run': runId
  'omnifile-expires': readEnvironmentVariable('OMNIFILE_RUN_EXPIRES')
}
// The small size (main.bicep `sizes`; prices in docs/milestones/results/wp16.md): a Burstable B1ms server with 32 GB,
// 0.25 to 0.5 vCPU apps on Consumption, one api and one worker replica, web and enrol scaling to zero, 30 days of
// logs under a 0.5 GB daily cap, alerts every 15 minutes.
param deploymentSize = 'small'

// Images, by digest.
param registryServer = registry
param registryResourceId = readEnvironmentVariable('OMNIFILE_TEST_REGISTRY_ID', '')
param apiImage = serviceImage
param enrolImage = '${registry}/${images.images[images.apps.enrol].repository}@${images.images[images.apps.enrol].digest}'
param workerImage = '${registry}/${images.images[images.apps.worker].repository}@${images.images[images.apps.worker].digest}'
param verifierImage = '${registry}/${images.images[images.apps.verifier].repository}@${images.images[images.apps.verifier].digest}'
param webImage = '${registry}/${images.images[images.apps.web].repository}@${images.images[images.apps.web].digest}'
param relayImage = '${registry}/${images.images[images.apps.relay].repository}@${images.images[images.apps.relay].digest}'
param healthProbePath = '/healthz'

// The three allow-lists (closed by default, threat model T1).
param relayAllowedCidrs = loadJsonContent('relay-allowed-cidrs.json', '$.prefixes')
param enrolAllowedCidrs = split(readEnvironmentVariable('OMNIFILE_TEST_ENROL_CIDRS'), ',')
param webAllowedCidrs = split(readEnvironmentVariable('OMNIFILE_TEST_WEB_CIDRS'), ',')
param adminAllowedCidrs = empty(adminCidrs) ? [] : split(adminCidrs, ',')

// PostgreSQL: small's B1ms (35 user connections; wp16.md has the budget). Set postgresSkuName = 'Standard_B2s'
// (414) if a run is refused connections.
param postgresEntraAdminObjectId = readEnvironmentVariable('OMNIFILE_TEST_PG_ADMIN_OID')
param postgresEntraAdminName = readEnvironmentVariable('OMNIFILE_TEST_PG_ADMIN_NAME')
param postgresEntraAdminType = readEnvironmentVariable('OMNIFILE_TEST_PG_ADMIN_TYPE', 'Group')
param postgresHighAvailability = 'Disabled'
param postgresGeoRedundantBackup = false
param postgresBackupRetentionDays = 7
param postgresAdminSourcePrefixes = empty(pgAdminCidrs) ? [] : split(pgAdminCidrs, ',')

// Storage: the non-WORM pair and one WORM share with a 1-day policy, created unlocked and never
// locked, and no CanNotDelete lock, so the run can be torn down the next day.
param liveRedundancy = 'Standard_ZRS'
param archiveRedundancy = 'Standard_LRS'
param wormShares = [
  {
    name: 'tst'
    redundancy: 'Standard_LRS'
    retentionDays: 1
  }
]
param lockWormAccounts = false
param blobSoftDeleteDays = 1
param changeFeedRetentionDays = 1

// Key Vault: the HSM key as in production, but purgeable at teardown.
param keyVaultSku = 'premium'
param keyVaultPurgeProtection = false
param keyVaultSoftDeleteDays = 7
param keyVaultUploaderObjectId = readEnvironmentVariable('OMNIFILE_TEST_UPLOADER_OID', '')
param keyVaultUploaderType = 'User'

// A self-contained network: the template makes the private endpoints and their DNS.
param createPrivateEndpoints = true
param createPrivateDnsZones = true
param caeZoneRedundant = false

// T5: the default, TCP ingress and direct TLS.
param apiIngress = 'tcp'

// Second phase, once the CA, the relay certificate and the Teams app exist.
param edgeCaCertPem = edgeCaPem
param relayCertSha256 = empty(relayPin) ? [] : [ relayPin ]
param teamsAppCatalogId = readEnvironmentVariable('OMNIFILE_TEST_TEAMS_APP_ID', '')
param webClientId = readEnvironmentVariable('OMNIFILE_TEST_WEB_CLIENT_ID', '')
param alertsEnabled = false
