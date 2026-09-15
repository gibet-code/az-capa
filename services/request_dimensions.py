"""Request-scoped dimensions facade over immutable reference snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from core import settings
from services import reference_data
from storage.reference_data_store import LOCATIONS, SUBSCRIPTIONS, ReferenceDataStore
from storage.subscription_context_store import SubscriptionContextStore

BUSINESS_CONTEXT = "business_context"
_CAPABILITIES = {SUBSCRIPTIONS, LOCATIONS, BUSINESS_CONTEXT}


def _canonical(value: object) -> str:
    return str(value or "").strip().lower()


def parse_context_filters(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("contextFilters must be an array.")
    parsed = []
    seen: set[str] = set()
    for criterion in value:
        if not isinstance(criterion, dict):
            raise ValueError("Each Context filter must be an object.")
        field_key = _canonical(criterion.get("fieldKey"))
        values = criterion.get("values")
        if not field_key:
            raise ValueError("Each Context filter requires a Field key.")
        if field_key in seen:
            raise ValueError(f"Context Field key {field_key!r} may appear only once.")
        if not isinstance(values, list) or not values:
            raise ValueError("Each Context filter requires a non-empty values array.")
        if any(
            item is not None and (not isinstance(item, str) or not item.strip())
            for item in values
        ):
            raise ValueError("Context filter values must be non-empty strings or null.")
        seen.add(field_key)
        parsed.append({"fieldKey": field_key, "values": values})
    return parsed


class RequestDimensionsView:
    def __init__(
        self,
        capabilities: Iterable[str],
        reference_store: ReferenceDataStore,
        context_store: SubscriptionContextStore,
    ) -> None:
        self._capabilities = frozenset(capabilities)
        unknown = self._capabilities - _CAPABILITIES
        if unknown:
            raise ValueError(f"Unsupported request-dimension capability(s): {sorted(unknown)}.")
        reference_capabilities = self._capabilities & {SUBSCRIPTIONS, LOCATIONS}
        self._reference_view = reference_data.open_current_view(reference_capabilities, reference_store)
        self._context_store = context_store
        self._context_snapshot: dict | None = None
        self._context_loaded = False

    def _require(self, capability: str) -> None:
        if capability not in self._capabilities:
            raise ValueError(f"Request-dimension capability {capability!r} was not requested.")

    def _context(self) -> dict | None:
        self._require(BUSINESS_CONTEXT)
        if not self._context_loaded:
            self._context_snapshot = self._context_store.open_snapshot()
            self._context_loaded = True
        return self._context_snapshot

    @property
    def enabled_fields(self) -> dict[str, str]:
        snapshot = self._context()
        return {
            key: str(metadata.get("name") or key)
            for key, metadata in ((snapshot or {}).get("fields") or {}).items()
            if metadata.get("enabled")
        }

    def filter_subscriptions(
        self,
        app_subscription_ids: Iterable[str],
        subscription_ids: Iterable[str] | None = None,
        context_filters: list[dict] | None = None,
    ) -> tuple[str, ...]:
        allowed = {_canonical(value) for value in app_subscription_ids if _canonical(value)}
        if subscription_ids is not None:
            requested = list(subscription_ids)
            if not requested:
                return ()
            allowed &= {_canonical(value) for value in requested if _canonical(value)}
        if not context_filters:
            return tuple(sorted(allowed))

        snapshot = self._context()
        fields = (snapshot or {}).get("fields") or {}
        subscriptions = (snapshot or {}).get("subscriptions") or {}
        seen_fields: set[str] = set()
        for criterion in context_filters:
            field_key = _canonical(criterion.get("fieldKey"))
            values = criterion.get("values")
            if (
                not field_key
                or field_key in seen_fields
                or not isinstance(values, list)
                or not values
                or any(value is not None and (not isinstance(value, str) or not value.strip()) for value in values)
            ):
                return ()
            seen_fields.add(field_key)
            if not (fields.get(field_key) or {}).get("enabled"):
                return ()
            include_missing = None in values
            selected = {_canonical(value) for value in values if value is not None}
            allowed = {
                subscription_id
                for subscription_id in allowed
                if (
                    (subscriptions.get(subscription_id) or {}).get(field_key) is None
                    and include_missing
                )
                or _canonical((subscriptions.get(subscription_id) or {}).get(field_key)) in selected
            }
        return tuple(sorted(allowed))

    @staticmethod
    def filter_locations(
        app_locations: Iterable[str],
        locations: Iterable[str] | None = None,
    ) -> tuple[str, ...]:
        configured = {_canonical(value) for value in app_locations if _canonical(value)}
        if locations is None:
            return tuple(sorted(configured))
        requested = list(locations)
        if not requested:
            return ()
        normalized = {_canonical(value) for value in requested if _canonical(value)}
        return tuple(sorted(configured & normalized)) if configured else tuple(sorted(normalized))

    def enrich_rows(self, rows: list[dict]) -> list[dict]:
        if SUBSCRIPTIONS in self._capabilities or LOCATIONS in self._capabilities:
            self._reference_view.enrich_rows(rows)
        if BUSINESS_CONTEXT in self._capabilities:
            snapshot = self._context()
            fields = self.enabled_fields
            values = (snapshot or {}).get("subscriptions") or {}
            for row in rows:
                subscription_id = _canonical(row.get("subscriptionId"))
                subscription_values = values.get(subscription_id) or {}
                row["businessContext"] = {
                    field_key: subscription_values.get(field_key)
                    for field_key in fields
                }
        return rows

    def catalogue(
        self,
        subscription_ids: Iterable[str],
        locations: Iterable[str] | None = None,
    ) -> dict:
        self._require(SUBSCRIPTIONS)
        self._require(LOCATIONS)
        self._require(BUSINESS_CONTEXT)
        visible_subscriptions = {
            _canonical(value) for value in subscription_ids if _canonical(value)
        }
        context = self._context()
        fields = (context or {}).get("fields") or {}
        mappings = (context or {}).get("subscriptions") or {}
        context_fields = []
        for field_key, field in fields.items():
            if not field.get("enabled"):
                continue
            counts: dict[str, dict] = {}
            missing = 0
            for subscription_id in visible_subscriptions:
                value = (mappings.get(subscription_id) or {}).get(field_key)
                if value is None:
                    missing += 1
                    continue
                canonical = _canonical(value)
                if canonical not in counts:
                    counts[canonical] = {"value": value, "label": value, "subscriptionCount": 0}
                counts[canonical]["subscriptionCount"] += 1
            options = sorted(counts.values(), key=lambda item: (item["label"].casefold(), item["value"]))
            if missing:
                options.append({"value": None, "label": "Not mapped", "subscriptionCount": missing})
            context_fields.append({
                "fieldKey": field_key,
                "name": str(field.get("name") or field_key),
                "values": options,
            })
        context_fields.sort(key=lambda item: (item["name"].casefold(), item["fieldKey"]))
        return {
            "schemaVersion": 1,
            "subscriptions": self._reference_view.options(SUBSCRIPTIONS, visible_subscriptions),
            "locations": self._reference_view.options(LOCATIONS, locations),
            "contextFields": context_fields,
            "metadata": self.metadata(),
        }

    def metadata(self) -> dict:
        result: dict = {"referenceData": {}}
        reference_metadata = self._reference_view.metadata()
        now = datetime.now(timezone.utc)
        for kind, metadata in reference_metadata.items():
            published_at = metadata.get("publishedAt")
            stale = False
            if published_at:
                age = now - datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
                maximum = (
                    timedelta(minutes=settings.subscription_reference_max_stale_minutes())
                    if kind == SUBSCRIPTIONS
                    else timedelta(hours=settings.location_reference_max_stale_hours())
                )
                stale = age > maximum
            result["referenceData"][kind] = {**metadata, "stale": stale}
        if BUSINESS_CONTEXT in self._capabilities:
            snapshot = self._context()
            result["businessContext"] = {
                "generation": snapshot.get("generation") if snapshot else None,
                "publishedAt": snapshot.get("publishedAt") if snapshot else None,
                "fields": self.enabled_fields,
            }
        return result


def open_current_view(
    capabilities: Iterable[str],
    reference_store: ReferenceDataStore | None = None,
    context_store: SubscriptionContextStore | None = None,
) -> RequestDimensionsView:
    from storage.reference_data_store import store as default_reference_store
    from storage.subscription_context_store import store as default_context_store

    return RequestDimensionsView(
        capabilities,
        reference_store or default_reference_store,
        context_store or default_context_store,
    )