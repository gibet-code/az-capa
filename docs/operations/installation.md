# Install Azure Capacity in Azure

## Purpose

This runbook installs a new Azure Capacity instance from a prebuilt release ZIP
stored in a private GitHub repository.

The installation deliberately separates responsibilities:

| Role | Responsibility |
|---|---|
| Azure infrastructure operator | Resource group, `infra/main.bicep`, custom Azure role, managed-identity assignment, `infra/auth.bicep`, and package deployment |
| Entra administrator | App registration, delegated permission consent, app roles, federated credential, and user/group assignments |
| GitHub user | Download the release ZIP from the private repository to the deployment workstation |

One person may hold more than one role, but complete the phases in the order
shown. The main infrastructure deployment does not create or configure Entra
resources.

## 1. Prerequisites

Collect the following before starting:

- An Azure subscription and an existing or new resource group.
- A globally unique lowercase name prefix, 3 to 17 characters long.
- The application scope you want the application to work on, it can be : all accessible to the application identity, or a subscription ID list or a management group IDs list, plus optional
  Azure location filters.
- The cost-management method and agreement details described in
  [Environment variables](environment-variables.md).
- Azure CLI and Bicep installed on the Azure operator's workstation.
- Permission to deploy resources and role assignments in the resource group.
- Permission to create a custom role at the target management group, such as
  **Role Based Access Control Administrator** with role-definition permissions.
- An Entra administrator who can create app registrations and grant tenant-wide
  consent. The detailed requirements are in [Easy Auth setup](easy-auth-setup.md).
- Access to the private GitHub repository's Releases page.

From the repository root, sign in and select the deployment subscription:

```powershell
az login --tenant <tenant-id>
az account set --subscription <subscription-id>
```

## 2. Download the release artifact

This is a manual step because the GitHub repository is private and the artifact
must not be referenced from Bicep.

1. In a browser, open the private GitHub repository.
2. Select **Releases**, then open the approved release.
3. Under **Assets**, download `az-capacity-<version-or-commit>.zip` to the Azure
   operator's workstation, for example under `C:\Downloads`.
4. Do not extract or rebuild the ZIP. Retain the release version and checksum
   supplied with the release for the deployment record.

## 3. Deploy the Azure infrastructure

Copy `infra/main.bicepparam` to an environment-specific parameter file and set at
least `namePrefix`, `scope`, `costManagement`, and `storageConnectivity`. Do not
add Entra application IDs: Easy Auth is a separate deployment phase.

Create the resource group and deploy the template:

```powershell
$ResourceGroup = '<resource-group>'
$Location = '<azure-region>'

az group create --name $ResourceGroup --location $Location

$Deployment = az deployment group create `
  --resource-group $ResourceGroup `
  --name azcapacity-infra `
  --template-file infra/main.bicep `
  --parameters infra/main.bicepparam `
  --query properties.outputs.result.value `
  --output json | ConvertFrom-Json

$FunctionAppName = $Deployment.functionAppName
$ManagedIdentityPrincipalId = $Deployment.managedIdentityPrincipalId
$ManagedIdentityClientId = $Deployment.managedIdentityClientId

$Deployment | Format-List
```

Record all three output values. The Entra administrator needs the Function App
name and managed-identity details for authentication setup.

## 4. Create and assign the custom Azure role

The backend collectors use the Function App's managed identity. Deploy the
least-privilege role definition at the management group that contains the target
subscriptions:

```powershell
$ManagementGroupId = '<management-group-id>'

az deployment mg create `
  --management-group-id $ManagementGroupId `
  --location $Location `
  --name azcapacity-backend-reader-role `
  --template-file infra/backend-reader-role.bicep

az role assignment create `
  --assignee-object-id $ManagedIdentityPrincipalId `
  --assignee-principal-type ServicePrincipal `
  --role 'Azure Capacity Backend Reader' `
  --scope "/providers/Microsoft.Management/managementGroups/$ManagementGroupId"
```

Assign at individual descendant subscriptions instead when the identity must not
read the entire management group.

## 5. Configure Entra and Easy Auth

Send the Entra administrator these non-secret values:

- Function App name: `$FunctionAppName`
- Function App URL: `https://$FunctionAppName.azurewebsites.net`
- User-assigned managed identity client ID: `$ManagedIdentityClientId`
- User-assigned managed identity principal/object ID:
  `$ManagedIdentityPrincipalId`

The Entra administrator completes
[Easy Auth setup](easy-auth-setup.md), then returns the Application (client) ID
and tenant ID to the Azure operator.

The Azure operator then updates `infra/auth.bicepparam` with the Function App
name and returned application ID and deploys the separate Easy Auth template:

```powershell
az deployment group create `
  --resource-group $ResourceGroup `
  --name azcapacity-auth `
  --template-file infra/auth.bicep `
  --parameters infra/auth.bicepparam
```

This template configures only the Function App's `authsettingsV2`. It does not
create or modify the Entra app registration.

## 6. Deploy the application ZIP

Follow the **Deploy** section of
[Function Package Build and Deployment](function-package-deployment.md). Use the
ZIP downloaded in Step 2 and keep remote build disabled.

## 7. Verify the installation

1. Restart the Function App in the Azure portal.
2. Browse to `https://<function-app-name>.azurewebsites.net` and sign in with the
   administrator account assigned the `Admin` role. Confirm the application opens
   and the account menu shows the signed-in administrator.
3. From the account menu, select **Log Out**. Confirm the session ends and the
   application requires Entra sign-in again.
4. Sign back in with the same administrator account.
5. Open **Settings → Data Collection**. Start the initial collection for each
   enabled data source and wait for it to complete. Confirm the status and last
   update information show successful collection for the configured scope.
6. Open **ODCR Coverage** and **ODCR Usage** and confirm both reports load.

> ODCR reports inherit the Azure scope visible to the signed-in user. The listed
> resources should therefore be limited to resources that the administrator can
> see in Azure.

The installation is complete when authentication and role checks pass, the app
scope is correct, and a collection can access the intended Azure subscriptions.