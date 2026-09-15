"""Typed app-settings accessors with defaults.

Single source of truth for configuration read from environment variables (App
Settings / ``local.settings.json``). Grouped as:

* ``SCOPE_*``            — global scoping, reused app-wide.
* ``COST_MANAGEMENT_*``  — shared by any Cost Management consumer.
* ``USAGE_*`` / ``VM_*`` — the VM usage pipeline.
* storage               — Table Storage connection resolution.
"""
from __future__ import annotations

import os

# Cost Management retrieval methods.
METHOD_PER_SUB = "per-sub"
METHOD_PER_BILLING = "per-billing"
VALID_METHODS = (METHOD_PER_SUB, METHOD_PER_BILLING)

# Billing agreement type. On EA, AmortizedCost.UsageQuantity double-counts
# savings-plan hours, so hours (ActualCost) and cost (AmortizedCost) need two
# queries. On MCA a single AmortizedCost query returns correct hours AND cost.
AGREEMENT_EA = "ea"
AGREEMENT_MCA = "mca"
VALID_AGREEMENTS = (AGREEMENT_EA, AGREEMENT_MCA)

def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_list(name: str) -> list[str]:
    raw = _get(name)
    if not raw:
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


# ── Global scope ──────────────────────────────────────────────────────────────
def scope_subscription_ids() -> list[str]:
    return _get_list("SCOPE_SUBSCRIPTION_IDS")


def scope_management_group_ids() -> list[str]:
    return _get_list("SCOPE_MANAGEMENT_GROUP_IDS")


def scope_locations() -> list[str]:
    """Configured ARM location codes; consumers canonicalize them for matching."""
    return _get_list("SCOPE_LOCATIONS")


def validate_scope_exclusivity(subscription_ids, management_group_ids) -> None:
    """Enforce that subscription and management-group scoping are mutually exclusive."""
    if subscription_ids and management_group_ids:
        raise ValueError(
            "Subscription IDs and management group IDs are mutually exclusive — "
            "provide one or the other, not both."
        )


# ── Cost Management (shared by any CM consumer) ───────────────────────────────
def cost_management_method() -> str:
    method = (_get("COST_MANAGEMENT_METHOD", METHOD_PER_SUB) or METHOD_PER_SUB).lower()
    if method not in VALID_METHODS:
        raise ValueError(f"COST_MANAGEMENT_METHOD must be one of {VALID_METHODS}, got {method!r}.")
    return method


def cost_management_agreement_type() -> str:
    agreement = (_get("COST_MANAGEMENT_AGREEMENT_TYPE", AGREEMENT_EA) or AGREEMENT_EA).lower()
    if agreement not in VALID_AGREEMENTS:
        raise ValueError(f"COST_MANAGEMENT_AGREEMENT_TYPE must be one of {VALID_AGREEMENTS}, got {agreement!r}.")
    return agreement


def cost_management_billing_account_id() -> str | None:
    return _get("COST_MANAGEMENT_BILLING_ACCOUNT_ID")


def cost_management_billing_profile_id() -> str | None:
    return _get("COST_MANAGEMENT_BILLING_PROFILE_ID")


def cost_management_max_concurrency() -> int:
    return _get_int("COST_MANAGEMENT_MAX_CONCURRENCY", 8)


def cost_management_time_chunk_days() -> int:
    return _get_int("COST_MANAGEMENT_TIME_CHUNK_DAYS", 0)


def cost_finalization_lag_days() -> int:
    return _get_int("COST_FINALIZATION_LAG_DAYS", 2)


# ── VM usage pipeline ─────────────────────────────────────────────────────────
def usage_historical_days() -> int:
    return _get_int("USAGE_HISTORICAL_DAYS", 30)


def vm_usage_table_name() -> str:
    return _get("VM_USAGE_TABLE_NAME", "VmDailyUsage")


def vm_usage_checkpoint_table_name() -> str:
    return _get("VM_USAGE_CHECKPOINT_TABLE_NAME", "VmUsageCheckpoints")


