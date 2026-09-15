// Enables App Service Authentication (Easy Auth) on the deployed Function App.
// The app registration is authenticated with the Function App's user-assigned
// managed identity as a federated credential (no client secret). The MI-client-ID
// app setting it relies on (OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID) is declared in
// main.bicep. Deploy AFTER main.bicep and AFTER creating the app registration + MI
// federated credential per docs/operations/easy-auth-setup.md (Steps 1-4).

@description('Name of the Function App deployed by main.bicep (output functionAppName).')
param functionAppName string

@description('Application (client) ID of the Entra app registration created in Step 1.')
param appRegistrationClientId string

@description('Entra tenant ID used for the OpenID issuer.')
param tenantId string = tenant().tenantId

resource functionApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: functionAppName
}

resource authSettings 'Microsoft.Web/sites/config@2024-04-01' = {
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
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
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
