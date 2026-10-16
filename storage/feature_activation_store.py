"""Azure Table persistence for the singleton feature-activation state."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from core import settings
from .table import LazyTableClient, table_service_client

_PARTITION_KEY = "config"
_ROW_KEY = "features"


class FeatureActivationConflictError(ValueError):
    """The singleton feature state changed concurrently."""


@dataclass(frozen=True)
class StoredFeatureActivation:
    enabled_feature_ids: tuple[str, ...]
    etag: str


class FeatureActivationStore:
    def __init__(
        self,
        table_name: Callable[[], str] = settings.feature_activation_table_name,
    ) -> None:
        self._table = LazyTableClient(table_name, table_service_client)

    def get(self) -> StoredFeatureActivation | None:
        try:
            entity = self._table().get_entity(_PARTITION_KEY, _ROW_KEY)
        except ResourceNotFoundError:
            return None
        enabled = json.loads(entity["EnabledFeatureIds"])
        if not isinstance(enabled, list) or any(not isinstance(value, str) for value in enabled):
            raise ValueError("EnabledFeatureIds must be a JSON array of strings.")
        return StoredFeatureActivation(
            enabled_feature_ids=tuple(enabled),
            etag=str(entity.metadata["etag"]),
        )

    def replace(
        self,
        enabled_feature_ids: tuple[str, ...],
        *,
        expected_etag: str | None,
    ) -> None:
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": _ROW_KEY,
            "EnabledFeatureIds": json.dumps(enabled_feature_ids, separators=(",", ":")),
        }
        table = self._table()
        if expected_etag is None:
            try:
                table.create_entity(entity)
            except ResourceExistsError as exc:
                raise FeatureActivationConflictError(
                    "Feature activation was created concurrently."
                ) from exc
            return
        try:
            table.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=expected_etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except HttpResponseError as exc:
            if exc.status_code == 412:
                raise FeatureActivationConflictError(
                    "Feature activation was modified concurrently."
                ) from exc
            raise
