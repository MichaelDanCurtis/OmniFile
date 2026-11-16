// The verifier: a Container Apps job that scales out for bulk full-content CRC64-NVME checks (decision 24). Each execution
// runs one pass over the verify queue and exits (`python -m omnifile_service.worker.verifier`; 0 when every message was
// handled, 1 otherwise).
//
// Schedule (main.bicep's default): an execution on a cron schedule, each replica draining the queue it shares with the
//   worker, within its time budget.
// Manual: an execution only when someone (an operator, or the worker through the ARM "start job" action and the
//   job-starter role, see roles.bicep) starts one.
// Event: KEDA starts executions from a scale rule. The example rule watches the depth of a PGMQ queue.
//   VERIFY: the PostgreSQL scaler with Entra (managed identity) authentication on Container Apps jobs. If it is not
//   available, keep Manual, or have the worker start the job when it enqueues verification work.

param name string
param location string
param tags object
param environmentId string
param workloadProfileName string = 'Consumption'
param identityId string
param image string
param registryServer string = ''
@description('The container\'s command (argv). It replaces the image\'s ENTRYPOINT, and the image\'s CMD is then ignored.')
@minLength(1)
param command array

@allowed([ 'Schedule', 'Manual', 'Event' ])
param triggerType string = 'Schedule'
@description('Schedule only: when executions start (cron, UTC).')
param cronExpression string = '*/15 * * * *'
@description('Replicas that run at the same time in one execution.')
param parallelism int = 4
@description('Replicas that must finish successfully to complete one execution.')
param replicaCompletionCount int = 4
@description('Seconds. A large file takes a long full read, so the default is generous.')
param replicaTimeoutSeconds int = 14400
param replicaRetryLimit int = 1
param cpu string = '1'
param memory string = '2Gi'
param env array = []

@description('Event trigger only: KEDA scale rules for the job.')
param eventScaleRules array = []
param eventMaxExecutions int = 10
param eventPollingIntervalSeconds int = 30

resource job 'Microsoft.App/jobs@2025-07-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: workloadProfileName
    configuration: {
      triggerType: triggerType
      replicaTimeout: replicaTimeoutSeconds
      replicaRetryLimit: replicaRetryLimit
      scheduleTriggerConfig: triggerType == 'Schedule' ? {
        cronExpression: cronExpression
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      } : null
      manualTriggerConfig: triggerType == 'Manual' ? {
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      } : null
      eventTriggerConfig: triggerType == 'Event' ? {
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
        scale: {
          minExecutions: 0
          maxExecutions: eventMaxExecutions
          pollingInterval: eventPollingIntervalSeconds
          rules: eventScaleRules
        }
      } : null
      registries: empty(registryServer) ? [] : [
        {
          server: registryServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: name
          image: image
          command: command
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: env
        }
      ]
    }
  }
}

output id string = job.id
output name string = job.name
