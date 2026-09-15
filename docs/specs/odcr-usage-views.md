# ODCR usage views

## Status

Current implemented frontend and API contract. The implementation checklist at
the end is retained only as a verification summary.

## Goal

Add a view selector to ODCR Usage:

- **By Capacity Reservation** is the current experience and remains the default.
- **By Capacity Reservation Group** presents one row per capacity reservation group (CRG), including live groups that currently contain no reservations.

Both views use one load operation and the same user/app scope. Switching views is client-side and must not issue another HTTP request.

## User experience

### Shared controls

Place a two-option segmented control in the command bar, before **Unused values**:

- **By Capacity Reservation**
- **By Capacity Reservation Group**

The selected view controls the table, chart grouping options, filter fields, empty-state copy, pagination count, and CSV columns. It does not change the globally selected subscription scope or reload data.

The following controls remain shared:

- Refresh and Export to CSV.
- Hours / Cost.
- Include deleted.
- Subscription, Resource group, Location, and Deployment filters.
- The 1-day, 7-day, and 30-day period definitions.

On a view change:

- Reset to page 1.
- Clear a sort whose column does not exist in the destination view.
- Preserve shared filter selections when their values still exist.
- Remove view-specific filter selections and text-search effects.
- If the current chart grouping is unavailable, select the destination view's default chart grouping.

### By Capacity Reservation

This is the existing default view.

Table columns:

1. Capacity reservation (`CRG name / reservation name`)
2. Subscription
3. Resource group
4. Region
5. Deployment, with the logical/physical zone switch
6. Size
7. Reserved quantity
8. Last day unused
9. Last 7 days unused
10. Last 30 days unused

Filters:

- Subscription
- Resource group
- Location
- Capacity Reservation Group
- Deployment (`Regional`, logical zone, or physical zone)

Text search continues to match reservation name, CRG name, and the other displayed fields.

Chart **Group by** options:

- None
- Capacity Reservation Group (default)
- Capacity reservation
- Subscription
- Resource Group
- Location

A deleted reservation is hidden unless **Include deleted** is enabled. Existing chart, table, and CSV behavior otherwise remains unchanged.

### By Capacity Reservation Group

Table columns:

1. Capacity reservation group
2. Subscription
3. Resource group
4. Region
5. Deployment (`Regional` or `Zonal`)
6. Reservation count
7. Total reserved quantity
8. Last day unused
9. Last 7 days unused
10. Last 30 days unused

Definitions:

- **Reservation count** is the number of current reservations returned by Resource Graph. Historical/deleted reservations never increase this count, regardless of **Include deleted**.
- **Total reserved quantity** is the sum of `reservedQuantity` across current reservations only. A live CRG with no current reservations displays `0`; deleted reservations do not contribute because their historical usage records do not preserve quantity.
- Unused daily arrays and the 1/7/30-day counters are element-wise sums across both current and historical reservations associated with the CRG. Missing periods remain unavailable rather than being coerced to zero.
- A live CRG with no current reservations therefore has reservation count `0` and total reserved quantity `0`, but can have non-zero historical unused usage from reservations that were deleted during the displayed period.

Filters:

- Subscription
- Resource group
- Location
- Capacity Reservation Group
- Deployment (`Regional` or `Zonal`)

Keep the Capacity Reservation Group filter for direct selection and chart interactions. The Deployment filter has no logical/physical switch in this view and does not expose individual zones.

Text search matches the CRG name and the other displayed group fields. It does not match child reservation names or sizes.

Chart **Group by** options:

- None
- Capacity Reservation Group (default)
- Subscription
- Resource Group
- Location

The **Capacity reservation** option is removed. Chart values are built from the filtered CRG rows. Clicking a CRG series selects that value in the Capacity Reservation Group filter; it does not modify text search. Clicking the other series applies the corresponding shared filter.

**Include deleted** behavior:

- A live CRG is always shown with the toggle off, including when it has no reservations.
- A CRG is marked **Deleted** when it is absent from the live CRG inventory but is identified through historical reservation usage. No child-status condition is needed.
- The backend pre-marks such a group with `resourceExists: false`; the client does not infer group deletion from its children.
- Enabling the toggle adds those deleted CRGs to the table, chart, filters, and CSV. It does not change reservation count, total reserved quantity, or usage aggregation for CRGs that are already visible.
- A live CRG with zero reservations is not deleted.
- A live CRG containing only historical child reservations is not deleted because the group itself still exists.

CSV export follows the active view and exports all filtered rows, not only the current page.

## API contract

Keep `POST /api/odcr_usage/list` and its current request body. Change the response from one flattened `value` collection to normalized collections:

