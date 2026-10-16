"""Feature activation HTTP endpoints."""
from __future__ import annotations

import json

import azure.durable_functions as df
import azure.functions as func

from services import feature_activation
from . import _authorization as authorization

bp = df.Blueprint()


def _json(body: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status_code, mimetype="application/json")


@bp.route(route="api/features", methods=["GET"])
def feature_list(req: func.HttpRequest) -> func.HttpResponse:
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    try:
        return _json({"features": feature_activation.service.list_features()})
    except feature_activation.FeatureStateUnavailableError as exc:
        return _json({"error": "feature_state_unavailable", "message": str(exc)}, 503)


@bp.route(route="api/features/{feature_id}", methods=["PATCH"])
def feature_update(req: func.HttpRequest) -> func.HttpResponse:
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    feature_id = req.route_params.get("feature_id") or ""
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "invalid_body", "message": "A JSON body is required."}, 400)
    if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
        return _json({"error": "invalid_body", "message": "enabled must be a JSON boolean."}, 400)
    try:
        return _json(feature_activation.service.update(feature_id, body["enabled"]))
    except feature_activation.UnknownFeatureError as exc:
        return _json({"error": "unknown_feature", "message": str(exc)}, 404)
    except feature_activation.AtLeastOneFeatureRequiredError as exc:
        return _json({"error": "at_least_one_feature_required", "message": str(exc)}, 409)
    except feature_activation.FeatureUpdateConflictError as exc:
        return _json({"error": "feature_update_conflict", "message": str(exc)}, 409)
    except feature_activation.FeatureStateUnavailableError as exc:
        return _json({"error": "feature_state_unavailable", "message": str(exc)}, 503)
