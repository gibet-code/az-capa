"""Durable Functions orchestrator + activity for Location reference-data refresh."""
from __future__ import annotations

import azure.durable_functions as df

from services.reference_data import location_pipeline
from ._durable import run_catalogue_refresh, with_run_recording

bp = df.Blueprint()


@bp.orchestration_trigger(context_name="context")
def location_reference_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(
        context,
        "location_reference",
        run_catalogue_refresh(context, "location_reference_refresh_activity"),
    ))


@bp.activity_trigger(input_name="request")
def location_reference_refresh_activity(request: dict) -> dict:
    return location_pipeline.refresh(request)