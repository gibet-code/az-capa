"""Pure source-event projection and payload compression (Sections 4 and 6).

Projection promotes a recipe's declared source paths to typed columns,
derives standardized error columns, and gzip-compresses retained JSON. It is
best-effort: a path that cannot be resolved yields no column and flags the event
``reprojection-pending`` so a later versioned replay (Section 10.3) can re-derive
it from the retained payload. Projection never raises on bad data.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime

from .recipes import Recipe

PAYLOAD_ENCODING = "gzip+json"
ERROR_COLUMNS = frozenset({
    "ErrorCode",
    "ErrorMessage",
    "ErrorDetailCode",
    "ErrorDetailMessage",
    "ErrorDetailCount",
    "ErrorCodesJson",
    "HasAllocationFailure",
})

_MISSING = object()


@dataclass(frozen=True)
class ProjectionResult:
    columns: dict[str, object] = field(default_factory=dict)
    error_details_gz: bytes | None = None
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

    Numeric path segments index lists, such as ``error.details.0.code``.
    Returns ``_MISSING`` when a segment is absent so callers distinguish an
    absent field from a real ``None`` value.
    """
    current: object = properties
    for segment in path.split("."):
        current = _coerce_container(current)
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index < len(current):
                current = current[index]
                continue
            return _MISSING
        return _MISSING
    return current


def _status_matches(on_status: str | None, status: str | None) -> bool:
    if on_status is None:
        return True
    return bool(status) and status.lower() == on_status.lower()


def _sanitized_json(value: object) -> object:
    return json.loads(json.dumps(value, default=_json_default))


def _error_codes(error: dict) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def visit(value: object) -> None:
        if not isinstance(value, dict):
            return
        code = value.get("code")
        if isinstance(code, str) and code.strip():
            normalized = code.casefold()
            if normalized not in seen:
                seen.add(normalized)
                codes.append(code)
        details = value.get("details")
        if isinstance(details, list):
            for detail in details:
                visit(detail)

    visit(error)
    return codes


def _project_error(error: object) -> tuple[dict[str, object], bytes | None]:
    if not isinstance(error, dict):
        return {}, None

    columns: dict[str, object] = {}
    parent_code = error.get("code")
    parent_message = error.get("message")
    if parent_code is not None:
        columns["ErrorCode"] = parent_code
    if parent_message is not None:
        columns["ErrorMessage"] = parent_message

    details_value = error.get("details")
    details = details_value if isinstance(details_value, list) else []
    columns["ErrorDetailCount"] = len(details)
    first_detail = details[0] if details and isinstance(details[0], dict) else {}
    if first_detail.get("code") is not None:
        columns["ErrorDetailCode"] = first_detail["code"]
    if first_detail.get("message") is not None:
        columns["ErrorDetailMessage"] = first_detail["message"]

    codes = _error_codes(error)
    columns["ErrorCodesJson"] = json.dumps(codes, separators=(",", ":"))
    columns["HasAllocationFailure"] = any(
        code.casefold() in {"allocationfailed", "zonalallocationfailed"}
        for code in codes
    )
    sanitized_details = _sanitized_json(
        details_value if details_value is not None else []
    )
    return columns, compress_json(sanitized_details)


def project(event: dict, recipe: Recipe) -> ProjectionResult:
    """Derive a recipe's projected columns from one event (best-effort)."""
    properties = event.get("properties")
    status = event.get("status")
    columns: dict[str, object] = {}
    error_details_gz = None
    pending = False
    for spec in recipe.projected_fields:
        if not _status_matches(spec.on_status, status):
            continue
        try:
            value = _MISSING
            for path in (spec.path, *spec.fallback_paths):
                value = resolve_path(properties, path)
                if value is not _MISSING and value is not None:
                    break
        except Exception:  # noqa: BLE001 — projection is best-effort, never fatal
            pending = True
            continue
        if value is _MISSING or value is None:
            continue
        columns[spec.column] = value
    error_spec = recipe.error_projection
    if error_spec and _status_matches(error_spec.on_status, status):
        try:
            error = resolve_path(properties, error_spec.path)
            if error is not _MISSING and error is not None:
                error_columns, error_details_gz = _project_error(error)
                columns.update(error_columns)
        except Exception:  # noqa: BLE001 — projection is best-effort, never fatal
            pending = True
    return ProjectionResult(
        columns=columns,
        error_details_gz=error_details_gz,
        reprojection_pending=pending,
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def compress_payload(event: dict) -> bytes:
    """Serialize and gzip the whole event for inline retention."""
    return compress_json(event)


def compress_json(value: object) -> bytes:
    """Serialize a JSON-compatible value deterministically and gzip it."""
    raw = json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def decompress_payload(payload: bytes) -> dict:
    """Inverse of :func:`compress_payload` for replay (Section 10.3)."""
    return json.loads(gzip.decompress(payload))


def decompress_json(payload: bytes) -> object:
    """Decompress a payload created by :func:`compress_json`."""
    return json.loads(gzip.decompress(payload))
