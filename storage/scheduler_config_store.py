"""Azure Table persistence for data-collection scheduler configuration.

One row per pipeline holds runtime-editable cadence and enablement. The store
returns raw rows (or ``None`` on a miss); merging registry defaults is the
service layer's job, so storage never imports the registry.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from core import settings
from .table import LazyTableClient, table_service_client

_CONFIG_PK = "config"


class SchedulerConfigConflictError(ValueError):
    """The submitted config revision is stale or the row changed concurrently."""


class SchedulerConfigStore:
    def __init__(self, table_name: Callable[[], str] = settings.scheduler_config_table_name) -> None:
        self._table_name = table_name
        self._table = LazyTableClient(table_name, table_service_client)

    @staticmethod
    def _row_to_config(entity) -> dict[str, Any]:
        return {
            "pipeline": entity["RowKey"],
            "enabled": entity.get("Enabled"),
            "frequencySeconds": entity.get("FrequencySeconds"),
            "anchorSeconds": entity.get("AnchorSeconds"),
            "revision": entity.get("Revision"),
            "updatedAt": entity.get("UpdatedAt"),
            "updatedBy": json.loads(entity["UpdatedBy"]) if entity.get("UpdatedBy") else None,
        }

    def get(self, pipeline_id: str) -> dict[str, Any] | None:
        try:
            entity = self._table().get_entity(_CONFIG_PK, pipeline_id)
        except ResourceNotFoundError:
            return None
        return self._row_to_config(entity)

    def list(self) -> list[dict[str, Any]]:
        entities = self._table().query_entities(f"PartitionKey eq '{_CONFIG_PK}'")
        return [self._row_to_config(entity) for entity in entities]

    def clear(self) -> int:
        """Delete every override row; effective config then reverts to registry defaults."""
        table = self._table()
        cleared = 0
        for entity in table.query_entities(f"PartitionKey eq '{_CONFIG_PK}'"):
            table.delete_entity(_CONFIG_PK, entity["RowKey"])
            cleared += 1
        return cleared

    def patch(
        self,
        pipeline_id: str,
        *,
        enabled: bool,
        frequency_seconds: int,
        anchor_seconds: int,
        expected_revision: int | None,
        updated_by: dict[str, Any],
        updated_at: datetime,
    ) -> dict[str, Any]:
        table = self._table()
        try:
            existing = table.get_entity(_CONFIG_PK, pipeline_id)
        except ResourceNotFoundError:
            existing = None

        if existing is None:
            if expected_revision not in (None, 0):
                raise SchedulerConfigConflictError(
                    "A new scheduler config row must not provide a revision."
                )
            entity = self._build_entity(
                pipeline_id, enabled, frequency_seconds, anchor_seconds, 1, updated_by, updated_at
            )
            try:
                table.create_entity(entity)
            except ResourceExistsError as exc:
                raise SchedulerConfigConflictError(
                    "Scheduler config was created concurrently."
                ) from exc
            return self._row_to_config(entity)

        current_revision = int(existing.get("Revision") or 0)
        if expected_revision is None or int(expected_revision) != current_revision:
            raise SchedulerConfigConflictError(
                f"Scheduler config revision is stale; current revision is {current_revision}."
            )
        entity = self._build_entity(
            pipeline_id,
            enabled,
            frequency_seconds,
            anchor_seconds,
            current_revision + 1,
            updated_by,
            updated_at,
        )
        try:
            table.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=existing.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except HttpResponseError as exc:
            if exc.status_code == 412:
                raise SchedulerConfigConflictError(
                    "Scheduler config was modified concurrently."
                ) from exc
            raise
        return self._row_to_config(entity)

    @staticmethod
    def _build_entity(
        pipeline_id: str,
        enabled: bool,
        frequency_seconds: int,
        anchor_seconds: int,
        revision: int,
        updated_by: dict[str, Any],
        updated_at: datetime,
    ) -> dict[str, Any]:
        return {
            "PartitionKey": _CONFIG_PK,
            "RowKey": pipeline_id,
            "Enabled": bool(enabled),
            "FrequencySeconds": int(frequency_seconds),
            "AnchorSeconds": int(anchor_seconds),
            "Revision": revision,
            "UpdatedAt": updated_at,
            "UpdatedBy": json.dumps(updated_by, separators=(",", ":")),
        }
