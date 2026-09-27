# Allocation Events

## Status

Current implemented feature specification. Last verified on 2026-09-24.

This document defines the Allocation Events VM report and per-VM activity
timeline. The retained Activity Log source, operation assembly, recipe versions,
and collection commit protocol are defined in
[Activity Log History Pipeline](activity-log-history.md).

## Purpose

Allocation Events helps an operator answer:

- which visible VMs recently succeeded or failed allocation-related operations;
- which failures are confirmed Azure capacity-allocation failures;
- whether failures came from lifecycle actions or VM creation;
- what Azure error hierarchy was returned for a failed operation; and
- how one VM's lifecycle and configuration changed over the collected coverage
  period.

The report combines a live Resource Graph VM inventory with retained Activity
Log operations. Resource Graph defines the current caller-visible population;
Activity Log provides historical operation facts.

## Scope

In scope:

- A VM inventory limited by Application Scope, signed-in user visibility, and
  Global Filters.
- Allocation success and failure metrics for 1-day, 7-day, and 30-day periods.
- A switch between confirmed allocation failures and all operation failures.
- Successful and failed VM creates in allocation metrics.
- Local report filtering, sorting, pagination, CSV export, and logical/physical
  availability-zone display.
- A dynamically loaded, full-coverage VM activity timeline.
- Structured Azure parent and nested error details.
- Coverage, staleness, loading, empty, and error states.

Out of scope:

- Failed create attempts for VMs that are no longer returned by Resource Graph.
  The report population is current visible VMs, not an orphan-operation
  inventory.
- Treating every VM write failure as an allocation failure.
- Inferring placement from a VM update that omits location or zones.
- Reconstructing information absent from the Azure Activity Log payload.
- Editing, retrying, or otherwise changing Azure resources.

## Terminology

| Term | Meaning |
| --- | --- |
| VM inventory | Current VM resources returned by Resource Graph for the signed-in user inside Application Scope and Global Filters. |
| Lifecycle operation | Start, Restart, Reapply, or Redeploy. Deallocate is retained for history but excluded from allocation metrics. |
| Create operation | A VM write operation whose correlated projected provisioning state is `Creating`. |
| Allocation event | A completed lifecycle or create operation included in report metrics. |
| All failure | Any included allocation event whose terminal outcome is `failed`. |
| Confirmed allocation failure | An included failure whose complete Azure error hierarchy contains `AllocationFailed` or `ZonalAllocationFailed`. |
| Coverage end | The per-subscription Activity Log collection boundary used as the metric-period anchor. |
| Complete period | A period fully covered by current `vm-lifecycle` and `vm-configuration` recipe versions. |
| Current state | Live VM configuration read from Resource Graph when the timeline is requested. |

## Authorization and Population

The list endpoint requires the application User policy. Its effective
population is:

```text
Application Scope
AND signed-in user Resource Graph visibility
AND Global Subscription filter
AND Global Location filter
AND enabled Business Context filters
```

`Admin` satisfies the User policy. Application roles do not expand Azure RBAC.

The timeline performs a second, exact authorization check for the requested VM:

1. validate and normalize the ARM VM resource ID;
2. verify that its subscription belongs to Application Scope;
3. query that exact VM through Resource Graph with the caller's ARM token; and
4. read retained Activity Log storage only when the VM is found.

An absent, deleted, or caller-invisible VM returns the same not-found response.
Stored history is not disclosed. See [Authorization](authorization.md).

## Data Sources

### Current VM inventory

Resource Graph provides:

- ARM resource ID and type;
- subscription ID and display name;
- resource group and VM name;
- canonical location;
- logical availability zone;
- VM size;
- power state; and
- current provisioning state and Capacity Reservation Group for timeline
  requests.

Resource IDs, resource types, subscription IDs, and locations are normalized
for matching. Azure Location Reference Data supplies display names. The zone
mapping pipeline enriches logical zones with physical zones.

### Retained Activity Log operations

The feature consumes retained `vm-lifecycle` and `vm-configuration` operations
and their per-subscription coverage. It does not call live Activity Log from an
interactive report request.

