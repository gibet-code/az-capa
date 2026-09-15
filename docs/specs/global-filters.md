# Global Filters

## Status

Current implemented contract, including App Scope integration. Last verified on
2026-09-15.

Management Group is not an end-user Global Filter. It remains an
administrator-owned application-scope mechanism.

## Purpose

Global Filters apply one server-side scope to ODCR Coverage and ODCR Usage.
Users may independently constrain Subscription, Location, and any enabled
Business Context field. They do not modify collection scope or saved settings.

## Global Filters Card

The header **Edit global filters** button opens an anchored card and shows an
active indicator whenever at least one criterion is applied.

The card contains:

- Subscription, backed by Azure Subscription Reference Data.
- Location, backed by Azure Location Reference Data.
- One selector for each enabled Business Context field.
- A refresh action that rereads the published catalogue.
- Save and Cancel actions.

Each selector has an independent enable switch. Enabled selectors expose search
and checkbox options. Multiple selectors may be expanded simultaneously. Each
option list keeps its natural height up to 240 pixels on desktop and 150 pixels
on narrow viewports, then scrolls independently. The card body remains a
viewport safety fallback while the header and footer stay fixed.

Subscription labels are Azure display names and values are subscription IDs.
Location labels are Azure display names such as `West Europe` and values are
canonical ARM names such as `westeurope`. Context options use semantic values
and show visible subscription counts. JSON `null` is **Not mapped**.

Opening the card creates a draft copy. Cancel or clicking outside discards the
draft. Save replaces the applied criteria, closes the card, and reloads the
active report. The inactive report is invalidated and reloads when opened.

Save is disabled while the catalogue is unavailable or loading, and when an
enabled selector has no values selected.

## State and Semantics

Applied and draft state use this shape:

```json
{
  "subscriptionIds": null,
  "locations": ["westeurope"],
  "contextFilters": [
    { "fieldKey": "environment", "values": ["Production", "Development"] },
    { "fieldKey": "it_organization", "values": ["IT INFRASTRUCTURE"] }
  ]
}
```

`null` means Subscription or Location is unrestricted. A Context field absent
from `contextFilters` is unrestricted. The browser stores semantic Context
criteria rather than materialized Context-derived subscription IDs.

Applied criteria are retained in browser `localStorage` after Save and restored
after identity resolution, before the first report request. Only committed
criteria are retained; an open card's draft edits remain in Alpine component
memory and are discarded by Cancel, dismissal, or page reload. Storage is
partitioned by tenant and user in Azure and uses a dedicated local-development
partition. It is not written to cookies, the URL, or the backend. See
[Frontend Preference Store](frontend-preferences.md).

Effective population:

```text
configured application scope
AND signed-in user visibility
AND selected subscriptions, when present
AND selected locations, when present
AND Context field A criterion
AND Context field B criterion
```

Values within one Context field are ORed. Different Context fields are ANDed.
Context comparisons are case-insensitive. JSON `null` selects missing mappings
and remains distinct from literal strings such as `"Unknown"`.

Unknown, disabled, duplicate, malformed, or empty Context criteria fail closed.
A valid combination that matches nothing returns zero rows.

## Report Request Contract

Both reports receive the same optional POST properties:

```json
{
  "subscriptionIds": ["00000000-1111-2222-3333-444444444444"],
  "locations": ["westeurope"],
  "contextFilters": [
    { "fieldKey": "environment", "values": ["Production", null] }
  ]
}
```

| Property | Omitted or `null` | Explicit `[]` |
| --- | --- | --- |
| `subscriptionIds` | Unrestricted inside permitted scope | Zero rows |
| `locations` | Unrestricted inside configured scope | Zero rows |
| `contextFilters` | No Context restriction | No Context restriction |

The browser omits unrestricted properties. Explicit empty Subscription or
Location arrays retain fail-closed behavior for non-UI callers.

| Report | Endpoint |
| --- | --- |
| Coverage | `POST /api/odcr/coverage` |
| Usage | `POST /api/odcr_usage/list` |

Legacy Coverage GET query filters remain supported. A blank legacy query value
is unrestricted for compatibility.

## Catalogue Contract

`GET /api/global-filters/catalogue` returns data visible to both the signed-in
user and the configured application boundary:

