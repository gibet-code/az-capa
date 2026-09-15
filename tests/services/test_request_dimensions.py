from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from services.request_dimensions import BUSINESS_CONTEXT, RequestDimensionsView, parse_context_filters
from storage.reference_data_store import LOCATIONS, SUBSCRIPTIONS


class RequestDimensionsViewTests(unittest.TestCase):
    def test_context_filter_parser_rejects_duplicates_and_empty_values(self):
        with self.assertRaisesRegex(ValueError, "only once"):
            parse_context_filters([
                {"fieldKey": "environment", "values": ["prod"]},
                {"fieldKey": "Environment", "values": ["dev"]},
            ])
        with self.assertRaisesRegex(ValueError, "non-empty values"):
            parse_context_filters([{"fieldKey": "environment", "values": []}])

    def _view(self, capabilities, context_snapshot=None):
        reference_store = MagicMock()
        reference_store.open_snapshot.return_value = None
        context_store = MagicMock()
        context_store.open_snapshot.return_value = context_snapshot
        return RequestDimensionsView(capabilities, reference_store, context_store), reference_store, context_store

    def test_context_filter_values_or_within_field_and_and_across_fields(self):
        snapshot = {
            "generation": "context-1",
            "fields": {
                "environment": {"name": "Environment", "enabled": True},
                "cost_center": {"name": "Cost Center", "enabled": True},
            },
            "subscriptions": {
                "sub-a": {"environment": "Production", "cost_center": "100"},
                "sub-b": {"environment": "Development", "cost_center": "100"},
                "sub-c": {"environment": "production"},
            },
        }
        view, _, context_store = self._view({BUSINESS_CONTEXT}, snapshot)

        result = view.filter_subscriptions(
            ["sub-a", "sub-b", "sub-c"],
            context_filters=[
                {"fieldKey": "environment", "values": ["PRODUCTION"]},
                {"fieldKey": "cost_center", "values": ["100", None]},
            ],
        )

        self.assertEqual(("sub-a", "sub-c"), result)
        context_store.open_snapshot.assert_called_once_with()

    def test_explicit_empty_subscription_or_location_is_zero_scope(self):
        view, _, _ = self._view(set())

        self.assertEqual((), view.filter_subscriptions(["sub-a"], []))
        self.assertEqual((), view.filter_locations(["westeurope"], []))

    def test_unknown_or_malformed_context_filter_fails_closed(self):
        view, _, _ = self._view({BUSINESS_CONTEXT}, {"fields": {}, "subscriptions": {}})

        self.assertEqual((), view.filter_subscriptions(
            ["sub-a"], context_filters=[{"fieldKey": "unknown", "values": ["x"]}]
        ))
        self.assertEqual((), view.filter_subscriptions(
            ["sub-a"], context_filters=[{"fieldKey": "unknown", "values": []}]
        ))

    def test_enrichment_and_metadata_use_same_pinned_context_snapshot(self):
        snapshot = {
            "generation": "context-1",
            "publishedAt": "2026-08-24T12:00:00+00:00",
            "fields": {"environment": {"name": "Environment", "enabled": True}},
            "subscriptions": {"sub-a": {"environment": "Production"}},
        }
        view, _, context_store = self._view({BUSINESS_CONTEXT}, snapshot)
        rows = [{"subscriptionId": "SUB-A"}]

        view.enrich_rows(rows)
        metadata = view.metadata()

        self.assertEqual({"environment": "Production"}, rows[0]["businessContext"])
        self.assertEqual("context-1", metadata["businessContext"]["generation"])
        context_store.open_snapshot.assert_called_once_with()

    def test_unrequested_context_capability_is_not_loaded(self):
        view, _, context_store = self._view({SUBSCRIPTIONS, LOCATIONS})

        view.metadata()

        context_store.open_snapshot.assert_not_called()

    def test_catalogue_counts_context_values_only_for_visible_subscriptions(self):
        reference_store = MagicMock()
        reference_store.open_snapshot.side_effect = [
            {
                "values": {
                    "sub-a": {"displayName": "Subscription A"},
                    "sub-hidden": {"displayName": "Hidden"},
                },
            },
            {"values": {"eastus": {"displayName": "East US"}}},
        ]
        context_store = MagicMock()
        context_store.open_snapshot.return_value = {
            "fields": {"environment": {"name": "Environment", "enabled": True}},
            "subscriptions": {
                "sub-a": {"environment": "Production"},
                "sub-b": {},
                "sub-hidden": {"environment": "Secret"},
            },
        }
        view = RequestDimensionsView(
            {SUBSCRIPTIONS, LOCATIONS, BUSINESS_CONTEXT},
            reference_store,
            context_store,
        )

        result = view.catalogue(["sub-a", "sub-b"], ["eastus"])

        self.assertEqual({"sub-a", "sub-b"}, {item["value"] for item in result["subscriptions"]})
        values = result["contextFields"][0]["values"]
        self.assertEqual(["Production", None], [item["value"] for item in values])
        self.assertNotIn("Secret", str(result))


if __name__ == "__main__":
    unittest.main()