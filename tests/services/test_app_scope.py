from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.app_scope import (
    AppScopeEmptyError,
    AppScopeInitializingError,
    ConfiguredAppScope,
    MODE_ALL,
    MODE_MANAGEMENT_GROUPS,
    MODE_SUBSCRIPTIONS,
)
from services.app_scope import diagnose_app_scope, resolve_app_scope


def _snapshot(values: dict) -> dict:
    return {"kind": "subscriptions", "values": values}


class _Store:
    """Minimal ReferenceDataStore stub returning a fixed Subscription snapshot."""

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.calls = 0

    def open_snapshot(self, kind):
        assert kind == "subscriptions"
        self.calls += 1
        return self._snapshot


class _StoreNever:
    def open_snapshot(self, kind):  # pragma: no cover - must never be called
        raise AssertionError("snapshot must not be read for selected subscriptions")


class ResolveAppScopeTests(unittest.TestCase):
    def test_selected_subscriptions_use_configured_ids_without_snapshot(self):
        configured = ConfiguredAppScope(MODE_SUBSCRIPTIONS, ("sub-a", "sub-b"), ())

        result = resolve_app_scope(None, configured, _StoreNever())

        self.assertEqual(("sub-a", "sub-b"), result.subscription_ids)

    def test_all_scope_returns_normalized_snapshot_keys(self):
        store = _Store(_snapshot({"sub-b": {}, "sub-a": {}}))

        result = resolve_app_scope(None, ConfiguredAppScope(MODE_ALL, (), ()), store)

        self.assertEqual(("sub-a", "sub-b"), result.subscription_ids)

    def test_management_group_matches_ancestor_id_case_insensitively(self):
        store = _Store(_snapshot({
            "sub-a": {"managementGroupAncestors": [{"id": "MG-A", "name": "Group A"}]},
            "sub-b": {"managementGroupAncestors": [{"id": "mg-other"}]},
        }))

        result = resolve_app_scope(
            None, ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a",), ()), store
        )

        self.assertEqual(("sub-a",), result.subscription_ids)

    def test_management_group_no_match_raises_empty(self):
        store = _Store(_snapshot({"sub-a": {"managementGroupAncestors": [{"id": "mg-x"}]}}))

        with self.assertRaises(AppScopeEmptyError):
            resolve_app_scope(
                None, ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a",), ()), store
            )

    def test_all_scope_empty_snapshot_raises_empty(self):
        store = _Store(_snapshot({}))

        with self.assertRaises(AppScopeEmptyError):
            resolve_app_scope(None, ConfiguredAppScope(MODE_ALL, (), ()), store)

    def test_absent_snapshot_raises_initializing(self):
        store = _Store(None)

        with self.assertRaises(AppScopeInitializingError):
            resolve_app_scope(None, ConfiguredAppScope(MODE_ALL, (), ()), store)

    def test_absent_snapshot_with_live_fallback_resolves_from_inventory(self):
        store = _Store(None)
        identity = SimpleNamespace(get_token=lambda: "token")
        inventory = [
            {"id": "sub-b", "name": "Beta", "state": "Enabled"},
            {"id": "sub-a", "name": "Alpha", "state": "Enabled"},
        ]

        with patch(
            "services.app_scope.get_subscription_inventory", return_value=inventory
        ) as fetch:
            result = resolve_app_scope(
                identity,
                ConfiguredAppScope(MODE_ALL, (), ()),
                store,
                allow_live_fallback=True,
            )

        fetch.assert_called_once_with("token")
        self.assertEqual(("sub-a", "sub-b"), result.subscription_ids)

    def test_live_fallback_filters_management_groups_via_ancestors(self):
        store = _Store(None)
        identity = SimpleNamespace(get_token=lambda: "token")
        inventory = [
            {"id": "sub-a", "managementGroupAncestors": [{"id": "MG-A"}]},
            {"id": "sub-b", "managementGroupAncestors": [{"id": "mg-other"}]},
        ]

        with patch("services.app_scope.get_subscription_inventory", return_value=inventory):
            result = resolve_app_scope(
                identity,
                ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a",), ()),
                store,
                allow_live_fallback=True,
            )

        self.assertEqual(("sub-a",), result.subscription_ids)

    def test_live_fallback_without_identity_still_raises_initializing(self):
        store = _Store(None)

        with self.assertRaises(AppScopeInitializingError):
            resolve_app_scope(
                None, ConfiguredAppScope(MODE_ALL, (), ()), store, allow_live_fallback=True
            )

    def test_snapshot_present_never_queries_live_even_when_fallback_allowed(self):
        store = _Store(_snapshot({"sub-a": {}}))

        with patch("services.app_scope.get_subscription_inventory") as fetch:
            result = resolve_app_scope(
                SimpleNamespace(get_token=lambda: "token"),
                ConfiguredAppScope(MODE_ALL, (), ()),
                store,
                allow_live_fallback=True,
            )

        fetch.assert_not_called()
        self.assertEqual(("sub-a",), result.subscription_ids)


