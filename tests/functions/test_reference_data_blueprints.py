from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from functions._durable import run_catalogue_refresh
from functions.location_reference_bp import location_reference_refresh_activity


class ReferenceDataBlueprintTests(unittest.TestCase):
    def test_refresh_orchestrator_retries_transient_result_with_durable_timer(self):
        context = MagicMock()
        context.current_utc_datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
        generator = run_catalogue_refresh(context, "refresh_activity", max_attempts=2)

        activity = next(generator)
        self.assertEqual("refresh_activity", context.call_activity.call_args.args[0])
        timer = generator.send({"status": "transient", "retryAfter": 7})
        context.create_timer.assert_called_once()
        self.assertIs(timer, context.create_timer.return_value)
        activity = generator.send(None)
        self.assertIs(activity, context.call_activity.return_value)
        with self.assertRaises(StopIteration) as completed:
            generator.send({"status": "ok", "itemCount": 2})
        self.assertEqual("completed", completed.exception.value["status"])

    @patch("functions.location_reference_bp.location_pipeline")
    def test_location_activity_is_thin(self, pipeline):
        pipeline.refresh.return_value = {"status": "ok"}
        request = {"source": "test"}

        self.assertEqual({"status": "ok"}, location_reference_refresh_activity(request))
        pipeline.refresh.assert_called_once_with(request)


if __name__ == "__main__":
    unittest.main()