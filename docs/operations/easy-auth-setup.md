# Easy Auth setup

## Status

Current operational runbook.

This guide configures **App Service Authentication (Easy Auth)** on the deployed
Function App so that:

1. Users must sign in with their Entra ID (Azure AD) account to reach the app, and
2. Each request carries the **user's own ARM access token** in the
   `X-MS-TOKEN-AAD-ACCESS-TOKEN` header, which the backend forwards to Azure
   Resource Manager / Resource Graph. That makes user-scoped endpoints (e.g.
  `GET /api/odcr/coverage`) return only what the **signed-in user** can see — the same
   list as the Azure portal — instead of the app's managed identity view.

This configuration requires no On-Behalf-Of code and no client secret when the
managed-identity federated credential option below is used.

> Managed identity stays in charge of the background collectors (VM/CR usage,
> Activity Log, Zone Mapping). Easy Auth only adds the **user** identity used by
> the interactive `/api/*` read endpoints.

Do this once per deployed instance, after the Azure infrastructure deployment.
The Entra administrator creates the app registration, federated credential,
`User` / `Admin` app roles, and assignments manually in the portal. Entra
resources, role definitions, and assignments are intentionally never provisioned
by `infra/main.bicep`.

For the complete installation order and role handoffs, start with
[Install Azure Capacity in Azure](installation.md). This page is the detailed
authentication subprocedure.

See [Authorization](../specs/authorization.md) for the normative role, trust-boundary,
endpoint-policy, and frontend-capability contract. This document is the
deployment and operational procedure for that specification.

---

## Prerequisites

- The Function App is already deployed (call its host `https://<app>.azurewebsites.net`).
- You can create an **Entra app registration** and **grant admin consent** in the
  tenant (Application Administrator / Cloud Application Administrator, or Global
  Administrator). Admin consent is required for the ARM delegated permission.
- Azure CLI signed in to the right tenant: `az login --tenant <tenant-id>`.

Set some shell variables (bash):

```bash
APP_NAME="<function-app-name>"
RG="<resource-group>"
TENANT_ID="$(az account show --query tenantId -o tsv)"
HOST="https://${APP_NAME}.azurewebsites.net"
```

---

## Step 1 — Create the app registration

Portal: **Entra ID → App registrations → New registration**.

- **Name:** e.g. `az-capacity-app-reg`.
- **Supported account types:** *Single tenant only* (Accounts in this
  organizational directory only).
