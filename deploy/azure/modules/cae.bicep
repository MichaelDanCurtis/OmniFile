// A workload-profiles Container Apps environment in an existing subnet.
//
// internal = true : no public endpoint; an internal load balancer inside the VNet (api, worker, verifier job).
// internal = false: public inbound IP (the Teams relay).
//
// The accessibility level is a property of the ENVIRONMENT and cannot be changed afterwards. An app's `ingress.external`
// only chooses whether it is published at the environment's boundary. So one environment cannot hold both an
// internal-only api and a public relay: that is why main.bicep creates two environments.
// Source: https://learn.microsoft.com/en-us/azure/container-apps/networking#accessibility-level
//         https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview#how-ingress-visibility-interacts-with-the-environment-type

param name string
param location string
param tags object
param subnetId string
param internal bool
param workspaceName string
param zoneRedundant bool = false
@description('Encrypt traffic between apps inside the environment (api <-> worker). VERIFY it is generally available for workload-profile environments, and measure the throughput cost, before turning it on.')
param peerEncryption bool = false

@description('Optional dedicated workload profile type (for example D8) for the verifier job. Empty = Consumption only.')
param dedicatedProfileType string = ''
param dedicatedProfileMaxCount int = 4

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

var consumptionProfile = [
  {
    name: 'Consumption'
    workloadProfileType: 'Consumption'
  }
]
var dedicatedProfile = empty(dedicatedProfileType) ? [] : [
  {
    name: 'dedicated'
    workloadProfileType: dedicatedProfileType
    minimumCount: 0
    maximumCount: dedicatedProfileMaxCount
  }
]

resource env 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    zoneRedundant: zoneRedundant
    vnetConfiguration: {
      infrastructureSubnetId: subnetId
      internal: internal
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    workloadProfiles: concat(consumptionProfile, dedicatedProfile)
    peerTrafficConfiguration: {
      encryption: {
        enabled: peerEncryption
      }
    }
  }
}

output id string = env.id
output name string = env.name
output defaultDomain string = env.properties.defaultDomain
output staticIp string = env.properties.staticIp
output dedicatedProfileName string = empty(dedicatedProfileType) ? 'Consumption' : 'dedicated'
