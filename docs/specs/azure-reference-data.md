# Azure Reference Data

## Status

Current implemented architecture. Subscription and Location catalogues use a
refresh-only lifecycle and integrate with App Scope. Last verified on
2026-09-15.

Azure Reference Data owns native Azure identifier-to-display metadata. The
implemented catalogues are Subscription and Location. They are independent from
Business Context, Zone Mapping, and each other.

## Goals and Boundaries

- Keep canonical Azure identifiers as API, filtering, join, persistence, and
  authorization values.
- Present consistent Azure display names with deterministic canonical fallback.
- Refresh each typed catalogue independently without blocking report reads.
- Pin immutable generations through capability-selected request views.
- Avoid report-specific joins whose only purpose is display-name lookup.

Azure Reference Data is not editable Business Context. Display names are not
authorization inputs. Missing metadata never becomes Business Context **Not
mapped**; it falls back to the canonical identifier.

## Subscription Catalogue

Subscription IDs are trimmed and lowercased. A snapshot has this logical shape:

```json
{
  "schemaVersion": 1,
  "kind": "subscriptions",
  "generation": "01J...",
  "publishedAt": "2026-08-28T10:00:00Z",
  "sourceUpdatedAt": "2026-08-28T09:59:58Z",
  "values": {
    "00000000-1111-2222-3333-444444444444": {
      "displayName": "Production France",
      "state": "Enabled",
      "managementGroupAncestors": [
        { "id": "france", "displayName": "France" },
        { "id": "europe", "displayName": "Europe" }
      ]
    }
  }
}
```

The source is Resource Graph subscription inventory queried with the backend
identity. The catalogue contains subscriptions visible to that identity and is
never exposed wholesale to ordinary users.

The normalized Management Group chain preserves immutable IDs and display names
in Resource Graph order: immediate parent first, then ancestors toward tenant
root. App-scope resolution uses the Subscription snapshot keys and ancestor
chains as the backend identity's authoritative application boundary.

## Location Catalogue

Canonical keys are lowercase ARM location names. The primary label is Azure
`displayName`; `regionalDisplayName` is retained but not used as the default
label.

```json
{
  "schemaVersion": 1,
  "kind": "locations",
  "generation": "01J...",
  "publishedAt": "2026-08-28T05:15:00Z",
  "sourceSubscriptionId": "00000000-1111-2222-3333-444444444444",
  "values": {
    "westeurope": {
      "displayName": "West Europe",
      "regionalDisplayName": "(Europe) West Europe"
    }
  }
}
```

Refresh resolves application scope and chooses the first subscription ID in
canonical sorted order. It calls the subscription-scoped Azure Locations API
and follows pagination. Only physical regions are published. Entries without a
usable canonical name are discarded; blank display names fall back to the
canonical name.

Location Reference Data uses explicit client projections:

- `fetch_location_catalogue()` and `map_location_catalogue_entries()` preserve
  display metadata for this catalogue.
- `fetch_zone_mapping_locations()` and `map_zone_mapping_regions()` retain the
  separate Zone Mapping interpretation.

A Location refresh failure does not invoke Zone Mapping, try a second source
subscription, or replace the previous successful Location generation.
If App Scope contains no subscriptions, Location refresh makes no Azure request,
preserves the previous generation, and completes as a successful no-work
operation because there is no in-scope resource requiring Location enrichment.

## Canonical and Display Contract

Report rows preserve stable values and friendly labels separately:

```json
{
  "subscriptionId": "00000000-1111-2222-3333-444444444444",
  "subscriptionName": "Production France",
  "location": "westeurope",
  "locationDisplayName": "West Europe"
}
```

Fallback:

```text
subscriptionName = catalogue displayName or subscriptionId
locationDisplayName = catalogue displayName or location
```

Filter options use:

```json
{
  "value": "westeurope",
  "label": "West Europe",
  "secondaryLabel": "westeurope"
}
```

Canonical `value` is the only value sent to APIs. Options are deduplicated by
canonical value and sorted by label. A label change does not change selection
identity.

Coverage and Usage use Location display names in tables, detail drawers, local
filter labels, report/chart Location group labels, search, and CSV exports.
Canonical names remain local-filter values, report grouping keys, and server
request values.

## Independent Refresh Operations

Each catalogue has its own timer, Durable roots, operation gate, service
pipeline, persisted prefix, status, L1 cache entry, and failure lifecycle.

| Catalogue | Source | Deployed schedule | Stale threshold |
| --- | --- | --- | --- |
| Subscription | Resource Graph inventory | Every 15 minutes | 60 minutes |
| Location | Azure Locations API | Daily at 05:15 UTC | 48 hours |

Schedules and thresholds are application settings, not API guarantees.

Administrative routes:

| Method and route | Purpose |
| --- | --- |
| `POST /api/reference_data/subscriptions/refresh` | Start Subscription refresh |
| `GET /api/reference_data/subscriptions/status` | Subscription data/operation status |
| `POST /api/reference_data/locations/refresh` | Start Location refresh |
| `GET /api/reference_data/locations/status` | Location data/operation status |

Timers and manual starts use the same deterministic orchestrators. Activities
are thin adapters to `services/reference_data.py`. Shared helpers provide
bounded retry timers for transient activity outcomes. An active refresh or
blocks another refresh for the same catalogue, but never blocks the other
catalogue.

Every successful refresh retrieves and validates the complete source, writes
one immutable generation, and then replaces the current manifest. A failed
attempt preserves the last successful generation and records a sanitized error.
Reports continue with the current snapshot or canonical fallback.

