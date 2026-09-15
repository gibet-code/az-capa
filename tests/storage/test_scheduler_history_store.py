from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from storage.scheduler_history_store import SchedulerHistoryStore
from tests.storage._fake_table import FakeTable


def _at(day: int = 2, hour: int = 6, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


class SchedulerHistoryStoreTests(unittest.TestCase):
    def _store(self) -> tuple[SchedulerHistoryStore, FakeTable]:
        store = SchedulerHistoryStore(lambda: "SchedulerRunHistory")
        table = FakeTable()
        store._table = lambda: table
        return store, table

    def test_open_row_is_running(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(), "inst-1")

        runs = store.list_by_pipeline("cr_usage")
        self.assertEqual(1, len(runs))
        self.assertEqual("running", runs[0]["status"])
        self.assertEqual("inst-1", runs[0]["instanceId"])
        self.assertIsNone(runs[0]["summary"])

    def test_list_by_pipeline_newest_first(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(1), "inst-old")
        store.open_row("cr_usage", "refresh", "scheduled", _at(3), "inst-new")

        runs = store.list_by_pipeline("cr_usage")
        self.assertEqual(["inst-new", "inst-old"], [run["instanceId"] for run in runs])

    def test_list_by_pipeline_honors_limit(self):
        store, _ = self._store()
        for day in range(1, 6):
            store.open_row("cr_usage", "refresh", "scheduled", _at(day), f"inst-{day}")

        self.assertEqual(2, len(store.list_by_pipeline("cr_usage", limit=2)))

    def test_close_by_instance_sets_terminal_and_summary(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(), "inst-1")

        closed = store.close_by_instance("cr_usage", "inst-1", "completed", _at(6, 12), {"upserted": 5})

        self.assertTrue(closed)
        run = store.list_by_pipeline("cr_usage")[0]
        self.assertEqual("completed", run["status"])
        self.assertEqual(_at(6, 12), run["completedAt"])
        self.assertEqual({"upserted": 5}, run["summary"])

    def test_close_by_instance_is_idempotent(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(), "inst-1")
        store.close_by_instance("cr_usage", "inst-1", "completed", _at(6, 12), {"upserted": 5})

        # A backstop reconciler trying to close the same run finds no running row.
        second = store.close_by_instance("cr_usage", "inst-1", "failed", _at(6, 20))

        self.assertFalse(second)
        run = store.list_by_pipeline("cr_usage")[0]
        self.assertEqual("completed", run["status"])

    def test_list_running_spans_pipelines(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(), "inst-cr")
        store.open_row("vm_usage", "refresh", "scheduled", _at(), "inst-vm")
        store.close_by_instance("vm_usage", "inst-vm", "completed", _at(6, 10))

        running = store.list_running()
        self.assertEqual(["cr_usage"], [run["pipeline"] for run in running])

    def test_prune_deletes_old_terminal_keeps_running_and_recent(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(1), "inst-old")
        store.close_by_instance("cr_usage", "inst-old", "completed", _at(1, 6, 12))
        store.open_row("cr_usage", "refresh", "scheduled", _at(1), "inst-old-running")
        store.open_row("cr_usage", "refresh", "scheduled", _at(3), "inst-recent")
        store.close_by_instance("cr_usage", "inst-recent", "completed", _at(3, 6, 12))

        deleted = store.prune(older_than=_at(2))

        self.assertEqual(1, deleted)
        remaining = {run["instanceId"] for run in store.list_by_pipeline("cr_usage")}
        self.assertEqual({"inst-old-running", "inst-recent"}, remaining)

    def test_rows_for_instance_finds_across_pipelines(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(), "inst-1")
        store.open_row("vm_usage", "refresh", "scheduled", _at(), "inst-2")

        rows = store.rows_for_instance("inst-1")
        self.assertEqual(["cr_usage"], [row["pipeline"] for row in rows])

    def test_delete_by_instance_removes_matching_rows(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(), "inst-1")
        store.close_by_instance("cr_usage", "inst-1", "completed", _at(6, 12))

        deleted = store.delete_by_instance("inst-1")

        self.assertEqual(1, deleted)
        self.assertEqual([], store.list_by_pipeline("cr_usage"))

    def test_delete_terminal_keeps_running(self):
        store, _ = self._store()
        store.open_row("cr_usage", "refresh", "scheduled", _at(1), "inst-done")
        store.close_by_instance("cr_usage", "inst-done", "completed", _at(1, 6, 12))
        store.open_row("vm_usage", "refresh", "scheduled", _at(3), "inst-running")

        deleted = store.delete_terminal()

        self.assertEqual(1, deleted)
        self.assertEqual([], store.list_by_pipeline("cr_usage"))
        self.assertEqual(["inst-running"], [r["instanceId"] for r in store.list_by_pipeline("vm_usage")])


if __name__ == "__main__":
    unittest.main()
