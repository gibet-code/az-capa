"""Durable Functions orchestrators + activities for compute SKU catalog refresh/flush."""
from __future__ import annotations

from datetime import timedelta

import azure.durable_functions as df

from services import compute_skus
from ._durable import MAX_PAGE_THROTTLES, run_flush, with_run_recording

bp = df.Blueprint()

_FLUSH_NAMES = {
    "flush_delete": "compute_skus_flush_delete",
    "flush_ensure": "compute_skus_flush_ensure",
}


@bp.orchestration_trigger(context_name="context")
def compute_skus_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "compute_skus", _compute_skus_refresh(context)))


def _compute_skus_refresh(context: df.DurableOrchestrationContext):
    plan = yield context.call_activity("compute_skus_resolve_plan", {})
    if plan["action"] == "fail":
        return {"status": "failed", "reason": plan["reason"], "message": plan["message"]}

    attempt = 0
    while True:
        result = yield context.call_activity("compute_skus_fetch_and_store", plan)
        if result.get("status") not in ("throttled", "transient"):
            break
        if attempt >= MAX_PAGE_THROTTLES:
            return {"status": "failed", "reason": "exceeded max transient retries"}
        wait = result.get("retryAfter", 5)
        if result["status"] == "transient":
            wait = max(wait, min(5 * (2 ** attempt), 120))
        attempt += 1
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=wait))

    if result.get("status") != "ok":
        return {"status": "failed", "reason": result.get("error") or "catalog retrieval failed"}
    return {**result, "status": "completed"}


@bp.orchestration_trigger(context_name="context")
def compute_skus_flush_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "compute_skus", run_flush(context, _FLUSH_NAMES)))


@bp.activity_trigger(input_name="request")
def compute_skus_resolve_plan(request: dict) -> dict:
    return compute_skus.pipeline.resolve_plan(request)


@bp.activity_trigger(input_name="request")
def compute_skus_fetch_and_store(request: dict) -> dict:
    return compute_skus.pipeline.fetch_and_store(request)


@bp.activity_trigger(input_name="flushInput")
def compute_skus_flush_delete(flushInput: dict) -> dict:
    return compute_skus.pipeline.flush_delete_storage(flushInput)


@bp.activity_trigger(input_name="flushInput")
def compute_skus_flush_ensure(flushInput: dict) -> dict:
    return compute_skus.pipeline.flush_ensure_storage(flushInput)