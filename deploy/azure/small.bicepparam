using './main.bicep'

// The small deployment: the cheapest that keeps every constraint, for a small site or a first test run. Copy, then
// edit the placeholders. What it costs, resource by resource, and what it gives up: docs/milestones/results/wp16.md.
//   deploy/azure/relay-ranges.sh --location westus3 --out deploy/azure/relay-allowed-cidrs.json   (the relay's ranges)
//   deploy/azure/whatif.sh -g <rg> -p deploy/azure/small.bicepparam                               (the preview first)
//
// What small changes (main.bicep, `sizes`): a Burstable B1ms PostgreSQL server with 32 GB and no high availability;
// api and the worker at 0.5 vCPU and 1 GiB, one replica each; web and enrol at 0.25 vCPU, scaling to zero; the relay
// at 0.25 vCPU, always one warm replica; Consumption only; a 0.5 GB daily log cap with 30 days' retention; alerts
// every 15 minutes. What it keeps: private endpoints for storage and the vault with no public fallback, the relay as
// the only public endpoint, one identity and one database role per app, the required allow-lists, api's direct TLS
// (threat model T5), and the HSM-backed CA key.

param deploymentSize = 'small'
// Container Apps is cheaper in West US 3 than in West US 2 (wp16.md).
param location = 'westus3'
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

// T5: api terminates mutual TLS itself behind a TCP ingress, at every size.
param apiIngress = 'tcp'

// The three allow-lists are required at every size: the deployment is closed until you set them (threat model T1).
param relayAllowedCidrs = loadJsonContent('relay-allowed-cidrs.json', '$.prefixes')
param enrolAllowedCidrs = [
  '10.20.0.0/16'
]
param webAllowedCidrs = [
  '10.30.0.0/16'
]

// Storage: the non-WORM pair. ZRS for the live account is what Smart tier needs; the archive account stays LRS.
param deployNonWormStorage = true
param liveRedundancy = 'Standard_ZRS'
param archiveRedundancy = 'Standard_LRS'

// Private endpoints are the customer's to create (decision 22), at every size: one per storage account and one for
// the vault. Set both true to have this template create them and their DNS zones instead; the cost is the same.
param createPrivateEndpoints = false
param createPrivateDnsZones = false