# ── Capacity reservation usage pipeline ───────────────────────────────────────
def cr_usage_table_name() -> str:
    return _get("CR_USAGE_TABLE_NAME", "CrDailyUsage")


def cr_usage_checkpoint_table_name() -> str:
    return _get("CR_USAGE_CHECKPOINT_TABLE_NAME", "CrUsageCheckpoints")


def cr_usage_summary_table_name() -> str:
    return _get("CR_USAGE_SUMMARY_TABLE_NAME", "CrUsageSummaries")


# ── Activity Log pipeline ────────────────────────────────────────────────────
def activity_log_historical_days() -> int:
    return min(max(_get_int("ACTIVITY_LOG_HISTORICAL_DAYS", 30), 1), 89)


def activity_log_refresh_overlap_hours() -> int:
    return max(_get_int("ACTIVITY_LOG_REFRESH_OVERLAP_HOURS", 24), 0)


def activity_log_ingestion_lag_minutes() -> int:
    return max(_get_int("ACTIVITY_LOG_INGESTION_LAG_MINUTES", 20), 0)


def activity_log_max_concurrency() -> int:
    return max(_get_int("ACTIVITY_LOG_MAX_CONCURRENCY", 20), 1)


def activity_log_replay_max_units_per_run() -> int:
    return max(_get_int("ACTIVITY_LOG_REPLAY_MAX_UNITS_PER_RUN", 200), 0)


def activity_log_pending_unknown_hours() -> int:
    return max(_get_int("ACTIVITY_LOG_PENDING_UNKNOWN_HOURS", 48), 0)


def activity_log_operations_table_name() -> str:
    return _get("ACTIVITY_LOG_OPERATIONS_TABLE_NAME", "ActivityLogOperations")


def activity_log_collection_state_table_name() -> str:
    return _get("ACTIVITY_LOG_COLLECTION_STATE_TABLE_NAME", "ActivityLogCollectionState")


def activity_log_spill_container_name() -> str:
    return _get("ACTIVITY_LOG_SPILL_BLOB_CONTAINER", "activity-log-spill")


# ── Zone mapping pipeline ─────────────────────────────────────────────────────
def zone_mapping_max_concurrency() -> int:
    return max(_get_int("ZONE_MAPPING_MAX_CONCURRENCY", 15), 1)


def zone_mapping_blob_container_name() -> str:
    return _get("ZONE_MAPPING_BLOB_CONTAINER", "zone-mappings")


# ── Compute SKU catalog pipeline ─────────────────────────────────────────────
def compute_sku_blob_container_name() -> str:
    return _get("COMPUTE_SKU_BLOB_CONTAINER", "compute-skus")


# ── Azure Reference Data ─────────────────────────────────────────────────────
def azure_reference_data_blob_container_name() -> str:
    return _get("AZURE_REFERENCE_DATA_BLOB_CONTAINER", "azure-reference-data")


def azure_reference_data_l1_ttl_seconds() -> int:
    return max(_get_int("AZURE_REFERENCE_DATA_L1_TTL_SECONDS", 60), 1)


def subscription_reference_max_stale_minutes() -> int:
    return max(_get_int("SUBSCRIPTION_REFERENCE_MAX_STALE_MINUTES", 60), 1)


def location_reference_max_stale_hours() -> int:
    return max(_get_int("LOCATION_REFERENCE_MAX_STALE_HOURS", 48), 1)


# ── User-authored ODCR coverage decisions ────────────────────────────────────
def odcr_coverage_decisions_table_name() -> str:
    return _get("ODCR_COVERAGE_DECISIONS_TABLE_NAME", "OdcrCoverageDecisions")


# ── Business Context ─────────────────────────────────────────────────────────
def subscription_context_max_fields() -> int:
    return min(max(_get_int("SUBSCRIPTION_CONTEXT_MAX_FIELDS", 5), 1), 20)


def subscription_context_max_upload_bytes() -> int:
    return max(_get_int("SUBSCRIPTION_CONTEXT_MAX_UPLOAD_BYTES", 10 * 1024 * 1024), 1)


def subscription_context_admin_role() -> str:
    return _get("SUBSCRIPTION_CONTEXT_ADMIN_ROLE", "BusinessContext.Admin")


