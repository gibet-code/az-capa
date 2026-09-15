"""ODCR usage inventory query and enrichment helpers."""
from __future__ import annotations

from collections.abc import Iterable

from clients.resource_graph_kql import in_filter as _in_filter

def build_capacity_reservation_group_graph_query(
    *,
    locations: Iterable[str] | None = None,
) -> str:
    """Build the user-visible capacity reservation group inventory query."""
    filters = [
        _in_filter("location", locations),
    ]
    filter_kql = "\n".join(value for value in filters if value)
    return f"""resources
| where type =~ 'microsoft.compute/capacityreservationgroups'
{filter_kql}
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
| order by id asc"""


def build_capacity_reservation_graph_query() -> str:
    """Build the user-visible capacity reservation inventory query."""
    return """resources
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
| order by id asc"""


def zone_mapping_pairs(rows: list[dict]) -> set[tuple[str, str]]:
    """Return subscription/location pairs required by ODCR usage rows."""
    pairs = {
        (
            str(row.get("subscriptionId") or "").strip().lower(),
            str(row.get("location") or "").strip().lower(),
        )
        for row in rows
    }
    return {(subscription_id, location) for subscription_id, location in pairs if subscription_id and location}


def apply_physical_zones(
    rows: list[dict],
    mappings: dict[tuple[str, str], dict[str, str]],
) -> None:
    """Attach physical zones to capacity reservation rows."""
    for row in rows:
        logical_zone = str(row.get("logicalZone") or "").strip()
        if not logical_zone:
            row["logicalZone"] = None
            row["physicalZone"] = None
            continue
        pair = (
            str(row.get("subscriptionId") or "").strip().lower(),
            str(row.get("location") or "").strip().lower(),
        )
        row["logicalZone"] = logical_zone
        row["physicalZone"] = (mappings.get(pair) or {}).get(logical_zone)
