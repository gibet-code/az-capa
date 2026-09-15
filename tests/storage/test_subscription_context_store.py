from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from core.identity import UserAuditMetadata
from storage.subscription_context_store import ContextFieldConflictError, SubscriptionContextStore

SUB_A = "11111111-1111-1111-1111-111111111111"


class SubscriptionContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.table = MagicMock()
        self.container = MagicMock()
        self.store = SubscriptionContextStore(lambda: "ContextTable", lambda: "context-container")
        self.store._table = lambda: self.table
        self.store._container_cache = self.container
        self.audit = UserAuditMetadata("tenant", "object", "Admin")

    def test_save_publishes_manifest_after_definition_write(self):
        events = []
        self.table.get_entity.side_effect = ResourceNotFoundError("missing")
        self.table.query_entities.return_value = []
        self.table.create_entity.side_effect = lambda _entity: events.append("table:create")

        def upload(name, *_args, **_kwargs):
            events.append(f"blob:{name.split('/')[0]}")
            return MagicMock()

        self.container.upload_blob.side_effect = upload
        manifest_blob = MagicMock()
        manifest_blob.upload_blob.side_effect = lambda *_args, **_kwargs: (
            events.append("blob:current.json") or {"etag": '"manifest-etag"'}
        )
        self.container.get_blob_client.return_value = manifest_blob
        self.store.get_source_mapping = MagicMock(return_value={SUB_A: "HR"})

        saved = self.store.save_manual_definition(
            self._definition(),
            {SUB_A: "HR"},
            self.audit,
            datetime(2026, 8, 24, tzinfo=timezone.utc),
        )

        self.assertEqual(1, saved["revision"])
        self.assertEqual(["blob:sources", "blob:snapshots", "table:create", "blob:current.json"], events)
        self.assertEqual("tenant", saved["updatedBy"]["tenantId"])

    def test_save_rejects_stale_revision_before_blob_write(self):
        self.store.get_definition = MagicMock(return_value={**self._definition(), "revision": 3})

        with self.assertRaisesRegex(ContextFieldConflictError, "current revision is 3"):
            self.store.save_manual_definition(
                {**self._definition(), "revision": 2},
                {SUB_A: "HR"},
                self.audit,
            )

        self.container.upload_blob.assert_not_called()

    def test_delete_removes_definition_and_all_source_blobs(self):
        existing = {
            **self._definition(),
            "revision": 1,
            "sourceBlob": "sources/business_unit/1.json.gz",
            "updatedAt": datetime(2026, 8, 24, tzinfo=timezone.utc),
            "updatedBy": {"tenantId": "tenant", "objectId": "object", "displayName": "Admin"},
            "etag": 'W/"etag"',
        }
        self.store.get_definition = MagicMock(return_value=existing)
        self.store.list_definitions = MagicMock(return_value=[existing])
        self.store._write_generation = MagicMock(return_value=("snapshots/next.json.gz", {"fields": {}}))
        self.store._publish_generation = MagicMock()
        self.container.list_blobs.side_effect = [
            [
                SimpleNamespace(name="sources/business_unit/1.json.gz"),
                SimpleNamespace(name="sources/business_unit/2.json.gz"),
            ],
            [SimpleNamespace(name="locks/business_unit/1")],
        ]

        deleted = self.store.delete_definition("business_unit")

        self.assertTrue(deleted)
        self.table.delete_entity.assert_called_once()
        self.assertEqual(3, self.container.delete_blob.call_count)
        self.store._publish_generation.assert_called_once()

    @patch("storage.subscription_context_store.RefreshLease")
    @patch("storage.subscription_context_store.BlobLeaseClient")
    def test_try_acquire_refresh_lease_returns_owner(self, lease_client_type, refresh_lease_type):
        lease_client = lease_client_type.return_value
        refresh_lease_type.return_value = MagicMock()

        result = self.store.try_acquire_refresh_lease("organization", 3, 60)

        self.assertIs(refresh_lease_type.return_value, result)
        lease_client.acquire.assert_called_once_with(lease_duration=60)
        refresh_lease_type.assert_called_once_with(lease_client, 60)

    @patch("storage.subscription_context_store.BlobLeaseClient")
    def test_try_acquire_refresh_lease_returns_none_for_contender(self, lease_client_type):
        error = ResourceExistsError("lease held")
        error.error_code = "LeaseAlreadyPresent"
        lease_client_type.return_value.acquire.side_effect = error

        result = self.store.try_acquire_refresh_lease("organization", 3, 60)

        self.assertIsNone(result)

    @patch("storage.subscription_context_store.RefreshLease")
    @patch("storage.subscription_context_store.BlobLeaseClient")
    def test_operation_lease_uses_one_shared_lock(self, lease_client_type, refresh_lease_type):
        result = self.store.try_acquire_operation_lease(60)

        self.assertIs(refresh_lease_type.return_value, result)
        self.container.get_blob_client.assert_called_once_with("locks/operation")
        lease_client_type.return_value.acquire.assert_called_once_with(lease_duration=60)

    def test_refresh_batch_is_idempotent_when_run_generation_is_current(self):
        self.store._snapshot = MagicMock(return_value={"generation": "run-1"})

        result = self.store.publish_refresh_batch(
            "run-1",
            {"business_unit": {SUB_A: "HR"}},
            {"business_unit": 1},
            datetime(2026, 8, 24, tzinfo=timezone.utc),
        )

        self.assertTrue(result["alreadyPublished"])
        self.container.upload_blob.assert_not_called()
        self.table.update_entity.assert_not_called()

    def test_refresh_rejects_changed_definition_revision_before_upload(self):
        self.store.get_definition = MagicMock(return_value={**self._definition(), "revision": 4})

        with self.assertRaisesRegex(ContextFieldConflictError, "changed during refresh"):
            self.store.refresh_mapping(
                "business_unit",
                {SUB_A: "HR"},
                datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc),
                expected_revision=3,
            )

        self.container.upload_blob.assert_not_called()

    def test_resolve_materializes_null_and_respects_disabled_fields(self):
        self.store._snapshot = MagicMock(return_value={
            "fields": {
                "business_unit": {"enabled": True},
                "environment": {"enabled": False},
            },
            "subscriptions": {SUB_A: {"business_unit": "HR", "environment": "Prod"}},
        })

        active = self.store.resolve([SUB_A, "22222222-2222-2222-2222-222222222222"])
        all_fields = self.store.resolve([SUB_A], include_disabled=True)

        self.assertEqual({"business_unit": "HR"}, active[SUB_A])
        self.assertIsNone(active["22222222-2222-2222-2222-222222222222"]["business_unit"])
        self.assertEqual("Prod", all_fields[SUB_A]["environment"])

    def test_dynamic_expiry_survives_entity_round_trip(self):
        resolved_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        expires_at = datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc)
        definition = {
            **self._definition(),
            "revision": 1,
            "sourceBlob": "sources/organization/1.json.gz",
            "resolvedAt": resolved_at,
            "expiresAt": expires_at,
            "updatedAt": resolved_at,
            "updatedBy": {"tenantId": "tenant", "objectId": "object", "displayName": "Admin"},
        }

        entity = self.store._entity_from_definition(definition)
        restored = self.store._definition_from_entity(entity)

        self.assertEqual(expires_at, entity["ExpiresAt"])
        self.assertEqual(expires_at, restored["expiresAt"])

    @staticmethod
    def _definition():
        return {
            "key": "business_unit",
            "name": "Business Unit",
            "enabled": True,
            "valueSource": {"type": "manual_file", "version": 1},
        }


if __name__ == "__main__":
    unittest.main()