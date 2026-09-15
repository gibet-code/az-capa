# Activity Log Materializers

## Status

Deferred planning. The source collector and inline operation assembler are
implemented and specified in
[Activity Log History](../specs/activity-log-history.md). This document defines
the product direction for downstream domain histories; it is not an implemented
API or storage contract.

## Shared Boundary

Materializers consume assembled operations from `ActivityLogOperations` using
its `PublishedVersion` watermark. The asynchronous Seam B boundary and any
`activity-log-operation-ready` notification are introduced with the first
materializer, when throughput, fan-out, retry, and dead-letter requirements are
known.

Each materializer still requires a separate design for physical partitioning,
serving APIs, retention, replay orchestration, and report projections. A defect
in one materializer must never block source collection or inline operation
assembly.

## Subscription Lifecycle

The goal is to enrich the Subscription catalogue and reports with the best
available creation-time estimate when directory subscription-creation events are
unavailable.

Required Activity Log inputs:

- subscription ID;
- event and submission timestamps for the pinned
  `Microsoft.Management/register/action` event;
- status and substatus;
- operation ID; and
- source-event identity and collection provenance.

The earliest successful observed registration is a proxy, not proof of
subscription creation. The output must expose the estimate, its source, and its
confidence.

## VM Configuration History

The goal is to reconstruct per-VM configuration over time and associate an
allocation attempt with the configuration attempted or effective at that time.

Required Activity Log inputs include VM write and delete operations, terminal
outcomes, provider errors, timestamps, operation and correlation IDs, and the
applied configuration from the Accepted-stage `responseBody`. Activity Log does
not carry a separate submitted request body.

VM size, location, priority, and capacity-reservation association are available
from update responses. A failed write has no response body, so its size must be
attributed from configuration history. Availability zone is present on the
create response but omitted by later updates; if creation predates the source
window, current Resource Graph state is the fallback. Zone must never be inferred
from a Capacity Reservation Group name. Delete and recreate events establish
resource-generation boundaries.

## VMSS Configuration History

Uniform VMSS is the first required mode, without preventing later Flexible VMSS
support. Inputs include model write/delete, scale and instance operations,
request and terminal stages, scale-set and instance IDs, SKU and capacity,
location and zones, orchestration mode, capacity-reservation association,
timestamps, correlation identifiers, provider errors, and partial outcomes.

## Capacity Reservation History

The goal is to show CRG placement and Capacity Reservation quantity changes over
time and identify acquisition attempts that succeed or fail.

Important source characteristics:

- Operation-name casing varies and must be normalized for operation identity.
- A CRG-scoped Activity Log query includes descendant Capacity Reservation
  events, while each reservation retains its full child resource ID.
- A single bulk deployment can share one correlation ID across many
  reservations; resource ID keeps those operations distinct.
- CRG writes provide location and zones.
- Capacity Reservation writes provide `sku.name`, `sku.capacity`, zones,
  reservation ID, and provisioning state.
- Quantity changes are ordinary writes with a changed `sku.capacity`.
- Provider errors must distinguish capacity shortage from quota, policy,
  authorization, and validation failures.

## Allocation History

The goal is a cross-resource analytical dataset for allocation success, failure,
and likely capacity pressure by time, location, zone, SKU, quantity, and
originating or impacted resource.

It consumes assembled VM, VMSS, and Capacity Reservation outcomes together with
configuration histories. It must preserve unknown and ambiguous values rather
than infer them, and it must distinguish attempted configuration from effective
resource state.

## Validation Before Design

- Capture representative VM create, resize, failed resize, delete/recreate,
  regional, and zonal events across Portal, CLI, Bicep/ARM, Terraform, and
  policy-driven writes.
- Capture VMSS Uniform create, model update, scale-out, scale-in, and partial or
  failed allocation events.
- Capture CRG and Capacity Reservation create, quantity change, no-op, failure,
  and delete events, including provider error shapes.
- Confirm pairing behavior when correlation IDs are absent, reused across
  resources, or reused across multiple operations on one resource.
- Define the first materializer's serving API, partitioning, retention, replay,
  and backfill behavior before introducing an asynchronous handoff.

## Open Decisions

- Which materializer is implemented first.
- Whether Seam B uses Queue Storage, Service Bus, or watermark polling.
- Whether materialized histories are stored with source operations or in
  dedicated tables or accounts.
- Retention and rebuild guarantees for each history.
- How report projections expose confidence, partial outcomes, and unavailable
  historical configuration.
