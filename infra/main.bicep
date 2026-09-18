// CloudLens — subscription-scope entry point.
//
// Creates the resource group and deploys the whole stack into it, so a customer
// deployment is one command against an empty subscription:
//
//   az deployment sub create -l <region> -f infra/main.bicep -p @infra/main.parameters.json
//
targetScope = 'subscription'

@description('Name of the resource group to create or reuse.')
param resourceGroupName string = 'rg-cloudlens'

@description('Azure region for every resource in the stack.')
param location string

@description('Prefix for resource names other than the independently named export storage account.')
@minLength(2)
@maxLength(12)
param namePrefix string = 'cloudlens'

@description('Optional environment token appended to resource names other than export storage, e.g. prod. Empty keeps the default naming (id-cloudlens, plan-cloudlens); setting it produces CL-id-prod, CL-plan-prod and so on. These names become deterministic, so deploy.ps1 checks the globally-unique ones for availability first.')
@maxLength(10)
param nameSuffix string = ''

@description('Reviewed, stable export storage account name (3-24 lowercase letters/digits). Required: for a new deployment generate a random name once and save it in the parameters; for redeployment use the actual existing account, never a newly generated name. deploy.ps1 generates/persists or adopts this value.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Object ID of the Entra principal that becomes the PostgreSQL administrator. Usually the person or pipeline running this deployment.')
param adminPrincipalObjectId string

@description('Sign-in name of that administrator, e.g. someone@contoso.com.')
param adminPrincipalName string

