from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.zone_mapping import ZoneMappingPipeline


class ZoneMappingPipelineTests(unittest.TestCase):
    @patch("services.zone_mapping._fetch_locations")
    @patch("services.zone_mapping.for_backend_operation")
    def test_resolve_plan_uses_backend_token_for_region_probe(self, backend_operation, fetch_locations):
        identity = MagicMock()
        identity.get_token.return_value = "backend-token"
        backend_operation.return_value = identity
        fetch_locations.return_value = {
            "status": "ok",
            "regions": [{"region": "eastus"}],
        }
        store = MagicMock()
        store.get_state.return_value = None
        pipeline = ZoneMappingPipeline(store)
        pipeline._scope = MagicMock(return_value={
            "subs": ["sub-a"],
            "configSelectors": {"subscriptions": [], "managementGroups": []},
        })

        result = pipeline.resolve_plan({})

        fetch_locations.assert_called_once_with("sub-a", "backend-token")
        self.assertEqual("initial-full", result["reason"])

    @patch("services.zone_mapping._fetch_locations")
    @patch("services.zone_mapping.for_backend_operation")
    def test_fetch_subscription_writes_blob_fragment(self, backend_operation, fetch_locations):
        identity = MagicMock()
        identity.get_token.return_value = "backend-token"
        backend_operation.return_value = identity
        regions = [{"region": "eastus", "zoneMappings": {"1": "eastus-az1"}}]
        fetch_locations.return_value = {"status": "ok", "regions": regions}
        store = MagicMock()
        store.replace_subscription_mappings.return_value = 1
        pipeline = ZoneMappingPipeline(store)

        result = pipeline.fetch_and_store({"subscriptionId": "sub-a"})

        store.replace_subscription_mappings.assert_called_once_with("sub-a", regions)
        self.assertEqual({
            "subscriptionId": "sub-a",
            "status": "ok",
            "regions": 1,
            "upserted": 1,
        }, result)

    def test_write_state_publishes_atomic_snapshot(self):
        store = MagicMock()
        store.publish_snapshot.return_value = {
            "subscriptions": 2,
            "regions": 126,
            "compressedBytes": 2048,
        }
        pipeline = ZoneMappingPipeline(store)
        state = {"subs": ["sub-a", "sub-b"], "knownRegions": ["eastus"]}

        result = pipeline.write_state(state)

        store.publish_snapshot.assert_called_once_with(state)
        self.assertEqual(True, result["ok"])
        self.assertEqual(2, result["subscriptions"])

if __name__ == "__main__":
    unittest.main()
