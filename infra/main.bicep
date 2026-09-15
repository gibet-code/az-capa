// Azure Capacity — infrastructure.
// Provisions a Flex Consumption Python Function App with two storage accounts:
// one for the Functions host + app data, one dedicated to Durable Functions.
// storageConnectivity selects how the app reaches storage: Public, ServiceEndpoint
// (VNet-injected app subnet allowed on storage) or PrivateEndpoint (storage public
// access disabled, reached over private endpoints in the same VNet).
// Code is deployed separately (Core Tools `func azure functionapp publish`).
// Easy Auth: set configureEasyAuth=true + appRegistrationClientId to wire it up here,
// or leave it off and run infra/auth.bicep afterwards — see docs/operations/easy-auth-setup.md.

targetScope = 'resourceGroup'

@description('Global scoping. Subscription IDs and management group IDs are mutually exclusive; leave both empty to discover subscriptions visible to the app identity.')
type scopeConfig = {
  @description('Comma-separated subscription IDs, or empty to discover subscriptions visible to the app identity.')
  subscriptionIds: string
  @description('Comma-separated management group IDs (mutually exclusive with subscriptionIds).')
  managementGroupIds: string
  @description('Comma-separated meter/location filter; empty for all locations.')
  locations: string
}

@description('Cost Management retrieval configuration, shared by every Cost Management consumer.')
type costManagementConfig = {
  @description('per-sub queries each subscription; per-billing queries the billing profile.')
  method: 'per-sub' | 'per-billing'
  @description('EA needs two queries (hours + cost); MCA needs one.')
  agreementType: 'ea' | 'mca'
  @description('Billing account ID (required for per-billing).')
  billingAccountId: string
  @description('Billing profile ID (MCA per-billing).')
  billingProfileId: string
}

@description('Outputs describing the deployed app.')
type deploymentOutputs = {
  functionAppName: string
  functionAppHostName: string
  @description('Managed identity principal ID — assign Azure Capacity Backend Reader on the target scope.')
  managedIdentityPrincipalId: string
  managedIdentityClientId: string
  @description('True when Easy Auth was configured in this deployment.')
  easyAuthConfigured: bool
  @description('Post-deployment instructions.')
  nextSteps: string
}

@description('Prefix for resource names; also the default Function App name stem. Lowercase letters/numbers.')
@minLength(3)
@maxLength(17)
param namePrefix string = 'azcapacity'

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

param scope scopeConfig = {
  subscriptionIds: ''
  managementGroupIds: ''
  locations: ''
}

param costManagement costManagementConfig = {
  method: 'per-sub'
  agreementType: 'ea'
  billingAccountId: ''
  billingProfileId: ''
}

@description('Memory per instance. Allowed Flex values: 512, 2048, 4096.')
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 2048

@description('Maximum scale-out instance count.')
@minValue(1)
@maxValue(1000)
param maximumInstanceCount int = 40

@description('Deploy Application Insights + Log Analytics for observability.')
param deployApplicationInsights bool = true

@description('Network path from the Function App to its storage accounts. Public keeps storage on its public endpoint; ServiceEndpoint creates a VNet, injects the app into a delegated subnet and restricts storage to that subnet via a Microsoft.Storage service endpoint; PrivateEndpoint creates a VNet with the app subnet plus a private-endpoint subnet, disables storage public access and reaches storage over private endpoints (blob/queue/table) resolved by private DNS in the same VNet.')
@allowed(['Public', 'ServiceEndpoint', 'PrivateEndpoint'])
param storageConnectivity string = 'Public'

@description('Address space for the VNet created for ServiceEndpoint/PrivateEndpoint. Two /26 subnets are carved out (app + private endpoints).')
param vnetAddressPrefix string = '10.100.0.0/24'

@description('Set to true if an Entra app registration already exists for Easy Auth. When true, appRegistrationClientId is required and Easy Auth (authsettingsV2) is configured in this deployment. When false, deploy infra/auth.bicep after creating the app registration (docs/operations/easy-auth-setup.md).')
param configureEasyAuth bool = false

@description('Application (client) ID of the existing Entra app registration. Required when configureEasyAuth is true; ignored otherwise.')
param appRegistrationClientId string = ''

@description('Entra tenant ID for the Easy Auth OpenID issuer. Defaults to the deployment tenant.')
param entraTenantId string = tenant().tenantId