class DiagnoseAppScopeTests(unittest.TestCase):
    def test_all_scope_reports_count_and_enforced_generation(self):
        store = _Store({
            "kind": "subscriptions",
            "generation": 5,
            "publishedAt": "2026-08-31T00:00:00+00:00",
            "values": {"sub-a": {}, "sub-b": {}},
        })

        result = diagnose_app_scope(None, ConfiguredAppScope(MODE_ALL, (), ("eastus",)), store=store)

        self.assertEqual(MODE_ALL, result["mode"])
        self.assertEqual("All accessible subscriptions", result["modeLabel"])
        self.assertEqual(2, result["resolvedCount"])
        self.assertFalse(result["isEmpty"])
        self.assertFalse(result["hasUnresolved"])
        self.assertEqual(["eastus"], result["locations"])
        self.assertEqual(5, result["enforced"]["generation"])
        self.assertFalse(result["live"])

    def test_all_scope_empty_snapshot_is_empty_without_raising(self):
        store = _Store(_snapshot({}))

        result = diagnose_app_scope(None, ConfiguredAppScope(MODE_ALL, (), ()), store=store)

        self.assertEqual(0, result["resolvedCount"])
        self.assertTrue(result["isEmpty"])

    def test_selected_subscriptions_breakdown_flags_unresolved(self):
        store = _Store(_snapshot({"sub-a": {"displayName": "Alpha"}}))

        result = diagnose_app_scope(
            None, ConfiguredAppScope(MODE_SUBSCRIPTIONS, ("sub-a", "sub-typo"), ()), store=store
        )

        self.assertEqual(1, result["resolvedCount"])
        self.assertTrue(result["hasUnresolved"])
        self.assertFalse(result["isEmpty"])
        self.assertEqual(
            [
                {"id": "sub-a", "resolved": True, "displayName": "Alpha"},
                {"id": "sub-typo", "resolved": False, "displayName": None},
            ],
            result["subscriptions"],
        )

    def test_selected_subscriptions_all_unresolved_is_empty(self):
        store = _Store(_snapshot({}))

        result = diagnose_app_scope(
            None, ConfiguredAppScope(MODE_SUBSCRIPTIONS, ("sub-a",), ()), store=store
        )

        self.assertTrue(result["isEmpty"])

    def test_management_group_breakdown_reports_per_group_counts(self):
        store = _Store(_snapshot({
            "sub-a": {"managementGroupAncestors": [{"id": "mg-a"}]},
            "sub-b": {"managementGroupAncestors": [{"id": "mg-a"}]},
        }))

        result = diagnose_app_scope(
            None, ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a", "mg-empty"), ()), store=store
        )

        self.assertEqual(2, result["resolvedCount"])
        self.assertTrue(result["hasUnresolved"])
        self.assertFalse(result["isEmpty"])
        self.assertEqual(
            [{"id": "mg-a", "count": 2}, {"id": "mg-empty", "count": 0}],
            result["managementGroups"],
        )

    @patch("services.app_scope.get_subscription_inventory")
    def test_live_read_queries_azure_inventory(self, get_inventory):
        get_inventory.return_value = [{"id": "SUB-A", "name": "Alpha"}]
        store = _Store(_snapshot({}))
        identity = SimpleNamespace(get_token=lambda: "backend-token")

        result = diagnose_app_scope(
            identity, ConfiguredAppScope(MODE_ALL, (), ()), live=True, store=store
        )

        get_inventory.assert_called_once_with("backend-token")
        self.assertTrue(result["live"])
        self.assertEqual(1, result["resolvedCount"])
        self.assertFalse(result["isEmpty"])


if __name__ == "__main__":
    unittest.main()