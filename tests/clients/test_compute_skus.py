from __future__ import annotations

import http.client
import json
from unittest.mock import MagicMock, patch

from clients.compute_skus import fetch_catalog_pages


@patch("clients.compute_skus.urllib.request.urlopen")
def test_fetches_every_unfiltered_page(urlopen):
    first_response = MagicMock()
    first_response.__enter__.return_value.read.return_value = json.dumps({
        "value": [{"name": "Standard_D2s_v5"}],
        "nextLink": "https://management.azure.com/next-page",
    }).encode()
    second_response = MagicMock()
    second_response.__enter__.return_value.read.return_value = json.dumps({
        "value": [{"name": "Standard_E2s_v5"}],
    }).encode()
    urlopen.side_effect = [first_response, second_response]

    result = fetch_catalog_pages("sub-a", "token")

    assert result["status"] == "ok"
    assert result["pages"] == 2
    assert result["records"] == 2
    first_url = urlopen.call_args_list[0].args[0].full_url
    assert "$filter" not in first_url
    assert "location" not in first_url.lower()


@patch("clients.compute_skus.urllib.request.urlopen")
def test_incomplete_response_is_transient(urlopen):
    response = MagicMock()
    response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(
        b"partial",
        1024,
    )
    urlopen.return_value = response

    result = fetch_catalog_pages("sub-a", "token")

    assert result["status"] == "transient"
    assert result["retryAfter"] == 5
    assert "1024 more expected" in result["error"]