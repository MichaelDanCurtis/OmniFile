// Private DNS zones for the storage and Key Vault private endpoints, linked to the VNet. Optional: the customer may
// already have these zones in a hub subscription, in which case they skip this and link their own (decision 22).

param tags object
param vnetId string
param zoneNames array

resource zones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for z in zoneNames: {
  name: z
  location: 'global'
  tags: tags
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (z, i) in zoneNames: {
  parent: zones[i]
  name: 'vnet'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}]

output zoneIds array = [for (z, i) in zoneNames: zones[i].id]
