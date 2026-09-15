# Application Scope

## Status

Proposed behavior for review as of 2026-08-30. A startup-warmed Subscription
snapshot with a retryable initializing response, cross-worker last-writer-wins
publication, relaxed handling of backend identity changes, continued use of
stale generations, removal of Subscription and Location flush operations, strict
scope-only Business Context output, and environment-only App Scope configuration
are approved.

An empty effective App Scope is now treated as a fatal configuration error, not
a valid fail-closed value: enforcement resolution raises and every data-serving
path returns a clear, admin-facing message. This is distinct from a non-empty
scope that legitimately yields zero rows (for example, a user-token report whose
signed-in caller can access none of the in-scope resources), which remains a
normal `200` result. The remaining presentation and error-shape decisions are
now resolved; see [Decisions Before Implementation](#decisions-before-implementation).

Azure Reference Data owns catalogue collection, snapshot persistence,
publication, freshness reporting, schedules, and presentation fallbacks. Those
contracts are defined in [Azure Reference Data](azure-reference-data.md) and
are not repeated here.

## Purpose

Application Scope is the administrator-owned boundary used by report reads,
user-authored mutations, and data-collection pipelines. It limits the Azure
subscriptions and locations that the application may expose or target and that
collection pipelines should normally process.

The security boundary is enforced at every user-facing read and mutation.
Collection pipelines should also narrow Azure requests to App Scope to avoid
unnecessary query cost and persistence, but collecting inaccessible surplus
enrichment data is not by itself an authorization violation when there is no
raw-data API and every read joins or partitions that data through the current
App Scope. Such surplus data must never affect user-visible rows, aggregates,
counts, options, exports, or metadata.

Application Scope is distinct from signed-in user visibility and Global
Filters:

```text
report population
  = application scope
  AND signed-in user visibility
  AND Global Filters
```

Application Scope never expands Azure RBAC. Global Filters only narrow the
application scope.

## Configuration

Application Scope reads:

```text
SCOPE_SUBSCRIPTION_IDS
SCOPE_MANAGEMENT_GROUP_IDS
SCOPE_LOCATIONS
```

These Function App environment variables are the sole persisted App Scope
configuration. `SCOPE_SUBSCRIPTION_IDS` and `SCOPE_MANAGEMENT_GROUP_IDS` are
mutually exclusive. Identifiers are trimmed, lowercased, deduplicated, and
sorted. Locations are canonical ARM location names.

Changing App Scope requires an administrator to update the relevant Function
App environment variables and restart the Function App. There is no editable
runtime configuration, configuration Table entity, configuration API, or
configuration cache.

## Subscription Scope Resolution

### Explicit subscriptions

When `SCOPE_SUBSCRIPTION_IDS` is non-empty, the enforced boundary is those
configured IDs used directly. Enforcement needs no Reference Data for this mode:
it neither reads nor initializes the Subscription snapshot, the boundary is
always the configured IDs, and it never raises `AppScopeEmptyError` because the
configured set is non-empty by definition.

Validation of the configured IDs — deciding which are actually visible to the
backend identity — is a diagnostic concern only. The App Scope Settings tab
performs it through the live read (see
[Enforcement vs Diagnostics Resolution](#enforcement-vs-diagnostics-resolution)),
not against the cached snapshot, and it never changes the enforced boundary:

- IDs the backend identity can see are shown as resolved.
- IDs it cannot see are shown as unresolved (likely a typo or missing access);
  they remain in the enforced boundary and simply return nothing from
  backend-identity collection.
- If every configured ID is unresolved, the boundary is still those IDs, so
  enforcement does not raise; reports return an empty *result set* (a normal
  `200`) and the App Scope tab shows an Error state so the administrator can
  correct the IDs.

### Management Groups

When `SCOPE_MANAGEMENT_GROUP_IDS` is non-empty, App Scope reads the published
Subscription Reference Data snapshot and selects subscriptions for which at
least one `managementGroupAncestors[].id` matches a configured Management Group
ID case-insensitively.

`SCOPE_MANAGEMENT_GROUP_IDS` accepts bare Management Group IDs only — the
immutable Management Group ID, which is exactly the form stored in each
subscription's `managementGroupAncestors[].id`. Management Group display names
and full ARM Management Group resource IDs
(`/providers/Microsoft.Management/managementGroups/<id>`) are not accepted
matching keys; a configured value that is not a bare Management Group ID simply
matches nothing. This keeps the comparison a direct, case-insensitive equality
check against the snapshot with no path parsing or normalization step.

When no configured Management Group matches any subscription, the effective
scope is empty and enforcement resolution raises `AppScopeEmptyError` (see
[Empty Scope Handling](#empty-scope-handling)). It does not cause another
refresh, and there is no separate Management Group Resource Graph query. A
mistyped, empty, inaccessible, or absent Management Group therefore has the same
fatal result. Diagnostics resolution (used by the Settings tab) does not raise;
it reports the per-Management-Group resolved count so an administrator can see
exactly which configured Management Group IDs matched zero subscriptions.

### All accessible subscriptions

When neither subscription IDs nor Management Group IDs are configured, App
Scope returns all keys from the published Subscription Reference Data
snapshot.

The result is a concrete, non-empty tuple, never `None` and never empty. If the
published snapshot contains no subscriptions, the effective scope is empty and
enforcement resolution raises `AppScopeEmptyError`. A signed-in user's Resource
Graph visibility may be broader than the backend identity that published the
snapshot; existing consumer-specific `None` conventions remain unchanged only
where that consumer independently defines and safely handles `None` as an
unfiltered backend scope, and resolution itself never produces that `None`.

## Enforcement vs Diagnostics Resolution

App Scope exposes two resolution entry points over the same inputs. They differ
only in how they read Subscription Reference Data and how they treat an empty
result:

- Enforcement — `resolve_app_scope(identity)` returns a non-empty
  `ResolvedAppScope` or raises `AppScopeEmptyError`. Every security boundary
  (report reads, mutations, collection pipelines) uses this path against the
  cached published generation for stable, low-cost resolution. All and
  Management Group modes read the cached Subscription snapshot;
  Selected-subscription mode uses the configured IDs directly, reads no
  snapshot, and never raises.
- Diagnostics — `diagnose_app_scope(identity, live=False)` never raises for an
  empty result. It returns the normalized mode, configured selectors, configured
  locations, the resolved subscription count, a per-selector breakdown, any
  unresolved selectors, an `isEmpty` flag, and the enforced published generation
  and time, so a difference between the resolved result and the enforced
  generation is a clear signal to refresh Subscription Reference Data.

The live-versus-cached choice is a parameter on the Azure Reference Data
Subscription getter, which `diagnose_app_scope` forwards through its `live`
flag; it is not a parameter of enforcement. By default the getter returns the
cached/published generation (L1, then the published snapshot); the `live` option
instead queries Azure (the Resource Graph inventory) directly and returns the
fresh result. The live read is a pure read: it fetches fresh data, returns it,
and never republishes a generation, so it can never mutate the enforced
boundary. Only a normal scheduled or administrative Subscription Reference Data
refresh replaces the published generation.

Only the App Scope Settings tab performs the live read: it calls
`diagnose_app_scope(identity, live=True)` so the administrator always sees
current Azure reality and, for Selected-subscription mode, validates the
configured IDs against the live inventory. Every other caller uses a cached
read — enforcement through `resolve_app_scope`, and the Data Collection status
display through `diagnose_app_scope(identity, live=False)` purely to show the
configured scope and whether it is currently empty, with no live Azure call. The
getter contract lives in [Azure Reference Data](azure-reference-data.md).

Diagnostics still propagates genuine infrastructure failures (storage read
failure, invalid snapshot) as errors; only an empty effective result is
non-fatal for diagnostics.

## Subscription Snapshot Initialization

App Scope reads the published Subscription snapshot but never initializes it
inline on a read. Resolution uses an explicitly named getter such as
`open_subscription_snapshot()`:

1. Open the published Subscription Reference Data snapshot.
2. If it exists, return it immediately, regardless of age or whether `values`
   is empty.
3. If no current Subscription manifest exists, return a retryable
   `app_scope_initializing` response (HTTP 503) without performing a Resource
   Graph inventory on the request thread and without blocking.
4. Storage failures and invalid persisted data are errors, not an absent
   snapshot: they propagate as failures rather than an initializing response.

Opening the snapshot is distinct from resolving the effective scope: a
successfully opened snapshot that yields an empty effective scope still raises
`AppScopeEmptyError` at the enforcement layer, while an absent snapshot yields
the retryable initializing response. Neither triggers an inline refresh, and a
Management Group with no matches does not either.

The snapshot is populated by the Subscription Reference Data pipeline, not by a
read. That pipeline is the Subscription **Reference Data** blueprint
(`functions/subscription_reference_bp.py`), which owns the `SUBSCRIPTIONS`
snapshot through `subscription_pipeline.refresh` — not the Business Context
blueprint (`subscription_context_bp.py`), which publishes an unrelated dataset.
That pipeline warms the snapshot proactively at worker startup, in
addition to its normal schedule (schedules are owned by
[Azure Reference Data](azure-reference-data.md)). The startup warm is a distinct
startup-triggered invocation — for example a warm timer registered with
`run_on_startup=True`, or an equivalent app-startup hook — because the existing
scheduled Subscription refresh timer runs with `run_on_startup=False` and
therefore does not fire on a cold boot. No existing timer in the app uses
`run_on_startup=True`, so the warm trigger is a net-new mechanism to add and
exercise. Under steady state, reads find a
published snapshot; the only window in which a read returns the initializing
response is the short interval between worker start and the first successful
warm. Duplicate startup warming across workers is accepted as last-writer-wins.

The retryable `app_scope_initializing` (HTTP 503) needs no dedicated client
handling: the existing generic error path is an acceptable presentation for the
brief cold-start window, and no frontend retry logic is introduced. The App
Scope Settings tab is unaffected because it resolves through diagnostics (which
never returns the initializing response) rather than through an enforced read.

Because reads never initialize inline, App Scope resolution takes no
initialization lock, runs no Resource Graph inventory on the request thread, and
cannot block a request waiting for one. A synchronous first-read initialization
path — and the locking and bounded-wait machinery it would require — is deferred;
see [Deferred / Future Considerations](#deferred--future-considerations).

## Pipeline Layering

App Scope resolution (All and Management Group modes) reads the published
Subscription snapshot; when it is absent, resolution returns the retryable
initializing response rather than triggering a refresh. The Subscription
pipeline owns populating the snapshot (startup warm and schedule), and the
Location Reference Data refresh resolves App Scope to select a source
subscription. These dependencies must not form a cycle
in the `services` layer; the [Backend architecture](backend-architecture.md)
dependency direction is enforced by splitting the Subscription and Location
pipelines into separate modules ordered by dependency:

- A base Subscription pipeline module (for example
  `services/subscription_reference.py`) owns `SubscriptionReferencePipeline`. It
  depends only on `clients`, `storage`, and `core`, and never imports App Scope.
- `services/app_scope.py` reads the published snapshot directly from
  `storage.reference_data_store` and imports no `services` pipeline: because a
  read never initializes inline, it needs neither the Subscription pipeline nor
  the Location pipeline.
- A higher Location pipeline module owns `LocationReferencePipeline`, which
  imports `services.app_scope` to select its source subscription.

The resulting imports are one-way — `subscription_reference` → (`clients`,
`storage`, `core`); `app_scope` → (`storage`, `clients`, `core`); `location`
pipeline → `app_scope` — with no cycle and no function-local cycle-breaking
imports. Because `app_scope` imports no pipeline, the initialization
import-cycle pressure is removed entirely; the module split is retained for a
clear dependency layering and to let the Subscription pipeline own the startup
warm. `storage.reference_data_store` continues to import no `services` module.
The module split is covered by `tests/integration/test_function_discovery.py`,
which protects blueprint and orchestrator registration across the move.

## Location Reference Data

App Scope does not initialize Location Reference Data.
`SCOPE_LOCATIONS` is a configured boundary and does not depend on the Location
catalogue.

Reports and Global Filters follow the normal Location Reference Data read and
canonical-fallback behavior defined in
[Azure Reference Data](azure-reference-data.md). A missing Location snapshot
remains missing until its scheduled or administrative refresh succeeds; a
report read does not call the Azure Locations API.

## Location Scope Enforcement

`SCOPE_LOCATIONS` is enforced as a read-time boundary on each persisted row's
canonical ARM `location`, not at collection time. Reads partition every
location-bearing dataset through `SCOPE_LOCATIONS` (empty = all locations), so a
row whose ARM location falls outside the boundary is never exposed in rows,
aggregates, counts, options, exports, or metadata — exactly as for the
subscription boundary. The same read boundary limits the location values offered
as Global Filters options to in-scope locations.

Collection treats location only as a best-effort cost-narrowing hint, never as a
correctness gate. VM Usage and CR Usage source from Cost Management, whose
`ResourceLocation` dimension uses billing meter-region vocabulary (for example
`EU West`) rather than ARM location codes (for example `westeurope`). Passing ARM
codes to that dimension can match zero rows and silently under-collect, so until
an ARM-location -> meter-region conversion exists these pipelines must not send a
location filter: they collect the location superset within the current
subscription and billing boundary and rely on read-time enforcement. When the
conversion lands it is a pure collected-volume optimization and must not change
what a report exposes.

Only datasets whose user-visible rows carry a resource ARM `location` are
location-bearing and read-partitioned by `SCOPE_LOCATIONS` — VM Usage, CR Usage,
and any resource-inventory read. Reference and event datasets are exempt at both
collection and read: Zone Mapping is a region→zone lookup table surfaced only
through a join to already location-filtered rows, and Activity Log events carry
no resource-location dimension. Neither is partitioned by `SCOPE_LOCATIONS`, so
“locations are ignored” for those two means ignored at collection and at read;
they need no location filtering because they cannot expose an out-of-boundary
location on their own.

Adopting the read-time boundary requires removing location from the VM/CR usage
collection-scope descriptor. The usage refresh currently folds `locations` into
its persisted scope state and its scope-change descriptor and lists a location
change in its structural-refusal set, so a location change today forces a
flush-then-refresh. Under this design `usage_refresh` must drop `locations` from
`_describe_scope`, from the persisted scope state, and from the
structural-refusal set, so a `SCOPE_LOCATIONS` change is neither a structural
refusal nor a Current-versus-Last difference.

Because location narrowing is applied at read and collection stores the superset,
changing `SCOPE_LOCATIONS` does not reshape any persisted dataset: it takes
effect on the next read and requires no re-collection or flush.

Read-side location enforcement and the removal of the collection-time location
filter must land together. Removing the filter before read enforcement exists
would collect the superset while nothing narrows it at read, so a non-empty
`SCOPE_LOCATIONS` would be silently unapplied to VM/CR usage — a fail-open
regression. Until read enforcement is in place, deployments that depend on VM/CR
usage must leave `SCOPE_LOCATIONS` empty (All locations) so the boundary never
appears applied while it is not.

## Freshness

Snapshot age never triggers source refresh during App Scope resolution. The
last published Subscription generation remains authoritative until the normal
scheduled or administrative Subscription Reference Data refresh replaces it.
The App Scope tab's live diagnostic reads from Azure for display, but that live
read is a pure read and never replaces the published generation.

Reference-data staleness and L1 settings remain owned by
[Azure Reference Data](azure-reference-data.md). The obsolete App Scope Table
cache and its settings are removed.

Enforcement holds no App Scope-owned resolved-result cache. All and Management
Group resolution read the published Subscription snapshot through
`storage.reference_data_store`, whose L1 in-memory cache (default 60 s,
`AZURE_REFERENCE_DATA_L1_TTL_SECONDS`) and ETag revalidation absorb the hot-path
cost, and each read recomputes the boundary from that store-cached snapshot
in-process. The recomputation is a trivial scan — return the snapshot keys for
All mode, or match `managementGroupAncestors[].id` for Management Group mode — so
no dedicated resolved-scope cache is warranted. Because the snapshot is
backend-published and identity-independent, resolution is not keyed by caller
identity.

The removed App Scope Table cache comprises `storage/app_scope_cache.py`
(`AppScopeCache`), the `_identity_fingerprint` helper in `services/app_scope.py`,
the `app_scope_cache_table_name` and `app_scope_cache_ttl_seconds` settings, and
the `APP_SCOPE_CACHE_TABLE_NAME` / `APP_SCOPE_CACHE_TTL_SECONDS` environment
variables. The `AppScopeCache` Azure Table itself is no longer created or read.
The existing tests that exercise the removed code must be deleted or rewritten in
the same change: `AppScopeCacheTests` in `tests/core/test_app_scope.py`, and the
`_identity_fingerprint` import and cases in `tests/services/test_app_scope.py`.
Otherwise the suite fails to import once the modules are gone.

## Empty Scope Handling

An empty effective App Scope is a fatal configuration error. Enforcement
resolution never returns an empty subscription set and never substitutes
`None`; it raises `AppScopeEmptyError`. Only the two modes that derive their
boundary from the Subscription snapshot can produce an empty effective scope:

- All mode with a published Subscription snapshot that contains no
  subscriptions (for example, the backend identity has not yet been granted
  Reader).
- Management Group mode where no configured Management Group matches any
  subscription in the snapshot.

Selected-subscription mode cannot produce an empty effective scope: its boundary
is the configured IDs, which is non-empty by definition and needs no snapshot.
Configured IDs that the backend identity cannot see are a diagnostic warning
(surfaced live by the App Scope tab), not an empty scope; such a scope simply
returns an empty *result set*.

An empty *scope* is distinct from an empty *result set*. A non-empty scope may
still return zero rows — for example, a user-token report (ODCR coverage) whose
signed-in caller can access none of the in-scope resources, a
Selected-subscription scope whose configured IDs are all unresolved, or a filter
that matches nothing. That is a normal `200` response. The empty-scope error
applies only when App Scope itself resolves to zero subscriptions.

Required behavior when `AppScopeEmptyError` is raised:

| Surface | Behavior |
| --- | --- |
| Coverage and Usage report reads | Return a single, admin-facing error response with a clear message. This applies only to the empty-*scope* fault; a non-empty scope that yields zero rows (for example, a user-token report the caller cannot see) is a normal `200` |
| Global Filters catalogue | Return the same admin-facing error rather than an empty option list |
| User-authored mutations | Reject with the same admin-facing message |
| Cost Management, Activity Log, Zone Mapping, Compute SKU, and Location refresh | Fail the operation with the propagated message before any Azure call; preserve the existing catalogue/dataset (a failed refresh keeps the prior generation and rows) |
| Business Context read/template | Fail with the same message; no partial template is produced |
| App Scope Settings tab and Data Collection status | Use diagnostics resolution: render configured values plus a prominent empty-scope error and troubleshooting breakdown; never crash |

The admin-facing message names the resolved mode and the likely fixes (correct
`SCOPE_*`, grant the backend identity Reader, or refresh Subscription Reference
Data) and never leaks tokens, storage credentials, or raw Azure errors. The SPA
surfaces the returned `{ "error": "app_scope_empty", "message": ... }` body as a
banner/toast.

Security invariant — no catch-and-widen: no caller may catch
`AppScopeEmptyError` (or any resolution error) and continue with a broadened,
`None`, or unfiltered scope. An empty scope must fail closed as an error, never
fail open. Because enforcement never yields an empty list, downstream filter
builders never receive `()` to mishandle.

Cost Management retains one explicit, separate meaning for `None`:

```text
None = a deliberate whole-billing-profile query (per-billing method + All mode)
```

`None` here is chosen deliberately by the collection method, never derived from
an empty or caught App Scope result. Filter builders must not rely on tuple or
list truthiness in a way that could turn a missing subscription list into an
unfiltered query.

## Settings UI

Add **App Scope** as its own Settings tab beside App settings, Data Collection,
and Business Context. The tab is read-only; the application does not edit its
own Function App environment variables.

The tab is organized into two sections: **Subscription scope** and **Location
scope**.

### Subscription scope

- an overall **health banner** with three states:
  - **OK** — the effective scope resolved to one or more subscriptions;
  - **Error** — the app cannot serve data until it is fixed: either the
    effective scope resolved to zero subscriptions (All or Management Group
    mode), or every configured subscription ID is unresolved so queries return
    nothing;
  - **Warning** — the scope resolved to at least one subscription but some
    configured selectors did not resolve;
- the normalized **mode**: All accessible subscriptions, Selected management
  groups, or Selected subscriptions;
- the **resolved subscription count** (the effective boundary size);
- the **Subscription Reference Data** generation and publication time behind the
  enforced boundary, with its staleness, so a difference from the live
  diagnostic resolution signals that a refresh is needed;
- a mode-specific **resolution breakdown**:
  - **Selected subscriptions** — a per-ID table. Each configured subscription ID
    shows either **Resolved** with its display name (found in the backend
    inventory) or **Unresolved** with a warning icon (not visible to the backend
    identity — likely a typo, a subscription outside the backend identity's
    access, or a stale inventory). A summary reads "X of Y configured
    subscriptions resolved". Unresolved IDs are listed with fix guidance and
    remain in the enforced boundary; if every ID is unresolved the banner shows
    **Error** because queries will return nothing, although enforcement still
    runs with the configured IDs rather than raising.
  - **Selected management groups** — the total resolved subscription count, then
    a per-Management-Group table showing the resolved subscription **count** per
    configured Management Group ID (counts only). A Management Group that
    resolved **0** subscriptions is flagged with a warning icon. Subscriptions
    matching more than one configured Management Group are counted once in the
    total.
  - **All accessible subscriptions** — the resolved subscription count only;
    there are no selectors to break down. A count of **0** is the empty-scope
    **Error** state.

### Location scope

- the configured **location** values, or **All locations** when none are set;
- the **location boundary count** when locations are configured; otherwise
  **All locations** rather than a misleading catalogue-derived count;
- locations are a static configured boundary and are not resolved against Azure,
  so this section shows no per-value resolution state.

Read-only administrator guidance for both sections:

1. update `SCOPE_SUBSCRIPTION_IDS` or `SCOPE_MANAGEMENT_GROUP_IDS`;
2. update `SCOPE_LOCATIONS` when the location boundary must change;
3. refresh Subscription Reference Data if a selector is unresolved because the
   inventory is stale; and
4. restart the Function App for new environment values to take effect.

The tab provides no Edit, Save, Preview, Restore, or Flush action. Environment
variables remain deployment-managed configuration and the application requires
no permission to modify its own hosting resource.

A dedicated authenticated `GET /api/app_scope` read model supplies the tab using
diagnostics resolution, which reads Subscription Reference Data through the
getter's live option, so it reflects current Azure state on each load. This
endpoint bundles everything the tab renders and returns the normalized mode,
configured selectors, configured locations, resolved subscription count, the
per-selector breakdown (per-ID resolved/unresolved for Selected subscriptions;
per-Management-Group resolved counts for Selected management groups), the
enforced Subscription Reference Data generation and publication time, and an
`isEmpty` flag. It is kept off `GET /api/settings` so the App settings tab never
pays for the live Azure inventory query. This admin-only breakdown is never
exposed to Global Filters or any non-admin surface.

Loading the tab performs a live App Scope diagnostic, which issues a
Subscription inventory query against Azure with the backend identity through the
getter's live read rather than the cached generation.
Because the tab uses diagnostics resolution, an empty result renders the error
banner and breakdown rather than failing the request. If resolution fails for an
infrastructure reason, the tab still shows the configured environment values and
restart guidance, marks the resolved count unavailable, and shows a sanitized
error. It never queries or initializes Location Reference Data merely to render
the tab.

## Existing Dataset Impact After a Scope Change

Changing `SCOPE_*` and restarting the Function App changes the live boundary,
but restart itself does not modify, delete, flush, rebuild, or refresh any
persisted collection dataset and enqueues no Durable operations. The dataset is
reconciled to the new boundary by the next refresh, not by the restart.

Each scope-dependent pipeline reconciles the difference between its
last-collected scope and the current scope on its **next refresh — scheduled or
manual, with no distinct administrator action required** — provided the change is
*compatible*. A compatible change is a pure change to the resolved subscription
set: the refresh pulls data for newly in-scope subscriptions and deletes data
for subscriptions no longer in scope. This is identical whether the set changed
because a subscription entered or left a configured Management Group (membership
drift) or because an administrator edited `SCOPE_SUBSCRIPTION_IDS` /
`SCOPE_MANAGEMENT_GROUP_IDS`, including switching between Management Group and
subscription-list selection. Reconciliation does not depend on the trigger
source and requires no manual gate.

Some changes are *structurally incompatible* and cannot be reconciled by adding
and removing subscriptions, because the shape of the persisted data changes. For
VM/CR usage these are a collection-method / billing-account / billing-profile
change and any transition between the whole unfiltered billing-profile query
(All mode, per-billing) and a materialized subscription subset. A
`SCOPE_LOCATIONS` change is not structural: location is a read-time boundary
(see [Location Scope Enforcement](#location-scope-enforcement)), so it applies on
the next read without re-collection or flush. Such a transition is an
intentional, administrator-driven decision. The pipeline refuses to reconcile
it: **every refresh — scheduled and manual — fails with a clear admin-facing
message** naming the previous and new scope and instructing the administrator to
flush then refresh, and the refusal repeats on each attempt until the pipeline is
flushed. The prior dataset is preserved unchanged while the refusal stands.

**Settings > Data Collection** surfaces both cases. Each scope-dependent section
shows **Current scope** and **Last loaded scope** (normalized configured mode,
subscription or Management Group selectors, plus each pipeline's own collection
filters — for Activity Log its operation names and terminal statuses; equivalent
ordering, casing, whitespace, or duplicate differences are not shown). A
compatible difference is informational — it is applied automatically on the next
refresh and clears itself. A structurally incompatible difference shows the
**Flush then refresh required** warning and disables Refresh until the
pipeline's normal pipeline-specific Flush is run; the removal of Flush applies
only to Subscription and Location Reference Data. Only a successful refresh
records the new last-loaded scope.

The last-loaded scope is not a separate App Scope store: each scope-dependent
pipeline persists it inside its own dataset state, captured only on a successful
refresh (for example, Activity Log and Zone Mapping fold the resolved scope into
the state they write, and their status read compares that stored state to the
current resolution). The Current-versus-Last comparison covers only the resolved
subscription set and the pipeline's own collection filters; it **excludes
locations entirely**, because `SCOPE_LOCATIONS` is a read-time boundary (see
[Location Scope Enforcement](#location-scope-enforcement)) and is not a
collection dimension for any pipeline. A `SCOPE_LOCATIONS` change therefore never
produces a Last-loaded-scope difference or warning. Activity Log's only
structural warning is a change to its operation names or terminal statuses, and
Zone Mapping has no structural dimension and so surfaces no scope-change warning
at all; both auto-reconcile subscription additions and removals.

| Data Collection section | App Scope dependency | Warning and administrator action |
| --- | --- | --- |
| VM Usage and CR Usage | Subscription boundary at collection and read; `SCOPE_LOCATIONS` enforced at read only | Subscription additions/removals within a materialized subset (or forward-gap within All mode): reconciled automatically by the next refresh. A `SCOPE_LOCATIONS` change takes effect on the next read with no re-collection or flush (location is read-enforced; collection stores the location superset). A collection-method / billing-account / billing-profile change, or a transition between the unfiltered billing profile and a materialized subscription subset: **Flush then refresh required** — every refresh fails with the message until the pipeline is flushed. |
| Activity Log | Subscription boundary; locations are ignored at both collection and read | Reconciled automatically by the next refresh: it queries added subscriptions, deletes removed-subscription data, and retains existing in-scope data. A change to operation names or terminal statuses remains a flush-then-refresh, independent of App Scope. |
| Zone Mapping | Subscription boundary; locations are ignored at both collection and read (region→zone lookup surfaced only via joins to location-filtered rows) | Reconciled automatically by the next refresh: it queries added subscriptions, deletes removed-subscription mappings, and performs its normal region-drift check. |
| Compute SKU Properties | Uses one in-scope source subscription; locations are ignored | The catalogue is regional/global data that is identical regardless of which in-scope subscription sources it, so subscription additions and removals do not surface a scope-change warning. It is affected only when the previously recorded source subscription itself leaves scope; the next refresh then automatically selects the first canonical in-scope subscription and publishes a complete replacement. |
| Subscription Reference Data | Independent source inventory used to resolve App Scope | No scope-change warning or rebuild. Its normal scheduled and administrative refresh lifecycle continues. |
| Location Reference Data | Uses an in-scope subscription only as an Azure API source; `SCOPE_LOCATIONS` does not constrain this catalogue | No scope-change warning or rebuild. Its next normal refresh selects a source from the then-current App Scope. |
| Business Context | Read/template output is intersected with live App Scope | No dataset mutation or rebuild. The new boundary applies when data is read or a template is generated. |

When the new App Scope resolves to zero subscriptions, it is a fatal
configuration error, not a scope-mismatch warning. Data Collection shows the
empty-scope error (via the cached diagnostics read, with no live Azure call) and
scope-dependent Refresh actions fail with the same admin-facing message until
the administrator corrects `SCOPE_*`, grants the backend identity Reader, or
refreshes Subscription Reference Data so the scope resolves to at least one
subscription. Previously persisted data is preserved unchanged while the scope
is empty.

## Refresh and Removal of Flush

App Scope has no independent persisted resolved-result cache and therefore no
App Scope flush operation. Restarting the Function App loads changed `SCOPE_*`
environment values. Refreshing Subscription Reference Data re-evaluates the
resolved subscription count for Management Group and All subscription scope.

The proposed administrative lifecycle for both Subscription and Location
Reference Data is refresh-only. Refresh publishes a complete replacement
generation through the existing manifest protocol. Deleting a healthy current
generation is not required to obtain fresh source data and creates avoidable
missing-data races.

Accordingly, the pending implementation should remove the Subscription and
Location Reference Data flush routes, orchestrators, UI actions, and tests.
This approved target lifecycle is also specified in
[Azure Reference Data](azure-reference-data.md).

## Failure Semantics

| Condition | Behavior |
| --- | --- |
| Explicit subscription configuration | Return configured IDs directly |
| Subscription snapshot absent | Return a retryable `app_scope_initializing` (HTTP 503); the startup warm and scheduled refresh populate it, with no inline refresh and no blocking |
| Subscription snapshot stale | Return it without source refresh |
| Subscription snapshot open with empty values | Snapshot returned without source refresh; if this yields an empty effective scope, resolution raises `AppScopeEmptyError` |
| All mode resolves to zero subscriptions | Raise `AppScopeEmptyError` without source refresh |
| Management Group has no matches | Raise `AppScopeEmptyError` without source refresh |
| Storage read fails | Fail; absence cannot be established safely |
| Manifest or snapshot is invalid | Fail; do not repair corruption implicitly during a read |
| Startup or scheduled warm fails or publishes nothing | Snapshot stays absent; reads keep returning the retryable initializing response until a later warm succeeds |
| Location snapshot absent | Preserve normal canonical fallback; do not initialize on read |

## Security Properties

- Subscription Reference Data is collected with the backend identity.
- Management Group and All modes include only subscriptions present in the
  published backend inventory.
- User-facing Azure queries continue to receive a concrete App Scope list, so
  broader user RBAC cannot escape the backend-defined boundary.
- Resolution never returns an empty or `None` scope; an empty effective scope
  raises `AppScopeEmptyError`, and no caller may catch it and continue with a
  broadened or unfiltered scope.
- A stale generation intentionally remains the boundary until normal refresh
  replaces it.
- A snapshot published by a prior backend identity remains usable until normal
  refresh replaces it. Identity-bound snapshot validation is deferred as a
  future Azure Reference Data improvement.
- The read-only Settings tab introduces no application-owned configuration
  mutation surface.

## Deferred / Future Considerations

These enhancements are intentionally out of scope for the initial
implementation. Add one only when a concrete requirement justifies it.

- **Synchronous first-read Subscription initialization.** If a requirement
  emerges that the first read after a cold boot must return data rather than a
  retryable `app_scope_initializing` (HTTP 503), a read may initialize the
  snapshot inline. That path requires the machinery deliberately omitted from
  the approved design: a process-local, non-reentrant lock with double-checked
  locking so concurrent readers coalesce onto one refresh; deterministic lock
  release; a request timeout on the in-thread Resource Graph inventory; and a
  bounded waiter timeout that falls back to the retryable 503 instead of
  blocking. It buys only the first post-cold-boot read, so it is not worth its
  complexity unless the startup warm proves insufficient in practice.
- **Identity-bound snapshot validation.** Validating that a snapshot was
  published by the current backend identity is deferred as a future Azure
  Reference Data improvement (see [Security Properties](#security-properties)).
- **ARM-location → meter-region conversion.** Converting canonical ARM location
  codes to Cost Management meter-region vocabulary, so VM/CR usage collection can
  narrow by location instead of collecting the superset, is a pure
  collected-volume optimization tracked in the backlog; it must never change what
  a report exposes (see [Location Scope Enforcement](#location-scope-enforcement)).

## Decisions Before Implementation

Empty effective scope now fails closed as `AppScopeEmptyError` rather than a
successful no-work outcome. These previously open points are now resolved:

1. **HTTP status for empty scope on data-serving endpoints.** `409 Conflict`
   carrying `{ "error": "app_scope_empty", "message": ... }`. Mutations reject
   with the same code and message.
2. **Live diagnostic read: cost and divergence display.** Accepted as low cost.
   Only the App Scope Settings tab performs the live read (a pure read that never
   republishes); it validates Selected-subscription IDs live and shows the
   enforced published generation alongside the live result, with an inline
   "refresh Subscription Reference Data to apply" hint when they differ. Data
   Collection status and enforcement do not perform the live read.
3. **Selected-subscription validation.** Enforcement uses the configured IDs
   directly with no Subscription-snapshot dependency and never raises. Per-ID
   validation is live and performed only by the App Scope tab; all-unresolved is
   a diagnostic Error state, not a fatal enforcement error.
4. **Collection operation result shape on empty scope.** The Durable operation
   fails with the empty-scope message and preserves prior data for Cost
   Management, Activity Log, Zone Mapping, Compute SKU, and Location refresh; no
   silent no-op.
5. **Cold-start Subscription initialization.** Reads never initialize the
   snapshot inline. On an absent snapshot the getter returns a retryable
   `app_scope_initializing` (HTTP 503); the Subscription Reference Data
   blueprint (`functions/subscription_reference_bp.py`) populates the
   snapshot through a startup warm (a `run_on_startup=True` warm timer or an
   equivalent app-startup hook) plus its normal schedule. This removes the
   initialization lock, the in-request Resource Graph inventory, and the
   bounded-wait machinery. Cross-worker duplicate warming is accepted as
   last-writer-wins. A synchronous first-read initialization path is deferred
   unless a requirement emerges that the first post-cold-boot read must return
   data rather than a retryable 503; see
   [Deferred / Future Considerations](#deferred--future-considerations).
6. **Client handling of the initializing response.** No dedicated frontend
   handling is added for the retryable `app_scope_initializing` (HTTP 503). The
   existing generic error path is accepted for the brief cold-start window; the
   App Scope Settings tab is unaffected because it resolves through diagnostics.

## Implementation Sequencing

Several changes are coupled and can silently under-collect or break the test
suite if merged in the wrong order. The recommended slice order is:

1. **Location read enforcement + collection-filter removal, together.** Add
   read-time location enforcement to the location-bearing datasets and remove the
   meter-region collection filter from VM/CR usage in the same change, and keep
   `SCOPE_LOCATIONS` empty until both have landed. Splitting these fails open
   (filter removed but not yet read-enforced) or under-collects (filter kept),
   per the transitional caveat in
   [Location Scope Enforcement](#location-scope-enforcement).
2. **`usage_refresh` structural-scope change.** Drop `locations` from
   `_describe_scope`, the persisted scope state, and the `REFUSE_STRUCTURAL`
   set, so a `SCOPE_LOCATIONS` change is no longer treated as structural.
3. **Gap-F dead-code and test removal.** Remove the App Scope Table cache and
   its settings/env vars, and delete or rewrite `AppScopeCacheTests`
   (`tests/core/test_app_scope.py`) and the `_identity_fingerprint` cases
   (`tests/services/test_app_scope.py`) in the same change.
4. **Startup warm + retryable 503.** Add the `run_on_startup=True` warm to the
   Subscription Reference Data blueprint and switch reads to the
   `app_scope_initializing` (HTTP 503) response on an absent snapshot.
5. **Refresh-only lifecycle.** Remove the Subscription and Location Reference
   Data flush routes, orchestrators, UI actions, and tests.

No dedicated frontend change is required: the retryable 503 uses the existing
generic error path.

## Required Verification

Tests must prove:

1. Explicit subscription mode enforces the configured IDs directly, reads and
    initializes no Subscription snapshot, and never raises; per-ID validation is
    performed only by the App Scope tab's live read and never changes the
    enforced boundary.
2. All mode returns normalized Subscription snapshot keys.
3. Management Group matching compares configured bare Management Group IDs to
    snapshot `managementGroupAncestors[].id` by direct case-insensitive equality;
    a display name or a full ARM Management Group resource ID matches nothing (no
    path normalization is performed).
4. No Management Group match raises `AppScopeEmptyError` without a source
    refresh; diagnostics reports the per-Management-Group zero count.
5. An absent Subscription manifest triggers no inline refresh; the snapshot is
    populated only by the Subscription pipeline's startup warm and schedule.
6. Stale snapshots do not trigger refresh; a published-empty snapshot does not
    trigger refresh and an empty effective scope raises `AppScopeEmptyError`.
7. App Scope and reports never initialize Location Reference Data.
8. Storage failures and invalid snapshots do not trigger initialization.
9. An absent snapshot makes reads return a retryable `app_scope_initializing`
    (HTTP 503) without running a Resource Graph inventory on the request thread,
    without taking an initialization lock, and without blocking; resolution
    never triggers an inline refresh.
10. Enforcement never returns an empty or `None` scope; no caller catches
    `AppScopeEmptyError` and widens scope; Cost Management issues no unfiltered
    request as a result of empty scope.
11. Report and Global Filters endpoints return the admin-facing `app_scope_empty`
    error (HTTP 409) when the *scope* is empty, but return a normal `200` (zero
    rows / no options) when a non-empty scope simply yields no user-visible
    results.
12. Refresh-only administration has no remaining Subscription or Location
    flush route or UI action.
13. The App Scope Settings tab is read-only, reports resolved subscription and
  location counts, and identifies the three `SCOPE_*` settings and restart
  requirement.
14. Business Context read/template fails with the empty-scope error when App
  Scope is empty and produces no partial template.
15. No App Scope mutation, preview, restore, configuration Table, or
  configuration-cache surface remains.
16. Restarting after a `SCOPE_*` change starts no collection operation and
  mutates no persisted collection data.
17. Compatible scope changes (subscription add/remove, whether from membership
  drift or a selector edit including switching between Management Group and
  subscription-list selection) are reconciled automatically by the next refresh
  with no manual gate and no dependence on the trigger source; only structurally
  incompatible changes surface a **Flush then refresh required** warning.
18. Scheduled and manual refreshes both auto-reconcile compatible changes and
  record the new last-loaded scope on success; a structurally incompatible
  change fails every refresh (scheduled and manual) with the flush-then-refresh
  message and preserves the prior dataset until the pipeline is flushed.
19. Empty-scope Compute SKU and Location refreshes make no Azure request, fail
  the operation with the empty-scope message, and preserve their current
  catalogues.
20. VM and CR usage reads cannot expose persisted rows outside the current
  effective App Scope — including rows whose canonical ARM `location` falls
  outside `SCOPE_LOCATIONS` — even if a collector persisted surplus subscription
  or location rows.
21. Diagnostics resolution never raises for an empty result and reports mode,
  resolved count, per-selector breakdown, unresolved selectors, snapshot
  generation/time, and `isEmpty`.
22. The App Scope tab renders the empty-scope error state and the mode-specific
  breakdown (per-ID resolved/unresolved; per-Management-Group counts with a
  zero-count warning) without failing the request.
23. Selected-subscription mode with at least one resolved ID remains usable,
  warns on unresolved IDs, and never drops an unresolved ID from the enforced
  boundary; Selected-subscription mode with every configured ID unresolved does
  not raise — enforcement keeps the configured IDs, reports return an empty
  result set, and the App Scope tab shows the Error state.
24. The App Scope tab's diagnostics uses the getter's live option (a live Azure
  query, not the cached generation) while Data Collection status diagnostics
  uses the cached read with no live Azure call; neither raises on empty, and
  both report the enforced published generation/time so divergence is visible.
25. The App Scope Settings tab presents Subscription scope and Location scope as
  separate sections; the Location section shows configured locations or All
  locations with no per-value resolution.
26. App Scope resolution reads the Subscription snapshot with no circular
  import: the base Subscription pipeline module imports no App Scope,
  `services/app_scope.py` reads the snapshot from `storage.reference_data_store`
  and imports no `services` pipeline, and the Location pipeline imports
  `services.app_scope`; module discovery and blueprint/orchestrator registration
  remain intact after the split.
27. Startup warming publishes the Subscription snapshot shortly after worker
  boot so steady-state reads find a published snapshot; a read returns the
  retryable initializing response only during the cold window before the first
  successful warm, and duplicate startup warms across workers resolve as
  last-writer-wins.
28. Enforcement keeps no App Scope-owned resolved-result cache: All and
  Management Group resolution read the store-cached Subscription snapshot (the
  store's L1 TTL and ETag revalidation are the only hot-path cache) and recompute
  the boundary per read without keying on caller identity; `AppScopeCache`,
  `_identity_fingerprint`, the `app_scope_cache_table_name` /
  `app_scope_cache_ttl_seconds` settings, and the `APP_SCOPE_CACHE_*`
  environment variables are gone, and no `AppScopeCache` Table is created or read.
29. `SCOPE_LOCATIONS` is enforced as a read-time boundary on canonical ARM
  `location`: VM/CR usage collection sends no meter-region location filter to
  Cost Management (it collects the location superset within the subscription and
  billing boundary), the usage refresh drops `locations` from its scope
  descriptor, its persisted scope state, and its structural-refusal set so a
  `SCOPE_LOCATIONS` change is neither a structural refusal nor a
  Current-versus-Last difference, a `SCOPE_LOCATIONS` change applies on the next
  read with no re-collection or flush, and reads exclude out-of-boundary
  locations from rows, aggregates, counts, options, exports, and metadata.
30. Each scope-dependent pipeline persists its last-loaded scope inside its own
  dataset state (no separate App Scope last-loaded store), recorded only on a
  successful refresh; the Data Collection Current-versus-Last comparison covers
  the resolved subscription set and the pipeline's own collection filters and
  excludes locations, so a `SCOPE_LOCATIONS` change surfaces no Last-loaded-scope
  difference or warning for any pipeline, Activity Log warns only on an
  operation-name / terminal-status change, and Zone Mapping surfaces no
  scope-change warning.
31. Read-time location enforcement is applied only to location-bearing datasets
  whose user-visible rows carry a resource ARM `location` (VM Usage, CR Usage,
  and resource-inventory reads); Zone Mapping and Activity Log are not
  partitioned by `SCOPE_LOCATIONS` at collection or read and cannot expose an
  out-of-boundary location because they carry no independent location dimension
  (Zone Mapping surfaces only through a join to already location-filtered rows).
