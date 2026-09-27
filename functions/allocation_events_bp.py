"""User-scoped Allocation Events VM inventory endpoint."""
from __future__ import annotations

import json
import logging

import azure.durable_functions as df
import azure.functions as func

from core import identity
from services import request_dimensions
from services import allocation_events

from . import _authorization as authorization
from ._app_scope_http import app_scope_error_response

bp = df.Blueprint()


def _request_filters(req: func.HttpRequest) -> tuple[list[str] | None, list[str] | None, list[dict]]:
    body = req.get_json()
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")

    subscriptions = body.get("subscriptionIds")
    if subscriptions is not None and (
        not isinstance(subscriptions, list)
        or any(not isinstance(value, str) for value in subscriptions)
    ):
        raise ValueError("subscriptionIds must be an array of strings.")

    locations = body.get("locations")
    if locations is not None and (
        not isinstance(locations, list)
        or any(not isinstance(value, str) or not value.strip() for value in locations)
    ):
        raise ValueError("locations must be an array of non-empty strings.")

    context_filters = request_dimensions.parse_context_filters(body.get("contextFilters"))
    return subscriptions, locations, context_filters


def _timeline_resource_id(req: func.HttpRequest) -> str:
    body = req.get_json()
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")
    resource_id = body.get("resourceId")
    return allocation_events.validate_vm_resource_id(resource_id)[0]


@bp.route(route="api/allocation-events/vms", methods=["POST"])
def allocation_event_vms(req: func.HttpRequest) -> func.HttpResponse:
    """List VMs visible to the signed-in user within the configured app scope."""
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)

    try:
        requested_subscriptions, requested_locations, requested_context = _request_filters(req)
    except (TypeError, ValueError) as exc:
        return func.HttpResponse(
            json.dumps({"error": "invalid_filter", "message": str(exc)}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        who = identity.for_user_action(req)
    except identity.UserNotAuthenticated:
        return func.HttpResponse(
            json.dumps({"error": "not_authenticated", "message": "Sign in to list VMs."}),
            status_code=401,
            mimetype="application/json",
        )

    try:
        body = allocation_events.list_visible_vms(
            who,
            subscription_ids=requested_subscriptions,
            locations=requested_locations,
            context_filters=requested_context,
        )
    except Exception as exc:  # noqa: BLE001 - report failures must fail closed
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Allocation Events VM query failed (identity source=%s)", who.source)
        return func.HttpResponse(
            json.dumps({"error": "query_failed", "message": str(exc)}),
            status_code=502,
            mimetype="application/json",
        )
    return func.HttpResponse(json.dumps(body), mimetype="application/json")


@bp.route(route="api/allocation-events/timeline", methods=["POST"])
def allocation_event_timeline(req: func.HttpRequest) -> func.HttpResponse:
    """Return projected history for one VM visible to the signed-in user."""
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)

    try:
        resource_id = _timeline_resource_id(req)
    except (TypeError, ValueError) as exc:
        return func.HttpResponse(
            json.dumps({"error": "invalid_request", "message": str(exc)}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        who = identity.for_user_action(req)
    except identity.UserNotAuthenticated:
        return func.HttpResponse(
            json.dumps({"error": "not_authenticated", "message": "Sign in to view VM history."}),
            status_code=401,
            mimetype="application/json",
        )

    try:
        body = allocation_events.get_vm_timeline(who, resource_id)
    except allocation_events.AllocationEventVmNotFound:
        return func.HttpResponse(
            json.dumps({"error": "not_found", "message": "Virtual machine was not found."}),
            status_code=404,
            mimetype="application/json",
        )
    except Exception as exc:  # noqa: BLE001 - report failures must fail closed
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Allocation Events timeline query failed (identity source=%s)", who.source)
        return func.HttpResponse(
            json.dumps({"error": "query_failed", "message": str(exc)}),
            status_code=502,
            mimetype="application/json",
        )
    return func.HttpResponse(json.dumps(body), mimetype="application/json")
