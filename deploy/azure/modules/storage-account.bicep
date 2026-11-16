// One storage account, in one of three layouts (decisions 5, 22, 29-31):
//
//   live     non-WORM, ZRS (or GZRS/RA-GZRS), default tier Smart. Uploads must NOT set an explicit tier, or the blob never
//            enters Smart. Smart has no Archive, and Archive is forbidden on ZRS, hence the second account:
//   archive  non-WORM, LRS/GRS. The service copies a file here server-side on its archive date with x-ms-access-tier=Archive,
//            then deletes the original from the live account.
//   worm     one account per WORM share, LRS/GRS, versioning + version-level immutability. Tiers in place with Set Blob Tier.
//            Scheduled tiers only: Smart is not offered until pilot 4 verifies it with immutability.
//
// All layouts: shared-key access off (Entra only), public network access off by default, TLS 1.2 minimum, no anonymous
// access, blob change feed on (it is the catalog-sync and recall-status stream; Event Grid cannot reach private endpoints).
// Blob Inventory exists only on non-WORM accounts (version-level immutability disables it).
// Point-in-time restore and last-access tracking are never enabled: both are incompatible with immutability.

param name string
param location string
param tags object
param workspaceId string

@allowed([ 'live', 'archive', 'worm' ])
param layout string

@allowed([ 'Standard_LRS', 'Standard_GRS', 'Standard_ZRS', 'Standard_GZRS', 'Standard_RAGZRS' ])
param skuName string

@description('Blob versioning: the storage for decision 30 (rewind). WORM requires it. VERIFY in pilot 4 that Smart tier works on an account with versioning (and, for WORM, immutability): the docs do not say.')
param versioningEnabled bool = true
@minValue(1)
@maxValue(365)
param changeFeedRetentionDays int = 14
@minValue(1)
@maxValue(365)
param softDeleteDays int = 7

@allowed([ 'Enabled', 'Disabled' ])
param publicNetworkAccess string = 'Disabled'

@description('Containers to create. Per-share containers are normally created by the service at run time.')
param containers array = []

@description('WORM only. Default retention in days for new versions in policyContainers. 0 = no default policy (the service sets it). The policy is created UNLOCKED; the service locks it after the validation window (decision 31). After it is locked, remove this setting from the parameters: do not re-deploy a locked policy.')
@minValue(0)
param wormRetentionDays int = 0
@description('WORM only. Containers that get the default policy. Never the recall container: recall copies must be deletable (decision 31).')
param policyContainers array = []
@description('CanNotDelete resource lock on the account. Used on WORM accounts, which can never be recreated as non-WORM and hold the compliance record.')
param lockAccount bool = false

@description('Daily Blob Inventory for reconcile. Ignored on WORM accounts, where the platform does not allow it.')
param inventoryEnabled bool = true

@description('Delete non-current versions older than this many days (the ArchiveAdmin maximum rewind depth). 0 = no rule. Ignored on WORM accounts, where retention governs deletion.')
@minValue(0)
param versionMaxAgeDays int = 0

@description('Days a bulk snapshot export (control API section 6) is kept in the omnifile-exports container before the lifecycle rule deletes it. The service deletes its own after 2 days; this is the backstop.')
@minValue(1)
param exportMaxAgeDays int = 3

var isWorm = layout == 'worm'
var isLive = layout == 'live'
var withInventory = inventoryEnabled && !isWorm
var withVersionRule = versionMaxAgeDays > 0 && !isWorm
// Bulk snapshot exports go to the account a dataset's files are in (its primary account): live or WORM, never archive.
// The container carries no immutability policy. VERIFY in pilot 4 that lifecycle deletion runs in it on a WORM account.
var withExports = isLive || isWorm
var exportsContainer = 'omnifile-exports'
var versionRule = {
  name: 'prune-old-versions'
  enabled: true
  type: 'Lifecycle'
  definition: {
    filters: {
      blobTypes: [
        'blockBlob'
      ]
    }
    actions: {
      version: {
        delete: {
          daysAfterCreationGreaterThan: versionMaxAgeDays
        }
      }
    }
  }
}
var exportRule = {
  name: 'expire-snapshot-exports'
  enabled: true
  type: 'Lifecycle'
  definition: {
    filters: {
      blobTypes: [
        'blockBlob'
      ]
      prefixMatch: [
        '${exportsContainer}/'
      ]
    }
    actions: {
      baseBlob: {
        delete: {
          daysAfterCreationGreaterThan: exportMaxAgeDays
        }
      }
      version: {
        delete: {
          daysAfterCreationGreaterThan: exportMaxAgeDays
        }
      }
    }
  }
}
var lifecycleRules = concat(withVersionRule ? [ versionRule ] : [], withExports ? [ exportRule ] : [])

