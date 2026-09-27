from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services.activity_log import projection
from services.activity_log.projection import ProjectionResult, decompress_json, project
from services.activity_log.recipes import ErrorProjection, ProjectedField, Recipe


def _vm_event(status="Accepted", response_body=None, status_message=None):
    properties = {}
    if response_body is not None:
        properties["responseBody"] = json.dumps(response_body)
    if status_message is not None:
        properties["statusMessage"] = json.dumps(status_message)
    return {"status": status, "properties": properties}


class ResolvePathTests(unittest.TestCase):
    def test_descends_through_embedded_json_string(self):
        event = _vm_event(response_body={
            "location": "eastus",
            "properties": {"hardwareProfile": {"vmSize": "Standard_D4as_v4"}},
        })
        value = projection.resolve_path(
            event["properties"], "responseBody.properties.hardwareProfile.vmSize"
        )
        self.assertEqual("Standard_D4as_v4", value)

    def test_missing_segment_returns_sentinel(self):
        event = _vm_event(response_body={"location": "eastus"})
        value = projection.resolve_path(event["properties"], "responseBody.zones")
        self.assertIs(projection._MISSING, value)

    def test_malformed_json_string_is_not_a_container(self):
        value = projection.resolve_path({"responseBody": "not json"}, "responseBody.location")
        self.assertIs(projection._MISSING, value)

    def test_malformed_embedded_container_stays_raw_and_stops_descent(self):
        # Opens like JSON but is invalid: it stays a raw string, so the next segment misses.
        value = projection.resolve_path({"responseBody": "{oops"}, "responseBody.location")
        self.assertIs(projection._MISSING, value)

    def test_descends_through_list_index(self):
        properties = {"statusMessage": json.dumps({
            "error": {"details": [{"code": "AllocationFailed"}]}
        })}

        value = projection.resolve_path(properties, "statusMessage.error.details.0.code")

        self.assertEqual("AllocationFailed", value)


class ProjectTests(unittest.TestCase):
    def test_projects_declared_columns(self):
        recipe = Recipe(
            id="p", version=1, operation_names=("a/b",),
            projected_fields=(
                ProjectedField("VmSize", "responseBody.properties.hardwareProfile.vmSize"),
                ProjectedField("Location", "responseBody.location"),
            ),
        )
        event = _vm_event(response_body={
            "location": "eastus",
            "properties": {"hardwareProfile": {"vmSize": "Standard_D4as_v4"}},
        })
        result = project(event, recipe)
        self.assertEqual(
            {"VmSize": "Standard_D4as_v4", "Location": "eastus"},
            result.columns,
        )
        self.assertFalse(result.reprojection_pending)

    def test_on_status_gates_field(self):
        recipe = Recipe(
            id="p", version=1, operation_names=("a/b",),
            projected_fields=(ProjectedField("ErrorCode", "statusMessage.error.code", on_status="Failed"),),
        )
        succeeded = _vm_event(status="Succeeded", status_message={"error": {"code": "X"}})
        self.assertEqual({}, project(succeeded, recipe).columns)
        failed = _vm_event(status="Failed", status_message={"error": {"code": "AllocationFailed"}})
        self.assertEqual({"ErrorCode": "AllocationFailed"}, project(failed, recipe).columns)

    def test_project_uses_fallback_path_when_primary_is_absent(self):
        recipe = Recipe(
            id="p", version=1, operation_names=("a/b",),
            projected_fields=(
                ProjectedField(
                    "ErrorCode",
                    "statusMessage.error.details.0.code",
                    on_status="Failed",
                    fallback_paths=("statusMessage.error.code",),
                ),
            ),
        )
        failed = _vm_event(
            status="Failed",
            status_message={"error": {"code": "ResourceOperationFailure"}},
        )

        self.assertEqual(
            {"ErrorCode": "ResourceOperationFailure"},
            project(failed, recipe).columns,
        )

    def test_absent_field_is_skipped_not_pending(self):
        recipe = Recipe(
            id="p", version=1, operation_names=("a/b",),
            projected_fields=(ProjectedField("Zones", "responseBody.zones"),),
        )
        event = _vm_event(response_body={"location": "eastus"})
        result = project(event, recipe)
        self.assertEqual({}, result.columns)
        self.assertFalse(result.reprojection_pending)

    def test_project_never_raises(self):
        recipe = Recipe(
            id="p", version=1, operation_names=("a/b",),
            projected_fields=(ProjectedField("X", "responseBody.a.b.c"),),
        )
        # properties is not a dict — must not raise.
        result = project({"status": "Accepted", "properties": None}, recipe)
        self.assertIsInstance(result, ProjectionResult)
        self.assertEqual({}, result.columns)

    def test_unexpected_resolve_failure_flags_reprojection_pending(self):
        recipe = Recipe(
            id="p", version=1, operation_names=("a/b",),
            projected_fields=(ProjectedField("Location", "responseBody.location"),),
        )
        event = _vm_event(response_body={"location": "eastus"})
        with patch.object(projection, "resolve_path", side_effect=RuntimeError("boom")):
            result = project(event, recipe)
        self.assertTrue(result.reprojection_pending)
        self.assertEqual({}, result.columns)


