from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from clients.resource_graph import (
    _post,
    get_accessible_subscriptions,
    get_subscription_inventory,
    get_subscriptions_under_management_groups,
    run_graph_query,
)


def _response(payload: bytes):
    response = MagicMock()
    response.read.return_value = payload
    response.__enter__.return_value = response
    return response


def _http_error(code: int, body: bytes = b"error", headers=None):
    return urllib.error.HTTPError("https://example", code, "failure", headers or {}, io.BytesIO(body))


@patch("clients.resource_graph.time.sleep")
@patch("clients.resource_graph.urllib.request.urlopen")
def test_post_retries_transient_error_using_advertised_delay(urlopen, sleep):
    urlopen.side_effect = [
        _http_error(429, headers={"Retry-After": "3"}),
        _response(b'{"data":[{"id":"sub-a"}]}'),
    ]

    result = _post("https://example", "token", {"query": "resources"}, max_retries=1)

    assert result == {"data": [{"id": "sub-a"}]}
    sleep.assert_called_once_with(3)


@patch("clients.resource_graph.time.sleep")
@patch("clients.resource_graph.urllib.request.urlopen")
def test_post_retries_network_error_then_reraises_at_limit(urlopen, sleep):
    failure = urllib.error.URLError("offline")
    urlopen.side_effect = [failure, failure]

    with pytest.raises(urllib.error.URLError):
        _post("https://example", "token", {}, max_retries=1)

    sleep.assert_called_once_with(5.0)


@patch("clients.resource_graph.urllib.request.urlopen")
def test_post_surfaces_non_transient_http_response(urlopen):
    urlopen.side_effect = _http_error(403, b"forbidden")

    with pytest.raises(RuntimeError, match=r"Resource Graph query failed \(403\): forbidden"):
        _post("https://example", "token", {}, max_retries=2)


@patch("clients.resource_graph._post")
def test_graph_query_uses_subscription_scope_on_every_page(post):
    post.side_effect = [
        {"data": [{"id": "one"}], "$skipToken": "next"},
        {"data": [{"id": "two"}]},
    ]

    rows = run_graph_query("resources", "token", subscription_ids=[" SUB-B ", "sub-a", "sub-b"])

    assert rows == [{"id": "one"}, {"id": "two"}]
    assert [call.args[2]["subscriptions"] for call in post.call_args_list] == [
        ["sub-b", "sub-a"],
        ["sub-b", "sub-a"],
    ]
    assert post.call_args_list[1].args[2]["options"]["$skipToken"] == "next"


@patch("clients.resource_graph._post")
def test_graph_query_empty_subscription_scope_fails_closed(post):
    assert run_graph_query("resources", "token", subscription_ids=[]) == []
    post.assert_not_called()


@patch("clients.resource_graph.run_graph_query")
def test_subscription_inventory_normalizes_tags(run_graph_query):
    run_graph_query.return_value = [{
        "id": "SUB-A",
        "name": "Subscription A",
        "state": "Enabled",
        "managementGroupAncestors": [],
        "tags": {"Environment": "PRD", "CostCenter": None},
    }]

    inventory = get_subscription_inventory("token")

    assert inventory[0]["tags"] == {"Environment": "PRD", "CostCenter": ""}


@patch("clients.resource_graph.run_graph_query")
def test_subscription_inventory_normalizes_ancestors_and_sorts(run_graph_query):
    run_graph_query.return_value = [
        {"id": "sub-b", "name": "Zulu", "managementGroupAncestors": None},
        {
            "id": "sub-a",
            "name": " Alpha ",
            "managementGroupAncestors": [
                {"name": " team ", "displayName": " Team Name "},
                {"name": "root", "displayName": ""},
                {"displayName": "ignored"},
            ],
            "tags": "not-a-dict",
        },
        {"id": ""},
    ]

    inventory = get_subscription_inventory("token")

    assert [item["id"] for item in inventory] == ["sub-a", "sub-b"]
    assert inventory[0]["managementGroupAncestors"] == [
        {"id": "team", "name": "Team Name"},
        {"id": "root", "name": "root"},
    ]
    assert inventory[0]["tags"] == {}


@patch("clients.resource_graph.run_graph_query")
def test_accessible_subscriptions_filters_escapes_and_sorts(run_graph_query):
    run_graph_query.return_value = [{"id": "sub-b"}, {"id": "sub-a"}, {"other": "ignored"}]

    result = get_accessible_subscriptions("token", [" SUB-B ", "sub'a", "sub-b"])

    assert result == ["sub-a", "sub-b"]
    query = run_graph_query.call_args.args[0]
    assert "subscriptionId in~ ('sub''a', 'sub-b')" in query


@patch("clients.resource_graph.run_graph_query")
def test_accessible_subscriptions_explicit_empty_scope_fails_closed(run_graph_query):
    assert get_accessible_subscriptions("token", []) == []
    run_graph_query.assert_not_called()


def test_management_group_resolution_requires_ids():
    with pytest.raises(ValueError, match="At least one management group"):
        get_subscriptions_under_management_groups("token", [])


@patch("clients.resource_graph.run_graph_query")
def test_management_group_resolution_rejects_unknown_id(run_graph_query):
    run_graph_query.return_value = [{"name": "known"}]

    with pytest.raises(ValueError, match="Unknown management group ID.*missing"):
        get_subscriptions_under_management_groups("token", ["missing"])

    assert run_graph_query.call_count == 1


@patch("clients.resource_graph.run_graph_query")
def test_management_group_resolution_normalizes_deduplicates_and_queries(run_graph_query):
    run_graph_query.side_effect = [
        [{"name": "Team-A"}],
        [{"subscriptionId": "sub-b"}, {"subscriptionId": "sub-a"}, {"subscriptionId": "sub-a"}],
    ]

    result = get_subscriptions_under_management_groups(
        "token",
        ["/providers/Microsoft.Management/managementGroups/TEAM-A/", "team-a"],
    )

    assert result == ["sub-a", "sub-b"]
    assert "where mgId in ('team-a')" in run_graph_query.call_args_list[1].args[0]