targetScope = 'resourceGroup'

@description('Existing storage account in this resource group. This deployment never recreates it.')
param storageAccountName string

@description('New virtual network name; preserve on subsequent deployments.')
param virtualNetworkName string

@description('New Blob private endpoint name.')
param privateEndpointName string

param location string = resourceGroup().location
param virtualNetworkAddressPrefix string = '10.70.0.0/16'
param appSubnetAddressPrefix string = '10.70.1.0/24'
param endpointSubnetAddressPrefix string = '10.70.3.0/24'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [virtualNetworkAddressPrefix]
    }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: appSubnetAddressPrefix
          delegations: [
            {
              name: 'appservice'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
        }
      }
      {
        name: 'snet-privatelink'
        properties: {
          addressPrefix: endpointSubnetAddressPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'link-${virtualNetworkName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: network.id
    }
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: privateEndpointName
  location: location
  properties: {
    subnet: {
      id: '${network.id}/subnets/snet-privatelink'
    }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource dnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: zone.id
        }
      }
    ]
  }
  dependsOn: [link]
}

output appSubnetId string = '${network.id}/subnets/snet-app'
output privateEndpointId string = endpoint.id
output privateDnsZoneId string = zone.id
