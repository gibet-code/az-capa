from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from services.data_collection import cadence

# Daily at 06:30 UTC.
_DAILY = 86400
_ANCHOR_0630 = 23400
# Every 15 minutes on the :00/:15/:30/:45 grid.
_QUARTER_HOUR = 900


def _utc(year=2026, month=9, day=2, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class SlotStartTests(unittest.TestCase):
    def test_daily_slot_lands_on_anchor(self):
        slot = cadence.slot_start_epoch(_utc(hour=8), _ANCHOR_0630, _DAILY)
        self.assertEqual(_utc(hour=6, minute=30), datetime.fromtimestamp(slot, tz=timezone.utc))

    def test_before_anchor_belongs_to_previous_day(self):
        slot = cadence.slot_start_epoch(_utc(hour=5), _ANCHOR_0630, _DAILY)
        self.assertEqual(
            _utc(day=1, hour=6, minute=30), datetime.fromtimestamp(slot, tz=timezone.utc)
        )

    def test_quarter_hour_grid(self):
        slot = cadence.slot_start_epoch(_utc(hour=8, minute=52), 0, _QUARTER_HOUR)
        self.assertEqual(_utc(hour=8, minute=45), datetime.fromtimestamp(slot, tz=timezone.utc))


class IsDueTests(unittest.TestCase):
    def test_never_dispatched_is_due(self):
        self.assertTrue(cadence.is_due(_utc(hour=7), None, _ANCHOR_0630, _DAILY))

    def test_dispatched_this_slot_not_due(self):
        now = _utc(hour=8)
        last = _utc(hour=6, minute=30)  # exactly on this slot boundary
        self.assertFalse(cadence.is_due(now, last, _ANCHOR_0630, _DAILY))

    def test_dispatched_previous_slot_is_due(self):
        now = _utc(hour=8)
        last = _utc(day=1, hour=6, minute=31)  # yesterday's run
        self.assertTrue(cadence.is_due(now, last, _ANCHOR_0630, _DAILY))

    def test_no_drift_when_dispatch_is_late(self):
        # Slot at 06:30 but dispatch happened late at 06:47; next day still 06:30.
        anchor, freq = _ANCHOR_0630, _DAILY
        last = _utc(day=2, hour=6, minute=47)
        # Same day, after the late run: not due again.
        self.assertFalse(cadence.is_due(_utc(day=2, hour=23), last, anchor, freq))
        # Next day at the anchor: due again, exactly one period later (no drift).
        self.assertTrue(cadence.is_due(_utc(day=3, hour=6, minute=31), last, anchor, freq))

    def test_manual_only_pipeline_never_due(self):
        self.assertFalse(cadence.is_due(_utc(hour=7), None, 0, 0))
        self.assertFalse(cadence.is_due(_utc(hour=7), _utc(day=1), 0, None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
