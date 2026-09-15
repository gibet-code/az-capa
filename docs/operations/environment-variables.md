# Environment variable audit

## Status

Current reference. Last verified against the Python settings, Azure Functions
bindings, and Bicep configuration on 2026-09-15.

This inventory covers first-party Python environment reads, Azure Functions binding
expressions, and settings supplied to the deployed Function App by
`infra/main.bicep`. It does not include environment variables read internally by
third-party packages.

`Not shipped` means the application intentionally uses the code default. `Removed`
means the setting was present in Bicep before this audit and was removed because its
value exactly duplicated the code default. Parameter-backed settings remain in
Bicep because they carry deployment configuration even when their parameter default
matches the code default.

## Application configuration

| Environment variable | Default in code when absent or empty | Bicep value after audit | Status |
|---|---|---|---|
| `SCOPE_SUBSCRIPTION_IDS` | `[]` | `scope.subscriptionIds` | Kept: deployment parameter |
| `SCOPE_MANAGEMENT_GROUP_IDS` | `[]` | `scope.managementGroupIds` | Kept: deployment parameter |
| `SCOPE_LOCATIONS` | `[]` | `scope.locations` | Kept: deployment parameter |
| `COST_MANAGEMENT_METHOD` | `per-sub` | `costManagement.method` | Kept: deployment parameter |
| `COST_MANAGEMENT_AGREEMENT_TYPE` | `ea` | `costManagement.agreementType` | Kept: deployment parameter |
| `COST_MANAGEMENT_BILLING_ACCOUNT_ID` | `None` | `costManagement.billingAccountId` | Kept: required for per-billing deployments |
| `COST_MANAGEMENT_BILLING_PROFILE_ID` | `None` | `costManagement.billingProfileId` | Kept: required for MCA per-billing deployments |
| `COST_MANAGEMENT_MAX_CONCURRENCY` | `8` | Removed (`8`) | Redundant default |
| `COST_MANAGEMENT_TIME_CHUNK_DAYS` | `0` | Removed (`0`) | Redundant default |
| `COST_FINALIZATION_LAG_DAYS` | `2` | Removed (`2`) | Redundant default |
| `USAGE_HISTORICAL_DAYS` | `30` | Removed (`30`) | Redundant default |
| `VM_USAGE_TABLE_NAME` | `VmDailyUsage` | Not shipped | Code default |
| `VM_USAGE_CHECKPOINT_TABLE_NAME` | `VmUsageCheckpoints` | Not shipped | Code default |
| `CR_USAGE_TABLE_NAME` | `CrDailyUsage` | Not shipped | Code default |
| `CR_USAGE_CHECKPOINT_TABLE_NAME` | `CrUsageCheckpoints` | Not shipped | Code default |
| `CR_USAGE_SUMMARY_TABLE_NAME` | `CrUsageSummaries` | Not shipped | Code default |
| `ACTIVITY_LOG_HISTORICAL_DAYS` | `30`, clamped to 1-89 | Not shipped | Code default |
| `ACTIVITY_LOG_REFRESH_OVERLAP_HOURS` | `24`, minimum 0 | Not shipped | Code default |
| `ACTIVITY_LOG_INGESTION_LAG_MINUTES` | `20`, minimum 0 | Not shipped | Code default |
| `ACTIVITY_LOG_MAX_CONCURRENCY` | `10`, minimum 1 | Not shipped | Code default |
| `ACTIVITY_LOG_REPLAY_MAX_UNITS_PER_RUN` | `200`, minimum 0 | Not shipped | Code default |
| `ACTIVITY_LOG_PENDING_UNKNOWN_HOURS` | `48`, minimum 0 | Not shipped | Code default |
| `ACTIVITY_LOG_OPERATIONS_TABLE_NAME` | `ActivityLogOperations` | Not shipped | Code default |
| `ACTIVITY_LOG_COLLECTION_STATE_TABLE_NAME` | `ActivityLogCollectionState` | Not shipped | Code default |
| `ACTIVITY_LOG_SPILL_CONTAINER_NAME` | `activity-log-spill` | Not shipped | Code default |
| `ZONE_MAPPING_MAX_CONCURRENCY` | `15`, minimum 1 | Not shipped | Code default |
| `ZONE_MAPPING_BLOB_CONTAINER` | `zone-mappings` | Removed; Bicep provisions `zone-mappings` | Redundant default |
| `COMPUTE_SKU_BLOB_CONTAINER` | `compute-skus` | Removed; Bicep provisions `compute-skus` | Redundant default |
| `AZURE_REFERENCE_DATA_BLOB_CONTAINER` | `azure-reference-data` | Removed; Bicep provisions `azure-reference-data` | Redundant default |
| `AZURE_REFERENCE_DATA_L1_TTL_SECONDS` | `60`, minimum 1 | Removed (`60`) | Redundant default |
| `SUBSCRIPTION_REFERENCE_MAX_STALE_MINUTES` | `60`, minimum 1 | Removed (`60`) | Redundant default |
| `LOCATION_REFERENCE_MAX_STALE_HOURS` | `48`, minimum 1 | Removed (`48`) | Redundant default |
| `ODCR_COVERAGE_DECISIONS_TABLE_NAME` | `OdcrCoverageDecisions` | Not shipped | Code default |
| `SUBSCRIPTION_CONTEXT_MAX_FIELDS` | `5`, clamped to 1-20 | Not shipped | Code default |
| `SUBSCRIPTION_CONTEXT_MAX_UPLOAD_BYTES` | `10485760`, minimum 1 | Not shipped | Code default |
| `SUBSCRIPTION_CONTEXT_ADMIN_ROLE` | `BusinessContext.Admin` | Not shipped | Code default |
| `SUBSCRIPTION_CONTEXT_TABLE_NAME` | `SubscriptionContextFields` | Not shipped | Code default |
| `SUBSCRIPTION_CONTEXT_BLOB_CONTAINER` | `subscription-context` | Removed; Bicep provisions `subscription-context` | Redundant default |
| `SUBSCRIPTION_CONTEXT_REFRESH_LEASE_SECONDS` | `60`, clamped to 15-60 | Not shipped | Code default |
| `SUBSCRIPTION_CONTEXT_MAX_STALE_MINUTES` | `20`, minimum 1 | Removed (`20`) | Redundant default |
| `DATA_COLLECTION_DISPATCH_SCHEDULE` | `0 */5 * * * *` in an otherwise unused Python accessor | `0 */5 * * * *` | Kept: `%...%` is resolved by the Functions host before Python runs |
| `DATA_COLLECTION_HISTORY_RETENTION_DAYS` | `30`, minimum 1 | Removed (`30`) | Redundant default |
| `DATA_COLLECTION_MAX_DISPATCH_PER_TICK` | `0` (unlimited) | Removed (`0`) | Redundant default |
| `DATA_COLLECTION_MIN_FREQUENCY_SECONDS` | `300`, minimum 1 | Removed (`300`) | Redundant default |
| `DATA_COLLECTION_MAX_FREQUENCY_SECONDS` | `2592000` | Removed (`2592000`) | Redundant default |
| `SCHEDULER_RUN_STATE_TABLE_NAME` | `SchedulerRunState` | Removed (`SchedulerRunState`) | Redundant default |
| `SCHEDULER_RUN_HISTORY_TABLE_NAME` | `SchedulerRunHistory` | Removed (`SchedulerRunHistory`) | Redundant default |
| `SCHEDULER_CONFIG_TABLE_NAME` | `SchedulerConfig` | Removed (`SchedulerConfig`) | Redundant default |

