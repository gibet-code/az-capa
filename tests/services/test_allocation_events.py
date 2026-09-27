from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.app_scope import ConfiguredAppScope, MODE_ALL, ResolvedAppScope
from services.activity_log.projection import compress_json
from services.allocation_events import (
    AllocationEventVmNotFound,
    build_vm_visibility_query,
    build_vm_inventory_query,
    get_vm_timeline,
    list_visible_vms,
    project_vm_inventory_row,
    validate_vm_resource_id,
)


class _TestDimensions:
    @staticmethod
    def filter_subscriptions(app_ids, requested=None, context_filters=None):
        allowed = tuple(value.lower() for value in app_ids)
        if requested is None:
            return allowed
        selected = {value.strip().lower() for value in requested if value.strip()}
        return tuple(value for value in allowed if value in selected)

    @staticmethod
    def filter_locations(app_locations, requested=None):
        configured = tuple(value.lower() for value in app_locations)
        if requested is None:
            return configured
        selected = {value.strip().lower() for value in requested if value.strip()}
        return tuple(value for value in configured if value in selected) if configured else tuple(sorted(selected))

    @staticmethod
    def enrich_rows(rows):
        for row in rows:
            row["locationDisplayName"] = "East US"
            row["businessContext"] = {"service": "payments"}
        return rows

    @staticmethod
    def metadata():
        return {
            "referenceData": {},
            "businessContext": {"fields": {"service": "Service"}},
        }


class _LocationDimensions:
    @staticmethod
    def enrich_rows(rows):
        for row in rows:
            row["locationDisplayName"] = "East US"
        return rows


def _scope():
    return ResolvedAppScope(
        ConfiguredAppScope(MODE_ALL, (), ("eastus", "westeurope")),
        ("sub-a", "sub-b"),
    )


def test_vm_inventory_query_projects_report_fields_and_location_filter():
    query = build_vm_inventory_query(["East US", "westeurope"])

    assert "microsoft.compute/virtualmachines" in query
    assert "location in~ ('East US', 'westeurope')" in query
    assert "subscriptionName" in query
    assert "logicalZone" in query
    assert "vmSize" in query
    assert "powerState" in query


def test_vm_inventory_projection_removes_unrelated_graph_fields():
    row = {
        "id": "/subscriptions/sub-a/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm-1",
        "name": "vm-1",
        "businessContext": {"service": "payments"},
        "tags": {"secret": "not returned"},
    }

    projected = project_vm_inventory_row(row)

    assert projected == {
        "id": row["id"],
        "name": "vm-1",
        "businessContext": {"service": "payments"},
    }


@patch("services.allocation_events.request_dimensions.open_current_view", return_value=_TestDimensions())
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.zone_mapping.pipeline.get_mappings")
@patch("services.allocation_events.run_graph_query")
@patch("services.allocation_events.activity_log_store")
def test_visible_vm_list_intersects_scope_and_enriches_rows(
    activity_store,
    run_graph_query,
    get_mappings,
    _backend,
    _resolve,
    _dimensions,
):
    run_graph_query.return_value = [{
        "id": "/subscriptions/sub-b/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm-1",
        "resourceType": "microsoft.compute/virtualmachines",
        "subscriptionId": "sub-b",
        "subscriptionName": "Subscription B",
        "resourceGroup": "rg",
        "name": "vm-1",
        "location": "eastus",
        "logicalZone": "1",
        "vmSize": "Standard_D2s_v5",
        "powerState": "VM running",
        "tags": {"not": "returned"},
    }]
    get_mappings.return_value = {("sub-b", "eastus"): {"1": "eastus-az3"}}
    activity_store.get_coverage.return_value = None
    who = SimpleNamespace(get_token=lambda: "user-token")

    result = list_visible_vms(
        who,
        subscription_ids=["SUB-B"],
        locations=["EastUS"],
        context_filters=[{"fieldKey": "service", "values": ["payments"]}],
    )

    assert result["count"] == 1
    assert result["metadata"]["schemaVersion"] == 2
    assert result["value"][0]["businessContext"] == {"service": "payments"}
    assert result["value"][0]["physicalZone"] == "eastus-az3"
    assert result["value"][0]["allocation"]["periods"]["30d"]["complete"] is False
    assert "tags" not in result["value"][0]
    query, token = run_graph_query.call_args.args
    assert "location in~ ('eastus')" in query
    assert token == "user-token"
    assert run_graph_query.call_args.kwargs["subscription_ids"] == ("sub-b",)
    get_mappings.assert_called_once_with({("sub-b", "eastus")})


