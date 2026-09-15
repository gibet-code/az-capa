"""Azure Table persistence for shared manual ODCR coverage decisions."""
from __future__ import annotations

import concurrent.futures
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from azure.data.tables import UpdateMode

from core import settings
from core.identity import UserAuditMetadata
from .table import LazyTableClient, MAX_TRANSACTION_OPERATIONS as _MAX_BATCH, table_service_client

_MAX_READ_CONCURRENCY = 20
_COVERAGE_SELECT = [
    "RowKey",
    "ResourceId",
    "Decision",
    "Priority",
    "Note",
    "UpdatedByTenantId",
    "UpdatedByObjectId",
    "UpdatedByDisplayName",
    "UpdatedAt",
]


def resource_row_key(resource_id: str) -> str:
    return hashlib.sha256(resource_id.encode("utf-8")).hexdigest()


class OdcrCoverageDecisionsStore:
    def __init__(self, table_name: Callable[[], str] = settings.odcr_coverage_decisions_table_name) -> None:
        self._table_name = table_name
        self._table = LazyTableClient(table_name, table_service_client)

    def upsert_change(
        self,
        change,
        audit: UserAuditMetadata,
        updated_at: datetime | None = None,
    ) -> list[dict]:
        timestamp = updated_at or datetime.now(timezone.utc)
        by_partition: dict[str, list[dict]] = defaultdict(list)
        for resource_id in change.resource_ids:
            subscription_id = resource_id.split("/")[2].lower()
            by_partition[subscription_id].append({
                "PartitionKey": subscription_id,
                "RowKey": resource_row_key(resource_id),
                "ResourceId": resource_id,
                "Decision": change.decision or "cleared",
                "Priority": change.priority,
                "Note": change.note,
                "UpdatedByTenantId": audit.tenant_id,
                "UpdatedByObjectId": audit.object_id,
                "UpdatedByDisplayName": audit.display_name,
                "UpdatedAt": timestamp,
            })
        table = self._table()
        entities: list[dict] = []
        for partition_entities in by_partition.values():
            for start in range(0, len(partition_entities), _MAX_BATCH):
                chunk = partition_entities[start:start + _MAX_BATCH]
                table.submit_transaction([
                    ("upsert", entity, {"mode": UpdateMode.REPLACE}) for entity in chunk
                ])
                entities.extend(chunk)
        return entities

    def get_for_subscriptions(self, subscription_ids) -> dict[str, dict]:
        normalized_subscriptions = sorted({
            str(value).strip().lower() for value in subscription_ids if value
        })
        if not normalized_subscriptions:
            return {}
        table = self._table()
        result: dict[str, dict] = {}

        def query_subscription(subscription_id: str):
            escaped = subscription_id.replace("'", "''")
            return table.query_entities(
                f"PartitionKey eq '{escaped}'",
                select=_COVERAGE_SELECT,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_READ_CONCURRENCY, len(normalized_subscriptions))
        ) as executor:
            row_groups = executor.map(query_subscription, normalized_subscriptions)
            rows = (entity for group in row_groups for entity in group)
            for entity in rows:
                resource_id = str(entity.get("ResourceId") or "").strip().lower()
                if resource_id and resource_row_key(resource_id) == entity.get("RowKey"):
                    result[resource_id] = entity
        return result


store = OdcrCoverageDecisionsStore()