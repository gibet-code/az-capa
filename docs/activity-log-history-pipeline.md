# Activity Log History Pipeline

## Status

Implemented as of 2026-09-08. This document describes the pipeline as built
through Activity Log source collection and inline operation assembly.

The pipeline collects Activity Log source events and assembles them into
operations inline. Each collected event is retained durably and replayable from
its inline payload, except outcome-only recipes (lifecycle actions) that opt out
of payload retention (Section 4); operation assembly runs as a phase of the same
collection orchestration rather than through a separate queue-triggered worker.
Domain history stores and reporting projections are intentionally deferred.
Their required inputs are recorded so the source pipeline does not discard
information they will need.

## Purpose

The Activity Log pipeline that this design replaced collected terminal VM
lifecycle events for a small operation allowlist and stored a compact event
representation. That was enough to identify that an operation succeeded or
failed, but not enough to:

- reconstruct the VM size, location, and zone that applied to a historical
  allocation attempt;
- distinguish a submitted VM configuration from one that was successfully
  applied;
- extend the same analysis to VM scale sets, Capacity Reservation Groups, and
  Capacity Reservations;
- estimate subscription creation time from an early subscription-scoped event;
  or
- reprocess historical source events when correlation or classification rules
  improve.

This pipeline captures Activity Log source facts once, assembles related event
stages into operations, and exposes replayable inputs to later domain
materializers. A failure in a downstream materializer must never prevent raw
collection coverage from advancing.

## Goals

- Use one generic subscription-scoped Activity Log collector for all approved
  resource and subscription operation families.
- Preserve enough source data to add VMSS, capacity-reservation, subscription,
  and future materializers without recollecting still-available Azure events.
- Correlate request-bearing Accepted or Started events with terminal Succeeded,
  Failed, or Cancelled events.
- Keep collected source durable and replayable independently of deferred domain
  materializers, so a downstream projection defect never blocks collection.
- Provide at-least-once processing with deterministic idempotency at every
  stage.
- Recover from delayed Activity Log ingestion, out-of-order events, worker
  failure, and failed assembly re-derived from the retained payloads.
- Preserve collected history beyond Azure Activity Log's 90-day source
  retention, subject to the application's retention policy.

## Non-goals

- Define physical schemas or APIs for subscription lifecycle, VM history, VMSS
  history, CRG/CR history, allocation history, or dashboard rollups.
- Define allocation-failure taxonomy across Azure provider error codes.
- Guarantee the actual subscription creation time. Without directory-level
  subscription events, the result is an explicitly identified estimate.
- Treat every failed write as a capacity failure.
- Implement tenant or directory Activity Log collection.
- Use event arrival order as an operation-ordering guarantee.

## Design Principles

1. **Retained source is the collection commit boundary.** Collection succeeds
   when the selected source events — their projected fields and compressed
   payloads — are durably stored, not when every downstream consumer has
   processed them.
2. **The source ledger is monotonic.** Adding a collection recipe backfills new
   events but does not invalidate or flush previously collected events.
3. **The retained rows are the handoff, not a message bus.** Each operation row
   owns its source payload inline (gzip-compressed) and is the durable,
   replayable input to assembly and to later materializers; Blob is used only to
   spill a rare oversize payload. Any future cross-process notification carries a
   small versioned pointer, never an event payload.
4. **Delivery is at least once.** Duplicate pages, events, and repeated
   assembly runs are expected and harmless.
5. **Event time, not arrival order, controls assembly.** Activity Log stages can
   be delayed or returned on different refreshes.
6. **Unpaired is data.** Stages are paired by `correlationId` scoped to one
   resource; a stage that carries no `correlationId`, or whose counterpart
   never arrives, stays visible as an `unpaired` operation with a status value,
   never silently forced into a match.
7. **Replay is versioned.** Recipe, parser, and assembler versions are stored
   with their outputs so corrected logic can rebuild derived state from the
   retained payloads.

---

## Architecture

```text
Data Collection dispatcher
          |
          v
Activity Log collection orchestrator
  resolve recipes and subscription scope
  fan out subscription/time-window work
  fetch pages, project recipe fields, retain compressed payload
  assemble operations from the just-retained events (inline phase)
  reconcile per-recipe version drift (bounded replay) and sweep stale operations
          |
          +----> ActivityLogOperations (Table)      [projected fields + inline gzip payload]
          +----> ActivityLogCollectionState (Table)  [coverage + per-recipe materialized version]
          +----> OversizePayloadSpill (private Blob)  [rare: payload exceeds Table property limit]

Deferred (introduced with the first domain materializer, not now):
  - operation-ready notification for downstream materializers
  - the materializers and end stores themselves

Overlap re-query (part of normal collection) reconciles late/out-of-order
stages; a per-recipe version bump triggers a bounded replay from the retained
payloads when projection, normalization, or matching rules change.
```

