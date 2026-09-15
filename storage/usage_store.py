"""Table Storage persistence for a daily-usage dataset + its state record.

Design notes
------------
* **Single dataset per store.** One state record describes what is currently
  loaded (scope fingerprint, resolved subscriptions or the ALL marker, and the
  covered date window). Days with no usage are *not* written; a missing row
  inside the covered window means genuine zero usage and can be zero-filled on
  read.
* **Data table** ``PartitionKey = sha1(resourceId)`` (resource-centric reads),
  ``RowKey = yyyymmdd`` (date-ordered within a resource). Upsert overwrites a day.
* **State table** holds a single record (``PartitionKey = 'state'``,
  ``RowKey = 'current'``).

:class:`UsageStore` is constructed with the two table names, so several pipelines
(VM usage, capacity-reservation usage, ...) reuse the exact same persistence with
their own tables. Table names are supplied as callables and resolved lazily, so
App Settings need not be present at import time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Callable

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from core import settings
from .table import (
    MAX_TRANSACTION_OPERATIONS as _MAX_BATCH,
    decode_json_array_properties,
    encode_json_array_properties,
    table_service_client,
)

_SUMMARY_DAYS = 30
_SUMMARY_PROJECTION_VERSION = 1

_STATE_PK = "state"
_STATE_RK = "current"


def resource_partition_key(resource_id: str) -> str:
    """Stable partition key for a resource ID (its lowercase SHA-1 hex digest)."""
    return hashlib.sha1(resource_id.strip().lower().encode("utf-8")).hexdigest()


class UsageStore:
    """Persistence for one daily-usage dataset (data table + state table)."""

    def __init__(
        self,
        data_table_name: Callable[[], str],
        checkpoint_table_name: Callable[[], str],
        summary_table_name: Callable[[], str] | None = None,
    ) -> None:
        # Callables (not values) so table names resolve lazily from App Settings.
        self._data_table_name = data_table_name
        self._checkpoint_table_name = checkpoint_table_name
        self._summary_table_name = summary_table_name
        self._table_cache: dict[str, TableClient] = {}

    # ── Table clients ─────────────────────────────────────────────────────────
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

    def _data_table(self) -> TableClient:
        return self._table(self._data_table_name())

    def _checkpoint_table(self) -> TableClient:
        return self._table(self._checkpoint_table_name())

    def _summary_table(self) -> TableClient | None:
        return self._table(self._summary_table_name()) if self._summary_table_name else None

    def _table_names(self) -> list[str]:
        names = [self._data_table_name(), self._checkpoint_table_name()]
        if self._summary_table_name:
            names.append(self._summary_table_name())
        return names

    # ── Flush (delete + recreate) ─────────────────────────────────────────────
    def delete_usage_tables(self) -> list[str]:
        """Delete the data + checkpoint tables (full flush). Idempotent.

        Deleting a table is a single O(1) service call regardless of row count —
        far cheaper than batch-deleting millions of entities. The name then stays
        reserved (409 ``TableBeingDeleted``) for up to ~40s on real Azure Storage,
        so recreation is handled separately (see :meth:`ensure_usage_tables`).
        Both tables are cleared together: keeping stale checkpoints after wiping
        the data would make the watermark lie about coverage.
        """
        service = table_service_client()
        deleted = []
        for name in self._table_names():
            try:
                service.delete_table(name)
                deleted.append(name)
            except ResourceNotFoundError:
                pass
            self._table_cache.pop(name, None)
        logging.info("Requested deletion of usage tables: %s", deleted)
        return deleted

    def ensure_usage_tables(self) -> bool:
        """Recreate the tables. Returns ``False`` while a table is still being
        deleted (409 ``TableBeingDeleted``) so the caller can wait and retry."""
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
        return True

    # ── Daily usage records ───────────────────────────────────────────────────
    def upsert_daily_records(self, records: list[dict], merge: bool = False) -> int:
        """Upsert daily usage records, batching transactions per partition.

        Each record must carry ``resourceId``, ``subscriptionId``, ``date``
        (``yyyy-mm-dd``) and ``currency``; the metric fields ``uptimeHours`` and
        ``cost`` are each optional so a single stream can write just its own
        metric.

        Pass ``merge=True`` (UpdateMode.MERGE) for the per-page pipeline: an EA
        run writes hours and cost in *separate* streams, so MERGE + partial
        records keeps one stream from clobbering the other's field. The batch/sync
        path leaves the default REPLACE (records carry both metrics).
        """
        if not records:
            return 0

        now = datetime.now(timezone.utc)
        mode = UpdateMode.MERGE if merge else UpdateMode.REPLACE
        by_partition: dict[str, list[dict]] = {}
        for rec in records:
            resource_id = rec["resourceId"]
            pk = resource_partition_key(resource_id)
            row_key = rec["date"].replace("-", "")
            entity = {
                "PartitionKey": pk,
                "RowKey": row_key,
                "ResourceId": resource_id,
                "SubscriptionId": rec.get("subscriptionId"),
                "UsageDate": rec["date"],
                "RetrievedAt": now,
            }
            if rec.get("currency") is not None:
                entity["Currency"] = rec["currency"]
            if "uptimeHours" in rec:
                entity["UptimeHours"] = float(rec.get("uptimeHours") or 0.0)
            if "cost" in rec:
                entity["Cost"] = float(rec.get("cost") or 0.0)
            by_partition.setdefault(pk, []).append(entity)

        table = self._data_table()
        written = 0
        for entities in by_partition.values():
            for start in range(0, len(entities), _MAX_BATCH):
                chunk = entities[start:start + _MAX_BATCH]
                table.submit_transaction([("upsert", e, {"mode": mode}) for e in chunk])
                written += len(chunk)
        self._upsert_summary_records(records)
        logging.info("Upserted %d daily usage record(s).", written)
        return written

    def _upsert_summary_records(self, records: list[dict]) -> None:
        """MERGE the newest 30 daily values into the optional read projection."""
        table = self._summary_table()
        if table is None:
            return

        finalized_end = datetime.now(timezone.utc).date() - timedelta(
            days=settings.cost_finalization_lag_days()
        )
        cutoff = finalized_end - timedelta(days=_SUMMARY_DAYS - 1)
        entities_by_key: dict[tuple[str, str], dict] = {}
        for record in records:
            resource_id = str(record.get("resourceId") or "").strip().lower()
            subscription_id = str(record.get("subscriptionId") or "").strip().lower()
            if not resource_id or not subscription_id:
                continue
            usage_date = datetime.strptime(record["date"], "%Y-%m-%d").date()
            if usage_date < cutoff or usage_date > finalized_end:
                continue
            row_key = resource_partition_key(resource_id)
            entity = entities_by_key.setdefault(
                (subscription_id, row_key),
                {
                    "PartitionKey": subscription_id,
                    "RowKey": row_key,
                    "ResourceId": resource_id,
                    "UpdatedAt": datetime.now(timezone.utc),
                },
            )
            slot = usage_date.toordinal() % _SUMMARY_DAYS
            suffix = f"{slot:02d}"
            if record.get("currency") is not None:
                entity["Currency"] = record["currency"]
            if "uptimeHours" in record:
                entity[f"HoursDate{suffix}"] = record["date"]
                entity[f"Hours{suffix}"] = float(record.get("uptimeHours") or 0.0)
            if "cost" in record:
                entity[f"CostDate{suffix}"] = record["date"]
                entity[f"Cost{suffix}"] = float(record.get("cost") or 0.0)

        by_partition: dict[str, list[dict]] = {}
        for entity in entities_by_key.values():
            by_partition.setdefault(entity["PartitionKey"], []).append(entity)
        for entities in by_partition.values():
            for start in range(0, len(entities), _MAX_BATCH):
                chunk = entities[start:start + _MAX_BATCH]
                table.submit_transaction([
                    ("upsert", entity, {"mode": UpdateMode.MERGE})
                    for entity in chunk
                ])

    def query_usage_summaries(self, subscription_ids) -> list[dict]:
        """Read rolling usage entities by their indexed subscription partitions."""
        table = self._summary_table()
        if table is None:
            return []
        subscriptions = sorted({str(value).strip().lower() for value in subscription_ids if value})
        if not subscriptions:
            return []

        def query_partition(subscription_id: str) -> list[dict]:
            return list(table.query_entities(f"PartitionKey eq '{subscription_id}'"))

        entities = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(subscriptions))) as executor:
            for partition_rows in executor.map(query_partition, subscriptions):
                entities.extend(partition_rows)
        return entities

    def projection_is_current(self, state: dict) -> bool:
        """Whether the optional read projection represented by state is current."""
        if self._summary_table_name is None:
            return True
        return state.get("projectionVersion") == _SUMMARY_PROJECTION_VERSION

    def delete_subscriptions_data(self, sub_ids) -> int:
        """Delete every daily-usage row for the given subscription IDs.

        The data table is partitioned by resource hash, so rows are located by
        scanning the ``SubscriptionId`` property, then deleted in per-partition
        batches. Returns the number of rows deleted.
        """
        subs = [s.lower() for s in sub_ids if s]
        if not subs:
            return 0
        table = self._data_table()
        by_partition: dict[str, list[str]] = {}
        for sub in subs:
            for e in table.query_entities(f"SubscriptionId eq '{sub}'", select=["PartitionKey", "RowKey"]):
                by_partition.setdefault(e["PartitionKey"], []).append(e["RowKey"])
        deleted = 0
        for pk, row_keys in by_partition.items():
            for start in range(0, len(row_keys), _MAX_BATCH):
                chunk = row_keys[start:start + _MAX_BATCH]
                table.submit_transaction([("delete", {"PartitionKey": pk, "RowKey": rk}) for rk in chunk])
                deleted += len(chunk)
        summary_table = self._summary_table()
        if summary_table is not None:
            for sub in subs:
                summary_rows = list(summary_table.query_entities(
                    f"PartitionKey eq '{sub}'", select=["PartitionKey", "RowKey"]
                ))
                for start in range(0, len(summary_rows), _MAX_BATCH):
                    chunk = summary_rows[start:start + _MAX_BATCH]
                    summary_table.submit_transaction([
                        ("delete", {"PartitionKey": row["PartitionKey"], "RowKey": row["RowKey"]})
                        for row in chunk
                    ])
                    deleted += len(chunk)
        logging.info("Deleted %d row(s) across %d subscription(s).", deleted, len(subs))
        return deleted

    # ── Dataset state (single record) ─────────────────────────────────────────
    def get_state(self) -> dict | None:
        """Return the record describing the currently-loaded dataset, or ``None``.

        ``subs`` is the sorted resolved subscription set, or ``None`` for the
        per-billing *unfiltered whole profile* (the ALL marker).
        """
        try:
            e = self._checkpoint_table().get_entity(partition_key=_STATE_PK, row_key=_STATE_RK)
        except Exception:
            return None
        return {
            "method": e.get("Method"),
            "billingAccount": e.get("BillingAccount") or None,
            "billingProfile": e.get("BillingProfile") or None,
            "locations": json.loads(e.get("Locations") or "[]"),
            "subs": None if e.get("IsAll") else decode_json_array_properties(
                e,
                legacy_property="Subs",
                chunk_prefix="Subs",
                count_property="SubsChunkCount",
            ),
            "configSelectors": json.loads(e.get("ConfigSelectors") or "{}"),
            "coverageStart": e.get("CoverageStart"),
            "coverageEnd": e.get("CoverageEnd"),
            "projectionVersion": e.get("ProjectionVersion"),
        }

    def set_state(self, state: dict) -> None:
        """Persist the single dataset state record (replace)."""
        is_all = state.get("subs") is None
        entity = {
                "PartitionKey": _STATE_PK,
                "RowKey": _STATE_RK,
                "Method": state.get("method"),
                "BillingAccount": state.get("billingAccount") or "",
                "BillingProfile": state.get("billingProfile") or "",
                "Locations": json.dumps(state.get("locations") or []),
                "IsAll": is_all,
                "ConfigSelectors": json.dumps(state.get("configSelectors") or {}),
                "CoverageStart": state.get("coverageStart"),
                "CoverageEnd": state.get("coverageEnd"),
                "ProjectionVersion": _SUMMARY_PROJECTION_VERSION if self._summary_table_name else 0,
                "UpdatedAt": datetime.now(timezone.utc),
            }
        if is_all:
            entity["Subs"] = ""
            entity["SubsChunkCount"] = 0
        else:
            entity.update(encode_json_array_properties(
                sorted(state.get("subs") or []),
                legacy_property="Subs",
                chunk_prefix="Subs",
                count_property="SubsChunkCount",
            ))
        self._checkpoint_table().upsert_entity(
            entity,
            mode=UpdateMode.REPLACE,
        )

    def delete_state(self) -> None:
        """Remove the dataset state record (idempotent)."""
        try:
            self._checkpoint_table().delete_entity(partition_key=_STATE_PK, row_key=_STATE_RK)
        except Exception:
            pass
