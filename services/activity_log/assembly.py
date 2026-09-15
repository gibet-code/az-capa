"""Pure operation assembly: correlate source events into operation state.

Assembly is generic and source-level (Seam A, Section 7). It groups prepared
events by operation identity, folds their stages into an outcome, and merges the
applied configuration. It implements no VM/VMSS/CR/subscription business rules
and is a deterministic, idempotent function of its input, so re-running it over
overlapping or replayed events converges on the same result.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .recipes import TERMINAL_STATUSES

OUTCOME_PENDING = "pending"
OUTCOME_UNKNOWN = "unknown"
PAIRING_PAIRED = "paired"
PAIRING_UNPAIRED = "unpaired"

_ACCEPTED = "accepted"
_STARTED = "started"

_OUTCOME_BY_STATUS = {
    "succeeded": "succeeded",
    "failed": "failed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class PreparedEvent:
    """A normalized source event carrying its projection outputs."""

    event_data_id: str
    event_timestamp: str
    subscription_id: str
    resource_id: str | None
    operation_name: str
    status: str | None
    resource_type: str | None = None
    submission_timestamp: str | None = None
    operation_id: str | None = None
    correlation_id: str | None = None
    columns: dict[str, object] = field(default_factory=dict)
    payload_gz: bytes | None = None
    payload_encoding: str | None = None
    reprojection_pending: bool = False


def identity_string(event: PreparedEvent) -> str:
    """The operation identity (Section 8).

    ``operationName`` is lowercased so casing variants of the same operation do
    not split its stages. Without a ``correlationId`` the event stands alone as
    an ``unpaired`` operation carrying its own event id.
    """
    subscription = (event.subscription_id or "").lower()
    resource = (event.resource_id or "").lower()
    operation = (event.operation_name or "").lower()
    if event.correlation_id:
        return f"{subscription}|{resource}|{event.correlation_id}|{operation}"
    return f"{subscription}|{resource}|unpaired|{operation}|{event.event_data_id}"


def identity_hash(identity: str) -> str:
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def partition_key(subscription_id: str, operation_identity_hash: str) -> str:
    return f"{(subscription_id or '').lower()}|{operation_identity_hash[:2]}"


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _outcome(events: list[PreparedEvent]) -> str:
    terminal = [event for event in events if (event.status or "").lower() in TERMINAL_STATUSES]
    if not terminal:
        return OUTCOME_PENDING
    latest = max(terminal, key=lambda event: _parse_timestamp(event.event_timestamp))
    return _OUTCOME_BY_STATUS.get((latest.status or "").lower(), OUTCOME_UNKNOWN)


def _select_applied(events: list[PreparedEvent]) -> PreparedEvent | None:
    """The event whose payload holds the applied config (Accepted stage first)."""
    accepted = [event for event in events if (event.status or "").lower() == _ACCEPTED]
    pool = accepted or [event for event in events if event.payload_gz is not None] or events
    payload_bearing = [event for event in pool if event.payload_gz is not None]
    candidates = payload_bearing or pool
    return max(candidates, key=lambda event: _parse_timestamp(event.event_timestamp), default=None)


def _merge_columns(events: list[PreparedEvent]) -> dict[str, object]:
    """Merge projected columns; the Accepted stage (applied config) wins."""
    ordered = sorted(events, key=lambda event: (event.status or "").lower() == _ACCEPTED)
    merged: dict[str, object] = {}
    for event in ordered:
        merged.update(event.columns)
    return merged


def _first_id(events: list[PreparedEvent], *statuses: str) -> str | None:
    wanted = {value.lower() for value in statuses}
    ordered = sorted(events, key=lambda event: _parse_timestamp(event.event_timestamp))
    for event in ordered:
        if (event.status or "").lower() in wanted:
            return event.event_data_id
    return None


def _terminal_id(events: list[PreparedEvent]) -> str | None:
    terminal = [event for event in events if (event.status or "").lower() in TERMINAL_STATUSES]
    if not terminal:
        return None
    return max(terminal, key=lambda event: _parse_timestamp(event.event_timestamp)).event_data_id


def assemble_operation(events: list[PreparedEvent]) -> dict:
    """Fold one identity's events into an operation record (Section 8 fields)."""
    ordered = sorted(events, key=lambda event: _parse_timestamp(event.event_timestamp))
    representative = ordered[-1]
    identity = identity_string(representative)
    identity_digest = identity_hash(identity)
    applied = _select_applied(ordered)
    all_ids = sorted({event.event_data_id for event in ordered if event.event_data_id})
    return {
        "OperationIdentity": identity,
        "OperationIdentityHash": identity_digest,
        "PartitionKey": partition_key(representative.subscription_id, identity_digest),
        "RowKey": identity_digest,
        "SubscriptionId": (representative.subscription_id or "").lower(),
        "ResourceId": representative.resource_id,
        "ResourceType": representative.resource_type,
        "OperationName": representative.operation_name,
        "OperationId": representative.operation_id,
        "CorrelationId": representative.correlation_id,
        "FirstEventTime": ordered[0].event_timestamp,
        "LastEventTime": ordered[-1].event_timestamp,
        "RequestEventId": _first_id(ordered, _ACCEPTED, _STARTED),
        "TerminalEventId": _terminal_id(ordered),
        "AllEventIds": all_ids,
        "Columns": _merge_columns(ordered),
        "AppliedEventId": applied.event_data_id if applied else None,
        "AppliedPayloadGz": applied.payload_gz if applied else None,
        "PayloadEncoding": applied.payload_encoding if applied else None,
        "Outcome": _outcome(ordered),
        "PairingStatus": PAIRING_PAIRED if representative.correlation_id else PAIRING_UNPAIRED,
        "ReprojectionPending": any(event.reprojection_pending for event in ordered),
    }


def assemble(events: list[PreparedEvent]) -> list[dict]:
    """Group prepared events by identity and assemble each operation.

    Duplicate ``eventDataId`` values within the input are collapsed first
    (Activity Log pages can repeat an event), so the result is stable regardless
    of how many times an event is seen (Section 5).
    """
    deduped: dict[str, PreparedEvent] = {}
    for event in events:
        key = f"{(event.subscription_id or '').lower()}|{event.event_data_id}"
        deduped[key] = event

    grouped: dict[str, list[PreparedEvent]] = {}
    for event in deduped.values():
        grouped.setdefault(identity_string(event), []).append(event)

    return [assemble_operation(group) for group in grouped.values()]