var useServiceEndpoint = storageConnectivity == 'ServiceEndpoint'
var usePrivateEndpoint = storageConnectivity == 'PrivateEndpoint'
var useVnet = useServiceEndpoint || usePrivateEndpoint
var vnetName = '${toLower(namePrefix)}-vnet'
var appSubnetName = 'functionapp'

var storagePublicNetworkAccess = usePrivateEndpoint ? 'Disabled' : 'Enabled'
var storageNetworkAcls = useServiceEndpoint
  ? {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      virtualNetworkRules: [
        {
          action: 'Allow'
          id: appSubnet.id
        }
      ]
    }
  : usePrivateEndpoint
      ? {
          bypass: 'AzureServices'
          defaultAction: 'Deny'
        }
      : {
          bypass: 'AzureServices'
          defaultAction: 'Allow'
        }

var privateDnsZoneServices = ['blob', 'queue', 'table']
var privateEndpointConfigs = [
  {
    key: 'app-blob'
    storageId: appStorageAccount.id
    service: 'blob'
  }
  {
    key: 'app-table'
    storageId: appStorageAccount.id
    service: 'table'
  }
  {
    key: 'durable-blob'
    storageId: durableStorageAccount.id
    service: 'blob'
  }
  {
    key: 'durable-queue'
    storageId: durableStorageAccount.id
    service: 'queue'
  }
  {
    key: 'durable-table'
    storageId: durableStorageAccount.id
    service: 'table'
  }
]

var uniqueSuffix = uniqueString(resourceGroup().id, namePrefix)
var sanitizedPrefix = replace(replace(toLower(namePrefix), '-', ''), '_', '')
var appStorageName = '${take(sanitizedPrefix, 24 - length(uniqueSuffix))}${uniqueSuffix}'
var durableStorageName = '${take(sanitizedPrefix, 22 - length(uniqueSuffix))}df${uniqueSuffix}'
var functionAppName = '${toLower(namePrefix)}-${uniqueSuffix}'
var deploymentContainerName = 'app-package'
var zoneMappingsContainerName = 'zone-mappings'
var computeSkusContainerName = 'compute-skus'
var azureReferenceDataContainerName = 'azure-reference-data'
var subscriptionContextContainerName = 'subscription-context'

var storageSuffix = environment().suffixes.storage
var appBlobUri = 'https://${appStorageName}.blob.${storageSuffix}'
var appQueueUri = 'https://${appStorageName}.queue.${storageSuffix}'
var appTableUri = 'https://${appStorageName}.table.${storageSuffix}'
var durableBlobUri = 'https://${durableStorageName}.blob.${storageSuffix}'
var durableQueueUri = 'https://${durableStorageName}.queue.${storageSuffix}'
var durableTableUri = 'https://${durableStorageName}.table.${storageSuffix}'

// Built-in role definition IDs for identity-based storage access.
var roleBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var roleTableDataContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var roleMonitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

// VNet networking (created for ServiceEndpoint and PrivateEndpoint).
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (useVnet) {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
  }
}

resource networkSecurityGroup 'Microsoft.Network/networkSecurityGroups@2023-11-01' = if (useVnet) {
  name: '${toLower(namePrefix)}-nsg'
  location: location
}

