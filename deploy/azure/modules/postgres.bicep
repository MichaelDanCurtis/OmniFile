// Azure Database for PostgreSQL Flexible Server: the catalog and, through PGMQ, the work queues (decision 27).
//  - Private access by VNet integration (delegated subnet + private DNS zone). No public endpoint.
//  - Entra authentication only. There is no password administrator.
//  - PGMQ is installed later as plain SQL by the service's migrations. It is NOT in the extension list.
//  - pg_cron is allow-listed and preloaded, for timed jobs that belong in the database.

param name string
param location string
param tags object
param delegatedSubnetId string
param vnetId string
param workspaceId string

@allowed([ '17', '18' ])
param version string = '18'
param skuName string = 'Standard_D4ds_v5'
@allowed([ 'Burstable', 'GeneralPurpose', 'MemoryOptimized' ])
param skuTier string = 'GeneralPurpose'
param storageSizeGB int = 128
@allowed([ 'Disabled', 'SameZone', 'ZoneRedundant' ])
param highAvailabilityMode string = 'Disabled'
param geoRedundantBackup bool = false
@minValue(7)
@maxValue(35)
param backupRetentionDays int = 14
param databaseName string = 'omnifile'

@description('Object id of the Entra user or group that administers the server. It creates the database roles for the service identities.')
@minLength(36)
@maxLength(36)
param entraAdminObjectId string
param entraAdminName string
@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param entraAdminType string = 'Group'

@description('Extensions the database may CREATE EXTENSION. Names are upper-case.')
param allowedExtensions array = [
  'PG_CRON'
  'PGCRYPTO'
  'PG_TRGM'
  'BTREE_GIST'
]

// The zone name must end in .postgres.database.azure.com. Using a per-server prefix keeps zones from clashing.
#disable-next-line no-hardcoded-env-urls
var dnsZoneName = '${name}.private.postgres.database.azure.com'

resource dnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: dnsZoneName
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZone
  name: 'vnet'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: version
    createMode: 'Default'
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: dnsZone.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: geoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    highAvailability: {
      mode: highAvailabilityMode
    }
  }
  dependsOn: [
    dnsLink
  ]
}

resource admin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2025-08-01' = {
  parent: server
  name: entraAdminObjectId
  properties: {
    principalType: entraAdminType
    principalName: entraAdminName
    tenantId: tenant().tenantId
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    admin
  ]
}

// Server parameters. Flexible Server accepts one configuration write at a time, so each one waits for the previous.
resource cfgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: join(allowedExtensions, ',')
    source: 'user-override'
  }
  dependsOn: [
    database
  ]
}

resource cfgPreload 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: server
  name: 'shared_preload_libraries'
  properties: {
    value: 'pg_cron,pg_stat_statements'
    source: 'user-override'
  }
  dependsOn: [
    cfgExtensions
  ]
}

resource cfgCronDb 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: server
  name: 'cron.database_name'
  properties: {
    value: databaseName
    source: 'user-override'
  }
  dependsOn: [
    cfgPreload
  ]
}

resource cfgTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: server
  name: 'ssl_min_protocol_version'
  properties: {
    value: 'TLSv1.2'
    source: 'user-override'
  }
  dependsOn: [
    cfgCronDb
  ]
}

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: server
  name: 'to-log-analytics'
  properties: {
    workspaceId: workspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
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
  dependsOn: [
    cfgTls
  ]
}

output id string = server.id
output name string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
output databaseName string = database.name
output privateDnsZoneName string = dnsZone.name