The existing Data Collection dispatcher remains the only scheduler and manual
start point. It starts the finite collection orchestration and records that
run's status. Assembly and the stale-operation sweep are phases of that same
orchestration, not independently scheduled workers.

## 1. Collection Recipes

The collector is generic. A static, code-defined recipe registry describes
which Activity Log operations and event stages are needed. Resource-specific
interpretation does not belong in the collector.

Each recipe has this logical shape:

```json
{
  "id": "compute-configuration",
  "version": 3,
  "resourceTypes": ["microsoft.compute/virtualmachines"],
  "operationNames": [
    "microsoft.compute/virtualmachines/write",
    "microsoft.compute/virtualmachines/delete"
  ],
  "statuses": null,
  "projectedFields": [
    { "column": "VmSize", "path": "responseBody.properties.hardwareProfile.vmSize" },
    { "column": "Location", "path": "responseBody.location" },
    { "column": "Zones", "path": "responseBody.zones" },
    { "column": "ProvisioningState", "path": "responseBody.properties.provisioningState" },
    { "column": "ErrorCode", "path": "statusMessage.error.code", "onStatus": "Failed" }
  ]
}
```

A recipe declares only *what* to collect and *what to keep*. Collection tuning —
initial lookback, overlap, and ingestion lag — is global (Section 3), not
per-recipe.

**Field optionality.** The server-side selectors are optional; an unset or
`null` field imposes no filter on that dimension (widest match):

- `operationNames` (unset) — no operation filter. When set, it drives the
  server-side `operations eq` filter, which is the only *efficient* narrowing
  Activity Log offers and is case-insensitive (Section 3).
- `statuses` (unset, the default above) — collect every stage. Pairing needs
  both the request stage(s) and the terminal stage, so most recipes leave this
  unset rather than enumerating stages.
- `resourceTypes` (unset) — no resource-type constraint. There is no verified
  server-side resource-type filter, so when set it is enforced by a coarser
  `resourceProvider eq` push-down plus a client-side resource-type check
  (Section 3).

**Invariant:** a recipe must declare **at least one of `resourceTypes` or
`operationNames`**. `operationNames` is what makes collection tractable on busy
providers (it maps to `operations eq`); a `resourceTypes`-only recipe is valid
but falls back to the broader `resourceProvider eq` pull. The exact invariant
Azure operation names must be confirmed with representative events before a
recipe is enabled.

**Retention.** `projectedFields` promote source paths to typed, queryable
columns on the operation row. Payload retention is per-recipe, governed by a
`retainPayload` flag (default `true`): a recipe that retains its payload keeps
the whole matched event, gzip-compressed inline on the row, spilling to Blob
only on oversize (Section 4), so future `projectedFields` stay replayable from
stored data. Outcome-only recipes — the lifecycle-action family, which projects
nothing beyond the failure reason — set `retainPayload=false`: their rows carry
projected columns and the operation outcome but no stored event, so they are not
replayable and instead depend on re-collection within the source window if a new
field is ever added. `version` is bumped to trigger a per-recipe replay that
re-derives history from the retained payloads (Section 10.3); recipes that opt
out of retention are excluded from replay.

The initial recipe families are:

| Recipe family | Why it is collected |
|---|---|
| Existing VM lifecycle actions (`start`/`deallocate`/`reapply`/`redeploy`) | Allocation/deallocation outcomes; allocation failures surface on these action operations (not on `write`), with provider failure details. |
| VM write/delete | Submitted and applied VM configuration; resource-generation boundaries. |
| VMSS write/delete and relevant scale/instance actions | Scale-set model changes, requested capacity changes, and allocation outcomes. |
| CRG write/delete | Capacity-reservation placement context such as location and zones. |
| Capacity Reservation write/delete | Requested SKU/capacity and reservation provisioning outcomes; child events carry their own resource ID and are returned by a CRG-scoped query. Operation-name casing varies across events, but the server-side `operations` filter folds case, so collection is unaffected; only the operation identity used for pairing normalizes casing. |
| `Microsoft.Management` registration | Earliest observed `Microsoft.Management/register/action` on `/subscriptions/{id}/providers/Microsoft.Management`, a subscription-creation proxy. |

