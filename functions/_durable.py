"""Shared durable-orchestration bodies for the daily-usage pipelines.

Durable function *names* are global to the app, so each pipeline registers its
own uniquely-named orchestrator/activity triggers (see the ``*_bp.py`` blueprints).
Those triggers are thin: they ``yield from`` the generators here, passing the
concrete activity names for their pipeline. The refresh page-loop / throttle
logic and the flush delete-recreate loop live here once.
"""
from __future__ import annotations

from datetime import timedelta

import azure.durable_functions as df

# A page that keeps throttling past this many consecutive waits is treated as a
# hard failure (bounds a pathological loop while tolerating long 429 back-offs).
MAX_PAGE_THROTTLES = 60

# After deleting a table, Azure Storage reserves the name (~40s) before it can be
# recreated. Wait it out with durable timers instead of blocking an activity.
FLUSH_RETRY_SECONDS = 10
FLUSH_MAX_ATTEMPTS = 30


def run_catalogue_refresh(
    context: df.DurableOrchestrationContext,
    activity_name: str,
    *,
    max_attempts: int = 5,
):
    """Run one catalogue refresh activity with bounded durable retry timers."""
    attempt = 0
    while True:
        result = yield context.call_activity(activity_name, {})
        if result.get("status") not in ("throttled", "transient"):
            break
        attempt += 1
        if attempt >= max_attempts:
            return {"status": "failed", "reason": "exceeded max transient retries"}
        delay = max(result.get("retryAfter", 5), min(5 * (2 ** (attempt - 1)), 120))
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=delay))

    if result.get("status") != "ok":
        return {"status": "failed", "reason": result.get("error") or "catalogue refresh failed"}
    return {**result, "status": "completed"}


def run_refresh(context: df.DurableOrchestrationContext, names: dict):
    """Resolve a plan, run its deletions + page loads, and advance state on success.

    ``names`` maps the logical steps to this pipeline's activity names:
    ``resolve``, ``delete_subscriptions``, ``fetch_page``, ``write_state``.
    """
    plan = yield context.call_activity(names["resolve"], {})
    if plan["action"] == "fail":
        return {"status": "failed", "reason": plan["reason"], "message": plan["message"]}

    deleted_rows = 0
    if plan.get("deletions"):
        d = yield context.call_activity(names["delete_subscriptions"], plan["deletions"])
        deleted_rows = d.get("deleted", 0)

    work_units = plan["workUnits"]
    streams = plan["streams"]

    # Serial by design: the Cost Management binding limit is a GLOBAL request
    # count, so concurrent scopes/pages just throttle each other. Each work unit
    # runs each stream's pages one at a time, waiting out 429s with a durable
    # timer and persisting every page as it lands.
    results = []
    for item in work_units:
        outcome = {"scope": item["scope"], "start": item["start"], "end": item["end"], "success": True, "upserted": 0}
        for stream in streams:
            next_link = None
            throttles = 0
            while True:
                page_input = {
                    "scope": item["scope"],
                    "start": item["start"],
                    "end": item["end"],
                    "filter": item["filter"],
                    "exportType": stream["exportType"],
                    "aggs": stream["aggs"],
                    "kind": stream["kind"],
                    "nextLink": next_link,
                }
                res = yield context.call_activity(names["fetch_page"], page_input)
                status = res.get("status")
                if status == "throttled":
                    throttles += 1
                    if throttles > MAX_PAGE_THROTTLES:
                        outcome["success"] = False
                        outcome["error"] = "exceeded max throttle retries"
                        break
                    due = context.current_utc_datetime + timedelta(seconds=res.get("retryAfter", 20))
                    yield context.create_timer(due)
                    continue
                if status == "error":
                    outcome["success"] = False
                    outcome["error"] = res.get("error")
                    break
                throttles = 0
                outcome["upserted"] += res.get("upserted", 0)
                next_link = res.get("nextLink")
                if not next_link:
                    break
            if not outcome["success"]:
                break
        results.append(outcome)

    failures = [r for r in results if not r.get("success")]
    state_written = False
    if not failures:
        # Advance coverage only when every work unit fully succeeded; the plan is
        # idempotent, so a failed run can simply be re-issued.
        yield context.call_activity(names["write_state"], plan["newState"])
        state_written = True

    if not failures:
        status = "completed"
    elif len(failures) == len(results):
        status = "failed"
    else:
        status = "partial"

    return {
        "status": status,
        "action": plan["reason"],
        "workUnits": len(work_units),
        "deletedRows": deleted_rows,
        "upserted": sum(r.get("upserted", 0) for r in results),
        "failed": len(failures),
        "stateWritten": state_written,
    }


def run_flush(context: df.DurableOrchestrationContext, names: dict):
    """Delete the usage tables, then wait out the reservation window and recreate.

    ``names`` maps to this pipeline's activity names: ``flush_delete``,
    ``flush_ensure``.
    """
    deleted = yield context.call_activity(names["flush_delete"], {})

    # Recreation 409s ("TableBeingDeleted") until Azure releases the name (~40s);
    # wait it out with durable timers. Azurite recreates immediately (0 waits).
    ready = False
    attempts = 0
    while attempts < FLUSH_MAX_ATTEMPTS:
        result = yield context.call_activity(names["flush_ensure"], {})
        if result.get("ready"):
            ready = True
            break
        attempts += 1
        due = context.current_utc_datetime + timedelta(seconds=FLUSH_RETRY_SECONDS)
        yield context.create_timer(due)

    return {
        "deletedTables": deleted.get("deleted", []),
        "recreated": ready,
        "waitAttempts": attempts,
    }


# ── Data-collection scheduler adapter + run recording ─────────────────────────
# The dispatcher service stays free of Durable SDK types; this adapter bridges the
# concrete client to its DurableClient protocol.
from services.data_collection.dispatcher import RunStatus  # noqa: E402


class DurableClientAdapter:
    """Adapt ``df.DurableOrchestrationClient`` to the dispatcher's ``DurableClient``."""

    def __init__(self, client: df.DurableOrchestrationClient) -> None:
        self._client = client

    async def start_new(self, orchestrator: str, client_input: dict | None = None) -> str:
        return await self._client.start_new(orchestrator, client_input=client_input or {})

    async def get_status(self, instance_id: str) -> RunStatus | None:
        status = await self._client.get_status(
            instance_id, show_history=False, show_history_output=False, show_input=False
        )
        if status is None or status.runtime_status is None:
            return None
        return RunStatus(
            runtime_status=status.runtime_status.value,
            custom_status=status.custom_status,
            output=status.output,
        )


def with_run_recording(context: df.DurableOrchestrationContext, pipeline_id: str, body):
    """Wrap an orchestrator body so it closes its run-history row on every exit.

    Calls the shared ``record_run_outcome`` activity on both the success and the
    caught-exception path; the dispatcher tick reconciler is the backstop for runs
    that die before reaching here.
    """
    try:
        result = yield from body
    except Exception as exc:
        yield context.call_activity(
            "record_run_outcome",
            {
                "pipeline": pipeline_id,
                "instanceId": context.instance_id,
                "outcome": "failed",
                "summary": {"error": str(exc)[:500]},
            },
        )
        raise
    outcome = result.get("status") if isinstance(result, dict) else None
    yield context.call_activity(
        "record_run_outcome",
        {
            "pipeline": pipeline_id,
            "instanceId": context.instance_id,
            "outcome": outcome or "completed",
            "summary": result if isinstance(result, dict) else None,
        },
    )
    return result
