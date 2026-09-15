from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core import settings
from services.data_collection import registry
from services.data_collection.dispatcher import (
    ConfigUpdateError,
    DataCollectionDispatcher,
    RunStatus,
    normalize_progress,
)
from storage.scheduler_config_store import SchedulerConfigStore
from storage.scheduler_history_store import SchedulerHistoryStore
from storage.scheduler_state_store import SchedulerStateStore
from tests.storage._fake_table import FakeTable

_NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


class FakeDurableClient:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.statuses: dict[str, RunStatus] = {}
        self._seq = 0
        self.fail_orchestrators: set[str] = set()

    async def start_new(self, orchestrator: str, client_input: dict | None = None) -> str:
        if orchestrator in self.fail_orchestrators:
            raise RuntimeError(f"start_new failed for {orchestrator}")
        self._seq += 1
        instance_id = f"inst-{self._seq}"
        self.started.append((orchestrator, instance_id))
        self.statuses.setdefault(instance_id, RunStatus("Running"))
        return instance_id

    async def get_status(self, instance_id: str) -> RunStatus | None:
        return self.statuses.get(instance_id)

    def started_orchestrators(self) -> set[str]:
        return {orchestrator for orchestrator, _ in self.started}


class DispatcherTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = SchedulerStateStore(lambda: "SchedulerRunState")
        state_table = FakeTable()
        self.state._table = lambda: state_table
        self.history = SchedulerHistoryStore(lambda: "SchedulerRunHistory")
        history_table = FakeTable()
        self.history._table = lambda: history_table
        self.config = SchedulerConfigStore(lambda: "SchedulerConfig")
        config_table = FakeTable()
        self.config._table = lambda: config_table
        self.client = FakeDurableClient()
        self.now = _NOW
        self.dispatcher = DataCollectionDispatcher(
            state_store=self.state,
            history_store=self.history,
            config_store=self.config,
            clock=lambda: self.now,
        )

    def _disable(self, pipeline_id: str) -> None:
        self.config.patch(
            pipeline_id,
            enabled=False,
            frequency_seconds=registry.get(pipeline_id).default_frequency_seconds,
            anchor_seconds=registry.get(pipeline_id).default_anchor_seconds,
            expected_revision=None,
            updated_by={"objectId": "admin"},
            updated_at=self.now,
        )


class StartTests(DispatcherTestBase):
    async def test_start_records_state_and_history(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        self.assertEqual("started", result.status)
        self.assertEqual(1, len(self.client.started))
        state = self.state.get("cr_usage")
        self.assertEqual(result.instance_id, state["activeInstanceId"])
        self.assertEqual("manual", state["lastTrigger"])
        run = self.history.list_by_pipeline("cr_usage")[0]
        self.assertEqual("running", run["status"])
        self.assertEqual(result.instance_id, run["instanceId"])

    async def test_second_start_is_busy(self):
        await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        self.assertEqual("busy", result.status)
        self.assertEqual(1, len(self.client.started))

    async def test_lost_claim_returns_busy_without_start(self):
        # Force the atomic claim to lose (simulating a concurrent winner).
        self.state.try_claim = lambda *args, **kwargs: False

        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        self.assertEqual("busy", result.status)
        self.assertEqual([], self.client.started)

    async def test_start_new_failure_releases_claim(self):
        self.client.fail_orchestrators.add("cr_usage_refresh_orchestrator")

        with self.assertRaises(RuntimeError):
            await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        # Scope must not be wedged busy after a failed launch.
        self.assertIsNone(self.state.get("cr_usage")["activeInstanceId"])
        self.assertEqual([], self.history.list_by_pipeline("cr_usage"))

    async def test_scheduled_refresh_skipped_when_disabled(self):
        self._disable("cr_usage")

        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "scheduled")

        self.assertEqual("disabled", result.status)
        self.assertEqual([], self.client.started)

    async def test_manual_refresh_allowed_when_disabled(self):
        self._disable("cr_usage")

        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        self.assertEqual("started", result.status)

    async def test_flush_on_non_flush_pipeline_raises(self):
        with self.assertRaises(ValueError):
            await self.dispatcher.start(self.client, "subscription_reference", "flush", "manual")

    async def test_start_unknown_pipeline_raises_keyerror(self):
        with self.assertRaises(KeyError):
            await self.dispatcher.start(self.client, "nope", "refresh", "manual")


