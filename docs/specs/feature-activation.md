# Feature Activation

## Status

Current implemented contract. Last verified on 2026-10-05.

## Purpose

Allow an administrator to activate application features without redeploying the
Function App. Feature activation controls:

- whether the feature appears in the SPA navigation;
- whether feature-specific HTTP endpoints allow access; and
- whether a collector used only by that feature starts new collection runs.

This is an application-wide setting, not a per-user rollout mechanism. Existing
Entra application roles remain the authorization boundary: activation can
remove a capability from eligible users, but it cannot grant a capability to a
user who lacks the required role.

## Initial Features

The static feature registry initially contains exactly two entries:

| ID | Display name | Maturity | Default |
| --- | --- | --- | --- |
| `odcr` | On-Demand Capacity Reservations | General availability | Enabled |
| `allocation_events` | Allocation Events | Preview | Disabled |

`Allocation Events` is the existing repository name for the VM allocation-event
report. The admin list displays it as **Allocation Events (Preview)**.

Both features can be enabled or disabled, subject to one application-wide
invariant: **at least one registered feature must remain enabled**. This keeps
the application useful without introducing a permanent special case for ODCR.

The feature registry is the source of truth for IDs, labels, maturity, and
defaults. The data-collection registry owns collector dependencies. Storage
contains activation state, not feature definitions. A persisted state containing
unknown IDs or no enabled registered feature is invalid and must not create or
silently remove a capability.

## Admin Experience

Add a **Features** tab to the existing Settings area. The tab is visible only to
administrators and contains a short activation list rather than a general
feature-management system.

Each row contains:

- display name on the left;
- a **Preview** badge for Allocation Events;
- an on/off switch on the right; and
- a short status or error message while saving.

Behavior:

- ODCR is on and Allocation Events is off when no persisted state exists.
- An Admin may change either feature.
- The switch for the sole enabled feature is disabled and explains that at
  least one feature must remain enabled.
- The backend independently rejects any request that would disable the last
  enabled feature; frontend switch state is not an enforcement boundary.
- Saving updates the row after the backend confirms the write.
- A successful change shows: **Saved. Reload the app for navigation changes to
  take effect.**
- A failed change restores the confirmed value and surfaces the backend error.
- The switch is disabled while its request is in progress.

The list does not provide staged rollouts, schedules, tenant/user targeting,
dependencies editable by users, bulk actions, or feature deletion.

## Startup and Navigation

After resolving `/api/me`, the SPA requests the effective feature list once
from `GET /api/features`. It does not poll and does not refresh that application
snapshot after an admin changes a toggle. A full browser reload obtains a new
snapshot.

Navigation visibility is the intersection of role eligibility and the startup
feature snapshot:

```text
visible = role allows feature AND startup feature snapshot says enabled
```

- ODCR navigation is visible to `User` and `Admin` only when `odcr` is enabled.
- Allocation Events navigation is visible to `User` and `Admin` only when
  `allocation_events` is enabled.
- The Settings navigation and Features tab remain controlled by the Admin role,
  not by a feature flag.
- A manually entered route for a hidden or disabled feature redirects to the
  first enabled feature available to the user, in registry order. The
  at-least-one invariant guarantees a fallback for every recognized User or
  Admin. Admins may still navigate to Settings.

The existing `capabilities.useOdcr` value currently also acts as the SPA's
general recognized-role capability. Renaming that contract is useful cleanup
but is not required for this feature. The implementation may combine it with
the separate startup feature map to avoid an unrelated authorization refactor.

If the startup feature request fails, the SPA must not guess that a mutable
feature is enabled. It shows an availability error with a reload action and
does not load feature reports. Admin Settings may remain reachable so an
administrator can diagnose the application. The error must not be converted
into a successful-looking default response.

## HTTP Contract

### List effective features

```http
GET /api/features
```

Requires a recognized `User` or `Admin` application role, but deliberately does
not require any feature to be enabled. It returns the complete static registry
in stable registry order with effective values:

```json
{
  "features": [
    {
      "id": "odcr",
      "name": "On-Demand Capacity Reservations",
      "enabled": true,
      "maturity": "general_availability"
    },
    {
      "id": "allocation_events",
      "name": "Allocation Events",
      "enabled": false,
      "maturity": "preview"
    }
  ]
}
```

The admin tab and startup navigation use the same endpoint. Non-admin callers
receive the same non-sensitive state needed to render navigation.

### Change one feature

```http
PATCH /api/features/{featureId}
Content-Type: application/json

{ "enabled": true }
```

Requires `Admin`. The endpoint:

1. validates the ID against the static registry;
2. requires `enabled` to be a JSON boolean;
3. applies the requested change to the complete current activation state;
4. rejects the change if it would leave no registered feature enabled;
5. persists the complete activation state;
6. refreshes the current process's L1 entry; and
7. returns the updated effective feature.

Errors use the existing JSON `{ "error", "message" }` shape:

