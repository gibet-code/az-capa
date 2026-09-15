from __future__ import annotations

import unittest

from core.app_scope import (
    ConfiguredAppScope,
    MODE_ALL,
    MODE_MANAGEMENT_GROUPS,
    ResolvedAppScope,
    intersect_filters,
)


class AppScopeTests(unittest.TestCase):
    def test_intersection_enforces_subscriptions_and_locations(self):
        configured = ConfiguredAppScope(MODE_ALL, (), ("eastus", "westeurope"))
        scope = ResolvedAppScope(configured, ("sub-a", "sub-b"))

        result = intersect_filters(scope, ["SUB-B", "sub-outside"], ["WestEurope"])

        self.assertEqual(("sub-b",), result.subscription_ids)
        self.assertEqual(("westeurope",), result.locations)
        self.assertFalse(result.is_empty)

    def test_disjoint_filter_is_explicitly_empty(self):
        scope = ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), ()), ("sub-a",))

        result = intersect_filters(scope, ["sub-outside"])

        self.assertTrue(result.is_empty)
        self.assertEqual((), result.subscription_ids)

    def test_explicit_empty_subscription_filter_returns_no_rows(self):
        scope = ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), ()), ("sub-a",))

        result = intersect_filters(scope, [])

        self.assertTrue(result.is_empty)
        self.assertEqual((), result.subscription_ids)

    def test_explicit_empty_location_filter_returns_no_rows(self):
        scope = ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), ("eastus",)), ("sub-a",))

        result = intersect_filters(scope, None, [])

        self.assertTrue(result.is_empty)
        self.assertEqual((), result.locations)

    def test_omitted_filters_remain_unrestricted(self):
        scope = ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), ("eastus",)), ("sub-a",))

        result = intersect_filters(scope, None, None)

        self.assertFalse(result.is_empty)
        self.assertEqual(("sub-a",), result.subscription_ids)
        self.assertEqual(("eastus",), result.locations)

    def test_location_changes_do_not_change_selector_fingerprint(self):
        first = ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a",), ("eastus",))
        second = ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a",), ("westeurope",))

        self.assertEqual(first.selector_fingerprint, second.selector_fingerprint)


if __name__ == "__main__":
    unittest.main()