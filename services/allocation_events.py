"""Resource Graph VM inventory for the Allocation Events report."""
from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import re

from clients.resource_graph import run_graph_query
from clients.resource_graph_kql import in_filter
from core import identity, settings
from services import request_dimensions, zone_mapping
from services.activity_log import recipes
from services.activity_log.projection import decompress_json
from services.app_scope import resolve_app_scope
from services.odcr_coverage import apply_physical_zones, zone_mapping_pairs
from storage.activity_log_store import ActivityLogStore

_PERIOD_DAYS = {"1d": 1, "7d": 7, "30d": 30}
_DEFAULT_PERIOD = "30d"
_CONFIRMED_FAILURE_CODES = ("AllocationFailed", "ZonalAllocationFailed")
_LIFECYCLE_RECIPE = recipes.get("vm-lifecycle")
_CONFIGURATION_RECIPE = recipes.get("vm-configuration")
_LIFECYCLE_OPERATION_NAMES = tuple(
    name for name in _LIFECYCLE_RECIPE.operation_names
    if name != "microsoft.compute/virtualmachines/deallocate/action"
)
_LIFECYCLE_OPERATION_NAMES_FOLDED = frozenset(
    name.casefold() for name in _LIFECYCLE_OPERATION_NAMES
)
_CREATE_OPERATION_NAME = "microsoft.compute/virtualmachines/write"
_OPERATION_NAMES = (*_LIFECYCLE_OPERATION_NAMES, _CREATE_OPERATION_NAME)
_OPERATION_COLUMNS = (
    "ResourceId",
    "OperationName",
    "Outcome",
    "LastEventTime",
    "ErrorCode",
    "ErrorMessage",
    "ErrorDetailCode",
    "ErrorDetailMessage",
    "ErrorDetailCount",
    "ErrorCodesJson",
    "HasAllocationFailure",
    "ProvisioningState",
)
_MAX_QUERY_WORKERS = 8
_VM_RESOURCE_ID = re.compile(
    r"^/subscriptions/([^/]+)/resourcegroups/([^/]+)/providers/"
    r"microsoft\.compute/virtualmachines/([^/]+)$",
    re.IGNORECASE,
)
_TIMELINE_RECIPES = (
    recipes.get("vm-lifecycle"),
    recipes.get("vm-configuration"),
)
_TIMELINE_OPERATIONS_FOLDED = {
    operation.casefold(): recipe.id
    for recipe in _TIMELINE_RECIPES
    for operation in recipe.operation_names
}
_TIMELINE_COLUMNS = (
    "OperationIdentityHash",
    "ResourceId",
    "OperationName",
    "Outcome",
    "LastEventTime",
    "ErrorCode",
    "ErrorMessage",
    "ErrorDetailCode",
    "ErrorDetailMessage",
    "ErrorDetailCount",
    "ErrorCodesJson",
    "HasAllocationFailure",
    "ErrorDetailsGz",
    "ErrorDetailsSpillBlob",
    "VmSize",
    "Location",
    "Zones",
    "ProvisioningState",
    "CapacityReservationGroupId",
)
_TIMELINE_VM_FIELDS = (
    "id",
    "resourceType",
    "subscriptionId",
    "subscriptionName",
    "resourceGroup",
    "name",
    "location",
    "locationDisplayName",
    "logicalZone",
    "physicalZone",
    "vmSize",
    "powerState",
    "provisioningState",
    "capacityReservationGroupId",
)

activity_log_store = ActivityLogStore()


class AllocationEventVmNotFound(Exception):
    """The requested VM is outside the caller's visible, configured scope."""


