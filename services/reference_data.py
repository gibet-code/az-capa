"""Typed Azure Reference Data views and refresh coordination."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from clients.azure_locations import fetch_location_catalogue
from clients.resource_graph import get_subscription_inventory
from core.identity import for_backend_operation
from services.app_scope import resolve_app_scope
from storage.reference_data_store import (
    LOCATIONS,
    SUBSCRIPTIONS,
    ReferenceDataStore,
    store as reference_store,
)


def _canonical(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_subscriptions(inventory: Iterable[dict]) -> dict[str, dict]:
    values: dict[str, dict] = {}
    for item in inventory:
        subscription_id = _canonical(item.get("id"))
        if not subscription_id:
            continue
        entry = {
            "displayName": str(item.get("name") or "").strip() or None,
            "state": str(item.get("state") or "").strip() or None,
            "managementGroupAncestors": list(item.get("managementGroupAncestors") or []),
        }
        existing = values.get(subscription_id)
        if existing is not None and existing != entry:
            raise ValueError(f"Conflicting Subscription reference data for {subscription_id}.")
        values[subscription_id] = entry
    return values


def normalize_locations(entries: Iterable[dict]) -> dict[str, dict]:
    values: dict[str, dict] = {}
    for item in entries:
        name = _canonical(item.get("name"))
        if not name:
            continue
        entry = {
            "displayName": str(item.get("displayName") or "").strip() or None,
            "regionalDisplayName": str(item.get("regionalDisplayName") or "").strip() or None,
        }
        existing = values.get(name)
        if existing is not None and existing != entry:
            raise ValueError(f"Conflicting Location reference data for {name}.")
        values[name] = entry
    return values


class ReferenceDataView:
    def __init__(self, store: ReferenceDataStore, catalogues: Iterable[str]) -> None:
        self._store = store
        self._catalogues = frozenset(catalogues)
        unknown = self._catalogues - {SUBSCRIPTIONS, LOCATIONS}
        if unknown:
            raise ValueError(f"Unsupported reference-data catalogue(s): {sorted(unknown)}.")
        self._snapshots: dict[str, dict | None] = {}

    def _snapshot(self, kind: str) -> dict | None:
        if kind not in self._catalogues:
            raise ValueError(f"Reference-data capability {kind!r} was not requested.")
        if kind not in self._snapshots:
            self._snapshots[kind] = self._store.open_snapshot(kind)
        return self._snapshots[kind]

    def subscription_name(self, subscription_id: str) -> str:
        canonical = _canonical(subscription_id)
        snapshot = self._snapshot(SUBSCRIPTIONS)
        entry = (snapshot.get("values") or {}).get(canonical) if snapshot else None
        return str((entry or {}).get("displayName") or canonical)

    def location_display_name(self, location: str) -> str:
        canonical = _canonical(location)
        snapshot = self._snapshot(LOCATIONS)
        entry = (snapshot.get("values") or {}).get(canonical) if snapshot else None
        return str((entry or {}).get("displayName") or canonical)

    def enrich_rows(self, rows: list[dict]) -> list[dict]:
        for row in rows:
            if SUBSCRIPTIONS in self._catalogues and row.get("subscriptionId"):
                row["subscriptionName"] = self.subscription_name(row["subscriptionId"])
            if LOCATIONS in self._catalogues and row.get("location"):
                row["locationDisplayName"] = self.location_display_name(row["location"])
        return rows

    def options(self, kind: str, allowed_values: Iterable[str] | None = None) -> list[dict]:
        snapshot = self._snapshot(kind)
        values = (snapshot or {}).get("values") or {}
        allowed = (
            {_canonical(value) for value in allowed_values if _canonical(value)}
            if allowed_values is not None
            else None
        )
        result = []
        for canonical, entry in values.items():
            if allowed is not None and canonical not in allowed:
                continue
            result.append({
                "value": canonical,
                "label": str((entry or {}).get("displayName") or canonical),
                "secondaryLabel": canonical,
            })
        if allowed is not None:
            for canonical in allowed - set(values):
                result.append({"value": canonical, "label": canonical, "secondaryLabel": canonical})
        return sorted(result, key=lambda item: (item["label"].casefold(), item["value"]))

    def metadata(self) -> dict:
        result = {}
        for kind in self._catalogues:
            snapshot = self._snapshot(kind)
            result[kind] = {
                "generation": snapshot.get("generation") if snapshot else None,
                "publishedAt": snapshot.get("publishedAt") if snapshot else None,
            }
        return result


def open_current_view(
    catalogues: Iterable[str],
    store: ReferenceDataStore = reference_store,
) -> ReferenceDataView:
    return ReferenceDataView(store, catalogues)


def _status(store: ReferenceDataStore, kind: str, maximum_age: timedelta) -> dict:
    snapshot = store.open_snapshot(kind)
    published_at = snapshot.get("publishedAt") if snapshot else None
    stale = False
    if published_at:
        resolved = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        stale = datetime.now(timezone.utc) - resolved > maximum_age
    return {
        "hasData": snapshot is not None,
        "itemCount": len(snapshot.get("values") or {}) if snapshot else 0,
        "generation": snapshot.get("generation") if snapshot else None,
        "publishedAt": published_at,
        "stale": stale,
        "source": snapshot.get("source") if snapshot else None,
    }


class SubscriptionReferencePipeline:
    def __init__(self, store: ReferenceDataStore = reference_store) -> None:
        self._store = store

    def refresh(self, _request: dict) -> dict:
        try:
            identity = for_backend_operation()
            inventory = get_subscription_inventory(identity.get_token())
            values = normalize_subscriptions(inventory)
            generation = self._store.publish(
                SUBSCRIPTIONS,
                values,
                {"inventoryCount": len(inventory)},
            )
            return {"status": "ok", "generation": generation, "itemCount": len(values)}
        except Exception as exc:  # noqa: BLE001 - returned to Durable as an operation result
            return {"status": "error", "error": str(exc)}

    def status(self) -> dict:
        from core import settings
        return _status(
            self._store,
            SUBSCRIPTIONS,
            timedelta(minutes=settings.subscription_reference_max_stale_minutes()),
        )


class LocationReferencePipeline:
    def __init__(self, store: ReferenceDataStore = reference_store) -> None:
        self._store = store

    def refresh(self, _request: dict) -> dict:
        try:
            identity = for_backend_operation()
            scope = resolve_app_scope(identity, allow_live_fallback=True)
            if not scope.subscription_ids:
                return {"status": "error", "error": "The app scope contains no subscriptions."}
            subscription_id = sorted(scope.subscription_ids)[0]
            result = fetch_location_catalogue(subscription_id, identity.get_token())
            if result.get("status") != "ok":
                return result
            values = normalize_locations(result.get("locations") or [])
            generation = self._store.publish(
                LOCATIONS,
                values,
                {"subscriptionId": subscription_id},
            )
            return {
                "status": "ok",
                "generation": generation,
                "itemCount": len(values),
                "sourceSubscriptionId": subscription_id,
            }
        except Exception as exc:  # noqa: BLE001 - returned to Durable as an operation result
            return {"status": "error", "error": str(exc)}

    def status(self) -> dict:
        from core import settings
        return _status(
            self._store,
            LOCATIONS,
            timedelta(hours=settings.location_reference_max_stale_hours()),
        )


subscription_pipeline = SubscriptionReferencePipeline()
location_pipeline = LocationReferencePipeline()