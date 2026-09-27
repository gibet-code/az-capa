from __future__ import annotations

import gzip
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
)
from azure.data.tables import UpdateMode

from storage.activity_log_store import (
    ActivityLogStore,
    _error_details_spill_blob_name,
    _spill_blob_name,
)
from tests.storage._fake_table import FakeTable


class _TxnTable(FakeTable):
    def submit_transaction(self, operations):
        for op in operations:
            action = op[0]
            if action == "upsert":
                mode = (op[2] if len(op) > 2 else {}).get("mode", UpdateMode.MERGE)
                self.upsert_entity(op[1], mode)
            elif action == "delete":
                self.delete_entity(op[1]["PartitionKey"], op[1]["RowKey"])


class _FakeTableService:
    def __init__(self):
        self.tables: dict[str, _TxnTable] = {}
        self.deleted: list[str] = []

    def create_table_if_not_exists(self, name):
        return self.tables.setdefault(name, _TxnTable())

    def get_table_client(self, name):
        return self.tables.setdefault(name, _TxnTable())

    def delete_table(self, name):
        if name not in self.tables:
            raise ResourceNotFoundError("missing")
        self.deleted.append(name)
        self.tables.pop(name, None)


class _FakeBlob:
    def __init__(self, data: bytes, content_settings=None):
        self._data = data
        self.properties = SimpleNamespace(content_settings=content_settings)

    def download_blob(self):
        return self

    def readall(self):
        return self._data