class ErrorProjectionTests(unittest.TestCase):
    def setUp(self):
        self.recipe = Recipe(
            id="errors",
            version=1,
            operation_names=("a/b",),
            error_projection=ErrorProjection("statusMessage.error", "Failed"),
        )

    def test_parent_and_first_detail_are_distinct_and_details_are_retained(self):
        details = [
            {"code": "AllocationFailed", "message": "specific", "target": "vm"},
            {"code": "Other", "message": "second"},
        ]
        event = _vm_event(status="Failed", status_message={
            "error": {
                "code": "ResourceOperationFailure",
                "message": "parent",
                "details": details,
            }
        })

        result = project(event, self.recipe)

        self.assertEqual("ResourceOperationFailure", result.columns["ErrorCode"])
        self.assertEqual("parent", result.columns["ErrorMessage"])
        self.assertEqual("AllocationFailed", result.columns["ErrorDetailCode"])
        self.assertEqual("specific", result.columns["ErrorDetailMessage"])
        self.assertEqual(2, result.columns["ErrorDetailCount"])
        self.assertEqual(details, decompress_json(result.error_details_gz))

    def test_allocation_code_is_found_after_first_detail_and_recursively(self):
        event = _vm_event(status="Failed", status_message={
            "error": {
                "code": "Parent",
                "details": [
                    {"code": "Other"},
                    {"code": "Wrapper", "details": [
                        {"code": "ZonalAllocationFailed"},
                    ]},
                ],
            }
        })

        result = project(event, self.recipe)

        self.assertEqual(
            '["Parent","Other","Wrapper","ZonalAllocationFailed"]',
            result.columns["ErrorCodesJson"],
        )
        self.assertTrue(result.columns["HasAllocationFailure"])

    def test_zero_details_has_count_and_empty_retained_array(self):
        result = project(
            _vm_event(status="Failed", status_message={
                "error": {"code": "Parent", "details": []},
            }),
            self.recipe,
        )

        self.assertEqual(0, result.columns["ErrorDetailCount"])
        self.assertNotIn("ErrorDetailCode", result.columns)
        self.assertEqual([], decompress_json(result.error_details_gz))

    def test_codes_are_deduplicated_case_insensitively_in_encounter_order(self):
        result = project(
            _vm_event(status="Failed", status_message={
                "error": {
                    "code": "AllocationFailed",
                    "details": [
                        {"code": "allocationfailed"},
                        {"code": ""},
                        {"code": "Another"},
                        {"code": "ANOTHER"},
                    ],
                },
            }),
            self.recipe,
        )

        self.assertEqual(
            '["AllocationFailed","Another"]',
            result.columns["ErrorCodesJson"],
        )

    def test_error_projection_only_runs_on_owned_stage(self):
        result = project(
            _vm_event(status="Accepted", status_message={
                "error": {"code": "AllocationFailed", "details": []},
            }),
            self.recipe,
        )

        self.assertEqual({}, result.columns)
        self.assertIsNone(result.error_details_gz)


class PayloadTests(unittest.TestCase):
    def test_round_trip(self):
        event = {"eventDataId": "1", "properties": {"responseBody": "{\"a\":1}"}}
        blob = projection.compress_payload(event)
        self.assertTrue(blob.startswith(b"\x1f\x8b"))
        self.assertEqual(event, projection.decompress_payload(blob))

    def test_payload_encoding_marker(self):
        self.assertEqual("gzip+json", projection.PAYLOAD_ENCODING)

    def test_round_trip_serializes_datetime_as_isoformat(self):
        event = {"eventTimestamp": datetime(2026, 9, 1, tzinfo=timezone.utc)}
        restored = projection.decompress_payload(projection.compress_payload(event))
        self.assertEqual("2026-09-01T00:00:00+00:00", restored["eventTimestamp"])

    def test_round_trip_serializes_unknown_object_as_string(self):
        class _Weird:
            def __str__(self) -> str:
                return "weird!"

        restored = projection.decompress_payload(projection.compress_payload({"x": _Weird()}))
        self.assertEqual("weird!", restored["x"])


if __name__ == "__main__":
    unittest.main()
