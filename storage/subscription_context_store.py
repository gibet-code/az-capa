"""Table definitions and Blob snapshots for Business Context."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.storage.blob import BlobLeaseClient, ContentSettings

from core import settings
from core.identity import UserAuditMetadata
from .blob import blob_service_client, decode_json, encode_json_gzip
from .table import LazyTableClient, table_service_client

_PARTITION_KEY = "field"
_SCHEMA_VERSION = 1
_MANIFEST_BLOB = "current.json"
_GZIP_SETTINGS = ContentSettings(content_type="application/json", content_encoding="gzip")
_JSON_SETTINGS = ContentSettings(content_type="application/json")


class ContextFieldConflictError(ValueError):
    """The submitted definition revision is stale or its key already exists."""


class RefreshLease:
    """A renewable fixed-duration Blob lease held by one refresh worker."""

    def __init__(self, lease: BlobLeaseClient, duration_seconds: int) -> None:
        self._lease = lease
        self._duration_seconds = duration_seconds
        self._stop = threading.Event()
        self._operation_lock = threading.Lock()
        self._renew_error: Exception | None = None
        self._thread = threading.Thread(target=self._renew_loop, daemon=True)
        self._thread.start()

    def _renew_loop(self) -> None:
        interval = max(self._duration_seconds / 2, 5)
        while not self._stop.wait(interval):
            try:
                with self._operation_lock:
                    self._lease.renew()
            except Exception as exc:  # noqa: BLE001 - surfaced by renew() before publication
                self._renew_error = exc
                return

    def renew(self) -> None:
        if self._renew_error is not None:
            raise RuntimeError("Business Context refresh lease renewal failed.") from self._renew_error
        with self._operation_lock:
            self._lease.renew()

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        try:
            with self._operation_lock:
                self._lease.release()
        except Exception:  # noqa: BLE001 - release failure must not mask refresh outcome
            logging.warning("Business Context refresh lease release failed", exc_info=True)


class SubscriptionContextStore:
    def __init__(
        self,
        table_name: Callable[[], str] = settings.subscription_context_table_name,
        container_name: Callable[[], str] = settings.subscription_context_blob_container_name,
    ) -> None:
        self._table_name = table_name
        self._container_name = container_name
        self._table = LazyTableClient(table_name, table_service_client)
        self._container_cache = None
        self._cache_lock = threading.Lock()
        self._cached_manifest_etag: str | None = None
        self._cached_snapshot: dict | None = None

    def _container(self):
        if self._container_cache is None:
            container = blob_service_client().get_container_client(self._container_name())
            try:
                container.create_container()
            except Exception as exc:  # noqa: BLE001 - Azurite/Azure exception types differ
                if getattr(exc, "status_code", None) != 409:
                    raise
            self._container_cache = container
        return self._container_cache

    def list_definitions(self) -> list[dict[str, Any]]:
        entities = self._table().query_entities(f"PartitionKey eq '{_PARTITION_KEY}'")
        return sorted(
            (self._definition_from_entity(entity) for entity in entities),
            key=lambda item: (item["name"].lower(), item["key"]),
        )

    def get_definition(self, field_key: str) -> dict[str, Any] | None:
        try:
            entity = self._table().get_entity(_PARTITION_KEY, field_key.lower())
        except ResourceNotFoundError:
            return None
        return self._definition_from_entity(entity)

    def get_source_mapping(self, definition: dict[str, Any]) -> dict[str, str]:
        source_blob = definition.get("sourceBlob")
        if not source_blob:
            return {}
        return decode_json(self._container().download_blob(source_blob).readall()).get("mapping") or {}

    def save_manual_definition(
        self,
        definition: dict[str, Any],
        mapping: dict[str, str],
        updated_by: UserAuditMetadata,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        return self.save_resolved_definition(definition, mapping, updated_by, updated_at=updated_at)

    def save_resolved_definition(
        self,
        definition: dict[str, Any],
        mapping: dict[str, str],
        updated_by: UserAuditMetadata,
        updated_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        existing = self.get_definition(definition["key"])
        submitted_revision = definition.get("revision")
        if existing is None and submitted_revision is not None:
            raise ContextFieldConflictError("A new context field must not provide a revision.")
        if existing is not None and submitted_revision != existing["revision"]:
            raise ContextFieldConflictError(
                f"Context field revision is stale; current revision is {existing['revision']}."
            )

        revision = 1 if existing is None else existing["revision"] + 1
        source_blob = f"sources/{definition['key']}/{revision}-{uuid.uuid4().hex}.json.gz"
        self._container().upload_blob(
            source_blob,
            encode_json_gzip({"schemaVersion": _SCHEMA_VERSION, "mapping": mapping}),
            overwrite=False,
            content_settings=_GZIP_SETTINGS,
        )
        resolved_at = updated_at or datetime.now(timezone.utc)
        current = {
            **definition,
            "revision": revision,
            "sourceBlob": source_blob,
            "resolvedAt": resolved_at,
            "expiresAt": expires_at,
            "updatedAt": resolved_at,
            "updatedBy": {
                "tenantId": updated_by.tenant_id,
                "objectId": updated_by.object_id,
                "displayName": updated_by.display_name,
            },
        }
        definitions = [item for item in self.list_definitions() if item["key"] != current["key"]]
        definitions.append(current)
        generation_blob, snapshot = self._write_generation(definitions)
        self._write_definition(current, existing)
        self._publish_generation(generation_blob, snapshot)
        return current

    def refresh_mapping(
        self,
        field_key: str,
        mapping: dict[str, str],
        resolved_at: datetime,
        expires_at: datetime,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        existing = self.get_definition(field_key)
        if existing is None:
            raise ContextFieldConflictError("Context field was deleted before refresh completed.")
        if expected_revision is not None and existing["revision"] != expected_revision:
            raise ContextFieldConflictError(
                f"Context field changed during refresh; current revision is {existing['revision']}."
            )
        source_blob = f"sources/{field_key}/{existing['revision']}-{uuid.uuid4().hex}.json.gz"
        self._container().upload_blob(
            source_blob,
            encode_json_gzip({"schemaVersion": _SCHEMA_VERSION, "mapping": mapping}),
            overwrite=False,
            content_settings=_GZIP_SETTINGS,
        )
        current = {
            **existing,
            "sourceBlob": source_blob,
            "resolvedAt": resolved_at,
            "expiresAt": expires_at,
        }
        definitions = [item for item in self.list_definitions() if item["key"] != field_key]
        definitions.append(current)
        generation_blob, snapshot = self._write_generation(definitions)
        self._write_definition(current, existing)
        self._publish_generation(generation_blob, snapshot)
        return current

    def delete_definition(self, field_key: str) -> bool:
        existing = self.get_definition(field_key)
        if existing is None:
            return False
        definitions = [item for item in self.list_definitions() if item["key"] != field_key.lower()]
        generation_blob, snapshot = self._write_generation(definitions)
        etag = existing.get("etag")
        kwargs = {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag else {}
        self._table().delete_entity(_PARTITION_KEY, field_key.lower(), **kwargs)
        self._publish_generation(generation_blob, snapshot)
        container = self._container()
        for blob in container.list_blobs(name_starts_with=f"sources/{field_key.lower()}/"):
            container.delete_blob(blob.name)
        for blob in container.list_blobs(name_starts_with=f"locks/{field_key.lower()}/"):
            container.delete_blob(blob.name)
        return True

    def try_acquire_refresh_lease(
        self,
        field_key: str,
        revision: int,
        duration_seconds: int,
    ) -> RefreshLease | None:
        if not 15 <= duration_seconds <= 60:
            raise ValueError("Blob lease duration must be between 15 and 60 seconds.")
        blob = self._container().get_blob_client(f"locks/{field_key.lower()}/{revision}")
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        lease = BlobLeaseClient(blob)
        try:
            lease.acquire(lease_duration=duration_seconds)
        except ResourceExistsError as exc:
            if getattr(exc, "error_code", None) == "LeaseAlreadyPresent":
                return None
            raise
        return RefreshLease(lease, duration_seconds)

    def try_acquire_operation_lease(self, duration_seconds: int) -> RefreshLease | None:
        if not 15 <= duration_seconds <= 60:
            raise ValueError("Blob lease duration must be between 15 and 60 seconds.")
        blob = self._container().get_blob_client("locks/operation")
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        lease = BlobLeaseClient(blob)
        try:
            lease.acquire(lease_duration=duration_seconds)
        except ResourceExistsError as exc:
            if getattr(exc, "error_code", None) == "LeaseAlreadyPresent":
                return None
            raise
        return RefreshLease(lease, duration_seconds)

    def publish_refresh_batch(
        self,
        refresh_run_id: str,
        updates: dict[str, dict[str, str]],
        expected_revisions: dict[str, int],
        resolved_at: datetime,
    ) -> dict[str, Any]:
        snapshot = self._snapshot()
        if snapshot and snapshot.get("generation") == refresh_run_id:
            return {"generation": refresh_run_id, "alreadyPublished": True}

        definitions = self.list_definitions()
        by_key = {definition["key"]: definition for definition in definitions}
        for field_key, revision in expected_revisions.items():
            current = by_key.get(field_key)
            if current is None or current["revision"] != revision:
                raise ContextFieldConflictError(
                    f"Context field {field_key!r} changed during refresh."
                )

        updated_definitions = []
        for definition in definitions:
            mapping = updates.get(definition["key"])
            if mapping is None:
                updated_definitions.append(definition)
                continue
            source_blob = (
                f"sources/{definition['key']}/{definition['revision']}-{refresh_run_id}.json.gz"
            )
            self._container().upload_blob(
                source_blob,
                encode_json_gzip({"schemaVersion": _SCHEMA_VERSION, "mapping": mapping}),
                overwrite=True,
                content_settings=_GZIP_SETTINGS,
            )
            updated_definitions.append({
                **definition,
                "sourceBlob": source_blob,
                "resolvedAt": resolved_at,
                "expiresAt": None,
            })

        generation_blob, next_snapshot = self._write_generation(
            updated_definitions,
            generation=refresh_run_id,
        )
        for definition in updated_definitions:
            if definition["key"] in updates:
                self._write_definition(definition, by_key[definition["key"]])
        self._publish_generation(generation_blob, next_snapshot)
        return {"generation": refresh_run_id, "alreadyPublished": False}

    def snapshot_field_is_fresh(
        self,
        field_key: str,
        revision: int,
        resolved_after: datetime,
    ) -> bool:
        snapshot = self._snapshot()
        metadata = (snapshot.get("fields") or {}).get(field_key) if snapshot else None
        if not metadata or metadata.get("revision") != revision or not metadata.get("resolvedAt"):
            return False
        resolved_at = datetime.fromisoformat(str(metadata["resolvedAt"]).replace("Z", "+00:00"))
        return resolved_at > resolved_after

    def resolve(self, subscription_ids, include_disabled: bool = False) -> dict[str, dict[str, str | None]]:
        requested = sorted({str(value).strip().lower() for value in subscription_ids if str(value).strip()})
        snapshot = self._snapshot()
        if snapshot is None:
            return {subscription_id: {} for subscription_id in requested}
        fields = {
            key: metadata
            for key, metadata in (snapshot.get("fields") or {}).items()
            if include_disabled or metadata.get("enabled")
        }
        values = snapshot.get("subscriptions") or {}
        return {
            subscription_id: {
                key: (values.get(subscription_id) or {}).get(key)
                for key in fields
            }
            for subscription_id in requested
        }

    def _write_definition(self, definition: dict[str, Any], existing: dict[str, Any] | None) -> None:
        entity = self._entity_from_definition(definition)
        if existing is None:
            self._table().create_entity(entity)
            return
        kwargs = {"mode": UpdateMode.REPLACE}
        if existing.get("etag"):
            kwargs.update({"etag": existing["etag"], "match_condition": MatchConditions.IfNotModified})
        self._table().update_entity(entity, **kwargs)

    def _write_generation(
        self,
        definitions: list[dict[str, Any]],
        generation: str | None = None,
    ) -> tuple[str, dict]:
        deterministic_generation = generation is not None
        subscriptions: dict[str, dict[str, str]] = {}
        fields = {}
        for definition in definitions:
            fields[definition["key"]] = {
                "name": definition["name"],
                "enabled": definition["enabled"],
                "revision": definition["revision"],
                "valueSourceType": definition["valueSource"]["type"],
                "resolvedAt": self._iso(definition.get("resolvedAt") or definition["updatedAt"]),
                "expiresAt": self._iso(definition.get("expiresAt")) if definition.get("expiresAt") else None,
            }
            for subscription_id, value in self.get_source_mapping(definition).items():
                subscriptions.setdefault(subscription_id, {})[definition["key"]] = value
        generation = generation or uuid.uuid4().hex
        snapshot = {
            "schemaVersion": _SCHEMA_VERSION,
            "generation": generation,
            "publishedAt": datetime.now(timezone.utc).isoformat(),
            "fields": fields,
            "subscriptions": subscriptions,
        }
        blob_name = f"snapshots/{generation}.json.gz"
        self._container().upload_blob(
            blob_name,
            encode_json_gzip(snapshot),
            overwrite=deterministic_generation,
            content_settings=_GZIP_SETTINGS,
        )
        return blob_name, snapshot

    def open_snapshot(self) -> dict | None:
        return self._snapshot()

    def flush_generated_data(self) -> list[str]:
        container = self._container()
        names = [blob.name for blob in container.list_blobs(name_starts_with="snapshots/")]
        try:
            container.get_blob_client(_MANIFEST_BLOB).get_blob_properties()
            names.append(_MANIFEST_BLOB)
        except ResourceNotFoundError:
            pass
        for name in names:
            container.delete_blob(name)
        with self._cache_lock:
            self._cached_manifest_etag = None
            self._cached_snapshot = None
        return names

    def _publish_generation(self, generation_blob: str, snapshot: dict) -> None:
        manifest = json.dumps(
            {"schemaVersion": _SCHEMA_VERSION, "snapshotBlob": generation_blob},
            separators=(",", ":"),
        ).encode()
        response = self._container().get_blob_client(_MANIFEST_BLOB).upload_blob(
            manifest,
            overwrite=True,
            content_settings=_JSON_SETTINGS,
        )
        with self._cache_lock:
            self._cached_manifest_etag = str(response.get("etag") or "") or None
            self._cached_snapshot = snapshot

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
        snapshot = decode_json(self._container().download_blob(manifest["snapshotBlob"]).readall())
        if snapshot.get("schemaVersion") != _SCHEMA_VERSION:
            raise ValueError(f"Unsupported Business Context snapshot schema: {snapshot.get('schemaVersion')}")
        with self._cache_lock:
            self._cached_manifest_etag = etag
            self._cached_snapshot = snapshot
        return snapshot

    @staticmethod
    def _entity_from_definition(definition: dict[str, Any]) -> dict[str, Any]:
        updated_by = definition["updatedBy"]
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": definition["key"],
            "Name": definition["name"],
            "Enabled": definition["enabled"],
            "ValueSourceJson": json.dumps(definition["valueSource"], separators=(",", ":")),
            "Revision": definition["revision"],
            "SourceBlob": definition["sourceBlob"],
            "ResolvedAt": definition.get("resolvedAt") or definition["updatedAt"],
            "UpdatedAt": definition["updatedAt"],
            "UpdatedByTenantId": updated_by["tenantId"],
            "UpdatedByObjectId": updated_by["objectId"],
            "UpdatedByDisplayName": updated_by["displayName"],
        }
        if definition.get("expiresAt") is not None:
            entity["ExpiresAt"] = definition["expiresAt"]
        return entity

    @staticmethod
    def _definition_from_entity(entity) -> dict[str, Any]:
        metadata = getattr(entity, "metadata", None) or {}
        return {
            "key": entity["RowKey"],
            "name": entity["Name"],
            "enabled": bool(entity["Enabled"]),
            "valueSource": json.loads(entity["ValueSourceJson"]),
            "revision": int(entity["Revision"]),
            "sourceBlob": entity["SourceBlob"],
            "resolvedAt": entity.get("ResolvedAt") or entity["UpdatedAt"],
            "expiresAt": entity.get("ExpiresAt"),
            "updatedAt": entity["UpdatedAt"],
            "updatedBy": {
                "tenantId": entity.get("UpdatedByTenantId", ""),
                "objectId": entity.get("UpdatedByObjectId", ""),
                "displayName": entity.get("UpdatedByDisplayName", ""),
            },
            "etag": metadata.get("etag"),
        }

    @staticmethod
    def _iso(value: Any) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)


store = SubscriptionContextStore()