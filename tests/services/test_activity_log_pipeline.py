from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from services.activity_log import recipes
from services.activity_log.pipeline import (
    ASSEMBLER_VERSION,
    ActivityLogPipeline,
    _prepare_event,
    _reconcile_operation,
    _row_needs_reconcile,
    assemble_operations,
)
from services.activity_log.projection import compress_payload
from datetime import timedelta

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
_CUTOFF = "2026-09-07T11:40:00Z"  # now - 20 min ingestion lag
_HISTORICAL_START = "2026-08-08T11:40:00Z"  # cutoff - 30 days


def _vm_event(event_id: str, status: str, correlation: str = "corr-1", body: dict | None = None) -> dict:
    properties = {}
    if body is not None:
        properties["responseBody"] = json.dumps(body)
    return {
        "eventDataId": event_id,
        "eventTimestamp": "2026-09-01T00:00:00Z",
        "submissionTimestamp": "2026-09-01T00:00:01Z",
        "subscriptionId": "sub-1",
        "resourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        "resourceType": "microsoft.compute/virtualmachines",
        "operationName": "microsoft.compute/virtualmachines/write",
        "operationId": "op",
        "correlationId": correlation,
        "status": status,
        "properties": properties,
    }


class AssembleOperationsTests(unittest.TestCase):
    def test_prepare_event_returns_none_for_unmatched_event(self):
        event = _vm_event("e1", "Accepted")
        event["operationName"] = "microsoft.storage/storageaccounts/write"
        event["resourceType"] = "microsoft.storage/storageaccounts"

        self.assertIsNone(_prepare_event(event))

    def test_assemble_projects_columns_and_stamps_versions(self):
        events = [
            _vm_event("e1", "Accepted", body={
                "location": "eastus",
                "properties": {"hardwareProfile": {"vmSize": "Standard_D2s_v5"}},
            }),
            _vm_event("e2", "Succeeded"),
        ]

        operations = assemble_operations(events)

        self.assertEqual(1, len(operations))
        operation = operations[0]
        self.assertEqual("Standard_D2s_v5", operation["Columns"]["VmSize"])
        self.assertEqual("eastus", operation["Columns"]["Location"])
        self.assertEqual("succeeded", operation["Outcome"])
        self.assertEqual(ASSEMBLER_VERSION, operation["AssemblerVersion"])
        self.assertEqual(1, operation["RecipeVersion"])
        self.assertIsNotNone(operation["AppliedPayloadGz"])

    def test_lifecycle_events_are_not_payload_retained(self):
        accepted = _vm_event("l1", "Accepted")
        accepted["operationName"] = "microsoft.compute/virtualmachines/start/action"
        succeeded = _vm_event("l2", "Succeeded")
        succeeded["operationName"] = "microsoft.compute/virtualmachines/start/action"

        operations = assemble_operations([accepted, succeeded])

        self.assertEqual(1, len(operations))
        operation = operations[0]
        self.assertEqual("succeeded", operation["Outcome"])
        self.assertIsNotNone(operation["AppliedEventId"])
        self.assertIsNone(operation["AppliedPayloadGz"])
        self.assertIsNone(operation["PayloadEncoding"])


class ResolvePlanTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        self.pipeline = ActivityLogPipeline(self.store)
        self.now_patch = patch("services.activity_log.pipeline._utc_now", return_value=_NOW)
        self.scope_patch = patch.object(
            ActivityLogPipeline, "_scope",
            return_value={"subs": ["sub-1"], "configSelectors": {"subscriptions": ["sub-1"], "managementGroups": []}},
        )
        self.identity_patch = patch("services.activity_log.pipeline.for_backend_operation")
        self.now_patch.start()
        self.scope_patch.start()
        self.identity_patch.start()
        self.versions = {recipe.id: recipe.version for recipe in recipes.all_recipes()}
        self.recipe_ids = sorted(self.versions)

    def tearDown(self):
        patch.stopall()

    def test_initial_plan_uses_historical_window_and_server_operations(self):
        self.store.list_coverage.return_value = []

        plan = self.pipeline.resolve_plan({})

        self.assertEqual("load", plan["action"])
        self.assertEqual("initial-full", plan["reason"])
        self.assertEqual([], plan["deletions"])
        self.assertEqual(list(recipes.server_operations(recipes.all_recipes())), plan["operations"])
        unit = plan["workUnits"][0]
        self.assertEqual("sub-1", unit["subscriptionId"])
        self.assertEqual(_HISTORICAL_START, unit["start"])
        self.assertEqual(_CUTOFF, unit["end"])
        state = plan["newState"][0]
        self.assertEqual(self.recipe_ids, state["enabledRecipeIds"])
        self.assertEqual(self.versions, state["materializedRecipeVersions"])

    def test_refresh_uses_overlap_window_when_no_drift(self):
        self.store.list_coverage.return_value = [{
            "subscriptionId": "sub-1",
            "coverageStart": "2026-06-01T00:00:00Z",
            "coverageEnd": "2026-09-06T11:40:00Z",
            "enabledRecipeIds": self.recipe_ids,
            "materializedRecipeVersions": self.versions,
        }]

        plan = self.pipeline.resolve_plan({})

        self.assertEqual("refresh", plan["reason"])
        self.assertEqual("2026-09-05T11:40:00Z", plan["workUnits"][0]["start"])  # coverageEnd - 24h

    def test_version_drift_forces_reconcile_from_historical_start(self):
        self.store.list_coverage.return_value = [{
            "subscriptionId": "sub-1",
            "coverageStart": "2026-06-01T00:00:00Z",
            "coverageEnd": "2026-09-06T11:40:00Z",
            "enabledRecipeIds": self.recipe_ids,
            "materializedRecipeVersions": {"vm-configuration": 0},
        }]

        plan = self.pipeline.resolve_plan({})

        self.assertEqual("reconcile", plan["reason"])
        self.assertEqual(_HISTORICAL_START, plan["workUnits"][0]["start"])


class PipelineIoTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        self.pipeline = ActivityLogPipeline(self.store)
        self.identity_patch = patch("services.activity_log.pipeline.for_backend_operation")
        identity = self.identity_patch.start()
        identity.return_value.get_token.return_value = "token"

    def tearDown(self):
        patch.stopall()

    def test_fetch_and_store_page_assembles_and_upserts(self):
        self.store.upsert_operations.return_value = 1
        page = {
            "status": "ok",
            "received": 2,
            "events": [
                _vm_event("e1", "Accepted", body={"location": "eastus"}),
                _vm_event("e2", "Succeeded"),
            ],
            "nextLink": None,
        }
        with patch("services.activity_log.pipeline.fetch_page", return_value=page):
            result = self.pipeline.fetch_and_store_page({"subscriptionId": "sub-1", "start": "s", "end": "e"})

        self.assertEqual("ok", result["status"])
        self.assertEqual(2, result["received"])
        self.assertEqual(1, result["operations"])
        self.assertEqual(1, result["upserted"])
        upserted = self.store.upsert_operations.call_args.args[0]
        self.assertEqual(1, len(upserted))

    def test_fetch_and_store_page_passes_throttle_through(self):
        with patch("services.activity_log.pipeline.fetch_page", return_value={"status": "throttled", "retryAfter": 20}):
            result = self.pipeline.fetch_and_store_page({"subscriptionId": "sub-1", "start": "s", "end": "e"})

        self.assertEqual({"status": "throttled", "retryAfter": 20}, result)
        self.store.upsert_operations.assert_not_called()

    def test_fetch_and_store_page_returns_error_when_persistence_fails(self):
        page = {"status": "ok", "received": 1, "nextLink": None,
                "events": [_vm_event("e1", "Succeeded")]}
        self.store.upsert_operations.side_effect = RuntimeError("boom")
        with patch("services.activity_log.pipeline.fetch_page", return_value=page):
            result = self.pipeline.fetch_and_store_page({"subscriptionId": "sub-1", "start": "s", "end": "e"})

        self.assertEqual("error", result["status"])
        self.assertIn("boom", result["error"])

    def test_write_state_sets_coverage_per_subscription(self):
        result = self.pipeline.write_state([
            {"subscriptionId": "sub-1", "coverageStart": "a", "coverageEnd": "b"},
            {"coverageStart": "x"},  # skipped: no subscription id
        ])

        self.assertEqual({"ok": True, "written": 1}, result)
        self.store.set_coverage.assert_called_once()
        subscription_id, coverage = self.store.set_coverage.call_args.args
        self.assertEqual("sub-1", subscription_id)
        self.assertIn("lastUpsertAt", coverage)

    def test_status_aggregates_coverage(self):
        self.store.list_coverage.return_value = [{
            "subscriptionId": "sub-1",
            "coverageStart": "2026-06-01T00:00:00Z",
            "coverageEnd": "2026-09-06T11:40:00Z",
        }]
        with patch("services.activity_log.pipeline._utc_now", return_value=_NOW), \
                patch.object(ActivityLogPipeline, "_scope", return_value={"subs": ["sub-1"], "configSelectors": {}}):
            status = self.pipeline.status({})

        self.assertTrue(status["hasData"])
        self.assertTrue(status["isRefreshEnabled"])
        self.assertEqual("2026-09-06T11:40:00Z", status["coverageEnd"])
        self.assertEqual(1, status["subscriptionsCovered"])