The subscription-creation proxy is a single pinned filter, not a generic
provider-registration scan: `operationName = Microsoft.Management/register/action`
on `resourceId = /subscriptions/{subscriptionId}/providers/Microsoft.Management`.
It is an `operationNames`-only recipe with no natural resource type, which is
why the recipe invariant is “at least one of `resourceTypes` or `operationNames`”
rather than “`resourceTypes` mandatory”. Because both the operation and the
resource ID are fixed, no target-namespace extraction or generic
`/register/action` parsing is needed. That registration is not itself a
subscription-create event — it can be automatic, delayed, repeated, or absent —
so downstream code exposes its earliest timestamp as an estimate with
provenance.

A newly added or widened recipe starts from empty coverage and its next
collection backfills up to the global lookback boundary; it does not delete or
flush previously retained rows. Independent per-recipe backfill windows are not
tracked — coverage is per subscription (Section 6).

## 2. Generic Source Event

The REST client requests and normalizes only source-level concepts. The
canonical event envelope is:

```text
schemaVersion
eventDataId
eventTimestamp
submissionTimestamp
subscriptionId
resourceId
resourceType
resourceGroupName
resourceProviderName
operationName
operationId
correlationId
eventName
status
subStatus
httpMethod
caller
properties
retrievedAt
recipeIds
```

`correlationId` is the pairing key. Real Activity Log data shows it stays
constant across all stages of one resource operation, while `operationId` does
not: for Compute async writes the `Started`/`Accepted` stages share one
`operationId`, but the terminal `Succeeded`/`Failed` stage carries a new one.
Pairing therefore matches on `correlationId` scoped to `resourceId` and
`operationName` (Section 8). `operationId` is retained per event as a
within-request-stage grouping and validation signal, never as the cross-stage
key. Because a `correlationId` can also span a multi-resource ARM deployment,
it is never used alone — the `resourceId` scope is what keeps a group to one
operation.

Activity Log does not expose the submitted request body. Real data shows
`httpRequest` carries only method, URI, and client identifiers, while the
applied resource state appears as a JSON string in `properties.responseBody`
on the `Accepted` stage of a successful write; a failed write emits no
`responseBody` at all. Property name casing and value shape are not guaranteed,
so the source representation preserves the `properties` dictionary without
VM-specific parsing. Later parsers must accept object, JSON-string, empty,
missing, and provider-specific variants.

### Sensitive payload handling

VM and VMSS write payloads can contain identity, OS profile, extension, custom
data, or other sensitive configuration. The retained payload store (the operation
table and any oversize spill) therefore:

- is private and inaccessible from user-facing APIs;
- uses the storage account's encryption and network controls;
- grants read access only to the backend processing identity and approved
  operators;
- never writes raw properties, bodies, caller values, or signed URLs to logs;
- has an explicit retention policy decided before production enablement; and
- supports a later sanitizing ingestion policy if retaining complete properties
  is not approved.

The operation row stores the compressed payload inline under the same
protections (Section 4); the projected columns hold only the recipe-selected
fields.

## 3. Azure Query Strategy

Collection remains subscription-scoped because provider-emitted resource
events are reliably available at subscription scope. Management-group or
tenant collection is not a substitute: those scopes can omit events emitted
directly by resource providers and can introduce duplicates.

For each subscription work unit, the collector:

1. queries from the work unit's start through `now - ingestionLag`;
2. applies the supported Activity Log server-side filters on the management
   events endpoint (api-version `2017-03-01-preview`): the event-time window,
   `eventChannels eq 'Admin, Operation'`, `levels`, and — from the recipe's
   `operationNames` — `operations eq '{comma-joined names}'`. The `operations`
   filter matches case-insensitively, so no casing variants are enumerated. When
   a recipe omits `operationNames`, the coarser `resourceProvider eq` (derived
   from `resourceTypes`) is pushed instead and the resource type is checked
   client-side;
3. requests the canonical source fields, including `properties`, `operationId`,
   `correlationId`, `eventName`, and `httpRequest`;
4. follows the opaque `nextLink` without rebuilding it;
5. for each returned page, re-filters client-side by recipe `resourceTypes` and
   `statuses`, projects the recipe's fields, retains each matching event's
   compressed payload, and assembles operations (Section 8); and
6. retries throttled and transient pages using Durable timers.

Ingestion lag, overlap, and initial lookback are global settings, not per-recipe
knobs. The default ingestion lag is at least 20 minutes because Azure documents
typical Activity Log availability in the 3-20 minute range. The default overlap
is at least 24 hours. Reducing either requires evidence that delayed events are
still captured.

