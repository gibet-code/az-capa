"""Azure subscription-locations client and availability-zone mapping normalization."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ._http import TRANSIENT_STATUS_CODES, azure_error_detail, retry_after_seconds

API_VERSION = "2022-12-01"
_PHYSICAL_ZONE_PATTERN = re.compile(r"-az([1-9][0-9]*)\Z", re.IGNORECASE)


def build_url(subscription_id: str) -> str:
    base = (
        "https://management.azure.com/subscriptions/"
        f"{urllib.parse.quote(subscription_id, safe='')}/locations"
    )
    return f"{base}?api-version={API_VERSION}"


def _physical_zone_number(value: str) -> str:
    match = _PHYSICAL_ZONE_PATTERN.search(str(value).strip())
    if not match:
        raise ValueError(f"Unexpected ARM physical zone value: {value!r}")
    return match.group(1)


def map_zone_mapping_regions(payload: dict) -> list[dict]:
    """Extract physical regions and their logical-to-physical zone mappings."""
    regions = []
    for location in payload.get("value", []) or []:
        metadata = location.get("metadata") or {}
        if metadata.get("regionType") != "Physical":
            continue
        mappings = location.get("availabilityZoneMappings") or []
        zone_map = {
            str(entry.get("logicalZone")): _physical_zone_number(entry.get("physicalZone"))
            for entry in mappings
            if entry.get("logicalZone") and entry.get("physicalZone")
        }
        regions.append({
            "region": location.get("name"),
            "regionType": location.get("type"),
            "zoneMappings": zone_map,
            "supportsAvailabilityZones": bool(zone_map),
        })
    return regions


def map_location_catalogue_entries(payload: dict) -> list[dict]:
    """Extract physical-region identifiers and Azure display metadata."""
    entries = []
    for location in payload.get("value", []) or []:
        metadata = location.get("metadata") or {}
        if metadata.get("regionType") != "Physical":
            continue
        name = str(location.get("name") or "").strip().lower()
        if not name:
            continue
        entries.append({
            "name": name,
            "displayName": str(location.get("displayName") or "").strip() or None,
            "regionalDisplayName": str(location.get("regionalDisplayName") or "").strip() or None,
            "regionType": "Physical",
        })
    return entries


def _fetch_normalized_locations(
    subscription_id: str,
    token: str,
    mapper: Callable[[dict], list[dict[str, Any]]],
    result_key: str,
) -> dict:
    url = build_url(subscription_id)
    items: list[dict] = []
    while url:
        http_request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(http_request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_STATUS_CODES:
                return {
                    "status": "throttled" if exc.code == 429 else "transient",
                    "statusCode": exc.code,
                    "retryAfter": retry_after_seconds(exc.headers),
                }
            return {"status": "error", "statusCode": exc.code, "error": azure_error_detail(exc.read())}
        except (urllib.error.URLError, TimeoutError) as exc:
            return {"status": "transient", "retryAfter": 5, "error": str(exc)}
        items.extend(mapper(payload))
        url = payload.get("nextLink")
    return {"status": "ok", result_key: items}


def fetch_zone_mapping_locations(subscription_id: str, token: str) -> dict:
    """Fetch physical regions normalized for logical-to-physical zone mapping."""
    return _fetch_normalized_locations(
        subscription_id,
        token,
        map_zone_mapping_regions,
        "regions",
    )


def fetch_location_catalogue(subscription_id: str, token: str) -> dict:
    """Fetch physical regions normalized for the Location reference catalogue."""
    return _fetch_normalized_locations(
        subscription_id,
        token,
        map_location_catalogue_entries,
        "locations",
    )