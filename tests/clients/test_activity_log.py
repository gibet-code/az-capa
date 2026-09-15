from __future__ import annotations

import email.message
import io
import json
import urllib.error
from unittest.mock import patch

from clients.activity_log import (
    build_filter,
    fetch_page,
    normalize_event,
    normalize_events,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: bytes = b"{}", retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://management.azure.com", code, "err", headers, io.BytesIO(body))


_REQUEST = {"subscriptionId": "sub-1", "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}


def test_filter_uses_operations_when_present():
    result = build_filter("start", "end", operations=["provider/o'hare/action"])

    assert "operations eq 'provider/o''hare/action'" in result
    assert "eventTimestamp ge 'start'" in result
    assert "eventTimestamp le 'end'" in result
    assert "eventChannels eq 'Admin, Operation'" in result
    assert "levels eq 'Critical,Error,Warning,Informational'" in result


def test_filter_falls_back_to_resource_provider():
    result = build_filter("start", "end", operations=None, resource_provider="Microsoft.Compute")

    assert "resourceProvider eq 'Microsoft.Compute'" in result
    assert "operations eq" not in result


def test_filter_without_selectors_still_windows():
    result = build_filter("start", "end")

    assert "operations eq" not in result
    assert "resourceProvider eq" not in result
    assert "eventTimestamp ge 'start'" in result


def test_normalize_event_preserves_properties_and_derives_type():
    event = {
        "eventDataId": "e1",
        "operationName": {"value": "Microsoft.Compute/virtualMachines/write"},
        "status": {"value": "Accepted"},
        "correlationId": "corr-1",
        "operationId": "op-1",
        "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        "properties": {"responseBody": "{\"location\":\"eastus\"}"},
    }

    normalized = normalize_event(event)

    assert normalized["eventDataId"] == "e1"
    assert normalized["operationName"] == "Microsoft.Compute/virtualMachines/write"
    assert normalized["status"] == "Accepted"
    assert normalized["correlationId"] == "corr-1"
    assert normalized["operationId"] == "op-1"
    assert normalized["resourceType"] == "Microsoft.Compute/virtualMachines"
    assert normalized["properties"] == {"responseBody": "{\"location\":\"eastus\"}"}


def test_normalize_event_derives_nested_resource_type():
    event = {
        "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/"
        "capacityReservationGroups/crg/capacityReservations/cr",
    }

    assert (
        normalize_event(event)["resourceType"]
        == "Microsoft.Compute/capacityReservationGroups/capacityReservations"
    )


def test_normalize_events_returns_all_without_filtering():
    payload = {"value": [
        {"eventDataId": "a", "operationName": {"value": "x/write"}, "status": {"value": "Succeeded"}},
        {"eventDataId": "b", "operationName": {"value": "y/delete"}, "status": {"value": "Failed"}},
    ]}

    result = normalize_events(payload)

    assert [event["eventDataId"] for event in result] == ["a", "b"]


def test_fetch_page_normalizes_events_on_success():
    body = json.dumps({"value": [{"eventDataId": "a"}], "nextLink": "next"}).encode()
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
        result = fetch_page(_REQUEST, "token")

    assert result["status"] == "ok"
    assert result["received"] == 1
    assert [event["eventDataId"] for event in result["events"]] == ["a"]
    assert result["nextLink"] == "next"


def test_fetch_page_maps_429_to_throttled():
    with patch("urllib.request.urlopen", side_effect=_http_error(429, retry_after="30")):
        result = fetch_page(_REQUEST, "token")

    assert result["status"] == "throttled"
    assert result["statusCode"] == 429
    assert result["retryAfter"] == 30


def test_fetch_page_maps_5xx_to_transient():
    with patch("urllib.request.urlopen", side_effect=_http_error(503)):
        result = fetch_page(_REQUEST, "token")

    assert result["status"] == "transient"
    assert result["statusCode"] == 503


def test_fetch_page_maps_non_transient_http_error_to_error():
    body = b'{"error":{"code":"BadRequest","message":"nope"}}'
    with patch("urllib.request.urlopen", side_effect=_http_error(400, body=body)):
        result = fetch_page(_REQUEST, "token")

    assert result["status"] == "error"
    assert result["statusCode"] == 400
    assert "BadRequest" in result["error"]


def test_fetch_page_maps_urlerror_to_transient():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns fail")):
        result = fetch_page(_REQUEST, "token")

    assert result["status"] == "transient"
    assert result["retryAfter"] == 5