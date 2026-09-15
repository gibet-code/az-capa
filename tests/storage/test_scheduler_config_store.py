from __future__ import annotations

import unittest
from datetime import datetime, timezone

from storage.scheduler_config_store import SchedulerConfigConflictError, SchedulerConfigStore
from tests.storage._fake_table import FakeTable


def _now() -> datetime:
    return datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)


_WHO = {"tenantId": "T", "objectId": "O", "displayName": "Jane Doe"}


class SchedulerConfigStoreTests(unittest.TestCase):
    def _store(self) -> tuple[SchedulerConfigStore, FakeTable]:
        store = SchedulerConfigStore(lambda: "SchedulerConfig")
        table = FakeTable()
        store._table = lambda: table
        return store, table

    def test_get_missing_returns_none(self):
        store, _ = self._store()
        self.assertIsNone(store.get("cr_usage"))

    def test_patch_creates_first_revision(self):
        store, _ = self._store()

        result = store.patch(
            "cr_usage",
            enabled=False,
            frequency_seconds=86400,
            anchor_seconds=23400,
            expected_revision=None,
            updated_by=_WHO,
            updated_at=_now(),
        )

        self.assertEqual(1, result["revision"])
        self.assertFalse(result["enabled"])
        self.assertEqual(_WHO, result["updatedBy"])
        self.assertEqual(1, store.get("cr_usage")["revision"])

    def test_patch_create_rejects_stale_revision(self):
        store, _ = self._store()
        with self.assertRaises(SchedulerConfigConflictError):
            store.patch(
                "cr_usage",
                enabled=True,
                frequency_seconds=86400,
                anchor_seconds=23400,
                expected_revision=1,
                updated_by=_WHO,
                updated_at=_now(),
            )

    def test_patch_increments_revision(self):
        store, _ = self._store()
        store.patch(
            "cr_usage",
            enabled=False,
            frequency_seconds=86400,
            anchor_seconds=23400,
            expected_revision=None,
            updated_by=_WHO,
            updated_at=_now(),
        )

        result = store.patch(
            "cr_usage",
            enabled=True,
            frequency_seconds=3600,
            anchor_seconds=0,
            expected_revision=1,
            updated_by=_WHO,
            updated_at=_now(),
        )

        self.assertEqual(2, result["revision"])
        self.assertTrue(result["enabled"])
        self.assertEqual(3600, result["frequencySeconds"])

    def test_patch_rejects_stale_revision_on_update(self):
        store, _ = self._store()
        store.patch(
            "cr_usage",
            enabled=False,
            frequency_seconds=86400,
            anchor_seconds=23400,
            expected_revision=None,
            updated_by=_WHO,
            updated_at=_now(),
        )

        with self.assertRaises(SchedulerConfigConflictError):
            store.patch(
                "cr_usage",
                enabled=True,
                frequency_seconds=3600,
                anchor_seconds=0,
                expected_revision=0,
                updated_by=_WHO,
                updated_at=_now(),
            )

    def test_patch_detects_concurrent_write_via_etag(self):
        store, table = self._store()
        store.patch(
            "cr_usage",
            enabled=False,
            frequency_seconds=86400,
            anchor_seconds=23400,
            expected_revision=None,
            updated_by=_WHO,
            updated_at=_now(),
        )

        original_update = table.update_entity

        def racing_update(entity, *args, **kwargs):
            table.rows[("config", "cr_usage")]["Revision"] = 99
            table._bump(("config", "cr_usage"))
            return original_update(entity, *args, **kwargs)

        table.update_entity = racing_update
        with self.assertRaises(SchedulerConfigConflictError):
            store.patch(
                "cr_usage",
                enabled=True,
                frequency_seconds=3600,
                anchor_seconds=0,
                expected_revision=1,
                updated_by=_WHO,
                updated_at=_now(),
            )

    def test_list_returns_all_config_rows(self):
        store, _ = self._store()
        for pipeline in ("cr_usage", "vm_usage"):
            store.patch(
                pipeline,
                enabled=True,
                frequency_seconds=86400,
                anchor_seconds=0,
                expected_revision=None,
                updated_by=_WHO,
                updated_at=_now(),
            )

        pipelines = {row["pipeline"] for row in store.list()}
        self.assertEqual({"cr_usage", "vm_usage"}, pipelines)

    def test_clear_removes_all_rows(self):
        store, _ = self._store()
        for pipeline in ("cr_usage", "vm_usage"):
            store.patch(
                pipeline,
                enabled=True,
                frequency_seconds=86400,
                anchor_seconds=0,
                expected_revision=None,
                updated_by=_WHO,
                updated_at=_now(),
            )

        cleared = store.clear()

        self.assertEqual(2, cleared)
        self.assertEqual([], store.list())
        self.assertIsNone(store.get("cr_usage"))

    def test_clear_empty_returns_zero(self):
        store, _ = self._store()
        self.assertEqual(0, store.clear())


if __name__ == "__main__":
    unittest.main()
