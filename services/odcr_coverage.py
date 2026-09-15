"""ODCR coverage report queries and VM enrichment."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Iterable
from clients.resource_graph_kql import in_filter as _in_filter, normalize_values as _normalize_values

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import urllib3

logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)

_DEPLOYMENT_TYPE_PATTERN = re.compile(r"Zone [1-9][0-9]*\Z")
_VMSS_FLEXIBLE_VALUES = {"true", "false"}
_RESERVATION_STATUS_VALUES = {
    "Not associated",
    "Associated, no capacity",
    "Associated, capacity available",
    "Associated, capacity fully used",
    "Allocated, within capacity",
    "Allocated, overallocated",
    "Configuration issue",
}
_RESERVATION_API_VERSION = "2024-11-01"
_RESERVATION_MAX_CONCURRENCY = 40
_RESERVATION_MAX_RETRIES = 3
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_ODCR_REQUIREMENT_DEFINITIONS = {
    "regularPriority": {
        "label": "Not an Azure Spot instance",
        "failureReason": "Spot VMs aren't supported by on-demand capacity reservations.",
    },
    "dedicatedHost": {
        "label": "No dedicated host",
        "failureReason": "VMs deployed on Azure Dedicated Hosts aren't supported by ODCR.",
    },
    "availabilitySet": {
        "label": "No availability set",
        "failureReason": (
            "Availability sets aren't supported by ODCR. The VM's update-domain placement "
            "is inherited from this availability set and isn't reported as a separate failure."
        ),
    },
    "proximityPlacementGroup": {
        "label": "No proximity placement group",
        "failureReason": "Proximity placement groups are an unsupported ODCR deployment constraint.",
    },
    "ultraDisk": {
        "label": "No Ultra Disk Storage",
        "failureReason": "Azure Ultra Disk Storage isn't supported with ODCR.",
    },
    "legacyVMNVA": {
        "label": "Eligible for the ODCR capacity SLA (no LegacyVMNVA tag)",
        "failureReason": "The ODCR SLA doesn't apply to deployments using the LegacyVMNVA tag.",
    },
    "vmssPlacementModel": {"label": "VMSS placement model"},
    "vmSkuSupport": {
        "label": "VM SKU supports ODCR",
        "failureReason": "This VM SKU doesn't support on-demand capacity reservations in this region.",
    },
}
_RESERVATION_HTTP = urllib3.PoolManager(
    maxsize=_RESERVATION_MAX_CONCURRENCY,
    block=True,
)


def vm_list_metadata(business_context: dict | None = None) -> dict:
    """Return definitions shared by all compact VM list rows."""
    return {
        "schemaVersion": 3,
        "odcrRequirements": {
            "defaultStatus": "Passed",
            "definitions": [
                {"key": key, **definition}
                for key, definition in _ODCR_REQUIREMENT_DEFINITIONS.items()
            ],
        },
        "businessContext": business_context or {},
    }


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def project_vm_list_row(row: dict) -> dict:
    """Remove backend working fields and compact repeated prerequisite details."""
    projected = dict(row)
    projected["logicalZone"] = str(projected.get("logicalZone") or "").strip() or None
    projected["isVmssFlexible"] = _as_bool(projected.get("isVmssFlexible"))
    requirements = projected.get("odcrRequirements") or []
    sparse_requirements = []
    for requirement in requirements:
        status = requirement.get("status")
        if status == "Passed":
            continue
        key = requirement.get("key")
        result = {"key": key, "status": status}
        definition = _ODCR_REQUIREMENT_DEFINITIONS.get(key, {})
        reason = requirement.get("reason")
        if reason and reason != definition.get("failureReason"):
            result["reason"] = reason
        if status in {"Failed", "Unknown"}:
            observed_value = requirement.get("observedValue")
            resource_id = requirement.get("resourceId")
            if observed_value:
                result["observedValue"] = observed_value
            if resource_id:
                result["resourceId"] = resource_id
        sparse_requirements.append(result)

    projected["odcrRequirements"] = sparse_requirements
    for field in (
        "deploymentType",
        "azureSpot",
        "dedicatedHostId",
        "dedicatedHost",
        "isOnDedicatedHost",
        "availabilitySetId",
        "availabilitySet",
        "hasAvailabilitySet",
        "proximityPlacementGroupId",
        "proximityPlacementGroup",
        "hasProximityPlacementGroup",
        "hasUltraDiskStorageAttached",
        "hasLegacyVMNVATag",
        "capacityReservationSupported",
        "capacityReservationSupportReason",
        "reservationLifecycleStatus",
        "odcrUnsupportedReasons",
        "odcrUnknownReasons",
        "odcrUnsupportedRequirementCodes",
        "odcrUnknownRequirementCodes",
        "matchingCapacityReservationIds",
        "_physicalZoneResolved",
    ):
        projected.pop(field, None)
    return projected


def _validate_deployment_types(values: Iterable[str] | None) -> None:
    invalid = [
        value
        for value in (_normalize_values(values) or [])
        if value != "Regional" and not _DEPLOYMENT_TYPE_PATTERN.fullmatch(value)
    ]
    if invalid:
        raise ValueError(
            "Invalid deploymentType value(s): " + ", ".join(invalid)
            + ". Expected Regional or Zone N."
        )


def _validate_vmss_flexible(values: Iterable[str] | None) -> None:
    invalid = [
        value for value in (_normalize_values(values) or [])
        if value.lower() not in _VMSS_FLEXIBLE_VALUES
    ]
    if invalid:
        raise ValueError(
            "Invalid vmssFlexible value(s): " + ", ".join(invalid)
            + ". Expected true or false."
        )


def _bool_filter(field: str, values: Iterable[str] | None) -> str | None:
    normalized = _normalize_values(values)
    if not normalized:
        return None
    literals = ", ".join(value.lower() for value in normalized)
    return f"| where {field} in ({literals})"


def _validate_reservation_statuses(values: Iterable[str] | None) -> None:
    invalid = [value for value in (_normalize_values(values) or []) if value not in _RESERVATION_STATUS_VALUES]
    if invalid:
        raise ValueError(
            "Invalid reservationStatus value(s): " + ", ".join(invalid)
            + ". Expected a supported reservation lifecycle status."
        )


def build_vm_graph_query(
    *,
    resource_groups: Iterable[str] | None = None,
    locations: Iterable[str] | None = None,
    deployment_types: Iterable[str] | None = None,
    vm_sizes: Iterable[str] | None = None,
    vmss_flexible: Iterable[str] | None = None,
    reservation_statuses: Iterable[str] | None = None,
) -> str:
    """Build the user-scoped VM inventory query with optional multi-value filters."""
    _validate_deployment_types(deployment_types)
    _validate_vmss_flexible(vmss_flexible)
    _validate_reservation_statuses(reservation_statuses)

    native_filters = [
        _in_filter("resourceGroup", resource_groups),
        _in_filter("location", locations),
    ]
    computed_filters = [
        _in_filter("deploymentType", deployment_types),
        _in_filter("vmSize", vm_sizes),
        _bool_filter("isVmssFlexible", vmss_flexible),
    ]
    native_filter_kql = "\n".join(filter_value for filter_value in native_filters if filter_value)
    computed_filter_kql = "\n".join(filter_value for filter_value in computed_filters if filter_value)

    return f"""resources
