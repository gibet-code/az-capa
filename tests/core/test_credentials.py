from __future__ import annotations

import concurrent.futures
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from azure.core.credentials import AccessToken

from core import credentials


class CachedTokenCredentialTests(unittest.TestCase):
    def test_cache_hit_and_refresh_skew(self):
        inner = MagicMock()
        inner.get_token.side_effect = [
            AccessToken("first", int(time.time()) + 3600),
            AccessToken("second", int(time.time()) + 3600),
        ]
        cached = credentials._CachedTokenCredential(inner)

        self.assertEqual("first", cached.get_token("scope").token)
        self.assertEqual("first", cached.get_token("scope").token)
        cached._tokens[(('scope',), None, None, False)] = AccessToken(
            "expiring", int(time.time()) + 299
        )
        self.assertEqual("second", cached.get_token("scope").token)
        self.assertEqual(2, inner.get_token.call_count)

    def test_scope_and_request_options_are_isolated(self):
        inner = MagicMock()
        inner.get_token.side_effect = lambda *scopes, **kwargs: AccessToken(
            f"{scopes}:{kwargs.get('tenant_id')}:{kwargs.get('claims')}",
            int(time.time()) + 3600,
        )
        cached = credentials._CachedTokenCredential(inner)

        cached.get_token("scope-a")
        cached.get_token("scope-b")
        cached.get_token("scope-a", tenant_id="tenant-a")
        cached.get_token("scope-a", claims="claims-a")
        cached.get_token("scope-a", tenant_id="tenant-a")

        self.assertEqual(4, inner.get_token.call_count)

    def test_cae_option_is_part_of_cache_key(self):
        inner = MagicMock()
        inner.get_token.return_value = AccessToken("token", int(time.time()) + 3600)
        cached = credentials._CachedTokenCredential(inner)

        cached.get_token("scope")
        cached.get_token("scope", enable_cae=True)
        cached.get_token("scope", enable_cae=True)

        self.assertEqual(2, inner.get_token.call_count)

    def test_concurrent_miss_refreshes_once(self):
        inner = MagicMock()

        def get_token(*_scopes, **_kwargs):
            time.sleep(0.02)
            return AccessToken("token", int(time.time()) + 3600)

        inner.get_token.side_effect = get_token
        cached = credentials._CachedTokenCredential(inner)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            tokens = list(executor.map(lambda _index: cached.get_token("scope").token, range(20)))

        self.assertEqual(["token"] * 20, tokens)
        self.assertEqual(1, inner.get_token.call_count)

    def test_failed_refresh_is_not_cached(self):
        inner = MagicMock()
        inner.get_token.side_effect = [RuntimeError("unavailable"), AccessToken(
            "token", int(time.time()) + 3600
        )]
        cached = credentials._CachedTokenCredential(inner)

        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            cached.get_token("scope")
        self.assertEqual("token", cached.get_token("scope").token)
        self.assertEqual(2, inner.get_token.call_count)

    def test_get_credential_constructs_singleton(self):
        created = MagicMock()
        with patch.object(credentials, "_credential", None), patch.object(
            credentials, "_create_credential", return_value=created
        ) as create:
            first = credentials.get_credential()
            second = credentials.get_credential()

        self.assertIs(first, second)
        create.assert_called_once_with()

    def test_get_credential_reuses_value_initialized_while_waiting_for_lock(self):
        initialized = MagicMock()

        class InitializingLock:
            def __enter__(self):
                credentials._credential = initialized

            def __exit__(self, *_args):
                return False

        with (
            patch.object(credentials, "_credential", None),
            patch.object(credentials, "_credential_lock", InitializingLock()),
            patch.object(credentials, "_create_credential") as create,
        ):
            result = credentials.get_credential()

        self.assertIs(initialized, result)
        create.assert_not_called()

    def test_close_delegates_when_inner_supports_it(self):
        inner = MagicMock()
        cached = credentials._CachedTokenCredential(inner)

        cached.close()

        inner.close.assert_called_once_with()

    def test_close_is_optional(self):
        inner = object()

        credentials._CachedTokenCredential(inner).close()


class CredentialSelectionTests(unittest.TestCase):
    def test_local_detection_requires_exact_azurite_value(self):
        with patch.dict(os.environ, {"AzureWebJobsStorage": "UseDevelopmentStorage=true"}):
            self.assertTrue(credentials.is_running_locally())
        with patch.dict(os.environ, {"AzureWebJobsStorage": "https://storage"}):
            self.assertFalse(credentials.is_running_locally())

    @patch("core.credentials.AzureCliCredential")
    @patch("core.credentials.is_running_locally", return_value=True)
    def test_create_credential_uses_cli_by_default_locally(self, _local, azure_cli):
        with patch.object(credentials, "LOCAL_AUTH_TYPE", "AzureCliCredential"):
            result = credentials._create_credential()

        self.assertIs(azure_cli.return_value, result)
        azure_cli.assert_called_once_with(process_timeout=60)

    @patch("core.credentials.ClientSecretCredential")
    @patch("core.credentials.is_running_locally", return_value=True)
    def test_create_credential_uses_configured_client_secret_locally(self, _local, client_secret):
        with (
            patch.object(credentials, "LOCAL_AUTH_TYPE", "ClientSecretCredential"),
            patch.object(credentials, "LOCAL_TENANT_ID", "tenant"),
            patch.object(credentials, "LOCAL_CLIENT_ID", "client"),
            patch.object(credentials, "LOCAL_CLIENT_SECRET", "secret"),
        ):
            result = credentials._create_credential()

        self.assertIs(client_secret.return_value, result)
        client_secret.assert_called_once_with(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
        )

    @patch("core.credentials.ManagedIdentityCredential")
    @patch("core.credentials.is_running_locally", return_value=False)
    def test_create_credential_uses_configured_managed_identity_in_azure(
        self,
        _local,
        managed_identity,
    ):
        with patch.object(credentials, "AZURE_CLIENT_ID", "managed-client"):
            result = credentials._create_credential()

        self.assertIs(managed_identity.return_value, result)
        managed_identity.assert_called_once_with(client_id="managed-client")


if __name__ == "__main__":
    unittest.main()
