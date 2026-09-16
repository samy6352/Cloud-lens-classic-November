// Registering an Entra administrator whose object ID is only known at runtime.
//
// A resource's name must be computable before the deployment starts, and the
// managed identity's principal ID is not -- it exists only once the identity has
// been created. Passing it through a nested deployment resolves it first, which
// is the supported way around that restriction.

@description('Name of the PostgreSQL flexible server.')
param postgresName string

@description('Object ID of the principal to register as an administrator.')
param principalObjectId string

@description('Display or sign-in name of that principal.')
param principalName string

@description('Type of that principal.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param principalType string

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresName
}

resource administrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: principalObjectId
  properties: {
    principalName: principalName
    principalType: principalType
    tenantId: subscription().tenantId
  }
}
