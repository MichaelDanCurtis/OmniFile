// Edge logs to Log Analytics through Azure Arc (decision 37; docs/design/observability.md section 2).
//
// The daemon writes JSON lines to the journal; rsyslog (in the appliance image) forwards the journal to syslog, and the
// Azure Monitor Agent extension on the Arc-connected edge ships the `daemon` facility (systemd units log there) to the
// workspace's Syslog table under this data collection rule. Nothing listens on the appliance; the agent dials out.
// Each edge is attached with edge-monitoring-machine.bicep (main.bicep loops over edgeArcMachineIds).

param namePrefix string
param uniq string
param location string
param tags object
param workspaceId string

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: '${namePrefix}-dcr-edge-${uniq}'
  location: location
  tags: tags
  kind: 'Linux'
  properties: {
    description: 'OmniFile edge journal (omnifile-edge, smbd, Ganesha) via syslog, to Log Analytics.'
    dataSources: {
      syslog: [
        {
          name: 'omnifile-edge'
          streams: [
            'Microsoft-Syslog'
          ]
          facilityNames: [
            'daemon'
            'auth'
          ]
          logLevels: [
            'Info'
            'Notice'
            'Warning'
            'Error'
            'Critical'
            'Alert'
            'Emergency'
          ]
        }
      ]
    }
    destinations: {
      logAnalytics: [
        {
          name: 'workspace'
          workspaceResourceId: workspaceId
        }
      ]
    }
    dataFlows: [
      {
        streams: [
          'Microsoft-Syslog'
        ]
        destinations: [
          'workspace'
        ]
      }
    ]
  }
}

output id string = dcr.id
