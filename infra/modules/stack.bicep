// CloudLens stack — everything that lives inside the resource group.
//
// Two deployment shapes, chosen with `networkIsolation`:
//
//   true  — customer shape. PostgreSQL is VNet-injected and has no public
//           endpoint; storage is reachable only through a private endpoint; the
//           web app routes all outbound traffic through the VNet.
//   false — development shape. PostgreSQL and storage keep public endpoints
//           restricted to Azure services and one developer IP. No VNet is
//           created, so the deployment is cheaper and faster to tear down.
//
// Nothing in the application code depends on which shape is used: the app only
// ever sees a hostname, a database name and a managed identity.

@description('Azure region for every resource.')
param location string

@description('Prefix for resource names.')
param namePrefix string

@description('Optional environment token appended to resource names other than storage. Empty keeps the default CAF naming (id-cloudlens); setting it brackets the type abbreviation instead (CL-id-prod). PostgreSQL is folded to lower case.')
param nameSuffix string = ''

@description('Stable, reviewed export storage name. Generate once for a new deployment; preserve the actual account name on redeployment.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Object ID of the Entra principal that becomes the PostgreSQL administrator.')
param adminPrincipalObjectId string

@description('Sign-in name of that administrator.')
param adminPrincipalName string

@description('Type of that administrator principal.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param adminPrincipalType string

@description('Whether to deploy the network-isolated shape.')
param networkIsolation bool

@description('IPv4 CIDR for the new VNet when isolated. Must not overlap networks it will connect to. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param vnetAddressPrefix string = '10.70.0.0/16'

@description('IPv4 CIDR for App Service integration, inside the VNet and disjoint from other subnets. Minimum size /28; /26 or larger recommended. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param appSubnetAddressPrefix string = '10.70.1.0/24'

@description('IPv4 CIDR for PostgreSQL, inside the VNet and disjoint from other subnets. Minimum size /28. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param postgresSubnetAddressPrefix string = '10.70.2.0/24'

@description('IPv4 CIDR for private endpoints, inside the VNet and disjoint from other subnets, with sufficient usable addresses. Preserve the deployed range on existing deployments.')
@minLength(9)
@maxLength(18)
param privateEndpointSubnetAddressPrefix string = '10.70.3.0/24'

@description('Developer client IP allowed through the PostgreSQL firewall when not isolated.')
param developerClientIp string

@description('App Service plan SKU.')
param appServicePlanSku string

@description('PostgreSQL compute SKU.')
param postgresSku string

@description('PostgreSQL storage in GB.')
param postgresStorageGb int

@description('Region for the AI Foundry account, which is rarely the same as the app: the regions with App Service quota and the regions offering a model on a pay-as-you-go SKU are different sets.')
param aiLocation string

@description('Model deployed for the chat agent.')
param modelName string

@description('Model deployment SKU. GlobalStandard is pay-as-you-go; a region that only offers GlobalProvisionedManaged needs a different aiLocation.')
param modelSku string

@description('Tokens-per-minute quota, in thousands.')
param modelCapacity int

@description('True calls Azure as the signed-in person; false is app-identity mode. See main.bicep.')
param authDelegatedArm bool = true

@description('Tags applied to every resource.')
param tags object

// A short suffix keeps globally-unique names (PostgreSQL, web app)
// from colliding between deployments while staying readable.
var suffix = substring(uniqueString(resourceGroup().id), 0, 5)

// Two naming schemes, because organisations name things differently and the default
// cannot be changed without renaming — which in Azure means deleting and recreating.
//
// Default (nameSuffix empty): the CAF form, type abbreviation first — id-cloudlens,
// plan-cloudlens — with a hash suffix on the globally-unique ones.
//
// Bracketed (nameSuffix set): the caller's own tokens surround the type abbreviation,
// giving CL-id-prod and CL-plan-prod. PostgreSQL requires lower case, so CL is folded.
// Storage is deliberately independent of both schemes: its persisted name is required.
//
// Neither carries the hash suffix in this scheme: a deterministic name is the point of
// asking for one. That trades collision-resistance for predictability, and the names
// are globally unique across all of Azure, so a deployment can fail on a name another
// tenant already holds. deploy.ps1 checks availability before it provisions.
var bracketed = !empty(nameSuffix)

var identityName = bracketed ? '${namePrefix}-id-${nameSuffix}' : 'id-${namePrefix}'
var workspaceName = bracketed ? '${namePrefix}-log-${nameSuffix}' : 'log-${namePrefix}'
var insightsName = bracketed ? '${namePrefix}-appi-${nameSuffix}' : 'appi-${namePrefix}'
var postgresName = bracketed ? toLower('${namePrefix}-postgres-${nameSuffix}') : '${namePrefix}-pg-${suffix}'
var planName = bracketed ? '${namePrefix}-plan-${nameSuffix}' : 'plan-${namePrefix}'
var webAppName = bracketed ? '${namePrefix}-web-${nameSuffix}' : '${namePrefix}-${suffix}'
var vnetName = bracketed ? '${namePrefix}-vnet-${nameSuffix}' : 'vnet-${namePrefix}'
var foundryName = bracketed ? '${namePrefix}-aif-${nameSuffix}' : 'aif-${namePrefix}-${suffix}'
var projectName = bracketed ? '${namePrefix}-proj-${nameSuffix}' : 'proj-${namePrefix}'
var databaseName = 'cloudlens'

var postgresDnsZoneName = '${postgresName}.private.postgres.database.azure.com'
var blobDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'

// Role definition IDs are constants across every Azure tenant.
var storageBlobDataContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var monitoringMetricsPublisher = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
var cognitiveServicesOpenAIUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
var userAccessAdministrator = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9')

// ---------------------------------------------------------------- identity

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

// ------------------------------------------------------------- observability

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: insightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    // Local auth off means telemetry is ingested with Entra credentials only,
    // which is what the managed identity below is granted.
    DisableLocalAuth: true
  }
}

// ----------------------------------------------------------------- network

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = if (networkIsolation) {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
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
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: postgresSubnetAddressPrefix
          delegations: [
            {
              name: 'postgres'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
      {
        name: 'snet-privatelink'
        properties: {
          addressPrefix: privateEndpointSubnetAddressPrefix
        }
      }
    ]
  }
}

resource postgresDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (networkIsolation) {
  name: postgresDnsZoneName
  location: 'global'
  tags: tags
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (networkIsolation) {
  parent: postgresDnsZone
  name: 'link-${vnetName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource blobDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (networkIsolation) {
  name: blobDnsZoneName
  location: 'global'
  tags: tags
}

resource blobDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (networkIsolation) {
  parent: blobDnsZone
  name: 'link-${vnetName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// ----------------------------------------------------------------- storage
//
// Holds Cost Management exports and the Parquet archive. Shared keys are
// disabled so every read is an Entra call made by the managed identity — there
// is no connection string to leak into app settings or a customer runbook.

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: networkIsolation ? 'Disabled' : 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: networkIsolation ? 'Deny' : 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource exportsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'cloudlens'
  properties: {
    publicAccess: 'None'
  }
}

resource focusExportsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'focus-exports'
  properties: {
    publicAccess: 'None'
  }
}

resource blobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (networkIsolation) {
  name: bracketed ? '${namePrefix}-pe-blob-${nameSuffix}' : 'pe-${namePrefix}-blob'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/snet-privatelink'
    }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource blobPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (networkIsolation) {
  parent: blobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobDnsZone.id
        }
      }
    ]
  }
  dependsOn: [
    blobDnsLink
  ]
}

