"""Shared Blob snapshot serialization primitives."""
from __future__ import annotations

import gzip
import json
import os
import threading
from datetime import datetime

from azure.storage.blob import BlobServiceClient

from core import credentials, settings

_client_lock = threading.Lock()
_service_clients = {}


def blob_service_client() -> BlobServiceClient:
    local = credentials.is_running_locally()
    key = (
        "local",
        os.getenv("AzureWebJobsStorage") or "UseDevelopmentStorage=true",
    ) if local else ("azure", settings.blob_storage_endpoint(), os.getenv("AZURE_CLIENT_ID"))
    client = _service_clients.get(key)
    if client is not None:
        return client
    with _client_lock:
        client = _service_clients.get(key)
        if client is not None:
            return client
        if local:
            client = BlobServiceClient.from_connection_string(key[1], api_version="2023-11-03")
        else:
            client = BlobServiceClient(
                account_url=key[1],
                credential=credentials.get_credential(),
                api_version="2023-11-03",
            )
        _service_clients[key] = client
        return client


def encode_json_gzip(payload: dict) -> bytes:
    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
    ).encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def decode_json(raw: bytes) -> dict:
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    return json.loads(raw)