"""Pure fixed-grid cadence math for the data-collection scheduler.

Slots land on a fixed grid anchored to the Unix epoch (which is day-aligned):
``slot = floor((now - anchor) / frequency) * frequency + anchor``. A pipeline is
due when it has never dispatched, or its last dispatch predates the current slot.
Because the grid is absolute rather than relative to the previous run, cadence
never drifts when a dispatch is late.

This module is intentionally dependency-free and deterministic so it can be
reused by the (deterministic) orchestrator layer and unit-tested in isolation.
"""
from __future__ import annotations

from datetime import datetime


def slot_start_epoch(now: datetime, anchor_seconds: int, frequency_seconds: int) -> int:
    """Return the epoch-seconds boundary of the slot containing ``now``.

    ``anchor_seconds`` is an offset from midnight UTC (e.g. 23400 == 06:30). The
    Unix epoch is day-aligned, so a seconds-of-day anchor produces a stable daily
    (or sub-daily) grid for any frequency that divides evenly into the period.
    """
    now_ts = int(now.timestamp())
    return ((now_ts - anchor_seconds) // frequency_seconds) * frequency_seconds + anchor_seconds


def is_due(
    now: datetime,
    last_dispatched_at: datetime | None,
    anchor_seconds: int,
    frequency_seconds: int,
) -> bool:
    """Return True when the pipeline should dispatch for the current slot.

    Manual-only pipelines (no positive frequency) are never automatically due.
    """
    if not frequency_seconds or frequency_seconds <= 0:
        return False
    if last_dispatched_at is None:
        return True
    return last_dispatched_at.timestamp() < slot_start_epoch(now, anchor_seconds, frequency_seconds)
