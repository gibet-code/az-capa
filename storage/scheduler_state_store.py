"""Azure Table persistence for data-collection scheduler run state.

One row per pipeline scope records whether a run is active and when it last
dispatched. The claim primitive is atomic (insert-if-absent / ETag-if-unchanged)
so two concurrent callers cannot both start the same scope.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from core import settings
from .table import LazyTableClient, table_service_client

_STATE_PK = "state"
_MARKER_PK = "dispatcher"
_MARKER_RK = "marker"


def _drop_none(entity: dict[str, Any]) -> dict[str, Any]:
    # Table Storage cannot store None; omitting a property under REPLACE clears it.
    return {key: value for key, value in entity.items() if value is not None}


class SchedulerStateStore:
    def __init__(self, table_name: Callable[[], str] = settings.scheduler_run_state_table_name) -> None:
        self._table_name = table_name
        self._table = LazyTableClient(table_name, table_service_client)

    @staticmethod
    def _row_to_state(entity) -> dict[str, Any]:
        return {
            "pipeline": entity["RowKey"],
            "scope": entity.get("Scope"),
            "activeInstanceId": entity.get("ActiveInstanceId"),
            "activeAction": entity.get("ActiveAction"),
            "lastDispatchedAt": entity.get("LastDispatchedAt"),
            "lastTrigger": entity.get("LastTrigger"),
            "lastOutcome": entity.get("LastOutcome"),
            "lastCompletedAt": entity.get("LastCompletedAt"),
        }

    def get(self, pipeline_id: str) -> dict[str, Any] | None:
        try:
            entity = self._table().get_entity(_STATE_PK, pipeline_id)
        except ResourceNotFoundError:
            return None
        return self._row_to_state(entity)

    def list(self) -> list[dict[str, Any]]:
        entities = self._table().query_entities(f"PartitionKey eq '{_STATE_PK}'")
        return [self._row_to_state(entity) for entity in entities]

    def try_claim(
        self,
        pipeline_id: str,
        scope: str,
        action: str,
        trigger: str,
        dispatched_at: datetime,
    ) -> bool:
        """Atomically reserve the scope; return True only for the winning caller.

        Writes a placeholder ``ActiveInstanceId`` so the scope immediately reads
        busy, guarded by insert-if-absent (new row) or ETag-if-unchanged (idle
        existing row). Fill the real id with ``set_active_instance`` on success,
        or ``release_claim`` if ``start_new`` fails.
        """
        table = self._table()
        placeholder = f"pending:{uuid.uuid4().hex}"
        try:
            entity = table.get_entity(_STATE_PK, pipeline_id)
        except ResourceNotFoundError:
            entity = None

        if entity is None:
            new_entity = _drop_none({
                "PartitionKey": _STATE_PK,
                "RowKey": pipeline_id,
                "Scope": scope,
                "ActiveInstanceId": placeholder,
                "ActiveAction": action,
                "LastDispatchedAt": dispatched_at,
                "LastTrigger": trigger,
            })
            try:
                table.create_entity(new_entity)
                return True
            except ResourceExistsError:
                return False

        if entity.get("ActiveInstanceId"):
            return False

        entity["Scope"] = scope
        entity["ActiveInstanceId"] = placeholder
        entity["ActiveAction"] = action
        entity["LastDispatchedAt"] = dispatched_at
        entity["LastTrigger"] = trigger
        try:
            table.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=entity.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except HttpResponseError as exc:
            if exc.status_code == 412:
                return False
            raise

    def set_active_instance(self, pipeline_id: str, instance_id: str) -> None:
        """Replace the placeholder claim with the real orchestration instance id."""
        self._table().upsert_entity(
            {"PartitionKey": _STATE_PK, "RowKey": pipeline_id, "ActiveInstanceId": instance_id},
            mode=UpdateMode.MERGE,
        )

    def release_claim(self, pipeline_id: str) -> None:
        """Clear an unfilled claim after a failed ``start_new``."""
        self._clear_active(pipeline_id)

    def close_run(self, pipeline_id: str, outcome: str, completed_at: datetime) -> None:
        """Clear the active run and record its terminal outcome."""
        self._clear_active(pipeline_id, outcome=outcome, completed_at=completed_at)

    def _clear_active(
        self,
        pipeline_id: str,
        outcome: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        try:
            entity = self._table().get_entity(_STATE_PK, pipeline_id)
        except ResourceNotFoundError:
            return
        # ActiveInstanceId + ActiveAction intentionally omitted so REPLACE clears them.
        new_entity = _drop_none({
            "PartitionKey": _STATE_PK,
            "RowKey": pipeline_id,
            "Scope": entity.get("Scope"),
            "LastDispatchedAt": entity.get("LastDispatchedAt"),
            "LastTrigger": entity.get("LastTrigger"),
            "LastOutcome": outcome if outcome is not None else entity.get("LastOutcome"),
            "LastCompletedAt": completed_at if completed_at is not None else entity.get("LastCompletedAt"),
        })
        self._table().upsert_entity(new_entity, mode=UpdateMode.REPLACE)

    def get_marker(self) -> dict[str, Any]:
        try:
            entity = self._table().get_entity(_MARKER_PK, _MARKER_RK)
        except ResourceNotFoundError:
            return {}
        return {"lastTickAt": entity.get("LastTickAt"), "lastPrunedAt": entity.get("LastPrunedAt")}

    def record_tick(self, tick_at: datetime, pruned_at: datetime | None = None) -> None:
        entity = {"PartitionKey": _MARKER_PK, "RowKey": _MARKER_RK, "LastTickAt": tick_at}
        if pruned_at is not None:
            entity["LastPrunedAt"] = pruned_at
        self._table().upsert_entity(entity, mode=UpdateMode.MERGE)
