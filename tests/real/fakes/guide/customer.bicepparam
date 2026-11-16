// Fixture: the customer's parameters file, as docs/guide/customer.bicepparam reads omnifile.env.
// The OMNI_TEST_* values are empty for a customer (main.bicep's defaults) and set by the
// real-cloud harness for a test run. WP1's whatif.sh compiles it with the Bicep CLI against
// main.bicep; the fake az then answers the what-if.
using '../../deploy/azure/main.bicep'

var runId = readEnvironmentVariable('OMNI_TEST_RUN_ID', '')

param namePrefix = readEnvironmentVariable('OMNI_PREFIX')
param tags = empty(runId) ? { product: 'omnifile-gateway' } : {
  product: 'omnifile-gateway'
  'omnifile-run': runId
  'omnifile-expires': readEnvironmentVariable('OMNI_TEST_RUN_EXPIRES')
  'omnifile-harness': 'tests-real'
}
// The size, as docs/guide/customer.bicepparam reads it (#59): a test run deploys small.
var sizeChoice = readEnvironmentVariable('OMNI_DEPLOYMENT_SIZE', '')
param deploymentSize = empty(sizeChoice) ? 'standard' : sizeChoice
param keyVaultPurgeProtection = bool(readEnvironmentVariable('OMNI_TEST_KV_PURGE_PROTECTION', 'true'))
param lockWormAccounts = bool(readEnvironmentVariable('OMNI_TEST_LOCK_WORM', 'true'))
param enrolAllowedCidrs = split(readEnvironmentVariable('OMNI_EDGE_CIDRS'), ',')
param webAllowedCidrs = split(readEnvironmentVariable('OMNI_USER_CIDRS'), ',')

// The relay's allow-list, as the guide's step 13 writes it (deploy/azure/relay-ranges.sh).
param relayAllowedCidrs = loadJsonContent('../../deploy/azure/relay-allowed-cidrs.json', '$.prefixes')

// The images: the run's registry and the digests in the release's lock file.
var registry = '${readEnvironmentVariable('OMNI_ACR')}.azurecr.io'
var lock = loadJsonContent('../../deploy/images/images.lock.json')
param apiImage = '${registry}/${lock.images[lock.apps.api].repository}@${lock.images[lock.apps.api].digest}'
param enrolImage = '${registry}/${lock.images[lock.apps.enrol].repository}@${lock.images[lock.apps.enrol].digest}'
param webImage = '${registry}/${lock.images[lock.apps.web].repository}@${lock.images[lock.apps.web].digest}'
param workerImage = '${registry}/${lock.images[lock.apps.worker].repository}@${lock.images[lock.apps.worker].digest}'
param verifierImage = '${registry}/${lock.images[lock.apps.verifier].repository}@${lock.images[lock.apps.verifier].digest}'
param relayImage = '${registry}/${lock.images[lock.apps.relay].repository}@${lock.images[lock.apps.relay].digest}'

// The PostgreSQL admin group (the harness makes it at plan).
param postgresEntraAdminName = readEnvironmentVariable('OMNI_PG_ADMIN_GROUP')
param postgresEntraAdminObjectId = readEnvironmentVariable('OMNIFILE_TEST_PG_ADMIN_OID')
