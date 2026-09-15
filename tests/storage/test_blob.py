from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from storage import blob


class BlobServiceClientTests(unittest.TestCase):
    def setUp(self):
        blob._service_clients.clear()
        self.addCleanup(blob._service_clients.clear)

    @patch("storage.blob.BlobServiceClient")
    @patch("storage.blob.credentials.get_credential")
    @patch("storage.blob.settings.blob_storage_endpoint", return_value="https://example.blob.core.windows.net")
    @patch("storage.blob.credentials.is_running_locally", return_value=False)
    def test_azure_client_uses_account_url(
        self,
        _is_local,
        _endpoint,
        get_credential,
        blob_service_client,
    ):
        credential = MagicMock()
        get_credential.return_value = credential

        result = blob.blob_service_client()

        self.assertIs(blob_service_client.return_value, result)
        blob_service_client.assert_called_once_with(
            account_url="https://example.blob.core.windows.net",
            credential=credential,
            api_version="2023-11-03",
        )


if __name__ == "__main__":
    unittest.main()