## Storage and identity reads

| Environment variable | Default or fallback in code | Bicep value after audit | Status |
|---|---|---|---|
| `AzureWebJobsStorage` | No production default; `UseDevelopmentStorage=true` selects local mode | Not shipped as a connection string; production uses the identity-based `AzureWebJobsStorage__*` settings below | Required locally; host-required connection name |
| `AZURE_CLIENT_ID` | `None` | `managedIdentity.properties.clientId` | Required to select the user-assigned identity |
| `LOCAL_AUTH_TYPE` | `AzureCliCredential` | Not shipped | Local development only |
| `LOCAL_TENANT_ID` | `None` | Not shipped | Local client-secret authentication only |
| `LOCAL_CLIENT_ID` | `None` | Not shipped | Local client-secret authentication only |
| `LOCAL_CLIENT_SECRET` | `None` | Not shipped | Local client-secret authentication only |
| `BLOB_STORAGE_ENDPOINT` | Falls back to `AzureWebJobsStorage__blobServiceUri`, then account name plus suffix | Not shipped | Optional explicit override |
| `TABLE_STORAGE_ENDPOINT` | Falls back to `AzureWebJobsStorage__tableServiceUri`, then account name plus suffix | Not shipped | Optional explicit override |
| `STORAGE_ENDPOINT_SUFFIX` | `core.windows.net` | Not shipped | Optional sovereign-cloud fallback |
| `AzureWebJobsStorage__accountName` | Required if no explicit service URI is available | `appStorageName` | Required by host and code fallback |
| `AzureWebJobsStorage__blobServiceUri` | No default | `appBlobUri` | Required endpoint |
| `AzureWebJobsStorage__tableServiceUri` | No default | `appTableUri` | Required endpoint |