@patch("services.allocation_events.request_dimensions.open_current_view", return_value=_TestDimensions())
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.zone_mapping.pipeline.get_mappings", return_value={})
@patch("services.allocation_events.run_graph_query")
@patch("services.allocation_events.activity_log_store")
def test_activity_aggregation_filters_visible_vms_and_reports_all_periods(
    activity_store,
    run_graph_query,
    _mappings,
    _backend,
    _resolve,
    _dimensions,
):
    vm_id = "/subscriptions/SUB-B/resourceGroups/RG/providers/Microsoft.Compute/virtualMachines/VM-1/"
    hidden_id = "/subscriptions/sub-b/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/hidden"
    run_graph_query.return_value = [{
        "id": vm_id,
        "resourceType": "microsoft.compute/virtualmachines",
        "subscriptionId": "sub-b",
        "name": "vm-1",
        "location": "eastus",
    }]
    activity_store.get_coverage.return_value = {
        "coverageStart": "2026-08-24T12:00:00Z",
        "coverageEnd": "2026-09-23T12:00:00Z",
        "enabledRecipeIds": ["vm-lifecycle", "vm-configuration"],
        "materializedRecipeVersions": {
            "vm-lifecycle": 3,
            "vm-configuration": 3,
        },
    }
    activity_store.query_operations_window.return_value = [
        {
            "ResourceId": vm_id.upper(),
            "OperationName": "microsoft.compute/virtualmachines/start/action",
            "Outcome": "Succeeded",
            "LastEventTime": "2026-09-22T12:00:00Z",
        },
        {
            "ResourceId": vm_id,
            "OperationName": "microsoft.compute/virtualmachines/restart/action",
            "Outcome": "FAILED",
            "LastEventTime": "2026-09-23T12:00:00Z",
            "ErrorCode": "allocationfailed",
            "ErrorMessage": "capacity unavailable",
            "HasAllocationFailure": True,
        },
        {
            "ResourceId": vm_id,
            "OperationName": "microsoft.compute/virtualmachines/reapply/action",
            "Outcome": "failed",
            "LastEventTime": "2026-09-20T12:00:00Z",
            "ErrorCode": "OtherFailure",
            "HasAllocationFailure": False,
        },
        {
            "ResourceId": vm_id,
            "OperationName": "Microsoft.Compute/virtualMachines/Write",
            "Outcome": "succeeded",
            "LastEventTime": "2026-09-23T10:00:00Z",
            "ProvisioningState": "Creating",
            "HasAllocationFailure": False,
        },
        {
            "ResourceId": vm_id,
            "OperationName": "Microsoft.Compute/virtualMachines/Write",
            "Outcome": "failed",
            "LastEventTime": "2026-09-23T10:30:00Z",
            "ProvisioningState": "Creating",
            "ErrorCode": "ResourceOperationFailure",
            "ErrorDetailCode": "ZonalAllocationFailed",
            "ErrorDetailMessage": "No zonal capacity",
            "HasAllocationFailure": True,
        },
        {
            "ResourceId": vm_id,
            "OperationName": "Microsoft.Compute/virtualMachines/Write",
            "Outcome": "failed",
            "LastEventTime": "2026-09-23T10:45:00Z",
            "ProvisioningState": "Updating",
            "ErrorDetailCode": "AllocationFailed",
            "HasAllocationFailure": True,
        },
        {
            "ResourceId": vm_id,
            "OperationName": "microsoft.compute/virtualmachines/deallocate/action",
            "Outcome": "failed",
            "LastEventTime": "2026-09-23T11:00:00Z",
            "ErrorCode": "AllocationFailed",
        },
        {
            "ResourceId": vm_id,
            "OperationName": "microsoft.compute/virtualmachines/redeploy/action",
            "Outcome": "pending",
            "LastEventTime": "2026-09-23T11:30:00Z",
        },
        {
            "ResourceId": hidden_id,
            "OperationName": "microsoft.compute/virtualmachines/start/action",
            "Outcome": "failed",
            "LastEventTime": "2026-09-23T12:00:00Z",
            "ErrorCode": "AllocationFailed",
        },
    ]

    result = list_visible_vms(SimpleNamespace(get_token=lambda: "token"))

    vm = result["value"][0]
    assert vm["id"] == vm_id.rstrip("/").lower()
    assert set(vm["allocation"]["periods"]) == {"1d", "7d", "30d"}
    assert vm["allocation"]["periods"]["1d"] == {
        "start": "2026-09-22T12:00:00Z",
        "end": "2026-09-23T12:00:00Z",
        "complete": True,
        "succeeded": 2,
        "failed": 2,
        "confirmedAllocationFailures": 2,
        "succeededLifecycle": 1,
        "succeededCreates": 1,
        "failedLifecycle": 1,
        "failedCreates": 1,
        "confirmedLifecycleAllocationFailures": 1,
        "confirmedCreateAllocationFailures": 1,
    }
    assert vm["allocation"]["periods"]["7d"]["failed"] == 3
    assert vm["allocation"]["periods"]["30d"]["complete"] is True
    assert vm["allocation"]["lastAllocation"]["action"] == "Restart"
    assert vm["allocation"]["lastFailure"]["errorCode"] == "allocationfailed"
    assert set(vm["allocation"]["lastAllocation"]) == {"timestamp", "outcome", "action"}
    assert set(vm["allocation"]["lastFailure"]) == {
        "timestamp", "action", "errorCode", "errorMessage"
    }
    assert result["metadata"]["activityLog"] == {
        "periods": ["1d", "7d", "30d"],
        "defaultPeriod": "30d",
        "confirmedFailureCodes": ["AllocationFailed", "ZonalAllocationFailed"],
        "coverageStart": "2026-08-24T12:00:00Z",
        "coverageEnd": "2026-09-23T12:00:00Z",
    }
    args = activity_store.query_operations_window.call_args
    assert args.args[1:3] == ("2026-08-24T12:00:00Z", "2026-09-23T12:00:00Z")
    assert args.args[3] is None
    assert args.kwargs["select"] == (
        "ResourceId", "OperationName", "Outcome", "LastEventTime",
        "ErrorCode", "ErrorMessage", "ErrorDetailCode", "ErrorDetailMessage",
        "ErrorDetailCount", "ErrorCodesJson", "HasAllocationFailure",
        "ProvisioningState",
    )


