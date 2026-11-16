// Custom roles, scoped to this resource group, for the few jobs no built-in role covers without over-granting.
//
// Why the worker does not get "Storage Blob Data Owner": that role's blobs/* includes
//   .../blobs/immutableStorage/runAsSuperUser/action, .../blobs/deleteBlobVersion/action and .../blobs/permanentDelete/action,
// which are the actions that can bypass or destroy versions and retention. The worker role below lists its data actions
// one by one and excludes those. (Role and action names: https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/storage)

var wormManagerId = guid(resourceGroup().id, 'omnifile-worm-policy-manager')
var blobWorkerId = guid(resourceGroup().id, 'omnifile-blob-worker')
var retentionOfficerId = guid(resourceGroup().id, 'omnifile-worm-retention-officer')
var jobStarterId = guid(resourceGroup().id, 'omnifile-job-starter')
var moverOperatorId = guid(resourceGroup().id, 'omnifile-mover-operator')
var sasMinterId = guid(resourceGroup().id, 'omnifile-sas-minter')
var settingsReaderId = guid(resourceGroup().id, 'omnifile-settings-reader')

// Control plane. Create, extend and lock container immutability policies. It cannot delete a policy, cannot write the
// account (so it cannot turn off versioning, shared-key restrictions or network rules) and cannot read keys.
resource wormManager 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: wormManagerId
  properties: {
    roleName: 'OmniFile WORM Policy Manager (${resourceGroup().name})'
    description: 'Create, extend and lock container immutability policies on OmniFile WORM storage accounts. Cannot delete policies, change account settings or read keys (decision 31).'
    type: 'CustomRole'
    permissions: [
      {
        // VERIFY: that this list is enough for create, extend and lock in pilot 4.
        actions: [
          'Microsoft.Storage/storageAccounts/read'
          'Microsoft.Storage/storageAccounts/blobServices/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies/lock/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies/extend/action'
        ]
        notActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies/delete'
        ]
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

// Data plane, for the worker. Tier changes, server-side copies, restores, index tags, legal holds (Set Blob Legal Hold needs
// containers/write), create containers. No version delete, no permanent delete, no immutability override, no ACL
// or ownership changes, no container delete. It also requests user-delegation keys: the portal's download links are
// user-delegation SAS minted by the worker (worker/portal_jobs.py).
resource blobWorker 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: blobWorkerId
  properties: {
    roleName: 'OmniFile Blob Worker (${resourceGroup().name})'
    description: 'Blob data access for the OmniFile worker without immutability override, version delete or permanent delete.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action'
        ]
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/move/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/filter/action'
        ]
        notDataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/deleteBlobVersion/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/permanentDelete/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/immutableStorage/runAsSuperUser/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/runAsSuperUser/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/manageOwnership/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/modifyPermissions/action'
          'Microsoft.Storage/storageAccounts/blobServices/containers/delete'
        ]
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

