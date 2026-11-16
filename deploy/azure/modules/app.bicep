// A Container App with a user-assigned managed identity and optional HTTP ingress.

param name string
param location string
param tags object
param environmentId string
param workloadProfileName string = 'Consumption'

param identityId string
param image string
param registryServer string = ''
@description('The container\'s command (argv). It replaces the image\'s ENTRYPOINT, and the image\'s CMD is then ignored, so each app runs exactly this. Required: one image serves several apps.')
@minLength(1)
param command array

@description('none = no ingress. internal = reachable only by other apps in the environment. external = published at the environment boundary (for an INTERNAL environment that means the VNet, not the internet).')
@allowed([ 'none', 'internal', 'external' ])
param ingress string = 'none'
param targetPort int = 8080
@description('http = the ingress terminates TLS (HTTP/1.1 and HTTP/2). tcp = the ingress passes TCP through and the app terminates TLS itself (threat model T5); needs a VNet environment.')
@allowed([ 'http', 'tcp' ])
param transport string = 'http'
@description('tcp only: the port the environment exposes. It must be unique across the environment and is never 80 or 443 (used by HTTP ingress). 0 = the target port.')
param exposedPort int = 0
@description('mTLS at the ingress (http only). require = the client must present a certificate; the app then validates X-Forwarded-Client-Cert against the OmniFile CA.')
@allowed([ 'require', 'accept', 'ignore' ])
param clientCertificateMode string = 'ignore'
@description('HTTP path for the readiness probe (liveness is a TCP probe on the port). Empty = platform default TCP probes.')
param healthProbePath string = ''

param cpu string = '0.5'
param memory string = '1Gi'
param minReplicas int = 1
param maxReplicas int = 3
param concurrentRequestsPerReplica int = 50
@description('Add a CPU scale rule. Used by apps with no HTTP ingress.')
param scaleOnCpuPercent int = 0

param env array = []
@description('Container Apps secrets. Use { name, keyVaultUrl, identity } to reference a specific Key Vault secret.')
param secrets array = []
@description('Volumes, for example [{ name: "mtls", storageType: "Secret", secrets: [{ secretRef: "x", path: "file" }] }].')
param volumes array = []
param volumeMounts array = []
@description('CIDR ranges allowed through the ingress. Empty = no ingress IP restriction. When set, all other sources are refused.')
param ingressAllowedCidrs array = []

var hasIngress = ingress != 'none'
var isTcp = hasIngress && transport == 'tcp'
// The app terminates TLS (and requires a client certificate) on a TCP ingress, so an HTTP probe can't reach it:
// probe the port instead. api binds its port only once enrol has signed its server certificate, so a startup
// probe gives it up to 10 + 10 x 30 s first: liveness and readiness start only after it, so a replica waiting on
// enrol isn't restarted in a loop (review of #34). After that, a restart retries.
var tcpProbes = [
  {
    type: 'Startup'
    tcpSocket: {
      port: targetPort
    }
    initialDelaySeconds: 10
    periodSeconds: 30
    failureThreshold: 10
  }
  {
    type: 'Liveness'
    tcpSocket: {
      port: targetPort
    }
  }
  {
    type: 'Readiness'
    tcpSocket: {
      port: targetPort
    }
  }
]
// Liveness is the port alone, readiness the health path: an app still waiting for its second-phase
// settings (the CA, its app registration, the relay's certificate) answers 503 there, so it shows
// as not ready and takes no traffic, and is not restarted in a loop. A process that dies closes the port.
var probes = isTcp ? tcpProbes : empty(healthProbePath) || !hasIngress ? [] : [
  {
    type: 'Liveness'
    tcpSocket: {
      port: targetPort
    }
  }
  {
    type: 'Readiness'
    httpGet: {
      path: healthProbePath
      port: targetPort
    }
  }
]
var httpRule = hasIngress && !isTcp ? [
  {
    name: 'http'
    http: {
      metadata: {
        concurrentRequests: string(concurrentRequestsPerReplica)
      }
    }
  }
] : []
var tcpRule = isTcp ? [
  {
    name: 'tcp'
    tcp: {
      metadata: {
        concurrentConnections: string(concurrentRequestsPerReplica)
      }
    }
  }
] : []
var cpuRule = scaleOnCpuPercent > 0 ? [
  {
    name: 'cpu'
    custom: {
      type: 'cpu'
      metadata: {
        type: 'Utilization'
        value: string(scaleOnCpuPercent)
      }
    }
  }
] : []

resource app 'Microsoft.App/containerApps@2025-07-01' = {
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
      activeRevisionsMode: 'Single'
      ingress: hasIngress ? {
        external: ingress == 'external'
        targetPort: targetPort
        transport: isTcp ? 'tcp' : 'auto'
        exposedPort: isTcp ? (exposedPort == 0 ? targetPort : exposedPort) : null
        allowInsecure: isTcp ? null : false
        // The property path is properties.configuration.ingress.clientCertificateMode. The platform requires the
        // certificate but does not validate the chain: the app must check X-Forwarded-Client-Cert against the OmniFile CA.
        // A TCP ingress terminates no TLS, so it has no client certificate mode: the app checks the certificate itself.
        clientCertificateMode: isTcp ? null : clientCertificateMode
        ipSecurityRestrictions: empty(ingressAllowedCidrs) ? null : map(ingressAllowedCidrs, (cidr, i) => {
          name: 'allow-${i}'
          ipAddressRange: cidr
          action: 'Allow'
        })
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      } : null
      registries: empty(registryServer) ? [] : [
        {
          server: registryServer
          identity: identityId
        }
      ]
      secrets: secrets
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
          probes: probes
          volumeMounts: volumeMounts
        }
      ]
      volumes: volumes
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: concat(httpRule, tcpRule, cpuRule)
      }
    }
  }
}

output id string = app.id
output name string = app.name
output fqdn string = hasIngress ? app.properties.configuration.ingress.fqdn : ''
@description('The port clients connect to: 443 for HTTP ingress, the exposed port for TCP.')
output port int = isTcp ? (exposedPort == 0 ? targetPort : exposedPort) : 443
