from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import azure.functions as func

from functions import _authorization as authorization
from functions.web_bp import api_me, api_me_role, api_settings


def _request(
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> func.HttpRequest:
    return func.HttpRequest(
        method=method,
        url="http://localhost/api/test",
        headers=headers or {},
        params={},
        route_params={},
        body=body,
    )


class WebAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    @patch("functions.web_bp.identity.decode_jwt_claims", return_value={
        "name": "Jane Developer",
        "preferred_username": "jane@example.com",
    })
    @patch("functions.web_bp.identity.for_user_action")
    @patch("functions._authorization.credentials.is_running_locally", return_value=True)
    def test_me_returns_cli_identity_and_local_admin_capabilities(
        self, _local, for_user_action, _decode_claims
    ):
        for_user_action.return_value.get_token.return_value = "cli-arm-token"

        response = api_me(_request())

        body = json.loads(response.get_body())
        self.assertEqual(200, response.status_code)
        self.assertTrue(body["local"])
        self.assertFalse(body["authenticated"])
        self.assertEqual("Jane Developer", body["name"])
        self.assertEqual("jane@example.com", body["detail"])
        self.assertEqual(["Admin"], body["roles"])
        self.assertTrue(body["capabilities"]["useOdcr"])
        self.assertTrue(body["capabilities"]["administer"])

    @patch("functions.web_bp.credentials.is_running_locally", return_value=True)
    def test_local_role_switch_sets_http_only_cookie(self, _local):
        response = api_me_role(
            _request(method="POST", body=json.dumps({"role": "User"}).encode())
        )

        self.assertEqual(200, response.status_code)
        cookie = response.headers["Set-Cookie"]
        self.assertIn(f"{authorization.LOCAL_ROLE_COOKIE}=User", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    @patch("functions.web_bp.credentials.is_running_locally", return_value=False)
    def test_role_switch_is_not_available_in_azure(self, _local):
        self.assertEqual(404, api_me_role(_request(method="POST")).status_code)

    @patch("functions.web_bp.settings.scope_subscription_ids")
    @patch("functions.web_bp.authorization.require_admin")
    def test_settings_forbidden_stops_before_settings_read(self, require_admin, scope_ids):
        require_admin.side_effect = authorization.AuthorizationError(403, "forbidden", "Admin required")

        response = api_settings(_request())

        self.assertEqual(403, response.status_code)
        scope_ids.assert_not_called()

if __name__ == "__main__":
    unittest.main()
