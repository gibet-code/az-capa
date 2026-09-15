using './main.bicep'

param namePrefix = 'azcapacity'

param scope = {
  subscriptionIds: ''
  managementGroupIds: ''
  locations: ''
}

param costManagement = {
  method: 'per-sub'
  agreementType: 'ea'
  billingAccountId: ''
  billingProfileId: ''
}

param deployApplicationInsights = true

param storageConnectivity = 'PrivateEndpoint'

// Easy Auth: set to true and supply the existing app registration's client ID to
// configure Easy Auth in this deployment. Leave false to run infra/auth.bicep later
// (see docs/operations/easy-auth-setup.md).
param configureEasyAuth = false
param appRegistrationClientId = ''