@patch("services.allocation_events.request_dimensions.open_current_view", return_value=_TestDimensions())
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.zone_mapping.pipeline.get_mappings", return_value={})
@patch("services.allocation_events.run_graph_query")
@patch("services.allocation_events.activity_log_store")
def test_period_completeness_requires_start_coverage_and_current_recipe(
    activity_store,
    run_graph_query,
    _mappings,
    _backend,
    _resolve,
    _dimensions,
):
    run_graph_query.return_value = [{
        "id": "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
        "subscriptionId": "sub-a",
        "name": "vm",
        "location": "eastus",
    }]
    activity_store.get_coverage.return_value = {
        "coverageStart": "2026-09-16T12:00:01Z",
        "coverageEnd": "2026-09-23T12:00:00Z",
        "enabledRecipeIds": ["vm-lifecycle", "vm-configuration"],
        "materializedRecipeVersions": {
            "vm-lifecycle": 2,
            "vm-configuration": 2,
        },
    }
    activity_store.query_operations_window.return_value = []

    result = list_visible_vms(SimpleNamespace(get_token=lambda: "token"))

    periods = result["value"][0]["allocation"]["periods"]
    assert periods["1d"]["complete"] is False
    assert periods["7d"]["complete"] is False
    assert periods["30d"]["complete"] is False
    assert result["metadata"]["activityLog"]["coverageStart"] == "2026-09-16T12:00:01Z"


@patch("services.allocation_events.request_dimensions.open_current_view", return_value=_TestDimensions())
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.run_graph_query")
def test_explicit_empty_subscription_scope_returns_empty_without_graph_query(
    run_graph_query,
    _backend,
    _resolve,
    _dimensions,
):
    result = list_visible_vms(
        SimpleNamespace(get_token=lambda: "user-token"),
        subscription_ids=[],
    )

    assert result["value"] == []
    run_graph_query.assert_not_called()


def test_timeline_resource_id_validation_and_narrow_query():
    resource_id, subscription_id = validate_vm_resource_id(
        " /subscriptions/SUB-A/resourceGroups/RG/providers/"
        "Microsoft.Compute/virtualMachines/VM-1/ "
    )

    assert resource_id.endswith("/virtualmachines/vm-1")
    assert subscription_id == "sub-a"
    query = build_vm_visibility_query(resource_id)
    assert f"id =~ '{resource_id}'" in query
    assert "microsoft.compute/virtualmachines" in query


