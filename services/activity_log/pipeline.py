"""Recipe-driven Activity Log collection: plan, page fetch, assemble, persist.

This is the a1 collection service (spec Sections 3, 6, 8). It resolves a
per-subscription plan from the enabled recipes and each subscription's retained
coverage, then, per Azure page, projects each matching event, assembles source
operations, and upserts them. Assembly is deterministic and idempotent, so
overlapping re-queries and version-drift replays converge on the same rows.

The service owns behavior only: it reads the pure recipe/projection/assembly
modules and delegates all persistence to :class:`ActivityLogStore`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from clients.activity_log import fetch_page
from core import settings
from core.app_scope import MODE_MANAGEMENT_GROUPS, MODE_SUBSCRIPTIONS, configured_scope
from core.identity import for_backend_operation
from services.app_scope import resolve_app_scope
from storage.activity_log_store import ActivityLogStore

from .assembly import PreparedEvent, assemble
from .projection import (
    ERROR_COLUMNS,
    PAYLOAD_ENCODING,
    compress_payload,
    decompress_payload,
    project,
)
from .recipes import all_recipes, matching_recipes, server_operations

# Bumped when assembly's folding logic changes (drives Section 10.3 replay).
ASSEMBLER_VERSION = 2

_RETENTION_DAYS = 89


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_query_end(value: datetime, target_end: datetime) -> str:
    if value == target_end:
        value -= timedelta(milliseconds=1)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return _format_timestamp(value)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _complete_day_end(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _merge_windows(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(windows):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def enabled_recipes():
    return all_recipes()


def _recipe_versions(recipes) -> dict[str, int]:
    return {recipe.id: recipe.version for recipe in recipes}


def _recipe_version_for(resource_type: str | None, operation_name: str | None) -> int | None:
    matched = matching_recipes(resource_type, operation_name, None)
    if not matched:
        return None
    return max(recipe.version for recipe in matched)


def _prepare_event(event: dict) -> PreparedEvent | None:
    """Project one normalized event against every matching recipe.

    Returns ``None`` when the event matches no recipe (it is retained nowhere).
    Projection is best-effort. Applied configuration events can retain their
    whole payload for replay; failed VM events retain only sanitized details.
    """
    resource_type = event.get("resourceType")
    operation_name = event.get("operationName")
    status = event.get("status")
    matched = matching_recipes(resource_type, operation_name, status)
    if not matched:
        return None

    columns: dict[str, object] = {}
    error_details_gz = None
    pending = False
    for recipe in matched:
        result = project(event, recipe)
        columns.update(result.columns)
        if result.error_details_gz is not None:
            error_details_gz = result.error_details_gz
        pending = pending or result.reprojection_pending

    retain_payload = any(
        recipe.retain_payload
        and (recipe.error_projection is None or (status or "").lower() == "accepted")
        for recipe in matched
    )
    return PreparedEvent(
        event_data_id=event.get("eventDataId"),
        event_timestamp=event.get("eventTimestamp"),
        subscription_id=event.get("subscriptionId"),
        resource_id=event.get("resourceId"),
        operation_name=operation_name,
        status=status,
        resource_type=resource_type,
        submission_timestamp=event.get("submissionTimestamp"),
        operation_id=event.get("operationId"),
        correlation_id=event.get("correlationId"),
        columns=columns,
        error_details_gz=error_details_gz,
        payload_gz=compress_payload(event) if retain_payload else None,
        payload_encoding=PAYLOAD_ENCODING if retain_payload else None,
        reprojection_pending=pending,
    )


def assemble_operations(events: list[dict]) -> list[dict]:
    """Project, assemble, and version-stamp one page's events."""
    prepared = [prepared for prepared in map(_prepare_event, events) if prepared is not None]
    operations = assemble(prepared)
    for operation in operations:
        operation["AssemblerVersion"] = ASSEMBLER_VERSION
        operation["RecipeVersion"] = _recipe_version_for(
            operation.get("ResourceType"), operation.get("OperationName")
        )
    return operations


def _age_exceeds(timestamp: str | None, now: datetime, horizon: timedelta) -> bool:
    if not timestamp:
        return False
    try:
        return now - _parse_timestamp(timestamp) > horizon
    except ValueError:
        return False


