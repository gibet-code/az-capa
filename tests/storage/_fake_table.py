"""In-memory Azure Table double for scheduler-store unit tests.

Supports the OData subset the scheduler stores emit (``eq``/``ne``/``lt``/``ge``
joined by ``and``, string and ``datetime'...'`` literals) plus ETag-guarded
updates so the atomic claim path can be tested without Azurite.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode

_CLAUSE = re.compile(r"^(\w+)\s+(eq|ne|lt|le|gt|ge)\s+(.+)$")


def _parse_value(token: str) -> Any:
    token = token.strip()
    if token.startswith("datetime'") and token.endswith("'"):
        return datetime.fromisoformat(token[len("datetime'"):-1])
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1]
    return token


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if actual is None:
        return False
    if op == "lt":
        return actual < expected
    if op == "le":
        return actual <= expected
    if op == "gt":
        return actual > expected
    if op == "ge":
        return actual >= expected
    raise ValueError(f"Unsupported operator {op!r}")


def _matches(entity: dict, query: str) -> bool:
    for clause in query.split(" and "):
        match = _CLAUSE.match(clause.strip())
        if match is None:
            raise ValueError(f"Unsupported filter clause: {clause!r}")
        field, op, raw = match.groups()
        if not _compare(entity.get(field), op, _parse_value(raw)):
            return False
    return True


class FakeEntity(dict):
    def __init__(self, data: dict, etag: str) -> None:
        super().__init__(data)
        self.metadata = {"etag": etag}


def _precondition_failed() -> HttpResponseError:
    error = HttpResponseError("The update condition specified in the request was not satisfied.")
    error.status_code = 412
    return error


class FakeTable:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self._etags: dict[tuple[str, str], str] = {}
        self._seq = 0

    def _bump(self, key: tuple[str, str]) -> None:
        self._seq += 1
        self._etags[key] = f"W/\"etag-{self._seq}\""

    def create_entity(self, entity: dict) -> None:
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("The entity already exists.")
        self.rows[key] = dict(entity)
        self._bump(key)

    def get_entity(self, partition_key: str, row_key: str) -> FakeEntity:
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("The entity does not exist.")
        return FakeEntity(self.rows[key], self._etags[key])

    def upsert_entity(self, entity: dict, mode: UpdateMode = UpdateMode.MERGE) -> None:
        key = (entity["PartitionKey"], entity["RowKey"])
        if mode == UpdateMode.MERGE and key in self.rows:
            self.rows[key].update(dict(entity))
        else:
            self.rows[key] = dict(entity)
        self._bump(key)

    def update_entity(
        self,
        entity: dict,
        mode: UpdateMode = UpdateMode.MERGE,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> None:
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self.rows:
            raise ResourceNotFoundError("The entity does not exist.")
        if etag is not None and etag != self._etags[key]:
            raise _precondition_failed()
        if mode == UpdateMode.MERGE:
            self.rows[key].update(dict(entity))
        else:
            self.rows[key] = dict(entity)
        self._bump(key)

    def delete_entity(self, partition_key: str, row_key: str, **_kwargs: Any) -> None:
        key = (partition_key, row_key)
        self.rows.pop(key, None)
        self._etags.pop(key, None)

    def query_entities(self, query: str, select: list[str] | None = None) -> list[FakeEntity]:
        # Real Table Storage returns rows ordered by (PartitionKey, RowKey).
        ordered = sorted(self.rows.items(), key=lambda item: item[0])
        return [
            FakeEntity(dict(value), self._etags[key])
            for key, value in ordered
            if _matches(value, query)
        ]
