"""Azure Table persistence for data-collection scheduler run history.

One compact, pre-labeled row per dispatched root run (no sub-orchestrator noise).
RowKey is an inverted timestamp so a partition scan returns newest-first. This is
the source of truth for the notifications feed and the per-pipeline run drawer.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from azure.data.tables import UpdateMode

from core import settings
from .table import LazyTableClient, table_service_client

_RUNNING = "running"
# Inverted-timestamp base (year ~5138 in ms); RowKey = base - startedAt_ms.
_MAX_MS = 99999999999999


def _row_key(started_at: datetime) -> str:
    milliseconds = int(started_at.timestamp() * 1000)
    return f"{_MAX_MS - milliseconds:014d}-{uuid.uuid4().hex}"


class SchedulerHistoryStore:
    def __init__(self, table_name: Callable[[], str] = settings.scheduler_run_history_table_name) -> None:
        self._table_name = table_name
        self._table = LazyTableClient(table_name, table_service_client)

    @staticmethod
    def _row_to_run(entity) -> dict[str, Any]:
        return {
            "pipeline": entity.get("Pipeline") or entity["PartitionKey"],
            "rowKey": entity["RowKey"],
            "action": entity.get("Action"),
            "trigger": entity.get("Trigger"),
            "startedAt": entity.get("StartedAt"),
            "status": entity.get("Status"),
            "completedAt": entity.get("CompletedAt"),
            "instanceId": entity.get("InstanceId"),
            "summary": json.loads(entity["Summary"]) if entity.get("Summary") else None,
        }

    def open_row(
        self,
        pipeline_id: str,
        action: str,
        trigger: str,
        started_at: datetime,
        instance_id: str,
    ) -> str:
        row_key = _row_key(started_at)
        self._table().create_entity({
            "PartitionKey": pipeline_id,
            "RowKey": row_key,
            "Pipeline": pipeline_id,
            "Action": action,
            "Trigger": trigger,
            "StartedAt": started_at,
            "Status": _RUNNING,
            "InstanceId": instance_id,
        })
        return row_key

    def close_by_instance(
        self,
        pipeline_id: str,
        instance_id: str,
        status: str,
        completed_at: datetime,
        summary: dict[str, Any] | None = None,
    ) -> bool:
        """Close the still-running row for this instance; no-op if already closed.

        Only rows whose ``Status`` is still ``running`` are updated, so the rich
        completion activity and the reconciler backstop never fight: whichever
        closes first wins and the other finds no running row.
        """
        table = self._table()
        query = f"PartitionKey eq '{pipeline_id}' and InstanceId eq '{instance_id}' and Status eq '{_RUNNING}'"
        closed = False
        for entity in table.query_entities(query):
            update = {
                "PartitionKey": entity["PartitionKey"],
                "RowKey": entity["RowKey"],
                "Status": status,
                "CompletedAt": completed_at,
            }
            if summary is not None:
                update["Summary"] = json.dumps(summary, separators=(",", ":"))
            table.upsert_entity(update, mode=UpdateMode.MERGE)
            closed = True
        return closed

    def list_by_pipeline(self, pipeline_id: str, limit: int = 50, after: str | None = None) -> list[dict[str, Any]]:
        """Root-only rows for one pipeline, newest first (inverted-ticks RowKey).

        ``after`` is a RowKey continuation: because RowKey ascends oldest→newest
        inverted, ``RowKey gt after`` yields the page following ``after``.
        """
        query = f"PartitionKey eq '{pipeline_id}'"
        if after:
            query += f" and RowKey gt '{after}'"
        runs: list[dict[str, Any]] = []
        for entity in self._table().query_entities(query):
            runs.append(self._row_to_run(entity))
            if len(runs) >= limit:
                break
        return runs

    def list_running(self) -> list[dict[str, Any]]:
        query = f"Status eq '{_RUNNING}'"
        return [self._row_to_run(entity) for entity in self._table().query_entities(query)]

    def list_recent(self, since: datetime) -> list[dict[str, Any]]:
        query = f"StartedAt ge datetime'{since.isoformat()}'"
        return [self._row_to_run(entity) for entity in self._table().query_entities(query)]

    def rows_for_instance(self, instance_id: str) -> list[dict[str, Any]]:
        query = f"InstanceId eq '{instance_id}'"
        return [self._row_to_run(entity) for entity in self._table().query_entities(query)]

    def delete_by_instance(self, instance_id: str) -> int:
        query = f"InstanceId eq '{instance_id}'"
        deleted = 0
        for entity in self._table().query_entities(query, select=["PartitionKey", "RowKey"]):
            self._table().delete_entity(entity["PartitionKey"], entity["RowKey"])
            deleted += 1
        return deleted

    def delete_terminal(self) -> int:
        query = f"Status ne '{_RUNNING}'"
        deleted = 0
        for entity in self._table().query_entities(query, select=["PartitionKey", "RowKey"]):
            self._table().delete_entity(entity["PartitionKey"], entity["RowKey"])
            deleted += 1
        return deleted

    def prune(self, older_than: datetime) -> int:
        query = f"StartedAt lt datetime'{older_than.isoformat()}' and Status ne '{_RUNNING}'"
        deleted = 0
        for entity in self._table().query_entities(
            query, select=["PartitionKey", "RowKey"]
        ):
            self._table().delete_entity(entity["PartitionKey"], entity["RowKey"])
            deleted += 1
        return deleted