def subscription_context_table_name() -> str:
    return _get("SUBSCRIPTION_CONTEXT_TABLE_NAME", "SubscriptionContextFields")


def subscription_context_blob_container_name() -> str:
    return _get("SUBSCRIPTION_CONTEXT_BLOB_CONTAINER", "subscription-context")


def subscription_context_refresh_lease_seconds() -> int:
    return min(max(_get_int("SUBSCRIPTION_CONTEXT_REFRESH_LEASE_SECONDS", 60), 15), 60)


def subscription_context_max_stale_minutes() -> int:
    return max(_get_int("SUBSCRIPTION_CONTEXT_MAX_STALE_MINUTES", 20), 1)


# ── Data Collection scheduler ─────────────────────────────
def data_collection_dispatch_schedule() -> str:
    """CRON for the single dispatcher timer; the only trigger needing a restart."""
    return _get("DATA_COLLECTION_DISPATCH_SCHEDULE", "0 */5 * * * *")


def data_collection_history_retention_days() -> int:
    return max(_get_int("DATA_COLLECTION_HISTORY_RETENTION_DAYS", 30), 1)


def data_collection_max_dispatch_per_tick() -> int:
    """Cap on pipelines started per tick to bound the cold-start burst; <=0 means unlimited."""
    return _get_int("DATA_COLLECTION_MAX_DISPATCH_PER_TICK", 0)


def data_collection_min_frequency_seconds() -> int:
    """Lower bound for an editable cadence; finer than the dispatch tick is unachievable."""
    return max(_get_int("DATA_COLLECTION_MIN_FREQUENCY_SECONDS", 300), 1)


def data_collection_max_frequency_seconds() -> int:
    """Upper bound for an editable cadence (default 30 days)."""
    return _get_int("DATA_COLLECTION_MAX_FREQUENCY_SECONDS", 2592000)


def scheduler_run_state_table_name() -> str:
    return _get("SCHEDULER_RUN_STATE_TABLE_NAME", "SchedulerRunState")


def scheduler_run_history_table_name() -> str:
    return _get("SCHEDULER_RUN_HISTORY_TABLE_NAME", "SchedulerRunHistory")


def scheduler_config_table_name() -> str:
    return _get("SCHEDULER_CONFIG_TABLE_NAME", "SchedulerConfig")


# ── Storage ───────────────────────────────────────────────
def blob_storage_endpoint() -> str:
    explicit = _get("BLOB_STORAGE_ENDPOINT") or _get("AzureWebJobsStorage__blobServiceUri")
    if explicit:
        return explicit.rstrip("/")
    account = _get("AzureWebJobsStorage__accountName")
    if not account:
        raise ValueError(
            "No blob endpoint configured. Set BLOB_STORAGE_ENDPOINT, "
            "AzureWebJobsStorage__blobServiceUri, or AzureWebJobsStorage__accountName."
        )
    suffix = _get("STORAGE_ENDPOINT_SUFFIX", "core.windows.net")
    return f"https://{account}.blob.{suffix}"


def table_storage_endpoint() -> str:
    """Table service URI for identity-based access when running in Azure.

    Prefers an explicit ``TABLE_STORAGE_ENDPOINT``, then the Functions
    identity-based convention ``AzureWebJobsStorage__tableServiceUri``, then
    builds ``https://<account>.table.<suffix>`` from
    ``AzureWebJobsStorage__accountName``.
    """
    explicit = _get("TABLE_STORAGE_ENDPOINT") or _get("AzureWebJobsStorage__tableServiceUri")
    if explicit:
        return explicit.rstrip("/")
    account = _get("AzureWebJobsStorage__accountName")
    if not account:
        raise ValueError(
            "No table endpoint configured. Set TABLE_STORAGE_ENDPOINT, "
            "AzureWebJobsStorage__tableServiceUri, or AzureWebJobsStorage__accountName."
        )
    suffix = _get("STORAGE_ENDPOINT_SUFFIX", "core.windows.net")
    return f"https://{account}.table.{suffix}"
