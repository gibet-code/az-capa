"""Durable Functions orchestrators + activities for Activity Log refresh and flush."""
from __future__ import annotations

from datetime import timedelta

import azure.durable_functions as df

from services import activity_log
from ._durable import MAX_PAGE_THROTTLES, run_flush, with_run_recording

bp = df.Blueprint()

_FLUSH_NAMES = {
    "flush_delete": "activity_log_flush_delete",
    "flush_ensure": "activity_log_flush_ensure",
}


def _progress(completed: int, total: int) -> dict:
    percent = 100 if total == 0 else round(completed * 100 / total)
    return {
        "completedSubscriptions": completed,
        "totalSubscriptions": total,
        "percentCompleted": percent,
    }


@bp.orchestration_trigger(context_name="context")
def activity_log_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "activity_log", _activity_log_refresh(context)))


def _activity_log_refresh(context: df.DurableOrchestrationContext):
    plan = yield context.call_activity("activity_log_resolve_plan", {})
    if plan["action"] == "noop":
        return {"status": "completed", "action": plan["reason"], "subscriptions": 0}

    deleted_rows = 0
    if plan.get("deletions"):
        result = yield context.call_activity("activity_log_delete_subscriptions", plan["deletions"])
        deleted_rows = result.get("deleted", 0)

    work_units = plan["workUnits"]
    total = len(work_units)
    completed = 0
    context.set_custom_status(_progress(completed, total))
    outcomes = []
    concurrency = plan["maxConcurrency"]

    for start in range(0, total, concurrency):
        batch = work_units[start:start + concurrency]
        tasks = [
            context.call_sub_orchestrator(
                "activity_log_subscription_orchestrator",
                {
                    **unit,
                    "operations": plan["operations"],
                },
            )
            for unit in batch
        ]
        batch_outcomes = yield context.task_all(tasks)
        outcomes.extend(batch_outcomes)
        completed += len(batch)
        context.set_custom_status(_progress(completed, total))

    failures = [outcome for outcome in outcomes if not outcome.get("success")]

    # Retained-data reconciliation (Section 10.2/10.3): re-project stale/pending
    # operations from stored payloads. Runs for every planned subscription
    # regardless of this run's fetch outcome; bounded per subscription per run.
    reconciled = 0
    reconcile_failures = []
    subscription_ids = [unit["subscriptionId"] for unit in work_units]
    for start in range(0, len(subscription_ids), concurrency):
        batch = subscription_ids[start:start + concurrency]
        tasks = [
            context.call_activity(
                "activity_log_reconcile",
                {"subscriptionId": subscription_id, "maxUnits": plan["replayMaxUnits"]},
            )
            for subscription_id in batch
        ]
        for result in (yield context.task_all(tasks)):
            if result.get("status") == "error":
                reconcile_failures.append(result)
            else:
                reconciled += result.get("reconciled", 0)

    committed = sum(1 for outcome in outcomes if outcome.get("committed"))
    return {
        "status": "completed" if not failures and not reconcile_failures else "partial",
        "action": plan["reason"],
        "subscriptions": total,
        "committedSubscriptions": committed,
        "deletedRows": deleted_rows,
        "received": sum(outcome.get("received", 0) for outcome in outcomes),
        "upserted": sum(outcome.get("upserted", 0) for outcome in outcomes),
        "reconciled": reconciled,
        "failed": len(failures),
        "reconcileFailed": len(reconcile_failures),
    }


@bp.orchestration_trigger(context_name="context")
def activity_log_subscription_orchestrator(context: df.DurableOrchestrationContext):
    request = context.get_input()
    next_link = None
    transient_attempts = 0
    received = 0
    upserted = 0
    pages = 0

    while True:
        page_request = {**request, "nextLink": next_link}
        result = yield context.call_activity("activity_log_fetch_page", page_request)
        status = result.get("status")
        if status in ("throttled", "transient"):
            transient_attempts += 1
            if transient_attempts > MAX_PAGE_THROTTLES:
                return {
                    "subscriptionId": request["subscriptionId"],
                    "success": False,
                    "error": "exceeded max transient retries",
                    "received": received,
                    "upserted": upserted,
                }
            wait = result.get("retryAfter", 5)
            if status == "transient":
                wait = max(wait, min(5 * (2 ** (transient_attempts - 1)), 120))
            due = context.current_utc_datetime + timedelta(seconds=wait)
            yield context.create_timer(due)
            continue
        if status == "error":
            return {
                "subscriptionId": request["subscriptionId"],
                "success": False,
                "error": result.get("error"),
                "received": received,
                "upserted": upserted,
            }

        transient_attempts = 0
        pages += 1
        received += result.get("received", 0)
        upserted += result.get("upserted", 0)
        next_link = result.get("nextLink")
        if not next_link:
            committed = False
            coverage = request.get("coverage")
            if coverage:
                # Advance this subscription's coverage only after all its pages land.
                yield context.call_activity("activity_log_write_state", [coverage])
                committed = True
            return {
                "subscriptionId": request["subscriptionId"],
                "success": True,
                "committed": committed,
                "pages": pages,
                "received": received,
                "upserted": upserted,
            }


@bp.orchestration_trigger(context_name="context")
def activity_log_flush_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "activity_log", run_flush(context, _FLUSH_NAMES)))


@bp.activity_trigger(input_name="request")
def activity_log_resolve_plan(request: dict) -> dict:
    return activity_log.pipeline.resolve_plan(request)


@bp.activity_trigger(input_name="pageInput")
def activity_log_fetch_page(pageInput: dict) -> dict:
    return activity_log.pipeline.fetch_and_store_page(pageInput)


@bp.activity_trigger(input_name="subIds")
def activity_log_delete_subscriptions(subIds: list) -> dict:
    return activity_log.pipeline.delete_subscriptions(subIds)


@bp.activity_trigger(input_name="reconcileInput")
def activity_log_reconcile(reconcileInput: dict) -> dict:
    return activity_log.pipeline.reconcile_subscription(reconcileInput)


@bp.activity_trigger(input_name="newState")
def activity_log_write_state(newState: dict) -> dict:
    return activity_log.pipeline.write_state(newState)


@bp.activity_trigger(input_name="flushInput")
def activity_log_flush_delete(flushInput: dict) -> dict:
    return activity_log.pipeline.flush_delete_tables(flushInput)


@bp.activity_trigger(input_name="flushInput")
def activity_log_flush_ensure(flushInput: dict) -> dict:
    return activity_log.pipeline.flush_ensure_tables(flushInput)