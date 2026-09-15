"""Immutable Blob snapshots for typed Azure Reference Data catalogues."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import ContentSettings

from core import settings
from .blob import blob_service_client, decode_json, encode_json_gzip

SUBSCRIPTIONS = "subscriptions"
LOCATIONS = "locations"
_KINDS = {SUBSCRIPTIONS, LOCATIONS}
_SCHEMA_VERSION = 1
_JSON_SETTINGS = ContentSettings(content_type="application/json")
_GZIP_SETTINGS = ContentSettings(content_type="application/json", content_encoding="gzip")


class ReferenceDataStore:
    def __init__(
        self,
        container_name: Callable[[], str] = settings.azure_reference_data_blob_container_name,
        l1_ttl_seconds: Callable[[], int] = settings.azure_reference_data_l1_ttl_seconds,
    ) -> None:
        self._container_name = container_name
        self._l1_ttl_seconds = l1_ttl_seconds
        self._container_cache = None
        self._cache_lock = threading.Lock()
        self._cache: dict[str, tuple[dict | None, str | None, float]] = {}

    @staticmethod
    def _validate_kind(kind: str) -> str:
        if kind not in _KINDS:
            raise ValueError(f"Unsupported reference-data kind: {kind!r}.")
        return kind

    @staticmethod
    def _manifest_name(kind: str) -> str:
        return f"{kind}/current.json"

    @staticmethod
    def _snapshot_name(kind: str, generation: str) -> str:
        return f"{kind}/snapshots/{generation}.json.gz"

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

    def _set_cache(self, kind: str, snapshot: dict | None, etag: str | None) -> None:
        expires_at = time.monotonic() + self._l1_ttl_seconds()
        with self._cache_lock:
            self._cache[kind] = (snapshot, etag, expires_at)

    def publish(
        self,
        kind: str,
        values: dict[str, dict],
        source: dict,
        *,
        published_at: datetime | None = None,
        generation: str | None = None,
    ) -> str:
        kind = self._validate_kind(kind)
        generation = generation or uuid.uuid4().hex
        timestamp = (published_at or datetime.now(timezone.utc)).isoformat()
        normalized_values = {key: values[key] for key in sorted(values)}
        snapshot = {
            "schemaVersion": _SCHEMA_VERSION,
            "kind": kind,
            "generation": generation,
            "publishedAt": timestamp,
            "source": source,
            "values": normalized_values,
        }
        snapshot_name = self._snapshot_name(kind, generation)
        container = self._container()
        container.get_blob_client(snapshot_name).upload_blob(
            encode_json_gzip(snapshot),
            overwrite=False,
            content_settings=_GZIP_SETTINGS,
        )
        manifest = {
            "schemaVersion": _SCHEMA_VERSION,
            "kind": kind,
            "generation": generation,
            "publishedAt": timestamp,
            "snapshotBlob": snapshot_name,
        }
        response = container.get_blob_client(self._manifest_name(kind)).upload_blob(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            overwrite=True,
            content_settings=_JSON_SETTINGS,
        )
        etag = response.get("etag") if hasattr(response, "get") else getattr(response, "etag", None)
        self._set_cache(kind, snapshot, str(etag) if etag else None)
        return generation

    def open_snapshot(self, kind: str) -> dict | None:
        kind = self._validate_kind(kind)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(kind)
            if cached and cached[2] > now:
                return cached[0]

        manifest_blob = self._container().get_blob_client(self._manifest_name(kind))
        try:
            properties = manifest_blob.get_blob_properties()
        except ResourceNotFoundError:
            self._set_cache(kind, None, None)
            return None
        etag = str(properties.etag)
        with self._cache_lock:
            cached = self._cache.get(kind)
            if cached and cached[0] is not None and cached[1] == etag:
                snapshot = cached[0]
                self._cache[kind] = (snapshot, etag, now + self._l1_ttl_seconds())
                return snapshot

        manifest = json.loads(manifest_blob.download_blob().readall())
        if manifest.get("schemaVersion") != _SCHEMA_VERSION or manifest.get("kind") != kind:
            raise ValueError(f"Invalid {kind} reference-data manifest.")
        snapshot = decode_json(
            self._container().get_blob_client(manifest["snapshotBlob"]).download_blob().readall()
        )
        if (
            snapshot.get("schemaVersion") != _SCHEMA_VERSION
            or snapshot.get("kind") != kind
            or snapshot.get("generation") != manifest.get("generation")
        ):
            raise ValueError(f"Invalid {kind} reference-data snapshot.")
        self._set_cache(kind, snapshot, etag)
        return snapshot


store = ReferenceDataStore()