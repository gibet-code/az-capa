from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import azure.functions as func

from functions import data_collection_bp
from functions.data_collection_bp import data_collection_config
from services.data_collection.dispatcher import ConfigUpdateError


def _principal() -> MagicMock:
    principal = MagicMock()
    principal.object_id = "admin-oid"
    principal.name = "Admin"
    principal.tenant_id = "tenant"
    return principal


def _patch_request(pipeline: str, body: dict | None) -> func.HttpRequest:
    raw = json.dumps(body).encode() if body is not None else b""
    return func.HttpRequest(
        method="PATCH",
        url=f"http://localhost/api/data-collection/{pipeline}/config",
        headers={"content-type": "application/json"},
        params={},
        route_params={"pipeline": pipeline},
        body=raw,
    )


class ConfigEndpointTests(unittest.IsolatedAsyncioTestCase):
    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_patch_config_success(self, require_admin):
        require_admin.return_value = _principal()
        saved = {"pipeline": "cr_usage", "revision": 2, "enabled": True}
        request = _patch_request(
            "cr_usage",
            {"enabled": True, "frequencySeconds": 3600, "anchorSeconds": 0, "expectedRevision": 1},
        )
        with patch.object(
            data_collection_bp._dispatcher, "update_config", return_value=saved
        ) as update_config:
            response = await data_collection_config(request)

        self.assertEqual(200, response.status_code)
        self.assertEqual(saved, json.loads(response.get_body()))
        _, kwargs = update_config.call_args
        self.assertEqual(1, kwargs["expected_revision"])
        self.assertEqual("admin-oid", kwargs["updated_by"]["objectId"])

    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_patch_config_unknown_pipeline_404(self, require_admin):
        require_admin.return_value = _principal()
        request = _patch_request(
            "nope", {"enabled": True, "frequencySeconds": 3600, "anchorSeconds": 0}
        )
        response = await data_collection_config(request)
        self.assertEqual(404, response.status_code)

    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_patch_config_invalid_body_400(self, require_admin):
        require_admin.return_value = _principal()
        request = _patch_request("cr_usage", {"enabled": True})  # missing frequency/anchor
        response = await data_collection_config(request)
        self.assertEqual(400, response.status_code)

    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_patch_config_cannot_disable_409(self, require_admin):
        require_admin.return_value = _principal()
        request = _patch_request(
            "subscription_reference",
            {"enabled": False, "frequencySeconds": 900, "anchorSeconds": 0, "expectedRevision": 1},
        )
        with patch.object(
            data_collection_bp._dispatcher,
            "update_config",
            side_effect=ConfigUpdateError("cannot_disable", "no"),
        ):
            response = await data_collection_config(request)
        self.assertEqual(409, response.status_code)
        self.assertEqual("cannot_disable", json.loads(response.get_body())["error"])

    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_patch_config_invalid_frequency_400(self, require_admin):
        require_admin.return_value = _principal()
        request = _patch_request(
            "cr_usage",
            {"enabled": True, "frequencySeconds": 60, "anchorSeconds": 0, "expectedRevision": 1},
        )
        with patch.object(
            data_collection_bp._dispatcher,
            "update_config",
            side_effect=ConfigUpdateError("invalid_frequency", "too small"),
        ):
            response = await data_collection_config(request)
        self.assertEqual(400, response.status_code)


def _reset_request() -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="http://localhost/api/data-collection/reset-defaults",
        headers={"content-type": "application/json"},
        params={},
        route_params={},
        body=b"{}",
    )


class ResetDefaultsEndpointTests(unittest.IsolatedAsyncioTestCase):
    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_reset_defaults_clears_overrides(self, require_admin):
        require_admin.return_value = _principal()
        with patch.object(
            data_collection_bp._dispatcher, "reset_config", return_value=3
        ) as reset_config:
            response = await data_collection_bp.data_collection_reset_defaults(_reset_request())

        self.assertEqual(200, response.status_code)
        body = json.loads(response.get_body())
        self.assertEqual({"status": "reset", "cleared": 3}, body)
        reset_config.assert_called_once_with()

    @patch("functions.data_collection_bp.authorization.require_admin")
    async def test_reset_defaults_requires_admin(self, require_admin):
        from functions import _authorization as authorization

        require_admin.side_effect = authorization.AuthorizationError(403, "forbidden", "no")
        with patch.object(data_collection_bp._dispatcher, "reset_config") as reset_config:
            response = await data_collection_bp.data_collection_reset_defaults(_reset_request())

        self.assertEqual(403, response.status_code)
        reset_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
