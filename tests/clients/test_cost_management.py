from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from clients.cost_management import (
    _post_query,
    build_query_body,
    build_scope,
    combine_filters,
    dimension_filter,
    page_next_link,
    post_query_once,
    query_metric,
    rows_from_payload,
)
from core.settings import METHOD_PER_BILLING, METHOD_PER_SUB


def _response(payload: bytes):
    response = MagicMock()
    response.read.return_value = payload
    response.__enter__.return_value = response
    return response


def _http_error(code: int, body: bytes = b"error", headers=None):
    return urllib.error.HTTPError("https://example", code, "failure", headers or {}, io.BytesIO(body))


def test_build_scope_validates_and_escapes_identifiers():
    assert build_scope(METHOD_PER_SUB, subscription_id="sub/id") == "/subscriptions/sub%2Fid"
    assert build_scope(METHOD_PER_BILLING, billing_account_id="account/id", billing_profile_id="profile id") == (
        "/providers/Microsoft.Billing/billingAccounts/account%2Fid/billingProfiles/profile%20id"
    )
    with pytest.raises(ValueError, match="subscription_id"):
        build_scope(METHOD_PER_SUB)
    with pytest.raises(ValueError, match="billing_account_id"):
        build_scope(METHOD_PER_BILLING)
    with pytest.raises(ValueError, match="Unknown Cost Management method"):
        build_scope("invalid")


def test_filter_helpers_preserve_single_and_multiple_shapes():
    first = dimension_filter("ResourceType", ["vm"])
    second = dimension_filter("SubscriptionId", ["sub-a"])

    assert combine_filters([]) is None
    assert combine_filters([None, first]) == first
    assert combine_filters([first, second]) == {"and": [first, second]}


def test_build_query_body_supports_multiple_metrics_and_grouping():
    body = build_query_body(
        "2026-08-01",
        "2026-08-02",
        {"filter": "value"},
        "AmortizedCost",
        ["UsageQuantity", "Cost"],
        ["ResourceId", "SubscriptionId"],
    )

    assert body["timePeriod"] == {
        "from": "2026-08-01T00:00:00+00:00",
        "to": "2026-08-02T23:59:59+00:00",
    }
    assert set(body["dataset"]["aggregation"]) == {"UsageQuantity", "Cost"}
    assert body["dataset"]["grouping"] == [
        {"type": "Dimension", "name": "ResourceId"},
        {"type": "Dimension", "name": "SubscriptionId"},
    ]


def test_response_helpers_handle_rows_and_missing_properties():
    payload = {
        "properties": {
            "columns": [{"name": "ResourceId"}, {"name": "Cost"}],
            "rows": [["vm-1", 2.5]],
            "nextLink": "https://next",
        }
    }

    assert rows_from_payload(payload) == [{"ResourceId": "vm-1", "Cost": 2.5}]
    assert page_next_link(payload) == "https://next"
    assert rows_from_payload({}) == []
    assert page_next_link({}) is None


@patch("clients.cost_management.urllib.request.urlopen")
def test_post_query_once_returns_payload(urlopen):
    urlopen.return_value = _response(b'{"properties":{"rows":[]}}')

    assert post_query_once("https://example", "token", {"query": True}) == {
        "status": "ok",
        "payload": {"properties": {"rows": []}},
    }


@patch("clients.cost_management.urllib.request.urlopen")
def test_post_query_once_honors_largest_cost_management_retry_header(urlopen):
    urlopen.side_effect = _http_error(429, headers={
        "Retry-After": "2",
        "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after": "7",
    })

    assert post_query_once("https://example", "token", {}) == {
        "status": "throttled",
        "retryAfter": 8.0,
    }


@patch("clients.cost_management.urllib.request.urlopen")
def test_post_query_once_classifies_server_network_and_client_errors(urlopen):
    urlopen.side_effect = _http_error(503)
    assert post_query_once("https://example", "token", {}) == {
        "status": "throttled",
        "retryAfter": 20.0,
    }

    urlopen.side_effect = urllib.error.URLError("offline")
    assert post_query_once("https://example", "token", {}) == {
        "status": "throttled",
        "retryAfter": 20.0,
    }

    urlopen.side_effect = _http_error(400, b"bad query")
    assert post_query_once("https://example", "token", {}) == {
        "status": "error",
        "error": "Cost Management query failed (400): bad query",
    }


@patch("clients.cost_management._post_query")
def test_query_metric_follows_next_links_with_same_body(post_query):
    post_query.side_effect = [
        {
            "properties": {
                "columns": [{"name": "ResourceId"}],
                "rows": [["one"]],
                "nextLink": "https://next",
            }
        },
        {"properties": {"columns": [{"name": "ResourceId"}], "rows": [["two"]]}},
    ]

    rows = query_metric(
        "/subscriptions/sub-a",
        "token",
        "2026-08-01",
        "2026-08-02",
        None,
        "ActualCost",
        "Cost",
    )

    assert rows == [{"ResourceId": "one"}, {"ResourceId": "two"}]
    assert post_query.call_args_list[1] == call("https://next", "token", post_query.call_args_list[0].args[2])


@patch("clients.cost_management.time.sleep")
@patch("clients.cost_management.urllib.request.urlopen")
def test_sync_post_retries_transient_http_and_network_errors(urlopen, sleep):
    urlopen.side_effect = [
        _http_error(503, headers={"Retry-After": "4"}),
        urllib.error.URLError("offline"),
        _response(b'{"properties":{}}'),
    ]

    assert _post_query("https://example", "token", {}, max_retries=2) == {"properties": {}}
    assert sleep.call_args_list == [call(5.0), call(20.0)]


@patch("clients.cost_management.urllib.request.urlopen")
def test_sync_post_surfaces_terminal_http_error(urlopen):
    urlopen.side_effect = _http_error(403, b"forbidden")

    with pytest.raises(RuntimeError, match=r"Cost Management query failed \(403\): forbidden"):
        _post_query("https://example", "token", {}, max_retries=2)
