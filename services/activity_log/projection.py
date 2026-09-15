"""Pure source-event projection and payload compression (Sections 4 and 6).

Projection promotes a recipe's declared source paths to typed columns and
gzip-compresses the whole event for inline retention. It is best-effort: a path
that cannot be resolved yields no column and flags the event
``reprojection-pending`` so a later versioned replay (Section 10.3) can re-derive
it from the retained payload. Projection never raises on bad data.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime

from .recipes import ProjectedField, Recipe

PAYLOAD_ENCODING = "gzip+json"

_MISSING = object()


@dataclass(frozen=True)
class ProjectionResult:
    columns: dict[str, object] = field(default_factory=dict)
    reprojection_pending: bool = False


def _coerce_container(value: object) -> object:
    """Parse an embedded JSON string (responseBody/statusMessage) into a container."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return value
    return value


def resolve_path(properties: object, path: str) -> object:
    """Resolve a dotted ``path`` against ``properties``.

    Returns ``_MISSING`` when a segment is absent so callers distinguish an
    absent field from a real ``None`` value.
    """
    current: object = properties
    for segment in path.split("."):
        current = _coerce_container(current)
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _status_matches(on_status: str | None, status: str | None) -> bool:
    if on_status is None:
        return True
    return bool(status) and status.lower() == on_status.lower()


def project(event: dict, recipe: Recipe) -> ProjectionResult:
    """Derive a recipe's projected columns from one event (best-effort)."""
    properties = event.get("properties")
    status = event.get("status")
    columns: dict[str, object] = {}
    pending = False
    for spec in recipe.projected_fields:
        if not _status_matches(spec.on_status, status):
            continue
        try:
            value = resolve_path(properties, spec.path)
        except Exception:  # noqa: BLE001 — projection is best-effort, never fatal
            pending = True
            continue
        if value is _MISSING or value is None:
            continue
        columns[spec.column] = value
    return ProjectionResult(columns=columns, reprojection_pending=pending)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def compress_payload(event: dict) -> bytes:
    """Serialize and gzip the whole event for inline retention."""
    raw = json.dumps(
        event,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def decompress_payload(payload: bytes) -> dict:
    """Inverse of :func:`compress_payload` for replay (Section 10.3)."""
    return json.loads(gzip.decompress(payload))
