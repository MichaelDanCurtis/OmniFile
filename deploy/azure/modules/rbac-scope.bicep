// Role assignments on one Key Vault, key or secret, Container Apps job or registry, selected by `kind`.
// Keys and secrets are assigned individually so an identity sees only the object it needs.

@allowed([ 'keyvault', 'key', 'secret', 'job', 'registry' ])
param kind string
@description('Vault, job or registry name. For key and secret: the vault name.')
param resourceName string
@description('For key and secret: the key or secret name.')
param childName string = ''

@description('Array of { principalId, principalType, roleDefinitionId }.')
param assignments array

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = if (kind == 'keyvault') {
  name: resourceName
}

resource vaultParent 'Microsoft.KeyVault/vaults@2024-11-01' existing = if (kind == 'key' || kind == 'secret') {
  name: resourceName
}

resource key 'Microsoft.KeyVault/vaults/keys@2024-11-01' existing = if (kind == 'key') {
  parent: vaultParent
  name: childName
}

resource secret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = if (kind == 'secret') {
  parent: vaultParent
  name: childName
}

resource job 'Microsoft.App/jobs@2025-07-01' existing = if (kind == 'job') {
  name: resourceName
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = if (kind == 'registry') {
  name: resourceName
}

resource onVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: if (kind == 'keyvault') {
  scope: vault
  name: guid(vault.id, a.principalId, a.roleDefinitionId)
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: a.roleDefinitionId
  }
}]

resource onKey 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: if (kind == 'key') {
  scope: key
  name: guid(key.id, a.principalId, a.roleDefinitionId)
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: a.roleDefinitionId
  }
}]

resource onSecret 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: if (kind == 'secret') {
  scope: secret
  name: guid(secret.id, a.principalId, a.roleDefinitionId)
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: a.roleDefinitionId
  }
}]

resource onJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: if (kind == 'job') {
  scope: job
  name: guid(job.id, a.principalId, a.roleDefinitionId)
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: a.roleDefinitionId
  }
}]

resource onRegistry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: if (kind == 'registry') {
  scope: registry
  name: guid(registry.id, a.principalId, a.roleDefinitionId)
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: a.roleDefinitionId
  }
}]
