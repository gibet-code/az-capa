"""Data-collection scheduler facade: one dispatch timer + one route family.

Replaces every per-pipeline timer and the per-pipeline ``refresh``/``flush``/
``status`` routes. Admin gating and registry validation happen here once; all
starts flow through the single ``DataCollectionDispatcher.start`` service method.
"""
from __future__ import annotations

import json
import logging

import azure.durable_functions as df
import azure.functions as func

from services.data_collection import registry
from services.data_collection.dispatcher import ConfigUpdateError, DataCollectionDispatcher

from . import _authorization as authorization
from ._durable import DurableClientAdapter

bp = df.Blueprint()

_dispatcher = DataCollectionDispatcher()

# ConfigUpdateError.code → HTTP status for the config edit route.
_CONFIG_ERROR_STATUS = {
    "unknown_pipeline": 404,
    "cannot_disable": 409,
    "stale_revision": 409,
    "invalid_frequency": 400,
    "invalid_anchor": 400,
}


def get_dispatcher() -> DataCollectionDispatcher:
    return _dispatcher


def _json(body: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body, default=str), status_code=status_code, mimetype="application/json"
    )


def _not_found(pipeline_id: str) -> func.HttpResponse:
    return _json({"error": "unknown_pipeline", "message": f"Unknown pipeline {pipeline_id!r}."}, 404)


def _status_body(pipeline_id: str) -> dict:
    """Return the pipeline's own freshness ``status()`` body (lazy-imported)."""
    if pipeline_id == "vm_usage":
        from services import vm_usage

        return vm_usage.pipeline.status({})
    if pipeline_id == "cr_usage":
        from services import cr_usage

        return cr_usage.pipeline.status({})
    if pipeline_id == "activity_log":
        from services import activity_log

        return activity_log.pipeline.status({})
    if pipeline_id == "compute_skus":
        from services import compute_skus

        return compute_skus.pipeline.status({})
    if pipeline_id == "zone_mapping":
        from services import zone_mapping

        return zone_mapping.pipeline.status({})
    if pipeline_id == "subscription_reference":
        from services.reference_data import subscription_pipeline

        return subscription_pipeline.status()
    if pipeline_id == "location_reference":
        from services.reference_data import location_pipeline

        return location_pipeline.status()
    if pipeline_id == "subscription_context":
        from services.subscription_context import service as context_service

        return context_service.status()
    raise KeyError(pipeline_id)


# ── Dispatch timer (the single scheduler for every pipeline) ──────────────────
@bp.timer_trigger(
    schedule="%DATA_COLLECTION_DISPATCH_SCHEDULE%", arg_name="timer", run_on_startup=True
)
@bp.durable_client_input(client_name="client")
async def data_collection_dispatch(
    timer: func.TimerRequest, client: df.DurableOrchestrationClient
) -> None:
    await get_dispatcher().tick(DurableClientAdapter(client))


# ── Facade routes ─────────────────────────────────────────────────────────────
@bp.route(route="api/data-collection", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def data_collection_list(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    pipelines = await get_dispatcher().list_pipelines(DurableClientAdapter(client))
    return _json({"pipelines": pipelines})


@bp.route(route="api/data-collection/{pipeline}/status", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def data_collection_status(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    pipeline_id = req.route_params.get("pipeline")
    if registry.try_get(pipeline_id) is None:
        return _not_found(pipeline_id)

    detail = _status_body(pipeline_id)
    envelope = await get_dispatcher().pipeline_status(
        DurableClientAdapter(client), pipeline_id, detail
    )
    # Spread the pipeline body at top level too, so the details drawer's status
    # panel (which reads flat freshness fields) works alongside the §2.2 envelope.
    return _json({**detail, **envelope})


@bp.route(route="api/data-collection/{pipeline}/run-history", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def data_collection_run_history(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    pipeline_id = req.route_params.get("pipeline")
    if registry.try_get(pipeline_id) is None:
        return _not_found(pipeline_id)
    try:
        limit = int(req.params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    after = req.params.get("after") or None
    result = await get_dispatcher().run_history(
        DurableClientAdapter(client), pipeline_id, limit=limit, after=after
    )
    return _json(result)


@bp.route(route="api/data-collection/{pipeline}/{action}", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def data_collection_action(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    pipeline_id = req.route_params.get("pipeline")
    action = req.route_params.get("action")
    descriptor = registry.try_get(pipeline_id)
    if descriptor is None:
        return _not_found(pipeline_id)
    if action not in ("refresh", "flush"):
        return _json({"error": "unknown_action", "message": f"Unknown action {action!r}."}, 404)
    if action == "flush" and not descriptor.supports_flush:
        return _json(
            {"error": "flush_unsupported", "message": f"{descriptor.label} does not support flush."},
            405,
        )

    result = await get_dispatcher().start(
        DurableClientAdapter(client), pipeline_id, action, "manual"
    )
    if result.status == "busy":
        return _json(
            {
                "error": "operation_in_progress",
                "message": f"A {descriptor.label} operation is already in progress.",
            },
            409,
        )
    logging.info("Started %s %s orchestration %s", descriptor.label, action, result.instance_id)
    return _json({"status": "started", "instanceId": result.instance_id}, 202)


@bp.route(route="api/data-collection/{pipeline}/config", methods=["PATCH"])
async def data_collection_config(req: func.HttpRequest) -> func.HttpResponse:
    try:
        principal = authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    pipeline_id = req.route_params.get("pipeline")
    if registry.try_get(pipeline_id) is None:
        return _not_found(pipeline_id)
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "invalid_body", "message": "A JSON body is required."}, 400)
    try:
        enabled = bool(body["enabled"])
        frequency_seconds = int(body["frequencySeconds"])
        anchor_seconds = int(body["anchorSeconds"])
    except (KeyError, TypeError, ValueError):
        return _json(
            {
                "error": "invalid_body",
                "message": "enabled, frequencySeconds and anchorSeconds are required.",
            },
            400,
        )
    expected_revision = body.get("expectedRevision")
    updated_by = {
        "objectId": principal.object_id,
        "name": principal.name,
        "tenantId": principal.tenant_id,
    }
    try:
        row = get_dispatcher().update_config(
            pipeline_id,
            enabled=enabled,
            frequency_seconds=frequency_seconds,
            anchor_seconds=anchor_seconds,
            expected_revision=expected_revision,
            updated_by=updated_by,
        )
    except ConfigUpdateError as exc:
        return _json({"error": exc.code, "message": exc.message}, _CONFIG_ERROR_STATUS.get(exc.code, 400))
    return _json(row)


@bp.route(route="api/data-collection/reset-defaults", methods=["POST"])
async def data_collection_reset_defaults(req: func.HttpRequest) -> func.HttpResponse:
    try:
        principal = authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    cleared = get_dispatcher().reset_config()
    logging.info(
        "Reset data-collection schedule to defaults (%d override rows cleared) by %s",
        cleared,
        principal.object_id,
    )
    return _json({"status": "reset", "cleared": cleared})


# ── Shared completion activity (called by every orchestrator) ─────────────────
@bp.activity_trigger(input_name="payload")
def record_run_outcome(payload: dict) -> dict:
    get_dispatcher().record_outcome(
        payload["pipeline"],
        payload["instanceId"],
        payload.get("outcome") or "completed",
        payload.get("summary"),
    )
    return {"recorded": True}
