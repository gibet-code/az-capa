"""Validation and wire contracts for shared manual ODCR coverage decisions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

DECISION_REQUIRED = "required"
DECISION_NOT_REQUIRED = "not_required"
VALID_DECISIONS = (DECISION_REQUIRED, DECISION_NOT_REQUIRED)
VALID_PRIORITIES = ("low", "medium", "high")
MAX_NOTE_LENGTH = 500
MAX_RESOURCE_IDS = 500

_VM_ID_RE = re.compile(
    r"^/subscriptions/([^/]+)/resourcegroups/([^/]+)/providers/"
    r"microsoft\.compute/virtualmachines/([^/]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CoverageDecisionChange:
    resource_ids: tuple[str, ...]
    decision: str | None
    priority: str | None
    note: str | None


def normalize_vm_resource_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Each resourceIds item must be a VM resource ID string.")
    normalized = value.strip().rstrip("/").lower()
    if not _VM_ID_RE.fullmatch(normalized):
        raise ValueError(f"Invalid VM resource ID: {value!r}.")
    return normalized


def subscription_id_from_resource_id(resource_id: str) -> str:
    match = _VM_ID_RE.fullmatch(resource_id)
    if not match:
        raise ValueError(f"Invalid VM resource ID: {resource_id!r}.")
    return match.group(1).lower()


def parse_decision_change(payload: object) -> CoverageDecisionChange:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    raw_ids = payload.get("resourceIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("resourceIds must be a non-empty array.")
    if len(raw_ids) > MAX_RESOURCE_IDS:
        raise ValueError(f"resourceIds cannot contain more than {MAX_RESOURCE_IDS} items.")

    resource_ids = tuple(dict.fromkeys(normalize_vm_resource_id(value) for value in raw_ids))
    decision = payload.get("decision")
    if decision is not None:
        if not isinstance(decision, str) or decision.lower() not in VALID_DECISIONS:
            raise ValueError(f"decision must be one of {VALID_DECISIONS} or null.")
        decision = decision.lower()

    priority = payload.get("priority")
    if priority is not None:
        if not isinstance(priority, str) or priority.lower() not in VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {VALID_PRIORITIES}.")
        priority = priority.lower()
    if decision == DECISION_REQUIRED and priority is None:
        raise ValueError("priority is required when ODCR coverage is required.")
    if decision != DECISION_REQUIRED and priority is not None:
        raise ValueError("priority is only valid when ODCR coverage is required.")

    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string or null.")
    note = note.strip() if note else None
    if note and len(note) > MAX_NOTE_LENGTH:
        raise ValueError(f"note cannot exceed {MAX_NOTE_LENGTH} characters.")
    if decision is None and note is not None:
        raise ValueError("note must be null when clearing an ODCR coverage decision.")
    return CoverageDecisionChange(resource_ids, decision, priority, note)


def coverage_decision_json(entity: dict) -> dict:
    decision = entity.get("Decision")
    return {
        "decision": None if decision == "cleared" else decision,
        "priority": entity.get("Priority") or None,
        "note": entity.get("Note") or None,
        "updatedBy": {
            "tenantId": entity.get("UpdatedByTenantId"),
            "objectId": entity.get("UpdatedByObjectId"),
            "displayName": entity.get("UpdatedByDisplayName"),
        },
        "updatedAt": _iso(entity.get("UpdatedAt")),
    }


def _iso(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def get_for_subscriptions(subscription_ids) -> dict[str, dict]:
    from storage.odcr_coverage_store import store

    return {
        resource_id: coverage_decision_json(entity)
        for resource_id, entity in store.get_for_subscriptions(subscription_ids).items()
    }


def upsert_change(change: CoverageDecisionChange, audit) -> list[dict]:
    from storage.odcr_coverage_store import store

    entities = store.upsert_change(change, audit)
    return [
        {"resourceId": entity["ResourceId"], "odcrCoverage": coverage_decision_json(entity)}
        for entity in entities
    ]


def build_visibility_query(resource_ids: tuple[str, ...]) -> str:
    values = ", ".join(f"'{value.replace(chr(39), chr(39) * 2)}'" for value in resource_ids)
    return (
        "resources "
        "| where type =~ 'microsoft.compute/virtualmachines' "
        f"| where id in~ ({values}) "
        "| project id = tolower(id)"
    )