The first successful collection for a subscription requests up to the global
89-day lookback. Azure retains the source for 90 days; the one-day margin
prevents boundary errors. History older than the earliest collected event cannot
be reconstructed from Activity Log alone.

## 4. Retained Payload and Oversize Spill

Each collected event that matches a **payload-retaining** recipe is retained
inline on its operation row (Section 8), not in a separate raw ledger. Retention
is governed per recipe by the `retainPayload` flag (Section 3); outcome-only
recipes (`retainPayload=false`) store projected columns and outcome but no event
body:

- recipe `projectedFields` become typed, queryable columns;
- for a retaining recipe, the full event is serialized to JSON, gzip-compressed,
  and stored as a binary property with a `PayloadEncoding` marker;
- retaining the whole event (not only `responseBody`) keeps future
  `projectedFields` replayable from stored data. When every recipe matching an
  event opts out of retention, the row's `AppliedPayloadGz` and `PayloadEncoding`
  are left null — the applied event is still identified (`AppliedEventId`) and
  its columns merged, but nothing is stored for replay.

Azure Table Storage caps a property at 64 KB and an entity at 1 MB. Real
Activity Log payloads are a few KB and compress to well under that, so inline
storage is the norm. The rare event whose compressed payload still exceeds the
property limit spills to a private Blob; the row keeps a pointer and a
truncation flag. Blob is therefore an exception path, not the primary store.

Deterministic content-addressed batch names are unnecessary: event-level
identity (`subscriptionId + eventDataId`) already makes duplicate and
overlapping pages harmless, so a retried or overlapping page simply converges on
the same operation state during assembly.

Empty query pages retain nothing; their successfully completed window is
represented by collection state after all pagination completes.

## 5. Event Identity and Dedup

There is no separate event-index table. Event-level idempotency is achieved
directly:

- within a page, duplicate `eventDataId` rows are collapsed in memory before
  assembly (Activity Log pages can repeat an event within one page);
- across pages and overlapping re-queries, assembly is keyed by operation
  identity, so the same event contributes the same result no matter how many
  times it is seen.

The authoritative source is the retained operation rows and their inline
payloads. Assembly and replay read those rows directly; there is no intermediate
index to keep consistent. Dropping the index removes a table, its partition
scheme, and its content-mismatch reconciliation, at the cost of reading the
affected recipe/partition rows on replay — acceptable at the expected volume.

## 6. Collection State and Commit Protocol

Collection progress is tracked per subscription:

```text
PartitionKey = subscriptionId
RowKey       = "coverage"

CoverageStart
CoverageEnd
EnabledRecipeIds
MaterializedRecipeVersions   (per-recipe version watermark; drives Section 10.3 replay)
LastSuccessfulRunId
LastUpsertAt
UpdatedAt
```

For one work unit, the commit sequence is:

1. Fetch an Azure page.
2. Gzip-compress each matching event's payload and derive its projected fields.
3. Assemble operations from the page's events (Section 8) and upsert
   `ActivityLogOperations` with the inline payload and projected columns.
   Payload retention follows each matching recipe's `retainPayload` flag;
   projection is best-effort, so an event whose fields cannot be derived is
   still assembled, with its row flagged `reprojection-pending` (Section 10.3)
   when a retained payload exists to replay from.
4. Continue to the next Azure page.
5. Advance the subscription coverage only after every page in the time window is
   upserted successfully.

With a single retained store, the upsert of projected columns plus inline
payload is the commit. Assembly is a deterministic, idempotent function of the
retained events and can be re-run from those rows at any time. The upsert is
keyed by operation identity, so a partial run is simply re-driven; coverage
never advances past a page whose upsert failed, and a crashed in-flight page is
re-fetched by the next overlap re-query (Section 10.1) within the 90-day source
window.

Removing a subscription from App Scope stops new collection. It does not
silently delete historical retained rows or derived facts. Purging retained
history is a separate explicit data-governance operation.

## 7. Assembly Placement and the Async Boundary

There are two seams in this pipeline, and they have very different risk
profiles:

- **Seam A — source collection to operation assembly.** Both sides are generic and
  source-level. Assembly only correlates events by identity (Section 8); it
  implements no VM, VMSS, CR, or subscription business rules and parses no
  domain configuration.
- **Seam B — operation assembly to domain materializers.** This is where
  domain parsing, business rules, and projection-specific defects live. All of
  those stores are deferred (see Deferred End Stores).

Assembly therefore runs **inline as a phase of the collection orchestration**,
not across a queue. Because assembly is a deterministic, idempotent function of
the retained events, it needs no separate durable delivery channel: the retained
rows (projected fields plus inline payload) are already the replayable input,
and duplicate or repeated runs converge on the same operation state.

