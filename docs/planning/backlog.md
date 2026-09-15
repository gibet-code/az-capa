# Backlog

## Status

Deferred planning. Items here are not current architecture or committed
implementation work.

Deferred work across the application. Completed behavior belongs in the relevant
architecture, contract, or operations document rather than in this backlog.

## App scope editing
Move scope configuration out of Function App environment settings and into a
settings table so it can be changed without restarting the Function App.

- Add a **Modify** button to the Scope card in App Settings.
- Open a right-side drawer with **Subscriptions** and **Locations** sections.
- Subscription choices: all accessible subscriptions (show the count), selected
  subscriptions, or selected management groups. Explain that the available scope
  is limited by the Function App managed identity permissions.
- Location choices: all locations or selected locations.
- Add Save and Cancel actions, persistence, validation, and pipeline handling for
  scope changes.

## 1. Resumability (partial-progress checkpointing)
The dataset-state record is persisted **only after a fully successful refresh**
(zero page failures), by `usage_write_state`. If a run is interrupted mid-way
(e.g. 4/22 subs done, or a stream partway through its pages), state is **not**
written, so the next refresh re-plans against the prior coverage window and
re-fetches every work unit (rows are overwritten idempotently, but the Cost
Management queries are repeated).

- Persist partial progress — e.g. advance `coverageEnd` (or record the completed
  work-units / subs) as each work unit finishes, instead of a single write at the
  end.
- On resume, have `resolve_plan` skip subs/days already covered so only the
  remaining work is queried.
- Keep the single final write for the common small-scope case where a whole
  refresh completes in one orchestration.

## Error propagation to users
Pipeline activities return useful per-work-unit errors, but several orchestrators
reduce them to failure counts before scheduler history and the UI receive the
result. Preserve bounded, sanitized failure details through the orchestration
result and run-history summary, and expose actionable messages in Data Collection
status/history without leaking tokens or oversized provider responses.

All-work-unit failures are classified as `failed`; `partial` is reserved for runs
with at least one successful and one failed work unit.

## 2. Cost Management throttling / scale
Empirically measured (2026-08): the binding limit is **request COUNT**, not QPU
and not response size. On a 429, QPU headers show plenty of budget
(`qpu-remaining: QueriesPerHour:588,...`) while `clienttype-requests-remaining:
DefaultQuota:0` with a `clienttype-retry-after` counting down ~30-60s. On a 200,
the QPU headers are absent entirely — so `qpu-consumed:1` seen earlier was just a
429-only placeholder, not a real per-query cost. A 1-day, 30-day-daily, and
30-day-monthly query all cost the **same one request** (verified: after one 38s
reset wait, all three succeeded back-to-back with no further 429s), differing
only in rows returned (13 / 405 / 39).

- Prefer `per-billing` for many subs under one billing profile (2 calls total,
  MG scope applied as a `SubscriptionId` filter).
- **Fetch the widest window in a single call.** Per-day chunking is
  counter-productive: it multiplies request count against the exact quota that
  binds. Keep `COST_MANAGEMENT_TIME_CHUNK_DAYS=0`.
- Backoff now honors `x-ms-ratelimit-microsoft.costmanagement-*-retry-after`
  (clienttype/entity/tenant) with +1s slack — done in `_post_query`.
- Monthly granularity is unusable for per-day uptime (returns `BillingMonth`
  buckets); always use `Daily` for multi-day windows.
- **Full-scope scale (done):** the retrieval is now a **per-page durable
  activity** (`usage_fetch_page`) driven by an orchestrator page loop. Each page
  gets a fresh ARM token (no mid-run 401 expiry), is upserted immediately (no
  all-or-nothing loss), and 429s are waited out with a durable timer (no ~5-min
  activity timeout). EA runs two streams (hours/cost) MERGE-upserted per page;
  MCA runs one. Serial by design (global limit).
- **TODO — `continue_as_new`:** for very large scopes the orchestration history
  grows one entry per page; add `continue_as_new` checkpointing (carry the
  current work-item index + nextLink) once page counts get large enough to
  bloat history. Not needed yet.
- **TODO — progress %:** parked. When added: cumulative rows / (RG VM count ×
  days), clamp ≤99%, snap to 100% at real end via `set_custom_status`.

## 3. Durable concurrency
`host.json` sets `maxConcurrentActivityFunctions: 2`, which caps real fan-out
parallelism in Azure. Revisit once the throttling strategy above is settled.

## 4. Locations / meter regions
The global `SCOPE_LOCATIONS` contract is ARM location codes (for example,
`westeurope`) so Resource Graph views can enforce it directly. VM Usage and
Capacity Reservation Usage currently pass those values verbatim to Cost
Management's `ResourceLocation` dimension, which uses billing meter-region
vocabulary (for example, `EU West`); ARM codes can match zero meter rows and
silently under-collect.