Collection, stage correlation, projected columns, recipe versions, structured
error retention, payload policy, physical storage, coverage commits, and replay
are owned by
[Activity Log History Pipeline](activity-log-history.md). Allocation Events
requires both VM recipe projections to be current before it presents a period
as complete.

Report and timeline storage reads query by normalized subscription and inclusive
time window. Operation names and resource IDs are then matched
case-insensitively in application code. This is required because Activity Log
can vary casing while Azure Table Storage string equality is case-sensitive.

## Allocation Event Classification

### Included lifecycle operations

The metric allowlist is:

- `Microsoft.Compute/virtualMachines/start/action`
- `Microsoft.Compute/virtualMachines/restart/action`
- `Microsoft.Compute/virtualMachines/reapply/action`
- `Microsoft.Compute/virtualMachines/redeploy/action`

Matching is case-insensitive. Deallocate operations remain available to the
timeline but do not represent an allocation attempt and are excluded from
report metrics.

### Included create operations

A VM write is a create allocation event only when its correlated configuration
projection has:

```text
ProvisioningState == Creating
```

Matching is case-insensitive.

This rule applies to succeeded and failed terminal outcomes. Ordinary VM
updates, deletes, and writes without a correlated `Creating` projection are
excluded from allocation metrics.

### Outcomes

Only terminal `succeeded` and `failed` operations contribute to metrics.
Pending, unknown, and cancelled operations do not.

The report tracks source breakdowns separately:

- lifecycle successes;
- successful creates;
- lifecycle failures;
- failed creates;
- confirmed lifecycle allocation failures; and
- confirmed create allocation failures.

## Failure Classification and Azure Error Shape

Confirmed allocation failure codes are:

- `AllocationFailed`
- `ZonalAllocationFailed`

Classification is case-insensitive and recursively evaluates the parent error
and every nested `details` entry. The first detail is not assumed to be the
capacity error.

`SkuNotAvailable` is not a confirmed allocation failure. It indicates SKU
catalogue or placement availability rather than a capacity allocation attempt.
Other failures such as `VMMarketplaceInvalidInput`,
`OSProvisioningClientError`, and `OperationNotAllowed` remain visible in
all-failures mode but are not classified as confirmed allocation failures.

The consumer-facing error contract preserves the parent code and message,
first-detail summary, immediate detail count, all recursively nested codes,
hierarchy-wide allocation classification, and complete sanitized `details`
array. The underlying projection and storage contract is defined by
[Activity Log History Pipeline](activity-log-history.md).

When Azure supplies no structured `statusMessage.error`, the operation remains
a failed event without invented code or message values.

## Coverage and Lookback Periods

The period choices are:

- 1 day;
- 7 days; and
- 30 days, selected by default.

Each VM's periods are anchored to its subscription's `coverageEnd`, not browser
time. The latest configured ingestion-lag window is therefore excluded by the
collector before coverage advances.

A VM period is complete only when:

1. the subscription has both coverage boundaries;
2. `coverageStart` is no later than the selected period start; and
3. both required recipe versions are enabled and current.

Incomplete periods display **Data unavailable** and do not present a partial
success rate as complete data.

The report-level metadata returns a common coverage start and end only when
every subscription represented in the current VM population has coverage.
Per-VM period completeness remains authoritative when aggregate coverage is
unavailable.

## Allocation Metrics

Each period returns:

```json
{
  "start": "2026-08-25T08:00:00Z",
  "end": "2026-09-24T08:00:00Z",
  "complete": true,
  "succeeded": 12,
  "failed": 3,
  "confirmedAllocationFailures": 1,
  "succeededLifecycle": 10,
  "succeededCreates": 2,
  "failedLifecycle": 2,
  "failedCreates": 1,
  "confirmedLifecycleAllocationFailures": 1,
  "confirmedCreateAllocationFailures": 0
}
```

The visible failure count depends on the failure-scope checkbox:

- checked: `confirmedAllocationFailures`;
- unchecked: `failed`.

Successes are unchanged by the failure scope.

```text
attempts = succeeded + visible failures
success rate = succeeded / attempts * 100
```

Metric states are:

