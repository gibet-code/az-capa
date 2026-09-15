import azure.durable_functions as df

from services import vm_usage
from ._durable import run_flush, run_refresh, with_run_recording

bp = df.Blueprint()

# Durable trigger names for this pipeline (global across the app), passed to the
# shared orchestration bodies so they call the right activities.
_NAMES = {
    "resolve": "vm_usage_resolve_plan",
    "delete_subscriptions": "vm_usage_delete_subscriptions",
    "fetch_page": "vm_usage_fetch_page",
    "write_state": "vm_usage_write_state",
    "flush_delete": "vm_usage_flush_delete",
    "flush_ensure": "vm_usage_flush_ensure",
}


# ── Orchestrators (started by the data-collection scheduler; recorded on exit) ─
@bp.orchestration_trigger(context_name="context")
def vm_usage_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "vm_usage", run_refresh(context, _NAMES)))


@bp.orchestration_trigger(context_name="context")
def vm_usage_flush_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "vm_usage", run_flush(context, _NAMES)))


# ── Activities (delegate to the VM pipeline object) ───────────────────────────
@bp.activity_trigger(input_name="request")
def vm_usage_resolve_plan(request: dict) -> dict:
    return vm_usage.pipeline.resolve_plan(request)


@bp.activity_trigger(input_name="pageInput")
def vm_usage_fetch_page(pageInput: dict) -> dict:
    return vm_usage.pipeline.fetch_and_store_page(pageInput)


@bp.activity_trigger(input_name="subIds")
def vm_usage_delete_subscriptions(subIds: list) -> dict:
    return vm_usage.pipeline.delete_subscriptions(subIds)


@bp.activity_trigger(input_name="newState")
def vm_usage_write_state(newState: dict) -> dict:
    return vm_usage.pipeline.write_state(newState)


@bp.activity_trigger(input_name="flushInput")
def vm_usage_flush_delete(flushInput: dict) -> dict:
    return vm_usage.pipeline.flush_delete_tables(flushInput)


@bp.activity_trigger(input_name="flushInput")
def vm_usage_flush_ensure(flushInput: dict) -> dict:
    return vm_usage.pipeline.flush_ensure_tables(flushInput)
