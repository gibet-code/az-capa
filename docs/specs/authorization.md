# Authorization

## Status

Current implemented contract. Last verified on 2026-08-31.

The application uses Entra app roles enforced by the Azure Functions HTTP
adapter. Easy Auth authenticates requests and supplies the trusted application
principal; Azure RBAC independently trims user-scoped Azure queries.

## Roles and capabilities

The app registration defines two roles with stable IDs and
`allowedMemberTypes: ['User']`:

| Role | Application capabilities |
|---|---|
| `User` | Use ODCR coverage and usage reports and update shared coverage decisions for VMs that pass App Scope and user-visibility checks. |
| `Admin` | Everything available to `User`, plus Settings, Business Context administration, pipeline status/refresh/flush operations, and Notifications. |

Entra app roles do not inherit. The application policy deliberately treats
`Admin` as satisfying the User policy, so administrators need only the `Admin`
assignment.

## Trust boundaries

Application roles come only from Easy Auth's Base64-encoded
`X-MS-CLIENT-PRINCIPAL` header. The parser accepts the claim type advertised by
`role_typ`, the observed Easy Auth `roles` claim, and the standard role claim
URI. A missing or malformed principal returns HTTP 401. An authenticated
principal without a required role returns HTTP 403.

`X-MS-TOKEN-AAD-ACCESS-TOKEN` is a separate ARM-audience provider token. It is
used only for calls made as the signed-in user and is never decoded to authorize
an application operation. App roles cannot expand Azure RBAC, and Azure RBAC
does not grant application capabilities.

The effective ODCR report population remains:

```text
Application Scope AND signed-in user visibility AND Global Filters
```

## Backend policy

The Functions adapter owns principal parsing, HTTP error responses, and the two
explicit guards in `functions/_authorization.py`:

- `require_user` accepts `User` or `Admin`.
- `require_admin` accepts only `Admin`.

Every guard runs before Durable status queries or starts, storage access,
history purge, and Azure calls. HTTP routes use the following policy:

| Surface | Policy |
|---|---|
| Static SPA and `/api/me` | Easy Auth authentication; `/api/me` returns normalized identity and capabilities. |
| `/api/me/role` | Local-only; returns 404 in Azure. |
| ODCR scope, filter catalogue, coverage, usage, and coverage-decision routes | User policy. |
| Settings and App Scope diagnostics | Admin policy. |
| Pipeline status, refresh, and flush routes | Admin policy. |
| Notifications GET and DELETE routes | Admin policy. |
| Business Context administration routes | Admin policy. |
| Timer triggers, orchestrators, and activities | Internal; no interactive role check. |

Reports may consume published Business Context enrichment without exposing its
administration APIs to `User`.

## Backend managed identity

In Azure, scheduled collectors and other backend operations use the Function
App's managed identity. The least-privilege custom role is defined in
`infra/backend-reader-role.bicep` and contains only these control-plane actions:

| Action | Backend use |
|---|---|
| `Microsoft.Resources/subscriptions/read` | See subscription containers returned by Resource Graph. |
| `Microsoft.Management/managementGroups/read` | See management-group containers returned by Resource Graph. |
| `Microsoft.Compute/skus/read` | Collect the Compute SKU catalogue. |
| `Microsoft.Resources/subscriptions/locations/read` | Collect subscription location and availability-zone metadata. |
| `Microsoft.Insights/eventtypes/values/read` | Collect subscription Activity Log events. |
| `Microsoft.CostManagement/query/read` | Query VM and capacity-reservation usage in subscription-scoped `per-sub` mode. |

Resource Graph does not require a separate query-endpoint permission. It
returns only objects for which the managed identity has the corresponding read
access; the subscription and management-group reads above provide that access
for the backend inventory queries.

The Cost Management action applies only to subscription-scoped `per-sub`
queries. Billing-account and billing-profile queries in `per-billing` mode still
require separate billing authorization. The role excludes VM, capacity
reservation, and ODCR reads because those requests run with the signed-in user's
ARM token. Storage data-plane and telemetry role assignments are app-local and
remain in `infra/main.bicep`.

Deploy the role at the management group that contains every target scope, then
assign it to the managed identity at that management group or at individual
descendant subscriptions. The deployment principal needs permission to create
custom role definitions at the selected management group.

```powershell
az deployment mg create `
	--management-group-id <management-group-id> `
	--location <deployment-location> `
	--template-file infra/backend-reader-role.bicep

az role assignment create `
	--assignee-object-id <managed-identity-principal-id> `
	--assignee-principal-type ServicePrincipal `
	--role 'Azure Capacity Backend Reader' `
	--scope /providers/Microsoft.Management/managementGroups/<management-group-id>
```

## Frontend contract

`/api/me` is the single normalized identity and capability contract in Azure and
locally. The SPA resolves it before loading protected data:

- `User` sees ODCR reports only.
- `Admin` also sees Settings and Notifications.
- A principal with neither role sees the denied state.

The SPA does not call or poll hidden Admin APIs and redirects manually entered
Admin routes to ODCR for non-admin users. Backend guards remain authoritative.

Local development uses an HttpOnly, SameSite=Lax role cookie with `Admin`,
`User`, and `None` contexts. It defaults to `Admin`. The cookie is ignored in
Azure, and the Azure CLI remains the local ARM identity.

## Provisioning and rollout

App-role definitions and user/group assignments belong to the app registration
and enterprise application; Bicep does not own them. The enterprise application
must have **Assignment required** enabled after a working administrator is
assigned and verified. Existing sessions must sign out and back in after role
assignment changes.

The complete provisioning and operational verification procedure is in
[Easy Auth setup](../operations/easy-auth-setup.md).

## Verification matrix

| Context | Expected result |
|---|---|
| Missing or malformed principal | HTTP 401 from application APIs. |
| No role or unknown role | HTTP 403 from protected APIs. |
| `User` | ODCR reads and coverage-decision writes succeed; Admin APIs return 403. |
| `Admin` | User and Admin surfaces succeed. |
| Unassigned tenant user | Entra rejects sign-in when Assignment required is enabled. |

Unauthorized Durable starters must not query the orchestration gate or start
work. Unauthorized notification calls must not query or purge history.