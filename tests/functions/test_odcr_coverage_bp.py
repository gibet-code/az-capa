from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import functions.odcr_coverage_bp as coverage_bp
from core.app_scope import ConfiguredAppScope, MODE_ALL, ResolvedAppScope
from functions.odcr_coverage_bp import subscription_inventory, vm_list


class _TestDimensions:
    def __init__(self):
        self.fields = {}

    def filter_subscriptions(self, app_ids, requested=None, context_filters=None):
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

    def enrich_rows(self, rows):
        self.fields = coverage_bp.enrich_rows_with_business_context(rows)
        return rows

    def metadata(self):
        return {
            "referenceData": {},
            "businessContext": {"generation": None, "publishedAt": None, "fields": self.fields},
        }


class VmEndpointTests(unittest.TestCase):
    def setUp(self):
        self.authorization_patch = patch("functions.odcr_coverage_bp.authorization.require_user")
        self.authorization_patch.start()
        self.addCleanup(self.authorization_patch.stop)
        self.context_enrichment_patch = patch(
            "functions.odcr_coverage_bp.enrich_rows_with_business_context",
            return_value={},
        )
        self.context_enrichment_patch.start()
        self.addCleanup(self.context_enrichment_patch.stop)
        self.dimensions_patch = patch(
            "functions.odcr_coverage_bp.request_dimensions.open_current_view",
            side_effect=lambda _capabilities: _TestDimensions(),
        )
        self.dimensions_patch.start()
        self.addCleanup(self.dimensions_patch.stop)

    def _scope(self, subscriptions=("sub-a", "sub-b"), locations=()):
        return ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), locations), subscriptions)

    @staticmethod
    def _vm_query(run_graph_query):
        return next(
            call.args[0]
            for call in run_graph_query.call_args_list
            if "microsoft.compute/virtualmachines" in call.args[0]
        )

    @patch("functions.odcr_coverage_bp.get_subscription_inventory")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_subscription_inventory_is_user_visible_and_app_scoped(
        self, for_user_action, _backend, resolve_scope, get_inventory
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "user-token")
        resolve_scope.return_value = self._scope(("sub-a",))
        get_inventory.return_value = [
            {"id": "sub-a", "name": "A", "state": "Enabled", "managementGroupAncestors": []},
            {"id": "sub-b", "name": "B", "state": "Enabled", "managementGroupAncestors": []},
        ]

        response = subscription_inventory(SimpleNamespace())

        self.assertEqual(200, response.status_code)
        self.assertEqual(["sub-a"], [item["id"] for item in json.loads(response.get_body())["value"]])
        get_inventory.assert_called_once_with("user-token")

    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.odcr_coverage_decisions.get_for_subscriptions", return_value={})
    @patch("functions.odcr_coverage_bp.run_graph_query", return_value=[])
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_post_body_applies_only_subscription_scope(
        self, for_user_action, run_graph_query, _coverage, _backend, resolve_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_scope.return_value = self._scope()
        request = SimpleNamespace(
            method="POST",
            get_json=lambda: {"subscriptionIds": ["sub-b"]},
            url="http://localhost/api/odcr/coverage?location=eastus",
        )

        response = vm_list(request)

        self.assertEqual(200, response.status_code)
        query = self._vm_query(run_graph_query)
        self.assertNotIn("subscriptionId in~", query)
        self.assertNotIn("location in~", query)
        vm_call = next(
            call for call in run_graph_query.call_args_list
            if "microsoft.compute/virtualmachines" in call.args[0]
        )
        self.assertEqual(("sub-b",), vm_call.kwargs["subscription_ids"])

    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_post_explicit_empty_subscription_scope_fails_closed(
        self, for_user_action, run_graph_query, _backend, resolve_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_scope.return_value = self._scope()
        request = SimpleNamespace(
            method="POST",
            get_json=lambda: {"subscriptionIds": []},
            url="http://localhost/api/odcr/coverage",
        )

        response = vm_list(request)

        self.assertEqual(200, response.status_code)
        self.assertEqual([], json.loads(response.get_body())["value"])
        run_graph_query.assert_not_called()

    @patch("functions.odcr_coverage_bp.odcr_coverage_decisions.get_for_subscriptions", side_effect=RuntimeError("table unavailable"))
    @patch("functions.odcr_coverage_bp.compute_skus.pipeline.enrich_vm_rows", side_effect=lambda rows: rows)
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_coverage_storage_error_fails_list_closed(
        self, for_user_action, run_graph_query, _backend, resolve_scope, _enrich, _coverage
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_scope.return_value = self._scope()
        run_graph_query.return_value = [{
            "id": "/subscriptions/sub-a/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm-1",
            "subscriptionId": "sub-a",
        }]

        response = vm_list(SimpleNamespace(url="http://localhost/api/odcr/coverage"))

        self.assertEqual(502, response.status_code)
        self.assertEqual("query_failed", json.loads(response.get_body())["error"])

    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.odcr_coverage_decisions.get_for_subscriptions")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    @patch("functions.odcr_coverage_bp.compute_skus.pipeline.enrich_vm_rows")
    def test_repeated_parameters_reach_query_builder(
        self, enrich_skus, for_user_action, run_graph_query, get_coverage,
        _for_backend_operation, resolve_app_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_app_scope.return_value = self._scope()
        get_coverage.return_value = {}
        run_graph_query.return_value = [{"id": "/vm/1"}]
        enrich_skus.side_effect = lambda rows: [
            {**row, "capacityReservationSupported": True} for row in rows
        ]
        request = SimpleNamespace(
            url=(
                "http://localhost/api/odcr/coverage?subscriptionId=sub-a&subscriptionId=sub-b"
                "&deploymentType=Regional&deploymentType=Zone+2&vmssFlexible=false"
            )
        )

        response = vm_list(request)

        self.assertEqual(200, response.status_code)
        body = json.loads(response.get_body())
        self.assertEqual(1, body["count"])
        requirement_metadata = body["metadata"]["odcrRequirements"]
        self.assertEqual("Passed", requirement_metadata["defaultStatus"])
        self.assertEqual(8, len(requirement_metadata["definitions"]))
        self.assertEqual("/vm/1", body["value"][0]["id"])
        self.assertEqual("Not associated", body["value"][0]["reservationStatus"])
        self.assertEqual(
            [{"key": "vmssPlacementModel", "status": "Not applicable"}],
            body["value"][0]["odcrRequirements"],
        )
        self.assertNotIn("odcrUnsupportedRequirementCodes", body["value"][0])
        self.assertNotIn("odcrUnsupportedReasons", body["value"][0])
        self.assertNotIn("matchingCapacityReservationIds", body["value"][0])
        self.assertEqual("Supported", body["value"][0]["odcrSupportabilityStatus"])
        self.assertNotIn("capacityReservationSupported", body["value"][0])
        self.assertIsNone(body["value"][0]["odcrCoverage"])
        query = self._vm_query(run_graph_query)
        self.assertNotIn("subscriptionId in~", query)
        self.assertIn("deploymentType in~ ('Regional', 'Zone 2')", query)
        self.assertIn("isVmssFlexible in (false)", query)
        vm_call = next(
            call for call in run_graph_query.call_args_list
            if "microsoft.compute/virtualmachines" in call.args[0]
        )
        self.assertEqual(("sub-a", "sub-b"), vm_call.kwargs["subscription_ids"])

    @patch("functions.odcr_coverage_bp.compute_skus.pipeline.enrich_vm_rows")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_sku_lookup_error_returns_vm_with_unknown_supportability(
        self, for_user_action, run_graph_query, _for_backend_operation, resolve_app_scope, enrich_skus
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_app_scope.return_value = self._scope()
        run_graph_query.return_value = [{
            "id": "/vm/1",
            "vmSize": "Standard_D2s_v5",
            "location": "eastus",
        }]
        enrich_skus.side_effect = lambda rows: [
            {
                **row,
                "capacityReservationSupported": None,
                "capacityReservationSupportReason": "Compute SKU catalog lookup failed: unavailable",
            }
            for row in rows
        ]
        request = SimpleNamespace(url="http://localhost/api/odcr/coverage")

        response = vm_list(request)
        vm = json.loads(response.get_body())["value"][0]

        self.assertEqual(200, response.status_code)
        self.assertEqual("Not associated", vm["reservationStatus"])
        self.assertEqual("Unknown", vm["odcrSupportabilityStatus"])
        self.assertNotIn("reservationLifecycleStatus", vm)
        self.assertNotIn("odcrUnknownRequirementCodes", vm)
        sku_requirement = next(
            item for item in vm["odcrRequirements"] if item["key"] == "vmSkuSupport"
        )
        self.assertEqual("Unknown", sku_requirement["status"])
        self.assertEqual("Compute SKU catalog lookup failed: unavailable", sku_requirement["reason"])

    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_blank_parameter_is_unrestricted(
        self, for_user_action, run_graph_query, _for_backend_operation, resolve_app_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_app_scope.return_value = self._scope()
        run_graph_query.return_value = []
        request = SimpleNamespace(url="http://localhost/api/odcr/coverage?location=")

        response = vm_list(request)

        self.assertEqual(200, response.status_code)
        query = self._vm_query(run_graph_query)
        self.assertNotIn("resourceGroup in~", query)
        self.assertNotIn("| where false", query)

    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_invalid_enum_returns_400_without_querying_graph(
        self, for_user_action, run_graph_query, for_backend_operation, resolve_app_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        request = SimpleNamespace(url="http://localhost/api/odcr/coverage?deploymentType=Zonal+1")

        response = vm_list(request)

        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_filter", json.loads(response.get_body())["error"])
        run_graph_query.assert_not_called()
        for_backend_operation.assert_not_called()
        resolve_app_scope.assert_not_called()

    @patch("functions.odcr_coverage_bp.enrich_vm_reservation_lifecycle")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_request_filters_are_intersected_with_global_scope(
        self, for_user_action, _for_backend_operation, resolve_app_scope, run_graph_query, enrich
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "user-token", source="test")
        resolve_app_scope.return_value = self._scope(("sub-a", "sub-b"), ("eastus", "westeurope"))
        run_graph_query.return_value = []
        enrich.return_value = []
        request = SimpleNamespace(
            url="http://localhost/api/odcr/coverage?subscriptionId=sub-b&subscriptionId=sub-outside&location=WESTEUROPE"
        )

        response = vm_list(request)

        self.assertEqual(200, response.status_code)
        query = self._vm_query(run_graph_query)
        self.assertNotIn("subscriptionId in~", query)
        self.assertNotIn("sub-outside", query)
        self.assertIn("location in~ ('westeurope')", query)
        vm_call = next(
            call for call in run_graph_query.call_args_list
            if "microsoft.compute/virtualmachines" in call.args[0]
        )
        self.assertEqual(("sub-b",), vm_call.kwargs["subscription_ids"])

    @patch("functions.odcr_coverage_bp.enrich_rows_with_business_context")
    @patch("functions.odcr_coverage_bp.odcr_coverage_decisions.get_for_subscriptions", return_value={})
    @patch("functions.odcr_coverage_bp.compute_skus.pipeline.enrich_vm_rows", side_effect=lambda rows: rows)
    @patch("functions.odcr_coverage_bp.enrich_vm_reservation_lifecycle")
    @patch("functions.odcr_coverage_bp.zone_mapping.pipeline.get_mappings")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_split_queries_reconcile_physical_zone_before_arm_enrichment(
        self, for_user_action, _backend, resolve_scope, run_graph_query, get_mappings,
        enrich_reservations, _enrich_skus, _coverage, enrich_context
    ):
        group_id = "/subscriptions/cr-sub/resourcegroups/rg/providers/microsoft.compute/capacityreservationgroups/group"
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "user-token", source="test")
        resolve_scope.return_value = self._scope(("vm-sub",), ("eastus",))

        def graph_result(query, _token, **_kwargs):
            if "microsoft.compute/virtualmachines" in query:
                return [{
                    "id": "/vm/1",
                    "subscriptionId": "vm-sub",
                    "location": "eastus",
                    "logicalZone": "1",
                    "vmSize": "Standard_D2s_v5",
                    "capacityReservationGroupId": group_id,
                }]
            return [
                {
                    "capacityReservationGroupId": group_id,
                    "capacityReservationGroupName": "group",
                    "matchingCapacityReservationId": "/reservations/wrong-zone",
                    "reservationSubscriptionId": "cr-sub",
                    "reservationLocation": "eastus",
                    "reservationZone": "1",
                    "reservationVmSize": "Standard_D2s_v5",
                },
                {
                    "capacityReservationGroupId": group_id,
                    "capacityReservationGroupName": "group",
                    "matchingCapacityReservationId": "/reservations/right-zone",
                    "reservationSubscriptionId": "cr-sub",
                    "reservationLocation": "eastus",
                    "reservationZone": "2",
                    "reservationVmSize": "Standard_D2s_v5",
                },
            ]

        run_graph_query.side_effect = graph_result
        get_mappings.return_value = {
            ("vm-sub", "eastus"): {"1": "eastus-az2"},
            ("cr-sub", "eastus"): {"1": "eastus-az1", "2": "eastus-az2"},
        }

        def enrich(rows, _token):
            self.assertEqual(["/reservations/right-zone"], rows[0]["matchingCapacityReservationIds"])
            rows[0]["reservationStatus"] = "Associated, capacity available"
            return rows

        enrich_reservations.side_effect = enrich

        def add_context(context_rows):
            for context_row in context_rows:
                context_row["businessContext"] = {"environment": None}
            return {"environment": "Environment"}

        enrich_context.side_effect = add_context

        response = vm_list(SimpleNamespace(url="http://localhost/api/odcr/coverage"))

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, run_graph_query.call_count)
        self.assertTrue(all(call.args[1] == "user-token" for call in run_graph_query.call_args_list))
        vm_call = next(
            call for call in run_graph_query.call_args_list
            if "microsoft.compute/virtualmachines" in call.args[0]
        )
        reservation_call = next(
            call for call in run_graph_query.call_args_list
            if "capacityreservationgroups/capacityreservations" in call.args[0]
        )
        self.assertEqual(("vm-sub",), vm_call.kwargs["subscription_ids"])
        self.assertIsNone(reservation_call.kwargs["subscription_ids"])
        get_mappings.assert_called_once_with({("vm-sub", "eastus"), ("cr-sub", "eastus")})
        body = json.loads(response.get_body())
        vm = body["value"][0]
        self.assertEqual("eastus-az2", vm["physicalZone"])
        self.assertEqual("group", vm["capacityReservationGroupName"])
        self.assertEqual({"environment": None}, vm["businessContext"])
        self.assertEqual(
            {"environment": "Environment"},
            body["metadata"]["businessContext"]["fields"],
        )
        self.assertEqual(1, enrich_context.call_count)
        self.assertEqual("vm-sub", enrich_context.call_args.args[0][0]["subscriptionId"])

    @patch("functions.odcr_coverage_bp.enrich_vm_reservation_lifecycle")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_disjoint_global_filter_returns_empty_without_query(
        self, _for_user_action, _for_backend_operation, resolve_app_scope, run_graph_query, enrich
    ):
        resolve_app_scope.return_value = self._scope(("sub-a",), ("eastus",))
        request = SimpleNamespace(url="http://localhost/api/odcr/coverage?subscriptionId=sub-outside")

        response = vm_list(request)

        body = json.loads(response.get_body())
        self.assertEqual(0, body["count"])
        self.assertEqual([], body["value"])
        self.assertEqual("Passed", body["metadata"]["odcrRequirements"]["defaultStatus"])
        run_graph_query.assert_not_called()
        enrich.assert_not_called()

    @patch("functions.odcr_coverage_bp.resolve_app_scope", side_effect=RuntimeError("scope unavailable"))
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_scope_resolution_failure_is_502(self, _for_user_action, _for_backend_operation, _resolve):
        request = SimpleNamespace(url="http://localhost/api/odcr/coverage")

        response = vm_list(request)

        self.assertEqual(502, response.status_code)
        self.assertEqual("scope_resolution_failed", json.loads(response.get_body())["error"])


if __name__ == "__main__":
    unittest.main()