This removes the `activity-log-raw-ready` queue, its per-batch assembler
receipt, the associated poison queue, and the raw-delivery reconciliation that
would otherwise be needed to hand collected events to a separate assembler. It
also removes the Queue data-plane RBAC and private-connectivity requirement that
an application-owned queue would impose on the private-endpoint deployment.

### Why inline assembly is safe here

- Assembly cannot suffer a domain-parser defect, because it does no domain
  parsing; its failure surface is limited to envelope normalization.
- The retained upsert, not downstream materialization, is the collection commit
  boundary (Section 6). Assembly is folded into that idempotent upsert; a defect
  is corrected by a versioned replay over the retained payloads (Section 10.3),
  and a crashed page is re-fetched by overlap re-query.
- Collection volume is small (representative runs are thousands of events), so
  throughput decoupling between fetch and assemble buys nothing at this scale.
- Durable Functions already provides per-activity retry and catch-and-continue;
  re-driving the idempotent upsert recovers the only isolation a poison queue
  would have provided.

### Why Seam B stays asynchronous (later)

Coupling collection to every domain materializer WOULD be a mistake: a VMSS
parser defect could block VM collection, Durable histories would grow,
backpressure would be opaque, and replaying one consumer would require custom
orchestration work. That decoupling is deferred until the first materializer
exists. At that point the materializer either polls `ActivityLogOperations` by
watermark or is driven by an `activity-log-operation-ready` notification; the
notification is not built now because it has no consumer (see Deferred End
Stores).

## 8. Operation Assembly

Operation assembly is an inline phase of the collection orchestration. After a
work unit's pages are retained, it reads the just-retained events, normalizes
event identifiers, and updates operation state. It does not implement VM, VMSS,
CR, or subscription business rules. Because it is idempotent and keyed by
operation identity, re-running it over overlapping or replayed events converges
on the same result.

### Operation identity

v1 pairing keys on `correlationId` scoped to a resource, because real data shows
`operationId` is not stable across an operation's stages (the terminal stage
gets a fresh `operationId`) while `correlationId` is:

1. when `correlationId` is present, identity is
   `subscriptionId + resourceId + correlationId + operationName`, with
   `operationName` lowercased so casing variants of the same operation do not
   split its stages; `operationId` is retained per event as a validation and
   within-request-stage grouping signal, not the cross-stage key;
2. when `correlationId` is absent, the event stands alone as an `unpaired`
   operation carrying its own source event ID — it is kept visible, never
   force-matched.

The `resourceId` and `operationName` scope is required: a bare `correlationId`
can span a multi-resource ARM deployment, so it is never used alone as proof
that two events are the same operation. Synthetic multi-signal identities and
ambiguity disambiguation are intentionally deferred. After the first production
run we measure pairing rates and revisit only if the data shows a richer matcher
is needed.

### Assembly store

`ActivityLogOperations` is assembly state, not a final domain store. Its logical
key supports deterministic point updates:

```text
PartitionKey = subscriptionId | operationIdentityHashPrefix
RowKey       = operationIdentityHash
```

Logical fields:

```text
OperationIdentity, OperationId, CorrelationId
SubscriptionId, ResourceId, ResourceType, OperationName (identity uses lowercased OperationName)
FirstEventTime, LastEventTime
RequestEventId, TerminalEventId, AllEventIds
ProjectedFields: recipe-declared typed columns (e.g. VmSize, Location, Zones, SkuName, Capacity, ProvisioningState, ErrorCode)
AppliedPayloadGz, PayloadEncoding        (full applied event, gzip-compressed inline; null for outcome-only recipes)
AppliedPayloadSpillBlob, PayloadTruncated (set only on the rare oversize spill)
Outcome: pending | succeeded | failed | cancelled | unknown
PairingStatus: paired | unpaired
AssemblerVersion, RecipeVersion
FirstSeenAt, LastSeenAt, FinalizedAt
PublishedVersion
```

The applied configuration is read from `properties.responseBody` on the
`Accepted` stage of a successful write; Activity Log carries no separate
submitted request body, so the applied event is retained inline (gzip) in
`AppliedPayloadGz` and the recipe's `projectedFields` promote its relevant
paths to typed columns. The terminal event validates whether the operation was
applied. A failed write emits no `responseBody`, so its size, location, and zone
must be attributed from the resource's configuration-at-the-time in a later
materializer, not from the failure event.

If a successful write contains no relevant configuration changes, the assembled
operation still exists. A later domain materializer decides whether it produces
a timeline entry.

### State transitions

