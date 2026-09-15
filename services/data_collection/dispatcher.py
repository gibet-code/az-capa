"""Data-collection dispatcher service.

The single start point and the per-tick scheduler for every pipeline. It owns no
Azure Functions or Durable SDK types: the Durable client is injected through the
``DurableClient`` protocol so this layer stays deterministic-friendly and unit
testable. Storage access goes through the three scheduler stores; registry
defaults are merged here (storage never imports the registry).

Key invariants:
- **Claim-first atomicity (G1):** ``start`` reserves the scope in ``RunState``
  with an atomic placeholder write *before* ``start_new``, so two concurrent
  callers can never both launch the same scope.
- **Self-healing gate:** ``scope_busy`` point-reads Durable status for a real
  active instance and clears stale "running" rows left by a crashed worker.
- **Fault isolation:** each pipeline's reconcile/dispatch step is wrapped so one
  failure never aborts the tick; a ``LastTickAt`` heartbeat makes stalls visible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from core import settings
from storage.scheduler_config_store import SchedulerConfigConflictError, SchedulerConfigStore
from storage.scheduler_history_store import SchedulerHistoryStore
from storage.scheduler_state_store import SchedulerStateStore

from . import cadence, registry
from .registry import PipelineDescriptor

logger = logging.getLogger(__name__)

# Durable runtime statuses that mean the instance is finished.
_TERMINAL = frozenset({"Completed", "Failed", "Terminated", "Canceled"})
_PLACEHOLDER_PREFIX = "pending:"
_RUNNING = "running"
# Domain run outcome → Durable-style status the SPA badge classifier understands.
_DISPLAY_RUNTIME = {"completed": "Completed", "failed": "Failed", "partial": "Completed"}

REFRESH = "refresh"
FLUSH = "flush"
SCHEDULED = "scheduled"
MANUAL = "manual"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _clamp01(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0.0, min(1.0, float(value)))


def normalize_progress(custom_status: Any) -> dict | None:
    """Map a pipeline's raw Durable custom status to the common ``{complete, label}`` shape (§2.4).

    Returns ``None`` for an absent or indeterminate status. Recognizes the
    already-normalized shape and the per-subscription shape that
    activity_log/zone_mapping/subscription_context emit
    (``{completedSubscriptions, totalSubscriptions, percentCompleted}``); every
    other shape is treated as running-but-indeterminate.
    """
    if not isinstance(custom_status, dict):
        return None
    if "complete" in custom_status or "label" in custom_status:
        label = custom_status.get("label")
        return {
            "complete": _clamp01(custom_status.get("complete")),
            "label": label if isinstance(label, str) else None,
        }
    if "percentCompleted" in custom_status:
        percent = custom_status.get("percentCompleted")
        complete = _clamp01(percent / 100) if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None
        completed = custom_status.get("completedSubscriptions")
        total = custom_status.get("totalSubscriptions")
        label = (
            f"{completed}/{total} subscriptions completed"
            if isinstance(completed, int) and isinstance(total, int)
            else None
        )
        return {"complete": complete, "label": label}
    return None


@dataclass(frozen=True)
class RunStatus:
    """Normalized view of a Durable orchestration status (adapter-provided)."""

    runtime_status: str
    custom_status: Any = None
    output: Any = None

    @property
    def is_terminal(self) -> bool:
        return self.runtime_status in _TERMINAL


class DurableClient(Protocol):
    """The slice of the Durable client the dispatcher needs, injected by callers."""

    async def start_new(self, orchestrator: str, client_input: dict | None = None) -> str: ...

    async def get_status(self, instance_id: str) -> RunStatus | None: ...


@dataclass(frozen=True)
class EffectiveConfig:
    enabled: bool
    frequency_seconds: int
    anchor_seconds: int
    revision: int


@dataclass(frozen=True)
class StartResult:
    status: str  # "started" | "busy" | "disabled"
    instance_id: str | None = None


class ConfigUpdateError(Exception):
    """A scheduler config edit was rejected; ``code`` maps to an HTTP status in the facade."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _outcome_from_status(status: RunStatus | None) -> str:
    if status is not None and status.runtime_status == "Completed":
        return "completed"
    return "failed"


