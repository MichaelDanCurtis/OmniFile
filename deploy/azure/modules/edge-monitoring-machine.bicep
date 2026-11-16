// One Arc-connected edge: the Azure Monitor Agent extension and its association with the edge data collection rule.
// Deployed to the edge machine's own resource group (main.bicep derives it from the machine's resource id).

param machineName string
param location string
param dcrId string

resource machine 'Microsoft.HybridCompute/machines@2024-07-10' existing = {
  name: machineName
}

resource ama 'Microsoft.HybridCompute/machines/extensions@2024-07-10' = {
  parent: machine
  name: 'AzureMonitorLinuxAgent'
  location: location
  properties: {
    publisher: 'Microsoft.Azure.Monitor'
    type: 'AzureMonitorLinuxAgent'
    autoUpgradeMinorVersion: true
    enableAutomaticUpgrade: true
  }
}

resource association 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: 'omnifile-edge'
  scope: machine
  properties: {
    dataCollectionRuleId: dcrId
    description: 'OmniFile edge journal to Log Analytics.'
  }
  dependsOn: [
    ama
  ]
}