Valid transitions are monotonic under one assembler version:

```text
absent -> pending
absent -> succeeded | failed | cancelled
pending -> succeeded | failed | cancelled
pending -> unknown (after reconciliation horizon)
unknown -> succeeded | failed | cancelled (late terminal correction)
```

A terminal event can arrive before the request-bearing event. The operation is
stored immediately and enriched when its counterpart arrives. With
correlation-scoped pairing all stages share the same identity, so out-of-order
arrival is just an upsert; no arrival-order resolution is needed.

### Downstream handoff (deferred)

`ActivityLogOperations` carries a `PublishedVersion` watermark so a future
materializer can detect changed operations. The cross-process
`activity-log-operation-ready` notification is deferred until the first domain
materializer exists; until then no notification is emitted and no consumer
depends on one. When introduced, consumers are independently idempotent, track
their own processing version, and their success is not part of collection
success.

## 9. Idempotency and Failure Handling

Deterministic identities are used throughout:

| Item | Identity |
|---|---|
| Oversize payload spill | Deterministic blob name (`subscription/eventDataId`) |
| Source event | `subscriptionId + eventDataId` |
| Operation | `subscriptionId + resourceId + correlationId + operationName` (else per-event `unpaired` synthetic) |
| Operation publication | `operationIdentity + assemblerVersion + materializedVersion` |

Collection activities retry transient and throttled pages with Durable timers
and bounded backoff. Projection runs inline and is idempotent, so its failure
mode is not a poison message: for a payload-retaining recipe the event payload
is retained regardless, and the row is flagged `reprojection-pending` and
re-derived by the same versioned replay that handles rule changes
(Section 10.3). A row whose projection
repeatedly fails on deterministic input stays flagged and is surfaced in status
with sanitized diagnostics on the row itself, never raw request bodies:

```text
ReprojectionPending (bool)
AssemblerVersion, RecipeVersion
AttemptCount, FirstFailedAt, LastFailedAt
ErrorCategory, SanitizedError
```

Metrics and status expose at least:

- source coverage by recipe and subscription;
- operations with reprojection pending and their age;
- assembly watermark and pending-operation count;
- unpaired and expired-operation counts; and
- oldest unresolved reprojection.

## 10. Reconciliation

Reconciliation is required because Activity Log stages can arrive late or out of
order, and because a crashed in-flight page must be recovered. It is not a
separate independently scheduled worker: overlap re-query, the stale-operation
sweep, and the per-recipe version reconcile are phases of the normal collection
orchestration. Each is idempotent and bounded to recent or explicitly incomplete
records; none scans all retained history on every run.

### 10.1 Activity Log overlap reconciliation

Normal collection re-queries at least the configured overlap window. The
overlapping re-query re-reads recent events, and the inline assembly phase
re-evaluates the affected operations by `correlationId`, so late and out-of-order
stages complete automatically without a dedicated worker. Coverage advances by
event-time query window, while `submissionTimestamp` and `retrievedAt` remain
available to measure source lag.

If observed source delays exceed the configured overlap, status raises a data-
quality warning and operators widen the overlap before trusting completeness.

### 10.2 Incomplete-operation sweep

As a final phase of each run, over the scope just collected, assembly revisits
operations in `pending` or `unknown` state:

1. re-evaluates the operation's events by `correlationId`;
2. finalizes newly completed operations and bumps their version;
3. moves old pending operations to `unknown` after a configurable horizon; and
4. permits a later terminal event to correct `unknown` to a terminal outcome.

The horizon must exceed the collection ingestion lag and overlap. Expiration is
not deletion and does not assert failure; it means no terminal event was found
within the evidence available. This is the "park unfinished operations until a
terminal event arrives" store: non-terminal operations remain visible and are
completed by a later overlap re-query rather than by arrival-order guessing.

### 10.3 Rebuild and version reconciliation

Re-derivation always reads the retained inline payloads. It is triggered two
ways, reconciled differently:

- **Projection, normalization, or matching change (or a `reprojection-pending`
  row)** — bump the recipe `version` (or the assembler version); a row flagged
  `reprojection-pending` by a projection failure (Section 9) is picked up by the
  same pass without a version bump. The main refresh orchestration compares each
  recipe's current version against the per-(subscription, recipe)
  `materializedVersion` watermark; where it is behind — or where rows are
  flagged — it enqueues a **bounded, throttled backfill** that re-derives
  operations in place from the **retained inline payloads**. The backfill is
  idempotent (keyed by operation identity) and drains over several cycles rather
  than blocking the forward path. No Azure re-fetch is needed, so it works past
  the 90-day source window. A row that keeps failing on deterministic input
  stays flagged and surfaced until code changes.