class DataCollectionDispatcher:
    def __init__(
        self,
        *,
        state_store: SchedulerStateStore | None = None,
        history_store: SchedulerHistoryStore | None = None,
        config_store: SchedulerConfigStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state_store or SchedulerStateStore()
        self._history = history_store or SchedulerHistoryStore()
        self._config = config_store or SchedulerConfigStore()
        self._now = clock or (lambda: datetime.now(timezone.utc))

    # ── configuration merge (registry defaults ← SchedulerConfig row) ──────────
    def effective_config(self, descriptor: PipelineDescriptor) -> EffectiveConfig:
        raw = self._config.get(descriptor.id)
        if raw is None:
            return EffectiveConfig(
                enabled=descriptor.default_enabled,
                frequency_seconds=descriptor.default_frequency_seconds,
                anchor_seconds=descriptor.default_anchor_seconds,
                revision=0,
            )
        return EffectiveConfig(
            enabled=raw["enabled"] if raw["enabled"] is not None else descriptor.default_enabled,
            frequency_seconds=(
                raw["frequencySeconds"]
                if raw["frequencySeconds"] is not None
                else descriptor.default_frequency_seconds
            ),
            anchor_seconds=(
                raw["anchorSeconds"]
                if raw["anchorSeconds"] is not None
                else descriptor.default_anchor_seconds
            ),
            revision=raw["revision"] or 0,
        )

    # ── list endpoint (cheap: app-owned stores + bounded live status) ──────────
    async def list_pipelines(self, client: DurableClient) -> list[dict]:
        """One-call summary of every pipeline for the table UI (§2.1).

        Reads only app-owned stores plus a bounded point ``get_status`` for the
        pipelines that hold an active instance, so it never calls per-pipeline
        ``status()``.
        """
        pipelines: list[dict] = []
        for descriptor in registry.all_descriptors():
            config = self.effective_config(descriptor)
            state = self._state.get(descriptor.id) or {}
            active = state.get("activeInstanceId")
            running = False
            progress = None
            if active and not active.startswith(_PLACEHOLDER_PREFIX):
                status = await client.get_status(active)
                if status is not None and not status.is_terminal:
                    running = True
                    progress = normalize_progress(status.custom_status)
                else:
                    self._reconcile_terminal(descriptor.id, active, status)
                    state = self._state.get(descriptor.id) or {}
            last_dispatched = state.get("lastDispatchedAt")
            next_due = None
            if config.enabled and last_dispatched and config.frequency_seconds > 0:
                next_due = last_dispatched + timedelta(seconds=config.frequency_seconds)
            pipelines.append(
                {
                    "id": descriptor.id,
                    "name": descriptor.label,
                    "enabled": config.enabled,
                    "canDisable": descriptor.can_disable,
                    "supportsFlush": descriptor.supports_flush,
                    "frequencySeconds": config.frequency_seconds,
                    "anchorSeconds": config.anchor_seconds,
                    "revision": config.revision,
                    "lastDispatchedAt": _iso(last_dispatched),
                    "lastTrigger": state.get("lastTrigger"),
                    "lastOutcome": state.get("lastOutcome"),
                    "lastCompletedAt": _iso(state.get("lastCompletedAt")),
                    "running": running,
                    "nextDueAt": _iso(next_due),
                    "progress": progress,
                }
            )
        return pipelines

    # ── detailed status envelope (§2.2) ────────────────────────────────────────
    async def pipeline_status(
        self, client: DurableClient, pipeline_id: str, detail: dict | None
    ) -> dict:
        """Wrap a pipeline's own ``status()`` body in the common scheduler envelope.

        Adds app-owned scheduling/run-state fields plus the normalized live
        ``progress``; ``detail`` is the pipeline's freshness body, passed through
        untouched.
        """
        descriptor = registry.get(pipeline_id)  # KeyError → facade maps to 404
        config = self.effective_config(descriptor)
        state = self._state.get(pipeline_id) or {}
        active = state.get("activeInstanceId")
        running = False
        progress = None
        if active and not active.startswith(_PLACEHOLDER_PREFIX):
            status = await client.get_status(active)
            if status is not None and not status.is_terminal:
                running = True
                progress = normalize_progress(status.custom_status)
            else:
                self._reconcile_terminal(pipeline_id, active, status)
                state = self._state.get(pipeline_id) or {}
        last_dispatched = state.get("lastDispatchedAt")
        next_due = None
        if config.enabled and last_dispatched and config.frequency_seconds > 0:
            next_due = last_dispatched + timedelta(seconds=config.frequency_seconds)
        return {
            "id": descriptor.id,
            "name": descriptor.label,
            "enabled": config.enabled,
            "canDisable": descriptor.can_disable,
            "supportsFlush": descriptor.supports_flush,
            "frequencySeconds": config.frequency_seconds,
            "anchorSeconds": config.anchor_seconds,
            "revision": config.revision,
            "lastDispatchedAt": _iso(last_dispatched),
            "lastTrigger": state.get("lastTrigger"),
            "lastOutcome": state.get("lastOutcome"),
            "lastCompletedAt": _iso(state.get("lastCompletedAt")),
            "running": running,
            "nextDueAt": _iso(next_due),
            "progress": progress,
            "detail": detail,
        }

    # ── run history drawer (§2.3) ──────────────────────────────────────────────
    async def run_history(
        self, client: DurableClient, pipeline_id: str, *, limit: int = 50, after: str | None = None
    ) -> dict:
        """Root-only run rows for a pipeline, newest first, with live enrichment.

        Terminal rows use the stored summary; running rows are point-confirmed
        against Durable so live progress surfaces and a crashed run self-heals to
        its terminal outcome. ``nextContinuation`` is the last RowKey when a full
        page was returned, else ``None``.
        """
        rows = self._history.list_by_pipeline(pipeline_id, limit=limit, after=after)
        runs: list[dict] = []
        for row in rows:
            run = {
                "startedAt": _iso(row.get("startedAt")),
                "rowKey": row.get("rowKey"),
                "action": row.get("action"),
                "trigger": row.get("trigger"),
                "status": row.get("status"),
                "instanceId": row.get("instanceId"),
            }
            if row.get("status") == _RUNNING:
                instance_id = row.get("instanceId")
                live = await client.get_status(instance_id) if instance_id else None
                if live is not None and not live.is_terminal:
                    run["runtimeStatus"] = live.runtime_status
                    run["progress"] = normalize_progress(live.custom_status)
                else:
                    self._reconcile_terminal(pipeline_id, instance_id, live)
                    run["status"] = _outcome_from_status(live)
                    run["runtimeStatus"] = live.runtime_status if live is not None else "Terminated"
                    run["completedAt"] = _iso(self._now())
                    run["summary"] = live.output if isinstance(live, RunStatus) and isinstance(live.output, dict) else None
            else:
                run["completedAt"] = _iso(row.get("completedAt"))
                run["summary"] = row.get("summary")
            runs.append(run)
        next_continuation = rows[-1]["rowKey"] if len(rows) >= limit and rows else None
        return {"pipeline": pipeline_id, "runs": runs, "nextContinuation": next_continuation}

    # ── config edit (validate → optimistic-concurrency patch) ──────────────────
    def update_config(
        self,
        pipeline_id: str,
        *,
        enabled: bool,
        frequency_seconds: int,
        anchor_seconds: int,
        expected_revision: int | None,
        updated_by: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and persist a cadence/enablement edit (§8).

        Raises ``ConfigUpdateError`` (code → HTTP) on an unknown pipeline, a
        disable of a non-disableable pipeline, an out-of-range cadence, or a
        stale revision.
        """
        descriptor = registry.try_get(pipeline_id)
        if descriptor is None:
            raise ConfigUpdateError("unknown_pipeline", f"Unknown pipeline {pipeline_id!r}.")
        if not enabled and not descriptor.can_disable:
            raise ConfigUpdateError("cannot_disable", f"{descriptor.label} cannot be disabled.")
        min_freq = settings.data_collection_min_frequency_seconds()
        max_freq = settings.data_collection_max_frequency_seconds()
        # 0 means "manual only" (never auto-dispatch); otherwise enforce the bounds.
        if frequency_seconds != 0 and not (min_freq <= frequency_seconds <= max_freq):
            raise ConfigUpdateError(
                "invalid_frequency",
                f"Frequency must be 0 (manual) or between {min_freq} and {max_freq} seconds.",
            )
        if anchor_seconds < 0 or anchor_seconds >= 86400:
            raise ConfigUpdateError(
                "invalid_anchor", "Anchor must be between 0 and 86399 seconds into the day."
            )
        try:
            return self._config.patch(
                pipeline_id,
                enabled=enabled,
                frequency_seconds=frequency_seconds,
                anchor_seconds=anchor_seconds,
                expected_revision=expected_revision,
                updated_by=updated_by,
                updated_at=self._now(),
            )
        except SchedulerConfigConflictError as exc:
            raise ConfigUpdateError("stale_revision", str(exc)) from exc

    def reset_config(self) -> int:
        """Clear every cadence/enablement override so all pipelines revert to registry defaults."""
        return self._config.clear()

    # ── the single start point ────────────────────────────────────────────────
    async def start(
        self,
        client: DurableClient,
        pipeline_id: str,
        action: str,
        trigger: str,
    ) -> StartResult:
        descriptor = registry.get(pipeline_id)  # KeyError → facade maps to 404
        if action == FLUSH and not descriptor.supports_flush:
            raise ValueError(f"Pipeline {pipeline_id!r} does not support flush")
        orchestrator = (
            descriptor.refresh_orchestrator if action == REFRESH else descriptor.flush_orchestrator
        )
        if orchestrator is None:
            raise ValueError(f"Pipeline {pipeline_id!r} has no {action} orchestrator")

        # Scheduled refresh honors enablement; manual refresh is an admin override.
        if action == REFRESH and trigger == SCHEDULED and not self.effective_config(descriptor).enabled:
            return StartResult("disabled")

        # Gate (self-heals a stale row), then atomically claim the scope.
        if await self.scope_busy(client, pipeline_id):
            return StartResult("busy")
        now = self._now()
        if not self._state.try_claim(pipeline_id, descriptor.scope, action, trigger, now):
            return StartResult("busy")

        try:
            instance_id = await client.start_new(orchestrator, {})
        except Exception:
            # Release the placeholder so a failed launch does not wedge the scope.
            self._state.release_claim(pipeline_id)
            raise

        self._state.set_active_instance(pipeline_id, instance_id)
        self._history.open_row(pipeline_id, action, trigger, now, instance_id)
        return StartResult("started", instance_id)

    # ── concurrency gate ──────────────────────────────────────────────────────
    async def scope_busy(self, client: DurableClient, pipeline_id: str) -> bool:
        state = self._state.get(pipeline_id)
        if state is None:
            return False
        active = state.get("activeInstanceId")
        if not active:
            return False
        # A placeholder means another caller's claim is mid-flight — treat as busy
        # without a status lookup so we never race an in-flight start.
        if active.startswith(_PLACEHOLDER_PREFIX):
            return True
        status = await client.get_status(active)
        if status is None or status.is_terminal:
            self._reconcile_terminal(pipeline_id, active, status)
            return False
        return True

    async def active_operation(
        self, client: DurableClient, pipeline_id: str
    ) -> tuple[bool, str | None, str | None]:
        """Return (in_progress, action, instance_id) for the facade status view.

        Self-heals a stale row via ``scope_busy`` and hides the in-flight claim
        placeholder from callers.
        """
        if not await self.scope_busy(client, pipeline_id):
            return (False, None, None)
        state = self._state.get(pipeline_id)
        active = state.get("activeInstanceId") if state else None
        action = state.get("activeAction") if state else None
        if active and active.startswith(_PLACEHOLDER_PREFIX):
            active = None
        return (True, action, active)

    # ── notifications feed (RunHistory-backed) ────────────────────────────────
    async def notifications(self, client: DurableClient, since: datetime) -> list[dict]:
        """Recent runs (since) plus any still-running run regardless of age.

        Running rows are confirmed with a point ``get_status`` so live progress
        surfaces and a crashed run self-heals to terminal instead of hanging.
        """
        now = self._now()
        items: list[dict] = []
        seen: set[str] = set()
        for row in self._history.list_running():
            instance_id = row.get("instanceId")
            status = await client.get_status(instance_id) if instance_id else None
            if status is not None and not status.is_terminal:
                items.append(self._notification_item(row, status.runtime_status, True, status.custom_status, None, now))
            else:
                self._reconcile_terminal(row["pipeline"], instance_id, status)
                runtime = status.runtime_status if status is not None else "Terminated"
                items.append(self._notification_item(row, runtime, False, None, now, now))
            if instance_id:
                seen.add(instance_id)
        for row in self._history.list_recent(since):
            if row.get("status") == _RUNNING:
                continue
            instance_id = row.get("instanceId")
            if instance_id and instance_id in seen:
                continue
            runtime = _DISPLAY_RUNTIME.get(row.get("status"), "Completed")
            items.append(self._notification_item(row, runtime, False, None, row.get("completedAt"), now))
        items.sort(key=lambda item: item["createdTime"] or "", reverse=True)
        return items

    def _notification_item(
        self,
        row: dict,
        runtime_status: str | None,
        is_running: bool,
        progress: Any,
        completed_at: datetime | None,
        now: datetime,
    ) -> dict:
        descriptor = registry.try_get(row["pipeline"])
        section = descriptor.label if descriptor else row["pipeline"]
        action = row.get("action") or ""
        operation = action.capitalize()
        started_at = row.get("startedAt")
        started_since = int((now - started_at).total_seconds()) if is_running and started_at else None
        duration = None
        if not is_running and started_at and completed_at:
            duration = int((completed_at - started_at).total_seconds())
        return {
            "instanceId": row.get("instanceId"),
            "kind": row["pipeline"],
            "section": section,
            "operation": operation,
            "label": f"{section} — {operation}" if operation else section,
            "runtimeStatus": runtime_status,
            "isRunning": is_running,
            "createdTime": _iso(started_at),
            "lastUpdatedTime": _iso(completed_at) if not is_running else None,
            "startedSinceSeconds": started_since,
            "durationSeconds": duration,
            "completedOn": _iso(completed_at) if not is_running else None,
            "progress": progress if isinstance(progress, dict) else None,
        }

    async def delete_run(self, client: DurableClient, instance_id: str) -> str:
        """Discard one run's history rows. Refuses while the run is still live."""
        rows = self._history.rows_for_instance(instance_id)
        if not rows:
            return "not_found"
        if any(row.get("status") == _RUNNING for row in rows):
            status = await client.get_status(instance_id)
            if status is not None and not status.is_terminal:
                return "running"
            for row in rows:
                if row.get("status") == _RUNNING:
                    self._reconcile_terminal(row["pipeline"], instance_id, status)
        self._history.delete_by_instance(instance_id)
        return "deleted"

    def clear_terminal_runs(self) -> int:
        """Discard every terminal run's history rows (running rows survive)."""
        return self._history.delete_terminal()

    # ── per-tick scheduler ────────────────────────────────────────────────────
    async def tick(self, client: DurableClient) -> None:
        now = self._now()

        # 1. Reconcile orphaned run state (backstop for crashed runs).
        for state in self._state.list():
            pipeline_id = state["pipeline"]
            try:
                await self._reconcile_state(client, state)
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception("scheduler reconcile failed for %s", pipeline_id)

        # 2. Due-based dispatch, priority then contention group.
        started_groups: set[str] = set()
        cap = settings.data_collection_max_dispatch_per_tick()
        started_count = 0
        for descriptor in registry.all_descriptors():
            if cap > 0 and started_count >= cap:
                break  # bound the burst; remaining due pipelines start next tick
            try:
                if await self._dispatch_one(client, descriptor, now, started_groups):
                    started_count += 1
                    if descriptor.contention_group is not None:
                        started_groups.add(descriptor.contention_group)
            except Exception:  # isolate one pipeline's failure from the rest
                logger.exception("scheduler dispatch failed for %s", descriptor.id)

        # 3. Retention prune (throttled to once per day) + heartbeat.
        pruned_at = None
        try:
            pruned_at = self._maybe_prune(now)
        except Exception:  # pragma: no cover - defensive isolation
            logger.exception("scheduler prune failed")
        self._state.record_tick(now, pruned_at=pruned_at)

    async def _dispatch_one(
        self,
        client: DurableClient,
        descriptor: PipelineDescriptor,
        now: datetime,
        started_groups: set[str],
    ) -> bool:
        config = self.effective_config(descriptor)
        if not config.enabled:
            return False
        state = self._state.get(descriptor.id)
        last_dispatched = state.get("lastDispatchedAt") if state else None
        if not cadence.is_due(now, last_dispatched, config.anchor_seconds, config.frequency_seconds):
            return False
        if await self.scope_busy(client, descriptor.id):
            return False
        if await self._contention_blocked(client, descriptor, started_groups):
            return False
        result = await self.start(client, descriptor.id, REFRESH, SCHEDULED)
        return result.status == "started"

    async def _contention_blocked(
        self,
        client: DurableClient,
        descriptor: PipelineDescriptor,
        started_groups: set[str],
    ) -> bool:
        group = descriptor.contention_group
        if group is None:
            return False
        if group in started_groups:
            return True
        for other in registry.all_descriptors():
            if other.id != descriptor.id and other.contention_group == group:
                if await self.scope_busy(client, other.id):
                    return True
        return False

    # ── reconcile / completion ────────────────────────────────────────────────
    async def _reconcile_state(self, client: DurableClient, state: dict) -> None:
        active = state.get("activeInstanceId")
        # Skip placeholders: an in-flight claim must not be cleared by the tick.
        if not active or active.startswith(_PLACEHOLDER_PREFIX):
            return
        status = await client.get_status(active)
        if status is None or status.is_terminal:
            self._reconcile_terminal(state["pipeline"], active, status)

    def _reconcile_terminal(
        self,
        pipeline_id: str,
        instance_id: str,
        status: RunStatus | None,
    ) -> None:
        outcome = _outcome_from_status(status)
        summary = status.output if isinstance(status, RunStatus) and status.output else None
        now = self._now()
        self._history.close_by_instance(pipeline_id, instance_id, outcome, now, summary)
        self._state.close_run(pipeline_id, outcome, now)

    def record_outcome(
        self,
        pipeline_id: str,
        instance_id: str,
        outcome: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """Close the run from the completion activity (fast, rich path)."""
        now = self._now()
        self._history.close_by_instance(pipeline_id, instance_id, outcome, now, summary)
        self._state.close_run(pipeline_id, outcome, now)

    # ── retention ─────────────────────────────────────────────────────────────
    def _maybe_prune(self, now: datetime) -> datetime | None:
        marker = self._state.get_marker()
        last_pruned = marker.get("lastPrunedAt")
        if last_pruned is not None and (now - last_pruned) < timedelta(days=1):
            return None
        retention_days = settings.data_collection_history_retention_days()
        self._history.prune(now - timedelta(days=retention_days))
        return now
