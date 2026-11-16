// Service metrics and alerts (decision 37; docs/design/observability.md section 1).
//
// - Application Insights, workspace-based on the one Log Analytics workspace, with local (key) authentication off:
//   the apps send metrics with their managed identities (service/src/omnifile_service/metrics.py,
//   OMNIFILE_METRICS_EXPORTER=azuremonitor), which need Monitoring Metrics Publisher on it. The Container Apps managed
//   OpenTelemetry agent is not used for metrics: it can't send them to Application Insights.
// - Alert rules: log search alerts over the AppMetrics table (where workspace-based Application Insights keeps custom
//   metrics), sent to the action group the customer names.
// - Optional: an Azure Monitor Private Link Scope, so metric ingestion and log queries stay on the VNet. It changes DNS for
//   every Azure Monitor endpoint the VNet resolves (one scope per DNS), so it is off unless asked for.

param namePrefix string
param uniq string
param location string
param tags object
param workspaceId string
@description('Principal ids that publish metrics (api, web, worker).')
param publisherPrincipalIds array
@description('Action group the alerts notify. Empty: the alerts fire in the portal only.')
param actionGroupId string = ''
param alertsEnabled bool = true
@description('How often each alert rule runs. A log search alert is billed per rule by its frequency: $1.50 a month at 5 minutes, $0.50 at 15 (westus3 list price, docs/milestones/results/wp16.md). The window stays 15 minutes.')
@allowed([ 'PT5M', 'PT15M' ])
param alertEvaluationFrequency string = 'PT5M'
@description('Create an Azure Monitor Private Link Scope with a private endpoint and its DNS zones.')
param privateLink bool = false
param privateEndpointSubnetId string = ''
param vnetId string = ''

@description('Thresholds; guesses until pilot 2 and the first real migration (observability.md section 4).')
param queueStuckSeconds int = 900
param changefeedLagSeconds int = 3600
param edgeSilentSeconds int = 900
param edgeFreeRatioMin string = '0.10'
@description('How long the CRL upload to Roles Anywhere may fail before the alert (decision 50).')
param crlFailingSeconds int = 900

var roleMetricsPublisher = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')

resource ai 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appi-${uniq}'
  location: location
  tags: tags
  kind: 'other'
  properties: {
    Application_Type: 'other'
    WorkspaceResourceId: workspaceId
    DisableLocalAuth: true
    publicNetworkAccessForIngestion: privateLink ? 'Disabled' : 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource publishers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for p in publisherPrincipalIds: {
  name: guid(ai.id, p, roleMetricsPublisher)
  scope: ai
  properties: {
    principalId: p
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleMetricsPublisher
  }
}]

// ------------------------------------------------------------------ alerts

