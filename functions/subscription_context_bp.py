"""Synchronous Business Context administration endpoints."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

import azure.durable_functions as df
import azure.functions as func

from core import identity, settings
from services.subscription_context import (
    ContextFieldConflictError,
    build_manual_template,
    build_preview,
    parse_context_field,
    parse_manual_mapping,
    rank_subscription_tag_keys,
    service as context_service,
)
from ._durable import with_run_recording
from . import _authorization as authorization
from ._app_scope_http import app_scope_error_response

bp = df.Blueprint()


def _json_response(body: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, default=str), status_code=status_code, mimetype="application/json")


def _authorize(req: func.HttpRequest):
    authorization.require_admin(req)
    return identity.for_user_action(req)


def _backend_inventory_and_scope() -> tuple[list[dict], tuple[str, ...]]:
    return context_service.backend_inventory_and_scope()


def _uploaded_field(req: func.HttpRequest):
    definition_raw = req.form.get("definition")
    upload = req.files.get("file")
    if not definition_raw or upload is None:
        raise ValueError("Multipart fields 'definition' and 'file' are required.")
    field = parse_context_field(json.loads(definition_raw))
    if field.value_source.get("type") != "manual_file":
        raise ValueError("Multipart uploads require the manual_file Value source.")
    content = upload.read()
    if len(content) > settings.subscription_context_max_upload_bytes():
        raise ValueError("Mapping file exceeds the configured upload limit.")
    return field, parse_manual_mapping(content, upload.filename)


def _is_json_request(req: func.HttpRequest) -> bool:
    return str(req.headers.get("content-type") or "").split(";", 1)[0].strip().lower() == "application/json"


def _json_field(req: func.HttpRequest):
    field = parse_context_field(req.get_json())
    if field.value_source.get("type") not in {"management_group_level", "subscription_tag"}:
        raise ValueError("JSON requests require a dynamic Value source.")
    return field


def _save_field(req: func.HttpRequest, who, expected_key: str | None = None) -> func.HttpResponse:
    try:
        inventory, in_scope = _backend_inventory_and_scope()
        audit = identity.user_audit_metadata(who)
        if _is_json_request(req):
            field = _json_field(req)
            if expected_key is not None and field.key != expected_key.strip().lower():
                raise ValueError("Definition Field key must match the route Field key.")
            if field.value_source["type"] == "management_group_level":
                saved, mapping, missing_reasons = context_service.save_management_group_field(
                    field,
                    inventory,
                    audit,
                )
            else:
                saved, mapping, missing_reasons = context_service.save_subscription_tag_field(
                    field,
                    inventory,
                    audit,
                )
        else:
            field, mapping = _uploaded_field(req)
            if expected_key is not None and field.key != expected_key.strip().lower():
                raise ValueError("Definition Field key must match the route Field key.")
            saved = context_service.save_manual_field(field, mapping, audit)
            missing_reasons = {}
        preview = build_preview(
            mapping,
            [item["id"] for item in inventory],
            in_scope,
            missing_reasons,
        )
    except ContextFieldConflictError as exc:
        return _json_response({"error": "revision_conflict", "message": str(exc)}, 409)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response({"error": "invalid_context_field", "message": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 - persistence/Azure failures are returned to the admin
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Business Context save failed")
        return _json_response({"error": "save_failed", "message": str(exc)}, 502)
    return _json_response({"field": saved, **preview}, 201 if expected_key is None else 200)


@bp.route(route="api/subscription-context", methods=["GET", "POST"])
def context_fields(req: func.HttpRequest) -> func.HttpResponse:
    try:
        who = _authorize(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "An ARM access token is required."}, 401)
    if str(req.method).upper() == "GET":
        try:
            fields = context_service.list_fields()
        except Exception as exc:  # noqa: BLE001 - storage failures are returned to the admin
            logging.exception("Business Context listing failed")
            return _json_response({"error": "list_failed", "message": str(exc)}, 502)
        return _json_response({
            "count": len(fields),
            "maxFields": settings.subscription_context_max_fields(),
            "value": fields,
        })
    return _save_field(req, who)


@bp.route(route="api/subscription-context/preview", methods=["POST"])
def preview_values(req: func.HttpRequest) -> func.HttpResponse:
    try:
        _authorize(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "An ARM access token is required."}, 401)

    try:
        inventory, in_scope = _backend_inventory_and_scope()
        if _is_json_request(req):
            field = _json_field(req)
            if field.value_source["type"] == "management_group_level":
                mapping, missing_reasons = context_service.preview_management_group_field(field, inventory)
            else:
                mapping, missing_reasons = context_service.preview_subscription_tag_field(field, inventory)
        else:
            field, mapping = _uploaded_field(req)
            missing_reasons = {}
        preview = build_preview(
            mapping,
            [item["id"] for item in inventory],
            in_scope,
            missing_reasons,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response({"error": "invalid_context_field", "message": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 - Azure/source failures are returned to the admin
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Business Context preview failed")
        return _json_response({"error": "preview_failed", "message": str(exc)}, 502)

    return _json_response({
        "field": {"key": field.key, "name": field.name, "enabled": field.enabled},
        **preview,
    })


@bp.route(route="api/subscription-context/value-source-options/tags", methods=["GET"])
def tag_source_options(req: func.HttpRequest) -> func.HttpResponse:
    try:
        _authorize(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "An ARM access token is required."}, 401)
    try:
        inventory, in_scope = _backend_inventory_and_scope()
        options = rank_subscription_tag_keys(inventory, in_scope, limit=15)
    except Exception as exc:  # noqa: BLE001 - Resource Graph failures are returned to the admin
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Business Context tag-key discovery failed")
        return _json_response({"error": "tag_options_failed", "message": str(exc)}, 502)
    return _json_response({"count": len(options), "value": options})


@bp.route(route="api/subscription-context/templates/manual", methods=["GET"])
def manual_template(req: func.HttpRequest) -> func.HttpResponse:
    try:
        _authorize(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "An ARM access token is required."}, 401)

    try:
        inventory, in_scope = _backend_inventory_and_scope()
        saved_mapping = None
        field_key = req.params.get("fieldKey")
        if field_key:
            existing = context_service.get_manual_mapping(field_key)
            if existing is None:
                return _json_response({"error": "not_found", "message": "Context field was not found."}, 404)
            _definition, saved_mapping = existing
        content, mimetype, filename = build_manual_template(
            inventory,
            in_scope,
            req.params.get("format", "xlsx"),
            saved_mapping,
        )
    except ValueError as exc:
        return _json_response({"error": "invalid_template_request", "message": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 - Azure/source failures are returned to the admin
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Business Context template generation failed")
        return _json_response({"error": "template_failed", "message": str(exc)}, 502)
    return func.HttpResponse(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route(route="api/subscription-context/resolve", methods=["POST"])
def resolve_values(req: func.HttpRequest) -> func.HttpResponse:
    try:
        _authorize(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "An ARM access token is required."}, 401)
    try:
        body = req.get_json()
        subscription_ids = body.get("subscriptionIds") if isinstance(body, dict) else None
        include_disabled = body.get("includeDisabled", False) if isinstance(body, dict) else False
        if not isinstance(subscription_ids, list) or any(not isinstance(value, str) for value in subscription_ids):
            raise ValueError("subscriptionIds must be an array of strings.")
        if not isinstance(include_disabled, bool):
            raise ValueError("includeDisabled must be a boolean.")
        values = context_service.resolve(subscription_ids, include_disabled)
    except ValueError as exc:
        return _json_response({"error": "invalid_resolve_request", "message": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 - storage failures are returned to the caller
        logging.exception("Business Context resolution failed")
        return _json_response({"error": "resolve_failed", "message": str(exc)}, 502)
    return _json_response({"count": len(values), "value": values})


@bp.orchestration_trigger(context_name="context")
def subscription_context_refresh_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "subscription_context", _run_context_refresh(context)))


def _run_context_refresh(context: df.DurableOrchestrationContext):
    request = context.get_input() or {}
    max_attempts = 3
    for attempt in range(max_attempts):
        result = yield context.call_activity(
            "subscription_context_refresh_activity",
            {
                "refreshRunId": context.instance_id,
                "scheduled": bool(request.get("scheduled")),
                "finalAttempt": attempt == max_attempts - 1,
            },
        )
        if result.get("status") != "transient":
            return result
        yield context.create_timer(
            context.current_utc_datetime + timedelta(seconds=5 * (2 ** attempt))
        )
    return {"status": "failed", "error": "Business Context refresh retries were exhausted."}


@bp.orchestration_trigger(context_name="context")
def subscription_context_flush_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from with_run_recording(context, "subscription_context", _run_context_flush(context)))


def _run_context_flush(context: df.DurableOrchestrationContext):
    max_attempts = 3
    for attempt in range(max_attempts):
        result = yield context.call_activity("subscription_context_flush_activity", {})
        if result.get("status") != "transient":
            return result
        yield context.create_timer(
            context.current_utc_datetime + timedelta(seconds=5 * (2 ** attempt))
        )
    return {"status": "failed", "error": "Business Context flush retries were exhausted."}


@bp.activity_trigger(input_name="request")
def subscription_context_refresh_activity(request: dict) -> dict:
    return context_service.refresh(request)


@bp.activity_trigger(input_name="request")
def subscription_context_flush_activity(request: dict) -> dict:
    return context_service.flush(request)


@bp.route(route="api/subscription-context/{fieldKey}", methods=["GET", "PATCH", "DELETE"])
def zzz_context_field(req: func.HttpRequest) -> func.HttpResponse:
    try:
        who = _authorize(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "An ARM access token is required."}, 401)
    field_key = str(req.route_params.get("fieldKey") or "").strip().lower()
    if not field_key:
        return _json_response({"error": "invalid_field_key", "message": "Field key is required."}, 400)
    method = str(req.method).upper()
    if method == "PATCH":
        return _save_field(req, who, field_key)
    if method == "DELETE":
        try:
            deleted = context_service.delete_field(field_key)
        except Exception as exc:  # noqa: BLE001 - storage failures are returned to the admin
            logging.exception("Business Context deletion failed")
            return _json_response({"error": "delete_failed", "message": str(exc)}, 502)
        if not deleted:
            return _json_response({"error": "not_found", "message": "Context field was not found."}, 404)
        return func.HttpResponse(status_code=204)
    try:
        field = context_service.get_field(field_key)
    except Exception as exc:  # noqa: BLE001 - storage failures are returned to the admin
        logging.exception("Business Context read failed")
        return _json_response({"error": "read_failed", "message": str(exc)}, 502)
    if field is None:
        return _json_response({"error": "not_found", "message": "Context field was not found."}, 404)
    return _json_response({"field": field})