def build_vm_inventory_query(locations: Iterable[str] | None = None) -> str:
    location_filter = in_filter("location", locations) or ""
    return f"""resources
| where type =~ 'microsoft.compute/virtualmachines'
{location_filter}
| extend logicalZone = tostring(zones[0])
| extend vmSize = tostring(properties.hardwareProfile.vmSize)
| extend isVmssFlexible = isnotnull(properties.virtualMachineScaleSet)
| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
| join kind=leftouter (
    resourcecontainers
    | where type =~ 'microsoft.resources/subscriptions'
    | project subscriptionId, subscriptionName = name
) on subscriptionId
| project id = tolower(id),
    resourceType = tolower(type),
    subscriptionId = tolower(subscriptionId),
    subscriptionName,
    resourceGroup,
    name,
    location = tolower(location),
    logicalZone,
    vmSize,
    isVmssFlexible,
    powerState
| order by id asc"""


def validate_vm_resource_id(resource_id: object) -> tuple[str, str]:
    """Validate and normalize an exact VM ARM resource ID."""
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError("resourceId must be a non-empty string.")
    normalized = _resource_id(resource_id)
    match = _VM_RESOURCE_ID.fullmatch(normalized)
    if match is None:
        raise ValueError("resourceId must be a virtual machine Azure resource ID.")
    return normalized, match.group(1).casefold()


def build_vm_visibility_query(resource_id: str) -> str:
    literal = resource_id.replace("'", "''")
    return f"""resources
| where type =~ 'microsoft.compute/virtualmachines'
| where id =~ '{literal}'
| extend logicalZone = tostring(zones[0])
| extend vmSize = tostring(properties.hardwareProfile.vmSize)
| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
| extend provisioningState = tostring(properties.provisioningState)
| extend capacityReservationGroupId = tostring(properties.capacityReservation.capacityReservationGroup.id)
| join kind=leftouter (
    resourcecontainers
    | where type =~ 'microsoft.resources/subscriptions'
    | project subscriptionId, subscriptionName = name
) on subscriptionId
| project id = tolower(id),
    resourceType = tolower(type),
    subscriptionId = tolower(subscriptionId),
    subscriptionName,
    resourceGroup,
    name,
    location = tolower(location),
    logicalZone,
    vmSize,
    powerState,
    provisioningState,
    capacityReservationGroupId"""


_VM_INVENTORY_FIELDS = (
    "id",
    "resourceType",
    "subscriptionId",
    "subscriptionName",
    "resourceGroup",
    "name",
    "location",
    "locationDisplayName",
    "logicalZone",
    "physicalZone",
    "vmSize",
    "isVmssFlexible",
    "powerState",
    "businessContext",
)


def project_vm_inventory_row(row: dict) -> dict:
    return {field: row[field] for field in _VM_INVENTORY_FIELDS if field in row}


def _resource_id(value: object) -> str:
    return str(value or "").strip().rstrip("/").casefold()


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _allocation_summary(operation: dict) -> dict:
    name = str(operation.get("OperationName") or "")
    action = name.rsplit("/", 2)[-2] if name.casefold().endswith("/action") else name.rsplit("/", 1)[-1]
    return {
        "action": (
            "Create"
            if operation.get("_allocationSource") == "create"
            else action.capitalize()
        ),
        "outcome": str(operation.get("Outcome") or "").casefold(),
        "timestamp": _format_time(operation["_timestamp"]),
    }


def _failure_summary(operation: dict) -> dict:
    name = str(operation.get("OperationName") or "")
    action = name.rsplit("/", 2)[-2] if name.casefold().endswith("/action") else name.rsplit("/", 1)[-1]
    return {
        "action": (
            "Create"
            if operation.get("_allocationSource") == "create"
            else action.capitalize()
        ),
        "timestamp": _format_time(operation["_timestamp"]),
        "errorCode": operation.get("ErrorDetailCode") or operation.get("ErrorCode"),
        "errorMessage": operation.get("ErrorDetailMessage") or operation.get("ErrorMessage"),
    }


def _coverage_details(coverage: dict | None) -> dict:
    start = _parse_time((coverage or {}).get("coverageStart"))
    end = _parse_time((coverage or {}).get("coverageEnd"))
    versions = (coverage or {}).get("materializedRecipeVersions") or {}
    enabled = set((coverage or {}).get("enabledRecipeIds") or [])
    recipes_current = (
        {"vm-lifecycle", "vm-configuration"}.issubset(enabled)
        and versions.get("vm-lifecycle") == _LIFECYCLE_RECIPE.version
        and versions.get("vm-configuration") == _CONFIGURATION_RECIPE.version
    )
    return {"start": start, "end": end, "recipesCurrent": recipes_current}


