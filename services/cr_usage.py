"""Capacity-reservation daily usage pipeline: the CR-specific Cost Management
filter, wired to the generic :class:`~services.usage_refresh.RefreshPipeline` over
the capacity-reservation usage tables.

Mirrors :mod:`services.vm_usage`; the only CR-specific piece is
:func:`build_cr_filter`. Everything else (scope resolution, planning, paging,
flush) is shared through :class:`RefreshPipeline`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core import settings
from clients.cost_management import combine_filters, dimension_filter
from .usage_refresh import RefreshPipeline
from storage.usage_store import UsageStore

# Cost Management ``ResourceType`` dimension value for capacity reservation
# groups. Unused-reservation charges are attributed to the reservation group
# resource, so filtering on this type isolates them from ordinary VM meters.
# NOTE: verify against your billing data — adjust if CR usage surfaces under a
# different ResourceType/MeterCategory in your agreement.
CR_RESOURCE_TYPES = ["microsoft.compute/capacityreservationgroups"]


def build_cr_filter(resource_ids=None, subscription_ids=None) -> dict | None:
    """Build the Cost Management filter for capacity-reservation usage.

    Location is a read-time boundary (see ``docs/app-scope.md`` > Location Scope
    Enforcement), not a collection-time gate: collection stores the location
    superset within the subscription/billing boundary, so no ``ResourceLocation``
    filter is sent here.
    """
    filters = [dimension_filter("ResourceType", CR_RESOURCE_TYPES)]
    if subscription_ids:
        filters.append(dimension_filter("SubscriptionId", list(subscription_ids)))
    if resource_ids:
        filters.append(dimension_filter("ResourceId", list(resource_ids)))
    return combine_filters(filters)


_store = UsageStore(
    settings.cr_usage_table_name,
    settings.cr_usage_checkpoint_table_name,
    settings.cr_usage_summary_table_name,
)

# The single object the durable triggers delegate to (resolve_plan, status,
# fetch_and_store_page, delete_subscriptions, write_state, flush_*).
pipeline = RefreshPipeline(build_cr_filter, _store)

USAGE_FIELDS = (
    "unusedHours",
    "unusedCost",
    "lastDayUnusedHours",
    "lastDayUnusedCost",
    "last7DaysUnusedHours",
    "last7DaysUnusedCost",
    "last30DaysUnusedHours",
    "last30DaysUnusedCost",
)


def _resource_identity(resource_id: str) -> dict | None:
    parts = resource_id.strip().lower().split("/")
    try:
        subscription_index = parts.index("subscriptions")
        resource_group_index = parts.index("resourcegroups")
        group_index = parts.index("capacityreservationgroups")
        reservation_index = parts.index("capacityreservations")
        subscription_id = parts[subscription_index + 1]
        resource_group = parts[resource_group_index + 1]
        group_name = parts[group_index + 1]
        reservation_name = parts[reservation_index + 1]
    except (ValueError, IndexError):
        return None
    return {
        "id": resource_id.strip().lower(),
        "resourceType": "microsoft.compute/capacityreservationgroups/capacityreservations",
        "name": reservation_name,
        "subscriptionId": subscription_id,
        "subscriptionName": None,
        "resourceGroup": resource_group,
        "location": None,
        "logicalZone": None,
        "physicalZone": None,
        "vmSize": None,
        "capacityReservationGroupId": "/".join(parts[:group_index + 2]),
        "capacityReservationGroupName": group_name,
    }


def _rolling_total(values: list[float | None], days: int) -> float | None:
    window = values[-days:]
    if len(window) < days or any(value is None for value in window):
        return None
    return sum(window)


def usage_view(subscription_ids) -> tuple[dict, dict[str, dict]]:
    """Return response metadata and rolling usage rows keyed by resource ID."""
    state = _store.get_state()
    finalization_lag_days = settings.cost_finalization_lag_days()
    if state is None:
        return {
            "schemaVersion": 1,
            "usage": {
                "currency": None,
                "dates": [],
                "coverageStart": None,
                "coverageEnd": None,
                "finalizationLagDays": finalization_lag_days,
                "isAvailable": False,
            },
        }, {}

    coverage_start = datetime.strptime(state["coverageStart"], "%Y-%m-%d").date()
    coverage_end = datetime.strptime(state["coverageEnd"], "%Y-%m-%d").date()
    dates = [coverage_end - timedelta(days=offset) for offset in range(29, -1, -1)]
    date_strings = [value.isoformat() for value in dates]
    if not _store.projection_is_current(state):
        return {
            "schemaVersion": 1,
            "usage": {
                "currency": None,
                "dates": date_strings,
                "coverageStart": state["coverageStart"],
                "coverageEnd": state["coverageEnd"],
                "finalizationLagDays": finalization_lag_days,
                "isAvailable": False,
            },
        }, {}
    summaries: dict[str, dict] = {}
    currencies = set()
    for entity in _store.query_usage_summaries(subscription_ids):
        identity = _resource_identity(str(entity.get("ResourceId") or ""))
        if identity is None:
            continue
        hours: list[float | None] = []
        costs: list[float | None] = []
        has_current_value = False
        for usage_date, date_text in zip(dates, date_strings):
            if usage_date < coverage_start:
                hours.append(None)
                costs.append(None)
                continue
            slot = usage_date.toordinal() % 30
            suffix = f"{slot:02d}"
            hours_match = entity.get(f"HoursDate{suffix}") == date_text
            cost_match = entity.get(f"CostDate{suffix}") == date_text
            hours.append(float(entity.get(f"Hours{suffix}") or 0.0) if hours_match else 0.0)
            costs.append(float(entity.get(f"Cost{suffix}") or 0.0) if cost_match else 0.0)
            has_current_value = has_current_value or hours_match or cost_match
        if not has_current_value:
            continue
        currency = str(entity.get("Currency") or "").strip()
        if currency:
            currencies.add(currency)
        summaries[identity["id"]] = {
            **identity,
            "unusedHours": hours,
            "unusedCost": costs,
            "lastDayUnusedHours": _rolling_total(hours, 1),
            "lastDayUnusedCost": _rolling_total(costs, 1),
            "last7DaysUnusedHours": _rolling_total(hours, 7),
            "last7DaysUnusedCost": _rolling_total(costs, 7),
            "last30DaysUnusedHours": _rolling_total(hours, 30),
            "last30DaysUnusedCost": _rolling_total(costs, 30),
        }
    if len(currencies) > 1:
        raise ValueError(f"CR usage contains multiple currencies: {sorted(currencies)}")
    return {
        "schemaVersion": 1,
        "usage": {
            "currency": next(iter(currencies), None),
            "dates": date_strings,
            "coverageStart": state["coverageStart"],
            "coverageEnd": state["coverageEnd"],
            "finalizationLagDays": finalization_lag_days,
            "isAvailable": True,
        },
    }, summaries


def empty_usage(metadata: dict) -> dict:
    """Return a zero/null usage payload aligned to the shared response dates."""
    usage = metadata["usage"]
    coverage_start = usage.get("coverageStart")
    values = [
        None if not usage.get("isAvailable") or (coverage_start and date_text < coverage_start) else 0.0
        for date_text in usage.get("dates") or []
    ]
    return {
        "unusedHours": list(values),
        "unusedCost": list(values),
        "lastDayUnusedHours": _rolling_total(values, 1),
        "lastDayUnusedCost": _rolling_total(values, 1),
        "last7DaysUnusedHours": _rolling_total(values, 7),
        "last7DaysUnusedCost": _rolling_total(values, 7),
        "last30DaysUnusedHours": _rolling_total(values, 30),
        "last30DaysUnusedCost": _rolling_total(values, 30),
    }


def usage_metrics(summary: dict) -> dict:
    """Project only usage fields when enriching a live Resource Graph row."""
    return {field: summary.get(field) for field in USAGE_FIELDS}