| where type =~ 'microsoft.compute/virtualmachines'
{native_filter_kql}
| extend vmResourceId = tolower(id)
| extend logicalZone = tostring(zones[0])
| extend vmSize = tostring(properties.hardwareProfile.vmSize)
| extend deploymentType = iif(isempty(logicalZone), 'Regional', strcat('Zone ', logicalZone))
| extend isVmssFlexible = isnotnull(properties.virtualMachineScaleSet)
| extend azureSpot = isnotempty(properties.evictionPolicy)
| extend dedicatedHostId = tostring(properties.host.id)
| extend dedicatedHost = extract('/hosts/([^/]+)', 1, dedicatedHostId)
| extend isOnDedicatedHost = isnotempty(dedicatedHostId)
| extend availabilitySetId = tostring(properties.availabilitySet.id)
| extend availabilitySet = extract('/availabilitySets/([^/]+)', 1, availabilitySetId)
| extend hasAvailabilitySet = isnotempty(availabilitySetId)
| extend proximityPlacementGroupId = tostring(properties.proximityPlacementGroup.id)
| extend proximityPlacementGroup = extract('/proximityPlacementGroups/([^/]+)', 1, proximityPlacementGroupId)
| extend hasProximityPlacementGroup = isnotempty(proximityPlacementGroupId)
| extend hasUltraDiskStorageAttached = tostring(properties.storageProfile.dataDisks) contains 'UltraSSD_LRS'
| extend hasLegacyVMNVATag = bag_has_key(tags, 'LegacyVMNVA')
| extend capacityReservationGroupId = tolower(tostring(properties.capacityReservation.capacityReservationGroup.id))
| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
{computed_filter_kql}
| join kind=leftouter (
    resourcecontainers
    | where type =~ 'microsoft.resources/subscriptions'
    | project subscriptionId, subscriptionName = name
) on subscriptionId
| project id = vmResourceId,
    resourceType = tolower(type),
    subscriptionId,
    subscriptionName,
    resourceGroup,
    name,
    location,
    logicalZone,
    vmSize,
    isVmssFlexible,
    azureSpot,
    dedicatedHostId,
    dedicatedHost,
    isOnDedicatedHost,
    availabilitySetId,
    availabilitySet,
    hasAvailabilitySet,
    proximityPlacementGroupId,
    proximityPlacementGroup,
    hasProximityPlacementGroup,
    hasUltraDiskStorageAttached,
    hasLegacyVMNVATag,
    capacityReservationGroupId,
    powerState