class ScopeBusyTests(DispatcherTestBase):
    async def test_idle_scope_not_busy(self):
        self.assertFalse(await self.dispatcher.scope_busy(self.client, "cr_usage"))

    async def test_pending_placeholder_is_busy(self):
        self.state.try_claim("cr_usage", "cr_usage", "refresh", "manual", self.now)
        self.assertTrue(await self.dispatcher.scope_busy(self.client, "cr_usage"))

    async def test_running_instance_is_busy(self):
        await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.assertTrue(await self.dispatcher.scope_busy(self.client, "cr_usage"))

    async def test_terminal_instance_self_heals(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.client.statuses[result.instance_id] = RunStatus("Completed", output={"upserted": 3})

        busy = await self.dispatcher.scope_busy(self.client, "cr_usage")

        self.assertFalse(busy)
        self.assertIsNone(self.state.get("cr_usage")["activeInstanceId"])
        run = self.history.list_by_pipeline("cr_usage")[0]
        self.assertEqual("completed", run["status"])
        self.assertEqual({"upserted": 3}, run["summary"])

    async def test_missing_status_self_heals_as_failed(self):
        await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.client.statuses.clear()  # instance vanished (worker crash)

        busy = await self.dispatcher.scope_busy(self.client, "cr_usage")

        self.assertFalse(busy)
        self.assertEqual("failed", self.state.get("cr_usage")["lastOutcome"])


class TickTests(DispatcherTestBase):
    async def test_cold_start_dispatches_all_due_in_priority_order(self):
        await self.dispatcher.tick(self.client)

        started = self.client.started_orchestrators()
        # One cost_management pipeline yields to the other within a tick.
        self.assertIn("zone_mapping_refresh_orchestrator", started)
        self.assertIn("subscription_reference_refresh_orchestrator", started)
        # Subscription Reference (lowest priority) warms first.
        self.assertEqual("subscription_reference_refresh_orchestrator", self.client.started[0][0])

    async def test_cost_management_group_serialized_to_one_per_tick(self):
        await self.dispatcher.tick(self.client)

        started = self.client.started_orchestrators()
        cost_started = {
            "cr_usage_refresh_orchestrator",
            "vm_usage_refresh_orchestrator",
        } & started
        self.assertEqual(1, len(cost_started))
        # cr_usage has the lower priority number, so it wins.
        self.assertIn("cr_usage_refresh_orchestrator", started)
        self.assertNotIn("vm_usage_refresh_orchestrator", started)

    async def test_disabled_pipeline_not_dispatched(self):
        self._disable("activity_log")

        await self.dispatcher.tick(self.client)

        self.assertNotIn(
            "activity_log_refresh_orchestrator", self.client.started_orchestrators()
        )

    async def test_not_due_pipeline_skipped(self):
        # Mark cr_usage as already dispatched this slot.
        self.state.try_claim("cr_usage", "cr_usage", "refresh", "scheduled", self.now)
        self.state.close_run("cr_usage", "completed", self.now)

        await self.dispatcher.tick(self.client)

        self.assertNotIn("cr_usage_refresh_orchestrator", self.client.started_orchestrators())

    async def test_reconcile_closes_orphaned_terminal_run(self):
        result = await self.dispatcher.start(self.client, "activity_log", "refresh", "manual")
        # The run died after starting; only Durable knows it finished.
        self.client.statuses[result.instance_id] = RunStatus("Failed")
        self.now = _NOW + timedelta(minutes=5)

        await self.dispatcher.tick(self.client)

        state = self.state.get("activity_log")
        self.assertIsNone(state["activeInstanceId"])
        self.assertEqual("failed", state["lastOutcome"])
        self.assertEqual("failed", self.history.list_by_pipeline("activity_log")[0]["status"])

    async def test_reconcile_skips_pending_placeholder(self):
        self.state.try_claim("cr_usage", "cr_usage", "refresh", "manual", self.now)

        await self.dispatcher.tick(self.client)

        # The in-flight placeholder must not be cleared by the tick reconciler.
        self.assertTrue(self.state.get("cr_usage")["activeInstanceId"].startswith("pending:"))

    async def test_active_group_member_blocks_dispatch(self):
        # cr_usage already running blocks vm_usage (same contention group) this tick.
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.assertEqual("started", result.status)

        await self.dispatcher.tick(self.client)

        self.assertNotIn("vm_usage_refresh_orchestrator", self.client.started_orchestrators())

    async def test_heartbeat_recorded_each_tick(self):
        await self.dispatcher.tick(self.client)
        self.assertEqual(self.now, self.state.get_marker()["lastTickAt"])

    async def test_prune_throttled_to_once_per_day(self):
        calls = []
        original_prune = self.history.prune
        self.history.prune = lambda older_than: (calls.append(older_than), original_prune(older_than))[1]

        await self.dispatcher.tick(self.client)
        await self.dispatcher.tick(self.client)  # same clock, within a day

        self.assertEqual(1, len(calls))
        self.assertEqual(self.now, self.state.get_marker()["lastPrunedAt"])

    async def test_one_pipeline_failure_does_not_abort_tick(self):
        # Fault isolation: a failing pipeline must not stop the others or the heartbeat.
        self.client.fail_orchestrators.add("compute_skus_refresh_orchestrator")

        await self.dispatcher.tick(self.client)

        started = self.client.started_orchestrators()
        self.assertNotIn("compute_skus_refresh_orchestrator", started)
        self.assertIn("subscription_reference_refresh_orchestrator", started)
        self.assertIn("activity_log_refresh_orchestrator", started)
        self.assertEqual(self.now, self.state.get_marker()["lastTickAt"])
        # The failed launch released its claim rather than wedging the scope.
        self.assertIsNone(self.state.get("compute_skus")["activeInstanceId"])


class DispatchCapTests(DispatcherTestBase):
    async def test_cap_limits_starts_per_tick_in_priority_order(self):
        with mock.patch.object(settings, "data_collection_max_dispatch_per_tick", return_value=2):
            await self.dispatcher.tick(self.client)

        self.assertEqual(2, len(self.client.started))
        self.assertEqual(
            [
                "subscription_reference_refresh_orchestrator",
                "location_reference_refresh_orchestrator",
            ],
            [orchestrator for orchestrator, _ in self.client.started],
        )

    async def test_capped_pipelines_dispatch_on_next_tick(self):
        with mock.patch.object(settings, "data_collection_max_dispatch_per_tick", return_value=2):
            await self.dispatcher.tick(self.client)
            self.assertNotIn(
                "compute_skus_refresh_orchestrator", self.client.started_orchestrators()
            )

            self.now = _NOW + timedelta(minutes=1)
            await self.dispatcher.tick(self.client)

        # Already-running catalogues stay busy/not-due, so the freed slots go to the next batch.
        self.assertIn("compute_skus_refresh_orchestrator", self.client.started_orchestrators())

    async def test_zero_cap_means_unlimited(self):
        with mock.patch.object(settings, "data_collection_max_dispatch_per_tick", return_value=0):
            await self.dispatcher.tick(self.client)

        # More than any small cap would allow — the whole due set launches at once.
        self.assertGreater(len(self.client.started), 2)

    async def test_heartbeat_still_recorded_when_capped(self):
        with mock.patch.object(settings, "data_collection_max_dispatch_per_tick", return_value=1):
            await self.dispatcher.tick(self.client)
        self.assertEqual(self.now, self.state.get_marker()["lastTickAt"])


class RecordOutcomeTests(DispatcherTestBase):
    async def test_record_outcome_closes_run(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        self.dispatcher.record_outcome(
            "cr_usage", result.instance_id, "completed", {"upserted": 10}
        )

        state = self.state.get("cr_usage")
        self.assertIsNone(state["activeInstanceId"])
        self.assertEqual("completed", state["lastOutcome"])
        run = self.history.list_by_pipeline("cr_usage")[0]
        self.assertEqual("completed", run["status"])
        self.assertEqual({"upserted": 10}, run["summary"])


class EffectiveConfigTests(DispatcherTestBase):
    def test_defaults_when_no_row(self):
        descriptor = registry.get("cr_usage")
        config = self.dispatcher.effective_config(descriptor)
        self.assertEqual(descriptor.default_enabled, config.enabled)
        self.assertEqual(descriptor.default_frequency_seconds, config.frequency_seconds)
        self.assertEqual(descriptor.default_anchor_seconds, config.anchor_seconds)
        self.assertEqual(0, config.revision)

    def test_config_row_overrides_defaults(self):
        self.config.patch(
            "cr_usage",
            enabled=False,
            frequency_seconds=3600,
            anchor_seconds=0,
            expected_revision=None,
            updated_by={"objectId": "admin"},
            updated_at=self.now,
        )
        config = self.dispatcher.effective_config(registry.get("cr_usage"))
        self.assertFalse(config.enabled)
        self.assertEqual(3600, config.frequency_seconds)
        self.assertEqual(1, config.revision)


class NotificationsTests(DispatcherTestBase):
    async def test_running_run_surfaces_with_live_progress(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.client.statuses[result.instance_id] = RunStatus(
            "Running", custom_status={"completedSubscriptions": 2, "totalSubscriptions": 5, "percentCompleted": 40}
        )

        items = await self.dispatcher.notifications(self.client, self.now - timedelta(days=7))

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertTrue(item["isRunning"])
        self.assertEqual("Running", item["runtimeStatus"])
        self.assertEqual("cr_usage", item["kind"])
        self.assertEqual("CR usage", item["section"])
        self.assertEqual("Refresh", item["operation"])
        self.assertEqual(40, item["progress"]["percentCompleted"])
        self.assertIsNotNone(item["startedSinceSeconds"])

    async def test_terminal_run_within_window_is_listed(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", result.instance_id, "completed", {"upserted": 9})

        items = await self.dispatcher.notifications(self.client, self.now - timedelta(days=7))

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertFalse(item["isRunning"])
        self.assertEqual("Completed", item["runtimeStatus"])
        self.assertEqual(0, item["durationSeconds"])
        self.assertIsNotNone(item["completedOn"])

    async def test_failed_outcome_maps_to_failed_runtime(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", result.instance_id, "failed", {"error": "boom"})

        items = await self.dispatcher.notifications(self.client, self.now - timedelta(days=7))

        self.assertEqual("Failed", items[0]["runtimeStatus"])

    async def test_running_row_with_dead_instance_self_heals(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        # Instance vanished from the hub (e.g., purged/crashed) → status None.
        self.client.statuses.pop(result.instance_id)

        items = await self.dispatcher.notifications(self.client, self.now - timedelta(days=7))

        self.assertEqual(1, len(items))
        self.assertFalse(items[0]["isRunning"])
        self.assertIsNone(self.state.get("cr_usage")["activeInstanceId"])
        self.assertEqual("failed", self.history.list_by_pipeline("cr_usage")[0]["status"])

    async def test_long_running_run_outside_window_still_shows(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        # A window that starts after this run began must not hide the running run.
        items = await self.dispatcher.notifications(self.client, self.now + timedelta(days=1))

        self.assertEqual(1, len(items))
        self.assertTrue(items[0]["isRunning"])


class DeleteRunTests(DispatcherTestBase):
    async def test_delete_missing_run_returns_not_found(self):
        self.assertEqual("not_found", await self.dispatcher.delete_run(self.client, "nope"))

    async def test_delete_running_run_is_refused(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")

        outcome = await self.dispatcher.delete_run(self.client, result.instance_id)

        self.assertEqual("running", outcome)
        self.assertEqual(1, len(self.history.list_by_pipeline("cr_usage")))

    async def test_delete_terminal_run_removes_row(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", result.instance_id, "completed")

        outcome = await self.dispatcher.delete_run(self.client, result.instance_id)

        self.assertEqual("deleted", outcome)
        self.assertEqual([], self.history.list_by_pipeline("cr_usage"))

    async def test_clear_terminal_runs_keeps_running(self):
        done = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", done.instance_id, "completed")
        await self.dispatcher.start(self.client, "vm_usage", "refresh", "manual")

        deleted = self.dispatcher.clear_terminal_runs()

        self.assertEqual(1, deleted)
        self.assertEqual([], self.history.list_by_pipeline("cr_usage"))
        self.assertEqual(1, len(self.history.list_by_pipeline("vm_usage")))


class ListPipelinesTests(DispatcherTestBase):
    async def test_lists_every_pipeline_with_registry_defaults(self):
        pipelines = await self.dispatcher.list_pipelines(self.client)

        self.assertEqual(len(registry.all_descriptors()), len(pipelines))
        first = pipelines[0]
        # Subscription Reference has the lowest priority, so it leads the list.
        self.assertEqual("subscription_reference", first["id"])
        self.assertFalse(first["canDisable"])
        self.assertTrue(first["enabled"])
        self.assertFalse(first["running"])
        self.assertIsNone(first["nextDueAt"])
        self.assertIsNone(first["lastDispatchedAt"])

    async def test_running_pipeline_reports_live_progress(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.client.statuses[result.instance_id] = RunStatus(
            "Running", custom_status={"complete": 0.5, "label": "halfway"}
        )

        pipelines = await self.dispatcher.list_pipelines(self.client)
        cr = next(p for p in pipelines if p["id"] == "cr_usage")

        self.assertTrue(cr["running"])
        self.assertEqual({"complete": 0.5, "label": "halfway"}, cr["progress"])

    async def test_next_due_computed_from_last_dispatch_when_enabled(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", result.instance_id, "completed")

        pipelines = await self.dispatcher.list_pipelines(self.client)
        cr = next(p for p in pipelines if p["id"] == "cr_usage")

        expected = (self.now + timedelta(seconds=cr["frequencySeconds"])).isoformat()
        self.assertFalse(cr["running"])
        self.assertEqual("completed", cr["lastOutcome"])
        self.assertEqual(expected, cr["nextDueAt"])

    async def test_next_due_null_when_disabled(self):
        self._disable("activity_log")

        pipelines = await self.dispatcher.list_pipelines(self.client)
        activity = next(p for p in pipelines if p["id"] == "activity_log")

        self.assertFalse(activity["enabled"])
        self.assertIsNone(activity["nextDueAt"])

    async def test_orphaned_running_row_self_heals_in_list(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        # The run died; Durable reports it terminal.
        self.client.statuses[result.instance_id] = RunStatus("Failed")

        pipelines = await self.dispatcher.list_pipelines(self.client)
        cr = next(p for p in pipelines if p["id"] == "cr_usage")

        self.assertFalse(cr["running"])
        self.assertIsNone(self.state.get("cr_usage")["activeInstanceId"])


class UpdateConfigTests(DispatcherTestBase):
    _WHO = {"objectId": "admin", "name": "Admin", "tenantId": "t"}

    async def test_creates_config_row_on_first_edit(self):
        row = self.dispatcher.update_config(
            "cr_usage",
            enabled=True,
            frequency_seconds=3600,
            anchor_seconds=0,
            expected_revision=None,
            updated_by=self._WHO,
        )

        self.assertEqual(1, row["revision"])
        config = self.dispatcher.effective_config(registry.get("cr_usage"))
        self.assertEqual(3600, config.frequency_seconds)

    async def test_stale_revision_rejected(self):
        self.dispatcher.update_config(
            "cr_usage",
            enabled=True,
            frequency_seconds=3600,
            anchor_seconds=0,
            expected_revision=None,
            updated_by=self._WHO,
        )
        with self.assertRaises(ConfigUpdateError) as ctx:
            self.dispatcher.update_config(
                "cr_usage",
                enabled=True,
                frequency_seconds=7200,
                anchor_seconds=0,
                expected_revision=None,  # should be 1 now
                updated_by=self._WHO,
            )
        self.assertEqual("stale_revision", ctx.exception.code)

    async def test_cannot_disable_infrastructural_pipeline(self):
        with self.assertRaises(ConfigUpdateError) as ctx:
            self.dispatcher.update_config(
                "subscription_reference",
                enabled=False,
                frequency_seconds=900,
                anchor_seconds=0,
                expected_revision=None,
                updated_by=self._WHO,
            )
        self.assertEqual("cannot_disable", ctx.exception.code)

    async def test_frequency_below_minimum_rejected(self):
        with self.assertRaises(ConfigUpdateError) as ctx:
            self.dispatcher.update_config(
                "cr_usage",
                enabled=True,
                frequency_seconds=60,
                anchor_seconds=0,
                expected_revision=None,
                updated_by=self._WHO,
            )
        self.assertEqual("invalid_frequency", ctx.exception.code)

    async def test_zero_frequency_allowed_as_manual(self):
        row = self.dispatcher.update_config(
            "zone_mapping",
            enabled=True,
            frequency_seconds=0,
            anchor_seconds=0,
            expected_revision=None,
            updated_by=self._WHO,
        )
        self.assertEqual(0, row["frequencySeconds"])

    async def test_invalid_anchor_rejected(self):
        with self.assertRaises(ConfigUpdateError) as ctx:
            self.dispatcher.update_config(
                "cr_usage",
                enabled=True,
                frequency_seconds=3600,
                anchor_seconds=90000,
                expected_revision=None,
                updated_by=self._WHO,
            )
        self.assertEqual("invalid_anchor", ctx.exception.code)

    async def test_unknown_pipeline_rejected(self):
        with self.assertRaises(ConfigUpdateError) as ctx:
            self.dispatcher.update_config(
                "nope",
                enabled=True,
                frequency_seconds=3600,
                anchor_seconds=0,
                expected_revision=None,
                updated_by=self._WHO,
            )
        self.assertEqual("unknown_pipeline", ctx.exception.code)


class NormalizeProgressTests(unittest.TestCase):
    def test_none_and_non_dict_yield_none(self):
        self.assertIsNone(normalize_progress(None))
        self.assertIsNone(normalize_progress("running"))

    def test_already_normalized_shape_passes_through_clamped(self):
        self.assertEqual(
            {"complete": 0.5, "label": "halfway"},
            normalize_progress({"complete": 0.5, "label": "halfway"}),
        )
        # Out-of-range complete clamps to [0, 1]; non-string label drops to None.
        self.assertEqual({"complete": 1.0, "label": None}, normalize_progress({"complete": 4, "label": 7}))

    def test_per_subscription_shape_maps_to_common_shape(self):
        self.assertEqual(
            {"complete": 0.4, "label": "2/5 subscriptions completed"},
            normalize_progress(
                {"completedSubscriptions": 2, "totalSubscriptions": 5, "percentCompleted": 40}
            ),
        )

    def test_indeterminate_shape_yields_none(self):
        self.assertIsNone(normalize_progress({"foo": "bar"}))


class PipelineStatusTests(DispatcherTestBase):
    async def test_envelope_wraps_detail_and_scheduling_fields(self):
        detail = {"hasData": True, "finalizedThrough": "2026-09-01"}

        envelope = await self.dispatcher.pipeline_status(self.client, "cr_usage", detail)

        self.assertEqual("cr_usage", envelope["id"])
        self.assertEqual("CR usage", envelope["name"])
        self.assertTrue(envelope["enabled"])
        self.assertFalse(envelope["running"])
        self.assertIsNone(envelope["progress"])
        self.assertEqual(detail, envelope["detail"])

    async def test_running_pipeline_reports_normalized_progress(self):
        result = await self.dispatcher.start(self.client, "activity_log", "refresh", "manual")
        self.client.statuses[result.instance_id] = RunStatus(
            "Running",
            custom_status={"completedSubscriptions": 8, "totalSubscriptions": 10, "percentCompleted": 80},
        )

        envelope = await self.dispatcher.pipeline_status(self.client, "activity_log", {})

        self.assertTrue(envelope["running"])
        self.assertEqual(
            {"complete": 0.8, "label": "8/10 subscriptions completed"}, envelope["progress"]
        )

    async def test_next_due_from_last_dispatch_when_enabled(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", result.instance_id, "completed")

        envelope = await self.dispatcher.pipeline_status(self.client, "cr_usage", {})

        expected = (self.now + timedelta(seconds=envelope["frequencySeconds"])).isoformat()
        self.assertEqual("completed", envelope["lastOutcome"])
        self.assertEqual(expected, envelope["nextDueAt"])

    async def test_orphaned_running_row_self_heals(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.client.statuses[result.instance_id] = RunStatus("Failed")

        envelope = await self.dispatcher.pipeline_status(self.client, "cr_usage", {})

        self.assertFalse(envelope["running"])
        self.assertIsNone(self.state.get("cr_usage")["activeInstanceId"])


class RunHistoryTests(DispatcherTestBase):
    async def test_terminal_rows_use_stored_summary_newest_first(self):
        first = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.dispatcher.record_outcome("cr_usage", first.instance_id, "completed", {"upserted": 5})
        self.now = _NOW + timedelta(minutes=1)
        second = await self.dispatcher.start(self.client, "cr_usage", "refresh", "scheduled")
        self.dispatcher.record_outcome("cr_usage", second.instance_id, "failed", {"error": "boom"})

        result = await self.dispatcher.run_history(self.client, "cr_usage")

        self.assertEqual("cr_usage", result["pipeline"])
        self.assertEqual(2, len(result["runs"]))
        # Newest first: the second (failed) run leads.
        self.assertEqual("failed", result["runs"][0]["status"])
        self.assertEqual({"error": "boom"}, result["runs"][0]["summary"])
        self.assertEqual("completed", result["runs"][1]["status"])
        self.assertEqual({"upserted": 5}, result["runs"][1]["summary"])
        self.assertIsNone(result["nextContinuation"])

    async def test_running_row_enriched_with_live_progress(self):
        result = await self.dispatcher.start(self.client, "activity_log", "refresh", "manual")
        self.client.statuses[result.instance_id] = RunStatus(
            "Running",
            custom_status={"completedSubscriptions": 3, "totalSubscriptions": 4, "percentCompleted": 75},
        )

        history = await self.dispatcher.run_history(self.client, "activity_log")

        run = history["runs"][0]
        self.assertEqual("running", run["status"])
        self.assertEqual("Running", run["runtimeStatus"])
        self.assertEqual({"complete": 0.75, "label": "3/4 subscriptions completed"}, run["progress"])

    async def test_running_row_with_dead_instance_self_heals(self):
        result = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
        self.client.statuses.pop(result.instance_id)

        history = await self.dispatcher.run_history(self.client, "cr_usage")

        run = history["runs"][0]
        self.assertEqual("failed", run["status"])
        self.assertIsNone(self.state.get("cr_usage")["activeInstanceId"])

    async def test_limit_and_continuation_paging(self):
        keys = []
        for minute in range(3):
            self.now = _NOW + timedelta(minutes=minute)
            started = await self.dispatcher.start(self.client, "cr_usage", "refresh", "manual")
            self.dispatcher.record_outcome("cr_usage", started.instance_id, "completed")

        page1 = await self.dispatcher.run_history(self.client, "cr_usage", limit=2)
        self.assertEqual(2, len(page1["runs"]))
        self.assertIsNotNone(page1["nextContinuation"])

        page2 = await self.dispatcher.run_history(
            self.client, "cr_usage", limit=2, after=page1["nextContinuation"]
        )
        self.assertEqual(1, len(page2["runs"]))
        self.assertIsNone(page2["nextContinuation"])
        # No overlap between pages.
        page1_keys = {run["rowKey"] for run in page1["runs"]}
        page2_keys = {run["rowKey"] for run in page2["runs"]}
        self.assertEqual(set(), page1_keys & page2_keys)


if __name__ == "__main__":
    unittest.main()
