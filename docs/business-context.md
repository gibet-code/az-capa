# Business Context

## Status

Implemented current-state specification as of 2026-08-28.

Normal reads are snapshot-only. Dynamic mappings refresh proactively through
Durable Functions.

Implemented Value sources are Manual CSV/XLSX, Management Group hierarchy, and
Subscription tag. MCA invoice section remains intentionally deferred.

## Purpose and Ownership

Business Context defines subscription-keyed classifications such as Environment,
Business Unit, IT Organization, or Cost Center. Native Subscription and Location
display metadata belongs to Azure Reference Data, not Business Context.

The user-facing feature and Settings tab are named **Business Context**. One
configured item is a **Context field**. Backend modules use Subscription Context
where the subscription-keyed nature must be explicit.

## Context Field Contract

```json
{
  "key": "environment",
  "name": "Environment",
  "enabled": true,
  "valueSource": {
    "type": "subscription_tag",
    "version": 1,
    "config": { "tagKey": "Environment" }
  },
  "revision": 4,
  "publishedGeneration": "01J...",
  "updatedAt": "2026-08-28T12:01:00Z",
  "updatedBy": { "tenantId": "...", "objectId": "...", "displayName": "..." }
}
```

Field keys are normalized to lowercase, match
`^[a-z][a-z0-9_]{1,31}$`, remain immutable while a field exists, and are unique.
Reserved keys include `subscription_id`, `subscription_name`, `resource_group`,
`location`, and `unknown`. Hard deletion makes a key reusable.

Names are trimmed, 1-80 characters, and case-insensitively unique. Values are
trimmed strings of 1-256 characters. Empty strings are not mapped values.
`SUBSCRIPTION_CONTEXT_MAX_FIELDS` defaults to 5 and is capped at 20.

Updates require the current revision. Stale updates return HTTP 409.

## Missing Values

The stored sentinel is JSON `null`, never a display string:

```json
{
  "environment": "Production",
  "business_unit": "Unknown",
  "it_organization": null
}
```

`"Unknown"` is intentional. `null` is displayed as **Not mapped**. CSV may use
an empty cell for `null`. Source-wide errors fail resolution rather than turning
every subscription into a missing value.

Resolvers may report `no_source_value`, `tag_not_present`,
`hierarchy_too_shallow`, and `subscription_not_in_snapshot` reasons.

## Value Sources

### Manual File

Manual sources accept UTF-8 CSV and `.xlsx` with `SubscriptionId` and `Value`
columns. Column names are case-insensitive after trimming. Blank Value cells are
Not mapped. Validation rejects malformed GUIDs, duplicate IDs, formulas,
unsupported shapes, the all-zero ID, and configured size/row limit violations.

Valid mappings for currently inaccessible subscriptions are retained with a
warning. Creation templates contain accessible subscriptions with empty values;
editing can download a prefilled template. The normalized source artifact is
retained. Scheduled refresh does not select manual fields; manual refresh
rematerializes them from that artifact.

### Management Group Hierarchy

Levels 1-6 are relative to tenant root; root is level 0 and is not a value.

- `selected_level` returns the selected level display name. A shorter chain is
  Not mapped with `hierarchy_too_shallow`.
- `hierarchy_path` returns the available path through the selected level,
  separated by ` > `, and accepts a shorter available chain.

Immutable Management Group IDs remain in diagnostics. A subscription with no
non-root path fails the source operation as
`management_group_path_unavailable`.

### Subscription Tag

The config stores one non-empty tag key of at most 512 characters. Matching is
case-insensitive. Missing or blank values are Not mapped with
`tag_not_present`. The UI offers up to 15 keys ranked by in-scope subscription
count and then name, while allowing free text.

### MCA Invoice Section

`mca_invoice_section` is deferred and is not advertised as an available source.

## Preview and Save

Preview is a bounded synchronous administration operation:

1. Submit the complete draft and source artifact.
2. Validate and resolve with the backend identity.
3. Return in-scope/outside-scope coverage, top values, warnings, diagnostics,
   and missing reasons without persistence.
4. Enable Save for that exact draft; any input change invalidates the gate.
5. Save independently validates and resolves again.
6. Persist the definition and atomically publish its mapping before success.

A failed save leaves the previous definition and generation unchanged. A
successful zero-coverage preview is valid and visibly reported.

## Proactive Refresh

Dynamic fields use `SUBSCRIPTION_CONTEXT_REFRESH_SCHEDULE`, deployed every 15
minutes. `SUBSCRIPTION_CONTEXT_MAX_STALE_MINUTES` defaults to 20. Staleness is
observable and does not block latest-successful-snapshot reads.

The timer starts no Durable instance when no enabled dynamic field exists.
Scheduled runs select enabled Management Group and Subscription Tag fields.
Manual refresh selects every enabled field, including manual mappings.