// Latest value per series in the window. Gauges arrive about once a minute from the worker's collector.
var latest = 'AppMetrics | where TimeGenerated > ago(15m) | extend p = todynamic(Properties)'
var alerts = [
  {
    name: 'queue-stuck'
    severity: 2
    description: 'A queue\'s oldest message is older than ${queueStuckSeconds} s: its handler is failing or the worker is down.'
    query: '${latest} | where Name == "omnifile.queue.oldest_age" and not(tostring(p.queue) endswith "_dlq") | summarize v = max(Max) by queue = tostring(p.queue) | where v > ${queueStuckSeconds}'
  }
  {
    name: 'dead-letters'
    severity: 2
    description: 'A dead-letter queue is not empty: a message failed for good. Read it with the runbook (deploy/azure/README.md).'
    query: '${latest} | where Name == "omnifile.queue.depth" and tostring(p.queue) endswith "_dlq" | summarize v = max(Max) by queue = tostring(p.queue) | where v > 0'
  }
  {
    name: 'changefeed-lag'
    severity: 3
    description: 'The Blob change feed is more than ${changefeedLagSeconds} s behind: tier and recall states arrive late.'
    query: '${latest} | where Name == "omnifile.changefeed.lag" | summarize v = max(Max) by container = tostring(p.container) | where v > ${changefeedLagSeconds}'
  }
  {
    name: 'migration-failed'
    severity: 2
    description: 'A migration prefix is in state failed.'
    query: '${latest} | where Name == "omnifile.migration.prefixes" and tostring(p.state) == "failed" | summarize v = max(Max) | where v > 0'
  }
  {
    name: 'staging-left'
    severity: 3
    description: 'A migration prefix cut over with staging objects it could not delete. They cost money and risk no data. The prefix\'s last error names the bucket; the alert ends a week after the cutover.'
    query: '${latest} | where Name == "omnifile.migration.staging_left" | summarize v = max(Max) | where v > 0'
  }
  {
    name: 'restore-throttled'
    severity: 3
    description: 'S3 throttled restores (or the restore budget was spent) in each of the last 10 minutes.'
    query: 'AppMetrics | where TimeGenerated > ago(10m) and Name == "omnifile.backend.throttled" | summarize n = sum(Sum) by bin(TimeGenerated, 1m) | where n > 0 | summarize minutes = count() | where minutes >= 10'
  }
  {
    name: 'tier-held-back'
    severity: 2
    description: 'A file that wants a different tier from others sharing its storage needs its own copy and can\'t get one, so it hasn\'t moved. It keeps firing until the copy can be made. Most often the worker lacks write rights on the storage account: the file\'s audit row, tier.move.refused with why own_copy_failed, says why in detail. Fix that, and each file tries again within a day by itself. Nothing has been billed.'
    query: '${latest} | where Name == "omnifile.tier.own_copy_stuck" | summarize v = max(Max) | where v > 0'
  }
  {
    name: 'tier-archive-overdue'
    severity: 2
    description: 'A file is past the end of its archive date and not in Archive, and is still waiting: an archive date is always meant to be respected. Look for a stuck tiering or verify queue, or a tier-held-back alert. It clears by itself when the files land. Counted from the 10,000 longest-due archive steps, so with more due at once the count stops there, still above zero.'
    query: '${latest} | where Name == "omnifile.tier.archive_overdue" | summarize v = max(Max) | where v > 0'
  }
  {
    name: 'share-map-stale'
    severity: 3
    description: 'The share map\'s counts are more than two hours behind: the worker\'s roll-up (the share_map sweep) is not keeping up, or not running. The map still shows its counts, with the time they are as of. Check the worker.'
    query: '${latest} | where Name == "omnifile.share_map.age" | summarize v = max(Max) | where v > 7200'
  }
  {
    name: 'tier-forced-archive'
    severity: 2
    description: 'A file went to Archive before its own date, against any pin it had: it shares its storage with a file whose archive date came, and neither could get a copy of its own. The file is offline until recalled, and whoever pinned it has been told. Its audit row, tier.move with forced, names it. Fix what stops the copies (tier-held-back has the same cause) so the next one doesn\'t happen.'
    query: 'AppMetrics | where TimeGenerated > ago(15m) and Name == "omnifile.tier.forced_archive" | extend p = todynamic(Properties) | summarize n = sum(Sum) by dataset = tostring(p.dataset_id), file = tostring(p.file_id) | where n > 0'
  }
  {
    name: 'tier-archive-late'
    severity: 2
    description: 'A file reached Archive after its archive date, which is always meant to be respected. Its audit row, tier.archive.late, says by how long. It can follow a stuck verify queue or a long outage.'
    query: 'AppMetrics | where TimeGenerated > ago(15m) and Name == "omnifile.tier.archive_late" | extend p = todynamic(Properties) | summarize n = sum(Sum) by dataset = tostring(p.dataset_id), file = tostring(p.file_id) | where n > 0'
  }
  {
    name: 'default-partition-rows'
    severity: 2
    description: 'Feed or audit rows landed in a DEFAULT partition: a time partition was missing. The worker\'s daily partitions sweep is failing or was down (runbook: deploy/azure/README.md, Feed and audit partitions).'
    query: '${latest} | where Name == "omnifile.catalog.default_partition_rows" | summarize v = max(Max) by table = tostring(p.table) | where v > 0'
  }
  {
    name: 'share-host-trusted-restricted'
    severity: 2
    description: 'A sec=sys (host-trusted) share now covers folders restricted to AD groups, and no override is recorded: sec=sys can\'t prove who reads them. In the portal, open the share: switch it to Kerberos, or record a host-trust override with a reason (threat model T7).'
    query: '${latest} | where Name == "omnifile.shares.host_trusted_restricted" | summarize v = max(Max) by share = tostring(p.share) | where v > 0'
  }
  {
    name: 'annex-filer-acls-hidden'
    severity: 2
    description: 'An annex filer\'s NAS shows no ACLs through its mount, so access there rests on mode bits alone and group-readable placements and share copies are refused. In the portal, open the annex: acknowledge it on the filer, or move the annex to NFSv4 or SMB (decision 64).'
    query: '${latest} | where Name == "omnifile.annex.filers_acls_hidden" | summarize v = max(Max) by filer = tostring(p.filer) | where v > 0'
  }
  {
    name: 'annex-copies-refused'
    severity: 2
    description: 'Azure\'s read-back refused an annexed file\'s copy three times in a row, so its edge is no longer asked to copy it again, or an ArchiveAdmin stopped copying it again (annex.md section 3.3). Nothing leaves the NAS for it. Open the annex\'s Problems in the portal, and check the storage backend; an ArchiveAdmin can acknowledge the file there.'
    query: '${latest} | where Name == "omnifile.annex.copies_refused" | summarize v = max(Max) by annex = tostring(p.annex) | where v > 0'
  }
  {
    name: 'edge-silent'
    severity: 1
    description: 'An edge has sent no heartbeat for ${edgeSilentSeconds} s.'
    query: '${latest} | where Name == "omnifile.edge.heartbeat_age" | summarize v = max(Max) by edge = tostring(p.edge) | where v > ${edgeSilentSeconds}'
  }
  {
    name: 'edge-guard-down'
    severity: 1
    description: 'An edge reports its smb-guard is not armed: SMB and local reads of stubs are refused or unguarded.'
    query: '${latest} | where Name == "omnifile.edge.guard_armed" | summarize v = min(Min) by edge = tostring(p.edge) | where v < 1'
  }
  {
    name: 'edge-recovery-used'
    severity: 1
    description: 'An edge\'s break-glass account was used and has not been sealed again (omnifile-setup recovery seal).'
    query: '${latest} | where Name == "omnifile.edge.recovery_used" | summarize v = max(Max) by edge = tostring(p.edge) | where v > 0'
  }
  {
    name: 'edge-quiet-periods-stale'
    severity: 2
    description: 'An edge has failed to read its shares\' quiet periods for 300 s: files there wait the longest period (600 s) before upload, and a change to a share\'s period doesn\'t reach it. Check the edge\'s log for "quiet periods can\'t be read" (decision 67).'
    query: '${latest} | where Name == "omnifile.edge.quiet_stale" | summarize v = max(Max) by edge = tostring(p.edge) | where v >= 300'
  }
  {
    name: 'edge-disk-full'
    severity: 2
    description: 'An edge\'s data volume has less than ${edgeFreeRatioMin} of its space free.'
    query: '${latest} | where Name == "omnifile.edge.data_free_ratio" | summarize v = min(Min) by edge = tostring(p.edge) | where v < ${edgeFreeRatioMin}'
  }
  {
    name: 'roles-anywhere-crl-failing'
    severity: 1
    description: 'The edge CA\'s CRL has not reached a Roles Anywhere trust anchor for ${crlFailingSeconds} s: a revoked edge\'s certificate still gets AWS credentials there. The worker retries; check its log, the service role\'s rolesanywhere:ImportCrl/UpdateCrl permissions and the Roles Anywhere interface endpoint (decision 50).'
    query: '${latest} | where Name == "omnifile.roles_anywhere.crl_failing" | summarize v = max(Max) by anchor = tostring(p.trust_anchor) | where v > ${crlFailingSeconds}'
  }
]