```json
{
  "schemaVersion": 2,
  "metadata": {
    "usage": {
      "currency": "EUR",
      "dates": ["2026-07-24", "2026-07-25"],
      "coverageStart": "2026-07-24",
      "coverageEnd": "2026-08-22",
      "finalizationLagDays": 2,
      "isAvailable": true
    }
  },
  "counts": {
    "capacityReservationGroups": 12,
    "capacityReservations": 31
  },
  "capacityReservationGroups": [
    {
      "id": "/subscriptions/.../capacityreservationgroups/crg-a",
      "resourceType": "microsoft.compute/capacityreservationgroups",
      "name": "crg-a",
      "subscriptionId": "...",
      "subscriptionName": "Production",
      "resourceGroup": "compute-rg",
      "location": "westeurope",
      "deploymentType": "Zonal",
      "resourceExists": true
    }
  ],
  "capacityReservations": [
    {
      "id": "/subscriptions/.../capacityreservationgroups/crg-a/capacityreservations/cr-a",
      "resourceType": "microsoft.compute/capacityreservationgroups/capacityreservations",
      "name": "cr-a",
      "capacityReservationGroupId": "/subscriptions/.../capacityreservationgroups/crg-a",
      "logicalZone": "1",
      "physicalZone": "2",
      "vmSize": "Standard_D4s_v5",
      "reservedQuantity": 4,
      "resourceExists": true,
      "unusedHours": [0.0, 24.0],
      "unusedCost": [0.0, 3.25],
      "lastDayUnusedHours": 24.0,
      "lastDayUnusedCost": 3.25,
      "last7DaysUnusedHours": 48.0,
      "last7DaysUnusedCost": 6.5,
      "last30DaysUnusedHours": 48.0,
      "last30DaysUnusedCost": 6.5
    }
  ]
}
```

Contract rules:

- All IDs and subscription IDs are normalized to lowercase.
- CRG-owned fields (`name`, subscription, resource group, location, deployment type) live only on the CRG object. The client joins reservations to groups by `capacityReservationGroupId`; it does not infer or repeat those fields from reservations.
- `deploymentType` is `Regional` when the CRG `zones` property is null or empty, otherwise `Zonal`. Do not return the allowed-zone list because this view only needs the classification.
- Every reservation references a CRG returned in `capacityReservationGroups`, including historical-only reservations. The backend synthesizes a historical CRG identity from the reservation ARM ID when the live CRG query has no matching row.
- A synthesized historical CRG has `resourceExists: false`, `deploymentType: null`, and any unavailable inventory fields as null. Its stable identity fields come from the ARM ID and historical reservation data.
- `resourceExists` on a CR continues to mean that reservation exists in Resource Graph. `resourceExists` on a CRG is the backend-owned deletion marker: `true` when the group exists in the CRG Resource Graph result and `false` when it was synthesized from historical usage. The UI displays the deleted badge directly from this field.
- Do not return pre-aggregated group usage rows. Returning normalized source sets prevents duplicated 30-day arrays and lets the client derive current inventory metrics separately from historical usage while **Include deleted** controls only group-row visibility.

For a short migration window, the endpoint may retain `value` as an alias of `capacityReservations`; remove it once the SPA and tests consume the v2 fields. `count` should be replaced by the explicit `counts` object because one scalar is ambiguous with two entity sets.

## Resource Graph queries

Run two independent Resource Graph queries in parallel with `ThreadPoolExecutor(max_workers=2)`, following the coverage endpoint pattern. Both queries use the same signed-in user token and effective subscription scope. Only the CRG query applies the location predicate because location is CRG-owned; reservation rows are admitted by matching them to the location-scoped CRG result.

The effective subscriptions are sent through Resource Graph's top-level
`subscriptions` request property. They are not repeated as KQL predicates.
An explicit empty scope returns no rows without sending a Resource Graph
request. This differs intentionally from ODCR Coverage, where the VM query is
scoped but its companion reservation lookup remains unscoped so a VM can
resolve a capacity reservation shared from another subscription.

### Capacity reservation groups

```kusto
resources
| where type =~ 'microsoft.compute/capacityreservationgroups'
| where location in~ (...)
| extend deploymentType = iff(isnull(zones) or array_length(zones) == 0, 'Regional', 'Zonal')
| join kind=leftouter (
    resourcecontainers
    | where type =~ 'microsoft.resources/subscriptions'
    | project subscriptionId, subscriptionName = name
) on subscriptionId
| project id = tolower(id),
    resourceType = tolower(type),
    name,
    subscriptionId = tolower(subscriptionId),
    subscriptionName,
    resourceGroup,
    location = tolower(location),
    deploymentType,
    resourceExists = true
| order by id asc
```

The production query builder retains escaping for the optional Location
predicate. Subscription scope belongs to the Resource Graph request envelope.

### Capacity reservations

```kusto
resources
| where type =~ 'microsoft.compute/capacityreservationgroups/capacityreservations'
| extend capacityReservationGroupId = tolower(substring(id, 0, indexof(tolower(id), '/capacityreservations/')))
| project id = tolower(id),
    resourceType = tolower(type),
    name,
    capacityReservationGroupId,
    logicalZone = tostring(zones[0]),
    vmSize = tostring(sku.name),
    reservedQuantity = toint(sku.capacity),
    resourceExists = true
| order by id asc
```

Remove the current nested CRG and subscription joins from the reservation query. Group location and subscription display data come from the CRG collection, avoiding repeated values and making empty groups possible.