| State | Display |
| --- | --- |
| Complete with attempts | `{succeeded} ok · {failed} failed · {rate}%` |
| Complete with no included events | `No allocation events` |
| Complete with failures that do not match confirmed-only mode | `No matching events` |
| Incomplete coverage | `Data unavailable` |

Rate styling is green at 95% or above, warning from 80% through less than 95%,
and error below 80%. Sorting by Allocation success orders measured rows by
rate, then attempt count, then VM name; non-measured states follow measured
rows.

`lastAllocation` is the latest included succeeded or failed allocation event
within the report's 30-day query window. `lastFailure` is the latest included
failure in that window regardless of confirmed-only mode. Compact cells show
relative time. Tooltips show the action, full browser-local date and time,
explicit IANA timezone, and error details where applicable.

## Report User Experience

### Header controls

The Allocation Events header contains:

- Refresh;
- Export to CSV;
- 1d, 7d, and 30d period buttons;
- a coverage-period information tooltip;
- **Allocation failures only**, checked by default; and
- **VMs with failures only**, unchecked by default.

The failures-only VM filter uses the currently selected period and failure
scope.

### Local filters and search

Local selectors cover:

- Subscription;
- every enabled Business Context field;
- Resource group;
- Location;
- Deployment;
- VM size; and
- Power state.

Search is case-insensitive and considers visible identity, scope, placement,
configuration, latest allocation, and latest failure fields. Local filters do
not modify collection scope. Reset restores all local options and clears
search.

Global Filters are server-side and reload the report. See
[Global Filters](global-filters.md).

### Table

Columns are:

1. Name
2. Subscription
3. enabled Business Context fields
4. Resource group
5. Region
6. Deployment
7. Size
8. Power state
9. Allocation success
10. Last allocation
11. Last failure
12. Details

Sortable columns use shared table behavior. Initial sorting is VM name
ascending. Page sizes are 20, 50, 100, and 200; 20 is the default.

The Deployment header contains the shared Log/Phy switch. Regional VMs display
**Regional**. Zoned VMs display a logical or physical zone according to the
shared mode. The mode remains synchronized with an open timeline.

### CSV export

CSV export uses all rows remaining after local search, filters, and the
failures-only VM filter, not only the visible page. It records:

- VM identity, subscription, Business Context, group, region, deployment,
  size, and power state;
- selected period and failure scope;
- visible success/failure counts and rate;
- all-failure and confirmed-failure counts; and
- latest allocation and failure action, outcome/code, message, and timestamp.

Count fields are blank when the selected period is incomplete; CSV export does
not convert unavailable coverage into zero events.

## VM Activity Timeline

### Loading and coverage

**View timeline** opens the right-side drawer immediately, then calls the
timeline endpoint. The list endpoint does not preload timeline histories.

The timeline always uses the subscription's full collected coverage,
independent of the 1d/7d/30d report selection and confirmed-only checkbox.

The drawer header contains:

- VM name;
- resource group;
- subscription;
- normalized location display name;
- coverage start and end;
- Complete or stale coverage state; and
- the synchronized Log/Phy control.

### Event order and grouping

Events are sorted from newest to oldest and grouped by the browser-local
calendar date. Every event shows:

- event title;
- outcome;
- full browser-local date and time with explicit IANA timezone;
- smaller muted relative time;
- projected detail values; and
- when present, structured Azure error information.

Adjacent events are connected by a muted vertical rail. The duration between
events is shown next to the rail using the shared human-readable duration
helper.

The first event is a live **Current configuration** snapshot created when the
timeline request succeeds. Historical events come only from:

- `vm-lifecycle`; and
- `vm-configuration`.

### Event labels

Configuration titles are:

- `Creating` -> **VM created**
- `Updating` -> **VM configuration updated**
- delete operation -> **VM deleted**
- other write -> **VM configuration write**

Lifecycle titles use the action name, such as **Start**, **Restart**,
**Reapply**, **Redeploy**, or **Deallocate**.

### Configuration details

Current configuration shows:

- Availability zone;
- VM size;
- Location;
- Power state;
- Provisioning state; and
- Capacity Reservation Group.

