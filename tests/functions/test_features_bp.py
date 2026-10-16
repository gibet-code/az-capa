from __future__ import annotations

import json
from unittest.mock import patch

import azure.functions as func

from functions import features_bp
from services import feature_activation


def _request(method="GET", feature_id=None, body=None):
    return func.HttpRequest(
        method=method,
        url="http://localhost/api/features",
        headers={"content-type": "application/json"},
        params={},
        route_params={"feature_id": feature_id} if feature_id else {},
        body=json.dumps(body).encode() if body is not None else b"",
    )


@patch("functions.features_bp.authorization.require_user")
def test_list_features_is_available_to_user(require_user):
    value = [{"id": "odcr", "enabled": True}]
    with patch.object(features_bp.feature_activation.service, "list_features", return_value=value):
        response = features_bp.feature_list(_request())

    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"features": value}
    require_user.assert_called_once()


@patch("functions.features_bp.authorization.require_admin")
def test_patch_requires_strict_boolean(require_admin):
    response = features_bp.feature_update(
        _request("PATCH", "odcr", {"enabled": 1})
    )

    assert response.status_code == 400


@patch("functions.features_bp.authorization.require_admin")
def test_patch_maps_last_feature_error(require_admin):
    with patch.object(
        features_bp.feature_activation.service,
        "update",
        side_effect=feature_activation.AtLeastOneFeatureRequiredError("keep one"),
    ):
        response = features_bp.feature_update(
            _request("PATCH", "odcr", {"enabled": False})
        )

    assert response.status_code == 409
    assert json.loads(response.get_body())["error"] == "at_least_one_feature_required"