def _query_subscription(subscription_id: str, coverage: dict | None) -> tuple[str, dict, list[dict]]:
    details = _coverage_details(coverage)
    end = details["end"]
    if end is None:
        return subscription_id, details, []
    rows = activity_log_store.query_operations_window(
        subscription_id,
        _format_time(end - timedelta(days=30)),
        _format_time(end),
        None,
        select=_OPERATION_COLUMNS,
    )
    return subscription_id, details, rows


def _attach_activity(rows: list[dict]) -> dict:
    by_subscription: dict[str, set[str]] = {}
    by_id: dict[str, dict] = {}
    for row in rows:
        resource_id = _resource_id(row.get("id"))
        if not resource_id:
            continue
        row["id"] = resource_id
        by_id[resource_id] = row
        subscription_id = str(row.get("subscriptionId") or "").casefold()
        by_subscription.setdefault(subscription_id, set()).add(resource_id)

    subscription_ids = sorted(key for key, ids in by_subscription.items() if ids)
    if not subscription_ids:
        return {
            "periods": list(_PERIOD_DAYS),
            "defaultPeriod": _DEFAULT_PERIOD,
            "confirmedFailureCodes": list(_CONFIRMED_FAILURE_CODES),
            "coverageStart": None,
            "coverageEnd": None,
        }

    coverages = {
        subscription_id: activity_log_store.get_coverage(subscription_id)
        for subscription_id in subscription_ids
    }
    with ThreadPoolExecutor(max_workers=min(_MAX_QUERY_WORKERS, len(subscription_ids))) as executor:
        results = list(executor.map(
            lambda subscription_id: _query_subscription(
                subscription_id, coverages[subscription_id]
            ),
            subscription_ids,
        ))

    activity_by_id: dict[str, list[dict]] = {resource_id: [] for resource_id in by_id}
    details_by_subscription: dict[str, dict] = {}
    for subscription_id, details, operations in results:
        details_by_subscription[subscription_id] = details
        for operation in operations:
            resource_id = _resource_id(operation.get("ResourceId"))
            timestamp = _parse_time(operation.get("LastEventTime"))
            operation_name = str(operation.get("OperationName") or "").casefold()
            outcome = str(operation.get("Outcome") or "").casefold()
            if (
                resource_id not in by_subscription[subscription_id]
                or timestamp is None
                or outcome not in {"succeeded", "failed"}
            ):
                continue
            if operation_name in _LIFECYCLE_OPERATION_NAMES_FOLDED:
                operation["_allocationSource"] = "lifecycle"
            elif (
                operation_name == _CREATE_OPERATION_NAME
                and str(operation.get("ProvisioningState") or "").casefold()
                == "creating"
            ):
                operation["_allocationSource"] = "create"
            else:
                continue
            operation["_timestamp"] = timestamp
            activity_by_id[resource_id].append(operation)

    for resource_id, row in by_id.items():
        subscription_id = str(row.get("subscriptionId") or "").casefold()
        details = details_by_subscription[subscription_id]
        end = details["end"]
        operations = activity_by_id[resource_id]
        periods = {}
        for key, days in _PERIOD_DAYS.items():
            start = end - timedelta(days=days) if end else None
            complete = bool(
                start
                and details["start"]
                and details["start"] <= start
                and details["recipesCurrent"]
            )
            selected = [
                operation for operation in operations
                if start is not None and start <= operation["_timestamp"] <= end
            ]
            failed = [
                operation for operation in selected
                if str(operation.get("Outcome") or "").casefold() == "failed"
            ]
            succeeded_lifecycle = sum(
                str(operation.get("Outcome") or "").casefold() == "succeeded"
                and operation.get("_allocationSource") == "lifecycle"
                for operation in selected
            )
            succeeded_creates = sum(
                str(operation.get("Outcome") or "").casefold() == "succeeded"
                and operation.get("_allocationSource") == "create"
                for operation in selected
            )
            failed_lifecycle = sum(
                operation.get("_allocationSource") == "lifecycle"
                for operation in failed
            )
            failed_creates = sum(
                operation.get("_allocationSource") == "create"
                for operation in failed
            )
            confirmed_lifecycle = sum(
                bool(operation.get("HasAllocationFailure"))
                and operation.get("_allocationSource") == "lifecycle"
                for operation in failed
            )
            confirmed_creates = sum(
                bool(operation.get("HasAllocationFailure"))
                and operation.get("_allocationSource") == "create"
                for operation in failed
            )
            periods[key] = {
                "start": _format_time(start) if start else None,
                "end": _format_time(end) if end else None,
                "complete": complete,
                "succeeded": succeeded_lifecycle + succeeded_creates,
                "failed": len(failed),
                "confirmedAllocationFailures": confirmed_lifecycle + confirmed_creates,
                "succeededLifecycle": succeeded_lifecycle,
                "succeededCreates": succeeded_creates,
                "failedLifecycle": failed_lifecycle,
                "failedCreates": failed_creates,
                "confirmedLifecycleAllocationFailures": confirmed_lifecycle,
                "confirmedCreateAllocationFailures": confirmed_creates,
            }
        latest = max(operations, key=lambda operation: operation["_timestamp"], default=None)
        failures = [
            operation for operation in operations
            if str(operation.get("Outcome") or "").casefold() == "failed"
        ]
        latest_failure = max(
            failures, key=lambda operation: operation["_timestamp"], default=None
        )
        row["allocation"] = {
            "periods": periods,
            "lastAllocation": _allocation_summary(latest) if latest else None,
            "lastFailure": _failure_summary(latest_failure) if latest_failure else None,
        }

    starts = [
        details["start"] for details in details_by_subscription.values()
        if details["start"] is not None
    ]
    ends = [
        details["end"] for details in details_by_subscription.values()
        if details["end"] is not None
    ]
    return {
        "periods": list(_PERIOD_DAYS),
        "defaultPeriod": _DEFAULT_PERIOD,
        "confirmedFailureCodes": list(_CONFIRMED_FAILURE_CODES),
        "coverageStart": (
            _format_time(max(starts))
            if len(starts) == len(details_by_subscription) else None
        ),
        "coverageEnd": (
            _format_time(min(ends))
            if len(ends) == len(details_by_subscription) else None
        ),
    }