| Condition | Status | Error |
| --- | --- | --- |
| Unknown feature | 404 | `unknown_feature` |
| Missing or non-boolean `enabled` | 400 | `invalid_body` |
| Attempt to disable the last enabled feature | 409 | `at_least_one_feature_required` |
| Storage conflict or failure | 409 or 503 | `feature_update_conflict` or `feature_state_unavailable` |

No delete/reset endpoint is needed initially.

## Backend Authorization

Feature enforcement belongs in the same backend permission guard that enforces
the application role. Extend the User policy to accept an optional feature ID,
conceptually:

```python
require_user(req, feature="allocation_events")
```

The guard checks in this order:

1. authenticate and normalize the principal;
2. require the `User` or `Admin` role;
3. validate the requested feature ID against the registry; and
4. require the effective feature state to be enabled.

A disabled feature returns HTTP 403:

```json
{
  "error": "feature_disabled",
  "message": "Allocation Events is not enabled."
}
```

A feature-state storage failure after the L1 value has expired returns HTTP 503
`feature_state_unavailable`; it must not silently allow access.

Every Allocation Events route passes `allocation_events`. Every ODCR report,
scope/catalogue, and coverage-decision route passes `odcr`. Generic identity,
feature-list, Admin, notification, Settings, and Data Collection administration
routes do not pass a feature. `require_admin` remains role-only so an
administrator cannot lock themselves out of feature administration.

The frontend is only a presentation optimization. Hiding a menu entry never
replaces the backend check, and a previously loaded browser cannot continue to
use a feature after the backend observes that it is disabled.

## Persistence Choice

Use Azure Table Storage, in a dedicated `FeatureActivation` table.

Table Storage is preferred over Blob Storage because:

- the complete activation state is one tiny entity and maps to a point read;
- an entity ETag provides straightforward conflict detection; and
- the application already has Table Storage access, lazy table creation, and
  store patterns.

Store the complete state in one entity rather than one entity per feature. The
cross-feature "at least one enabled" invariant must be checked and persisted
atomically. A singleton entity avoids a race in which two administrators disable
different features concurrently and leave none enabled.

Entity shape:

| Property | Value |
| --- | --- |
| `PartitionKey` | `config` |
| `RowKey` | `features` |
| `EnabledFeatureIds` | Compact JSON array of enabled registry IDs |

On the first read, the lazy Table client creates the table if necessary but does
not create the singleton entity. An absent entity means the registry defaults:
ODCR enabled and Allocation Events disabled. The first successful state change
creates the entity. Reads validate that every stored ID exists in the registry
and that at least one registered feature is enabled. Invalid persisted state
returns `feature_state_unavailable` and is logged rather than silently replaced
with defaults.

PATCH reads the singleton, applies one requested change, validates the invariant,
and conditionally replaces the entity using its ETag. Concurrent changes return
409 and cause the admin list to reload the confirmed state. Creating the
previously absent entity also handles a concurrent create as a conflict.

The low write frequency does not make Blob Storage materially cheaper or
simpler. Table Storage aligns with existing configuration stores and avoids
introducing another blob document convention.

Add a `FEATURE_ACTIVATION_TABLE_NAME` setting with `FeatureActivation` as the
default. The existing app-data storage account and managed-identity Table Data
Contributor permission are sufficient; no new storage account or secret is
required.

## L1 Cache

Feature reads use a process-local, thread-safe TTL cache similar to the
reference-data L1 cache:

- cache the complete effective feature map because it contains only two small
  entries;
- use `time.monotonic()` for expiry;
- cache the absent-entity/default result as well as persisted values;
- default TTL: 60 seconds, configurable through
  `FEATURE_ACTIVATION_L1_TTL_SECONDS`;
- update the current process's cache immediately after a successful PATCH; and
- never cache a storage exception as a feature value.

Each Function host process has its own L1 cache. A write is therefore immediate
in the handling process but may take up to one TTL on another warm process.
This is acceptable for a deployment feature switch, not a security revocation
mechanism, and must be documented in the admin UI/help text. Reducing the TTL
trades faster convergence for more Table reads. Cross-instance invalidation is
intentionally out of scope.

## Data Collection Dependencies

Add a `required_by_features` field to the static data-collection descriptor:

```python
required_by_features: tuple[str, ...] | None = None
```

Its semantics are:

- `None`: the collector is always required, regardless of feature activation;
- an empty tuple: the collector has no current feature consumer and refresh is
  never started;
- a non-empty tuple: the collector is required when **any** listed feature is
  enabled.

Existing collectors default to `None`. Set the Activity Log descriptor to:

```python
required_by_features=("allocation_events",)
```

VM Usage is currently not consumed by a report and uses:

```python
required_by_features=()
```

Registry validation must reject unknown feature IDs. The Data Collection list
contract exposes `requiredByFeatures` as `null` for always-required collectors,
an empty array for collectors with no current consumer, or the feature-ID array
so the admin UI can explain which disabled features block a collector.

