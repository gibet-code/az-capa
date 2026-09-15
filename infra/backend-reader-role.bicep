targetScope = 'managementGroup'

@description('Name of the custom role used by the Azure Capacity backend managed identity.')
param roleName string = 'Azure Capacity Backend Reader'

@description('Stable role definition ID. Keep this value unchanged when updating the role.')
param roleDefinitionId string = 'd6adfcee-0e85-4ba9-83ed-458e94144b8f'

resource backendReaderRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: roleDefinitionId
  properties: {
    roleName: roleName
    description: 'Reads Azure Capacity backend inventory, usage, Activity Log, Compute SKU, and location reference data.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Resources/subscriptions/read'
          'Microsoft.Management/managementGroups/read'
          'Microsoft.Compute/skus/read'
          'Microsoft.Resources/subscriptions/locations/read'
          'Microsoft.Insights/eventtypes/values/read'
          'Microsoft.CostManagement/query/read'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      managementGroup().id
    ]
  }
}

output roleDefinitionResourceId string = backendReaderRole.id
output roleDefinitionName string = backendReaderRole.name