class ReconcileTests(unittest.TestCase):
    _HORIZON = timedelta(hours=48)

    def _vm_operation(self, **overrides) -> dict:
        payload = compress_payload(_vm_event("e1", "Succeeded", body={
            "location": "eastus",
            "properties": {"hardwareProfile": {"vmSize": "Standard_D2s_v5"}},
        }))
        operation = {
            "ResourceType": "microsoft.compute/virtualmachines",
            "OperationName": "microsoft.compute/virtualmachines/write",
            "AppliedPayloadGz": payload,
            "Columns": {},
            "Outcome": "succeeded",
            "LastEventTime": "2026-09-01T00:00:00Z",
            "ReprojectionPending": False,
            "AssemblerVersion": ASSEMBLER_VERSION,
            "RecipeVersion": 1,
        }
        operation.update(overrides)
        return operation

    def test_row_needs_reconcile_selection(self):
        stale = self._vm_operation(AssemblerVersion=0)
        clean = self._vm_operation()
        no_payload = self._vm_operation(AssemblerVersion=0)
        no_payload.pop("AppliedPayloadGz")
        pending_aged = {"Outcome": "pending", "LastEventTime": "2026-01-01T00:00:00Z"}

        self.assertTrue(_row_needs_reconcile(stale, _NOW, self._HORIZON))
        self.assertFalse(_row_needs_reconcile(clean, _NOW, self._HORIZON))
        self.assertFalse(_row_needs_reconcile(no_payload, _NOW, self._HORIZON))
        self.assertTrue(_row_needs_reconcile(pending_aged, _NOW, self._HORIZON))

    def test_reconcile_reprojects_stale_operation(self):
        operation = self._vm_operation(AssemblerVersion=0, RecipeVersion=0, Columns={})

        changed = _reconcile_operation(operation, _NOW, self._HORIZON)

        self.assertTrue(changed)
        self.assertEqual("Standard_D2s_v5", operation["Columns"]["VmSize"])
        self.assertEqual(ASSEMBLER_VERSION, operation["AssemblerVersion"])
        self.assertEqual(1, operation["RecipeVersion"])

    def test_reconcile_ages_pending_operation(self):
        operation = self._vm_operation(Outcome="pending", LastEventTime="2026-01-01T00:00:00Z")

        changed = _reconcile_operation(operation, _NOW, self._HORIZON)

        self.assertTrue(changed)
        self.assertEqual("unknown", operation["Outcome"])

    def test_reconcile_clean_operation_is_noop(self):
        self.assertFalse(_reconcile_operation(self._vm_operation(), _NOW, self._HORIZON))

    def test_reconcile_subscription_is_bounded_and_counts_writes(self):
        class _FakeStore:
            def __init__(self, rows):
                self._rows = rows
                self.upserted = []

            def query_operations(self, subscription_id):
                return self._rows

            def hydrate_operation(self, row):
                return dict(row)

            def upsert_operations(self, operations):
                self.upserted.extend(operations)
                return len(operations)

        rows = [self._vm_operation(AssemblerVersion=0, RowKey=str(index)) for index in range(3)]
        store = _FakeStore(rows)
        pipeline = ActivityLogPipeline(store)

        with patch("services.activity_log.pipeline._utc_now", return_value=_NOW):
            result = pipeline.reconcile_subscription({"subscriptionId": "sub-1", "maxUnits": 2})

        self.assertEqual(2, result["reconciled"])
        self.assertEqual(2, len(store.upserted))

    def test_reconcile_subscription_noops_when_max_units_not_positive(self):
        store = MagicMock()
        pipeline = ActivityLogPipeline(store)

        result = pipeline.reconcile_subscription({"subscriptionId": "sub-1", "maxUnits": 0})

        self.assertEqual(
            {"subscriptionId": "sub-1", "status": "ok", "reconciled": 0},
            result,
        )
        store.query_operations.assert_not_called()

    def test_reconcile_subscription_returns_error_when_storage_fails(self):
        store = MagicMock()
        store.query_operations.side_effect = ConnectionError("storage reset")
        pipeline = ActivityLogPipeline(store)

        result = pipeline.reconcile_subscription({"subscriptionId": "sub-1", "maxUnits": 2})

        self.assertEqual("error", result["status"])
        self.assertEqual("sub-1", result["subscriptionId"])
        self.assertEqual("storage reset", result["error"])
        self.assertEqual(0, result["reconciled"])


if __name__ == "__main__":
    unittest.main()