- **Widening the collection filter** (`resourceTypes`, `operationNames`,
  `statuses`) — this needs events that were never collected, so it is a
  re-collection, not a replay: the widened recipe backfills from Azure and is
  therefore bounded to the 90-day source window. Older matching events are
  unrecoverable.

Side-by-side versioned state and a promotion gate are deferred — add them only if
a downstream consumer later needs a zero-downtime cutover between versions.

Retained payloads therefore bound how far corrected derivation can be rebuilt
without re-collection: for a payload-retaining recipe the whole event is kept, so
any new `projectedField` whose source path is inside the retained event is
replayable; a field that was never retained — including everything under an
outcome-only recipe (`retainPayload=false`) — is recoverable only by
re-collection within the source window.

---

## Deferred End Stores

The following stores are required by the product direction but are outside this
design's implementation scope. Their physical partitioning, APIs, materializer
orchestration, serving projections, and retention policies will be designed
later. The cross-process `activity-log-operation-ready` notification and the
Seam B asynchronous boundary (Section 7) are introduced together with the first
materializer below, not before; until then materializers can poll
`ActivityLogOperations` by its `PublishedVersion` watermark.

### Subscription lifecycle

**Why it is needed:** enrich the Subscription catalogue and reports with the
best available subscription creation-time estimate when directory subscription
creation events are unavailable.

**Activity Log data required:**

- subscription ID;
- event and submission timestamps of the pinned
  `Microsoft.Management/register/action` event;
- event status and substatus;
- operation ID; and
- source event ID and collection provenance.

Because the recipe pins the exact operation and resource ID, no target-namespace
extraction is needed. The result must distinguish estimated creation time,
estimate source, and confidence. The earliest successful observed
`Microsoft.Management` registration is a proxy, not proof of subscription
creation.

### VM configuration history

**Why it is needed:** show a per-VM configuration timeline and attribute
historical allocation attempts to the configuration that was attempted or
effective at that time rather than to the VM's current state.

**Activity Log data required:**

- VM write and delete operations;
- applied configuration from the `Accepted` stage `responseBody` (Activity Log
  carries no separate submitted request body);
- terminal outcome and provider errors;
- resource ID, subscription, and timestamps;
- operation and correlation IDs;
- applied VM size, location, priority, and capacity-reservation association
  (`capacityReservationGroup` id), all present in the update `responseBody`; a
  failed write emits no `responseBody`, so its size is attributed from the VM's
  configuration-at-the-time;
- availability zone, which real data confirms is emitted only on the create
  write's `responseBody` (`provisioningState = Creating`) as a top-level `zones`
  field; update writes omit it. When the create predates the 90-day source
  window the zone is not recoverable from Activity Log and must be read from
  current resource state (Resource Graph). Zone is never inferred from the
  capacity-reservation-group name; and
- delete/recreate evidence for resource-generation boundaries.

### VMSS configuration history

**Why it is needed:** show scale-set model changes and provide configuration and
requested quantity for scale and instance allocation attempts. Uniform VMSS is
the first required mode; the representation must not prevent later Flexible
VMSS support.

**Activity Log data required:**

- VMSS write/delete, scale, and relevant instance-operation events;
- request and terminal stages;
- scale-set and instance resource IDs;
- submitted location, zones, SKU name, capacity, orchestration mode, and
  capacity-reservation association when present;
- operation/correlation identifiers and timestamps; and
- provider errors and partial/ambiguous outcome evidence.

### CRG and Capacity Reservation configuration history

**Why it is needed:** show placement and reserved-capacity changes over time and
identify capacity-reservation acquisition attempts that succeed or fail.

**Activity Log data required:**

- CRG and Capacity Reservation write/delete operations. The operation name
  casing is inconsistent across events (Azure emits
  `capacityReservationGroups`/`CapacityReservationGroups` and
  `capacityReservations`/`CapacityReservations` interchangeably). The server-side
  `operations` filter matches case-insensitively (verified: one filter value
  returns all casing variants), so collection needs no casing handling; only the
  operation identity used for pairing must normalize `operationName` casing so an
  operation's stages are not split apart.
- parent/child resource IDs. Each Capacity Reservation event carries its own full
  child resource ID (`.../capacityReservationGroups/{crg}/capacityReservations/{cr}`);
  a CRG-scoped Activity Log query (`resourceUri eq {crgId}`) returns the CRG event
  **and all descendant Capacity Reservation events**, because `resourceUri`
  matches hierarchically. The child resource ID keeps sibling reservations
  distinct under the standard `correlationId + resourceId + operationName` pairing
  key even when a single bulk deployment shares one `correlationId` across many
  reservations (real data: 20 reservations created under one `correlationId`).
