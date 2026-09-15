"""Static, code-defined registry of collectable data pipelines.

The registry is the single source of truth the dispatcher and facade iterate.
It carries only scheduling and orchestration metadata — no behavior. Progress
normalization lives with each pipeline's own ``status()`` (see the design doc
§2.4), so this module deliberately holds no callables and imports no pipeline
services, keeping it a pure, cheaply-importable data table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# Contention groups (pipelines sharing one must not run concurrently).
_COST_MANAGEMENT = "cost_management"


@dataclass(frozen=True)
class PipelineDescriptor:
    id: str
    label: str
    refresh_orchestrator: str
    flush_orchestrator: str | None
    scope: str
    contention_group: str | None
    priority: int
    default_frequency_seconds: int
    default_anchor_seconds: int
    default_enabled: bool
    supports_flush: bool
    can_disable: bool

    def __post_init__(self) -> None:
        if not _ID_PATTERN.match(self.id):
            raise ValueError(f"Invalid pipeline id {self.id!r}")
        if self.supports_flush and not self.flush_orchestrator:
            raise ValueError(f"Pipeline {self.id!r} supports flush but has no flush orchestrator")


# Priority orders dispatch within a tick (lower runs first). Subscription
# Reference is lowest so its App Scope snapshot warms before scope-dependent
# pipelines. Zone Mapping is manual-only (zero frequency) so it never auto-dispatches.
_DESCRIPTORS: tuple[PipelineDescriptor, ...] = (
    PipelineDescriptor(
        id="subscription_reference",
        label="Subscription reference",
        refresh_orchestrator="subscription_reference_refresh_orchestrator",
        flush_orchestrator=None,
        scope="subscription_reference",
        contention_group=None,
        priority=10,
        default_frequency_seconds=900,
        default_anchor_seconds=0,
        default_enabled=True,
        supports_flush=False,
        can_disable=False,
    ),
    PipelineDescriptor(
        id="location_reference",
        label="Location reference",
        refresh_orchestrator="location_reference_refresh_orchestrator",
        flush_orchestrator=None,
        scope="location_reference",
        contention_group=None,
        priority=20,
        default_frequency_seconds=86400,
        default_anchor_seconds=18900,  # 05:15 UTC
        default_enabled=True,
        supports_flush=False,
        can_disable=True,
    ),
    PipelineDescriptor(
        id="compute_skus",
        label="Compute SKUs",
        refresh_orchestrator="compute_skus_refresh_orchestrator",
        flush_orchestrator="compute_skus_flush_orchestrator",
        scope="compute_skus",
        contention_group=None,
        priority=30,
        default_frequency_seconds=86400,
        default_anchor_seconds=19800,  # 05:30 UTC
        default_enabled=True,
        supports_flush=True,
        can_disable=True,
    ),
    PipelineDescriptor(
        id="subscription_context",
        label="Business context",
        refresh_orchestrator="subscription_context_refresh_orchestrator",
        flush_orchestrator="subscription_context_flush_orchestrator",
        scope="subscription_context",
        contention_group=None,
        priority=40,
        default_frequency_seconds=900,
        default_anchor_seconds=0,
        default_enabled=True,
        supports_flush=True,
        can_disable=True,
    ),
    PipelineDescriptor(
        id="cr_usage",
        label="CR usage",
        refresh_orchestrator="cr_usage_refresh_orchestrator",
        flush_orchestrator="cr_usage_flush_orchestrator",
        scope="cr_usage",
        contention_group=_COST_MANAGEMENT,
        priority=50,
        default_frequency_seconds=86400,
        default_anchor_seconds=23400,  # 06:30 UTC
        default_enabled=True,
        supports_flush=True,
        can_disable=True,
    ),
    PipelineDescriptor(
        id="vm_usage",
        label="VM usage",
        refresh_orchestrator="vm_usage_refresh_orchestrator",
        flush_orchestrator="vm_usage_flush_orchestrator",
        scope="vm_usage",
        contention_group=_COST_MANAGEMENT,
        priority=55,
        default_frequency_seconds=86400,
        default_anchor_seconds=21600,  # 06:00 UTC
        default_enabled=True,
        supports_flush=True,
        can_disable=True,
    ),
    PipelineDescriptor(
        id="activity_log",
        label="Activity log",
        refresh_orchestrator="activity_log_refresh_orchestrator",
        flush_orchestrator="activity_log_flush_orchestrator",
        scope="activity_log",
        contention_group=None,
        priority=60,
        default_frequency_seconds=86400,
        default_anchor_seconds=25200,  # 07:00 UTC
        default_enabled=True,
        supports_flush=True,
        can_disable=True,
    ),
    PipelineDescriptor(
        id="zone_mapping",
        label="Zone mapping",
        refresh_orchestrator="zone_mapping_refresh_orchestrator",
        flush_orchestrator="zone_mapping_flush_orchestrator",
        scope="zone_mapping",
        contention_group=None,
        priority=90,
        default_frequency_seconds=86400,
        default_anchor_seconds=20700,  # 05:45 UTC
        default_enabled=True,
        supports_flush=True,
        can_disable=True,
    ),
)

_BY_ID: dict[str, PipelineDescriptor] = {descriptor.id: descriptor for descriptor in _DESCRIPTORS}


def all_descriptors() -> tuple[PipelineDescriptor, ...]:
    """Every registered pipeline, in dispatch priority order (lowest first)."""
    return tuple(sorted(_DESCRIPTORS, key=lambda descriptor: descriptor.priority))


def try_get(pipeline_id: str) -> PipelineDescriptor | None:
    return _BY_ID.get(pipeline_id)


def get(pipeline_id: str) -> PipelineDescriptor:
    """Return the descriptor for ``pipeline_id`` or raise ``KeyError`` if unknown."""
    try:
        return _BY_ID[pipeline_id]
    except KeyError:
        raise KeyError(f"Unknown pipeline {pipeline_id!r}") from None


def ids() -> tuple[str, ...]:
    return tuple(descriptor.id for descriptor in all_descriptors())
