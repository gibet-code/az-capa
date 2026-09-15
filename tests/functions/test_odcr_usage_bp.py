from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import functions.odcr_usage_bp as usage_bp
from core.app_scope import ConfiguredAppScope, MODE_ALL, ResolvedAppScope
from functions.odcr_usage_bp import odcr_usage_list
from services.app_scope import AppScopeEmptyError, AppScopeInitializingError


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
        self.fields = usage_bp.enrich_rows_with_business_context(rows)
        return rows

    def metadata(self):
        return {
            "referenceData": {},
            "businessContext": {"generation": None, "publishedAt": None, "fields": self.fields},
        }


class OdcrUsageEndpointTests(unittest.TestCase):
    def setUp(self):
        self.authorization_patch = patch("functions.odcr_usage_bp.authorization.require_user")
        self.authorization_patch.start()
        self.addCleanup(self.authorization_patch.stop)
        self.dimensions_patch = patch(
            "functions.odcr_usage_bp.request_dimensions.open_current_view",
            side_effect=lambda _capabilities: _TestDimensions(),
        )
        self.dimensions_patch.start()
        self.addCleanup(self.dimensions_patch.stop)

    @staticmethod
    def _scope(subscriptions=("sub-a", "sub-b"), locations=()):
        return ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), locations), subscriptions)

    @patch("functions.odcr_usage_bp.enrich_rows_with_business_context")
    @patch("functions.odcr_usage_bp.zone_mapping.pipeline.get_mappings")
    @patch("functions.odcr_usage_bp.cr_usage.empty_usage")
    @patch("functions.odcr_usage_bp.cr_usage.usage_view")
    @patch("functions.odcr_usage_bp.get_accessible_subscriptions")
    @patch("functions.odcr_usage_bp.run_graph_query")
    @patch("functions.odcr_usage_bp.resolve_app_scope")
    @patch("functions.odcr_usage_bp.identity.for_backend_operation")
    @patch("functions.odcr_usage_bp.identity.for_user_action")
    def test_list_uses_user_graph_token_and_intersects_global_scope(
        self, for_user_action, _backend, resolve_scope, run_graph_query,
        get_accessible_subscriptions, usage_view, empty_usage, get_mappings, enrich_context
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "user-token", source="test")
        resolve_scope.return_value = self._scope(locations=("eastus",))
        group_id = "/subscriptions/sub-b/resourcegroups/rg/providers/microsoft.compute/capacityreservationgroups/group-a"
        reservation_id = f"{group_id}/capacityreservations/cr-1"

        def graph_result(query, _token, **_kwargs):
            if "capacityreservations'" in query:
                return [{
                    "id": reservation_id,
                    "capacityReservationGroupId": group_id,
                    "logicalZone": "1",
                    "reservedQuantity": 7,
                }]
            return [{
                "id": group_id,
                "name": "group-a",
                "subscriptionId": "sub-b",
                "subscriptionName": "Subscription B",
                "resourceGroup": "rg",
                "location": "eastus",
                "deploymentType": "Zonal",
                "resourceExists": True,
            }, {
                "id": f"{group_id}-empty",
                "name": "group-empty",
                "subscriptionId": "sub-b",
                "subscriptionName": "Subscription B",
                "resourceGroup": "rg",
                "location": "eastus",
                "deploymentType": "Regional",
                "resourceExists": True,
            }]

        run_graph_query.side_effect = graph_result
        get_mappings.return_value = {("sub-b", "eastus"): {"1": "2"}}
        usage_view.return_value = ({"schemaVersion": 1, "usage": {"dates": []}}, {
            reservation_id: {
                "id": reservation_id,
                "subscriptionId": "sub-b",
                "subscriptionName": None,
                "capacityReservationGroupId": group_id,
                "capacityReservationGroupName": "group-a",
                "location": None,
                "vmSize": None,
                "unusedHours": [],
                "unusedCost": [],
                "lastDayUnusedHours": 1.0,
                "lastDayUnusedCost": 2.0,
                "last7DaysUnusedHours": 3.0,
                "last7DaysUnusedCost": 4.0,
                "last30DaysUnusedHours": 5.0,
                "last30DaysUnusedCost": 6.0,
            },
        })
        empty_usage.return_value = {"unusedHours": [], "unusedCost": []}
        enriched_subscription_ids = []

        def add_context(context_rows):
            enriched_subscription_ids.extend(row.get("subscriptionId") for row in context_rows)
            for context_row in context_rows:
                context_row["businessContext"] = {"environment": None}
            return {"environment": "Environment"}

        enrich_context.side_effect = add_context
        request = SimpleNamespace(
            get_json=lambda: {"subscriptionIds": ["sub-b", "outside"]},
        )

        response = odcr_usage_list(request)

        self.assertEqual(200, response.status_code)
        body = json.loads(response.get_body())
        self.assertEqual(2, body["schemaVersion"])
        self.assertEqual("2", body["capacityReservations"][0]["physicalZone"])
        self.assertTrue(body["capacityReservations"][0]["resourceExists"])
        self.assertEqual(7, body["capacityReservations"][0]["reservedQuantity"])
        self.assertEqual(1.0, body["capacityReservations"][0]["lastDayUnusedHours"])
        self.assertEqual("group-a", body["capacityReservationGroups"][0]["name"])
        self.assertEqual("group-empty", body["capacityReservationGroups"][1]["name"])
        self.assertNotIn("location", body["capacityReservations"][0])
        self.assertEqual({"capacityReservationGroups": 2, "capacityReservations": 1}, body["counts"])
        self.assertIn("metadata", body)
        self.assertEqual(
            {"environment": "Environment"},
            body["metadata"]["businessContext"]["fields"],
        )
        self.assertEqual({"environment": None}, body["capacityReservationGroups"][0]["businessContext"])
        self.assertEqual({"environment": None}, body["capacityReservations"][0]["businessContext"])
        enriched_rows = enrich_context.call_args.args[0]
        self.assertEqual(3, len(enriched_rows))
        self.assertEqual(["sub-b", "sub-b", "sub-b"], enriched_subscription_ids)
        server_timing = response.headers["Server-Timing"]
        for stage in (
            "app_scope", "user_token", "capacity_reservation_group_resource_graph",
            "capacity_reservation_resource_graph", "resource_graph", "zone_mapping_lookup",
            "physical_zone_enrichment", "accessible_subscriptions",
            "historical_usage_lookup", "usage_join", "sort", "json_serialization", "total",
            "request_dimensions_enrichment",
        ):
            self.assertRegex(server_timing, rf"(?:^|, ){stage};dur=\d+\.\d")
        self.assertEqual("groups=2,reservations=1", response.headers["X-ODCR-Usage-Counts"])
        usage_view.assert_called_once_with(("sub-b",))
        get_accessible_subscriptions.assert_not_called()
        self.assertEqual(2, run_graph_query.call_count)
        queries = [call.args[0] for call in run_graph_query.call_args_list]
        self.assertTrue(all("subscriptionId in~" not in query for query in queries))
        self.assertTrue(all("outside" not in query for query in queries))
        self.assertEqual(1, sum("location in~ ('eastus')" in query for query in queries))
        self.assertTrue(all(call.args[1] == "user-token" for call in run_graph_query.call_args_list))
        self.assertTrue(all(call.kwargs["subscription_ids"] == ("sub-b",) for call in run_graph_query.call_args_list))

    @patch("functions.odcr_usage_bp.enrich_rows_with_business_context", return_value={})
    @patch("functions.odcr_usage_bp.zone_mapping.pipeline.get_mappings", return_value={})
    @patch("functions.odcr_usage_bp.cr_usage.usage_view")
    @patch("functions.odcr_usage_bp.get_accessible_subscriptions", return_value=["sub-b"])
    @patch("functions.odcr_usage_bp.run_graph_query", return_value=[])
    @patch("functions.odcr_usage_bp.resolve_app_scope")
    @patch("functions.odcr_usage_bp.identity.for_backend_operation")
    @patch("functions.odcr_usage_bp.identity.for_user_action")
    def test_historical_only_subscriptions_use_filtered_access_check(
        self, for_user_action, _backend, resolve_scope, _run_graph_query,
        get_accessible_subscriptions, usage_view, _get_mappings, _enrich_context,
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "user-token", source="test")
        resolve_scope.return_value = self._scope()
        group_id = "/subscriptions/sub-b/resourcegroups/rg/providers/microsoft.compute/capacityreservationgroups/deleted-group"
        reservation_id = f"{group_id}/capacityreservations/deleted-cr"
        usage_view.return_value = ({"schemaVersion": 1, "usage": {"dates": []}}, {
            reservation_id: {
                "id": reservation_id,
                "name": "deleted-cr",
                "subscriptionId": "sub-b",
                "resourceGroup": "rg",
                "capacityReservationGroupId": group_id,
                "capacityReservationGroupName": "deleted-group",
            },
        })

        response = odcr_usage_list(SimpleNamespace(get_json=lambda: {}))

        self.assertEqual(200, response.status_code)
        get_accessible_subscriptions.assert_called_once_with("user-token", ["sub-b"])
        body = json.loads(response.get_body())
        self.assertEqual([reservation_id], [row["id"] for row in body["capacityReservations"]])
        self.assertFalse(body["capacityReservations"][0]["resourceExists"])
        self.assertEqual(["deleted-group"], [group["name"] for group in body["capacityReservationGroups"]])
        self.assertFalse(body["capacityReservationGroups"][0]["resourceExists"])

    @patch("functions.odcr_usage_bp.enrich_rows_with_business_context", return_value={})
    @patch("functions.odcr_usage_bp.resolve_app_scope")
    @patch("functions.odcr_usage_bp.identity.for_backend_operation")
    @patch("functions.odcr_usage_bp.identity.for_user_action")
    @patch("functions.odcr_usage_bp.run_graph_query")
    def test_explicit_empty_scope_returns_no_rows(
        self, run_graph_query, for_user_action, _backend, resolve_scope, _enrich_context
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_scope.return_value = self._scope()

        response = odcr_usage_list(SimpleNamespace(get_json=lambda: {"subscriptionIds": []}))

        body = json.loads(response.get_body())
        self.assertEqual([], body["capacityReservationGroups"])
        self.assertEqual([], body["capacityReservations"])
        run_graph_query.assert_not_called()

    @patch("functions.odcr_usage_bp.identity.for_user_action")
    def test_invalid_body_returns_400(self, for_user_action):
        for_user_action.return_value = SimpleNamespace(source="test")

        response = odcr_usage_list(SimpleNamespace(get_json=lambda: {"subscriptionIds": "sub-a"}))

        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_filter", json.loads(response.get_body())["error"])

    @patch("functions.odcr_usage_bp.resolve_app_scope")
    @patch("functions.odcr_usage_bp.identity.for_backend_operation")
    @patch("functions.odcr_usage_bp.identity.for_user_action")
    def test_initializing_snapshot_returns_retryable_503(
        self, for_user_action, _backend, resolve_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_scope.side_effect = AppScopeInitializingError("warming up")

        response = odcr_usage_list(SimpleNamespace(get_json=lambda: {}))

        self.assertEqual(503, response.status_code)
        self.assertEqual("app_scope_initializing", json.loads(response.get_body())["error"])

    @patch("functions.odcr_usage_bp.resolve_app_scope")
    @patch("functions.odcr_usage_bp.identity.for_backend_operation")
    @patch("functions.odcr_usage_bp.identity.for_user_action")
    def test_empty_scope_returns_409(
        self, for_user_action, _backend, resolve_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token", source="test")
        resolve_scope.side_effect = AppScopeEmptyError("no subscriptions")

        response = odcr_usage_list(SimpleNamespace(get_json=lambda: {}))

        self.assertEqual(409, response.status_code)
        self.assertEqual("app_scope_empty", json.loads(response.get_body())["error"])


if __name__ == "__main__":
    unittest.main()