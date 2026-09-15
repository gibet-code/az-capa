from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from functions.subscription_context_bp import (
    _run_context_refresh,
    subscription_context_flush_activity,
    subscription_context_refresh_activity,
)


class SubscriptionContextRefreshBlueprintTests(unittest.IsolatedAsyncioTestCase):
    @patch("functions.subscription_context_bp.context_service")
    def test_refresh_activity_is_thin(self, service):
        service.refresh.return_value = {"status": "ok"}
        request = {"refreshRunId": "run-1", "scheduled": True}

        self.assertEqual({"status": "ok"}, subscription_context_refresh_activity(request))
        service.refresh.assert_called_once_with(request)

    @patch("functions.subscription_context_bp.context_service")
    def test_flush_activity_is_thin(self, service):
        service.flush.return_value = {"status": "ok"}

        self.assertEqual({"status": "ok"}, subscription_context_flush_activity({}))
        service.flush.assert_called_once_with({})

    def test_orchestrator_passes_instance_id_without_mapping_payloads(self):
        context = MagicMock()
        context.instance_id = "run-7"
        context.current_utc_datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
        context.get_input.return_value = {"scheduled": True}
        generator = _run_context_refresh(context)

        activity = next(generator)
        self.assertIs(activity, context.call_activity.return_value)
        context.call_activity.assert_called_once_with(
            "subscription_context_refresh_activity",
            {"refreshRunId": "run-7", "scheduled": True, "finalAttempt": False},
        )
        with self.assertRaises(StopIteration) as completed:
            generator.send({"status": "ok"})
        self.assertEqual({"status": "ok"}, completed.exception.value)

    def test_orchestrator_uses_durable_timer_for_transient_failure(self):
        context = MagicMock()
        context.instance_id = "run-8"
        context.current_utc_datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
        context.get_input.return_value = {"scheduled": True}
        generator = _run_context_refresh(context)

        next(generator)
        timer = generator.send({"status": "transient"})

        self.assertIs(timer, context.create_timer.return_value)
        context.create_timer.assert_called_once()


if __name__ == "__main__":
    unittest.main()