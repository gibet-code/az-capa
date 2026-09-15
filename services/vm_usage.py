"""VM daily usage pipeline: the VM-specific Cost Management filter, wired to the
generic :class:`~services.usage_refresh.RefreshPipeline` over the VM usage tables.

Everything the refresh/flush triggers call lives on :data:`pipeline`; the only
VM-specific piece here is :func:`build_vm_filter`.
"""
from __future__ import annotations

from core import settings
from clients.cost_management import combine_filters, dimension_filter
from .usage_refresh import RefreshPipeline
from storage.usage_store import UsageStore

VM_METER_CATEGORY = "Virtual Machines"


def build_vm_filter(resource_ids=None, subscription_ids=None) -> dict | None:
    """Build the Cost Management filter for VM meters, with optional narrowing.

    Location is a read-time boundary (see ``docs/app-scope.md`` > Location Scope
    Enforcement), not a collection-time gate: collection stores the location
    superset within the subscription/billing boundary, so no ``ResourceLocation``
    filter is sent here.
    """
    filters = [dimension_filter("MeterCategory", [VM_METER_CATEGORY])]
    if subscription_ids:
        filters.append(dimension_filter("SubscriptionId", list(subscription_ids)))
    if resource_ids:
        filters.append(dimension_filter("ResourceId", list(resource_ids)))
    return combine_filters(filters)


_store = UsageStore(settings.vm_usage_table_name, settings.vm_usage_checkpoint_table_name)

# The single object the durable triggers delegate to (resolve_plan, status,
# fetch_and_store_page, delete_subscriptions, write_state, flush_*).
pipeline = RefreshPipeline(build_vm_filter, _store)