def _row_needs_reconcile(row: dict, now: datetime, horizon: timedelta) -> bool:
    """Cheap row-level pre-filter run before loading any retained payload."""
    if (row.get("Outcome") or "").lower() == "pending" and _age_exceeds(row.get("LastEventTime"), now, horizon):
        return True
    has_payload = bool(row.get("AppliedPayloadGz") or row.get("AppliedPayloadSpillBlob"))
    if not has_payload:
        return False
    expected = _recipe_version_for(row.get("ResourceType"), row.get("OperationName"))
    return (
        bool(row.get("ReprojectionPending"))
        or row.get("AssemblerVersion") != ASSEMBLER_VERSION
        or row.get("RecipeVersion") != expected
    )


def _reconcile_operation(operation: dict, now: datetime, horizon: timedelta) -> bool:
    """Re-project the retained payload and age a stale pending operation."""
    changed = False
    expected_version = _recipe_version_for(operation.get("ResourceType"), operation.get("OperationName"))
    stale = (
        operation.get("ReprojectionPending")
        or operation.get("AssemblerVersion") != ASSEMBLER_VERSION
        or operation.get("RecipeVersion") != expected_version
    )
    payload = operation.get("AppliedPayloadGz")
    if stale and payload:
        event = decompress_payload(payload)
        columns = {
            key: value
            for key, value in (operation.get("Columns") or {}).items()
            if key in ERROR_COLUMNS
        }
        pending = False
        for recipe in matching_recipes(
            operation.get("ResourceType"), operation.get("OperationName"), event.get("status")
        ):
            result = project(event, recipe)
            columns.update(result.columns)
            pending = pending or result.reprojection_pending
        operation["Columns"] = columns
        operation["ReprojectionPending"] = pending
        operation["AssemblerVersion"] = ASSEMBLER_VERSION
        operation["RecipeVersion"] = expected_version
        changed = True

    if (operation.get("Outcome") or "").lower() == "pending" and _age_exceeds(
        operation.get("LastEventTime"), now, horizon
    ):
        operation["Outcome"] = "unknown"
        changed = True
    return changed