@description('Type of that administrator principal.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param adminPrincipalType string = 'User'

@description('True for a customer deployment: PostgreSQL is injected into the virtual network and has no public endpoint. False for development: PostgreSQL keeps a public endpoint restricted by firewall rules.')
param networkIsolation bool = true

@description('IPv4 CIDR for the new VNet when networkIsolation is true. Choose a range that does not overlap networks it will connect to. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param vnetAddressPrefix string = '10.70.0.0/16'

@description('IPv4 CIDR for App Service integration, contained within the VNet and disjoint from the other subnets. Minimum subnet size /28; /26 or larger is recommended. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param appSubnetAddressPrefix string = '10.70.1.0/24'

@description('IPv4 CIDR for PostgreSQL, contained within the VNet and disjoint from the other subnets. Minimum subnet size /28. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param postgresSubnetAddressPrefix string = '10.70.2.0/24'

@description('IPv4 CIDR for private endpoints, contained within the VNet and disjoint from the other subnets, with sufficient usable addresses. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param privateEndpointSubnetAddressPrefix string = '10.70.3.0/24'

@description('Client IP allowed through the PostgreSQL firewall when networkIsolation is false. Ignored otherwise.')
param developerClientIp string = ''

@description('App Service plan SKU. B1 is the smallest tier that supports Always On and VNet integration.')
param appServicePlanSku string = 'B1'

@description('PostgreSQL compute SKU.')
param postgresSku string = 'Standard_B1ms'

@description('PostgreSQL major version. Central India offers 11-18.')
param postgresVersion string = '16'

@description('Storage type. Premium_LRS ties IOPS to capacity; PremiumV2_LRS provisions IOPS and throughput independently of size.')
@allowed([
  'Premium_LRS'
  'PremiumV2_LRS'
])
param postgresStorageType string = 'Premium_LRS'

@description('Provisioned IOPS, PremiumV2_LRS only.')
param postgresIops int = 3000

@description('Provisioned throughput MB/sec, PremiumV2_LRS only.')
param postgresThroughput int = 125

@description('Create a new `costs` table partitioned by month. Must be correct before the application first starts; it cannot convert an existing table.')
param partitionCosts bool = false

@description('Session work_mem for application connections. Empty keeps the server default.')
param pgWorkMem string = ''

@description('Session statement_timeout for application connections. Empty means no cap.')
param pgStatementTimeout string = ''

@description('Ingest cost data on first boot when the warehouse is empty.')
param autoIngest bool = true

@description('Run the daily refresh.')
param autoRefresh bool = true

@description('Allow username/password sign-in alongside Entra.')
param authAllowLocal bool = false

@description('Local accounts as name:hash:role, PBKDF2 hashed.')
@secure()
param authUsers string = ''

@description('Schema the warehouse owns. Empty uses `public`.')
param pgSchema string = ''

@description('PostgreSQL storage in GB.')
param postgresStorageGb int = 32

@description('Region for the AI Foundry account. Rarely the same as `location`: the regions with App Service quota and the regions offering a model on a pay-as-you-go SKU are different sets.')
param aiLocation string = 'eastus2'

@description('Model deployed for the chat agent.')
param modelName string = 'gpt-4.1-mini'

@description('Model deployment SKU. GlobalStandard is pay-as-you-go; a region offering only GlobalProvisionedManaged needs a different aiLocation.')
param modelSku string = 'GlobalStandard'

@description('Tokens-per-minute quota, in thousands.')
param modelCapacity int = 30

@description('Create the Entra app registration people sign in through. Needs directory rights (Application Developer or higher); leave false where the identity team owns app registrations and pass an existing client ID to deploy.ps1 instead.')
param deployEntraApp bool = false

@description('Display name for that app registration.')
param entraAppDisplayName string = 'CloudLens'

@description('Stable unique name for it. Changing this creates a second registration rather than renaming the first.')
param entraAppUniqueName string = 'cloudlens'

@description('True calls Azure as the signed-in person, so each sees exactly their own subscriptions. Needs one-time tenant admin consent for ARM `user_impersonation`; without it nobody can sign in. False is app-identity mode: the app resolves who holds a cost-capable role, bounded by the subscriptions its own identity reads.')
param authDelegatedArm bool = true

@description('Tags applied to every resource.')
param tags object = {
  application: 'CloudLens'
  workload: 'azure-cost-management'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module stack 'modules/stack.bicep' = {
  name: 'cloudlens-stack'
  scope: rg
  params: {
    location: location
    namePrefix: namePrefix
    nameSuffix: nameSuffix
    storageAccountName: storageAccountName
    adminPrincipalObjectId: adminPrincipalObjectId
    adminPrincipalName: adminPrincipalName
    adminPrincipalType: adminPrincipalType
    networkIsolation: networkIsolation
    vnetAddressPrefix: vnetAddressPrefix
    appSubnetAddressPrefix: appSubnetAddressPrefix
    postgresSubnetAddressPrefix: postgresSubnetAddressPrefix
    privateEndpointSubnetAddressPrefix: privateEndpointSubnetAddressPrefix
    developerClientIp: developerClientIp
    appServicePlanSku: appServicePlanSku
    postgresSku: postgresSku
    postgresVersion: postgresVersion
    postgresStorageType: postgresStorageType
    postgresIops: postgresIops
    postgresThroughput: postgresThroughput
    partitionCosts: partitionCosts
    pgWorkMem: pgWorkMem
    pgStatementTimeout: pgStatementTimeout
    autoIngest: autoIngest
    autoRefresh: autoRefresh
    authAllowLocal: authAllowLocal
    authUsers: authUsers
    pgSchema: pgSchema
    postgresStorageGb: postgresStorageGb
    aiLocation: aiLocation
    modelName: modelName
    modelSku: modelSku
    modelCapacity: modelCapacity
    authDelegatedArm: authDelegatedArm
    tags: tags
  }
}

// The sign-in app registration.
//
// Deployed after the stack rather than alongside it, because the redirect URI
// contains the web app's hostname and that is not known until the stack exists.
// Taking it from the stack's own output is the only way to get it right without
// recomputing the name suffix here and hoping the two agree.
//
// AUTH_CLIENT_ID and friends are then set on the web app by deploy.ps1. They
// cannot be set in stack.bicep: the app settings live on the resource this
// module depends on, so reading the client ID there would be a cycle.
module entra 'modules/identity.bicep' = if (deployEntraApp) {
  name: 'cloudlens-entra'
  scope: rg
  params: {
    displayName: entraAppDisplayName
    uniqueName: entraAppUniqueName
    redirectUris: [
      '${stack.outputs.webAppUrl}/auth/callback'
      'http://localhost:8000/auth/callback'
    ]
  }
}

output resourceGroup string = rg.name
output webAppName string = stack.outputs.webAppName
output entraClientId string = deployEntraApp ? entra!.outputs.clientId : ''
output authRedirectUri string = '${stack.outputs.webAppUrl}/auth/callback'
output webAppUrl string = stack.outputs.webAppUrl
output postgresHost string = stack.outputs.postgresHost
output postgresDatabase string = stack.outputs.postgresDatabase
output storageAccount string = stack.outputs.storageAccount
output storageAccountId string = stack.outputs.storageAccountId
output storageContainer string = stack.outputs.storageContainer
output managedIdentityName string = stack.outputs.managedIdentityName
output managedIdentityClientId string = stack.outputs.managedIdentityClientId
output managedIdentityPrincipalId string = stack.outputs.managedIdentityPrincipalId
output foundryAccount string = stack.outputs.foundryAccount
output projectEndpoint string = stack.outputs.projectEndpoint
output modelDeployment string = stack.outputs.modelDeployment
