from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import identity
from core.identity import AzureIdentity, UserAuditMetadata, user_audit_metadata


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class IdentityTests(unittest.TestCase):
    def test_decode_jwt_claims_decodes_payload_and_rejects_opaque_token(self):
        self.assertEqual(
            {"tid": "tenant", "oid": "object"},
            identity.decode_jwt_claims(_jwt({"tid": "tenant", "oid": "object"})),
        )
        self.assertEqual({}, identity.decode_jwt_claims("opaque"))

    def test_static_token_credential_enforces_audience_and_expiry(self):
        credential = identity._StaticTokenCredential(
            _jwt({"exp": 2_000_000_000}),
            identity.ARM_SCOPE,
        )

        token = credential.get_token(identity.ARM_SCOPE)

        self.assertEqual(2_000_000_000, token.expires_on)
        with self.assertRaisesRegex(ValueError, "cannot mint a token"):
            credential.get_token("https://storage.azure.com/.default")

    @patch("core.identity.time.time", return_value=1_000)
    def test_static_token_credential_gives_opaque_token_short_fallback_expiry(self, _time):
        credential = identity._StaticTokenCredential("opaque", identity.ARM_SCOPE)

        self.assertEqual(1_300, credential.get_token(identity.ARM_SCOPE).expires_on)

    def test_azure_identity_delegates_credential_and_raw_token(self):
        credential = MagicMock()
        credential.get_token.return_value = SimpleNamespace(token="bearer")
        who = AzureIdentity("function", "managed_identity", credential)

        self.assertIs(credential, who.credential())
        self.assertEqual("bearer", who.get_token("scope"))
        credential.get_token.assert_called_once_with("scope")

    @patch("core.identity.get_credential")
    @patch("core.identity.is_running_locally", return_value=True)
    def test_function_identity_uses_cli_locally(self, _local, get_credential):
        who = identity.function_identity()

        self.assertEqual(("function", "azure_cli"), (who.kind, who.source))
        self.assertIs(get_credential.return_value, who.credential())

    @patch("core.identity.get_credential")
    @patch("core.identity.is_running_locally", return_value=False)
    def test_function_identity_uses_managed_identity_in_azure(self, _local, get_credential):
        who = identity.function_identity()

        self.assertEqual(("function", "managed_identity"), (who.kind, who.source))
        self.assertIs(get_credential.return_value, who.credential())

    @patch("core.identity.get_credential")
    @patch("core.identity.is_running_locally", return_value=True)
    def test_user_identity_uses_cli_locally(self, _local, get_credential):
        who = identity.user_identity(SimpleNamespace(headers={}))

        self.assertEqual(("user", "azure_cli"), (who.kind, who.source))
        self.assertIs(get_credential.return_value, who.credential())

    @patch("core.identity.is_running_locally", return_value=False)
    def test_user_identity_requires_forwarded_token_in_azure(self, _local):
        with self.assertRaises(identity.UserNotAuthenticated):
            identity.user_identity(SimpleNamespace(headers={}))

    @patch("core.identity.is_running_locally", return_value=False)
    def test_user_identity_wraps_forwarded_token_in_azure(self, _local):
        token = _jwt({"exp": 2_000_000_000})

        who = identity.user_identity(SimpleNamespace(headers={
            "X-MS-TOKEN-AAD-ACCESS-TOKEN": token,
        }))

        self.assertEqual(("user", "easy_auth"), (who.kind, who.source))
        self.assertEqual(token, who.get_token())

    @patch("core.identity.function_identity")
    def test_backend_operation_delegates_to_function_identity(self, function_identity):
        self.assertIs(function_identity.return_value, identity.for_backend_operation())

    @patch("core.identity.user_identity")
    def test_user_action_delegates_to_user_identity(self, user_identity):
        request = object()

        self.assertIs(user_identity.return_value, identity.for_user_action(request))
        user_identity.assert_called_once_with(request)


class UserAuditMetadataTests(unittest.TestCase):
    @patch("core.identity.decode_jwt_claims", return_value={
        "tid": "TENANT", "oid": "OBJECT", "name": "Jane Doe", "upn": "jane@example.com",
    })
    def test_extracts_stable_ids_and_display_name(self, _decode):
        who = AzureIdentity("user", "easy_auth", MagicMock())
        audit = user_audit_metadata(who, "token")
        self.assertEqual(UserAuditMetadata("tenant", "object", "Jane Doe"), audit)

    def test_rejects_non_user_identity(self):
        who = AzureIdentity("function", "managed_identity", MagicMock())
        with self.assertRaisesRegex(ValueError, "user identity"):
            user_audit_metadata(who, "token")

    @patch("core.identity.decode_jwt_claims", return_value={
        "tid": "TENANT", "sub": "SUBJECT", "preferred_username": "jane@example.com",
    })
    def test_falls_back_to_subject_and_preferred_username(self, _decode):
        who = AzureIdentity("user", "azure_cli", MagicMock())
        self.assertEqual(
            UserAuditMetadata("tenant", "subject", "jane@example.com"),
            user_audit_metadata(who, "token"),
        )

    @patch("core.identity.decode_jwt_claims", return_value={"tid": "tenant"})
    def test_rejects_missing_stable_object_id(self, _decode):
        who = AzureIdentity("user", "easy_auth", MagicMock())
        with self.assertRaisesRegex(ValueError, "tid and oid/sub"):
            user_audit_metadata(who, "token")


if __name__ == "__main__":
    unittest.main()