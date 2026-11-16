using './main.bicep'

// The standard deployment: the production sizing (main.bicep's default). Copy, then edit the placeholders.
// main.example.bicepparam is the same size with the optional production settings (high availability, geo-redundant
// backups, zone-redundant environment) and a WORM share. Costs, resource by resource: docs/milestones/results/wp16.md.
//   deploy/azure/relay-ranges.sh --location <region> --out deploy/azure/relay-allowed-cidrs.json   (the relay's ranges)
//   deploy/azure/whatif.sh -g <rg> -p deploy/azure/standard.bicepparam                             (the preview first)

param deploymentSize = 'standard'
param namePrefix = 'omni'

// Entra group that administers PostgreSQL and creates the database roles for the service identities.
param postgresEntraAdminObjectId = '00000000-0000-0000-0000-000000000000'
param postgresEntraAdminName = 'OmniFile-DB-Admins'

// Images, by digest: the digests are in deploy/images/images.lock.json (deploy/images/build.sh).
param registryServer = 'contoso.azurecr.io'
param registryResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-shared/providers/Microsoft.ContainerRegistry/registries/contoso'
param apiImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param workerImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param verifierImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param enrolImage = 'contoso.azurecr.io/omnifile-service@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param webImage = 'contoso.azurecr.io/omnifile-web@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param relayImage = 'contoso.azurecr.io/omnifile-relay@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param healthProbePath = '/healthz'

// T5: api terminates mutual TLS itself behind a TCP ingress.
param apiIngress = 'tcp'

// The three allow-lists are required: the deployment is closed until you set them (threat model T1).
param relayAllowedCidrs = loadJsonContent('relay-allowed-cidrs.json', '$.prefixes')
param enrolAllowedCidrs = [
  '10.20.0.0/16'
]
param webAllowedCidrs = [
  '10.30.0.0/16'
]

// Storage: the non-WORM pair.
param deployNonWormStorage = true
param liveRedundancy = 'Standard_ZRS'
param archiveRedundancy = 'Standard_LRS'

// Private endpoints are the customer's to create (decision 22).
param createPrivateEndpoints = false
param createPrivateDnsZones = false
