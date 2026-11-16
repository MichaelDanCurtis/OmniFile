// Data-plane (and WORM policy) role assignments on ONE storage account (decision 28 section 7).
// Only the OmniFile service identities and the break-glass admin group get any. Nobody else, ever.

param storageAccountName string

@description('Array of { principalId, principalType, roleDefinitionId }.')
param assignments array

resource account 'Microsoft.Storage/storageAccounts@2025-08-01' existing = {
  name: storageAccountName
}

resource roleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in assignments: {
  scope: account
  name: guid(account.id, a.principalId, a.roleDefinitionId)
  properties: {
    principalId: a.principalId
    principalType: a.principalType
    roleDefinitionId: a.roleDefinitionId
  }
}]