- request and terminal stages;
- CRG location and zones, read from the CRG write `responseBody` (real data:
  top-level `zones` such as `['1','2','3']` plus `location`);
- Capacity Reservation SKU name and requested capacity, read from the Capacity
  Reservation write `responseBody`: `sku.name` (reserved VM size), `sku.capacity`
  (requested quantity), top-level `zones`, `properties.reservationId`, and
  `properties.provisioningState`. A quantity change is emitted as an ordinary
  `write` whose `sku.capacity` differs from the prior write (real data:
  `capacity` moving `0 -> 7`, `0 -> 11`, `1 -> 0`), not as a distinct operation;
- operation/correlation identifiers and timestamps; and
- provider error codes/messages needed to distinguish capacity shortage from
  quota, policy, authorization, and validation failures.

### Allocation history

**Why it is needed:** provide a rich, cross-resource analytical dataset for
dashboarding allocation success, failure, and likely capacity pressure by time,
location, zone, SKU, quantity, and originating/impacted resource.

**Activity Log data required:**

- assembled outcomes for VM, VMSS, CR, and relevant lifecycle operations;
- attempted configuration from request-bearing events;
- terminal success/failure and provider error details;
- originating resource, impacted resource, and parent resource identifiers;
- timestamps, operation ID, correlation ID, and all source event IDs; and
- evidence needed to represent quantity, partial outcomes, pairing confidence,
  and unknown configuration without guessing.

The allocation store will consume configuration histories as additional context,
but source collection must preserve attempted configuration because a failed write
never becomes effective resource state.

## Required Validation Before Implementation

1. Capture representative real events for VM create, resize, no-op write,
   failed resize, delete/recreate, regional create, and zonal create.
2. Confirm applied-config presence in `responseBody`, casing, shape, redaction,
   and size across Portal, CLI, Bicep/ARM, Terraform, and policy-driven writes;
   confirm `httpRequest` never carries a body, and that a zonal VM's create
   write exposes a top-level `zones` field while its update writes do not (so
   zone is only recoverable from Activity Log when the create is inside the
   90-day window).
3. Confirm `correlationId`-scoped pairing holds beyond the initial probe (which
   showed the terminal stage carries a fresh `operationId`), and measure how
   often `correlationId` is absent or spans multiple resources. Real data
   already shows a single `correlationId` spanning 20 Capacity Reservations in
   one bulk deployment, which the per-resource `resourceId` scope keeps distinct;
   confirm this holds for other multi-resource deployments.
4. Capture VMSS Uniform create, model update, scale-out, scale-in, and partial or
   failed allocation events.
5. Capture CRG and Capacity Reservation create, resize, no-op, failure, and
   delete events and record provider error shapes. Real data confirms create and
   quantity-change (`sku.capacity`) writes, per-CR child resource IDs, zones on
   both CRG and CR writes, and case-varying operation names; delete and failure
   events were not observed in the probe window and still need representative
   samples.
6. Capture resource-provider registration events and confirm how the target
   `Microsoft.Management` namespace is represented.
7. Load-test page count, compressed payload size, inline-assembly throughput,
   and Table partition distribution at the expected subscription scale.
8. Complete a security review of retained `properties` and request bodies and
   approve access and retention controls.
9. Prove recovery for a failure after a page upsert, for a crashed in-flight
   page (re-fetched by overlap re-query), and during the inline projection phase
   (reprojection flag and replay from retained payload).
10. Prove out-of-order assembly, duplicate/overlapping re-query, missing
    terminal expiry, late terminal correction, unpaired-event handling,
    reprojection from the retained payload, and versioned replay.

## Open Decisions

- Raw properties/request-body retention period and whether sanitization is
  required at ingestion.
- Application storage account versus a dedicated account for retained payloads
  and their oversize Blob spill.
- Whether the deferred Seam B handoff (Section 7) uses Queue Storage, Service
  Bus, or watermark polling — decided with the first materializer, when its
  fan-out, dead-letter, and throughput needs are known.
- Exact recipe operation names and stage filters after representative-event
  validation.
- Pending-operation expiry horizon and replay window.
- Whether a richer matcher (beyond `correlationId + resourceId + operationName`)
  is needed for edge cases such as an absent `correlationId` or a single
  `correlationId` driving multiple operations on one resource — decided after
  the first production run; v1 pairs on correlation scoped to the resource.