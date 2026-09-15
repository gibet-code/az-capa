from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.reference_data import (
    LocationReferencePipeline,
    ReferenceDataView,
    SubscriptionReferencePipeline,
    normalize_locations,
)
from storage.reference_data_store import LOCATIONS, SUBSCRIPTIONS


class ReferenceDataViewTests(unittest.TestCase):
    def test_requested_catalogue_loads_lazily_and_is_pinned(self):
        store = MagicMock()
        store.open_snapshot.return_value = {
            "generation": "sub-generation",
            "values": {"sub-a": {"displayName": "Subscription A"}},
        }
        view = ReferenceDataView(store, {SUBSCRIPTIONS})

        self.assertEqual("Subscription A", view.subscription_name("SUB-A"))
        self.assertEqual("Subscription A", view.subscription_name("sub-a"))
        store.open_snapshot.assert_called_once_with(SUBSCRIPTIONS)

    def test_missing_catalogue_entry_falls_back_to_canonical_value(self):
        store = MagicMock()
        store.open_snapshot.return_value = None
        view = ReferenceDataView(store, {LOCATIONS})

        self.assertEqual("westeurope", view.location_display_name("WestEurope"))

    def test_unrequested_capability_fails_explicitly(self):
        view = ReferenceDataView(MagicMock(), {LOCATIONS})

        with self.assertRaisesRegex(ValueError, "was not requested"):
            view.subscription_name("sub-a")


class ReferenceDataPipelineTests(unittest.TestCase):
    @patch("services.reference_data.get_subscription_inventory")
    @patch("services.reference_data.for_backend_operation")
    def test_subscription_refresh_normalizes_and_publishes(self, backend, inventory):
        backend.return_value.get_token.return_value = "token"
        inventory.return_value = [{"id": "SUB-A", "name": "Subscription A", "state": "Enabled"}]
        store = MagicMock()
        store.publish.return_value = "generation"

        result = SubscriptionReferencePipeline(store).refresh({})

        self.assertEqual("ok", result["status"])
        values = store.publish.call_args.args[1]
        self.assertEqual("Subscription A", values["sub-a"]["displayName"])

    @patch("services.reference_data.fetch_location_catalogue")
    @patch("services.reference_data.resolve_app_scope")
    @patch("services.reference_data.for_backend_operation")
    def test_location_refresh_uses_first_canonical_scope_subscription(
        self, backend, resolve_scope, fetch_catalogue
    ):
        backend.return_value.get_token.return_value = "token"
        resolve_scope.return_value = SimpleNamespace(subscription_ids=("sub-z", "sub-a"))
        fetch_catalogue.return_value = {
            "status": "ok",
            "locations": [{"name": "WestEurope", "displayName": "West Europe"}],
        }
        store = MagicMock()
        store.publish.return_value = "generation"

        result = LocationReferencePipeline(store).refresh({})

        self.assertEqual("sub-a", result["sourceSubscriptionId"])
        fetch_catalogue.assert_called_once_with("sub-a", "token")
        self.assertEqual("West Europe", store.publish.call_args.args[1]["westeurope"]["displayName"])

    def test_location_normalization_rejects_conflicting_duplicates(self):
        with self.assertRaisesRegex(ValueError, "Conflicting Location"):
            normalize_locations([
                {"name": "eastus", "displayName": "East US"},
                {"name": "EASTUS", "displayName": "Different"},
            ])


if __name__ == "__main__":
    unittest.main()