from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import azure.functions as func

from functions.subscription_context_bp import (
    context_fields,
    manual_template,
    preview_values,
    resolve_values,
    tag_source_options,
    zzz_context_field,
)

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"


class SubscriptionContextBlueprintTests(unittest.TestCase):
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_preview_values_uses_real_multipart_request(self, _authorize, inventory_and_scope):
        inventory_and_scope.return_value = (
            [{"id": SUB_A, "name": "A"}, {"id": SUB_B, "name": "B"}],
            (SUB_A,),
        )
        definition = json.dumps({
            "key": "business_unit",
            "name": "Business Unit",
            "enabled": True,
            "valueSource": {"type": "manual_file", "version": 1},
        })
        csv_content = f"SubscriptionId,Value\r\n{SUB_A},HR\r\n".encode()
        request = self._multipart_request(definition, csv_content)

        response = preview_values(request)

        body = json.loads(response.get_body())
        self.assertEqual(200, response.status_code)
        self.assertEqual("HR", body["values"][SUB_A])
        self.assertIsNone(body["values"][SUB_B])
        self.assertEqual(1, body["inScope"]["mappedSubscriptions"])
        self.assertEqual(1, body["outsideScope"]["unmappedSubscriptions"])

    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_creation_template_download(self, _authorize, inventory_and_scope):
        inventory_and_scope.return_value = ([{"id": SUB_A, "name": "A"}], (SUB_A,))
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/api/subscription-context/templates/manual?format=csv",
            headers={},
            params={"format": "csv"},
            route_params={},
            body=b"",
        )

        response = manual_template(request)

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/csv", response.mimetype)
        self.assertIn(b"SubscriptionId,SubscriptionName,Scope,Value", response.get_body())
        self.assertIn(SUB_A.encode(), response.get_body())

    @patch("functions.subscription_context_bp.identity.user_audit_metadata")
    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_create_revalidates_and_saves(self, authorize, inventory_and_scope, service, audit_metadata):
        authorize.return_value = MagicMock()
        inventory_and_scope.return_value = ([{"id": SUB_A, "name": "A"}], (SUB_A,))
        service.save_manual_field.return_value = {
            "key": "business_unit",
            "name": "Business Unit",
            "enabled": True,
            "revision": 1,
        }
        request = self._multipart_request(
            json.dumps({
                "key": "business_unit",
                "name": "Business Unit",
                "enabled": True,
                "valueSource": {"type": "manual_file", "version": 1},
            }),
            f"SubscriptionId,Value\r\n{SUB_A},HR\r\n".encode(),
            url="http://localhost/api/subscription-context",
        )

        response = context_fields(request)

        self.assertEqual(201, response.status_code)
        saved_field, saved_mapping, saved_audit = service.save_manual_field.call_args.args
        self.assertEqual("business_unit", saved_field.key)
        self.assertEqual({SUB_A: "HR"}, saved_mapping)
        self.assertIs(audit_metadata.return_value, saved_audit)

    @patch("functions.subscription_context_bp.identity.user_audit_metadata")
    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_create_management_group_field_from_json(
        self,
        authorize,
        inventory_and_scope,
        service,
        audit_metadata,
    ):
        authorize.return_value = MagicMock()
        inventory = [{"id": SUB_A, "managementGroupAncestors": []}]
        inventory_and_scope.return_value = (inventory, (SUB_A,))
        service.save_management_group_field.return_value = (
            {"key": "organization", "name": "Organization", "revision": 1},
            {SUB_A: "Organization"},
            {},
        )
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/subscription-context",
            headers={"content-type": "application/json"},
            params={}, route_params={},
            body=json.dumps({
                "key": "organization",
                "name": "Organization",
                "enabled": True,
                "valueSource": {
                    "type": "management_group_level",
                    "version": 1,
                    "config": {"level": 1, "outputMode": "selected_level"},
                },
            }).encode(),
        )

        response = context_fields(request)

        self.assertEqual(201, response.status_code)
        service.save_management_group_field.assert_called_once()
        self.assertEqual("Organization", json.loads(response.get_body())["values"][SUB_A])

    @patch("functions.subscription_context_bp.identity.user_audit_metadata")
    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_create_subscription_tag_field_from_json(
        self,
        authorize,
        inventory_and_scope,
        service,
        audit_metadata,
    ):
        authorize.return_value = MagicMock()
        inventory = [{"id": SUB_A, "tags": {"Environment": "PRD"}}]
        inventory_and_scope.return_value = (inventory, (SUB_A,))
        service.save_subscription_tag_field.return_value = (
            {"key": "environment", "name": "Environment", "revision": 1},
            {SUB_A: "PRD"},
            {},
        )
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/subscription-context",
            headers={"content-type": "application/json"},
            params={}, route_params={},
            body=json.dumps({
                "key": "environment",
                "name": "Environment",
                "enabled": True,
                "valueSource": {
                    "type": "subscription_tag",
                    "version": 1,
                    "config": {"tagKey": "Environment"},
                },
            }).encode(),
        )

        response = context_fields(request)

        self.assertEqual(201, response.status_code)
        service.save_subscription_tag_field.assert_called_once()
        self.assertEqual("PRD", json.loads(response.get_body())["values"][SUB_A])

    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_preview_management_group_reports_shallow_reason(self, _authorize, inventory_and_scope, service):
        inventory = [{"id": SUB_A, "managementGroupAncestors": []}]
        inventory_and_scope.return_value = (inventory, (SUB_A,))
        service.preview_management_group_field.return_value = ({}, {SUB_A: "hierarchy_too_shallow"})
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/subscription-context/preview",
            headers={"content-type": "application/json"},
            params={}, route_params={},
            body=json.dumps({
                "key": "organization",
                "name": "Organization",
                "enabled": True,
                "valueSource": {
                    "type": "management_group_level",
                    "version": 1,
                    "config": {"level": 6, "outputMode": "selected_level"},
                },
            }).encode(),
        )

        response = preview_values(request)

        body = json.loads(response.get_body())
        self.assertEqual(200, response.status_code)
        self.assertEqual({"hierarchy_too_shallow": 1}, body["inScope"]["unmappedByReason"])

    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_preview_subscription_tag_reports_missing_reason(self, _authorize, inventory_and_scope, service):
        inventory = [{"id": SUB_A, "tags": {}}, {"id": SUB_B, "tags": {"Environment": "PRD"}}]
        inventory_and_scope.return_value = (inventory, (SUB_A, SUB_B))
        service.preview_subscription_tag_field.return_value = (
            {SUB_B: "PRD"},
            {SUB_A: "tag_not_present"},
        )
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/subscription-context/preview",
            headers={"content-type": "application/json"},
            params={}, route_params={},
            body=json.dumps({
                "key": "environment",
                "name": "Environment",
                "enabled": True,
                "valueSource": {
                    "type": "subscription_tag",
                    "version": 1,
                    "config": {"tagKey": "Environment"},
                },
            }).encode(),
        )

        response = preview_values(request)

        body = json.loads(response.get_body())
        self.assertEqual(200, response.status_code)
        self.assertEqual({"tag_not_present": 1}, body["inScope"]["unmappedByReason"])

    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_tag_source_options_rank_configured_scope(self, _authorize, inventory_and_scope):
        inventory_and_scope.return_value = (
            [
                {"id": SUB_A, "tags": {"Environment": "PRD", "CostCenter": "1"}},
                {"id": SUB_B, "tags": {"environment": "DEV"}},
            ],
            (SUB_A, SUB_B),
        )
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/api/subscription-context/value-source-options/tags",
            headers={}, params={}, route_params={}, body=b"",
        )

        response = tag_source_options(request)

        body = json.loads(response.get_body())
        self.assertEqual(200, response.status_code)
        self.assertEqual("Environment", body["value"][0]["key"])
        self.assertEqual(2, body["value"][0]["subscriptionCount"])

    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._authorize")
    def test_list_fields(self, _authorize, service):
        service.list_fields.return_value = [{"key": "business_unit", "name": "Business Unit"}]
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/api/subscription-context",
            headers={}, params={}, route_params={}, body=b"",
        )

        response = context_fields(request)

        body = json.loads(response.get_body())
        self.assertEqual(1, body["count"])
        self.assertEqual("business_unit", body["value"][0]["key"])

    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._authorize")
    def test_resolve_values(self, _authorize, service):
        service.resolve.return_value = {SUB_A: {"business_unit": "HR"}}
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/subscription-context/resolve",
            headers={"content-type": "application/json"}, params={}, route_params={},
            body=json.dumps({"subscriptionIds": [SUB_A], "includeDisabled": True}).encode(),
        )

        response = resolve_values(request)

        self.assertEqual(200, response.status_code)
        service.resolve.assert_called_once_with([SUB_A], True)

    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._authorize")
    def test_delete_field(self, _authorize, service):
        service.delete_field.return_value = True
        request = func.HttpRequest(
            method="DELETE",
            url="http://localhost/api/subscription-context/business_unit",
            headers={}, params={}, route_params={"fieldKey": "business_unit"}, body=b"",
        )

        response = zzz_context_field(request)

        self.assertEqual(204, response.status_code)
        service.delete_field.assert_called_once_with("business_unit")

    @patch("functions.subscription_context_bp.identity.user_audit_metadata")
    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_patch_rejects_mismatched_key_before_save(
        self,
        authorize,
        inventory_and_scope,
        service,
        _audit_metadata,
    ):
        authorize.return_value = MagicMock()
        inventory_and_scope.return_value = ([], ())
        request = func.HttpRequest(
            method="PATCH",
            url="http://localhost/api/subscription-context/expected",
            headers={"content-type": "application/json"},
            params={},
            route_params={"fieldKey": "expected"},
            body=json.dumps({
                "key": "different",
                "name": "Different",
                "enabled": True,
                "valueSource": {
                    "type": "management_group_level",
                    "version": 1,
                    "config": {"level": 1, "outputMode": "selected_level"},
                },
            }).encode(),
        )

        response = zzz_context_field(request)

        self.assertEqual(400, response.status_code)
        service.save_management_group_field.assert_not_called()

    @patch("functions.subscription_context_bp.context_service")
    @patch("functions.subscription_context_bp._backend_inventory_and_scope")
    @patch("functions.subscription_context_bp._authorize")
    def test_prefilled_template_download(self, _authorize, inventory_and_scope, service):
        inventory_and_scope.return_value = ([{"id": SUB_A, "name": "A"}], (SUB_A,))
        service.get_manual_mapping.return_value = (
            {"key": "business_unit"},
            {SUB_A: "HR"},
        )
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/api/subscription-context/templates/manual?format=csv&fieldKey=business_unit",
            headers={}, params={"format": "csv", "fieldKey": "business_unit"},
            route_params={}, body=b"",
        )

        response = manual_template(request)

        self.assertEqual(200, response.status_code)
        self.assertIn(b",HR", response.get_body())

    @staticmethod
    def _multipart_request(
        definition: str,
        content: bytes,
        url: str = "http://localhost/api/subscription-context/preview",
    ) -> func.HttpRequest:
        boundary = "business-context-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="definition"\r\n\r\n'
            f"{definition}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="mapping.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return func.HttpRequest(
            method="POST",
            url=url,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            params={},
            route_params={},
            body=body,
        )


if __name__ == "__main__":
    unittest.main()