## Platform-consumed Bicep settings

These settings have no first-party Python accessor, but they are consumed by Azure
Functions, Durable Functions, Easy Auth, or Application Insights. They are not
orphans.

| Environment variable | Bicep value | Consumer |
|---|---|---|
| `AzureWebJobsStorage__credential` | `managedidentity` | Functions host storage authentication |
| `AzureWebJobsStorage__clientId` | `managedIdentity.properties.clientId` | Functions host user-assigned identity selection |
| `AzureWebJobsStorage__queueServiceUri` | `appQueueUri` | Functions host storage endpoint |
| `AzureDurableFunctionStorage__accountName` | `durableStorageName` | Durable Functions connection named by `host.json` |
| `AzureDurableFunctionStorage__credential` | `managedidentity` | Durable Functions storage authentication |
| `AzureDurableFunctionStorage__clientId` | `managedIdentity.properties.clientId` | Durable Functions user-assigned identity selection |
| `AzureDurableFunctionStorage__blobServiceUri` | `durableBlobUri` | Durable Functions blob endpoint |
| `AzureDurableFunctionStorage__queueServiceUri` | `durableQueueUri` | Durable Functions queue endpoint |
| `AzureDurableFunctionStorage__tableServiceUri` | `durableTableUri` | Durable Functions table endpoint |
| `AzureWebJobsDisableHomepage` | `true` | Functions host; allows the SPA fallback to own `/` |
| `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` | `managedIdentity.properties.clientId` | Easy Auth federated token exchange |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Deployed Application Insights connection string | Application Insights, only when enabled |
| `APPLICATIONINSIGHTS_AUTHENTICATION_STRING` | `ClientId=<managed identity client ID>;Authorization=AAD` | Identity-based Application Insights ingestion, only when enabled |

`FUNCTIONS_WORKER_RUNTIME` and `AzureWebJobsSecretStorageType` are local Functions
host settings. Flex Consumption receives its Python runtime from
`functionAppConfig.runtime`, so Bicep does not ship either as an app setting.

## Orphan findings

No Bicep app setting is orphaned after this audit. Every retained setting is consumed
by first-party code, an Azure Functions binding, or an Azure platform feature.

The `data_collection_dispatch_schedule()` Python accessor currently has no
production caller. The environment variable itself is not orphaned: the timer
binding consumes it as `%DATA_COLLECTION_DISPATCH_SCHEDULE%`. The accessor is left
in place because removing code APIs was outside this deployment-setting cleanup.

Local settings may contain profile-specific or legacy keys that are outside the
Bicep deployment. They should be audited separately without publishing their values,
especially because local profiles can contain credentials.