class ActivityLogPipeline:
    def __init__(self, store: ActivityLogStore) -> None:
        self._store = store

    def _scope(self, identity) -> dict:
        configured = configured_scope()
        subscriptions = resolve_app_scope(
            identity, configured, allow_live_fallback=True
        ).subscription_ids
        return {
            "subs": list(subscriptions),
            "configSelectors": {
                "subscriptions": list(configured.selectors) if configured.mode == MODE_SUBSCRIPTIONS else [],
                "managementGroups": list(configured.selectors) if configured.mode == MODE_MANAGEMENT_GROUPS else [],
            },
        }

    @staticmethod
    def _describe(scope: dict) -> str:
        selectors = scope.get("configSelectors") or {}
        if selectors.get("managementGroups"):
            source = f"management group(s) {selectors['managementGroups']}"
        elif selectors.get("subscriptions"):
            source = f"{len(scope['subs'])} configured subscription(s)"
        else:
            source = "subscriptions visible to the calling identity"
        return f"scope={source} ({len(scope['subs'])} subscriptions), recipes={len(enabled_recipes())}"

    def resolve_plan(self, _request: dict) -> dict:
        scope = self._scope(for_backend_operation())
        recipes = enabled_recipes()
        operations = list(server_operations(recipes))
        recipe_ids = sorted(recipe.id for recipe in recipes)
        versions = _recipe_versions(recipes)

        target_end = _complete_day_end(_utc_now())
        retention_start = target_end - timedelta(days=_RETENTION_DAYS)
        historical_start = max(
            target_end - timedelta(days=settings.activity_log_historical_days()),
            retention_start,
        )
        overlap = timedelta(hours=settings.activity_log_refresh_overlap_hours())

        coverage_by_sub = {cov["subscriptionId"]: cov for cov in self._store.list_coverage()}

        work_units = []
        added = 0
        reconciled = 0
        historical_backfills = 0
        incremental = 0
        for subscription_id in sorted(scope["subs"]):
            cov = coverage_by_sub.get(subscription_id)
            drift = cov is not None and (
                cov.get("materializedRecipeVersions") != versions
                or sorted(cov.get("enabledRecipeIds") or []) != recipe_ids
            )
            windows: list[tuple[datetime, datetime]] = []
            if cov is None:
                windows.append((historical_start, target_end))
                coverage_start = historical_start
                coverage_end = target_end
                added += 1
            elif drift:
                windows.append((historical_start, target_end))
                coverage_start = min(
                    _parse_timestamp(cov["coverageStart"])
                    if cov.get("coverageStart") else historical_start,
                    historical_start,
                )
                coverage_end = max(
                    _parse_timestamp(cov["coverageEnd"])
                    if cov.get("coverageEnd") else target_end,
                    target_end,
                )
                reconciled += 1
            else:
                prior_start = (
                    _parse_timestamp(cov["coverageStart"])
                    if cov.get("coverageStart") else target_end
                )
                prior_end = (
                    _parse_timestamp(cov["coverageEnd"])
                    if cov.get("coverageEnd") else historical_start
                )
                if prior_start > historical_start:
                    windows.append((historical_start, min(prior_start, target_end)))
                    historical_backfills += 1
                if prior_end < target_end:
                    windows.append((
                        max(prior_end - overlap, historical_start),
                        target_end,
                    ))
                    incremental += 1
                coverage_start = min(prior_start, historical_start)
                coverage_end = max(prior_end, target_end)

            windows = _merge_windows(windows)
            if not windows:
                continue

            work_units.append({
                "subscriptionId": subscription_id,
                "windows": [
                    {
                        "start": _format_timestamp(start),
                        "end": _format_query_end(end, target_end),
                    }
                    for start, end in windows
                ],
                "coverage": {
                    "subscriptionId": subscription_id,
                    "coverageStart": _format_timestamp(coverage_start),
                    "coverageEnd": _format_timestamp(coverage_end),
                    "enabledRecipeIds": recipe_ids,
                    "materializedRecipeVersions": versions,
                },
            })

        if reconciled:
            reason = "reconcile"
        elif added and added == len(work_units):
            reason = "initial-full"
        elif historical_backfills:
            reason = "historical-backfill"
        elif incremental or added:
            reason = "incremental"
        else:
            reason = "up-to-date"
        return {
            "action": "load" if work_units else "noop",
            "reason": reason,
            "targetStart": _format_timestamp(historical_start),
            "targetEnd": _format_timestamp(target_end),
            "deletions": [],  # dropping a subscription stops collection; purge is explicit (Section 6)
            "workUnits": work_units,
            "operations": operations,
            "maxConcurrency": settings.activity_log_max_concurrency(),
            "replayMaxUnits": settings.activity_log_replay_max_units_per_run(),
        }

    def fetch_and_store_page(self, request: dict) -> dict:
        try:
            token = for_backend_operation().get_token()
            result = fetch_page(request, token)
            if result["status"] != "ok":
                return result
            operations = assemble_operations(result["events"])
            upserted = self._store.upsert_operations(operations)
            return {
                "status": "ok",
                "received": result["received"],
                "operations": len(operations),
                "upserted": upserted,
                "nextLink": result["nextLink"],
            }
        except Exception as exc:  # noqa: BLE001 — returned to Durable as a failed subscription
            logging.exception(
                "Activity Log page fetch or persistence failed for subscription %s",
                request.get("subscriptionId"),
            )
            return {"status": "error", "error": str(exc)}

    def delete_subscriptions(self, subscription_ids: list) -> dict:
        return {"deleted": self._store.delete_subscriptions_data(subscription_ids)}

    def reconcile_subscription(self, request: dict) -> dict:
        """Replay retained operations for one subscription (spec Section 10.2/10.3).

        Bounded and retained-data-only (no Azure fetch): re-projects rows whose
        projection is pending or whose assembler/recipe version is stale, and ages
        long-pending operations to ``unknown``. Idempotent — a clean subscription
        yields no writes.
        """
        subscription_id = request.get("subscriptionId")
        max_units = int(request.get("maxUnits", settings.activity_log_replay_max_units_per_run()))
        if not subscription_id or max_units <= 0:
            return {"subscriptionId": subscription_id, "status": "ok", "reconciled": 0}

        try:
            now = _utc_now()
            horizon = timedelta(hours=settings.activity_log_pending_unknown_hours())
            rows = self._store.query_operations(subscription_id)
            targets = [row for row in rows if _row_needs_reconcile(row, now, horizon)][:max_units]

            updates = []
            for row in targets:
                operation = self._store.hydrate_operation(row)
                if _reconcile_operation(operation, now, horizon):
                    updates.append(operation)
            upserted = self._store.upsert_operations(updates) if updates else 0
            return {"subscriptionId": subscription_id, "status": "ok", "reconciled": upserted}
        except Exception as exc:  # noqa: BLE001 - isolate one subscription's reconciliation
            logging.exception(
                "Activity Log reconciliation failed for subscription %s",
                subscription_id,
            )
            return {
                "subscriptionId": subscription_id,
                "status": "error",
                "error": str(exc),
                "reconciled": 0,
            }

    def write_state(self, state) -> dict:
        coverages = [state] if isinstance(state, dict) else list(state or [])
        written = 0
        now = _utc_now()
        for coverage in coverages:
            subscription_id = coverage.get("subscriptionId")
            if not subscription_id:
                continue
            self._store.set_coverage(subscription_id, {**coverage, "lastUpsertAt": now})
            written += 1
        return {"ok": True, "written": written}

    def status(self, _request: dict) -> dict:
        scope = self._scope(for_backend_operation())
        coverage_by_sub = {
            cov["subscriptionId"]: cov
            for cov in self._store.list_coverage()
            if cov.get("subscriptionId") in scope["subs"]
        }
        coverage = list(coverage_by_sub.values())
        target_end = _complete_day_end(_utc_now())
        target_start = max(
            target_end - timedelta(days=settings.activity_log_historical_days()),
            target_end - timedelta(days=_RETENTION_DAYS),
        )
        coverage_starts = [cov["coverageStart"] for cov in coverage if cov.get("coverageStart")]
        coverage_ends = [cov["coverageEnd"] for cov in coverage if cov.get("coverageEnd")]
        coverage_start = min(coverage_starts) if coverage_starts else None
        coverage_end = min(coverage_ends) if coverage_ends else None  # worst-covered subscription
        staleness = (
            None if not coverage_end
            else max(0, int((target_end - _parse_timestamp(coverage_end)).total_seconds() // 60))
        )
        overlap_minutes = settings.activity_log_refresh_overlap_hours() * 60
        versions = _recipe_versions(enabled_recipes())
        recipe_ids = sorted(versions)
        subscriptions_at_target = sum(
            1
            for subscription_id in scope["subs"]
            if (
                (cov := coverage_by_sub.get(subscription_id)) is not None
                and cov.get("coverageStart")
                and _parse_timestamp(cov["coverageStart"]) <= target_start
                and cov.get("coverageEnd")
                and _parse_timestamp(cov["coverageEnd"]) >= target_end
                and cov.get("materializedRecipeVersions") == versions
                and sorted(cov.get("enabledRecipeIds") or []) == recipe_ids
            )
        )
        return {
            "hasData": bool(coverage),
            "isRefreshEnabled": True,
            "warning": None,
            "finalizedThrough": _format_timestamp(target_end),
            "targetStart": _format_timestamp(target_start),
            "targetEnd": _format_timestamp(target_end),
            "coverageStart": coverage_start,
            "coverageEnd": coverage_end,
            "stalenessDays": None,
            "stalenessMinutes": staleness,
            "isStale": None if staleness is None else staleness > overlap_minutes,
            "subscriptionsCovered": len(coverage),
            "subscriptionsAtTarget": subscriptions_at_target,
            "subscriptionsDue": len(scope["subs"]) - subscriptions_at_target,
            "currentScope": self._describe(scope),
            "lastLoadedScope": None,
        }

    def flush_delete_tables(self, _input: dict) -> dict:
        return {"deleted": self._store.delete_tables()}

    def flush_ensure_tables(self, _input: dict) -> dict:
        return {"ready": self._store.ensure_tables()}


_store = ActivityLogStore()
pipeline = ActivityLogPipeline(_store)