// -------------------------------------------------------------- PostgreSQL
//
// Password authentication is disabled outright. The application connects with
// its managed identity and the administrator connects with their own Entra
// account, so the server has no password to rotate, store or hand over.

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: {
    name: postgresSku
    tier: startsWith(postgresSku, 'Standard_B') ? 'Burstable' : 'GeneralPurpose'
  }
  properties: {
    version: '16'
    storage: {
      storageSizeGB: postgresStorageGb
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
    network: networkIsolation ? {
      delegatedSubnetResourceId: '${vnet.id}/subnets/snet-postgres'
      privateDnsZoneArmResourceId: postgresDnsZone.id
      publicNetworkAccess: 'Disabled'
    } : {
      publicNetworkAccess: 'Enabled'
    }
  }
  dependsOn: [
    postgresDnsLink
  ]
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// The human or pipeline running the deployment. Without this nobody can create
// the application's database role, because there is no password administrator.
resource postgresAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: adminPrincipalObjectId
  properties: {
    principalName: adminPrincipalName
    principalType: adminPrincipalType
    tenantId: subscription().tenantId
  }
}

// The application's managed identity is also registered as an Entra administrator.
//
// This is not laziness about least privilege — it is what makes the isolated
// shape deployable at all. A non-admin identity has to be introduced to
// PostgreSQL with `pgaadauth_create_principal`, which must be run by an existing
// Entra admin *from inside the virtual network*; on a server with no public
// endpoint that means standing up a jump host during every deployment. The
// server hosts exactly one database belonging to exactly one application, so
// admin on it grants nothing the app does not already own.
//
// A customer who wants the app demoted to a plain role can run
// `infra/sql/create-app-role.sql` from a VNet-connected client and remove this
// administrator; the application connects identically either way.
module postgresAppAdmin 'postgres-admin.bicep' = {
  name: 'cloudlens-postgres-app-admin'
  params: {
    postgresName: postgres.name
    principalObjectId: identity.properties.principalId
    principalName: identityName
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    postgresAdmin
    database
  ]
}

