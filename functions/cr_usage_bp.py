import azure.durable_functions as df

from services import cr_usage
from ._durable import run_flush, run_refresh, with_run_recording

bp = df.Blueprint()

# Durable trigger names for this pipeline (global across the app), passed to the
# shared orchestration bodies so they call the right activities.
_NAMES = {
    "resolve": "cr_usage_resolve_plan",
    "delete_subscriptions": "cr_usage_delete_subscriptions",
    "fetch_page": "cr_usage_fetch_page",
    "write_state": "cr_usage_write_state",
    "flush_delete": "cr_usage_flush_delete",
    "flush_ensure": "cr_usage_flush_ensure",
}


# ── Orchestrators (started by the data-collection scheduler; recorded on exit) ─
@bp.orchestration_trigger(context_name="context")
def cr_usage_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "cr_usage", run_refresh(context, _NAMES)))


@bp.orchestration_trigger(context_name="context")
def cr_usage_flush_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "cr_usage", run_flush(context, _NAMES)))


# ── Activities (delegate to the CR pipeline object) ───────────────────────────
@bp.activity_trigger(input_name="request")
def cr_usage_resolve_plan(request: dict) -> dict:
    return cr_usage.pipeline.resolve_plan(request)


@bp.activity_trigger(input_name="pageInput")
def cr_usage_fetch_page(pageInput: dict) -> dict:
    return cr_usage.pipeline.fetch_and_store_page(pageInput)


@bp.activity_trigger(input_name="subIds")
def cr_usage_delete_subscriptions(subIds: list) -> dict:
    return cr_usage.pipeline.delete_subscriptions(subIds)


@bp.activity_trigger(input_name="newState")
def cr_usage_write_state(newState: dict) -> dict:
    return cr_usage.pipeline.write_state(newState)


@bp.activity_trigger(input_name="flushInput")
def cr_usage_flush_delete(flushInput: dict) -> dict:
    return cr_usage.pipeline.flush_delete_tables(flushInput)


@bp.activity_trigger(input_name="flushInput")
def cr_usage_flush_ensure(flushInput: dict) -> dict:
    return cr_usage.pipeline.flush_ensure_tables(flushInput)
