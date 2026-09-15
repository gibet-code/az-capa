"""Compressed Blob snapshots for logical-to-physical availability-zone mappings."""
from __future__ import annotations

import concurrent.futures
import threading
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import ContentSettings

from core import settings
from .blob import blob_service_client, decode_json as _decode, encode_json_gzip as _encode

_SCHEMA_VERSION = 1
_CURRENT_BLOB = "current.json.gz"
_FRAGMENT_PREFIX = "subscriptions/"
_MAX_BLOB_CONCURRENCY = 40
_CONTENT_SETTINGS = ContentSettings(content_type="application/json", content_encoding="gzip")


class ZoneMappingBlobStore:
    def __init__(self, container_name=settings.zone_mapping_blob_container_name) -> None:
        self._container_name = container_name
        self._container_cache = None
        self._cache_lock = threading.Lock()
        self._cached_etag: str | None = None
        self._cached_snapshot: dict | None = None

    def _container(self):
        if self._container_cache is None:
            container = blob_service_client().get_container_client(self._container_name())
            try:
                container.create_container()
            except Exception as exc:  # noqa: BLE001 - SDK exception differs across Azurite/Azure
                if getattr(exc, "status_code", None) != 409:
                    raise
            self._container_cache = container
        return self._container_cache

    @staticmethod
    def _fragment_name(subscription_id: str) -> str:
        return f"{_FRAGMENT_PREFIX}{subscription_id.lower()}.json.gz"

    @staticmethod
    def _fragment_payload(subscription_id: str, regions: list[dict]) -> dict:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "subscriptionId": subscription_id.lower(),
            "regions": {
                str(region["region"]).lower(): (region.get("zoneMappings") or {})
                for region in regions
                if region.get("region")
            },
        }

    def replace_subscription_mappings(self, subscription_id: str, regions: list[dict]) -> int:
        payload = self._fragment_payload(subscription_id, regions)
        self._container().upload_blob(
            self._fragment_name(subscription_id),
            _encode(payload),
            overwrite=True,
            content_settings=_CONTENT_SETTINGS,
        )
        return len(payload["regions"])

    def delete_subscriptions_data(self, subscription_ids) -> int:
        deleted = 0
        container = self._container()
        for subscription_id in sorted({str(value).lower() for value in subscription_ids if value}):
            try:
                container.delete_blob(self._fragment_name(subscription_id))
                deleted += 1
            except ResourceNotFoundError:
                pass
        return deleted

    def _read_fragment(self, blob_name: str) -> tuple[str, dict[str, dict[str, str]]]:
        payload = _decode(self._container().download_blob(blob_name).readall())
        return str(payload["subscriptionId"]).lower(), payload.get("regions") or {}

    def publish_snapshot(self, state: dict) -> dict:
        container = self._container()
        names = [blob.name for blob in container.list_blobs(name_starts_with=_FRAGMENT_PREFIX)]
        subscriptions: dict[str, dict[str, dict[str, str]]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_BLOB_CONCURRENCY, len(names)) or 1
        ) as executor:
            for subscription_id, regions in executor.map(self._read_fragment, names):
                subscriptions[subscription_id] = regions
        return self.import_snapshot(subscriptions, state, write_fragments=False)

    def import_snapshot(
        self,
        subscriptions: dict[str, dict[str, dict[str, str]]],
        state: dict,
        *,
        write_fragments: bool = True,
    ) -> dict:
        normalized = {
            str(subscription_id).lower(): {
                str(region).lower(): mappings or {}
                for region, mappings in regions.items()
            }
            for subscription_id, regions in subscriptions.items()
        }
        container = self._container()
        if write_fragments:
            def upload_fragment(item) -> None:
                subscription_id, regions = item
                payload = {
                    "schemaVersion": _SCHEMA_VERSION,
                    "subscriptionId": subscription_id,
                    "regions": regions,
                }
                container.upload_blob(
                    self._fragment_name(subscription_id),
                    _encode(payload),
                    overwrite=True,
                    content_settings=_CONTENT_SETTINGS,
                )

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_MAX_BLOB_CONCURRENCY, len(normalized)) or 1
            ) as executor:
                list(executor.map(upload_fragment, normalized.items()))

        generated_at = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "schemaVersion": _SCHEMA_VERSION,
            "generatedAt": generated_at,
            "state": state,
            "subscriptions": normalized,
        }
        raw = _encode(snapshot)
        response = container.get_blob_client(_CURRENT_BLOB).upload_blob(
            raw,
            overwrite=True,
            content_settings=_CONTENT_SETTINGS,
        )
        with self._cache_lock:
            self._cached_etag = str(response.get("etag") or "") or None
            self._cached_snapshot = snapshot
        return {
            "subscriptions": len(normalized),
            "regions": sum(len(regions) for regions in normalized.values()),
            "compressedBytes": len(raw),
        }

    def _snapshot(self) -> dict | None:
        blob = self._container().get_blob_client(_CURRENT_BLOB)
        try:
            properties = blob.get_blob_properties()
        except ResourceNotFoundError:
            return None
        etag = str(properties.etag)
        with self._cache_lock:
            if self._cached_snapshot is not None and self._cached_etag == etag:
                return self._cached_snapshot
        snapshot = _decode(blob.download_blob().readall())
        if snapshot.get("schemaVersion") != _SCHEMA_VERSION:
            raise ValueError(f"Unsupported zone mapping snapshot schema: {snapshot.get('schemaVersion')}")
        with self._cache_lock:
            self._cached_etag = etag
            self._cached_snapshot = snapshot
        return snapshot

    def get_mappings(self, pairs) -> dict[tuple[str, str], dict[str, str]]:
        normalized = {
            (str(subscription_id).strip().lower(), str(location).strip().lower())
            for subscription_id, location in pairs
            if str(subscription_id).strip() and str(location).strip()
        }
        if not normalized:
            return {}
        snapshot = self._snapshot()
        if snapshot is None:
            return {}
        subscriptions = snapshot.get("subscriptions") or {}
        result = {}
        for pair in normalized:
            regions = subscriptions.get(pair[0])
            if regions is not None and pair[1] in regions:
                result[pair] = regions[pair[1]]
        return result

    def get_state(self) -> dict | None:
        snapshot = self._snapshot()
        return (snapshot.get("state") or None) if snapshot else None

    def delete_snapshot_data(self) -> list[str]:
        container = self._container()
        names = [blob.name for blob in container.list_blobs()]
        if names:
            container.delete_blobs(*names)
        with self._cache_lock:
            self._cached_etag = None
            self._cached_snapshot = None
        return names

    def ensure_container(self) -> bool:
        self._container()
        return True
