from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from services.subscription_context import SubscriptionContextService

SUB_A = "11111111-1111-1111-1111-111111111111"


class SubscriptionContextRefreshTests(unittest.TestCase):
    def test_resolve_reads_snapshot_without_loading_live_inventory(self):
        store = MagicMock()
        store.resolve.return_value = {SUB_A: {"environment": "PRD"}}
        inventory_loader = MagicMock()
        service = SubscriptionContextService(store, inventory_loader=inventory_loader)

        result = service.resolve([SUB_A])

        self.assertEqual("PRD", result[SUB_A]["environment"])
        inventory_loader.assert_not_called()
        store.resolve.assert_called_once_with([SUB_A], False)

    def test_refresh_loads_inventory_once_and_publishes_all_fields_once(self):
        store = MagicMock()
        now = datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc)
        lease = MagicMock()
        store.try_acquire_operation_lease.return_value = lease
        store.list_definitions.return_value = [
            {
                "key": "organization",
                "enabled": True,
                "revision": 3,
                "sourceBlob": "sources/organization/3-old.json.gz",
                "valueSource": {
                    "type": "management_group_level",
                    "version": 1,
                    "config": {"level": 1, "outputMode": "selected_level"},
                },
            },
            {
                "key": "environment",
                "enabled": True,
                "revision": 2,
                "sourceBlob": "sources/environment/2-old.json.gz",
                "valueSource": {
                    "type": "subscription_tag",
                    "version": 1,
                    "config": {"tagKey": "Environment"},
                },
            },
        ]
        inventory_loader = MagicMock(return_value=[{
            "id": SUB_A,
            "tags": {"environment": "PRD"},
            "managementGroupAncestors": [
                {"id": "team", "name": "Team"},
                {"id": "organization", "name": "Organization"},
                {"id": "root", "name": "Tenant Root"},
            ],
        }])
        service = SubscriptionContextService(store, inventory_loader=inventory_loader, now=lambda: now)

        result = service.refresh({"refreshRunId": "run-1", "scheduled": True})

        self.assertEqual("completed", result["status"])
        inventory_loader.assert_called_once_with()
        store.publish_refresh_batch.assert_called_once_with(
            "run-1",
            {"organization": {SUB_A: "Organization"}, "environment": {SUB_A: "PRD"}},
            {"organization": 3, "environment": 2},
            now,
        )
        lease.release.assert_called_once_with()

    def test_failed_enabled_field_without_previous_mapping_blocks_publication(self):
        store = MagicMock()
        store.try_acquire_operation_lease.return_value = MagicMock()
        store.list_definitions.return_value = [self._dynamic_definition(source_blob=None)]
        service = SubscriptionContextService(
            store,
            inventory_loader=MagicMock(side_effect=RuntimeError("unavailable")),
        )

        result = service.refresh({"refreshRunId": "run-2", "scheduled": True, "finalAttempt": True})

        self.assertEqual("failed", result["status"])
        self.assertFalse(result["published"])
        store.publish_refresh_batch.assert_not_called()

    def test_failed_field_with_previous_mapping_is_retained_in_partial_publication(self):
        store = MagicMock()
        store.try_acquire_operation_lease.return_value = MagicMock()
        store.list_definitions.return_value = [
            self._dynamic_definition("sources/organization/3-previous.json.gz")
        ]
        service = SubscriptionContextService(
            store,
            inventory_loader=MagicMock(side_effect=RuntimeError("unavailable")),
        )

        result = service.refresh({"refreshRunId": "run-3", "scheduled": True, "finalAttempt": True})

        self.assertEqual("partial", result["status"])
        store.publish_refresh_batch.assert_called_once()
        self.assertEqual({}, store.publish_refresh_batch.call_args.args[1])

    def test_inventory_failure_is_transient_before_final_attempt(self):
        store = MagicMock()
        store.try_acquire_operation_lease.return_value = MagicMock()
        store.list_definitions.return_value = [self._dynamic_definition(None)]
        service = SubscriptionContextService(
            store,
            inventory_loader=MagicMock(side_effect=RuntimeError("unavailable")),
        )

        result = service.refresh({"refreshRunId": "run-4", "scheduled": True})

        self.assertEqual("transient", result["status"])
        store.publish_refresh_batch.assert_not_called()

    @staticmethod
    def _dynamic_definition(source_blob: str | None) -> dict:
        return {
            "key": "organization",
            "enabled": True,
            "revision": 3,
            "sourceBlob": source_blob,
            "valueSource": {
                "type": "management_group_level",
                "version": 1,
                "config": {"level": 1, "outputMode": "selected_level"},
            },
        }


if __name__ == "__main__":
    unittest.main()