resource rules 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for a in alerts: if (alertsEnabled) {
  name: '${namePrefix}-alert-${a.name}'
  location: location
  tags: tags
  properties: {
    displayName: 'OmniFile: ${a.name}'
    description: a.description
    severity: a.severity
    enabled: true
    scopes: [
      workspaceId
    ]
    evaluationFrequency: alertEvaluationFrequency
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: a.query
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: empty(actionGroupId) ? [] : [
        actionGroupId
      ]
    }
  }
}]

// ------------------------------------------------------------------ private link (optional)

var monitorZones = [
  'privatelink.monitor.azure.com'
  'privatelink.oms.opinsights.azure.com'
  'privatelink.ods.opinsights.azure.com'
  'privatelink.agentsvc.azure-automation.net'
]

resource ampls 'Microsoft.Insights/privateLinkScopes@2021-07-01-preview' = if (privateLink) {
  name: '${namePrefix}-ampls-${uniq}'
  location: 'global'
  tags: tags
  properties: {
    accessModeSettings: {
      // Private only for ingestion; queries from the portal stay possible.
      ingestionAccessMode: 'PrivateOnly'
      queryAccessMode: 'Open'
    }
  }
}

resource scopedWorkspace 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = if (privateLink) {
  parent: ampls
  name: 'workspace'
  properties: {
    linkedResourceId: workspaceId
  }
}

resource scopedAi 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = if (privateLink) {
  parent: ampls
  name: 'appinsights'
  properties: {
    linkedResourceId: ai.id
  }
}

resource zones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for z in monitorZones: if (privateLink) {
  name: z
  location: 'global'
  tags: tags
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (z, i) in monitorZones: if (privateLink) {
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

resource pe 'Microsoft.Network/privateEndpoints@2024-05-01' = if (privateLink) {
  name: '${namePrefix}-pe-ampls-${uniq}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'ampls'
        properties: {
          privateLinkServiceId: ampls.id
          groupIds: [
            'azuremonitor'
          ]
        }
      }
    ]
  }
  dependsOn: [
    scopedWorkspace
    scopedAi
  ]
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (privateLink) {
  parent: pe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (z, i) in monitorZones: {
      name: replace(z, '.', '-')
      properties: {
        privateDnsZoneId: zones[i].id
      }
    }]
  }
}

output connectionString string = ai.properties.ConnectionString
output id string = ai.id
