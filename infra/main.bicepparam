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

param connectivityProfile = 'Private'

param networkDeployment = 'Create'

param privateDnsManagement = 'Deploy'
