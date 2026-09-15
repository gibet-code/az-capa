from __future__ import annotations

import unittest

from services.activity_log import assembly
from services.activity_log.assembly import PreparedEvent


def _event(**overrides):
    base = dict(
        event_data_id="e1",
        event_timestamp="2026-06-30T17:43:07Z",
        subscription_id="SUB",
        resource_id="/subscriptions/SUB/resourceGroups/rg/providers/microsoft.compute/virtualMachines/vm1",
        operation_name="microsoft.compute/virtualMachines/write",
        status="Accepted",
    )
    base.update(overrides)
    return PreparedEvent(**base)


class IdentityTests(unittest.TestCase):
    def test_lowercases_subscription_resource_and_operation(self):
        identity = assembly.identity_string(_event(correlation_id="corr-1"))
        self.assertEqual(
            "sub|/subscriptions/sub/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm1|corr-1|microsoft.compute/virtualmachines/write",
            identity,
        )

    def test_unpaired_when_correlation_absent(self):
        identity = assembly.identity_string(_event(correlation_id=None, event_data_id="ev-9"))
        self.assertIn("|unpaired|", identity)
        self.assertTrue(identity.endswith("|ev-9"))

    def test_partition_key_scopes_by_subscription_and_hash_prefix(self):
        digest = assembly.identity_hash("x")
        self.assertEqual(f"sub|{digest[:2]}", assembly.partition_key("SUB", digest))


class AssembleTests(unittest.TestCase):
    def test_stages_with_same_correlation_pair_across_operation_ids(self):
        events = [
            _event(event_data_id="a", status="Started", operation_id="op-1", correlation_id="corr"),
            _event(event_data_id="b", status="Accepted", operation_id="op-1", correlation_id="corr",
                   event_timestamp="2026-06-30T17:43:08Z",
                   payload_gz=b"gz", payload_encoding="gzip+json", columns={"VmSize": "D4"}),
            _event(event_data_id="c", status="Succeeded", operation_id="op-2", correlation_id="corr",
                   event_timestamp="2026-06-30T17:43:20Z"),
        ]
        operations = assembly.assemble(events)
        self.assertEqual(1, len(operations))
        op = operations[0]
        self.assertEqual("paired", op["PairingStatus"])
        self.assertEqual("succeeded", op["Outcome"])
        self.assertEqual("a", op["RequestEventId"])
        self.assertEqual("c", op["TerminalEventId"])
        self.assertEqual(["a", "b", "c"], op["AllEventIds"])
        self.assertEqual({"VmSize": "D4"}, op["Columns"])
        self.assertEqual(b"gz", op["AppliedPayloadGz"])
        self.assertEqual("gzip+json", op["PayloadEncoding"])

    def test_duplicate_event_ids_within_input_are_collapsed(self):
        events = [
            _event(event_data_id="dup", correlation_id="corr"),
            _event(event_data_id="dup", correlation_id="corr"),
        ]
        operations = assembly.assemble(events)
        self.assertEqual(1, len(operations))
        self.assertEqual(["dup"], operations[0]["AllEventIds"])

    def test_terminal_only_is_pending_free_outcome(self):
        events = [_event(event_data_id="t", status="Failed", correlation_id="corr")]
        op = assembly.assemble(events)[0]
        self.assertEqual("failed", op["Outcome"])
        self.assertIsNone(op["RequestEventId"])
        self.assertEqual("t", op["TerminalEventId"])

    def test_request_only_is_pending(self):
        events = [_event(event_data_id="r", status="Accepted", correlation_id="corr")]
        op = assembly.assemble(events)[0]
        self.assertEqual("pending", op["Outcome"])

    def test_unpaired_events_do_not_merge(self):
        events = [
            _event(event_data_id="x", status="Succeeded", correlation_id=None),
            _event(event_data_id="y", status="Succeeded", correlation_id=None),
        ]
        operations = assembly.assemble(events)
        self.assertEqual(2, len(operations))
        self.assertTrue(all(op["PairingStatus"] == "unpaired" for op in operations))

    def test_accepted_payload_selected_over_terminal(self):
        events = [
            _event(event_data_id="acc", status="Accepted", correlation_id="corr",
                   payload_gz=b"applied", payload_encoding="gzip+json"),
            _event(event_data_id="term", status="Succeeded", correlation_id="corr",
                   event_timestamp="2026-06-30T17:43:30Z", payload_gz=b"terminal"),
        ]
        op = assembly.assemble(events)[0]
        self.assertEqual(b"applied", op["AppliedPayloadGz"])

    def test_reprojection_pending_propagates(self):
        events = [_event(event_data_id="p", correlation_id="corr", reprojection_pending=True)]
        self.assertTrue(assembly.assemble(events)[0]["ReprojectionPending"])


class ParseTimestampTests(unittest.TestCase):
    def test_missing_timestamp_is_treated_as_far_future(self):
        self.assertEqual(assembly._parse_timestamp(None), assembly._parse_timestamp(""))
        self.assertGreater(
            assembly._parse_timestamp(None),
            assembly._parse_timestamp("2026-06-30T17:43:07Z"),
        )

    def test_invalid_timestamp_is_treated_as_far_future(self):
        self.assertGreater(
            assembly._parse_timestamp("not-a-timestamp"),
            assembly._parse_timestamp("2026-06-30T17:43:07Z"),
        )


if __name__ == "__main__":
    unittest.main()
