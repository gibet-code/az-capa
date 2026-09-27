"""Azure Table + Blob persistence for assembled Activity Log operations (a1).

This is the retained-source assembly store (spec Sections 4, 6, 8). It persists
one row per operation identity in ``ActivityLogOperations`` with the recipe's
projected fields promoted to typed columns. Applied events and sanitized error
details are retained independently as gzip; rare oversize payloads spill to a
private Blob. Per-subscription collection coverage lives in
``ActivityLogCollectionState``.

Storage owns persistence only: it never imports services and encodes no VM,
VMSS, CR, or subscription business rules. Assembly hands it operation dicts.
"""
from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.data.tables import TableClient, UpdateMode
from azure.storage.blob import ContentSettings

from core import settings
from .blob import blob_service_client
from .table import (
    MAX_TRANSACTION_OPERATIONS as _MAX_BATCH,
    decode_json_array_properties,
    encode_json_array_properties,
    submit_table_transaction,
    table_service_client,
)

_COVERAGE_RK = "coverage"

_ALL_EVENT_IDS = dict(
    legacy_property="AllEventIds",
    chunk_prefix="AllEventIds",
    count_property="AllEventIdsChunkCount",
)

# Logical/system property names a projected column may never overwrite.
_RESERVED_COLUMNS = frozenset({
    "PartitionKey", "RowKey", "Timestamp",
    "OperationIdentity", "OperationIdentityHash", "OperationId", "CorrelationId",
    "SubscriptionId", "ResourceId", "ResourceType", "OperationName",
    "FirstEventTime", "LastEventTime", "RequestEventId", "TerminalEventId",
    "AllEventIds", "AllEventIdsChunkCount",
    "AppliedEventId", "AppliedPayloadGz", "PayloadEncoding",
    "AppliedPayloadSpillBlob", "PayloadTruncated",
    "ErrorDetailsGz", "ErrorDetailsSpillBlob",
    "Outcome", "PairingStatus", "ReprojectionPending",
    "AssemblerVersion", "RecipeVersion", "PublishedVersion",
    "FirstSeenAt", "LastSeenAt", "FinalizedAt",
})

_TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})
_CONTENT_SETTINGS = ContentSettings(content_type="application/gzip")


def _coerce_property(value):
    """Coerce a projected value to a Table-storable property."""
    if value is None or isinstance(value, (str, bool, int, float, bytes, datetime)):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _spill_blob_name(subscription_id: str, applied_event_id: str) -> str:
    return f"{(subscription_id or 'unknown').lower()}/{applied_event_id}.json.gz"


def _error_details_spill_blob_name(subscription_id: str, operation_identity_hash: str) -> str:
    return (
        f"{(subscription_id or 'unknown').lower()}/"
        f"{operation_identity_hash}.error-details.json.gz"
    )


def _download_gzip_blob(blob) -> bytes:
    downloader = blob.download_blob()
    payload = downloader.readall()
    if payload.startswith(b"\x1f\x8b"):
        return payload

    content_settings = getattr(downloader.properties, "content_settings", None)
    content_encoding = getattr(content_settings, "content_encoding", None)
    if (content_encoding or "").casefold() == "gzip":
        return gzip.compress(payload)
    return payload


