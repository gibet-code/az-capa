from __future__ import annotations

import pytest

from clients.azure_locations import (
    map_location_catalogue_entries,
    map_zone_mapping_regions,
)


def test_map_zone_mapping_regions_stores_physical_zone_numbers():
    payload = {"value": [{
        "name": "northeurope",
        "type": "Region",
        "metadata": {"regionType": "Physical"},
        "availabilityZoneMappings": [
            {"logicalZone": "1", "physicalZone": "northeurope-az2"},
            {"logicalZone": "2", "physicalZone": "northeurope-az1"},
        ],
    }]}

    assert map_zone_mapping_regions(payload)[0]["zoneMappings"] == {"1": "2", "2": "1"}


def test_map_zone_mapping_regions_rejects_unexpected_physical_zone_names():
    payload = {"value": [{
        "name": "northeurope",
        "metadata": {"regionType": "Physical"},
        "availabilityZoneMappings": [{"logicalZone": "1", "physicalZone": "unknown"}],
    }]}

    with pytest.raises(ValueError, match="Unexpected ARM physical zone"):
        map_zone_mapping_regions(payload)


def test_map_location_catalogue_entries_preserves_display_metadata():
    payload = {"value": [{
        "name": "WestEurope",
        "displayName": "West Europe",
        "regionalDisplayName": "(Europe) West Europe",
        "metadata": {"regionType": "Physical"},
    }]}

    assert map_location_catalogue_entries(payload) == [{
        "name": "westeurope",
        "displayName": "West Europe",
        "regionalDisplayName": "(Europe) West Europe",
        "regionType": "Physical",
    }]


def test_map_location_catalogue_entries_skips_nonphysical_and_nameless_values():
    payload = {"value": [
        {"name": "logical", "metadata": {"regionType": "Logical"}},
        {"displayName": "Missing name", "metadata": {"regionType": "Physical"}},
    ]}

    assert map_location_catalogue_entries(payload) == []