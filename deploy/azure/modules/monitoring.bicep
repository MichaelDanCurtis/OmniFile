// Log Analytics workspace: the one sink for storage, Postgres, Key Vault and Container Apps logs.

param name string
param location string
param tags object
@minValue(30)
@maxValue(730)
param retentionInDays int = 90
@description('Daily ingestion cap in GB, as a decimal string (\'0.5\'). \'-1\' = no cap. Workspace-based Application Insights ingests here, so the cap covers its metrics too.')
param dailyQuotaGb string = '-1'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    // Always sent, -1 included: a cap left out would stay on the workspace (small -> standard keeps 0.5 GB).
    workspaceCapping: {
      dailyQuotaGb: json(dailyQuotaGb)
    }
  }
}

output id string = workspace.id
output name string = workspace.name