resource appSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = if (useVnet) {
  parent: vnet
  name: appSubnetName
  properties: {
    addressPrefix: cidrSubnet(vnetAddressPrefix, 26, 0)
    networkSecurityGroup: {
      id: networkSecurityGroup.id
    }
    serviceEndpoints: useServiceEndpoint
      ? [
          {
            service: 'Microsoft.Storage'
          }
        ]
      : []
    delegations: [
      {
        name: 'flex'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
  }
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = if (usePrivateEndpoint) {
  parent: vnet
  name: 'private-endpoints'
  properties: {
    addressPrefix: cidrSubnet(vnetAddressPrefix, 26, 1)
    networkSecurityGroup: {
      id: networkSecurityGroup.id
    }
  }
  // Subnets in one VNet must be written serially.
  dependsOn: [
    appSubnet
  ]
}

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = [
  for service in privateDnsZoneServices: if (usePrivateEndpoint) {
    name: 'privatelink.${service}.${storageSuffix}'
    location: 'global'
  }
]

resource privateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [
  for (service, i) in privateDnsZoneServices: if (usePrivateEndpoint) {
    parent: privateDnsZone[i]
    name: 'link'
    location: 'global'
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: vnet.id
      }
    }
  }
]

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = [
  for pe in privateEndpointConfigs: if (usePrivateEndpoint) {
    name: '${toLower(namePrefix)}-pe-${pe.key}'
    location: location
    properties: {
      subnet: {
        id: privateEndpointSubnet.id
      }
      privateLinkServiceConnections: [
        {
          name: pe.key
          properties: {
            privateLinkServiceId: pe.storageId
            groupIds: [
              pe.service
            ]
          }
        }
      ]
    }
  }
]

resource privateEndpointDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = [
  for (pe, i) in privateEndpointConfigs: if (usePrivateEndpoint) {
    parent: privateEndpoint[i]
    name: 'default'
    properties: {
      privateDnsZoneConfigs: [
        {
          name: pe.service
          properties: {
            privateDnsZoneId: resourceId('Microsoft.Network/privateDnsZones', 'privatelink.${pe.service}.${storageSuffix}')
          }
        }
      ]
    }
    dependsOn: [
      privateDnsZone
    ]
  }
]

var baseAppSettings = [
  {
    name: 'AzureWebJobsStorage__accountName'
    value: appStorageName
  }
  {
    name: 'AzureWebJobsStorage__credential'
    value: 'managedidentity'
  }
  {
    name: 'AzureWebJobsStorage__clientId'
    value: managedIdentity.properties.clientId
  }
  {
    name: 'AzureWebJobsStorage__blobServiceUri'
    value: appBlobUri
  }
  {
    name: 'AzureWebJobsStorage__queueServiceUri'
    value: appQueueUri
  }
  {
    name: 'AzureWebJobsStorage__tableServiceUri'
    value: appTableUri
  }
  {
    name: 'AzureDurableFunctionStorage__accountName'
    value: durableStorageName
  }
  {
    name: 'AzureDurableFunctionStorage__credential'
    value: 'managedidentity'
  }
  {
    name: 'AzureDurableFunctionStorage__clientId'
    value: managedIdentity.properties.clientId
  }
  {
    name: 'AzureDurableFunctionStorage__blobServiceUri'
    value: durableBlobUri
  }
  {
    name: 'AzureDurableFunctionStorage__queueServiceUri'
    value: durableQueueUri
  }
  {
    name: 'AzureDurableFunctionStorage__tableServiceUri'
    value: durableTableUri
  }
  {
    name: 'AzureWebJobsDisableHomepage'
    value: 'true'
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: managedIdentity.properties.clientId
  }
  {
    // Easy Auth (auth.bicep) uses this MI as the app registration's federated
    // credential for the token exchange; harmless when Easy Auth is off.
    name: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
    value: managedIdentity.properties.clientId
  }
  {
    name: 'SCOPE_SUBSCRIPTION_IDS'
    value: scope.subscriptionIds
  }
  {
    name: 'SCOPE_MANAGEMENT_GROUP_IDS'
    value: scope.managementGroupIds
  }
  {
    name: 'SCOPE_LOCATIONS'
    value: scope.locations
  }
  {
    name: 'COST_MANAGEMENT_METHOD'
    value: costManagement.method
  }
  {
    name: 'COST_MANAGEMENT_AGREEMENT_TYPE'
    value: costManagement.agreementType
  }
  {
    name: 'COST_MANAGEMENT_BILLING_ACCOUNT_ID'
    value: costManagement.billingAccountId
  }
  {
    name: 'COST_MANAGEMENT_BILLING_PROFILE_ID'
    value: costManagement.billingProfileId
  }
  {
    name: 'DATA_COLLECTION_DISPATCH_SCHEDULE'
    value: '0 */5 * * * *'
  }
]

var appInsightsSettings = deployApplicationInsights
  ? [
      {
        name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
        value: applicationInsights.?properties.ConnectionString ?? ''
      }
      {
        name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING'
        value: 'ClientId=${managedIdentity.properties.clientId};Authorization=AAD'
      }
    ]
  : []

resource appStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: appStorageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    isHnsEnabled: false
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
    }
    publicNetworkAccess: storagePublicNetworkAccess
    networkAcls: storageNetworkAcls
  }
}

