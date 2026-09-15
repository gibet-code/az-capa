from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

import azure.functions as func

from functions import _authorization as authorization


def _request(headers: dict[str, str] | None = None) -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url="http://localhost/api/test",
        headers=headers or {},
        params={},
        route_params={},
        body=b"",
    )


def _principal_header(*roles: str, role_type: str = "roles") -> str:
    payload = {
        "auth_typ": "aad",
        "name_typ": "name",
        "role_typ": role_type,
        "claims": [
            {"typ": "name", "val": "Test User"},
            {"typ": "preferred_username", "val": "test@example.com"},
            *({"typ": "roles", "val": role} for role in roles),
        ],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


class AuthorizationTests(unittest.TestCase):
    @patch("functions._authorization.credentials.is_running_locally", return_value=False)
    def test_missing_principal_is_unauthorized(self, _local):
        with self.assertRaises(authorization.AuthorizationError) as raised:
            authorization.require_user(_request())
        self.assertEqual(401, raised.exception.status_code)

    @patch("functions._authorization.credentials.is_running_locally", return_value=False)
    def test_malformed_principal_is_unauthorized(self, _local):
        with self.assertRaises(authorization.AuthorizationError) as raised:
            authorization.require_admin(_request({"X-MS-CLIENT-PRINCIPAL": "not-base64"}))
        self.assertEqual(401, raised.exception.status_code)

    @patch("functions._authorization.credentials.is_running_locally", return_value=False)
    def test_user_policy_accepts_user_and_admin(self, _local):
        for role in (authorization.USER_ROLE, authorization.ADMIN_ROLE):
            with self.subTest(role=role):
                principal = authorization.require_user(
                    _request({"X-MS-CLIENT-PRINCIPAL": _principal_header(role)})
                )
                self.assertIn(role, principal.roles)

    @patch("functions._authorization.credentials.is_running_locally", return_value=False)
    def test_admin_policy_rejects_user(self, _local):
        with self.assertRaises(authorization.AuthorizationError) as raised:
            authorization.require_admin(
                _request({"X-MS-CLIENT-PRINCIPAL": _principal_header("User")})
            )
        self.assertEqual(403, raised.exception.status_code)

    @patch("functions._authorization.credentials.is_running_locally", return_value=False)
    def test_role_type_controls_role_claim_selection(self, _local):
        principal = authorization.require_admin(
            _request({"X-MS-CLIENT-PRINCIPAL": _principal_header("Admin")})
        )
        self.assertEqual("Test User", principal.name)
        self.assertEqual("test@example.com", principal.identity)

    @patch("functions._authorization.credentials.is_running_locally", return_value=False)
    def test_easy_auth_roles_claim_is_accepted_when_role_type_differs(self, _local):
        principal = authorization.require_user(
            _request({
                "X-MS-CLIENT-PRINCIPAL": _principal_header(
                    "User",
                    role_type="http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
                )
            })
        )

        self.assertEqual(frozenset({"User"}), principal.roles)

    @patch("functions._authorization.credentials.is_running_locally", return_value=True)
    def test_local_default_is_admin(self, _local):
        principal = authorization.require_admin(_request())
        self.assertTrue(principal.local)

    @patch("functions._authorization.credentials.is_running_locally", return_value=True)
    def test_local_cookie_switches_user_and_none(self, _local):
        user = authorization.require_user(
            _request({"Cookie": f"{authorization.LOCAL_ROLE_COOKIE}=User"})
        )
        self.assertEqual(frozenset({"User"}), user.roles)
        with self.assertRaises(authorization.AuthorizationError) as raised:
            authorization.require_user(
                _request({"Cookie": f"{authorization.LOCAL_ROLE_COOKIE}=None"})
            )
        self.assertEqual(403, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()