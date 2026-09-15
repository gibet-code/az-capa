from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.app_scope import ConfiguredAppScope, MODE_ALL, ResolvedAppScope
from core.identity import UserAuditMetadata
from functions.odcr_coverage_bp import update_odcr_coverage_decisions


VM_1 = "/subscriptions/SUB-A/resourceGroups/RG/providers/Microsoft.Compute/virtualMachines/VM-1"


class OdcrCoverageDecisionsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.authorization_patch = patch("functions.odcr_coverage_bp.authorization.require_user")
        self.authorization_patch.start()
        self.addCleanup(self.authorization_patch.stop)

    def _request(self, body):
        return SimpleNamespace(get_json=lambda: body)

    def _scope(self, subscriptions=("sub-a", "sub-b")):
        return ResolvedAppScope(ConfiguredAppScope(MODE_ALL, (), ()), subscriptions)

    @patch("functions.odcr_coverage_bp.odcr_coverage_decisions.upsert_change")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query")
    @patch("functions.odcr_coverage_bp.identity.user_audit_metadata")
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_bulk_update_validates_visibility_and_writes_once(
        self, for_user_action, user_audit, run_graph_query, _backend, resolve_scope, upsert
    ):
        who = SimpleNamespace(get_token=lambda: "user-token", source="test")
        for_user_action.return_value = who
        audit = UserAuditMetadata("tenant", "object", "Jane Doe")
        user_audit.return_value = audit
        resolve_scope.return_value = self._scope()
        second = VM_1.replace("SUB-A", "SUB-B").replace("VM-1", "VM-2")
        run_graph_query.return_value = [{"id": VM_1}, {"id": second}]
        upsert.return_value = [
            {"resourceId": VM_1.lower(), "odcrCoverage": {"decision": "required"}},
            {"resourceId": second.lower(), "odcrCoverage": {"decision": "required"}},
        ]

        response = update_odcr_coverage_decisions(self._request({
            "resourceIds": [VM_1, second],
            "decision": "required",
            "priority": "high",
            "note": "critical",
        }))

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, json.loads(response.get_body())["count"])
        self.assertIn("type =~ 'microsoft.compute/virtualmachines'", run_graph_query.call_args.args[0])
        run_graph_query.assert_called_once_with(run_graph_query.call_args.args[0], "user-token")
        upsert.assert_called_once()
        self.assertEqual(audit, upsert.call_args.args[1])

    @patch("functions.odcr_coverage_bp.odcr_coverage_decisions.upsert_change")
    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.run_graph_query", return_value=[])
    @patch("functions.odcr_coverage_bp.identity.user_audit_metadata", return_value=UserAuditMetadata("t", "o", "User"))
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_invisible_vm_is_rejected_before_storage(
        self, for_user_action, _audit, _graph, _backend, resolve_scope, upsert
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token")
        resolve_scope.return_value = self._scope()

        response = update_odcr_coverage_decisions(self._request({
            "resourceIds": [VM_1], "decision": "not_required",
        }))

        self.assertEqual(403, response.status_code)
        self.assertEqual("vm_not_accessible", json.loads(response.get_body())["error"])
        upsert.assert_not_called()

    @patch("functions.odcr_coverage_bp.resolve_app_scope")
    @patch("functions.odcr_coverage_bp.identity.for_backend_operation")
    @patch("functions.odcr_coverage_bp.identity.user_audit_metadata", return_value=UserAuditMetadata("t", "o", "User"))
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_out_of_app_scope_is_rejected_before_graph(
        self, for_user_action, _audit, _backend, resolve_scope
    ):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token")
        resolve_scope.return_value = self._scope(("sub-b",))

        with patch("functions.odcr_coverage_bp.run_graph_query") as graph:
            response = update_odcr_coverage_decisions(self._request({
                "resourceIds": [VM_1], "decision": None,
            }))

        self.assertEqual(403, response.status_code)
        graph.assert_not_called()

    @patch("functions.odcr_coverage_bp.identity.user_audit_metadata", return_value=UserAuditMetadata("t", "o", "User"))
    @patch("functions.odcr_coverage_bp.identity.for_user_action")
    def test_invalid_payload_returns_400(self, for_user_action, _audit):
        for_user_action.return_value = SimpleNamespace(get_token=lambda: "token")
        response = update_odcr_coverage_decisions(self._request({
            "resourceIds": [VM_1], "decision": "required",
        }))
        self.assertEqual(400, response.status_code)


if __name__ == "__main__":
    unittest.main()