```json
{
  "schemaVersion": 1,
  "subscriptions": [
    {
      "value": "00000000-1111-2222-3333-444444444444",
      "label": "Production France",
      "secondaryLabel": "00000000-1111-2222-3333-444444444444"
    }
  ],
  "locations": [
    { "value": "westeurope", "label": "West Europe", "secondaryLabel": "westeurope" }
  ],
  "contextFields": [
    {
      "fieldKey": "environment",
      "name": "Environment",
      "values": [
        { "value": "Production", "label": "Production", "subscriptionCount": 12 },
        { "value": null, "label": "Not mapped", "subscriptionCount": 2 }
      ]
    }
  ],
  "metadata": {}
}
```

The endpoint queries user-visible subscription inventory, intersects it with
application scope, and projects pinned Subscription, Location, and Business
Context snapshots. It never exposes the complete backend subscription catalogue
or per-subscription Context mappings.

Catalogue reads and the card refresh do not call Azure source APIs. Source
refresh belongs to Data Collection operations.

## Request Dimensions Consistency

Each report opens one capability-selected
`services/request_dimensions.py::RequestDimensionsView` with Subscription,
Location, and Business Context capabilities.

The view lazily pins one immutable generation per capability for the request.
Context filtering and row enrichment use the same pinned Context generation.
Subscription and Location generations may differ because display metadata does
not determine Context membership.

The view intersects Subscription and Location criteria with application scope,
applies Context criteria, enriches rows with `subscriptionName`,
`locationDisplayName`, and `businessContext`, and returns generation/freshness
metadata. Normal report reads use published snapshots and never run an Azure
Reference Data refresh pipeline. A stale published catalogue remains usable.
Missing Subscription or Location presentation metadata uses the canonical
identifier fallback. Invalid persisted data remains an error rather than being
repaired implicitly during a read.

The only read-time Reference Data initialization is outside this view: when App
Scope resolution requires the Subscription catalogue and no current
Subscription manifest exists, App Scope synchronously invokes the normal
Subscription refresh pipeline. This absent-only behavior does not apply to
Location Reference Data, stale catalogues, or ordinary report and Global
Filters reads. See [Application Scope](app-scope.md#subscription-snapshot-initialization).

## Interaction With Local Filters

Global Filters limit rows returned by the server. Local filters narrow those
returned rows in the browser.

Local options are rebuilt from returned rows. For example, a global IT
Organization restriction to `IT INFRASTRUCTURE` yields only
`IT INFRASTRUCTURE` in the matching local filter. Dynamic Context filter objects
retain identity across reloads so Alpine dropdowns receive the reseeded options.

Only the corresponding local dimension is marked:

- Global Subscription marks local Subscription.
- Global Location marks local Location.
- Global Context marks the local Context filter with the same Field key.

When that local filter selects every returned option, `all` is shown as `all*`.
Its menu shows **(*) Global filters in effect.** and an Edit action. Unrelated
local filters are not marked.

Canonical IDs, canonical Location names, and Context values remain matching
keys. Presentation uses Azure display names in tables, details, local Location
labels, Coverage Location dimensions, Usage Location chart series, search, and
CSV exports. Canonical Location values remain request and interaction keys.

## Identity and Errors

The backend identity resolves application scope and reads application-owned
snapshots. The signed-in user identity supplies Resource Graph visibility. A
missing user identity never falls back to Managed Identity.

Coverage VM inventory and both Usage inventory queries send the effective
Global Filter subscriptions through Resource Graph's built-in request scope.
Coverage's companion capacity-reservation lookup remains unscoped so
cross-subscription shared reservations can be reconciled with scoped VMs.

Invalid filters return `400 invalid_filter`; missing authentication returns
`401`; scope or catalogue failures return fail-closed `502` responses. Browser
loaders abort preceding requests and ignore stale responses by request sequence.

## Current Limitations

- Retained criteria do not roam across browsers or devices and do not update
  other open tabs until those tabs reload.
- There is no dedicated Clear All action; users disable selectors and Save.
- If an applied Context field is deleted, backend matching remains fail-closed,
  and the current card has no unavailable-field recovery row. Retention
  preserves the stale criterion until the user saves different criteria or
  clears browser site data.
- Catalogue option counts do not cascade as other draft dimensions change.

## Verification

Primary automated coverage:

- `tests/core/test_app_scope.py`
- `tests/services/test_request_dimensions.py`
- `tests/services/test_odcr_coverage.py`
- `tests/services/test_odcr_usage.py`
- `tests/integration/test_report_model.py`
- `tests/integration/test_web_assets.py`

Browser checks cover the card, per-dimension markers, returned-row option
reseeding, independent bounded option-list scrolling, and canonical/display
Location behavior.