- **Redirect URI:** in the platform dropdown pick **Web** (NOT
  *Single-page application (SPA)*), value:
  `https://<app>.azurewebsites.net/.auth/login/aad/callback`

  > The portal defaults this dropdown to **SPA** — you must switch it to **Web**.
  > Easy Auth redeems the auth code server-side (confidential client), which a SPA
  > redirect URI rejects with `AADSTS9002326` ("Cross-origin token redemption is
  > permitted only for the 'Single-Page Application' client-type").
- Register, then copy the **Application (client) ID**.
- **Enable ID tokens:** in the registration, go to **Authentication → Settings
  tab → Implicit grant and hybrid flows** and check **ID tokens (used for implicit
  and hybrid flows)** → **Save**. Easy Auth uses the hybrid flow; without this you
  get `AADSTS700054: response_type 'id_token' is not enabled for the application`.

CLI equivalent:

```bash
APP_ID=$(az ad app create \
  --display-name "az-capacity-${APP_NAME}" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "${HOST}/.auth/login/aad/callback" \
  --enable-id-token-issuance true \
  --query appId -o tsv)
az ad sp create --id "$APP_ID"
echo "App (client) ID: $APP_ID"
```

## Step 2 — Set the Application ID URI (audience)

Easy Auth validates the app's own token audience. Give the app an identifier URI.

Portal: **App registration → Expose an API → Application ID URI → Add** →
accept the prefilled default `api://<appId>` and save.

CLI:

```bash
az ad app update --id "$APP_ID" --identifier-uris "api://$APP_ID"
```

`api://$APP_ID` is the token audience for this application.

## Step 3 — Add the ARM delegated permission + admin consent

This is what lets the user token call Azure Resource Manager on the user's behalf.

Portal: **App registration → API permissions → Add a permission →
Azure Service Management → Delegated permissions → `user_impersonation`**
("Access Azure Resource Manager as organization users") → **Add permissions**.
Then **Grant admin consent for &lt;tenant&gt;**.

> This tenant may show **Admin consent required: No** for `user_impersonation`.
> Granting admin consent anyway is harmless and avoids a per-user consent prompt
> on first sign-in.

CLI (IDs are well-known and identical in every tenant):

```bash
# Azure Service Management API = 797f4846-ba00-4fd7-ba43-dac1f8f63013 # gitleaks:allow
# user_impersonation scope     = 41094075-9dad-400e-a0bd-54e686782033
az ad app permission add --id "$APP_ID" \
  --api 797f4846-ba00-4fd7-ba43-dac1f8f63013 \
  --api-permissions 41094075-9dad-400e-a0bd-54e686782033=Scope

az ad app permission admin-consent --id "$APP_ID"
```

## Step 4 — Define app roles and assign users/groups

App roles are defined manually on the app registration. Their **Value** is the
exact, case-sensitive string that Easy Auth places in the authenticated client
principal and that application authorization checks.

Portal: **Entra ID → App registrations → &lt;this application&gt; → App roles →
Create app role**. Create both enabled roles:

| Display name | Allowed member types | Value | Description | Enable this app role |
|---|---|---|---|---|
| User | Users/Groups | `User` | Use ODCR reports and edit visible VM coverage decisions. | Yes (checked) |
| Admin | Users/Groups | `Admin` | Use reports and administer Settings, Business Context, collection pipelines, and Notifications. | Yes (checked) |

Keep the generated role IDs stable. Do not delete and recreate roles during a
normal redeployment: assignments refer to those IDs. Entra app roles do not
inherit from one another, but the application treats `Admin` as satisfying its
User policy, so administrators need only the `Admin` assignment.

Immediately after creating the roles, configure assignments on the app
registration's **enterprise application** (service principal):

1. Portal: **Entra ID → Enterprise applications → &lt;this application&gt; → Users
  and groups → Add user/group**.
2. Assign at least one administrator to `Admin` and assign the intended report
  users to `User`. A security group can be assigned where tenant licensing
  permits it; nested group membership does not cascade.
3. Go to **Enterprise application → Properties → Assignment required? → Yes →
  Save**.

> Role authorization uses Easy Auth's Base64-encoded
> `X-MS-CLIENT-PRINCIPAL` header (`role_typ` and `claims`). The separate
> `X-MS-TOKEN-AAD-ACCESS-TOKEN` is an ARM-audience token used for Resource
> Manager / Resource Graph calls and must not be used to determine app roles.

## Step 5 — Add the managed identity federated credential (secretless)

Authenticate the app registration with the Function App's **user-assigned managed
identity** as a federated credential, so **no client secret** is stored. Easy Auth
exchanges the MI token for the app's token at the callback.

Portal: **App registration → Certificates & secrets → Federated credentials →
Add credential**:

- **Federated credential scenario:** *Managed Identity*.
- **Select managed identity:** click *Select a managed identity* → pick the
  deployment subscription → *User-assigned managed identity* → select the one the
  deployment created (default name `azcapacity-mi`) → **Select**.
- **Issuer / Subject identifier:** leave auto-filled (issuer
  `https://login.microsoftonline.com/<tenantId>/v2.0`; subject = the MI's object ID).
- **Name:** any descriptive, immutable value, e.g. `azcapacity-mi-fic`.
- **Audience:** leave the default **`api://AzureADTokenExchange`** — do not change it.
- **Add**.

## Step 6 — Return the application IDs

Give the Azure infrastructure operator the following non-secret values:

- Application (client) ID
- Entra tenant ID

The Entra configuration is complete.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/odcr/coverage` returns 401 | No forwarded user token (token store off, or ARM scope missing) | Re-deploy `infra/auth.bicep`; sign out/in |
| Empty VM list for a user who has access | Admin consent not granted, or wrong login scope | Step 3 admin consent; verify `loginParameters` scope |
| `X-MS-TOKEN-AAD-ACCESS-TOKEN` absent in `.auth/me` | `tokenStore.enabled` false | Set it true in `authsettingsV2` |
| Forwarded token expired after ~1h | No `offline_access` / no refresh | Add `offline_access` to the scope; `tokenRefreshExtensionHours` set |
| Sign-in loops / audience error | `allowedAudiences` / `clientId` mismatch | Ensure `api://<appId>` and `clientId` match the registration |
| `AADSTS700054: response_type 'id_token' is not enabled` | ID token issuance off on the app registration | Enable **ID tokens** (Step 1) or `az ad app update --id <appId> --enable-id-token-issuance true` |
| `AADSTS50105` / user is not assigned | Assignment is required and the user/group has no app-role assignment | Assign `User` or `Admin` in the enterprise application; sign out/in |
| Signed-in user receives HTTP 403 from app APIs | Principal has no recognized `User`/`Admin` role, or has a stale Easy Auth session | Correct the enterprise-app assignment; sign out/in |
| `Admin` cannot open a User report | Application User policy failed to treat `Admin` as satisfying User access | Verify the deployed authorization policy accepts either role for User endpoints |