class _FakeContainer:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.content_settings = {}

    def create_container(self):
        return None

    def upload_blob(self, name, data, overwrite=False, content_settings=None):
        self.blobs[name] = bytes(data)
        self.content_settings[name] = content_settings

    def get_blob_client(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        return _FakeBlob(self.blobs[name], self.content_settings.get(name))

    def delete_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        self.blobs.pop(name, None)


class _FakeBlobService:
    def __init__(self, container: _FakeContainer):
        self._container = container
        self.deleted: list[str] = []

    def get_container_client(self, name):
        return self._container

    def delete_container(self, name):
        self.deleted.append(name)


def _operation(**overrides) -> dict:
    base = {
        "OperationIdentity": "sub|/res|corr|op",
        "OperationIdentityHash": "abcd1234",
        "PartitionKey": "sub|ab",
        "RowKey": "abcd1234",
        "SubscriptionId": "sub",
        "ResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        "ResourceType": "microsoft.compute/virtualmachines",
        "OperationName": "microsoft.compute/virtualmachines/write",
        "OperationId": "op-1",
        "CorrelationId": "corr",
        "FirstEventTime": "2026-09-01T00:00:00Z",
        "LastEventTime": "2026-09-01T00:05:00Z",
        "RequestEventId": "e1",
        "TerminalEventId": "e2",
        "AllEventIds": ["e1", "e2"],
        "Columns": {"VmSize": "Standard_D2s_v5", "Zones": ["1"]},
        "AppliedEventId": "e1",
        "AppliedPayloadGz": b"gz-bytes",
        "PayloadEncoding": "gzip+json",
        "ErrorDetailsGz": b"error-details-gz",
        "Outcome": "succeeded",
        "PairingStatus": "paired",
        "ReprojectionPending": False,
    }
    base.update(overrides)
    return base


class ActivityLogStoreTests(unittest.TestCase):
    def setUp(self):
        self.service = _FakeTableService()
        self.container = _FakeContainer()
        self.blob_service = _FakeBlobService(self.container)
        self.table_patch = patch(
            "storage.activity_log_store.table_service_client", return_value=self.service
        )
        self.blob_patch = patch(
            "storage.activity_log_store.blob_service_client", return_value=self.blob_service
        )
        self.table_patch.start()
        self.blob_patch.start()
        self.store = ActivityLogStore(
            operations_table_name=lambda: "ActivityLogOperations",
            collection_state_table_name=lambda: "ActivityLogCollectionState",
            spill_container_name=lambda: "activity-log-spill",
            inline_payload_max_bytes=lambda: 60_000,
        )

    def tearDown(self):
        self.table_patch.stop()
        self.blob_patch.stop()

    def _ops_table(self) -> _TxnTable:
        return self.service.tables["ActivityLogOperations"]

    def test_upsert_promotes_columns_and_retains_inline_payload(self):
        written = self.store.upsert_operations([_operation()])

        self.assertEqual(1, written)
        row = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertEqual("Standard_D2s_v5", row["VmSize"])
        self.assertEqual('["1"]', row["Zones"])  # list coerced to JSON
        self.assertEqual(b"gz-bytes", row["AppliedPayloadGz"])
        self.assertEqual(b"error-details-gz", row["ErrorDetailsGz"])
        self.assertFalse(row["PayloadTruncated"])
        self.assertEqual('["e1","e2"]', row["AllEventIds"])
        self.assertEqual(0, row["AllEventIdsChunkCount"])
        self.assertIn("FirstSeenAt", row)
        self.assertIn("LastSeenAt", row)
        self.assertIn("FinalizedAt", row)  # terminal outcome

    def test_pending_outcome_has_no_finalized_at(self):
        self.store.upsert_operations([_operation(Outcome="pending", TerminalEventId=None)])

        row = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertNotIn("FinalizedAt", row)

    def test_reserved_projected_column_is_skipped(self):
        self.store.upsert_operations([_operation(Columns={
            "Outcome": "hacked",
            "ErrorDetailsGz": b"hacked",
            "ErrorDetailsSpillBlob": "hacked",
            "VmSize": "S1",
        })])

        row = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertEqual("succeeded", row["Outcome"])
        self.assertEqual(b"error-details-gz", row["ErrorDetailsGz"])
        self.assertNotEqual("hacked", row.get("ErrorDetailsSpillBlob"))
        self.assertEqual("S1", row["VmSize"])

    def test_oversize_payload_spills_to_blob(self):
        big = b"x" * 60_001
        self.store.upsert_operations([_operation(AppliedPayloadGz=big)])

        row = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertNotIn("AppliedPayloadGz", row)
        self.assertTrue(row["PayloadTruncated"])
        self.assertEqual("sub/e1.json.gz", row["AppliedPayloadSpillBlob"])
        self.assertEqual(big, self.container.blobs["sub/e1.json.gz"])
        settings = self.container.content_settings["sub/e1.json.gz"]
        self.assertEqual("application/gzip", settings.content_type)
        self.assertIsNone(settings.content_encoding)

    def test_zero_inline_limit_spills_all_payloads_and_error_details(self):
        self.store = ActivityLogStore(
            operations_table_name=lambda: "ActivityLogOperations",
            collection_state_table_name=lambda: "ActivityLogCollectionState",
            spill_container_name=lambda: "activity-log-spill",
            inline_payload_max_bytes=lambda: 0,
        )

        self.store.upsert_operations([_operation()])

        row = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertNotIn("AppliedPayloadGz", row)
        self.assertNotIn("ErrorDetailsGz", row)
        self.assertEqual("sub/e1.json.gz", row["AppliedPayloadSpillBlob"])
        self.assertEqual(
            "sub/abcd1234.error-details.json.gz",
            row["ErrorDetailsSpillBlob"],
        )

    def test_load_applied_payload_rehydrates_from_spill(self):
        big = b"y" * 60_001
        self.store.upsert_operations([_operation(AppliedPayloadGz=big)])
        row = self._ops_table().rows[("sub|ab", "abcd1234")]

        self.assertEqual(big, self.store.load_applied_payload(row))
        self.assertEqual(b"gz-bytes", self.store.load_applied_payload({"AppliedPayloadGz": b"gz-bytes"}))

    def test_load_error_details_recompresses_legacy_content_encoded_blob(self):
        decoded = b'[{"code":"AllocationFailed"}]'
        downloader = _FakeBlob(
            decoded,
            SimpleNamespace(content_encoding="gzip"),
        )
        container = MagicMock()
        container.get_blob_client.return_value = downloader

        with patch.object(self.store, "_container", return_value=container):
            payload = self.store.load_error_details(
                {"ErrorDetailsSpillBlob": "legacy.error-details.json.gz"}
            )

        self.assertEqual(decoded, gzip.decompress(payload))

    def test_reupsert_preserves_first_seen_and_published_version(self):
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.service.get_table_client("ActivityLogOperations")
        self._ops_table().rows[("sub|ab", "abcd1234")] = {
            "PartitionKey": "sub|ab", "RowKey": "abcd1234",
            "FirstSeenAt": old, "PublishedVersion": 3, "FinalizedAt": old,
        }
        self._ops_table()._etags[("sub|ab", "abcd1234")] = 'W/"etag-seed"'

        self.store.upsert_operations([_operation()])

        row = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertEqual(old, row["FirstSeenAt"])
        self.assertEqual(3, row["PublishedVersion"])
        self.assertEqual(old, row["FinalizedAt"])  # prior finalize preserved

    def test_spill_blob_name_is_deterministic(self):
        self.assertEqual("sub/evt.json.gz", _spill_blob_name("SUB", "evt"))
        self.assertEqual(
            "sub/hash.error-details.json.gz",
            _error_details_spill_blob_name("SUB", "hash"),
        )

    def test_error_details_inline_and_spilled_round_trip(self):
        self.store.upsert_operations([_operation()])
        inline = self._ops_table().rows[("sub|ab", "abcd1234")]
        self.assertEqual(b"error-details-gz", self.store.load_error_details(inline))

        big = b"d" * 60_001
        self.store.upsert_operations([_operation(ErrorDetailsGz=big)])
        spilled = self._ops_table().rows[("sub|ab", "abcd1234")]

        self.assertNotIn("ErrorDetailsGz", spilled)
        self.assertEqual(
            "sub/abcd1234.error-details.json.gz",
            spilled["ErrorDetailsSpillBlob"],
        )
        self.assertEqual(big, self.store.load_error_details(spilled))

    def test_query_operations_decodes_all_event_ids(self):
        self.store.upsert_operations([_operation()])

        results = self.store.query_operations("SUB")

        self.assertEqual(1, len(results))
        self.assertEqual(["e1", "e2"], results[0]["AllEventIds"])

    def test_query_operations_window_filters_and_projects_only_requested_columns(self):
        table = MagicMock()
        table.query_entities.return_value = [{
            "ResourceId": "/subscriptions/sub/vm1",
            "Outcome": "failed",
            "AppliedPayloadGz": b"must-not-leak",
        }]
        columns = ["ResourceId", "Outcome"]

        with patch.object(self.store, "_operations_table", return_value=table):
            rows = self.store.query_operations_window(
                "SUB",
                "2026-08-24T12:00:00Z",
                "2026-09-23T12:00:00Z",
                ["Microsoft.Compute/virtualMachines/start/action", "op'quoted"],
                select=columns,
            )

        query = table.query_entities.call_args.args[0]
        self.assertIn("SubscriptionId eq 'sub'", query)
        self.assertIn("LastEventTime ge '2026-08-24T12:00:00Z'", query)
        self.assertIn("LastEventTime le '2026-09-23T12:00:00Z'", query)
        self.assertIn("OperationName eq 'Microsoft.Compute/virtualMachines/start/action'", query)
        self.assertIn("OperationName eq 'op''quoted'", query)
        self.assertEqual(columns, table.query_entities.call_args.kwargs["select"])
        self.assertEqual([{"ResourceId": "/subscriptions/sub/vm1", "Outcome": "failed"}], rows)

    def test_query_operations_window_with_no_operations_avoids_storage(self):
        table = MagicMock()
        with patch.object(self.store, "_operations_table", return_value=table):
            rows = self.store.query_operations_window(
                "sub", "start", "end", [], select=["ResourceId"]
            )

        self.assertEqual([], rows)
        table.query_entities.assert_not_called()

    def test_query_operations_window_can_defer_case_insensitive_operation_filtering(self):
        table = MagicMock()
        table.query_entities.return_value = []

        with patch.object(self.store, "_operations_table", return_value=table):
            self.store.query_operations_window(
                "sub", "start", "end", None, select=["OperationName"]
            )

        query = table.query_entities.call_args.args[0]
        self.assertNotIn("OperationName eq", query)

    def test_hydrate_operation_round_trips_columns_and_payload(self):
        self.store.upsert_operations([_operation()])
        row = self.store.query_operations("SUB")[0]

        operation = self.store.hydrate_operation(row)

        self.assertEqual("e1", operation["AppliedEventId"])
        self.assertEqual(b"gz-bytes", operation["AppliedPayloadGz"])
        self.assertEqual(b"error-details-gz", operation["ErrorDetailsGz"])
        self.assertEqual("Standard_D2s_v5", operation["Columns"]["VmSize"])
        self.assertEqual('["1"]', operation["Columns"]["Zones"])
        self.assertEqual(["e1", "e2"], operation["AllEventIds"])
        self.assertNotIn("PartitionKey", operation["Columns"])
        self.assertNotIn("Outcome", operation["Columns"])

    def test_coverage_round_trip(self):
        self.store.set_coverage("SUB", {
            "coverageStart": "2026-06-01T00:00:00Z",
            "coverageEnd": "2026-09-01T00:00:00Z",
            "enabledRecipeIds": ["vm-lifecycle", "vm-configuration"],
            "materializedRecipeVersions": {"vm-lifecycle": 1, "vm-configuration": 2},
            "lastSuccessfulRunId": "run-9",
        })

        coverage = self.store.get_coverage("sub")

        self.assertEqual("sub", coverage["subscriptionId"])
        self.assertEqual(["vm-lifecycle", "vm-configuration"], coverage["enabledRecipeIds"])
        self.assertEqual({"vm-lifecycle": 1, "vm-configuration": 2}, coverage["materializedRecipeVersions"])
        self.assertEqual("run-9", coverage["lastSuccessfulRunId"])

    def test_get_coverage_missing_returns_none(self):
        self.assertIsNone(self.store.get_coverage("nope"))

    def test_list_coverage_returns_all_subscriptions(self):
        self.store.set_coverage("sub-a", {"coverageStart": "2026-06-01T00:00:00Z"})
        self.store.set_coverage("sub-b", {"coverageStart": "2026-06-02T00:00:00Z"})

        coverages = self.store.list_coverage()

        self.assertEqual({"sub-a", "sub-b"}, {cov["subscriptionId"] for cov in coverages})

    def test_ensure_tables_creates_tables_and_returns_true(self):
        self.assertTrue(self.store.ensure_tables())
        self.assertIn("ActivityLogOperations", self.service.tables)
        self.assertIn("ActivityLogCollectionState", self.service.tables)

    def test_ensure_tables_returns_false_while_table_being_deleted(self):
        with patch.object(self.service, "create_table_if_not_exists",
                          side_effect=HttpResponseError("The table is being deleted")):
            self.assertFalse(self.store.ensure_tables())

    def test_delete_tables_removes_tables_and_container(self):
        self.store.ensure_tables()

        deleted = self.store.delete_tables()

        self.assertIn("ActivityLogOperations", deleted)
        self.assertIn("ActivityLogCollectionState", deleted)
        self.assertIn("activity-log-spill", deleted)

    def test_delete_subscriptions_data_purges_rows_blobs_and_coverage(self):
        self.store.upsert_operations([_operation(
            AppliedPayloadGz=b"z" * 60_001,
            ErrorDetailsGz=b"d" * 60_001,
        )])
        self.store.set_coverage("sub", {"coverageStart": "2026-06-01T00:00:00Z"})

        deleted = self.store.delete_subscriptions_data(["SUB"])

        self.assertEqual(1, deleted)
        self.assertEqual({}, self._ops_table().rows)
        self.assertEqual({}, self.container.blobs)
        self.assertIsNone(self.store.get_coverage("sub"))


if __name__ == "__main__":
    unittest.main()