| order by id asc"""


def build_capacity_reservation_graph_query() -> str:
    """Build the user-visible CRG and Capacity Reservation metadata query."""
    return """resources
| where type =~ 'microsoft.compute/capacityreservationgroups'
| project capacityReservationGroupId = tolower(id), capacityReservationGroupName = name
| join kind=leftouter (
    resources
    | where type =~ 'microsoft.compute/capacityreservationgroups/capacityreservations'
    | extend capacityReservationGroupId = tolower(substring(id, 0, indexof(tolower(id), '/capacityreservations/')))
    | project capacityReservationGroupId,
        matchingCapacityReservationId = tolower(id),
        reservationSubscriptionId = tolower(subscriptionId),
        reservationLocation = tolower(location),
        reservationZone = tostring(zones[0]),
        reservationVmSize = tostring(sku.name)
) on capacityReservationGroupId
| project capacityReservationGroupId,
    capacityReservationGroupName,
    matchingCapacityReservationId,
    reservationSubscriptionId,
    reservationLocation,
    reservationZone,
    reservationVmSize"""


def zone_mapping_pairs(vm_rows: list[dict], reservation_rows: list[dict]) -> set[tuple[str, str]]:
    """Return unique subscription/location pairs needed for physical-zone mapping."""
    pairs = {
        (str(row.get("subscriptionId") or "").strip().lower(), str(row.get("location") or "").strip().lower())
        for row in vm_rows
    }
    pairs.update({
        (
            str(row.get("reservationSubscriptionId") or "").strip().lower(),
            str(row.get("reservationLocation") or "").strip().lower(),
        )
        for row in reservation_rows
    })
    return {(subscription_id, location) for subscription_id, location in pairs if subscription_id and location}


def apply_physical_zones(
    vm_rows: list[dict],
    reservation_rows: list[dict],
    mappings: dict[tuple[str, str], dict[str, str]],
) -> None:
    """Attach physical zones while preserving unresolved zonal rows as unknown."""
    def apply(row: dict, subscription_field: str, location_field: str, zone_field: str) -> None:
        logical_zone = str(row.get(zone_field) or "").strip()
        if not logical_zone:
            row["physicalZone"] = None
            row["_physicalZoneResolved"] = True
            return
        pair = (
            str(row.get(subscription_field) or "").strip().lower(),
            str(row.get(location_field) or "").strip().lower(),
        )
        physical_zone = (mappings.get(pair) or {}).get(logical_zone)
        row["physicalZone"] = physical_zone
        row["_physicalZoneResolved"] = bool(physical_zone)

    for row in vm_rows:
        apply(row, "subscriptionId", "location", "logicalZone")
    for row in reservation_rows:
        apply(row, "reservationSubscriptionId", "reservationLocation", "reservationZone")


def reconcile_vm_reservations(vm_rows: list[dict], reservation_rows: list[dict]) -> list[dict]:
    """Attach CRG names and physical-zone-matched reservation candidates to VMs."""
    group_names: dict[str, str] = {}
    broad_candidates: dict[tuple[str, str], set[str]] = {}
    strict_candidates: dict[tuple[str, str, str | None], set[str]] = {}
    unresolved_keys: set[tuple[str, str]] = set()

    for row in reservation_rows:
        group_id = str(row.get("capacityReservationGroupId") or "").strip().lower()
        if not group_id:
            continue
        group_name = row.get("capacityReservationGroupName")
        if group_name:
            group_names[group_id] = str(group_name)
        reservation_id = str(row.get("matchingCapacityReservationId") or "").strip().lower()
        vm_size = str(row.get("reservationVmSize") or "").strip().lower()
        if not reservation_id or not vm_size:
            continue
        broad_key = (group_id, vm_size)
        broad_candidates.setdefault(broad_key, set()).add(reservation_id)
        if row.get("_physicalZoneResolved") is not True:
            unresolved_keys.add(broad_key)
            continue
        strict_key = (group_id, vm_size, row.get("physicalZone"))
        strict_candidates.setdefault(strict_key, set()).add(reservation_id)

    for row in vm_rows:
        group_id = str(row.get("capacityReservationGroupId") or "").strip().lower()
        row["capacityReservationGroupName"] = group_names.get(group_id) if group_id else None
        vm_size = str(row.get("vmSize") or "").strip().lower()
        broad_key = (group_id, vm_size)
        if not group_id or not vm_size:
            candidates = None
        elif row.get("_physicalZoneResolved") is not True or broad_key in unresolved_keys:
            candidates = broad_candidates.get(broad_key)
        else:
            candidates = strict_candidates.get((group_id, vm_size, row.get("physicalZone")))
        row["matchingCapacityReservationIds"] = sorted(candidates) if candidates else None
        row.pop("_physicalZoneResolved", None)
    return vm_rows


def _retry_after_seconds(headers) -> float:
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
            except (TypeError, ValueError, OverflowError):
                pass
    raw_ms = headers.get("x-ms-retry-after-ms") if headers else None
    if raw_ms:
        try:
            return max(float(raw_ms) / 1000, 0.0)
        except ValueError:
            pass
    return 1.0


def _fetch_reservation_instance_view(reservation_id: str, token: str) -> dict:
    query = urllib.parse.urlencode({"api-version": _RESERVATION_API_VERSION, "$expand": "instanceView"})
    url = f"https://management.azure.com{reservation_id}?{query}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    for attempt in range(_RESERVATION_MAX_RETRIES + 1):
        try:
            response = _RESERVATION_HTTP.request(
                "GET",
                url,
                headers=headers,
                timeout=urllib3.Timeout(connect=10, read=120),
                retries=False,
            )
            if response.status < 400:
                return json.loads(response.data.decode("utf-8"))
            if response.status not in _RETRYABLE_STATUSES or attempt == _RESERVATION_MAX_RETRIES:
                detail = response.data.decode("utf-8", "replace")
                raise RuntimeError(f"Capacity Reservation request failed ({response.status}): {detail}")
            wait = _retry_after_seconds(response.headers)
            logging.warning(
                "Capacity Reservation HTTP %s; retrying in %.1fs (%d/%d)",
                response.status, wait, attempt + 1, _RESERVATION_MAX_RETRIES,
            )
            time.sleep(wait)
        except urllib3.exceptions.HTTPError as exc:
            if attempt == _RESERVATION_MAX_RETRIES:
                raise RuntimeError(f"Capacity Reservation request failed: {exc}") from exc
            wait = 1.0
            logging.warning(
                "Capacity Reservation network error; retrying in %.1fs (%d/%d): %s",
                wait, attempt + 1, _RESERVATION_MAX_RETRIES, exc,
            )
            time.sleep(wait)
    raise RuntimeError("Capacity Reservation request exhausted retries")


def _resource_ids(items) -> set[str]:
    return {
        str(item.get("id") or "").lower()
        for item in (items or [])
        if isinstance(item, dict) and item.get("id")
    }


def enrich_vm_reservation_lifecycle(rows: list[dict], token: str) -> list[dict]:
    """Enrich VM rows from authoritative Capacity Reservation instance views."""
    reservation_ids = sorted({
        reservation_id
        for row in rows
        for reservation_id in (row.get("matchingCapacityReservationIds") or [])
        if reservation_id
    })
    reservations: dict[str, dict] = {}
    if reservation_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_RESERVATION_MAX_CONCURRENCY) as executor:
            future_by_id = {
                executor.submit(_fetch_reservation_instance_view, reservation_id, token): reservation_id
                for reservation_id in reservation_ids
            }
            for future in concurrent.futures.as_completed(future_by_id):
                reservation_id = future_by_id[future]
                reservations[reservation_id] = future.result()

    for row in rows:
        row.update({
            "matchingCapacityReservationId": None,
            "matchingCapacityReservationName": None,
            "reservationCapacity": None,
            "reservationAssociatedCount": None,
            "reservationAllocatedCount": None,
            "isCapacityReservationAllocated": None,
        })
        group_id = row.get("capacityReservationGroupId") or ""
        if not group_id:
            row["reservationStatus"] = "Not associated"
            continue
        vm_id = str(row.get("id") or "").lower()
        candidate_ids = row.get("matchingCapacityReservationIds") or []
        matches = []
        for reservation_id in candidate_ids:
            reservation = reservations.get(reservation_id)
            if not reservation:
                continue
            properties = reservation.get("properties") or {}
            associated_ids = _resource_ids(properties.get("virtualMachinesAssociated"))
            if vm_id in associated_ids:
                matches.append((reservation_id, reservation, associated_ids))
        if len(matches) != 1:
            row["reservationStatus"] = "Configuration issue"
            continue

        reservation_id, reservation, associated_ids = matches[0]
        properties = reservation.get("properties") or {}
        instance_view = properties.get("instanceView") or {}
        utilization = instance_view.get("utilizationInfo") or {}
        allocated_ids = _resource_ids(utilization.get("virtualMachinesAllocated"))
        capacity = int((reservation.get("sku") or {}).get("capacity") or 0)
        is_allocated = vm_id in allocated_ids
        row.update({
            "matchingCapacityReservationId": reservation_id,
            "matchingCapacityReservationName": reservation.get("name") or reservation_id.rsplit("/", 1)[-1],
            "reservationCapacity": capacity,
            "reservationAssociatedCount": len(associated_ids),
            "reservationAllocatedCount": len(allocated_ids),
            "isCapacityReservationAllocated": is_allocated,
        })
        if capacity <= 0:
            row["reservationStatus"] = "Associated, no capacity"
        elif is_allocated and len(allocated_ids) <= capacity:
            row["reservationStatus"] = "Allocated, within capacity"
        elif is_allocated:
            row["reservationStatus"] = "Allocated, overallocated"
        elif len(allocated_ids) < capacity:
            row["reservationStatus"] = "Associated, capacity available"
        else:
            row["reservationStatus"] = "Associated, capacity fully used"
    return rows


def apply_odcr_eligibility(rows: list[dict]) -> list[dict]:
    """Attach ODCR prerequisite results and override unsupported VM statuses."""
    for row in rows:
        requirements = [
            {
                "key": "regularPriority",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["regularPriority"]["label"],
                "status": "Failed" if _as_bool(row.get("azureSpot")) else "Passed",
                "observedValue": f"Azure Spot: {'Yes' if _as_bool(row.get('azureSpot')) else 'No'}",
                "reason": _ODCR_REQUIREMENT_DEFINITIONS["regularPriority"]["failureReason"]
                if _as_bool(row.get("azureSpot")) else None,
                "resourceId": row.get("id"),
            },
            {
                "key": "dedicatedHost",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["dedicatedHost"]["label"],
                "status": "Failed" if _as_bool(row.get("isOnDedicatedHost")) else "Passed",
                "observedValue": row.get("dedicatedHost") or "No dedicated host",
                "reason": _ODCR_REQUIREMENT_DEFINITIONS["dedicatedHost"]["failureReason"]
                if _as_bool(row.get("isOnDedicatedHost")) else None,
                "resourceId": row.get("dedicatedHostId") or None,
            },
            {
                "key": "availabilitySet",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["availabilitySet"]["label"],
                "status": "Failed" if _as_bool(row.get("hasAvailabilitySet")) else "Passed",
                "observedValue": row.get("availabilitySet") or "No availability set",
                "reason": _ODCR_REQUIREMENT_DEFINITIONS["availabilitySet"]["failureReason"]
                if _as_bool(row.get("hasAvailabilitySet")) else None,
                "resourceId": row.get("availabilitySetId") or None,
            },
            {
                "key": "proximityPlacementGroup",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["proximityPlacementGroup"]["label"],
                "status": "Failed" if _as_bool(row.get("hasProximityPlacementGroup")) else "Passed",
                "observedValue": row.get("proximityPlacementGroup") or "No proximity placement group",
                "reason": _ODCR_REQUIREMENT_DEFINITIONS["proximityPlacementGroup"]["failureReason"]
                if _as_bool(row.get("hasProximityPlacementGroup")) else None,
                "resourceId": row.get("proximityPlacementGroupId") or None,
            },
            {
                "key": "ultraDisk",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["ultraDisk"]["label"],
                "status": "Failed" if _as_bool(row.get("hasUltraDiskStorageAttached")) else "Passed",
                "observedValue": f"Ultra Disk attached: {'Yes' if _as_bool(row.get('hasUltraDiskStorageAttached')) else 'No'}",
                "reason": _ODCR_REQUIREMENT_DEFINITIONS["ultraDisk"]["failureReason"]
                if _as_bool(row.get("hasUltraDiskStorageAttached")) else None,
                "resourceId": row.get("id"),
            },
            {
                "key": "legacyVMNVA",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["legacyVMNVA"]["label"],
                "status": "Failed" if _as_bool(row.get("hasLegacyVMNVATag")) else "Passed",
                "observedValue": f"LegacyVMNVA tag: {'Yes' if _as_bool(row.get('hasLegacyVMNVATag')) else 'No'}",
                "reason": _ODCR_REQUIREMENT_DEFINITIONS["legacyVMNVA"]["failureReason"]
                if _as_bool(row.get("hasLegacyVMNVATag")) else None,
                "resourceId": row.get("id"),
            },
            {
                "key": "vmssPlacementModel",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["vmssPlacementModel"]["label"],
                "status": "Not applicable",
                "observedValue": "Standalone VM inventory",
                "reason": None,
                "resourceId": None,
            },
            {
                "key": "vmSkuSupport",
                "label": _ODCR_REQUIREMENT_DEFINITIONS["vmSkuSupport"]["label"],
                "status": (
                    "Passed" if row.get("capacityReservationSupported") is True
                    else "Failed" if row.get("capacityReservationSupported") is False
                    else "Unknown"
                ),
                "observedValue": (
                    f"{row.get('vmSize') or 'Unknown VM size'} in "
                    f"{row.get('location') or 'unknown location'}: "
                    + (
                        "Supported" if row.get("capacityReservationSupported") is True
                        else "Not supported" if row.get("capacityReservationSupported") is False
                        else "Unknown"
                    )
                ),
                "reason": (
                    _ODCR_REQUIREMENT_DEFINITIONS["vmSkuSupport"]["failureReason"]
                    if row.get("capacityReservationSupported") is False
                    else row.get("capacityReservationSupportReason")
                    if row.get("capacityReservationSupported") is None
                    else None
                ),
                "resourceId": None,
            },
        ]
        row["odcrRequirements"] = requirements
        failed = [requirement for requirement in requirements if requirement["status"] == "Failed"]
        unknown = [requirement for requirement in requirements if requirement["status"] == "Unknown"]
        row["odcrUnsupportedReasons"] = [requirement["label"] for requirement in failed]
        row["odcrUnknownReasons"] = [
            requirement.get("reason") or requirement["label"] for requirement in unknown
        ]
        row["odcrSupportabilityStatus"] = (
            "Unsupported" if failed else "Unknown" if unknown else "Supported"
        )
    return rows


def filter_vm_reservation_statuses(rows: list[dict], statuses: Iterable[str] | None) -> list[dict]:
    _validate_reservation_statuses(statuses)
    selected = set(_normalize_values(statuses) or [])
    return rows if not selected else [row for row in rows if row.get("reservationStatus") in selected]