class ActivityLogStore:
    """Persistence for assembled operations and per-subscription coverage."""

    def __init__(
        self,
        operations_table_name=settings.activity_log_operations_table_name,
        collection_state_table_name=settings.activity_log_collection_state_table_name,
        spill_container_name=settings.activity_log_spill_container_name,
        inline_payload_max_bytes: Callable[[], int] = settings.activity_log_inline_payload_max_bytes,
    ) -> None:
        self._operations_table_name = operations_table_name
        self._collection_state_table_name = collection_state_table_name
        self._spill_container_name = spill_container_name
        self._inline_payload_max_bytes = inline_payload_max_bytes
        self._table_cache: dict[str, TableClient] = {}
        self._container_cache = None

    # ── infrastructure ────────────────────────────────────────────────────
    def _table(self, name: str) -> TableClient:
        client = self._table_cache.get(name)
        if client is None:
            service = table_service_client()
            try:
                service.create_table_if_not_exists(name)
            except ResourceExistsError:
                pass
            client = service.get_table_client(name)
            self._table_cache[name] = client
        return client

    def _operations_table(self) -> TableClient:
        return self._table(self._operations_table_name())

    def _collection_state_table(self) -> TableClient:
        return self._table(self._collection_state_table_name())

    def _container(self):
        if self._container_cache is None:
            container = blob_service_client().get_container_client(self._spill_container_name())
            try:
                container.create_container()
            except Exception as exc:  # noqa: BLE001 - SDK exception differs across Azurite/Azure
                if getattr(exc, "status_code", None) != 409:
                    raise
            self._container_cache = container
        return self._container_cache

    def _table_names(self) -> list[str]:
        return [self._operations_table_name(), self._collection_state_table_name()]

    # ── operation upsert ──────────────────────────────────────────────────
    def upsert_operations(self, operations: list[dict]) -> int:
        """Upsert assembled operations, spilling rare oversize payloads to Blob.

        Idempotent and keyed by operation identity: re-running over overlapping
        or replayed events converges on the same rows. ``FirstSeenAt``,
        ``FinalizedAt`` and ``PublishedVersion`` are preserved across upserts.
        """
        if not operations:
            return 0

        seen_at = datetime.now(timezone.utc)
        table = self._operations_table()
        by_partition: dict[str, dict[str, dict]] = {}
        for operation in operations:
            entity = self._build_entity(operation, seen_at)
            by_partition.setdefault(entity["PartitionKey"], {})[entity["RowKey"]] = entity

        written = 0
        for partition_key, entities_by_row_key in by_partition.items():
            preserved = self._existing_meta(table, partition_key)
            entities = []
            for row_key, entity in entities_by_row_key.items():
                self._apply_preserved_meta(entity, preserved.get(row_key), seen_at)
                entities.append(entity)
            for start in range(0, len(entities), _MAX_BATCH):
                chunk = entities[start:start + _MAX_BATCH]
                submit_table_transaction(
                    table,
                    [("upsert", entity, {"mode": UpdateMode.REPLACE}) for entity in chunk],
                )
                written += len(chunk)
        logging.info("Upserted %d Activity Log operation(s).", written)
        return written

    def _build_entity(self, operation: dict, seen_at: datetime) -> dict:
        outcome = (operation.get("Outcome") or "").lower()
        entity: dict = {
            "PartitionKey": operation["PartitionKey"],
            "RowKey": operation["RowKey"],
            "OperationIdentity": operation.get("OperationIdentity"),
            "OperationIdentityHash": operation.get("OperationIdentityHash"),
            "OperationId": operation.get("OperationId"),
            "CorrelationId": operation.get("CorrelationId"),
            "SubscriptionId": operation.get("SubscriptionId"),
            "ResourceId": operation.get("ResourceId"),
            "ResourceType": operation.get("ResourceType"),
            "OperationName": operation.get("OperationName"),
            "FirstEventTime": operation.get("FirstEventTime"),
            "LastEventTime": operation.get("LastEventTime"),
            "RequestEventId": operation.get("RequestEventId"),
            "TerminalEventId": operation.get("TerminalEventId"),
            "AppliedEventId": operation.get("AppliedEventId"),
            "PayloadEncoding": operation.get("PayloadEncoding"),
            "Outcome": operation.get("Outcome"),
            "PairingStatus": operation.get("PairingStatus"),
            "ReprojectionPending": bool(operation.get("ReprojectionPending")),
            "AssemblerVersion": operation.get("AssemblerVersion"),
            "RecipeVersion": operation.get("RecipeVersion"),
            "LastSeenAt": seen_at,
        }

        for column, value in (operation.get("Columns") or {}).items():
            if column in _RESERVED_COLUMNS:
                logging.warning("Skipping projected column %r: reserved property name.", column)
                continue
            coerced = _coerce_property(value)
            if coerced is not None:
                entity[column] = coerced

        entity.update(encode_json_array_properties(operation.get("AllEventIds") or [], **_ALL_EVENT_IDS))
        self._attach_payload(entity, operation)
        self._attach_error_details(entity, operation)

        entity = {key: value for key, value in entity.items() if value is not None}
        if outcome in _TERMINAL_OUTCOMES:
            entity["FinalizedAt"] = seen_at
        return entity

    def _attach_payload(self, entity: dict, operation: dict) -> None:
        payload = operation.get("AppliedPayloadGz")
        if not payload:
            return
        inline_limit = self._inline_payload_max_bytes()
        if inline_limit > 0 and len(payload) <= inline_limit:
            entity["AppliedPayloadGz"] = payload
            entity["PayloadTruncated"] = False
            return
        blob_name = _spill_blob_name(
            operation.get("SubscriptionId") or "",
            operation.get("AppliedEventId") or operation.get("OperationIdentityHash"),
        )
        self._container().upload_blob(
            blob_name, payload, overwrite=True, content_settings=_CONTENT_SETTINGS
        )
        entity["AppliedPayloadSpillBlob"] = blob_name
        entity["PayloadTruncated"] = True

    def _attach_error_details(self, entity: dict, operation: dict) -> None:
        payload = operation.get("ErrorDetailsGz")
        if not payload:
            return
        inline_limit = self._inline_payload_max_bytes()
        if inline_limit > 0 and len(payload) <= inline_limit:
            entity["ErrorDetailsGz"] = payload
            return
        blob_name = _error_details_spill_blob_name(
            operation.get("SubscriptionId") or "",
            operation.get("OperationIdentityHash") or operation.get("RowKey"),
        )
        self._container().upload_blob(
            blob_name, payload, overwrite=True, content_settings=_CONTENT_SETTINGS
        )
        entity["ErrorDetailsSpillBlob"] = blob_name

    @staticmethod
    def _existing_meta(table: TableClient, partition_key: str) -> dict[str, dict]:
        preserved: dict[str, dict] = {}
        rows = table.query_entities(
            f"PartitionKey eq '{partition_key}'",
            select=["RowKey", "FirstSeenAt", "FinalizedAt", "PublishedVersion"],
        )
        for row in rows:
            preserved[row["RowKey"]] = {
                "FirstSeenAt": row.get("FirstSeenAt"),
                "FinalizedAt": row.get("FinalizedAt"),
                "PublishedVersion": row.get("PublishedVersion"),
            }
        return preserved

    @staticmethod
    def _apply_preserved_meta(entity: dict, preserved: dict | None, seen_at: datetime) -> None:
        entity["FirstSeenAt"] = (preserved or {}).get("FirstSeenAt") or seen_at
        prior_finalized = (preserved or {}).get("FinalizedAt")
        if prior_finalized:
            entity["FinalizedAt"] = prior_finalized  # earliest terminal wins
        published = (preserved or {}).get("PublishedVersion")
        if published is not None:
            entity["PublishedVersion"] = published

    def load_applied_payload(self, entity: dict) -> bytes | None:
        """Return the gzip payload for an operation row, rehydrating from spill."""
        if entity.get("PayloadTruncated") and entity.get("AppliedPayloadSpillBlob"):
            blob = self._container().get_blob_client(entity["AppliedPayloadSpillBlob"])
            return _download_gzip_blob(blob)
        return entity.get("AppliedPayloadGz")

    def load_error_details(self, entity: dict) -> bytes | None:
        """Return compressed error details, rehydrating a spilled value."""
        if entity.get("ErrorDetailsSpillBlob"):
            blob = self._container().get_blob_client(entity["ErrorDetailsSpillBlob"])
            return _download_gzip_blob(blob)
        return entity.get("ErrorDetailsGz")

    def query_operations(self, subscription_id: str, extra_filter: str | None = None) -> list[dict]:
        """Return operation rows for a subscription (for reconcile/replay)."""
        subscription_id = (subscription_id or "").lower()
        query = f"SubscriptionId eq '{subscription_id}'"
        if extra_filter:
            query = f"{query} and {extra_filter}"
        rows = self._operations_table().query_entities(query)
        results = []
        for row in rows:
            record = dict(row)
            record["AllEventIds"] = decode_json_array_properties(row, **_ALL_EVENT_IDS)
            results.append(record)
        return results

    def query_operations_window(
        self,
        subscription_id: str,
        start_time: str,
        end_time: str,
        operation_names: Iterable[str] | None,
        *,
        select: Iterable[str],
    ) -> list[dict]:
        """Query projected operation columns in an inclusive event-time window."""
        def literal(value: object) -> str:
            return str(value).replace("'", "''")

        if operation_names is not None:
            names = tuple(dict.fromkeys(
                str(value) for value in operation_names if value
            ))
        else:
            names = ()
        if operation_names is not None and not names:
            return []

        query = (
            f"SubscriptionId eq '{literal((subscription_id or '').lower())}'"
            f" and LastEventTime ge '{literal(start_time)}'"
            f" and LastEventTime le '{literal(end_time)}'"
        )
        if names:
            operation_filter = " or ".join(
                f"OperationName eq '{literal(name)}'" for name in names
            )
            query = f"{query} and ({operation_filter})"
        columns = list(dict.fromkeys(select))
        rows = self._operations_table().query_entities(query, select=columns)
        return [
            {column: row[column] for column in columns if column in row}
            for row in rows
        ]

    def hydrate_operation(self, row: dict) -> dict:
        """Rebuild an assemble-shaped operation dict from a stored row.

        Projected columns are the non-reserved properties; the applied payload is
        rehydrated from spill when needed. Used by reconciliation replay so the
        row can be re-projected and re-upserted through :meth:`upsert_operations`.
        """
        all_event_ids = row.get("AllEventIds")
        if not isinstance(all_event_ids, list):
            all_event_ids = decode_json_array_properties(row, **_ALL_EVENT_IDS)
        columns = {
            key: value for key, value in row.items()
            if key not in _RESERVED_COLUMNS and key != "AllEventIdsChunkCount"
        }
        return {
            "OperationIdentity": row.get("OperationIdentity"),
            "OperationIdentityHash": row.get("OperationIdentityHash"),
            "PartitionKey": row.get("PartitionKey"),
            "RowKey": row.get("RowKey"),
            "SubscriptionId": row.get("SubscriptionId"),
            "ResourceId": row.get("ResourceId"),
            "ResourceType": row.get("ResourceType"),
            "OperationName": row.get("OperationName"),
            "OperationId": row.get("OperationId"),
            "CorrelationId": row.get("CorrelationId"),
            "FirstEventTime": row.get("FirstEventTime"),
            "LastEventTime": row.get("LastEventTime"),
            "RequestEventId": row.get("RequestEventId"),
            "TerminalEventId": row.get("TerminalEventId"),
            "AllEventIds": all_event_ids,
            "Columns": columns,
            "AppliedEventId": row.get("AppliedEventId"),
            "AppliedPayloadGz": self.load_applied_payload(row),
            "PayloadEncoding": row.get("PayloadEncoding"),
            "ErrorDetailsGz": self.load_error_details(row),
            "Outcome": row.get("Outcome"),
            "PairingStatus": row.get("PairingStatus"),
            "ReprojectionPending": bool(row.get("ReprojectionPending")),
            "AssemblerVersion": row.get("AssemblerVersion"),
            "RecipeVersion": row.get("RecipeVersion"),
        }

    def delete_subscriptions_data(self, subscription_ids) -> int:
        """Purge operation rows, spill blobs, and coverage for the subscriptions."""
        table = self._operations_table()
        deleted = 0
        for subscription_id in sorted({str(value).lower() for value in subscription_ids if value}):
            rows = list(table.query_entities(
                f"SubscriptionId eq '{subscription_id}'",
                select=[
                    "PartitionKey", "RowKey",
                    "AppliedPayloadSpillBlob", "ErrorDetailsSpillBlob",
                ],
            ))
            self._delete_spill_blobs(rows)
            by_partition: dict[str, list[dict]] = {}
            for row in rows:
                by_partition.setdefault(row["PartitionKey"], []).append(row)
            for partition_rows in by_partition.values():
                for start in range(0, len(partition_rows), _MAX_BATCH):
                    chunk = partition_rows[start:start + _MAX_BATCH]
                    submit_table_transaction(table, [
                        ("delete", {"PartitionKey": row["PartitionKey"], "RowKey": row["RowKey"]})
                        for row in chunk
                    ])
                    deleted += len(chunk)
            self._delete_coverage(subscription_id)
        return deleted

    def _delete_spill_blobs(self, rows) -> None:
        container = None
        for row in rows:
            for property_name in ("AppliedPayloadSpillBlob", "ErrorDetailsSpillBlob"):
                blob_name = row.get(property_name)
                if not blob_name:
                    continue
                container = container or self._container()
                try:
                    container.delete_blob(blob_name)
                except ResourceNotFoundError:
                    pass

    # ── collection state ──────────────────────────────────────────────────
    def get_coverage(self, subscription_id: str) -> dict | None:
        try:
            entity = self._collection_state_table().get_entity(
                (subscription_id or "").lower(), _COVERAGE_RK
            )
        except ResourceNotFoundError:
            return None
        return self._decode_coverage(entity)

    def list_coverage(self) -> list[dict]:
        rows = self._collection_state_table().query_entities(f"RowKey eq '{_COVERAGE_RK}'")
        return [self._decode_coverage(row) for row in rows]

    def set_coverage(self, subscription_id: str, coverage: dict) -> None:
        entity = {
            "PartitionKey": (subscription_id or "").lower(),
            "RowKey": _COVERAGE_RK,
            "CoverageStart": coverage.get("coverageStart"),
            "CoverageEnd": coverage.get("coverageEnd"),
            "EnabledRecipeIds": json.dumps(coverage.get("enabledRecipeIds") or [], separators=(",", ":")),
            "MaterializedRecipeVersions": json.dumps(
                coverage.get("materializedRecipeVersions") or {}, separators=(",", ":")
            ),
            "LastSuccessfulRunId": coverage.get("lastSuccessfulRunId"),
            "LastUpsertAt": coverage.get("lastUpsertAt"),
            "UpdatedAt": datetime.now(timezone.utc),
        }
        entity = {key: value for key, value in entity.items() if value is not None}
        self._collection_state_table().upsert_entity(entity, mode=UpdateMode.REPLACE)

    @staticmethod
    def _decode_coverage(entity: dict) -> dict:
        return {
            "subscriptionId": entity.get("PartitionKey"),
            "coverageStart": entity.get("CoverageStart"),
            "coverageEnd": entity.get("CoverageEnd"),
            "enabledRecipeIds": json.loads(entity.get("EnabledRecipeIds") or "[]"),
            "materializedRecipeVersions": json.loads(entity.get("MaterializedRecipeVersions") or "{}"),
            "lastSuccessfulRunId": entity.get("LastSuccessfulRunId"),
            "lastUpsertAt": entity.get("LastUpsertAt"),
            "updatedAt": entity.get("UpdatedAt"),
        }

    def _delete_coverage(self, subscription_id: str) -> None:
        try:
            self._collection_state_table().delete_entity((subscription_id or "").lower(), _COVERAGE_RK)
        except ResourceNotFoundError:
            pass

    # ── flush ─────────────────────────────────────────────────────────────
    def delete_tables(self) -> list[str]:
        service = table_service_client()
        deleted = []
        for name in self._table_names():
            try:
                service.delete_table(name)
                deleted.append(name)
            except ResourceNotFoundError:
                pass
            self._table_cache.pop(name, None)
        try:
            blob_service_client().delete_container(self._spill_container_name())
            deleted.append(self._spill_container_name())
        except ResourceNotFoundError:
            pass
        self._container_cache = None
        return deleted

    def ensure_tables(self) -> bool:
        service = table_service_client()
        for name in self._table_names():
            try:
                service.create_table_if_not_exists(name)
            except ResourceExistsError:
                pass
            except HttpResponseError as exc:
                if getattr(exc, "error_code", "") == "TableBeingDeleted" or "being deleted" in str(exc).lower():
                    return False
                raise
        try:
            self._container()
        except HttpResponseError as exc:
            if "being deleted" in str(exc).lower():
                return False
            raise
        return True
