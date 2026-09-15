"""Shared Azure Table persistence primitives."""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator, Sequence
from typing import Callable, TypeVar

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient, TableServiceClient

from core import credentials, settings

MAX_TRANSACTION_OPERATIONS = 100
MAX_STRING_PROPERTY_CHARS = 30_000

T = TypeVar("T")
_client_lock = threading.Lock()
_service_clients = {}


def table_service_client() -> TableServiceClient:
    local = credentials.is_running_locally()
    key = (
        "local",
        os.getenv("AzureWebJobsStorage") or "UseDevelopmentStorage=true",
    ) if local else ("azure", settings.table_storage_endpoint(), os.getenv("AZURE_CLIENT_ID"))
    client = _service_clients.get(key)
    if client is not None:
        return client
    with _client_lock:
        client = _service_clients.get(key)
        if client is not None:
            return client
        if local:
            client = TableServiceClient.from_connection_string(key[1])
        else:
            client = TableServiceClient(endpoint=key[1], credential=credentials.get_credential())
        _service_clients[key] = client
        return client


class LazyTableClient:
    """Create and cache one named table client on first use."""

    def __init__(
        self,
        table_name: Callable[[], str],
        service_factory: Callable[[], TableServiceClient] = table_service_client,
    ) -> None:
        self._table_name = table_name
        self._service_factory = service_factory
        self._client: TableClient | None = None

    def __call__(self) -> TableClient:
        if self._client is None:
            service = self._service_factory()
            try:
                service.create_table_if_not_exists(self._table_name())
            except ResourceExistsError:
                pass
            self._client = service.get_table_client(self._table_name())
        return self._client

    def invalidate(self) -> None:
        self._client = None


def batches(values: Sequence[T], size: int = MAX_TRANSACTION_OPERATIONS) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def chunk_json_array(
    values: Iterable[str],
    max_chars: int = MAX_STRING_PROPERTY_CHARS,
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for value in values:
        candidate = json.dumps([*current, value], separators=(",", ":"))
        if current and len(candidate) > max_chars:
            chunks.append(json.dumps(current, separators=(",", ":")))
            current = [value]
        else:
            current.append(value)
    if current:
        chunks.append(json.dumps(current, separators=(",", ":")))
    return chunks


def encode_json_array_properties(
    values: Iterable[str],
    *,
    legacy_property: str,
    chunk_prefix: str,
    count_property: str,
) -> dict:
    normalized = list(values)
    serialized = json.dumps(normalized, separators=(",", ":"))
    if len(serialized) <= MAX_STRING_PROPERTY_CHARS:
        return {legacy_property: serialized, count_property: 0}
    chunks = chunk_json_array(normalized)
    properties = {count_property: len(chunks)}
    properties.update({f"{chunk_prefix}{index:03d}": chunk for index, chunk in enumerate(chunks)})
    return properties


def decode_json_array_properties(
    entity: dict,
    *,
    legacy_property: str,
    chunk_prefix: str,
    count_property: str,
) -> list:
    chunk_count = int(entity.get(count_property) or 0)
    if not chunk_count:
        return json.loads(entity.get(legacy_property) or "[]")
    values = []
    for index in range(chunk_count):
        values.extend(json.loads(entity[f"{chunk_prefix}{index:03d}"]))
    return values