Decision (see [Application Scope](../specs/app-scope.md#location-scope-enforcement)):
location is a
**read-time** boundary enforced on each row's canonical ARM `location`, not a
collection-time gate. Required changes:

- Stop sending the location filter from `build_vm_filter` / `build_cr_filter` to
  Cost Management until a conversion exists; collect the location superset within
  the current subscription and billing boundary so nothing is under-collected.
- Drop `locations` from the usage refresh scope model: remove it from
  `usage_refresh._describe_scope`, from the persisted scope state, and from the
  `REFUSE_STRUCTURAL` set, so a `SCOPE_LOCATIONS` change no longer forces a
  flush-then-refresh and no longer shows as a Current-versus-Last difference.
- Enforce `SCOPE_LOCATIONS` on the read side of every location-bearing dataset
  (VM/CR usage and any other ARM-location-bearing rows), matching the
  subscription-boundary read enforcement. Reference/event datasets that carry no
  independent location dimension are exempt: Zone Mapping (region→zone lookup
  surfaced only via joins to already location-filtered rows) and Activity Log.
- Later, implement and test an ARM-location -> meter-region conversion as a pure
  collected-volume optimization; it must not change what a report exposes.

Until the read-side enforcement lands, a non-empty `SCOPE_LOCATIONS` is not
reliably applied to VM/CR usage, so leave it empty (All locations) for
deployments that depend on those pipelines.

## 5. Production configuration / secrets
For Azure, set these as App Settings (not `local.settings.json`), and reference
secrets (billing IDs, connection strings) via Key Vault instead of plain values.

## 6. Azure deployment (Bicep + "Deploy to Azure" button)
**Status: public-path Bicep done (`infra/main.bicep` + `infra/main.bicepparam`);
private path + button/auth automation still parked.** For dev, deploy with Core
Tools (`func azure functionapp publish <app>`) or run locally (`func start`).
Goal: public repo with a one-click **Deploy to Azure** button so anyone can run
their own instance.

**Done (public path, `infra/main.bicep`):** two storage accounts (host+app-data,
durable) wired with **managed identity / identity-based auth** — both accounts set
`allowSharedKeyAccess: false`; the app-data account exposes blob/queue/table via
`AzureWebJobsStorage__accountName`/`__credential=managedidentity`/`__clientId` +
service URIs, durable likewise via `AzureDurableFunctionStorage__*`. The MI is
granted Storage Blob + Table Data Contributor on the app-data account (Blob is
for the Functions host: deployment package, keys, leases — the app code only uses
Tables) and Blob/Queue/Table Data Contributor on the durable account; deployment
storage authenticates via `UserAssignedIdentity`. Locally the stores fall back to
the Azurite dev connection string (`is_running_locally()` gate in
`storage.table.table_service_client()`). Flex Consumption `FC1` plan; `sites`
kind `functionapp,linux` with `functionAppConfig` (python 3.12, `alwaysReady
durable instanceCount:1`); `AZURE_CLIENT_ID` set for the collectors' ARM access
(grant the MI **Reader** on the target scope manually — above the RG-scoped
template); App Insights + Log Analytics (optional); all `settings.py` App
Settings; `publicNetworkAccess: Enabled`, no VNet/private endpoints. Compiles
clean.

**Still parked below:**

Two planes (a button can only do the first):
- **Azure resources** via Bicep/ARM — the button (compile `main.bicep` →
  `azuredeploy.json` at a public raw URL; optional `createUiDefinition.json`).
- **Entra app registration + admin consent + Easy Auth wiring** — NOT ARM. See
  the manual guide [Easy Auth setup](../operations/easy-auth-setup.md)
  (automate later).

**Reusable from the reference Bicep** (FinOps disk-advisor `bicep/main.bicep`):
- **Flex Consumption:** `serverfarms` `FC1` `reserved:true`; `sites` kind
  `functionapp,linux` with `functionAppConfig`:
  - `deployment.storage` = `blobcontainer` authenticated via **user-assigned MI**,
  - `runtime` python 3.12,
  - `scaleAndConcurrency.alwaysReady: [{name:'durable', instanceCount:1}]` —
    **KEY for Durable** (keeps a warm instance so orchestrations dispatch),
    plus `instanceMemoryMB`, `maximumInstanceCount`, `http.perInstanceConcurrency`.
- **Secretless storage (done):** user-assigned MI +
  `AzureWebJobsStorage__accountName` / `__credential=managedidentity` (no shared
  keys, `allowSharedKeyAccess: false`); role assignments Storage **Blob + Table
  Data Contributor** on the app-data account (Blob for the Functions host,
  Table for the app data) and **Blob / Queue / Table Data Contributor** on the
  durable account. The pipeline stores build their `TableServiceClient` from
  `storage.table.table_service_client()`, which
  uses the MI credential in Azure and the Azurite dev connection string locally.
- **Easy Auth (`authsettingsV2`):** `tokenStore.enabled=true` +
  `tokenRefreshExtensionHours`. Reference app uses a **secretless app-reg** via a
  managed-identity **federated credential** (`clientSecretSettingName`
  → `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID`) instead of a client secret — prefer
  this over a stored secret. **MUST ADD** the ARM delegated login scope:
  `login.loginParameters: ['scope=openid profile offline_access https://management.azure.com/user_impersonation']`
  so `X-MS-TOKEN-AAD-ACCESS-TOKEN` is an ARM token.
- **Private/public:** `deploymentMode` param; VNet + subnet **delegated to
  `Microsoft.App/environments`** (required for Flex VNet integration); private
  endpoints for blob/queue/table/site; `publicNetworkAccess: Disabled` in private
  mode; `waitForOneMinute` deploymentScript to let role assignments propagate.
- **Code deploy in-template:** `sites/extensions` **`onedeploy`** with
  `packageUri` + `remoteBuild:true` (run-from a released zip). Alternatives:
  Deployment Center `sourcecontrols` (Oryx build from a fork) for true one-click,
  or GitHub Actions for day-2 updates.

**Fully-private deployments (customer constraint):** some customers **forbid
public outbound** from the VNet (no NAT/egress). Support a fully-private topology
and **let the user choose** public vs private. Caveat: the collectors call
`management.azure.com` (ARM/Resource Graph) — that outbound must be allowed via
egress or a supported private path; document the required outbound (there is no
Private Link for ARM), or gate features that need it. Private endpoints cover
blob/queue/table/site; ARM egress is the open question to design for.

## 7. Business Context scheduled-refresh notifications
Avoid filling Notification history with routine Business Context refreshes that
run every 15 minutes. Keep successful scheduled refreshes in status metadata,
telemetry, and logs rather than creating retained user notifications.

- Continue showing manually started Business Context Refresh and Flush
  operations in the Notifications pane and history.
- Show a scheduled refresh while it is actively running only if operational
  visibility is useful; do not retain it after routine success.
- Retain scheduled refreshes that fail, publish partial stale results, or leave
  Business Context stale so administrators can inspect their diagnostics.
- Keep field-level outcomes summarized under the refresh operation rather than
  creating one notification per Context field.

## 8. Cache provenance + L1-bypass on ODCR coverage/usage responses
The ODCR coverage (`/api/odcr/coverage`) and usage (`/api/odcr_usage/list`)
endpoints already emit a per-step `Server-Timing` header for troubleshooting.
Extend that diagnostics surface with cache provenance and an opt-in L1 bypass so
callers can see and control where each snapshot-backed step's data came from.

Only the four blob-snapshot-backed lookups support provenance; the Resource
Graph / ARM / Table steps are always live and the CPU steps have none:

- `app_scope` — `storage/reference_data_store.py` snapshot (L1 TTL fast-path /
  L2). Selected-subscriptions mode reads no snapshot (report `config`).
- `zone_mapping_lookup` — `storage/zone_mapping_store.py` snapshot (L1 etag / L2).
- `compute_sku_lookup` — `storage/compute_sku_store.py` snapshot (L1 etag / L2).
- Business-context part of `request_dimensions_enrichment` —
  `storage/subscription_context_store.py` snapshot (L1 etag / L2).

**Provenance (item 1):** classify each snapshot read as `l1` (in-memory, no blob
I/O — only `reference_data_store` has a TTL fast-path that avoids I/O),
`l2_revalidated` (etag `get_blob_properties` matched cached content, no
download), `l2_download` (full blob download + decode), or `absent`. The branch
points already exist in each store's `open_snapshot`/`_snapshot`; the work is
threading the classification up through the service wrappers (`resolve_app_scope`,
`zone_mapping.pipeline.get_mappings`, `compute_skus.pipeline.enrich_vm_rows`, and
the business-context enrichment) to a new response header (for example
`X-Cache-Provenance: app_scope=l1, zone_mapping_lookup=l2_download, ...`) without
disturbing the stores' many other internal callers.

**L1 bypass (item 2):** parse a request header (for example `Cache-Control:
no-cache` or `X-Cache-Bypass: l1`) and thread a `bypass_l1` flag down the same
call chain. In `reference_data_store` this skips the TTL short-circuit so the
etag check always runs; in the etag-only stores it must also skip the
etag-match short-circuit to force a fresh download. Caveat: "ignore L1" forces a
blob round-trip but still returns the same content when the published generation
is unchanged — it cannot bypass the published snapshot itself (only a pipeline
republish changes the data), and the already-live steps are unaffected.

Both parts share ~80% of the work (the same threading channel through the four
lookups); do them together. Choose a threading mechanism (return tuples vs. a
per-request context object) that does not change the stores' other callers.