resource account 'Microsoft.Storage/storageAccounts@2025-08-01' = {
  name: name
  location: location
  tags: union(tags, {
    'omnifile-layout': layout
  })
  kind: 'StorageV2'
  sku: {
    name: skuName
  }
  properties: {
    // Smart only on the live account (it must be ZRS-based). The archive account's default only applies to blobs that
    // arrive without an explicit tier; the service always sets Archive explicitly on copy.
    accessTier: isLive ? 'Smart' : (layout == 'archive' ? 'Cool' : 'Hot')
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    // Private mode (public network access off, decision 22): copies are allowed only between accounts reached over private
    // links, so a server-side copy can never leave the private path. The live -> archive copy then needs both accounts to
    // have private endpoints in the same VNet. VERIFY this in pilot 4 before the first archive-date copy. With public access
    // on (development only), fall back to 'AAD' (same Entra tenant).
    allowedCopyScope: publicNetworkAccess == 'Disabled' ? 'PrivateLink' : 'AAD'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    isHnsEnabled: false
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
    // Version-level WORM. This can only be switched on at account creation and never switched off, so WORM accounts
    // must never be deleted and recreated casually. No default policy is set: the service creates the container
    // policy unlocked, then locks it after the validation window (decision 31).
    immutableStorageWithVersioning: isWorm ? {
      enabled: true
    } : null
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-08-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: versioningEnabled || isWorm
    changeFeed: {
      enabled: true
      retentionInDays: changeFeedRetentionDays
    }
    deleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
  }
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-08-01' = [for c in union(containers, withExports ? [ exportsContainer ] : []): {
  parent: blobService
  name: c
  properties: {
    publicAccess: 'None'
  }
}]

// Default version-level policy on the data containers of a WORM account, created unlocked. VERIFY the resource shape for
// version-level immutability containers in pilot 4.
resource policyTargets 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-08-01' existing = [for c in policyContainers: {
  parent: blobService
  name: c
}]

resource defaultPolicies 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2025-08-01' = [for (c, i) in policyContainers: if (isWorm && wormRetentionDays > 0) {
  parent: policyTargets[i]
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: wormRetentionDays
    allowProtectedAppendWrites: false
  }
  dependsOn: [
    blobContainers
  ]
}]

resource accountLock 'Microsoft.Authorization/locks@2020-05-01' = if (lockAccount) {
  scope: account
  name: 'omnifile-do-not-delete'
  properties: {
    level: 'CanNotDelete'
    notes: 'OmniFile WORM account (decision 31). Version-level immutability cannot be removed once enabled, and the account holds the compliance record.'
  }
}

resource inventoryContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-08-01' = if (withInventory) {
  parent: blobService
  name: 'inventory'
  properties: {
    publicAccess: 'None'
  }
}

resource inventory 'Microsoft.Storage/storageAccounts/inventoryPolicies@2025-08-01' = if (withInventory) {
  parent: account
  name: 'default'
  properties: {
    policy: {
      enabled: true
      type: 'Inventory'
      rules: [
        {
          name: 'daily-reconcile'
          enabled: true
          destination: 'inventory'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              includeBlobVersions: true
              includeSnapshots: false
            }
            format: 'Parquet'
            objectType: 'Blob'
            schedule: 'Daily'
            // VERIFY: field names against the Blob Inventory schema when the reconcile job is written.
            schemaFields: [
              'Name'
              'Creation-Time'
              'Last-Modified'
              'Content-Length'
              'AccessTier'
              'AccessTierChangeTime'
              'BlobType'
              'VersionId'
              'IsCurrentVersion'
              'Tags'
            ]
          }
        }
      ]
    }
  }
  dependsOn: [
    inventoryContainer
  ]
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2025-08-01' = if (!empty(lifecycleRules)) {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: lifecycleRules
    }
  }
  dependsOn: [
    blobService
  ]
}

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: blobService
  name: 'to-log-analytics'
  properties: {
    workspaceId: workspaceId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

output id string = account.id
output name string = account.name
output blobEndpoint string = account.properties.primaryEndpoints.blob
output layout string = layout
