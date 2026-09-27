from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from functions import _authorization as authorization
from functions.allocation_events_bp import (
    allocation_event_timeline,
    allocation_event_vms,
)


def _request(body):
    return SimpleNamespace(get_json=lambda: body)


@patch("functions.allocation_events_bp.authorization.require_user")
@patch("functions.allocation_events_bp.allocation_events.list_visible_vms")
@patch("functions.allocation_events_bp.identity.for_user_action")
def test_vm_list_passes_user_identity_and_filters_to_service(
    for_user_action,
    list_visible_vms,
    _authorization,
):
    who = SimpleNamespace(source="test")
    for_user_action.return_value = who
    list_visible_vms.return_value = {
        "count": 1,
        "metadata": {"schemaVersion": 1},
        "value": [{"id": "vm-1"}],
    }

    response = allocation_event_vms(_request({
        "subscriptionIds": ["SUB-B"],
        "locations": ["EastUS"],
        "contextFilters": [{"fieldKey": "service", "values": ["payments"]}],
    }))

    assert response.status_code == 200
    assert json.loads(response.get_body())["value"] == [{"id": "vm-1"}]
    list_visible_vms.assert_called_once_with(
        who,
        subscription_ids=["SUB-B"],
        locations=["EastUS"],
        context_filters=[{"fieldKey": "service", "values": ["payments"]}],
    )


@patch("functions.allocation_events_bp.authorization.require_user")
@patch("functions.allocation_events_bp.allocation_events.list_visible_vms")
@patch("functions.allocation_events_bp.identity.for_user_action")
def test_service_error_fails_closed(
    for_user_action,
    list_visible_vms,
    _authorization,
):
    for_user_action.return_value = SimpleNamespace(source="test")
    list_visible_vms.side_effect = RuntimeError("graph unavailable")

    response = allocation_event_vms(_request({}))

    assert response.status_code == 502
    assert json.loads(response.get_body())["error"] == "query_failed"


@patch("functions.allocation_events_bp.authorization.require_user")
def test_invalid_filter_returns_400(_authorization):
    response = allocation_event_vms(_request({"locations": [""]}))

    assert response.status_code == 400
    assert json.loads(response.get_body())["error"] == "invalid_filter"


@patch("functions.allocation_events_bp.authorization.require_user")
@patch("functions.allocation_events_bp.allocation_events.get_vm_timeline")
@patch("functions.allocation_events_bp.identity.for_user_action")
def test_timeline_requires_user_and_passes_normalized_resource_id(
    for_user_action,
    get_vm_timeline,
    require_user,
):
    who = SimpleNamespace(source="test")
    for_user_action.return_value = who
    get_vm_timeline.return_value = {
        "metadata": {"schemaVersion": 1},
        "coverage": {},
        "recipes": {},
        "vm": {},
        "events": [],
    }

    response = allocation_event_timeline(_request({
        "resourceId": (
            "/subscriptions/SUB-A/resourceGroups/RG/providers/"
            "Microsoft.Compute/virtualMachines/VM-1/"
        ),
    }))

    assert response.status_code == 200
    require_user.assert_called_once()
    get_vm_timeline.assert_called_once_with(
        who,
        "/subscriptions/sub-a/resourcegroups/rg/providers/"
        "microsoft.compute/virtualmachines/vm-1",
    )


@patch("functions.allocation_events_bp.authorization.require_user")
@patch("functions.allocation_events_bp.identity.for_user_action")
def test_timeline_invalid_resource_id_returns_400_without_identity(
    for_user_action,
    _authorization,
):
    response = allocation_event_timeline(_request({"resourceId": "/subscriptions/sub-a"}))

    assert response.status_code == 400
    assert json.loads(response.get_body())["error"] == "invalid_request"
    for_user_action.assert_not_called()


@patch("functions.allocation_events_bp.authorization.require_user")
@patch("functions.allocation_events_bp.allocation_events.get_vm_timeline")
@patch("functions.allocation_events_bp.identity.for_user_action")
def test_timeline_invisible_vm_returns_generic_404(
    for_user_action,
    get_vm_timeline,
    _authorization,
):
    from services.allocation_events import AllocationEventVmNotFound

    for_user_action.return_value = SimpleNamespace(source="test")
    get_vm_timeline.side_effect = AllocationEventVmNotFound()

    response = allocation_event_timeline(_request({
        "resourceId": (
            "/subscriptions/sub-a/resourceGroups/rg/providers/"
            "Microsoft.Compute/virtualMachines/vm"
        ),
    }))

    assert response.status_code == 404
    assert json.loads(response.get_body())["error"] == "not_found"


@patch(
    "functions.allocation_events_bp.authorization.require_user",
    side_effect=authorization.AuthorizationError(403, "forbidden", "User role required."),
)
def test_timeline_authorization_failure_short_circuits(_authorization):
    response = allocation_event_timeline(_request({}))

    assert response.status_code == 403
    assert json.loads(response.get_body())["error"] == "forbidden"