Feature activation is a hard gate above the existing scheduler configuration:

```text
may collect =
    allocation_events feature enabled
    AND activity_log scheduler configuration enabled
```

Required behavior:

- before cadence, busy-state, or Durable launch checks, the dispatcher determines
  whether the collector is required; it does not attempt to launch a collector
  whose dependencies are not currently enabled;
- an always-required collector bypasses the feature-state lookup and is
  unaffected by feature activation; its existing schedule and pipeline-enabled
  setting still decide whether it runs;
- the scheduler does not dispatch a new Activity Log refresh while Allocation
  Events is disabled;
- a manual Activity Log refresh is also rejected while disabled, rather than
  bypassing the feature gate;
- status and run history remain visible to Admin;
- flush remains available to Admin because it deletes retained data rather than
  collecting new data;
- disabling does not rewrite the pipeline's cadence or enabled setting;
- re-enabling restores the previous pipeline configuration and an overdue
  pipeline may run on the next scheduler tick; and
- disabling does not terminate an orchestration already in progress. The
  current run finishes, but no new run starts.

The Data Collection UI should distinguish **disabled by schedule** from
**blocked by feature** so two controls do not appear contradictory.

The whole Activity Log pipeline is initially tied to Allocation Events even
though its recipe registry also contains capacity-reservation operations. If a
second feature later consumes that history, add its ID to
`required_by_features`; the "any enabled dependency" rule then keeps collection
active for either consumer.

## Consistency and Timing

Three intentionally different refresh boundaries apply:

| Surface | When a change becomes visible |
| --- | --- |
| Admin Features row | Immediately after successful PATCH |
| Backend route and collector checks | Immediately on the current host; within the L1 TTL on other hosts |
| SPA navigation | On full page reload only |

This means disabling can make a route return 403 before an already-open SPA
hides its menu. The SPA must surface that response normally. Enabling can make
the backend available before an open SPA adds its menu; the success message
instructs the administrator to reload.

## Failure and Edge Cases

- **Storage unavailable:** expired mutable feature checks return 503 and the
  collector logs the failure and skips dispatch for that tick. This is distinct
  from a normal disabled result.
- **Unknown registry ID:** fail explicitly; never treat it as disabled or create
  it through PATCH.
- **Last enabled feature:** return 409 without writing or changing the L1 cache.
- **Multiple administrators:** use the Table entity ETag on replacement. A
  conflict returns 409 and the admin list reloads the confirmed value.
- **Disable during an HTTP request:** the request that already passed the guard
  completes. The flag is checked once at request entry, not throughout service
  execution.
- **Disable during collection:** the active Durable run completes as described
  above. Forced termination and partial-write cleanup are out of scope.
- **Existing retained data:** disabling does not delete it. Backend guards keep
  it inaccessible through the feature routes, and re-enabling can reuse it.
- **Preview meaning:** the Preview badge communicates maturity only. It does not
  change authorization, persistence, or API stability by itself.

## Observability

Log successful changes with the feature ID and old and new effective values.
Log storage failures and collector skips caused by an unavailable feature
state. Do not log tokens or raw Easy Auth headers.

Existing Function telemetry is sufficient. Metrics, change-history storage, and
a dedicated audit screen are deferred.

## Verification

Implementation should add focused coverage for:

- registry defaults and rejection of unknown IDs;
- absent-entity defaults: ODCR enabled and Allocation Events disabled;
- either feature can be disabled when the other is enabled;
- rejection of a change that would disable the last enabled feature;
- atomic invariant preservation under concurrent admin changes;
- rejection of persisted unknown IDs, an empty enabled-ID list, and an empty
  feature activation state;
- Admin-only PATCH and User-readable GET;
- strict boolean request validation;
- L1 hit, expiry, negative/default caching, write-through update, and
  thread-safe access;
- 403 from feature-aware `require_user` when disabled;
- 503 rather than allow-on-error when state cannot be resolved;
- every ODCR and Allocation Events HTTP route using the appropriate feature;
- startup menu visibility for each role/state combination;
- no frontend polling or post-save mutation of the startup navigation snapshot;
- always-required collectors remaining independent of feature activation;
- collectors with no current consumer never starting refresh work;
- collectors with multiple dependencies running when any dependency is enabled;
- scheduler and manual refresh blocked while Allocation Events is disabled;
- flush/status/history behavior while disabled;
- no termination of an already-running Activity Log orchestration; and
- integration asset checks for the new Features tab and frontend module.

## Deferred

- Percentage rollouts, user/group targeting, and tenant-specific targeting.
- Time-based activation or automatic expiry.
- Feature prerequisites editable by administrators.
- Cross-instance cache invalidation.
- Persistent feature change history or last-update metadata.
- Terminating in-flight work when a feature is disabled.
- Moving existing pipeline cadence switches into the Features tab.
