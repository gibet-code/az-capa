"""KQL fragments shared by Azure Resource Graph clients and consumers."""
from __future__ import annotations

from collections.abc import Iterable


def normalize_values(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def in_filter(field: str, values: Iterable[str] | None) -> str | None:
    normalized = normalize_values(values)
    if not normalized:
        return None
    literals = ", ".join(_string_literal(value) for value in normalized)
    return f"| where {field} in~ ({literals})"
