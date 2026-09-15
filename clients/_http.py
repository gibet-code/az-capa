"""Shared HTTP response handling for raw Azure REST clients."""
from __future__ import annotations

import json

TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def retry_after_seconds(headers, fallback: int = 5) -> int:
    """Return Azure's standard retry delay in whole seconds."""
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            return max(1, int(float(raw)))
        except ValueError:
            pass
    raw_ms = headers.get("x-ms-retry-after-ms") if headers else None
    if raw_ms:
        try:
            return max(1, int((float(raw_ms) + 999) // 1000))
        except ValueError:
            pass
    return fallback


def azure_error_detail(raw: bytes) -> str:
    """Extract a stable code/message pair from an Azure JSON error body."""
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
        error = body.get("error", body)
        return f"{error.get('code') or 'HTTP error'}: {error.get('message') or 'No detail'}"
    except (AttributeError, json.JSONDecodeError):
        return "Non-JSON HTTP error response"
