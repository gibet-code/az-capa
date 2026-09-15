"""Durable Functions orchestrators + activities for zone-mapping refresh and flush."""
from __future__ import annotations

from datetime import timedelta

import azure.durable_functions as df

from services import zone_mapping
from ._durable import MAX_PAGE_THROTTLES, run_flush, with_run_recording

bp = df.Blueprint()

_FLUSH_NAMES = {
    "flush_delete": "zone_mapping_flush_delete",
    "flush_ensure": "zone_mapping_flush_ensure",
}


def _progress(completed: int, total: int) -> dict:
    percent = 100 if total == 0 else round(completed * 100 / total)
    return {
        "completedSubscriptions": completed,
        "totalSubscriptions": total,
        "percentCompleted": percent,
    }


@bp.orchestration_trigger(context_name="context")
def zone_mapping_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "zone_mapping", _zone_mapping_refresh(context)))


def _zone_mapping_refresh(context: df.DurableOrchestrationContext):
    plan = yield context.call_activity("zone_mapping_resolve_plan", {})
    if plan["action"] == "fail":
        return {"status": "failed", "reason": plan["reason"], "message": plan["message"]}

    deleted_rows = 0
    if plan.get("deletions"):
        result = yield context.call_activity("zone_mapping_delete_subscriptions", plan["deletions"])
        deleted_rows = result.get("deleted", 0)

    work_units = plan["workUnits"]
    total = len(work_units)
    completed = 0
    context.set_custom_status(_progress(completed, total))
    concurrency = plan["maxConcurrency"]

    # Fan out per-subscription activities directly (no sub-orchestrators) and
    # re-fan-out throttled/transient subscriptions after a cohort backoff timer.
    outcomes_by_sub: dict[str, dict] = {}
    pending = list(work_units)
    attempt = 0
    while pending:
        retry = []
        max_wait = 0
        for start in range(0, len(pending), concurrency):
            batch = pending[start:start + concurrency]
            tasks = [
                context.call_activity("zone_mapping_fetch_subscription", unit)
                for unit in batch
            ]
            batch_outcomes = yield context.task_all(tasks)
            for unit, outcome in zip(batch, batch_outcomes):
                status = outcome.get("status")
                if status in ("throttled", "transient") and attempt < MAX_PAGE_THROTTLES:
                    retry.append(unit)
                    wait = outcome.get("retryAfter", 5)
                    if status == "transient":
                        wait = max(wait, min(5 * (2 ** attempt), 120))
                    max_wait = max(max_wait, wait)
                    continue
                if status in ("throttled", "transient"):
                    outcome = {
                        "subscriptionId": unit["subscriptionId"],
                        "status": "error",
                        "error": "exceeded max transient retries",
                    }
                outcomes_by_sub[unit["subscriptionId"]] = outcome
                completed += 1
                context.set_custom_status(_progress(completed, total))
        if not retry:
            break
        attempt += 1
        due = context.current_utc_datetime + timedelta(seconds=max_wait or 5)
        yield context.create_timer(due)
        pending = retry

    outcomes = list(outcomes_by_sub.values())
    failures = [outcome for outcome in outcomes if outcome.get("status") != "ok"]
    state_written = False
    if not failures:
        yield context.call_activity("zone_mapping_write_state", plan["newState"])
        state_written = True

    return {
        "status": "completed" if not failures else "partial",
        "action": plan["reason"],
        "fullRefresh": plan.get("fullRefresh", False),
        "newRegions": plan.get("newRegions", []),
        "subscriptions": total,
        "deletedRows": deleted_rows,
        "regions": sum(outcome.get("regions", 0) for outcome in outcomes),
        "upserted": sum(outcome.get("upserted", 0) for outcome in outcomes),
        "failed": len(failures),
        "stateWritten": state_written,
    }


@bp.orchestration_trigger(context_name="context")
def zone_mapping_flush_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "zone_mapping", run_flush(context, _FLUSH_NAMES)))


@bp.activity_trigger(input_name="request")
def zone_mapping_resolve_plan(request: dict) -> dict:
    return zone_mapping.pipeline.resolve_plan(request)


@bp.activity_trigger(input_name="request")
def zone_mapping_fetch_subscription(request: dict) -> dict:
    return zone_mapping.pipeline.fetch_and_store(request)


@bp.activity_trigger(input_name="subIds")
def zone_mapping_delete_subscriptions(subIds: list) -> dict:
    return zone_mapping.pipeline.delete_subscriptions(subIds)


@bp.activity_trigger(input_name="newState")
def zone_mapping_write_state(newState: dict) -> dict:
    return zone_mapping.pipeline.write_state(newState)


@bp.activity_trigger(input_name="flushInput")
def zone_mapping_flush_delete(flushInput: dict) -> dict:
    return zone_mapping.pipeline.flush_delete_data(flushInput)


@bp.activity_trigger(input_name="flushInput")
def zone_mapping_flush_ensure(flushInput: dict) -> dict:
    return zone_mapping.pipeline.flush_ensure_storage(flushInput)
