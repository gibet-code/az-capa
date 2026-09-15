from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.compute_skus import (
    ComputeSkuPipeline,
    SkuCatalogConflict,
    _fetch_catalog,
    build_unique_catalog,
    friendly_family_name,
)


def _sku(name="Standard_D4ps_v5", family="standardDPSv5Family", memory="16"):
    return {
        "resourceType": "virtualMachines",
        "name": name,
        "size": name.removeprefix("Standard_"),
        "tier": "Standard",
        "family": family,
        "locations": ["eastus"],
        "capabilities": [
            {"name": "vCPUs", "value": "4"},
            {"name": "MemoryGB", "value": memory},
            {"name": "CapacityReservationSupported", "value": "True"},
            {"name": "RetirementDateUtc", "value": "01/01/9999"},
        ],
    }


class ComputeSkuCatalogTests(unittest.TestCase):
    def test_builds_unique_union_across_all_regions(self):
        east = _sku()
        west = _sku()
        west["name"] = "standard_d4ps_v5"
        west["locations"] = ["westeurope"]
        other = _sku("Standard_E8as_v5", "standardEASv5Family", "64")
        disk = {"resourceType": "disks", "name": "Premium_LRS"}

        result = build_unique_catalog([east, west, other, disk], date(2026, 8, 21))

        self.assertEqual(3, len(result))
        self.assertEqual(
            {("standard_d4ps_v5", "eastus"), ("standard_d4ps_v5", "westeurope"), ("standard_e8as_v5", "eastus")},
            {(row["name"].lower(), row["location"]) for row in result},
        )
        self.assertEqual("Dpsv5", result[0]["friendlyFamily"])
        self.assertEqual("General purpose", result[0]["category"])
        self.assertEqual("ARM", result[0]["processor"])
        self.assertEqual(4, result[0]["vcpus"])
        self.assertEqual(16.0, result[0]["memoryGB"])
        self.assertTrue(result[0]["capacityReservationSupported"])
        self.assertEqual("eastus", result[0]["location"])

    def test_rejects_conflicting_properties_within_same_sku_location(self):
        first = _sku()
        second = _sku(memory="32")

        with self.assertRaisesRegex(SkuCatalogConflict, "memoryGB"):
            build_unique_catalog([first, second])

    def test_capacity_reservation_support_is_preserved_per_region(self):
        supported = _sku()
        unsupported = _sku()
        unsupported["capabilities"][2]["value"] = "False"
        unsupported["locations"] = ["westeurope"]

        result = build_unique_catalog([unsupported, supported])
        by_location = {row["location"]: row for row in result}

        self.assertTrue(by_location["eastus"]["capacityReservationSupported"])
        self.assertFalse(by_location["westeurope"]["capacityReservationSupported"])

    def test_missing_capacity_reservation_capability_is_unknown(self):
        item = _sku()
        item["capabilities"] = [
            capability
            for capability in item["capabilities"]
            if capability["name"] != "CapacityReservationSupported"
        ]

        result = build_unique_catalog([item])

        self.assertIsNone(result[0]["capacityReservationSupported"])

    def test_api_retirement_and_curated_fallback(self):
        api = _sku("Standard_D2s_v5", "standardDSv5Family")
        api["capabilities"][-1]["value"] = "11/16/2028"
        curated = _sku("Standard_NC6s_v3", "standardNCSv3Family")

        result = build_unique_catalog([api, curated], date(2026, 8, 21))
        by_name = {row["name"]: row for row in result}

        self.assertEqual("Announced", by_name["Standard_D2s_v5"]["retirementStatus"])
        self.assertEqual("2028-11-16", by_name["Standard_D2s_v5"]["retirementDate"])
        self.assertEqual("Retired", by_name["Standard_NC6s_v3"]["retirementStatus"])
        self.assertEqual("2025-09-30", by_name["Standard_NC6s_v3"]["retirementDate"])

    def test_friendly_family_normalization(self):
        self.assertEqual("Dsv5", friendly_family_name("standardDSv5Family"))
        self.assertEqual("Dv2", friendly_family_name("standardDv2PromoFamily"))

class ComputeSkuPipelineTests(unittest.TestCase):
    @patch("services.compute_skus.for_backend_operation")
    @patch("services.compute_skus.resolve_app_scope")
    @patch("services.compute_skus.configured_scope")
    def test_resolve_plan_uses_first_sorted_in_scope_subscription(
        self, configured_scope, resolve_app_scope, for_backend_operation
    ):
        configured_scope.return_value = SimpleNamespace(mode="all", selectors=())
        resolve_app_scope.return_value = SimpleNamespace(subscription_ids=("sub-z", "sub-a"))
        pipeline = ComputeSkuPipeline(MagicMock())

        result = pipeline.resolve_plan({})

        self.assertEqual("sub-a", result["sourceSubscriptionId"])
        resolve_app_scope.assert_called_once_with(
            for_backend_operation.return_value,
            configured_scope.return_value,
            allow_live_fallback=True,
        )

    def test_enriches_vm_rows_with_supported_unsupported_and_unknown_results(self):
        store = MagicMock()
        store.find_pairs.return_value = (
            {
                ("standard_d2s_v5", "eastus"): {"capacityReservationSupported": True},
                ("standard_d4s_v5", "eastus"): {"capacityReservationSupported": False},
            },
            {("standard_d8s_v5", "eastus"): "table unavailable"},
        )
        rows = [
            {"vmSize": "Standard_D2s_v5", "location": "EASTUS"},
            {"vmSize": "Standard_D4s_v5", "location": "eastus"},
            {"vmSize": "Standard_D8s_v5", "location": "eastus"},
            {"vmSize": "Standard_D16s_v5", "location": "eastus"},
        ]

        result = ComputeSkuPipeline(store).enrich_vm_rows(rows)

        self.assertIs(result, rows)
        self.assertTrue(result[0]["capacityReservationSupported"])
        self.assertFalse(result[1]["capacityReservationSupported"])
        self.assertIsNone(result[2]["capacityReservationSupported"])
        self.assertEqual("table unavailable", result[2]["capacityReservationSupportReason"])
        self.assertIn("No Compute SKU catalog record", result[3]["capacityReservationSupportReason"])
        store.find_pairs.assert_called_once()


if __name__ == "__main__":
    unittest.main()