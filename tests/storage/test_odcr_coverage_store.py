from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.identity import UserAuditMetadata
from services.odcr_coverage_decisions import parse_decision_change
from storage.odcr_coverage_store import _COVERAGE_SELECT, OdcrCoverageDecisionsStore, resource_row_key


VM_1 = "/subscriptions/SUB-A/resourceGroups/RG/providers/Microsoft.Compute/virtualMachines/VM-1"


class OdcrCoverageDecisionsStoreTests(unittest.TestCase):
    def setUp(self):
        self.table = MagicMock()
        service = MagicMock()
        service.get_table_client.return_value = self.table
        self.settings_patch = patch("storage.odcr_coverage_store.table_service_client", return_value=service)
        self.settings_patch.start()
        self.store = OdcrCoverageDecisionsStore(lambda: "OdcrCoverageDecisions")
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        self.audit = UserAuditMetadata("tenant", "object", "Jane Doe")

    def tearDown(self):
        self.settings_patch.stop()

    def test_upsert_uses_subscription_partition_and_returns_entities(self):
        change = parse_decision_change({
            "resourceIds": [VM_1], "decision": "required", "priority": "high", "note": "critical",
        })

        result = self.store.upsert_change(change, self.audit, self.now)

        transaction = self.table.submit_transaction.call_args.args[0]
        entity = transaction[0][1]
        self.assertEqual("sub-a", entity["PartitionKey"])
        self.assertEqual(resource_row_key(VM_1.lower()), entity["RowKey"])
        self.assertEqual("Jane Doe", entity["UpdatedByDisplayName"])
        self.assertEqual("required", result[0]["Decision"])
        self.assertEqual(self.now, result[0]["UpdatedAt"])

    def test_clear_is_audited_tombstone(self):
        result = self.store.upsert_change(parse_decision_change({
            "resourceIds": [VM_1], "decision": None,
        }), self.audit, self.now)

        entity = self.table.submit_transaction.call_args.args[0][0][1]
        self.assertEqual("cleared", entity["Decision"])
        self.assertEqual("cleared", result[0]["Decision"])
        self.assertEqual("Jane Doe", result[0]["UpdatedByDisplayName"])

    def test_reads_subscription_partition_and_ignores_hash_mismatch(self):
        self.table.query_entities.return_value = [{
            "PartitionKey": "sub-a", "RowKey": resource_row_key(VM_1.lower()),
            "ResourceId": VM_1, "Decision": "not_required", "UpdatedAt": self.now,
        }, {
            "PartitionKey": "sub-a", "RowKey": "wrong", "ResourceId": "/bad",
        }]

        result = self.store.get_for_subscriptions(["SUB-A", "sub-a"])

        self.assertEqual([VM_1.lower()], list(result))
        self.assertEqual("not_required", result[VM_1.lower()]["Decision"])
        self.table.query_entities.assert_called_once_with(
            "PartitionKey eq 'sub-a'", select=_COVERAGE_SELECT,
        )

    def test_dense_reads_return_one_decision_per_vm_across_subscription_partitions(self):
        subscriptions = ["sub-a", "sub-b", "sub-c"]
        rows_by_subscription = {}
        for subscription_id in subscriptions:
            rows = []
            for index in range(1758):
                resource_id = (
                    f"/subscriptions/{subscription_id}/resourcegroups/rg/providers/"
                    f"microsoft.compute/virtualmachines/vm-{index}"
                )
                rows.append({
                    "RowKey": resource_row_key(resource_id), "ResourceId": resource_id,
                    "Decision": "not_required", "UpdatedAt": self.now,
                })
            rows_by_subscription[subscription_id] = rows

        def query_partition(query, *, select):
            self.assertEqual(_COVERAGE_SELECT, select)
            subscription_id = query.split("'")[1]
            return iter(rows_by_subscription[subscription_id])

        self.table.query_entities.side_effect = query_partition

        result = self.store.get_for_subscriptions([*subscriptions, "SUB-A"])

        self.assertEqual(5274, len(result))
        self.assertEqual(3, self.table.query_entities.call_count)
        self.assertTrue(all(value["Decision"] == "not_required" for value in result.values()))


if __name__ == "__main__":
    unittest.main()
