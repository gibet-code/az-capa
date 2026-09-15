from __future__ import annotations

import unittest
from datetime import datetime, timezone

from storage.scheduler_state_store import SchedulerStateStore
from tests.storage._fake_table import FakeTable


def _at(hour: int = 6, minute: int = 0) -> datetime:
    return datetime(2026, 9, 2, hour, minute, tzinfo=timezone.utc)


class SchedulerStateStoreTests(unittest.TestCase):
    def _store(self) -> tuple[SchedulerStateStore, FakeTable]:
        store = SchedulerStateStore(lambda: "SchedulerRunState")
        table = FakeTable()
        store._table = lambda: table
        return store, table

    def test_claim_creates_row_and_reads_busy(self):
        store, _ = self._store()

        self.assertTrue(store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at()))

        state = store.get("cr_usage")
        self.assertIsNotNone(state["activeInstanceId"])
        self.assertTrue(state["activeInstanceId"].startswith("pending:"))
        self.assertEqual("refresh", state["activeAction"])
        self.assertEqual("scheduled", state["lastTrigger"])
        self.assertEqual(_at(), state["lastDispatchedAt"])

    def test_second_claim_while_active_loses(self):
        store, _ = self._store()
        self.assertTrue(store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at()))

        self.assertFalse(store.try_claim("cr_usage", "cr_usage", "refresh", "manual", _at(7)))

    def test_claim_on_idle_existing_row_wins(self):
        store, _ = self._store()
        store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at())
        store.set_active_instance("cr_usage", "inst-1")
        store.close_run("cr_usage", "completed", _at(6, 12))

        self.assertTrue(store.try_claim("cr_usage", "cr_usage", "refresh", "manual", _at(7)))
        self.assertEqual("manual", store.get("cr_usage")["lastTrigger"])

    def test_concurrent_claim_on_idle_row_loses_on_etag(self):
        store, table = self._store()
        store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at())
        store.close_run("cr_usage", "completed", _at(6, 12))

        # Simulate another writer changing the row between our read and write.
        original_update = table.update_entity

        def racing_update(entity, *args, **kwargs):
            table.rows[("state", "cr_usage")]["ActiveInstanceId"] = "pending:other"
            table._bump(("state", "cr_usage"))
            return original_update(entity, *args, **kwargs)

        table.update_entity = racing_update
        self.assertFalse(store.try_claim("cr_usage", "cr_usage", "refresh", "manual", _at(7)))

    def test_set_active_instance_fills_real_id(self):
        store, _ = self._store()
        store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at())

        store.set_active_instance("cr_usage", "inst-42")

        self.assertEqual("inst-42", store.get("cr_usage")["activeInstanceId"])

    def test_release_claim_clears_active(self):
        store, _ = self._store()
        store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at())

        store.release_claim("cr_usage")

        state = store.get("cr_usage")
        self.assertIsNone(state["activeInstanceId"])
        self.assertIsNone(state["activeAction"])
        self.assertEqual(_at(), state["lastDispatchedAt"])

    def test_close_run_records_outcome_and_clears_active(self):
        store, _ = self._store()
        store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at())
        store.set_active_instance("cr_usage", "inst-1")

        store.close_run("cr_usage", "partial", _at(6, 15))

        state = store.get("cr_usage")
        self.assertIsNone(state["activeInstanceId"])
        self.assertEqual("partial", state["lastOutcome"])
        self.assertEqual(_at(6, 15), state["lastCompletedAt"])

    def test_list_returns_only_state_rows(self):
        store, _ = self._store()
        store.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", _at())
        store.try_claim("vm_usage", "vm_usage", "refresh", "scheduled", _at())
        store.record_tick(_at(6, 5))

        pipelines = {row["pipeline"] for row in store.list()}
        self.assertEqual({"cr_usage", "vm_usage"}, pipelines)

    def test_markers_round_trip_and_merge(self):
        store, _ = self._store()
        store.record_tick(_at(6, 5))
        store.record_tick(_at(6, 10), pruned_at=_at(6, 10))
        store.record_tick(_at(6, 15))

        marker = store.get_marker()
        self.assertEqual(_at(6, 15), marker["lastTickAt"])
        self.assertEqual(_at(6, 10), marker["lastPrunedAt"])


if __name__ == "__main__":
    unittest.main()