## Backend reconciliation

Implement the endpoint in this order:

1. Resolve user identity, app scope, effective subscriptions, and effective locations exactly as today.
2. Acquire the user ARM token once.
3. Build and run the CRG and CR queries in parallel. Record separate `capacity_reservation_group_resource_graph`, `capacity_reservation_resource_graph`, and combined `resource_graph` timings.
4. Keep live reservation rows only when `capacityReservationGroupId` matches a returned CRG. This makes the CRG query authoritative for location scope and avoids returning a child whose parent is outside that scope.
5. Load zone mappings for live reservation `(subscriptionId, location)` pairs. Because location now belongs to the CRG row, join CRs to CRGs before creating mapping pairs.
6. Load historical usage once with `cr_usage.usage_view(effective.subscription_ids)`.
7. Preserve the existing RBAC check for historical-only subscriptions. Do not expose historical records from subscriptions the user cannot currently access.
8. Reconcile live reservations with historical usage by normalized reservation ID. Live reservations receive usage metrics or `empty_usage(metadata)` and `resourceExists: true`.
9. Append accessible historical-only reservations with `resourceExists: false`. When an effective location scope is active, omit historical-only reservations whose parent CRG is absent because their historical summary does not store a trustworthy location; fail closed rather than assigning them to an unverified region.
10. Ensure every remaining reservation parent exists in the CRG map. Synthesize one historical CRG for missing parents, merging stable subscription/resource-group/group-name identity from `cr_usage._resource_identity()` data.
11. Sort both collections by normalized ID and serialize the v2 response.

Fail the entire request if either Resource Graph query fails. A partial inventory would make empty groups and deletion status misleading.

## Frontend derivation

Store the response as `odcrUsageGroups` and `odcrUsageReservations`, plus maps keyed by normalized ID.

For reservation view:

- Join each reservation to its CRG for subscription, resource group, location, group name, and group deployment context.
- Preserve reservation-level zone deployment display and physical-zone enrichment.
- Apply the existing local filters, search, sorting, chart, pagination, and CSV behavior.

For group view:

1. Start with all live CRGs.
2. Add synthesized deleted CRGs only when **Include deleted** is enabled.
3. Attach child reservations by normalized `capacityReservationGroupId`.
4. Derive reservation count and total reserved quantity from live child reservations only.
5. Derive daily usage arrays and rolling counters from all current and historical child reservations, independently of **Include deleted**.
6. Apply group-view filters and text search to the derived rows.
7. Feed those same filtered rows to chart, table, pagination, and CSV so all surfaces agree.

Use separate sort and page state per view if users are expected to switch repeatedly; otherwise reset them on every switch. Separate state is the preferred behavior because it preserves context without leaking invalid columns.

## Test plan

Backend unit tests:

- The CRG query applies escaped subscription and location filters; the CR query applies only the escaped subscription filter.
- CRG query projects `Regional`/`Zonal`; CR query projects parent ID and reservation fields without duplicated CRG joins.
- Endpoint launches two graph calls with the same user token and waits for both.
- Live empty CRG is returned.
- Live CR and historical usage reconcile by normalized ID.
- Historical-only CR creates one synthesized deleted CRG.
- Multiple historical CRs under one missing parent create one synthesized CRG.
- Historical-only subscriptions retain the current user-access check.
- A location-scoped response omits historical-only groups whose location cannot be established.
- Either graph-query failure returns the stable 502 response and no partial payload.
- Explicit empty scope performs neither graph query and returns two empty collections.

Frontend behavior tests:

- Default view remains **By Capacity Reservation**.
- Switching views performs no fetch.
- Group aggregation sums current and historical daily hours/cost and rolling counters while counting only current children.
- Empty live CRG displays zero current reservations, can retain historical usage, and is never marked deleted.
- Live CRG with only deleted children remains live.
- A CRG absent from live inventory but identified through history is backend-marked deleted, hidden by default, and shown when included.
- Group view has no Size column, reservation chart grouping, or logical/physical switch; it retains the CRG dropdown.
- Clicking a CRG chart series selects the CRG dropdown value without changing text search.
- Group text search matches CRG names but not child reservation names (subject to the clarification above).
- Active-view CSV headers and values match the displayed model.
- Shared filters, chart totals, table totals, and pagination all operate on the same filtered row set.

Browser verification:

- Desktop and mobile layouts keep the view selector and existing controls readable without overlap.
- A CRG with zero reservations appears in group view.
- Toggle repeatedly between views and verify filters, chart grouping, table headers, counts, deleted badges, and CSV.
- Confirm the endpoint `Server-Timing` exposes both parallel query timings and total Resource Graph wall time.

## Implementation verification

- CRG and CR query builders and reconciliation helpers have focused unit tests.
- The endpoint runs parallel queries and returns the normalized v2 response.
- The frontend keeps normalized reservation and group state and switches views
  without another request.
- Group derivation, filters, chart options, table rendering, and CSV export use
  the active view.
- Desktop and mobile behavior is covered by frontend and browser verification.