Create events show:

- Availability zone;
- VM size;
- Location;
- Provisioning state; and
- Capacity Reservation Group when present.

A create event with an empty or absent zone list displays **Regional**.

Update events show projected mutable configuration values but omit Location and
Availability zone. Their absence cannot be used to infer regional placement.
Locations use Azure Location Reference Data display names.

### Structured error presentation

Failed cards display the parent Azure code and message. A visible
**Allocation failure** badge appears only when `hasAllocationFailure` is true.

When details exist:

- **Details (n)** expands every immediate detail entry;
- each detail shows its code and message; and
- **View details JSON** exposes the complete sanitized details payload.

The compact first-detail fields never replace the parent error and never limit
the JSON disclosure.

### Drawer interaction

- Loading uses stable skeleton rows.
- A request error remains in the drawer with Retry.
- A successful response with no historical operations still shows current
  configuration; when no events can be shown, the drawer uses an explicit empty
  state.
- Escape, the close button, or backdrop dismissal closes the drawer.
- Closing cancels an active timeline request.
- Focus returns to the originating **View timeline** button.
- Opening another request supersedes and aborts the prior request.

## API Contract

### List visible VMs

```http
POST /api/allocation-events/vms
Content-Type: application/json
```

Request:

```json
{
  "subscriptionIds": ["00000000-0000-0000-0000-000000000000"],
  "locations": ["westeurope"],
  "contextFilters": [
    { "fieldKey": "environment", "values": ["Production"] }
  ]
}
```

Each filter may be omitted or `null` according to the shared Global Filter
contract.

Response:

```json
{
  "count": 1,
  "metadata": {
    "schemaVersion": 2,
    "referenceData": {},
    "businessContext": {},
    "activityLog": {
      "periods": ["1d", "7d", "30d"],
      "defaultPeriod": "30d",
      "confirmedFailureCodes": [
        "AllocationFailed",
        "ZonalAllocationFailed"
      ],
      "coverageStart": "2026-08-25T00:00:00Z",
      "coverageEnd": "2026-09-24T00:00:00Z"
    }
  },
  "value": [
    {
      "id": "/subscriptions/.../virtualmachines/vm-1",
      "resourceType": "microsoft.compute/virtualmachines",
      "subscriptionId": "...",
      "subscriptionName": "Subscription",
      "resourceGroup": "rg",
      "name": "vm-1",
      "location": "westeurope",
      "locationDisplayName": "West Europe",
      "logicalZone": "1",
      "physicalZone": "2",
      "vmSize": "Standard_D2s_v5",
      "powerState": "VM running",
      "businessContext": {},
      "allocation": {
        "periods": {},
        "lastAllocation": {
          "action": "Start",
          "outcome": "succeeded",
          "timestamp": "2026-09-24T07:00:00Z"
        },
        "lastFailure": null
      }
    }
  ]
}
```

The endpoint returns only projected report fields. Resource tags and unrelated
Resource Graph properties are not returned.

### VM timeline

```http
POST /api/allocation-events/timeline
Content-Type: application/json
```

Request:

```json
{
  "resourceId": "/subscriptions/.../providers/Microsoft.Compute/virtualMachines/vm-1"
}
```

Response:

```json
{
  "metadata": { "schemaVersion": 1 },
  "coverage": {
    "start": "2026-08-25T08:00:00Z",
    "end": "2026-09-24T08:00:00Z",
    "complete": true,
    "stale": false
  },
  "recipes": {
    "vm-lifecycle": { "version": 3, "current": true },
    "vm-configuration": { "version": 3, "current": true }
  },
  "vm": {},
  "events": [
    {
      "id": "operation-identity-hash",
      "recipe": "vm-lifecycle",
      "type": "lifecycle",
      "action": "Start",
      "timestamp": "2026-09-24T07:00:00Z",
      "outcome": "failed",
      "details": {},
      "error": {
        "code": "ResourceOperationFailure",
        "message": "The resource operation failed.",
        "detailCode": "AllocationFailed",
        "detailMessage": "The VM allocation failed.",
        "detailCount": 1,
        "codes": [
          "ResourceOperationFailure",
          "AllocationFailed"
        ],
        "hasAllocationFailure": true,
        "details": [
          {
            "code": "AllocationFailed",
            "message": "The VM allocation failed."
          }
        ]
      }
    }
  ]
}
```

