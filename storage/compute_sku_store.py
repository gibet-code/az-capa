"""Immutable compressed Blob snapshots for the global Compute SKU catalog."""
from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import ContentSettings

from core import settings
from .blob import blob_service_client, decode_json as _decode, encode_json_gzip as _encode

_SCHEMA_VERSION = 1
_MANIFEST_BLOB = "current.json"
_SNAPSHOT_PREFIX = "snapshots/"
_JSON_SETTINGS = ContentSettings(content_type="application/json")
_GZIP_SETTINGS = ContentSettings(content_type="application/json", content_encoding="gzip")


class ComputeSkuBlobStore:
    def __init__(self, container_name=settings.compute_sku_blob_container_name) -> None:
        self._container_name = container_name
        self._container_cache = None
        self._cache_lock = threading.Lock()
        self._cached_manifest_etag: str | None = None
        self._cached_snapshot: dict | None = None
        self._by_pair: dict[tuple[str, str], dict] = {}
        self._by_sku: dict[str, list[dict]] = {}
        self._by_location: dict[str, list[dict]] = {}

    def _container(self):
        if self._container_cache is None:
            container = blob_service_client().get_container_client(self._container_name())
            try:
                container.create_container()
            except Exception as exc:  # noqa: BLE001 - Azurite/Azure use different subclasses
                if getattr(exc, "status_code", None) != 409:
                    raise
            self._container_cache = container
        return self._container_cache

    @staticmethod
    def _snapshot_name(generation: str) -> str:
        return f"{_SNAPSHOT_PREFIX}{generation}.json.gz"

    @staticmethod
    def _normalize_catalog(catalog: list[dict]) -> list[dict]:
        return sorted(
            [dict(row) for row in catalog],
            key=lambda row: (
                str(row.get("name") or "").lower(),
                str(row.get("location") or "").lower(),
            ),
        )

    def _set_cache(self, snapshot: dict, manifest_etag: str | None) -> None:
        by_pair = {}
        by_sku: dict[str, list[dict]] = defaultdict(list)
        by_location: dict[str, list[dict]] = defaultdict(list)
        for row in snapshot.get("catalog") or []:
            sku_name = str(row.get("name") or "").strip().lower()
            location = str(row.get("location") or "").strip().lower()
            if not sku_name or not location:
                continue
            by_pair[(sku_name, location)] = row
            by_sku[sku_name].append(row)
            by_location[location].append(row)
        with self._cache_lock:
            self._cached_manifest_etag = manifest_etag
            self._cached_snapshot = snapshot
            self._by_pair = by_pair
            self._by_sku = dict(by_sku)
            self._by_location = dict(by_location)

    def publish_catalog(self, catalog: list[dict], source_subscription_id: str, scope: dict) -> str:
        generation = uuid.uuid4().hex
        generated_at = datetime.now(timezone.utc).isoformat()
        normalized = self._normalize_catalog(catalog)
        state = {
            "activeGeneration": generation,
            "sourceSubscriptionId": source_subscription_id.lower(),
            "skuCount": len({str(row.get("name") or "").lower() for row in normalized}),
            "skuLocationCount": len(normalized),
            "scope": scope,
            "updatedAt": generated_at,
        }
        snapshot = {
            "schemaVersion": _SCHEMA_VERSION,
            "generation": generation,
            "generatedAt": generated_at,
            "state": state,
            "catalog": normalized,
        }
        snapshot_name = self._snapshot_name(generation)
        container = self._container()
        container.get_blob_client(snapshot_name).upload_blob(
            _encode(snapshot),
            overwrite=False,
            content_settings=_GZIP_SETTINGS,
        )
        manifest = {
            "schemaVersion": _SCHEMA_VERSION,
            "generation": generation,
            "snapshotBlob": snapshot_name,
            "state": state,
        }
        response = container.get_blob_client(_MANIFEST_BLOB).upload_blob(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            overwrite=True,
            content_settings=_JSON_SETTINGS,
        )
        self._set_cache(snapshot, str(response.get("etag") or "") or None)
        return generation

    def import_catalog(self, catalog: list[dict], state: dict) -> str:
        return self.publish_catalog(
            catalog,
            str(state.get("sourceSubscriptionId") or ""),
            state.get("scope") or {},
        )

    def _snapshot(self) -> dict | None:
        manifest_blob = self._container().get_blob_client(_MANIFEST_BLOB)
        try:
            properties = manifest_blob.get_blob_properties()
        except ResourceNotFoundError:
            return None
        etag = str(properties.etag)
        with self._cache_lock:
            if self._cached_snapshot is not None and self._cached_manifest_etag == etag:
                return self._cached_snapshot
        manifest = json.loads(manifest_blob.download_blob().readall())
        if manifest.get("schemaVersion") != _SCHEMA_VERSION:
            raise ValueError(f"Unsupported Compute SKU manifest schema: {manifest.get('schemaVersion')}")
        snapshot = _decode(
            self._container().get_blob_client(manifest["snapshotBlob"]).download_blob().readall()
        )
        if snapshot.get("schemaVersion") != _SCHEMA_VERSION:
            raise ValueError(f"Unsupported Compute SKU snapshot schema: {snapshot.get('schemaVersion')}")
        if snapshot.get("generation") != manifest.get("generation"):
            raise ValueError("Compute SKU manifest and snapshot generations do not match.")
        self._set_cache(snapshot, etag)
        return snapshot

    def find(self, sku_name: str | None = None, location: str | None = None) -> list[dict]:
        if not sku_name and not location:
            raise ValueError("Provide sku_name, location, or both.")
        if self._snapshot() is None:
            return []
        normalized_sku = str(sku_name or "").lower()
        normalized_location = str(location or "").lower()
        with self._cache_lock:
            if normalized_sku and normalized_location:
                row = self._by_pair.get((normalized_sku, normalized_location))
                return [row] if row else []
            if normalized_sku:
                return list(self._by_sku.get(normalized_sku, ()))
            return list(self._by_location.get(normalized_location, ()))

    def find_pairs(
        self, pairs: set[tuple[str, str]]
    ) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], str]]:
        normalized = {
            (str(sku).strip().lower(), str(location).strip().lower())
            for sku, location in pairs
            if str(sku).strip() and str(location).strip()
        }
        if not normalized:
            return {}, {}
        try:
            snapshot = self._snapshot()
        except Exception as exc:  # noqa: BLE001 - preserve per-row unknown behavior
            message = f"Compute SKU catalog lookup failed: {exc}"
            return {}, {pair: message for pair in normalized}
        if snapshot is None:
            message = "The Compute SKU catalog has not been loaded."
            return {}, {pair: message for pair in normalized}
        with self._cache_lock:
            return {
                pair: self._by_pair[pair]
                for pair in normalized
                if pair in self._by_pair
            }, {}

    def get_state(self) -> dict | None:
        snapshot = self._snapshot()
        return snapshot.get("state") if snapshot else None

    def delete_storage(self) -> list[str]:
        container = self._container()
        names = [blob.name for blob in container.list_blobs()]
        if names:
            container.delete_blobs(*names)
        with self._cache_lock:
            self._cached_manifest_etag = None
            self._cached_snapshot = None
            self._by_pair = {}
            self._by_sku = {}
            self._by_location = {}
        return names

    def ensure_storage(self) -> bool:
        self._container()
        return True
