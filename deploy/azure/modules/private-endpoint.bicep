// One private endpoint (with its DNS zone group) for one resource. Used only when createPrivateEndpoints = true.
// In the normal case the customer creates these (decision 22).

param name string
param location string
param tags object
param subnetId string
param targetResourceId string
@description('Private-link sub-resource: blob for storage, vault for Key Vault.')
param groupId string
param privateDnsZoneId string

resource pe 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: name
        properties: {
          privateLinkServiceId: targetResourceId
          groupIds: [
            groupId
          ]
        }
      }
    ]
  }
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: pe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'zone'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}