Normal report and Global Filters catalogue reads do not call Azure Reference
Data sources and do not synchronously refresh either catalogue. Stale published
generations remain usable until a scheduled or administrative refresh replaces
them. Missing presentation metadata uses the canonical identifier fallback;
invalid persisted data remains an error rather than triggering implicit repair.

There is one read-time initialization exception: when App Scope resolution
requires Subscription Reference Data and no current Subscription manifest
exists, App Scope invokes the normal Subscription refresh pipeline
synchronously and then reopens the published snapshot. This is absent-only:
staleness, a published empty Subscription snapshot, or a Management Group with
no matches does not trigger refresh. Location Reference Data is never
initialized by App Scope or a report read. See
[Application Scope](app-scope.md#subscription-snapshot-initialization).

## Persistence and Cache

Blob container `azure-reference-data` contains independent prefixes:

```text
subscriptions/snapshots/{generation}.json.gz
subscriptions/current.json
locations/snapshots/{generation}.json.gz
locations/current.json
```

`ReferenceDataStore.publish()` writes compressed immutable generation data
before the manifest. The target administration surface does not delete current
catalogues. A successful refresh publishes a complete replacement generation;
a failed refresh preserves the prior generation.

Each Function worker has one L1 entry per kind containing snapshot, generation,
manifest ETag, and local expiry. `AZURE_REFERENCE_DATA_L1_TTL_SECONDS` defaults
to 60 seconds.

- Before expiry, readers perform no Blob operation.
- After expiry, one reader conditionally revalidates the manifest.
- An unchanged ETag renews local expiry without downloading the generation.
- A changed ETag downloads and validates the new generation before swapping L1.
- Per-kind single-flight loading prevents duplicate worker refreshes.
- Publication updates the executing worker immediately.
- Other workers observe publication after at most their L1 interval.

The L1 interval controls Blob revalidation, not source freshness. A
request-scoped view keeps its already pinned generation even if L1 changes.

## Typed Read Views

`services/reference_data.py::ReferenceDataView` is opened with an explicit set
of catalogue kinds. Unrequested kinds are neither fetched nor decoded. Kinds
load lazily and pin their generation on first use.

It provides typed operations including:

```python
view.subscription_name(subscription_id)
view.location_display_name(location)
view.enrich_rows(rows)
view.options(kind, allowed_values)
view.metadata()
```

`services/request_dimensions.py::RequestDimensionsView` combines requested
Reference Data capabilities with an independently pinned Business Context
snapshot. Reference generations may differ from each other and from Context.
Context filtering and enrichment still use one common Context generation.

## Report and Global Filter Integration

Coverage and Usage request Subscription, Location, and Business Context
capabilities. After user-scoped Resource Graph queries, the Request Dimensions
view enriches final rows and returns freshness metadata:

```json
{
  "referenceData": {
    "subscriptions": {
      "generation": "01J...",
      "publishedAt": "2026-08-28T10:00:00Z",
      "stale": false
    },
    "locations": {
      "generation": "01J...",
      "publishedAt": "2026-08-28T05:15:00Z",
      "stale": false
    }
  }
}
```

`GET /api/global-filters/catalogue` projects Subscription and Location options
from the pinned views. Subscription options are restricted to the intersection
of user visibility and application scope. Location options are restricted to
configured application locations when present. Reference Data remains
presentation/query metadata; Resource Graph under the signed-in user's identity
is authoritative for resource visibility.

## Administration and Notifications

Settings > Data Collection contains independent **Subscription Reference Data**
and **Location Reference Data** sections. Each provides Refresh, data
existence, item count, generation, publication time, stale state, active
operation, progress, and Durable instance ID.

Root refresh instances use stable labels in Notifications. Activities are not
presented as separate user operations.

## Settings

```text
AZURE_REFERENCE_DATA_BLOB_CONTAINER=azure-reference-data
AZURE_REFERENCE_DATA_L1_TTL_SECONDS=60
SUBSCRIPTION_REFERENCE_REFRESH_SCHEDULE=0 */15 * * * *
SUBSCRIPTION_REFERENCE_MAX_STALE_MINUTES=60
LOCATION_REFERENCE_REFRESH_SCHEDULE=0 15 5 * * *
LOCATION_REFERENCE_MAX_STALE_HOURS=48
```

The deployment creates the private Blob container and supplies all settings.

## Security and Failure Behavior

- Reference Data never broadens application scope or user RBAC.
- Ordinary users cannot enumerate the complete backend Subscription catalogue.
- Missing catalogues and unknown keys fall back to canonical identifiers.
- Refresh failure never makes Coverage or Usage unavailable.
- Tokens, storage credentials, raw Azure errors, and complete backend
  catalogues are not returned or logged for ordinary users.

## Future Improvements

- Record a non-secret backend identity fingerprint in Subscription snapshot
  source metadata. A future design can use it to detect snapshots published by
  a previous managed identity or local Azure CLI identity. For now, identity
  changes do not invalidate an existing snapshot; the next scheduled or
  administrative refresh replaces it using the current backend identity.

## Verification

Primary automated coverage:

- `tests/clients/test_azure_locations.py`
- `tests/services/test_reference_data.py`
- `tests/storage/test_reference_data_store.py`
- `tests/functions/test_reference_data_blueprints.py`
- `tests/services/test_request_dimensions.py`
- `tests/services/test_odcr_coverage.py`
- `tests/services/test_odcr_usage.py`
- `tests/integration/test_report_model.py`
- `tests/integration/test_web_assets.py`
