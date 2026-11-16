// One VNet with four subnets, each with its own NSG. Nothing here has a public IP.
//   snet-cae-internal  workload-profile Container Apps environment (api, enrol, web, worker, verifier). Internal load balancer.
//   snet-cae-relay     second, EXTERNAL Container Apps environment that hosts only the Teams relay.
//   snet-postgres      delegated to PostgreSQL Flexible Server (private access by VNet integration).
//   snet-private-endpoints  where the customer puts the storage / Key Vault private endpoints (decision 22).
//
// Isolation goals: the relay (internet-facing) can reach the internal api and nothing else in the VNet: not PostgreSQL,
// not the private endpoints, not on-premises. PostgreSQL accepts connections only from the internal environment.
// NSG rules per https://learn.microsoft.com/en-us/azure/container-apps/firewall-integration

param name string
param location string
param tags object
param addressPrefix string
param caeInternalSubnetPrefix string
param caeRelaySubnetPrefix string
param postgresSubnetPrefix string
param privateEndpointSubnetPrefix string

@description('Extra source ranges (for example a jump host subnet) allowed to reach PostgreSQL on 5432, so the Entra administrator can create roles.')
param postgresAdminSourcePrefixes array = []
@description('TCP ingress ports of the internal environment besides 443 (api\'s, with apiIngress = tcp).')
param extraIngressPorts array = []

@description('The internal environment\'s ports the relay may call: api\'s TCP port only. The relay calls nothing else there.')
@minLength(1)
param relayToInternalPorts array

var caeIngressPorts = concat([ '443', '31443' ], extraIngressPorts)

// ---- internal environment: edge appliances (VNet / on-prem), relay and admins come in on 443 (and api's TCP port) only.
resource internalNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-cae-internal-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-https-from-vnet'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: caeInternalSubnetPrefix
          destinationPortRanges: caeIngressPorts
        }
      }
      {
        name: 'allow-load-balancer-probes'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: caeInternalSubnetPrefix
          destinationPortRange: '30000-32767'
        }
      }
      {
        name: 'allow-intra-subnet'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: caeInternalSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: caeInternalSubnetPrefix
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-all-other-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      // Outbound: the default rules allow it. The service must reach PostgreSQL, the private endpoints, Teams, Graph, Entra
      // and, during migrations, AWS, so no deny-by-default egress is set here. Tighten with Azure Firewall and a UDR if the
      // client wants FQDN-level egress control (docs: use-azure-firewall).
    ]
  }
}

// ---- relay environment. NOTE: for an EXTERNAL workload-profile environment, inbound internet traffic arrives through a
// public IP in the managed resource group, not through this subnet, so inbound NSG rules cannot restrict it (docs).
// Restrict callers with the relay app's ingress IP restrictions instead (main.bicep: relayAllowedCidrs).
// What the NSG does control is OUTBOUND: the relay may call the platform, the internet on 443 (Bot Framework keys,
// registry) and the internal api. It may not reach PostgreSQL, the private endpoints, or any other private address.
resource relayNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-cae-relay-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'out-allow-intra-subnet'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: caeRelaySubnetPrefix
          destinationPortRange: '*'
        }
      }
      {
        name: 'out-allow-mcr'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'MicrosoftContainerRegistry'
          destinationPortRange: '443'
        }
      }
      {
        name: 'out-allow-frontdoor-firstparty'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureFrontDoor.FirstParty'
          destinationPortRange: '443'
        }
      }
      {
        name: 'out-allow-entra'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureActiveDirectory'
          destinationPortRange: '443'
        }
      }
      {
        name: 'out-allow-azure-dns'
        properties: {
          priority: 140
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '168.63.129.16'
          destinationPortRange: '53'
        }
      }
      {
        name: 'out-deny-postgres'
        properties: {
          priority: 190
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: postgresSubnetPrefix
          destinationPortRange: '*'
        }
      }
      {
        name: 'out-allow-internal-api'
        properties: {
          priority: 200
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: caeInternalSubnetPrefix
          destinationPortRanges: relayToInternalPorts
        }
      }
      {
        // login.botframework.com (signing keys) and the registry. NSGs cannot filter by name: to narrow this to those hosts,
        // route the subnet through Azure Firewall with FQDN rules.
        name: 'out-allow-internet-https'
        properties: {
          priority: 300
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: caeRelaySubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '443'
        }
      }
      {
        name: 'out-deny-all-private'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

// ---- PostgreSQL: only the internal environment (and named admin ranges) may connect.
resource postgresNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-postgres-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: concat([
      {
        name: 'allow-postgres-from-internal-env'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: caeInternalSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: postgresSubnetPrefix
          destinationPortRanges: [ '5432', '6432' ]
        }
      }
      {
        name: 'allow-intra-subnet'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: postgresSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: postgresSubnetPrefix
          destinationPortRange: '*'
        }
      }
    ], empty(postgresAdminSourcePrefixes) ? [] : [
      {
        name: 'allow-postgres-from-admin-ranges'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: postgresAdminSourcePrefixes
          sourcePortRange: '*'
          destinationAddressPrefix: postgresSubnetPrefix
          destinationPortRanges: [ '5432', '6432' ]
        }
      }
    ], [
      {
        name: 'deny-all-other-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ])
  }
}

// ---- private endpoints: HTTPS from inside the network (internal environment, on-prem edge appliances) only.
resource peNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-pe-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-https-from-vnet'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: privateEndpointSubnetPrefix
          destinationPortRange: '443'
        }
      }
      {
        name: 'deny-all-other-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressPrefix
      ]
    }
    subnets: [
      {
        name: 'snet-cae-internal'
        properties: {
          addressPrefix: caeInternalSubnetPrefix
          networkSecurityGroup: {
            id: internalNsg.id
          }
          delegations: [
            {
              name: 'cae'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-cae-relay'
        properties: {
          addressPrefix: caeRelaySubnetPrefix
          networkSecurityGroup: {
            id: relayNsg.id
          }
          delegations: [
            {
              name: 'cae'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: postgresSubnetPrefix
          networkSecurityGroup: {
            id: postgresNsg.id
          }
          delegations: [
            {
              name: 'pg'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          networkSecurityGroup: {
            id: peNsg.id
          }
          // Makes the NSG above apply to traffic to the private endpoints.
          privateEndpointNetworkPolicies: 'NetworkSecurityGroupEnabled'
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output caeInternalSubnetId string = '${vnet.id}/subnets/snet-cae-internal'
output caeRelaySubnetId string = '${vnet.id}/subnets/snet-cae-relay'
output postgresSubnetId string = '${vnet.id}/subnets/snet-postgres'
output privateEndpointSubnetId string = '${vnet.id}/subnets/snet-private-endpoints'