// Development shape only: let Azure services and the developer's own machine
// reach the public endpoint. Neither rule exists in the isolated shape.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!networkIsolation) {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [
    postgresAppAdmin
  ]
}

resource allowDeveloper 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!networkIsolation && !empty(developerClientIp)) {
  parent: postgres
  name: 'AllowDeployingClient'
  properties: {
    startIpAddress: developerClientIp
    endIpAddress: developerClientIp
  }
  dependsOn: [
    allowAzureServices
  ]
}

// ----------------------------------------------------------------- AI Foundry
//
// The chat agent is not optional: app/main.py refuses to start without
// PROJECT_ENDPOINT, so the account has to exist before the first deployment
// rather than being added later.
//
// It is deployed to `aiLocation` rather than `location` because the two
// constraints do not overlap. The regions with App Service quota and the
// regions offering a model on a pay-as-you-go SKU are different sets --
// centralindia, for instance, offers gpt-4.1-mini only as
// GlobalProvisionedManaged, which is reserved capacity nobody wants for a
// cost dashboard.

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: foundryName
  location: aiLocation
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled'
    // Required before a project can be created underneath the account. Azure
    // refuses the project with "Project can only created under AIServices Kind
    // account with allowProjectManagement set to true" without it.
    allowProjectManagement: true
    // Keys off. The app presents an Entra token from its managed identity, so a
    // key would be a second and weaker way in that nothing uses.
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: foundry
  name: projectName
  location: aiLocation
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'CloudLens'
    description: 'Cost analysis agent'
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundry
  name: modelName
  sku: {
    name: modelSku
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
    }
  }
  // A project and a deployment on the same account cannot be created in
  // parallel; ARM returns a conflict. The dependency serialises them.
  dependsOn: [
    project
  ]
}

