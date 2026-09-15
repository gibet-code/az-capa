"""Durable Functions orchestrator + activity for Subscription reference-data refresh."""
from __future__ import annotations

import azure.durable_functions as df

from services.reference_data import subscription_pipeline
from ._durable import run_catalogue_refresh, with_run_recording

bp = df.Blueprint()


@bp.orchestration_trigger(context_name="context")
def subscription_reference_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(
        context,
        "subscription_reference",
        run_catalogue_refresh(context, "subscription_reference_refresh_activity"),
    ))


@bp.activity_trigger(input_name="request")
def subscription_reference_refresh_activity(request: dict) -> dict:
    return subscription_pipeline.refresh(request)