@patch("services.allocation_events.activity_log_store")
@patch("services.allocation_events.run_graph_query")
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
def test_timeline_rejects_out_of_scope_before_graph_or_storage(
    _resolve,
    _backend,
    run_graph_query,
    activity_store,
):
    with pytest.raises(AllocationEventVmNotFound):
        get_vm_timeline(
            SimpleNamespace(get_token=lambda: "token"),
            "/subscriptions/sub-x/resourceGroups/rg/providers/"
            "Microsoft.Compute/virtualMachines/vm",
        )

    run_graph_query.assert_not_called()
    activity_store.get_coverage.assert_not_called()


@patch("services.allocation_events.activity_log_store")
@patch("services.allocation_events.run_graph_query", return_value=[])
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
def test_timeline_rejects_invisible_vm_before_storage(
    _resolve,
    _backend,
    _graph,
    activity_store,
):
    with pytest.raises(AllocationEventVmNotFound):
        get_vm_timeline(
            SimpleNamespace(get_token=lambda: "token"),
            "/subscriptions/sub-a/resourceGroups/rg/providers/"
            "Microsoft.Compute/virtualMachines/vm",
        )

    activity_store.get_coverage.assert_not_called()


@patch(
    "services.allocation_events._utc_now",
    return_value=datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc),
)
@patch(
    "services.allocation_events.zone_mapping.pipeline.get_mappings",
    return_value={("sub-a", "eastus"): {"1": "2"}},
)
@patch(
    "services.allocation_events.request_dimensions.open_current_view",
    return_value=_LocationDimensions(),
)
@patch("services.allocation_events.activity_log_store")
@patch("services.allocation_events.run_graph_query")
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
def test_timeline_projects_full_coverage_history_newest_first(
    _resolve,
    _backend,
    run_graph_query,
    activity_store,
    _dimensions,
    get_mappings,
    _now,
):
    vm_id = (
        "/subscriptions/sub-a/resourcegroups/rg/providers/"
        "microsoft.compute/virtualmachines/vm-1"
    )
    run_graph_query.return_value = [{
        "id": vm_id.upper(),
        "subscriptionId": "sub-a",
        "resourceGroup": "rg",
        "name": "vm-1",
        "location": "eastus",
        "logicalZone": "1",
        "vmSize": "Standard_D2s_v5",
        "powerState": "VM running",
        "provisioningState": "Succeeded",
        "capacityReservationGroupId": "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/crg",
        "secret": "not returned",
    }]
    activity_store.get_coverage.return_value = {
        "coverageStart": "2026-06-01T00:00:00Z",
        "coverageEnd": "2026-09-23T12:00:00Z",
        "enabledRecipeIds": ["vm-lifecycle", "vm-configuration"],
        "materializedRecipeVersions": {
            "vm-lifecycle": 3,
            "vm-configuration": 3,
        },
    }
    activity_store.query_operations_window.return_value = [
        {
            "OperationIdentityHash": "older-write",
            "ResourceId": vm_id,
            "OperationName": "Microsoft.Compute/virtualMachines/Write",
            "Outcome": "Succeeded",
            "LastEventTime": "2026-06-02T00:00:00Z",
            "VmSize": "Standard_D2s_v5",
            "Location": "eastus",
            "Zones": '["1"]',
            "ProvisioningState": None,
            "AppliedPayloadGz": b"must not escape",
        },
        {
            "OperationIdentityHash": "newer-start",
            "ResourceId": vm_id.upper(),
            "OperationName": "Microsoft.Compute/virtualMachines/Start/action",
            "Outcome": "FAILED",
            "LastEventTime": "2026-09-22T00:00:00Z",
            "ErrorCode": "ResourceOperationFailure",
            "ErrorMessage": "The operation failed.",
            "ErrorDetailCode": "AllocationFailed",
            "ErrorDetailMessage": "No capacity",
            "ErrorDetailCount": 1,
            "ErrorCodesJson": '["ResourceOperationFailure","AllocationFailed"]',
            "HasAllocationFailure": True,
            "ErrorDetailsGz": b"compressed",
        },
        {
            "OperationIdentityHash": "foreign",
            "ResourceId": vm_id + "-other",
            "OperationName": "microsoft.compute/virtualmachines/delete",
            "Outcome": "succeeded",
            "LastEventTime": "2026-09-23T00:00:00Z",
        },
    ]
    activity_store.load_error_details.return_value = compress_json([{
        "code": "AllocationFailed",
        "message": "No capacity",
    }])

    result = get_vm_timeline(
        SimpleNamespace(get_token=lambda: "user-token"), vm_id
    )

    query, token = run_graph_query.call_args.args
    assert f"id =~ '{vm_id}'" in query
    assert token == "user-token"
    assert run_graph_query.call_args.kwargs["subscription_ids"] == ("sub-a",)
    assert result["metadata"] == {"schemaVersion": 1}
    assert result["coverage"] == {
        "start": "2026-06-01T00:00:00Z",
        "end": "2026-09-23T12:00:00Z",
        "complete": True,
        "stale": False,
    }
    assert result["recipes"] == {
        "vm-lifecycle": {"version": 3, "current": True},
        "vm-configuration": {"version": 3, "current": True},
    }
    assert result["vm"]["id"] == vm_id
    assert "secret" not in result["vm"]
    assert "resourceType" in build_vm_visibility_query(vm_id)
    assert "subscriptionName" in build_vm_visibility_query(vm_id)
    assert [event["id"] for event in result["events"]] == [
        "current-state", "newer-start", "older-write"
    ]
    assert result["events"][0] == {
        "id": "current-state",
        "recipe": "current-state",
        "type": "current",
        "action": "Current configuration",
        "timestamp": "2026-09-23T12:30:00Z",
        "outcome": "current",
        "details": {
            "vmSize": "Standard_D2s_v5",
            "location": "eastus",
            "locationDisplayName": "East US",
            "zones": ["1"],
            "physicalZones": ["2"],
            "powerState": "VM running",
            "provisioningState": "Succeeded",
            "capacityReservationGroupId": "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/crg",
        },
    }
    assert result["events"][1] == {
        "id": "newer-start",
        "recipe": "vm-lifecycle",
        "type": "lifecycle",
        "action": "Start",
        "timestamp": "2026-09-22T00:00:00Z",
        "outcome": "failed",
        "details": {},
        "error": {
            "code": "ResourceOperationFailure",
            "message": "The operation failed.",
            "detailCode": "AllocationFailed",
            "detailMessage": "No capacity",
            "detailCount": 1,
            "codes": ["ResourceOperationFailure", "AllocationFailed"],
            "hasAllocationFailure": True,
            "details": [{
                "code": "AllocationFailed",
                "message": "No capacity",
            }],
        },
    }
    assert result["events"][2]["details"] == {
        "vmSize": "Standard_D2s_v5",
        "location": "eastus",
        "locationDisplayName": "East US",
        "zones": ["1"],
        "physicalZones": ["2"],
    }
    get_mappings.assert_called_once_with({("sub-a", "eastus")})
    query_call = activity_store.query_operations_window.call_args
    assert query_call.args[1:3] == (
        "2026-06-01T00:00:00Z", "2026-09-23T12:00:00Z"
    )
    assert query_call.args[3] is None
    assert "AppliedPayloadGz" not in query_call.kwargs["select"]
    assert "AppliedPayloadSpillBlob" not in query_call.kwargs["select"]
    assert "ErrorDetailsGz" in query_call.kwargs["select"]
    assert "ErrorDetailsSpillBlob" in query_call.kwargs["select"]