One deterministic Durable root calls one thin service activity. The service:

1. Requires the Durable instance ID as `refreshRunId`.
2. Acquires the shared Business Context operation lease.
3. Rereads definitions and selects work.
4. Loads backend subscription inventory once when needed.
5. Processes selected fields sequentially in memory.
6. Rechecks expected definition revisions.
7. Publishes at most one immutable generation.
8. Releases the lease in `finally`.

Mappings never enter Durable history. Inventory failure receives bounded Durable
timer retries with 5, 10, and 20 second backoff. Publication is idempotent by
`refreshRunId`.

### Partial Failure Policy

- Successful fields contribute new mappings.
- A failed field with a previous successful mapping retains it and is stale; one
  partial generation may still publish.
- If any failed enabled field lacks a previous successful mapping, no generation
  is published.

Readers therefore observe the complete prior generation or one complete new
generation, never per-field intermediate generations.

## Concurrency, Flush, and Persistence

Refresh, flush, Save publication, and deletion serialize through one Blob lease
at `locks/operation`. Durable status checks provide fast UI/HTTP conflict
behavior, but the Blob lease is authoritative. Revision checks prevent refresh
from overwriting a newer administrator change.

Flush removes generated snapshots, current manifest, and worker L1 state while
preserving definitions and normalized manual source artifacts. It calls no Azure
source and does not automatically refresh.

Azure Table Storage holds current definitions, revisions, source references,
and update audit metadata. Blob container `subscription-context` holds:

```text
sources/{fieldKey}/{sourceRevision}.json.gz
snapshots/{generation}.json.gz
current.json
locks/operation
```

Publication writes the compressed immutable generation before replacing the
current manifest.

## Snapshot-Only Reads and Request Dimensions

Normal resolution reads `SubscriptionContextStore` snapshots. It does not query
Azure, refresh sources, acquire a refresh lease, or wait for refresh. A report
batch loads at most one snapshot and makes no per-subscription storage calls.

`services/request_dimensions.py::RequestDimensionsView` requests the
`business_context` capability and pins one Context generation. The same snapshot
supplies field metadata, Global Filter matching, row enrichment, and response
metadata:

```json
{
  "businessContext": {
    "generation": "01J...",
    "publishedAt": "2026-08-28T12:01:00Z",
    "fields": { "environment": "Environment" }
  }
}
```

Each report row has every enabled Field key materialized as a string or `null`
under `businessContext`. The frontend reads `businessContext.fields`; generation
metadata is not a report column.

Coverage and Usage provide dynamic columns, row-derived local options,
case-insensitive filtering, search, sorting, and CSV export. Coverage report
dimensions and Usage chart grouping include enabled Context fields. Context
Global Filters use immutable Field keys, OR values within a field, AND fields,
and support JSON `null`.

## Administration and Operations

The Settings **Business Context** tab supports list, create, edit,
enable/disable, Preview, Save, templates, upload, and hard delete.

The Data Collection **Business Context** section provides Refresh, confirmed
Flush, generation, publication time, field counts, stale state, active operation,
and Durable instance ID. Root operations appear in Notifications.

| Method and route | Purpose |
| --- | --- |
| `GET/POST /api/subscription-context` | List or create fields |
| `GET/PATCH/DELETE /api/subscription-context/{fieldKey}` | Read/update/delete |
| `POST /api/subscription-context/preview` | Preview without persistence |
| `GET /api/subscription-context/templates/manual` | CSV/XLSX template |
| `GET /api/subscription-context/value-source-options/tags` | Ranked tag keys |
| `POST /api/subscription-context/resolve` | Authorized batch resolution |
| `POST /api/subscription-context/refresh` | Start manual Durable refresh |
| `GET /api/subscription-context/status` | Data and operation status |
| `POST /api/subscription-context/flush` | Start Durable flush |

Mutation and preview routes require the configured administrator role. Tokens,
uploads, storage credentials, and raw sensitive Azure errors are not logged.

## Current Limits

- MCA invoice section is not implemented.
- One field has at most one value per subscription.
- Coverage and Usage are the implemented report consumers.
- Generated snapshots are operational state, not business audit history.

## Verification

Primary automated coverage:

- `tests/services/test_subscription_context.py`
- `tests/services/test_subscription_context_refresh.py`
- `tests/storage/test_subscription_context_store.py`
- `tests/functions/test_subscription_context_bp.py`
- `tests/functions/test_subscription_context_refresh_blueprint.py`
- `tests/services/test_request_dimensions.py`
- `tests/services/test_odcr_coverage.py`
- `tests/services/test_odcr_usage.py`
- `tests/integration/test_report_model.py`