// ----------------------------------------------------------------- web app

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  tags: tags
  sku: {
    name: appServicePlanSku
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    // Empty when not isolated, which App Service reads as "no VNet integration".
    virtualNetworkSubnetId: networkIsolation ? '${vnet.id}/subnets/snet-app' : null
    vnetRouteAllEnabled: networkIsolation
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      // --proxy-headers, because App Service terminates TLS and forwards to the container over
      // plain HTTP. Without it every request looks like `http` to the application, which has two
      // consequences that both read as bugs elsewhere: the session cookie is issued without the
      // `Secure` attribute, and the login throttle keys on `request.client.host` -- the App
      // Service front end, identical for every visitor -- so one attacker locks out everybody.
      //
      // The trusted range is the platform's, not the internet's: only the front end can reach
      // the container, so accepting its X-Forwarded-* headers is not the usual open door.
      appCommandLine: 'python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*'
      alwaysOn: appServicePlanSku != 'F1'
      ftpsState: 'Disabled'
      http20Enabled: true
      minTlsVersion: '1.2'
      healthCheckPath: '/healthz'
      appSettings: [
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'AUTH_DISABLED'
          value: 'false'
        }
        {
          // The mode is set here rather than patched onto the web app afterwards, so a later
          // infrastructure-only redeployment cannot quietly flip a deliberate choice back.
          name: 'AUTH_DELEGATED_ARM'
          value: string(authDelegatedArm)
        }
        {
          // Belt and braces with --proxy-headers above. If the forwarded headers are ever not
          // trusted -- a start command edited by hand, a future platform change -- the cookie
          // must still carry `Secure` rather than silently reverting to an attribute-less one.
          name: 'AUTH_COOKIE_SECURE'
          value: 'true'
        }
        {
          // Where the local sign-in account and the session-signing key live.
          //
          // The default is `data/` next to the code, and a zip deploy replaces the whole of
          // wwwroot -- so every deployment generated a fresh key, invalidated every session,
          // and rebuilt the admin account with a new random password that only appears in the
          // log. /home is the mounted share that survives a deploy and a restart.
          name: 'AUTH_DATA_DIR'
          value: '/home/cloudlens'
        }
        {
          name: 'WEBSITES_CONTAINER_START_TIME_LIMIT'
          value: '600'
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: identity.properties.clientId
        }
        {
          name: 'CLOUDLENS_PRINCIPAL_ID'
          value: identity.properties.principalId
        }
        {
          name: 'CLOUDLENS_TENANT_ID'
          value: tenant().tenantId
        }
        {
          name: 'SNAPSHOT_CACHE_ENABLED'
          value: 'true'
        }
        {
          name: 'WEBSITE_WARMUP_PATH'
          value: '/readyz'
        }
        {
          name: 'WEBSITE_WARMUP_STATUSES'
          value: '200'
        }
        {
          name: 'PGHOST'
          value: postgres.properties.fullyQualifiedDomainName
        }
        {
          name: 'PGDATABASE'
          value: databaseName
        }
        {
          name: 'PGUSER'
          value: identityName
        }
        {
          name: 'PGSSLMODE'
          value: 'require'
        }
        {
          name: 'ARCHIVE_ACCOUNT'
          value: storage.name
        }
        {
          name: 'ARCHIVE_CONTAINER'
          value: exportsContainer.name
        }
        {
          name: 'FOCUS_EXPORT_CONTAINER'
          value: focusExportsContainer.name
        }
        {
          name: 'FOCUS_EXPORT_PREFIX'
          value: 'focus'
        }
        // The full resource id, which ARCHIVE_ACCOUNT alone cannot substitute for: a Cost
        // Management export names its destination by resource id, so without this the app
        // has a place to archive to but nowhere to point a scheduled export, and falls back
        // to hunting exports owned by other resource groups whose storage it usually cannot
        // reach.
        {
          name: 'ARCHIVE_ACCOUNT_ID'
          value: storage.id
        }
        // Region stamped on exports this deployment creates. An export carrying an identity
        // must name a real region, and it has to be one this stack actually lives in rather
        // than whichever region the code was first written against.
        {
          name: 'ARCHIVE_LOCATION'
          value: location
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: insights.properties.ConnectionString
        }
        {
          name: 'PROJECT_ENDPOINT'
          value: '${foundry.properties.endpoint}api/projects/${projectName}'
        }
        {
          name: 'MODEL_DEPLOYMENT_NAME'
          value: modelName
        }
        {
          name: 'WEBSITE_DNS_SERVER'
          value: networkIsolation ? '168.63.129.16' : ''
        }
      ]
    }
  }
}

// ---------------------------------------------------------------- RBAC
//
// Cost Management Reader is deliberately not granted here: its scope is the
// subscription or management group being analysed, which is often not the
// subscription the app runs in. deploy.ps1 grants it separately.

resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, storageBlobDataContributor)
  properties: {
    roleDefinitionId: storageBlobDataContributor
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Creating a Cost Management export with `identity: SystemAssigned` makes Azure
// grant that newly minted identity a role on the destination container *on the
// caller's behalf*. So the caller needs the right to hand out roles there, not
// merely to write there. Without this the app can archive to its own account but
// cannot schedule an export into it, and the refusal arrives as a bare 401
// RBACAccessDenied that names neither the action nor the resource. Scoped to the
// one storage account, which is far narrower than the role's name suggests.
resource storageUserAccessAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, userAccessAdministrator)
  properties: {
    roleDefinitionId: userAccessAdministrator
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    // Limits what may be delegated onward to exactly the role an export needs.
    condition: '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals{ba92f5b4-2d11-453d-a403-e96b0029c9fe}))'
    conditionVersion: '2.0'
  }
}

resource metricsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: insights
  name: guid(insights.id, identity.id, monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: monitoringMetricsPublisher
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Reader on the subscription does not cover the Foundry data plane. Without
// this every question comes back 401 from an app that otherwise looks healthy.
resource foundryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, identity.id, cognitiveServicesOpenAIUser)
  properties: {
    roleDefinitionId: cognitiveServicesOpenAIUser
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output webAppName string = webApp.name
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output postgresName string = postgres.name
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output postgresDatabase string = databaseName
output storageAccount string = storage.name
output storageAccountId string = storage.id
output storageContainer string = exportsContainer.name
output managedIdentityName string = identity.name
output managedIdentityClientId string = identity.properties.clientId
output managedIdentityPrincipalId string = identity.properties.principalId
output applicationInsightsConnectionString string = insights.properties.ConnectionString
output foundryAccount string = foundry.name
output foundryEndpoint string = foundry.properties.endpoint
output projectEndpoint string = '${foundry.properties.endpoint}api/projects/${projectName}'
output modelDeployment string = model.name
