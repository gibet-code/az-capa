from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import urllib3

from services.odcr_coverage import (
    _fetch_reservation_instance_view,
    apply_odcr_eligibility,
    apply_physical_zones,
    build_capacity_reservation_graph_query,
    build_vm_graph_query,
    enrich_vm_reservation_lifecycle,
    filter_vm_reservation_statuses,
    project_vm_list_row,
    reconcile_vm_reservations,
    zone_mapping_pairs,
)


class VmQueryTests(unittest.TestCase):
    def test_unfiltered_query_projects_inventory_fields(self):
        query = build_vm_graph_query()

        self.assertIn("resourceType = tolower(type)", query)
        self.assertIn("subscriptionName", query)
        self.assertIn("microsoft.resources/subscriptions", query)
        self.assertNotIn("resourceGroupTags", query)
        self.assertNotIn("subscriptionTags", query)
        self.assertNotIn("vmTags", query)
        self.assertNotIn("microsoft.resources/subscriptions/resourcegroups", query)
        self.assertIn("logicalZone = tostring(zones[0])", query)
        self.assertNotIn("deploymentType,", query)
        self.assertIn("powerState", query)
        self.assertIn("isVmssFlexible = isnotnull(properties.virtualMachineScaleSet)", query)
        self.assertIn("azureSpot = isnotempty(properties.evictionPolicy)", query)
        self.assertIn("dedicatedHostId = tostring(properties.host.id)", query)
        self.assertIn("isOnDedicatedHost = isnotempty(dedicatedHostId)", query)
        self.assertIn("availabilitySetId = tostring(properties.availabilitySet.id)", query)
        self.assertIn("hasAvailabilitySet = isnotempty(availabilitySetId)", query)
        self.assertIn("proximityPlacementGroupId = tostring(properties.proximityPlacementGroup.id)", query)
        self.assertIn("hasProximityPlacementGroup = isnotempty(proximityPlacementGroupId)", query)
        self.assertIn("hasUltraDiskStorageAttached", query)
        self.assertIn("bag_has_key(tags, 'LegacyVMNVA')", query)
        self.assertIn("capacityReservationGroupId", query)
        self.assertNotIn("capacityReservationGroupName", query)
        self.assertNotIn("matchingCapacityReservationIds", query)
        self.assertNotIn("capacityreservationgroups/capacityreservations", query)
        self.assertNotIn("virtualMachinesAssociated", query)
        self.assertNotIn("reservationStatus = case(", query)
        self.assertNotIn("| where false", query)

    def test_capacity_reservation_query_projects_zone_mapping_fields(self):
        query = build_capacity_reservation_graph_query()

        self.assertIn("microsoft.compute/capacityreservationgroups/capacityreservations", query)
        self.assertIn("capacityReservationGroupName", query)
        self.assertIn("matchingCapacityReservationId = tolower(id)", query)
        self.assertIn("reservationSubscriptionId = tolower(subscriptionId)", query)
        self.assertIn("reservationLocation = tolower(location)", query)
        self.assertIn("reservationZone = tostring(zones[0])", query)
        self.assertIn("reservationVmSize = tostring(sku.name)", query)
        self.assertIn("join kind=leftouter", query)
        self.assertNotIn("subscriptionId in~", query)

    def test_physical_zone_reconciliation_matches_across_logical_zones(self):
        group_id = "/subscriptions/cr-sub/resourcegroups/rg/providers/microsoft.compute/capacityreservationgroups/group"
        vm_rows = [{
            "id": "/vm/1", "subscriptionId": "vm-sub", "location": "eastus",
            "logicalZone": "1", "vmSize": "Standard_D2s_v5",
            "capacityReservationGroupId": group_id.upper(),
        }]
        reservation_rows = [
            {
                "capacityReservationGroupId": group_id, "capacityReservationGroupName": "group",
                "matchingCapacityReservationId": "/reservations/zone2",
                "reservationSubscriptionId": "cr-sub", "reservationLocation": "eastus",
                "reservationZone": "2", "reservationVmSize": "standard_d2s_v5",
            },
            {
                "capacityReservationGroupId": group_id, "capacityReservationGroupName": "group",
                "matchingCapacityReservationId": "/reservations/zone1",
                "reservationSubscriptionId": "cr-sub", "reservationLocation": "eastus",
                "reservationZone": "1", "reservationVmSize": "Standard_D2s_v5",
            },
        ]
        mappings = {
            ("vm-sub", "eastus"): {"1": "eastus-az2"},
            ("cr-sub", "eastus"): {"1": "eastus-az1", "2": "eastus-az2"},
        }

        self.assertEqual(
            {("vm-sub", "eastus"), ("cr-sub", "eastus")},
            zone_mapping_pairs(vm_rows, reservation_rows),
        )
        apply_physical_zones(vm_rows, reservation_rows, mappings)
        result = reconcile_vm_reservations(vm_rows, reservation_rows)[0]

        self.assertEqual("eastus-az2", result["physicalZone"])
        self.assertEqual("group", result["capacityReservationGroupName"])
        self.assertEqual(["/reservations/zone2"], result["matchingCapacityReservationIds"])

    def test_regional_reconciliation_deduplicates_and_sorts_candidates(self):
        vm_rows = [{
            "subscriptionId": "sub", "location": "westus", "logicalZone": None,
            "vmSize": "Standard_D2s_v5", "capacityReservationGroupId": "/groups/group",
        }]
        reservation = {
            "capacityReservationGroupId": "/groups/group",
            "capacityReservationGroupName": "group",
            "reservationSubscriptionId": "sub",
            "reservationLocation": "westus",
            "reservationZone": None,
            "reservationVmSize": "Standard_D2s_v5",
        }
        reservation_rows = [
            {**reservation, "matchingCapacityReservationId": "/reservations/b"},
            {**reservation, "matchingCapacityReservationId": "/reservations/a"},
            {**reservation, "matchingCapacityReservationId": "/reservations/a"},
        ]

        apply_physical_zones(vm_rows, reservation_rows, {})
        result = reconcile_vm_reservations(vm_rows, reservation_rows)[0]

        self.assertIsNone(result["physicalZone"])
        self.assertEqual(
            ["/reservations/a", "/reservations/b"],
            result["matchingCapacityReservationIds"],
        )

    def test_missing_zonal_mapping_preserves_broad_candidates(self):
        vm_rows = [{
            "subscriptionId": "vm-sub", "location": "eastus", "logicalZone": "1",
            "vmSize": "Standard_D2s_v5", "capacityReservationGroupId": "/groups/group",
        }]
        reservation_rows = [{
            "capacityReservationGroupId": "/groups/group",
            "capacityReservationGroupName": "group",
            "matchingCapacityReservationId": "/reservations/unknown-zone",
            "reservationSubscriptionId": "cr-sub", "reservationLocation": "eastus",
            "reservationZone": "3", "reservationVmSize": "Standard_D2s_v5",
        }]

        apply_physical_zones(vm_rows, reservation_rows, {})
        result = reconcile_vm_reservations(vm_rows, reservation_rows)[0]

        self.assertIsNone(result["physicalZone"])
        self.assertEqual(["/reservations/unknown-zone"], result["matchingCapacityReservationIds"])

    def test_filters_are_case_insensitive_deduplicated_and_escaped(self):
        query = build_vm_graph_query(
            resource_groups=["rg-a", "rg-b"], locations=["eastus"],
            deployment_types=["Regional", "Zone 2"], vm_sizes=["Standard_D2s_v5"],
            vmss_flexible=["true"], reservation_statuses=["Allocated, within capacity"],
        )

        self.assertNotIn("subscriptionId in~", query)
        self.assertIn("resourceGroup in~ ('rg-a', 'rg-b')", query)
        self.assertIn("location in~ ('eastus')", query)
        self.assertIn("deploymentType in~ ('Regional', 'Zone 2')", query)
        self.assertIn("vmSize in~ ('Standard_D2s_v5')", query)
        self.assertIn("isVmssFlexible in (true)", query)
        self.assertNotIn("reservationStatus in~", query)

    def test_subscription_scope_is_not_encoded_in_query(self):
        query = build_vm_graph_query()
        self.assertNotIn("subscriptionId in~", query)
        self.assertNotIn("| where false", query)

    def test_invalid_enum_filters_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "deploymentType"):
            build_vm_graph_query(deployment_types=["Zonal 1"])
        with self.assertRaisesRegex(ValueError, "vmssFlexible"):
            build_vm_graph_query(vmss_flexible=["Maybe"])
        with self.assertRaisesRegex(ValueError, "reservationStatus"):
            build_vm_graph_query(reservation_statuses=["Protected"])

    @patch("services.odcr_coverage._fetch_reservation_instance_view")
    def test_reservation_lifecycle_is_enriched_from_arm(self, fetch):
        reservation_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/capacityReservationGroups/group/capacityReservations/reservation"
        vm_id = "/subscriptions/sub/resourcegroups/vm-rg/providers/microsoft.compute/virtualmachines/vm1"
        fetch.return_value = {
            "name": "reservation", "sku": {"capacity": 2},
            "properties": {
                "virtualMachinesAssociated": [{"id": vm_id}],
                "instanceView": {"utilizationInfo": {"virtualMachinesAllocated": [{"id": vm_id}]}},
            },
        }
        rows = [{
            "id": vm_id,
            "capacityReservationGroupId": "/subscriptions/sub/resourcegroups/rg/providers/microsoft.compute/capacityreservationgroups/group",
            "matchingCapacityReservationIds": [reservation_id],
        }]

        enriched = enrich_vm_reservation_lifecycle(rows, "token")

        self.assertEqual("Allocated, within capacity", enriched[0]["reservationStatus"])
        self.assertEqual(2, enriched[0]["reservationCapacity"])
        self.assertEqual(1, enriched[0]["reservationAllocatedCount"])
        self.assertEqual(1, enriched[0]["reservationAssociatedCount"])
        self.assertEqual(reservation_id, enriched[0]["matchingCapacityReservationId"])
        self.assertEqual("reservation", enriched[0]["matchingCapacityReservationName"])
        fetch.assert_called_once_with(reservation_id, "token")

    @patch("services.odcr_coverage._fetch_reservation_instance_view")
    def test_association_selects_one_candidate_across_logical_zones(self, fetch):
        vm_id = "/subscriptions/vm-sub/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm1"
        reservation_ids = ["/reservations/zone1", "/reservations/zone2"]
        fetch.side_effect = [
            {
                "name": "zone1", "sku": {"capacity": 1},
                "properties": {
                    "virtualMachinesAssociated": [{"id": vm_id}],
                    "instanceView": {"utilizationInfo": {"virtualMachinesAllocated": [{"id": vm_id}]}},
                },
            },
            {
                "name": "zone2", "sku": {"capacity": 1},
                "properties": {
                    "virtualMachinesAssociated": [],
                    "instanceView": {"utilizationInfo": {"virtualMachinesAllocated": []}},
                },
            },
        ]
        rows = [{
            "id": vm_id,
            "capacityReservationGroupId": "/subscriptions/cr-sub/resourcegroups/rg/providers/microsoft.compute/capacityreservationgroups/group",
            "matchingCapacityReservationIds": reservation_ids,
        }]

        enriched = enrich_vm_reservation_lifecycle(rows, "token")

        self.assertEqual("/reservations/zone1", enriched[0]["matchingCapacityReservationId"])
        self.assertEqual("zone1", enriched[0]["matchingCapacityReservationName"])
        self.assertEqual("Allocated, within capacity", enriched[0]["reservationStatus"])

    def test_reservation_status_filter_runs_after_enrichment(self):
        rows = [
            {"id": "vm1", "reservationStatus": "Allocated, within capacity"},
            {"id": "vm2", "reservationStatus": "Not associated"},
        ]
        filtered = filter_vm_reservation_statuses(rows, ["Allocated, within capacity"])
        self.assertEqual(["vm1"], [row["id"] for row in filtered])

    def test_odcr_eligibility_overrides_status_for_hard_failures(self):
        rows = [{
            "id": "/vm/1", "vmSize": "Standard_D2s_v5",
            "reservationStatus": "Allocated, within capacity", "azureSpot": 1,
            "isOnDedicatedHost": 0, "hasAvailabilitySet": 0,
            "hasProximityPlacementGroup": 0, "hasUltraDiskStorageAttached": 0,
            "hasLegacyVMNVATag": 1, "capacityReservationSupported": True,
        }]

        result = apply_odcr_eligibility(rows)[0]

        self.assertEqual("Allocated, within capacity", result["reservationStatus"])
        self.assertNotIn("reservationLifecycleStatus", result)
        self.assertEqual([
            "Not an Azure Spot instance",
            "Eligible for the ODCR capacity SLA (no LegacyVMNVA tag)",
        ], result["odcrUnsupportedReasons"])
        statuses = {item["key"]: item["status"] for item in result["odcrRequirements"]}
        self.assertEqual("Not applicable", statuses["vmssPlacementModel"])
        self.assertNotIn("hibernationResume", statuses)
        self.assertEqual("Passed", statuses["vmSkuSupport"])
        self.assertEqual("Unsupported", result["odcrSupportabilityStatus"])

    def test_unknown_sku_lookup_sets_explicit_unknown_status(self):
        rows = [{
            "id": "/vm/1", "vmSize": "Standard_D2s_v5", "reservationStatus": "Not associated",
            "azureSpot": False, "isOnDedicatedHost": False, "hasAvailabilitySet": False,
            "hasProximityPlacementGroup": False, "hasUltraDiskStorageAttached": False,
            "hasLegacyVMNVATag": False, "capacityReservationSupported": None,
            "capacityReservationSupportReason": "No regional SKU record was found.",
        }]

        result = apply_odcr_eligibility(rows)[0]

        self.assertEqual("Not associated", result["reservationStatus"])
        self.assertNotIn("reservationLifecycleStatus", result)
        self.assertEqual([], result["odcrUnsupportedReasons"])
        self.assertEqual(["No regional SKU record was found."], result["odcrUnknownReasons"])
        self.assertEqual("Unknown", result["odcrSupportabilityStatus"])

    def test_vm_list_projection_keeps_only_non_passing_requirement_results(self):
        row = {
            "logicalZone": "1", "physicalZone": "2", "deploymentType": "Zone 1",
            "isVmssFlexible": False,
            "capacityReservationGroupId": "/capacityReservationGroups/group",
            "matchingCapacityReservationId": "/capacityReservations/reservation",
            "matchingCapacityReservationIds": ["/capacityReservations/reservation"],
            "azureSpot": False, "dedicatedHostId": "", "dedicatedHost": "",
            "isOnDedicatedHost": False,
            "availabilitySetId": "/availabilitySets/availability-set-a",
            "availabilitySet": "availability-set-a", "hasAvailabilitySet": True,
            "proximityPlacementGroupId": "", "proximityPlacementGroup": "",
            "hasProximityPlacementGroup": False, "hasUltraDiskStorageAttached": False,
            "hasLegacyVMNVATag": False, "capacityReservationSupported": None,
            "capacityReservationSupportReason": "Compute SKU catalog lookup failed: unavailable",
            "reservationLifecycleStatus": "Not associated",
            "odcrRequirements": [
                {"key": "regularPriority", "status": "Passed", "observedValue": "Azure Spot: No"},
                {
                    "key": "availabilitySet", "status": "Failed",
                    "observedValue": "availability-set-a",
                    "reason": (
                        "Availability sets aren't supported by ODCR. The VM's update-domain placement "
                        "is inherited from this availability set and isn't reported as a separate failure."
                    ),
                    "resourceId": "/availabilitySets/availability-set-a",
                },
                {"key": "vmssPlacementModel", "status": "Not applicable"},
                {
                    "key": "vmSkuSupport", "status": "Unknown",
                    "observedValue": "Standard_D2s_v5 in eastus: Unknown",
                    "reason": "Compute SKU catalog lookup failed: unavailable",
                },
            ],
            "odcrUnsupportedReasons": ["No availability set"],
            "odcrUnknownReasons": ["Compute SKU catalog lookup failed: unavailable"],
        }

        result = project_vm_list_row(row)

        self.assertEqual("/capacityReservationGroups/group", result["capacityReservationGroupId"])
        self.assertEqual("/capacityReservations/reservation", result["matchingCapacityReservationId"])
        self.assertEqual("1", result["logicalZone"])
        self.assertEqual("2", result["physicalZone"])
        self.assertIs(False, result["isVmssFlexible"])
        for field in (
            "deploymentType", "azureSpot", "dedicatedHostId", "dedicatedHost",
            "isOnDedicatedHost", "availabilitySetId", "availabilitySet", "hasAvailabilitySet",
            "proximityPlacementGroupId", "proximityPlacementGroup", "hasProximityPlacementGroup",
            "hasUltraDiskStorageAttached", "hasLegacyVMNVATag", "capacityReservationSupported",
            "capacityReservationSupportReason", "reservationLifecycleStatus",
            "matchingCapacityReservationIds", "odcrUnsupportedReasons", "odcrUnknownReasons",
            "odcrUnsupportedRequirementCodes", "odcrUnknownRequirementCodes",
        ):
            self.assertNotIn(field, result)
        self.assertEqual(
            [
                {
                    "key": "availabilitySet", "status": "Failed",
                    "observedValue": "availability-set-a",
                    "resourceId": "/availabilitySets/availability-set-a",
                },
                {"key": "vmssPlacementModel", "status": "Not applicable"},
                {
                    "key": "vmSkuSupport", "status": "Unknown",
                    "reason": "Compute SKU catalog lookup failed: unavailable",
                    "observedValue": "Standard_D2s_v5 in eastus: Unknown",
                },
            ],
            result["odcrRequirements"],
        )

    def test_regionally_unsupported_sku_is_a_hard_failure(self):
        rows = [{
            "id": "/vm/1", "vmSize": "Standard_D16lds_v5", "location": "qatarcentral",
            "reservationStatus": "Not associated", "azureSpot": False,
            "isOnDedicatedHost": False, "hasAvailabilitySet": False,
            "hasProximityPlacementGroup": False, "hasUltraDiskStorageAttached": False,
            "hasLegacyVMNVATag": False, "capacityReservationSupported": False,
        }]

        result = apply_odcr_eligibility(rows)[0]
        requirement = next(item for item in result["odcrRequirements"] if item["key"] == "vmSkuSupport")

        self.assertEqual("Failed", requirement["status"])
        self.assertEqual("Not associated", result["reservationStatus"])
        self.assertEqual(["VM SKU supports ODCR"], result["odcrUnsupportedReasons"])

    def test_odcr_failure_preserves_associated_and_allocated_lifecycle(self):
        base = {
            "id": "/vm/1", "vmSize": "Standard_D2s_v5", "azureSpot": False,
            "isOnDedicatedHost": False, "hasAvailabilitySet": False,
            "hasProximityPlacementGroup": False, "hasUltraDiskStorageAttached": False,
            "hasLegacyVMNVATag": True, "capacityReservationSupported": True,
            "matchingCapacityReservationName": "reservation",
        }
        rows = [
            {**base, "reservationStatus": "Allocated, within capacity", "isCapacityReservationAllocated": True},
            {**base, "reservationStatus": "Associated, capacity available", "isCapacityReservationAllocated": False},
        ]

        result = apply_odcr_eligibility(rows)

        self.assertEqual("Allocated, within capacity", result[0]["reservationStatus"])
        self.assertEqual("Associated, capacity available", result[1]["reservationStatus"])
        self.assertEqual("Unsupported", result[0]["odcrSupportabilityStatus"])
        self.assertEqual("Unsupported", result[1]["odcrSupportabilityStatus"])

    @patch("services.odcr_coverage.time.sleep")
    @patch("services.odcr_coverage._RESERVATION_HTTP.request")
    def test_reservation_fetch_honors_retry_after(self, request, sleep):
        request.side_effect = [
            SimpleNamespace(status=429, headers={"Retry-After": "3"}, data=b"throttled"),
            SimpleNamespace(status=200, headers={}, data=b'{"sku":{"capacity":1}}'),
        ]

        result = _fetch_reservation_instance_view("/reservation", "token")

        self.assertEqual(1, result["sku"]["capacity"])
        sleep.assert_called_once_with(3.0)
        self.assertEqual(2, request.call_count)

    @patch("services.odcr_coverage.time.sleep")
    @patch("services.odcr_coverage._RESERVATION_HTTP.request")
    def test_reservation_fetch_retries_network_error_with_fixed_delay(self, request, sleep):
        success = SimpleNamespace(status=200, headers={}, data=b'{"sku":{"capacity":1}}')
        request.side_effect = [urllib3.exceptions.ProtocolError("connection reset"), success]

        result = _fetch_reservation_instance_view("/reservation", "token")

        self.assertEqual(1, result["sku"]["capacity"])
        sleep.assert_called_once_with(1.0)
        self.assertEqual(2, request.call_count)


if __name__ == "__main__":
    unittest.main()
