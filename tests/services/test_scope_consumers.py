from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.app_scope import ConfiguredAppScope, MODE_ALL, MODE_MANAGEMENT_GROUPS, ResolvedAppScope
from services.usage_refresh import _resolve_scope


class RefreshScopeTests(unittest.TestCase):
    @patch("services.usage_refresh.resolve_app_scope")
    @patch("services.usage_refresh.configured_scope")
    @patch("services.usage_refresh.settings.cost_management_billing_profile_id", return_value="profile")
    @patch("services.usage_refresh.settings.cost_management_billing_account_id", return_value="account")
    @patch("services.usage_refresh.settings.cost_management_method", return_value="per-billing")
    def test_per_billing_all_preserves_unfiltered_sentinel(
        self, _method, _account, _profile, configured_scope, resolve_app_scope
    ):
        configured_scope.return_value = ConfiguredAppScope(MODE_ALL, (), ("eastus",))

        result = _resolve_scope(MagicMock())

        self.assertIsNone(result["subs"])
        # Location is a read-time boundary, not a collection scope dimension.
        self.assertNotIn("locations", result)
        resolve_app_scope.assert_not_called()

    @patch("services.usage_refresh.resolve_app_scope")
    @patch("services.usage_refresh.configured_scope")
    @patch("services.usage_refresh.settings.cost_management_method", return_value="per-sub")
    def test_per_sub_delegates_to_global_resolver(self, _method, configured_scope, resolve_app_scope):
        configured = ConfiguredAppScope(MODE_MANAGEMENT_GROUPS, ("mg-a",), ())
        configured_scope.return_value = configured
        resolve_app_scope.return_value = ResolvedAppScope(configured, ("sub-a",))
        identity = SimpleNamespace()

        result = _resolve_scope(identity)

        self.assertEqual(["sub-a"], result["subs"])
        resolve_app_scope.assert_called_once_with(identity, configured, allow_live_fallback=True)


if __name__ == "__main__":
    unittest.main()