using './main.bicep'

// Example parameters for a first, non-WORM deployment in one resource group. Copy, then edit.
//   deploy/azure/relay-ranges.sh --location <region> --out deploy/azure/relay-allowed-cidrs.json   (the relay's ranges)
//   az deployment group what-if|create -g <rg> -f main.bicep -p main.example.bicepparam   (do the what-if first)

param namePrefix = 'omni'
// standard (the production sizing) or small (the cheapest that keeps every constraint). standard.bicepparam and
// small.bicepparam are plain starting points for each; costs in docs/milestones/results/wp16.md.
param deploymentSize = 'standard'

// Entra group that administers PostgreSQL and creates the database roles for the service identities.
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'OmniFile-DB-Admins'

// Images, by digest: the digests are in deploy/images/images.lock.json (deploy/images/build.sh). api, enrol,
// worker and the verifier run the same service image, each with its own command (main.bicep appCommands).
param registryServer = 'contoso.azurecr.io'
param registryResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-shared/providers/Microsoft.ContainerRegistry/registries/contoso'
param apiImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param workerImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param verifierImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param enrolImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param webImage = 'contoso.azurecr.io/omnifile-web@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param relayImage = 'contoso.azurecr.io/omnifile-relay@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param healthProbePath = '/healthz'

// api terminates mutual TLS itself behind a TCP ingress (the default; threat model T5).
param apiIngress = 'tcp'

// Teams (see relay/README.md). The template creates the bot on the worker's identity. The public keys are not
// secret; the catalog id and the portal's client id come from deploy/entra/register.sh.
param relayIntentPublicKeys = '{"k1":"<base64url Ed25519 public key>"}'
param teamsIntentKid = 'k1'
param teamsAppCatalogId = ''
param webClientId = ''
// Phase 2, after the relay's client certificate is in Key Vault: set true and redeploy.
param relaySecretsProvisioned = false
// The three allow-lists are required: the deployment is closed until you set them (threat model T1).
// Ranges allowed to call the relay: the AzureBotService service tag's IPv4 prefixes, taken at deploy time (decision 49).
// Nobody types them: before every deployment run
//   deploy/azure/relay-ranges.sh --location <region> --out deploy/azure/relay-allowed-cidrs.json
// which reads `az network list-service-tags` and fails if the tag is missing or empty. VERIFY on a real tenant that every
// Teams call comes from them (real-tenant-checks.md). ['0.0.0.0/0'] instead opens the relay to the internet on purpose,
// its own checks then being the only gate.
param relayAllowedCidrs = loadJsonContent('relay-allowed-cidrs.json', '$.prefixes')
// Edge appliance networks allowed to enrol.
param enrolAllowedCidrs = [
  '10.20.0.0/16'
]
// Users' and Prizmanyx's networks allowed to reach the portal and partner API. Never a range covering the relay subnet.
param webAllowedCidrs = [
  '10.30.0.0/16'
]

// Storage: the non-WORM pair, plus one WORM share (its own LRS account).
param deployNonWormStorage = true
param liveRedundancy = 'Standard_ZRS'
param archiveRedundancy = 'Standard_GRS'
param wormShares = [
  {
    name: 'fin'
    redundancy: 'Standard_GRS'
    // Default retention, created UNLOCKED. Remove this line once the service has locked the policy.
    retentionDays: 2555
  }
]

// Break-glass: the only humans with Blob data-plane access (decision 28 section 7). Empty = nobody.
param breakGlassAdminGroupObjectId = '00000000-0000-0000-0000-000000000000'
param breakGlassBlobRole = 'Contributor'

// Optional production settings.
param postgresHighAvailability = 'ZoneRedundant'
param postgresGeoRedundantBackup = true
param caeZoneRedundant = true
param deployEnrol = true
param deployWeb = true

// Private endpoints are the customer's to create (decision 22). Leave these false and follow the README,
// or set true to have this template create the endpoints and DNS zones for you.
param createPrivateEndpoints = false
param createPrivateDnsZones = false