Invalid filter or resource-ID shapes return HTTP 400. Missing authentication
returns 401, insufficient application role returns 403, an invisible or absent
VM returns 404, and Azure/query failures return 502 with an explicit error
response.

## Loading, Empty, and Error States

- Initial report loading uses shared skeleton styling and keeps controls from
  presenting stale rows as current.
- Refresh clears the prior VM set and records load duration and relative load
  time after success.
- No inventory rows means no VMs are visible in the effective scope.
- No rows after local criteria means no VMs match the selected filters.
- Activity Log coverage absence is distinct from no allocation events.
- Report query errors produce a user notification and do not return a
  success-shaped empty result.
- Timeline loading, empty, authorization, and query failures stay local to the
  drawer.

See [Frontend loading-state convention](frontend-loading-states.md).

## Accessibility and Responsive Behavior

- Sort controls, filters, period buttons, checkboxes, pagination, zone mode,
  export, and timeline actions are keyboard reachable.
- Period buttons expose pressed state and Log/Phy controls use switch semantics.
- Drawer focus moves to Close when opened and returns to the originating action
  when closed.
- Error meaning, outcome, and metric quality are not conveyed by color alone.
- Full timestamps remain visible in timeline text and available as native hover
  text on compact report cells.
- The timeline drawer is bounded to the viewport and becomes full-width on
  narrow screens.
- Table and drawer content wrap or scroll without hiding actions.

## Acceptance Criteria

- The report population is the intersection of Application Scope, caller
  Resource Graph visibility, and Global Filters.
- Current VM inventory fields come from Resource Graph; historical operation
  facts come from retained Activity Log storage.
- Resource IDs, operation names, statuses, error codes, and subscription IDs
  are matched case-insensitively where their semantics require it.
- The report offers 1d, 7d, and 30d periods anchored to each subscription's
  coverage end.
- An incomplete period displays Data unavailable rather than partial metrics.
- Start, Restart, Reapply, and Redeploy are included; Deallocate is excluded
  from allocation metrics.
- VM writes count as creates only when the correlated provisioning state is
  Creating. Updates and deletes do not affect allocation metrics.
- Confirmed-only mode counts only failures containing `AllocationFailed` or
  `ZonalAllocationFailed` anywhere in the recursive Azure error hierarchy.
- `SkuNotAvailable` and other non-capacity failures remain available in
  all-failures mode and are not marked as confirmed allocation failures.
- Metric tooltips expose lifecycle/create source breakdowns and explicit
  browser-local timezone formatting.
- Local failure filtering follows the selected period and failure scope.
- CSV export includes all locally filtered rows and records the selected metric
  semantics.
- Timeline data loads only after View timeline is activated and always spans
  full collected coverage.
- The timeline reauthorizes the exact VM through caller-scoped Resource Graph
  before storage access.
- Events are newest-first, grouped by local date, connected by a muted rail,
  and separated by human-readable durations.
- Current and create cards show normalized Location and Availability zone.
  Update cards show neither.
- An absent create zone is displayed as Regional; an absent update zone does
  not infer placement.
- Parent error, complete details, recursive allocation classification, and raw
  sanitized detail JSON are all available without replacing one another.
- Log/Phy mode is synchronized between report and drawer.
- Loading, empty, error, cancellation, retry, Escape, and focus-restoration
  behavior are implemented.

## Current Limitations

- The current Resource Graph inventory is the authorization and population
  boundary. Failed creates that never produce or no longer retain a visible VM
  resource require a separately designed orphan-attempt report.
- Azure may emit a Failed event without `statusMessage.error`; the feature does
  not invent missing diagnostics.
- Pending operations remain excluded until a terminal stage arrives or
  reconciliation ages them to unknown.
- Coverage can be temporarily unavailable after a collection failure, table
  reset, or recipe-version transition. A successful recollection repairs the
  subscription without changing report compatibility.