def list_visible_vms(
    user_identity,
    *,
    subscription_ids: Iterable[str] | None = None,
    locations: Iterable[str] | None = None,
    context_filters: list[dict] | None = None,
) -> dict:
    """Return the user-visible VM inventory inside App Scope and report filters."""
    app_scope = resolve_app_scope(identity.for_backend_operation())
    dimensions = request_dimensions.open_current_view(
        {"subscriptions", "locations", "business_context"}
    )
    effective_subscriptions = dimensions.filter_subscriptions(
        app_scope.subscription_ids,
        subscription_ids,
        context_filters,
    )
    effective_locations = dimensions.filter_locations(
        app_scope.configured.locations,
        locations,
    )

    if subscription_ids == [] or locations == [] or not effective_subscriptions:
        rows = []
    else:
        rows = run_graph_query(
            build_vm_inventory_query(effective_locations),
            user_identity.get_token(),
            subscription_ids=effective_subscriptions,
        )
        pairs = zone_mapping_pairs(rows, [])
        mappings = zone_mapping.pipeline.get_mappings(pairs)
        apply_physical_zones(rows, [], mappings)
    dimensions.enrich_rows(rows)
    projected = [project_vm_inventory_row(row) for row in rows]
    activity_metadata = _attach_activity(projected)
    return {
        "count": len(projected),
        "metadata": {
            "schemaVersion": 2,
            **dimensions.metadata(),
            "activityLog": activity_metadata,
        },
        "value": projected,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timeline_recipe_metadata(coverage: dict | None) -> dict:
    enabled = set((coverage or {}).get("enabledRecipeIds") or [])
    materialized = (coverage or {}).get("materializedRecipeVersions") or {}
    return {
        recipe.id: {
            "version": recipe.version,
            "current": (
                recipe.id in enabled
                and materialized.get(recipe.id) == recipe.version
            ),
        }
        for recipe in _TIMELINE_RECIPES
    }


def _timeline_action(operation_name: str) -> str:
    parts = operation_name.rstrip("/").split("/")
    value = parts[-2] if parts[-1].casefold() == "action" and len(parts) > 1 else parts[-1]
    return value.capitalize()


def _timeline_details(operation: dict, recipe_id: str) -> dict:
    if recipe_id == "vm-lifecycle":
        fields = ()
    else:
        fields = (
            "VmSize",
            "Location",
            "Zones",
            "ProvisioningState",
            "CapacityReservationGroupId",
        )
    details = {}
    for field in fields:
        value = operation.get(field)
        if value is None:
            continue
        if field == "Zones" and isinstance(value, str):
            try:
                decoded = json.loads(value)
                value = decoded if isinstance(decoded, list) else value
            except (TypeError, ValueError):
                pass
        details[field[0].lower() + field[1:]] = value
    return details


def _timeline_error(operation: dict) -> dict | None:
    if not any(operation.get(field) is not None for field in (
        "ErrorCode",
        "ErrorMessage",
        "ErrorDetailCode",
        "ErrorDetailMessage",
        "ErrorDetailCount",
        "ErrorCodesJson",
        "HasAllocationFailure",
        "ErrorDetailsGz",
        "ErrorDetailsSpillBlob",
    )):
        return None
    codes = []
    encoded_codes = operation.get("ErrorCodesJson")
    if isinstance(encoded_codes, str):
        try:
            decoded_codes = json.loads(encoded_codes)
            if isinstance(decoded_codes, list):
                codes = [str(value) for value in decoded_codes]
        except json.JSONDecodeError:
            codes = []
    details = []
    payload = activity_log_store.load_error_details(operation)
    if payload:
        decoded_details = decompress_json(payload)
        if isinstance(decoded_details, list):
            details = decoded_details
    return {
        "code": operation.get("ErrorCode"),
        "message": operation.get("ErrorMessage"),
        "detailCode": operation.get("ErrorDetailCode"),
        "detailMessage": operation.get("ErrorDetailMessage"),
        "detailCount": int(operation.get("ErrorDetailCount") or 0),
        "codes": codes,
        "hasAllocationFailure": bool(operation.get("HasAllocationFailure")),
        "details": details,
    }


def get_vm_timeline(user_identity, resource_id: str) -> dict:
    """Return projected Activity Log history for one caller-visible VM."""
    normalized_id, subscription_id = validate_vm_resource_id(resource_id)
    app_scope = resolve_app_scope(identity.for_backend_operation())
    if subscription_id not in {
        value.casefold() for value in app_scope.subscription_ids
    }:
        raise AllocationEventVmNotFound()

    visible = run_graph_query(
        build_vm_visibility_query(normalized_id),
        user_identity.get_token(),
        subscription_ids=(subscription_id,),
    )
    vm = next(
        (
            row for row in visible
            if _resource_id(row.get("id")) == normalized_id
        ),
        None,
    )
    if vm is None:
        raise AllocationEventVmNotFound()
    retrieved_at = _utc_now()

    coverage = activity_log_store.get_coverage(subscription_id)
    start = _parse_time((coverage or {}).get("coverageStart"))
    end = _parse_time((coverage or {}).get("coverageEnd"))
    recipe_metadata = _timeline_recipe_metadata(coverage)
    complete = bool(
        start is not None
        and end is not None
        and start <= end
        and all(value["current"] for value in recipe_metadata.values())
    )
    target_end = retrieved_at.replace(hour=0, minute=0, second=0, microsecond=0)
    stale = end is None or end < target_end

    operations = []
    if start is not None and end is not None and start <= end:
        operations = activity_log_store.query_operations_window(
            subscription_id,
            _format_time(start),
            _format_time(end),
            None,
            select=_TIMELINE_COLUMNS,
        )

    events = []
    for operation in operations:
        if _resource_id(operation.get("ResourceId")) != normalized_id:
            continue
        operation_name = str(operation.get("OperationName") or "")
        recipe_id = _TIMELINE_OPERATIONS_FOLDED.get(operation_name.casefold())
        timestamp = _parse_time(operation.get("LastEventTime"))
        event_id = operation.get("OperationIdentityHash")
        if (
            recipe_id is None
            or timestamp is None
            or event_id is None
            or start is None
            or end is None
            or not start <= timestamp <= end
        ):
            continue
        event = {
            "id": str(event_id),
            "recipe": recipe_id,
            "type": "lifecycle" if recipe_id == "vm-lifecycle" else "configuration",
            "action": _timeline_action(operation_name),
            "timestamp": _format_time(timestamp),
            "outcome": str(operation.get("Outcome") or "").casefold(),
            "details": _timeline_details(operation, recipe_id),
            "_timestamp": timestamp,
        }
        error = _timeline_error(operation)
        if error is not None:
            event["error"] = error
        events.append(event)
    events.sort(key=lambda event: (event["_timestamp"], event["id"]), reverse=True)
    for event in events:
        event.pop("_timestamp")

    location_rows = [vm, *[
        event["details"]
        for event in events
        if event["recipe"] == "vm-configuration" and event["details"].get("location")
    ]]
    dimensions = request_dimensions.open_current_view({"locations"})
    dimensions.enrich_rows(location_rows)

    mapping_rows = [vm]
    for event in events:
        if event["recipe"] != "vm-configuration":
            continue
        details = event["details"]
        for logical_zone in details.get("zones") or []:
            mapping_rows.append({
                "subscriptionId": subscription_id,
                "location": details.get("location") or vm.get("location"),
                "logicalZone": logical_zone,
            })
    pairs = zone_mapping_pairs(mapping_rows, [])
    mappings = zone_mapping.pipeline.get_mappings(pairs) if pairs else {}
    apply_physical_zones(mapping_rows, [], mappings)
    mapping_index = 1
    for event in events:
        if event["recipe"] != "vm-configuration":
            continue
        zone_count = len(event["details"].get("zones") or [])
        if zone_count:
            event["details"]["physicalZones"] = [
                mapping_rows[index].get("physicalZone")
                for index in range(mapping_index, mapping_index + zone_count)
            ]
            mapping_index += zone_count

    current_details = {
        "vmSize": vm.get("vmSize") or "Unknown",
        "location": vm.get("location") or "Unknown",
        "locationDisplayName": vm.get("locationDisplayName") or vm.get("location") or "Unknown",
        "zones": [vm["logicalZone"]] if vm.get("logicalZone") else [],
        "physicalZones": [vm.get("physicalZone")] if vm.get("logicalZone") else [],
        "powerState": vm.get("powerState") or "Unknown",
        "provisioningState": vm.get("provisioningState") or "Unknown",
        "capacityReservationGroupId": vm.get("capacityReservationGroupId") or "None",
    }
    events.insert(0, {
        "id": "current-state",
        "recipe": "current-state",
        "type": "current",
        "action": "Current configuration",
        "timestamp": _format_time(retrieved_at),
        "outcome": "current",
        "details": current_details,
    })

    compact_vm = {
        field: vm[field] for field in _TIMELINE_VM_FIELDS if field in vm
    }
    compact_vm["id"] = normalized_id
    compact_vm["subscriptionId"] = subscription_id
    return {
        "metadata": {"schemaVersion": 1},
        "coverage": {
            "start": _format_time(start) if start else None,
            "end": _format_time(end) if end else None,
            "complete": complete,
            "stale": stale,
        },
        "recipes": recipe_metadata,
        "vm": compact_vm,
        "events": events,
    }