// Setting a per-version retention policy needs ONLY runAsSuperUser on blobs/immutableStorage (Set Blob Immutability Policy:
// https://learn.microsoft.com/en-us/rest/api/storageservices/set-blob-immutability-policy#permissions). That is the same action
// that lets a caller run immutable-storage operations as a super user, so it is NOT given to the worker by default.
// A deployment may assign this role (parameter grantRetentionOfficerToWorker) once pilot 4 shows per-version extension is needed.
resource retentionOfficer 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: retentionOfficerId
  properties: {
    roleName: 'OmniFile WORM Retention Officer (${resourceGroup().name})'
    description: 'Set per-version immutability policies on WORM blobs. Holds the immutableStorage runAsSuperUser action: grant to as few identities as possible.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/immutableStorage/runAsSuperUser/action'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource jobStarter 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: jobStarterId
  properties: {
    roleName: 'OmniFile Job Starter (${resourceGroup().name})'
    description: 'Start and read executions of the verifier Container Apps job.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.App/jobs/read'
          'Microsoft.App/jobs/start/action'
          'Microsoft.App/jobs/stop/action'
          'Microsoft.App/jobs/executions/read'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

// The migration engine (docs/design/migration-engine.md): define and run Storage Mover jobs on the customer's Storage
// Mover and its endpoints. It cannot create or delete movers, projects or endpoints, nor touch the data itself.
// VERIFY: the action names, against a real subscription (real-azure-checks.md, Storage Mover).
resource moverOperator 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: moverOperatorId
  properties: {
    roleName: 'OmniFile Mover Operator (${resourceGroup().name})'
    description: 'Create, start and read Storage Mover job definitions and runs for migrations.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.StorageMover/storageMovers/read'
          'Microsoft.StorageMover/storageMovers/projects/read'
          'Microsoft.StorageMover/storageMovers/endpoints/read'
          'Microsoft.StorageMover/storageMovers/projects/jobDefinitions/read'
          'Microsoft.StorageMover/storageMovers/projects/jobDefinitions/write'
          'Microsoft.StorageMover/storageMovers/projects/jobDefinitions/startJob/action'
          'Microsoft.StorageMover/storageMovers/projects/jobDefinitions/stopJob/action'
          'Microsoft.StorageMover/storageMovers/projects/jobDefinitions/jobRuns/read'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

// Data plane, for api: it mints per-blob user-delegation SAS for edges (read `r`; write `cw` for uploads), HEADs blobs
// and reads block lists. A user-delegation SAS grants at most what its signer holds, so this role is the ceiling of
// every edge credential: no delete of any kind, no container writes, no tags, no immutability. Storage Blob Data
// Contributor (which includes blob and container delete) is not used.
resource sasMinter 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: sasMinterId
  properties: {
    roleName: 'OmniFile SAS Minter (${resourceGroup().name})'
    description: 'Blob read and write and user-delegation keys for the OmniFile api, without any delete.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action'
        ]
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

// Control plane, read-only, for web's Settings checks (portal-api.md 5.20). Built-in Reader would do, but it reads every
// resource type in the group (*/read); this lists the reads the checks make and nothing else. No dataActions: web never
// reads a blob. No listKeys (an action, not a read), no write, no delete.
//   storageAccounts/read             the accounts: SKU (ZRS or not), public network access, network rules
//   storageAccounts/blobServices/read versioning and the change feed
//   roleAssignments/read, roleDefinitions/read   whether the service identities hold their data roles on each account
//   storageMovers/(agents, projects)/read        the Settings, Storage Mover page
resource settingsReader 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: settingsReaderId
  properties: {
    roleName: 'OmniFile Settings Reader (${resourceGroup().name})'
    description: 'Read-only control-plane reads for the Settings checks of the OmniFile portal: storage accounts, their blob service settings, role assignments and Storage Mover. No data access, no keys, no writes.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Storage/storageAccounts/read'
          'Microsoft.Storage/storageAccounts/blobServices/read'
          'Microsoft.Authorization/roleAssignments/read'
          'Microsoft.Authorization/roleDefinitions/read'
          'Microsoft.StorageMover/storageMovers/read'
          'Microsoft.StorageMover/storageMovers/agents/read'
          'Microsoft.StorageMover/storageMovers/projects/read'
          // Connect Azure's discovery: the service's own privatelink.blob zone and its VNet links (cloud-connect.md 1.5).
          'Microsoft.Network/privateDnsZones/read'
          'Microsoft.Network/privateDnsZones/virtualNetworkLinks/read'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

output settingsReaderRoleId string = settingsReader.id
output wormManagerRoleId string = wormManager.id
output blobWorkerRoleId string = blobWorker.id
output retentionOfficerRoleId string = retentionOfficer.id
output jobStarterRoleId string = jobStarter.id
output moverOperatorRoleId string = moverOperator.id
output sasMinterRoleId string = sasMinter.id