// Durable task-hub storage. Hardened identically to the app-data account; if
// Durable orchestrations fail to start after deploy, relax hardening here first.
resource durableStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: durableStorageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    isHnsEnabled: false
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
    }
    publicNetworkAccess: storagePublicNetworkAccess
    networkAcls: storageNetworkAcls
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: appStorageAccount
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource zoneMappingsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: zoneMappingsContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource computeSkusContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: computeSkusContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource azureReferenceDataContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: azureReferenceDataContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource subscriptionContextContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: subscriptionContextContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${toLower(namePrefix)}-mi'
  location: location
}

// Identity-based access to the app-data account: Blob for the Functions host
// (deployment package, host keys, singleton leases) and Table for the app data.
resource appBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appStorageAccount
  name: guid(appStorageAccount.id, managedIdentity.id, roleBlobDataContributor)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleBlobDataContributor)
  }
}

resource appTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appStorageAccount
  name: guid(appStorageAccount.id, managedIdentity.id, roleTableDataContributor)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleTableDataContributor)
  }
}

// Identity-based access to the Durable Functions account (blob/queue/table).
resource durableBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: durableStorageAccount
  name: guid(durableStorageAccount.id, managedIdentity.id, roleBlobDataContributor)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleBlobDataContributor)
  }
}

resource durableQueueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: durableStorageAccount
  name: guid(durableStorageAccount.id, managedIdentity.id, roleQueueDataContributor)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleQueueDataContributor)
  }
}

resource durableTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: durableStorageAccount
  name: guid(durableStorageAccount.id, managedIdentity.id, roleTableDataContributor)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleTableDataContributor)
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (deployApplicationInsights) {
  name: '${toLower(namePrefix)}-law'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = if (deployApplicationInsights) {
  name: '${toLower(namePrefix)}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    DisableLocalAuth: true
  }
}

// Identity-based telemetry ingestion (paired with App Insights DisableLocalAuth).
resource appInsightsMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApplicationInsights) {
  scope: applicationInsights
  name: guid(applicationInsights.id, managedIdentity.id, roleMonitoringMetricsPublisher)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleMonitoringMetricsPublisher)
  }
}

resource hostingPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${toLower(namePrefix)}-plan'
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  kind: 'functionapp'
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  dependsOn: [
    appBlobContributor
    appTableContributor
    zoneMappingsContainer
    computeSkusContainer
    durableBlobContributor
    durableQueueContributor
    durableTableContributor
    privateEndpointDnsGroup
  ]
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    virtualNetworkSubnetId: useVnet ? appSubnet.id : null
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${appStorageAccount.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: managedIdentity.id
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
      scaleAndConcurrency: {
        alwaysReady: [
          {
            name: 'durable'
            instanceCount: 1
          }
        ]
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
        triggers: {
          http: {
            perInstanceConcurrency: 16
          }
        }
      }
    }
    siteConfig: {
      appSettings: concat(baseAppSettings, appInsightsSettings)
    }
  }
}

resource authSettings 'Microsoft.Web/sites/config@2024-04-01' = if (configureEasyAuth) {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
          clientId: appRegistrationClientId
          // Option A: points at the MI-client-ID app setting, not a secret.
          clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
        }
        login: {
          loginParameters: [
            'scope=openid profile offline_access ${environment().resourceManager}user_impersonation'
          ]
        }
        validation: {
          allowedAudiences: [
            'api://${appRegistrationClientId}'
          ]
          defaultAuthorizationPolicy: {
            allowedApplications: [
              appRegistrationClientId
            ]
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
        tokenRefreshExtensionHours: 72
      }
    }
  }
}

output result deploymentOutputs = {
  functionAppName: functionApp.name
  functionAppHostName: functionApp.properties.defaultHostName
  managedIdentityPrincipalId: managedIdentity.properties.principalId
  managedIdentityClientId: managedIdentity.properties.clientId
  easyAuthConfigured: configureEasyAuth
  nextSteps: configureEasyAuth
    ? 'Easy Auth configured. Ensure the app registration has the MI federated credential + ID token issuance (docs/operations/easy-auth-setup.md Steps 1-4), then assign Azure Capacity Backend Reader on the target subs/MG.'
    : 'Easy Auth NOT configured. Create the app registration (docs/operations/easy-auth-setup.md Steps 1-4) then deploy infra/auth.bicep. Also assign Azure Capacity Backend Reader on the target subs/MG.'
}