@patch(
    "services.allocation_events._utc_now",
    return_value=datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc),
)
@patch(
    "services.allocation_events.request_dimensions.open_current_view",
    return_value=_LocationDimensions(),
)
@patch("services.allocation_events.activity_log_store")
@patch("services.allocation_events.run_graph_query")
@patch("services.allocation_events.identity.for_backend_operation")
@patch("services.allocation_events.resolve_app_scope", return_value=_scope())
def test_timeline_marks_incomplete_stale_recipe_coverage(
    _resolve,
    _backend,
    run_graph_query,
    activity_store,
    _dimensions,
    _now,
):
    vm_id = (
        "/subscriptions/sub-a/resourcegroups/rg/providers/"
        "microsoft.compute/virtualmachines/vm"
    )
    run_graph_query.return_value = [{"id": vm_id}]
    activity_store.get_coverage.return_value = {
        "coverageStart": "2026-06-01T00:00:00Z",
        "coverageEnd": "2026-09-01T00:00:00Z",
        "enabledRecipeIds": ["vm-lifecycle", "vm-configuration"],
        "materializedRecipeVersions": {
            "vm-lifecycle": 1,
            "vm-configuration": 3,
        },
    }
    activity_store.query_operations_window.return_value = []

    result = get_vm_timeline(SimpleNamespace(get_token=lambda: "token"), vm_id)

    assert result["coverage"]["complete"] is False
    assert result["coverage"]["stale"] is True
    assert result["recipes"]["vm-lifecycle"]["current"] is False
    assert result["recipes"]["vm-configuration"]["current"] is True
