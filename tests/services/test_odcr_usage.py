from __future__ import annotations

import unittest
from unittest.mock import patch

from clients.resource_graph import get_accessible_subscriptions
from services.odcr_usage import (
    apply_physical_zones,
    build_capacity_reservation_graph_query,
    build_capacity_reservation_group_graph_query,
    zone_mapping_pairs,
)


class OdcrUsageQueryTests(unittest.TestCase):
    def test_queries_project_normalized_capacity_inventory(self):
        group_query = build_capacity_reservation_group_graph_query(locations=["eastus"])
        reservation_query = build_capacity_reservation_graph_query()

        self.assertIn("microsoft.compute/capacityreservationgroups'", group_query)
        self.assertNotIn("capacityreservations", group_query)
        self.assertIn("microsoft.resources/subscriptions", group_query)
        self.assertNotIn("subscriptionId in~", group_query)
        self.assertIn("location in~ ('eastus')", group_query)
        self.assertIn("deploymentType = iff", group_query)
        self.assertIn("microsoft.compute/capacityreservationgroups/capacityreservations", reservation_query)
        self.assertNotIn("subscriptionId in~", reservation_query)
        self.assertNotIn("location in~", reservation_query)
        self.assertIn("logicalZone = tostring(zones[0])", reservation_query)
        self.assertIn("vmSize = tostring(sku.name)", reservation_query)
        self.assertIn("reservedQuantity = toint(sku.capacity)", reservation_query)

    @patch("clients.resource_graph.run_graph_query", return_value=[{"id": "sub-b"}])
    def test_accessible_subscription_query_filters_requested_candidates(self, run_graph_query):
        self.assertEqual(["sub-b"], get_accessible_subscriptions("token", ["SUB-B", "sub'a"]))
        query, token = run_graph_query.call_args.args
        self.assertEqual("token", token)
        self.assertIn("subscriptionId in~ ('sub''a', 'sub-b')", query)

    def test_physical_zone_enrichment_preserves_regional_and_unresolved_rows(self):
        rows = [
            {"subscriptionId": "SUB-A", "location": "EastUS", "logicalZone": "1"},
            {"subscriptionId": "sub-a", "location": "eastus", "logicalZone": "2"},
            {"subscriptionId": "sub-a", "location": "westus", "logicalZone": ""},
        ]

        self.assertEqual(
            {("sub-a", "eastus"), ("sub-a", "westus")},
            zone_mapping_pairs(rows),
        )
        apply_physical_zones(rows, {("sub-a", "eastus"): {"1": "3"}})

        self.assertEqual("3", rows[0]["physicalZone"])
        self.assertIsNone(rows[1]["physicalZone"])
        self.assertIsNone(rows[2]["logicalZone"])
        self.assertIsNone(rows[2]["physicalZone"])


if __name__ == "__main__":
    unittest.main()
