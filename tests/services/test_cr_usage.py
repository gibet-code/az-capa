from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from azure.data.tables import UpdateMode

from core import settings
from services import cr_usage
from storage.usage_store import UsageStore


RESOURCE_ID = (
    "/subscriptions/sub-a/resourceGroups/rg-a/providers/Microsoft.Compute/"
    "capacityReservationGroups/group-a/capacityReservations/reservation-a"
)


class UsageSummaryProjectionTests(unittest.TestCase):
    def test_existing_checkpoint_requires_one_projection_rebuild(self):
        store = UsageStore(lambda: "daily", lambda: "state", lambda: "summary")

        self.assertFalse(store.projection_is_current({}))
        self.assertTrue(store.projection_is_current({"projectionVersion": 1}))

    @patch.object(settings, "cost_finalization_lag_days", return_value=2)
    def test_upsert_projects_split_metrics_into_the_same_daily_slot(self, _lag):
        store = UsageStore(lambda: "daily", lambda: "state", lambda: "summary")
        daily_table = MagicMock()
        summary_table = MagicMock()
        store._data_table = MagicMock(return_value=daily_table)
        store._summary_table = MagicMock(return_value=summary_table)
        usage_date = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()

        store.upsert_daily_records([{
            "resourceId": RESOURCE_ID,
            "subscriptionId": "SUB-A",
            "date": usage_date,
            "currency": "EUR",
            "uptimeHours": 3.5,
        }], merge=True)
        store.upsert_daily_records([{
            "resourceId": RESOURCE_ID,
            "subscriptionId": "SUB-A",
            "date": usage_date,
            "currency": "EUR",
            "cost": 9.25,
        }], merge=True)

        first_summary = summary_table.submit_transaction.call_args_list[0].args[0][0]
        second_summary = summary_table.submit_transaction.call_args_list[1].args[0][0]
        slot = datetime.strptime(usage_date, "%Y-%m-%d").date().toordinal() % 30
        suffix = f"{slot:02d}"
        self.assertEqual("sub-a", first_summary[1]["PartitionKey"])
        self.assertEqual(3.5, first_summary[1][f"Hours{suffix}"])
        self.assertNotIn(f"Cost{suffix}", first_summary[1])
        self.assertEqual(9.25, second_summary[1][f"Cost{suffix}"])
        self.assertNotIn(f"Hours{suffix}", second_summary[1])
        self.assertEqual(UpdateMode.MERGE, first_summary[2]["mode"])


class UsageViewTests(unittest.TestCase):
    def _entity(self, coverage_end: str) -> dict:
        usage_date = datetime.strptime(coverage_end, "%Y-%m-%d").date()
        suffix = f"{usage_date.toordinal() % 30:02d}"
        return {
            "ResourceId": RESOURCE_ID,
            "Currency": "EUR",
            f"HoursDate{suffix}": coverage_end,
            f"Hours{suffix}": 3.5,
            f"CostDate{suffix}": coverage_end,
            f"Cost{suffix}": 9.25,
        }

    @patch.object(settings, "cost_finalization_lag_days", return_value=2)
    def test_usage_view_returns_shared_currency_dates_and_zero_filled_series(self, _lag):
        state = {"coverageStart": "2026-07-22", "coverageEnd": "2026-08-20", "projectionVersion": 1}
        with patch.object(cr_usage._store, "get_state", return_value=state), patch.object(
            cr_usage._store, "query_usage_summaries", return_value=[self._entity("2026-08-20")]
        ):
            metadata, rows = cr_usage.usage_view(["sub-a"])

        row = rows[RESOURCE_ID.lower()]
        self.assertEqual("EUR", metadata["usage"]["currency"])
        self.assertEqual(2, metadata["usage"]["finalizationLagDays"])
        self.assertEqual(30, len(metadata["usage"]["dates"]))
        self.assertEqual(30, len(row["unusedHours"]))
        self.assertEqual(3.5, row["lastDayUnusedHours"])
        self.assertEqual(3.5, row["last7DaysUnusedHours"])
        self.assertEqual(3.5, row["last30DaysUnusedHours"])
        self.assertEqual("reservation-a", row["name"])
        self.assertEqual("group-a", row["capacityReservationGroupName"])

    def test_usage_view_marks_incomplete_windows_unknown(self):
        state = {"coverageStart": "2026-08-15", "coverageEnd": "2026-08-20", "projectionVersion": 1}
        with patch.object(cr_usage._store, "get_state", return_value=state), patch.object(
            cr_usage._store, "query_usage_summaries", return_value=[self._entity("2026-08-20")]
        ):
            _, rows = cr_usage.usage_view(["sub-a"])

        row = rows[RESOURCE_ID.lower()]
        self.assertEqual(3.5, row["lastDayUnusedHours"])
        self.assertIsNone(row["last7DaysUnusedHours"])
        self.assertIsNone(row["last30DaysUnusedCost"])

    def test_incomplete_projection_is_not_visible(self):
        state = {"coverageStart": "2026-07-22", "coverageEnd": "2026-08-20"}
        with patch.object(cr_usage._store, "get_state", return_value=state), patch.object(
            cr_usage._store, "query_usage_summaries"
        ) as query_summaries:
            metadata, rows = cr_usage.usage_view(["sub-a"])

        self.assertFalse(metadata["usage"]["isAvailable"])
        self.assertEqual({}, rows)
        self.assertTrue(all(value is None for value in cr_usage.empty_usage(metadata)["unusedHours"]))
        query_summaries.assert_not_called()


if __name__ == "__main__":
    unittest.main()