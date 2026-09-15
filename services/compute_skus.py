"""Compute SKU catalog retrieval, normalization, and enrichment."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from clients.compute_skus import fetch_catalog_pages
from core import settings
from core.app_scope import MODE_MANAGEMENT_GROUPS, MODE_SUBSCRIPTIONS, configured_scope
from core.identity import for_backend_operation
from services.app_scope import resolve_app_scope
from storage.compute_sku_store import ComputeSkuBlobStore

_VM_RESOURCE_TYPE = "virtualmachines"
_RETIREMENT_NEVER = date(9999, 1, 1)
_RETIRED_SIZES_LIST_URL = (
    "https://learn.microsoft.com/azure/virtual-machines/sizes/retirement/"
    "retired-sizes-list"
)

_RETIREMENT_BY_FAMILY = {
    "standarddfamily": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standarddsfamily": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standarddv2family": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standarddsv2family": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standarddv2promofamily": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standarddsv2promofamily": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standardlsfamily": ("2028-05-01", "https://azure.microsoft.com/updates/?id=485569"),
    "standardffamily": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardfsfamily": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardfsv2family": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardlsv2family": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardgfamily": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardgsfamily": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardav2family": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardamv2family": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardbsfamily": ("2028-11-15", "https://azure.microsoft.com/updates/?id=500682"),
    "standardnvsv3family": ("2026-09-30", _RETIRED_SIZES_LIST_URL),
    "standardnvsv4family": ("2026-09-30", _RETIRED_SIZES_LIST_URL),
    "standardnpsfamily": ("2027-05-31", _RETIRED_SIZES_LIST_URL),
    "standarddcsv2family": ("2026-06-30", _RETIRED_SIZES_LIST_URL),
}

_RETIREMENT_BY_SIZE = {
    "standard_nc6s_v3": ("2025-09-30", _RETIRED_SIZES_LIST_URL),
    "standard_nc12s_v3": ("2025-09-30", _RETIRED_SIZES_LIST_URL),
    "standard_nc24s_v3": ("2025-09-30", _RETIRED_SIZES_LIST_URL),
    "standard_nc24rs_v3": ("2025-09-30", _RETIRED_SIZES_LIST_URL),
    "standard_m192idms_v2": ("2027-03-31", _RETIRED_SIZES_LIST_URL),
    "standard_m192ids_v2": ("2027-03-31", _RETIRED_SIZES_LIST_URL),
    "standard_m192ims_v2": ("2027-03-31", _RETIRED_SIZES_LIST_URL),
    "standard_m192is_v2": ("2027-03-31", _RETIRED_SIZES_LIST_URL),
    "standard_gs5": ("2028-05-01", "https://azure.microsoft.com/updates/?id=500682"),
}

_CATEGORY_RULES = [
    ("NC", "Accelerated (GPU)"), ("ND", "Accelerated (GPU)"),
    ("NG", "Accelerated (GPU)"), ("NV", "Accelerated (GPU)"),
    ("NM", "Accelerated (FPGA)"), ("NP", "Accelerated (FPGA)"),
    ("PB", "Accelerated (FPGA)"), ("HB", "HPC"), ("HC", "HPC"),
    ("HX", "HPC"), ("DC", "Confidential"), ("EC", "Confidential"),
    ("M", "M Series"), ("FX", "Compute optimized"),
    ("F", "Compute optimized"), ("E", "Memory optimized"),
    ("G", "Memory optimized"), ("L", "Storage optimized"),
    ("A", "General purpose"), ("B", "General purpose"),
    ("D", "General purpose"),
]
_PROCESSOR_CATEGORIES = {
    "General purpose", "Compute optimized", "Memory optimized",
    "Storage optimized", "Confidential",
}
_FAMILY_VERSION_CUT = re.compile(r"V\d")


class SkuCatalogConflict(ValueError):
    """The API returned incompatible properties for one SKU and location."""


def _family_token(family: str | None) -> str:
    token = re.sub(r"(?i)^standard", "", (family or "").strip())
    token = re.sub(r"(?i)family$", "", token)
    token = re.sub(r"(?i)promo", "", token)
    return re.sub(r"[^A-Z0-9]", "", token.upper())


def friendly_family_name(family: str | None) -> str | None:
    """Normalize an ARM family identifier to an Azure series label."""
    token = _family_token(family)
    if not token:
        return None
    return token[0] + token[1:].lower()


def classify_family(family: str | None) -> tuple[str | None, str | None]:
    token = _family_token(family)
    if not token:
        return None, None
    for prefix, category in _CATEGORY_RULES:
        if token.startswith(prefix):
            processor = None
            if category in _PROCESSOR_CATEGORIES:
                base = _FAMILY_VERSION_CUT.split(token[len(prefix):], maxsplit=1)[0]
                processor = "AMD" if "A" in base else "ARM" if "P" in base else "Intel"
            return category, processor
    return "Others", None


def _capabilities(item: dict) -> dict[str, str | None]:
    return {
        str(capability.get("name") or "").lower(): capability.get("value")
        for capability in item.get("capabilities") or []
        if capability.get("name")
    }


def _integer(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _decimal(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return None


def _retirement_date(value: str | None) -> date | None:
    if not value:
        return None
    for format_string in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, format_string).date()
            return None if parsed == _RETIREMENT_NEVER else parsed
        except ValueError:
            continue
    return None


def lookup_retirement(
    name: str | None,
    family: str | None,
    api_retirement_date: date | None,
    today: date | None = None,
) -> tuple[str | None, str | None, str | None]:
    curated = _RETIREMENT_BY_SIZE.get((name or "").lower())
    if curated is None:
        curated = _RETIREMENT_BY_FAMILY.get((family or "").lower())
    if api_retirement_date is not None:
        retirement_date = api_retirement_date.isoformat()
        retirement_link = curated[1] if curated else _RETIRED_SIZES_LIST_URL
    elif curated is not None:
        retirement_date, retirement_link = curated
    else:
        return None, None, None
    reference_date = today or date.today()
    status = "Retired" if retirement_date <= reference_date.isoformat() else "Announced"
    return status, retirement_date, retirement_link


def normalize_sku(item: dict, location: str, today: date | None = None) -> dict | None:
    if str(item.get("resourceType") or "").lower() != _VM_RESOURCE_TYPE:
        return None
    name = item.get("name")
    if not name:
        return None
    capabilities = _capabilities(item)
    family = item.get("family")
    category, processor = classify_family(family)
    retirement_status, retirement_date, retirement_link = lookup_retirement(
        name, family, _retirement_date(capabilities.get("retirementdateutc")), today
    )
    return {
        "name": name,
        "location": location.lower(),
        "size": item.get("size"),
        "tier": item.get("tier"),
        "family": family,
        "friendlyFamily": friendly_family_name(family),
        "vcpus": _integer(capabilities.get("vcpus")),
        "memoryGB": _decimal(capabilities.get("memorygb")),
        "category": category,
        "processor": processor,
        "capacityReservationSupported": _boolean(
            capabilities.get("capacityreservationsupported")
        ),
        "retirementStatus": retirement_status,
        "retirementDate": retirement_date,
        "retirementLink": retirement_link,
    }


def build_unique_catalog(items: list[dict], today: date | None = None) -> list[dict]:
    """Return one VM SKU record per unique (SKU name, location)."""
    catalog: dict[tuple[str, str], dict] = {}
    for item in items:
        locations = sorted({
            str(location).strip().lower()
            for location in item.get("locations") or []
            if str(location).strip()
        })
        if not locations:
            continue
        for location in locations:
            normalized = normalize_sku(item, location, today)
            if normalized is None:
                continue
            key = (normalized["name"].lower(), location)
            existing = catalog.get(key)
            comparable = {**normalized, "name": key[0]}
            existing_comparable = {**existing, "name": key[0]} if existing is not None else None
            if existing_comparable is not None and existing_comparable != comparable:
                differing = sorted(
                    field
                    for field in comparable
                    if existing_comparable.get(field) != comparable.get(field)
                )
                raise SkuCatalogConflict(
                    f"Conflicting properties for SKU {normalized['name']} in {location}: "
                    f"{', '.join(differing)}"
                )
            if existing is None:
                catalog[key] = normalized
    return [catalog[key] for key in sorted(catalog)]


def _fetch_catalog(subscription_id: str, token: str) -> dict:
    """Retrieve every page and normalize the union across regions."""
    result = fetch_catalog_pages(subscription_id, token)
    if result["status"] != "ok":
        return result
    try:
        catalog = build_unique_catalog(result["items"])
    except SkuCatalogConflict as exc:
        return {"status": "error", "error": str(exc)}
    return {
        "status": "ok",
        "catalog": catalog,
        "records": result["records"],
        "pages": result["pages"],
    }


class ComputeSkuPipeline:
    def __init__(self, store: ComputeSkuBlobStore) -> None:
        self._store = store

    def _scope(self, identity) -> dict:
        configured = configured_scope()
        subscriptions = resolve_app_scope(
            identity, configured, allow_live_fallback=True
        ).subscription_ids
        return {
            "subs": list(subscriptions),
            "configSelectors": {
                "subscriptions": list(configured.selectors) if configured.mode == MODE_SUBSCRIPTIONS else [],
                "managementGroups": list(configured.selectors) if configured.mode == MODE_MANAGEMENT_GROUPS else [],
            },
        }

    @staticmethod
    def _describe(scope: dict) -> str:
        selectors = scope.get("configSelectors") or {}
        if selectors.get("managementGroups"):
            source = f"management group(s) {selectors['managementGroups']}"
        elif selectors.get("subscriptions"):
            source = f"{len(scope['subs'])} configured subscription(s)"
        else:
            source = "subscriptions visible to the calling identity"
        return f"scope={source} ({len(scope['subs'])} subscriptions), locations=all"

    def resolve_plan(self, _request: dict) -> dict:
        try:
            scope = self._scope(for_backend_operation())
        except Exception as exc:  # noqa: BLE001 - returned as an orchestration result
            return {"action": "fail", "reason": "scope resolution failed", "message": str(exc)}
        if not scope["subs"]:
            return {
                "action": "fail",
                "reason": "empty scope",
                "message": "The app scope contains no subscriptions.",
            }
        return {
            "action": "load",
            "sourceSubscriptionId": sorted(scope["subs"])[0],
            "scope": scope,
        }

    def fetch_and_store(self, request: dict) -> dict:
        subscription_id = request["sourceSubscriptionId"]
        token = for_backend_operation().get_token()
        result = _fetch_catalog(subscription_id, token)
        if result["status"] in ("throttled", "transient"):
            return {
                "status": result["status"],
                "retryAfter": result.get("retryAfter", 5),
                "statusCode": result.get("statusCode"),
            }
        if result["status"] == "error":
            return {"status": "error", "error": result.get("error")}
        try:
            generation = self._store.publish_catalog(
                result["catalog"], subscription_id, request["scope"]
            )
        except Exception as exc:  # noqa: BLE001 - returned to Durable as a failed refresh
            logging.exception("Compute SKU catalog persistence failed")
            return {"status": "error", "error": str(exc)}
        return {
            "status": "ok",
            "generation": generation,
            "skus": len({sku["name"].lower() for sku in result["catalog"]}),
            "skuLocations": len(result["catalog"]),
            "records": result["records"],
            "pages": result["pages"],
        }

    def status(self, _request: dict) -> dict:
        scope = self._scope(for_backend_operation())
        state = self._store.get_state()
        updated_at = state.get("updatedAt") if state else None
        return {
            "hasData": state is not None,
            "isRefreshEnabled": True,
            "warning": None,
            "skuCount": state["skuCount"] if state else 0,
            "skuLocationCount": state["skuLocationCount"] if state else 0,
            "sourceSubscriptionId": state["sourceSubscriptionId"] if state else None,
            "lastUpdated": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
            "currentScope": self._describe(scope),
            "lastLoadedScope": self._describe(state["scope"]) if state else None,
        }

    def enrich_vm_rows(self, rows: list[dict]) -> list[dict]:
        """Attach regional ODCR capability data to VM inventory rows."""
        pairs = {
            (str(row.get("vmSize") or ""), str(row.get("location") or ""))
            for row in rows
            if row.get("vmSize") and row.get("location")
        }
        try:
            matches, errors = self._store.find_pairs(pairs)
        except Exception as exc:  # noqa: BLE001 - SKU lookup failure is an unknown prerequisite
            matches = {}
            errors = {
                (sku_name.lower(), location.lower()): f"Compute SKU catalog lookup failed: {exc}"
                for sku_name, location in pairs
            }
        for row in rows:
            sku_name = str(row.get("vmSize") or "").strip()
            location = str(row.get("location") or "").strip().lower()
            pair = (sku_name.lower(), location)
            row["capacityReservationSupported"] = None
            row["capacityReservationSupportReason"] = None
            if not sku_name or not location:
                missing = "VM size" if not sku_name else "location"
                row["capacityReservationSupportReason"] = (
                    f"The VM inventory does not contain a {missing} for this lookup."
                )
                continue
            if pair in errors:
                row["capacityReservationSupportReason"] = errors[pair]
                continue
            match = matches.get(pair)
            if match is None:
                row["capacityReservationSupportReason"] = (
                    f"No Compute SKU catalog record was found for {sku_name} in {location}."
                )
                continue
            supported = match.get("capacityReservationSupported")
            if supported is None:
                row["capacityReservationSupportReason"] = (
                    f"The Compute SKU catalog does not report ODCR support for {sku_name} in {location}."
                )
                continue
            row["capacityReservationSupported"] = supported
        return rows

    def flush_delete_storage(self, _input: dict) -> dict:
        return {"deleted": self._store.delete_storage()}

    def flush_ensure_storage(self, _input: dict) -> dict:
        return {"ready": self._store.ensure_storage()}


_store = ComputeSkuBlobStore(settings.compute_sku_blob_container_name)
pipeline = ComputeSkuPipeline(_store)