// Key Vault for the OmniFile CA that signs edge-appliance certificates (mTLS to the api app).
// Recommendation: keep the CA private key in Key Vault as a NON-EXPORTABLE key (HSM-backed on the premium SKU).
// The enrol identity gets only "Key Vault Crypto User" on that key: it can call sign, but nothing, including the service,
// can ever read the key out. The root certificate is created once at bootstrap by signing with this key.

param name string
param location string
param tags object
param workspaceId string

@allowed([ 'standard', 'premium' ])
param skuName string = 'premium'
@allowed([ 'Enabled', 'Disabled' ])
param publicNetworkAccess string = 'Disabled'
param createCaKey bool = true
param caKeyName string = 'omnifile-edge-ca'
@description('Purge protection, irreversible once on. false leaves it off (ARM refuses an explicit false), so a test vault can be purged.')
param purgeProtection bool = true
@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 90

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: skuName
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    enablePurgeProtection: purgeProtection ? true : null
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
  }
}

resource caKey 'Microsoft.KeyVault/vaults/keys@2024-11-01' = if (createCaKey) {
  parent: vault
  name: caKeyName
  properties: {
    kty: skuName == 'premium' ? 'EC-HSM' : 'EC'
    curveName: 'P-384'
    keyOps: [
      'sign'
      'verify'
    ]
    attributes: {
      enabled: true
    }
  }
}

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: vault
  name: 'to-log-analytics'
  properties: {
    workspaceId: workspaceId
    logs: [
      {
        categoryGroup: 'audit'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output id string = vault.id
output name string = vault.name
output uri string = vault.properties.vaultUri
output caKeyName string = caKeyName
