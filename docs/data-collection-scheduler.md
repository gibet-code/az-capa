# Data Collection Scheduler

## Status

Implemented as of 2026-09-02. Replaces per-pipeline timer triggers and
per-pipeline HTTP endpoints with a single scheduling dispatcher, a single
data-collection facade, and app-owned run-state / run-history / config stores.
No pipeline's collection logic (resolve/plan, paging, throttle handling, flush)
changed; only how runs are *triggered*, *gated*, *recorded*, and *surfaced*.

One design point changed during implementation. The spec assumed scope-dependent
pipelines tolerate an absent Subscription snapshot on cold start via a retryable
`app_scope_initializing` (HTTP 503) response, with dispatch ordering as a
courtesy. In practice that left a real cold-start race (daily pipelines that
failed on the first tick parked for 24h). It is resolved by an **opt-in live
scope fallback** in `resolve_app_scope`, which makes collection pipelines
independent of snapshot publish ordering. See **§11** and the updated §3/§9/§10.

## Purpose and Ownership

Data Collection was previously a set of independent pipelines, each owning its
own timer, its own HTTP `refresh`/`flush`/`status` routes, and its own
concurrency gate. Cadence and enablement were compiled into function
registration (CRON or `%ENV%` bindings), so changing them required an
environment-variable edit **and a host restart**.

This design centralizes *when* and *how* collection runs while leaving
*what* each pipeline collects untouched. Ownership boundaries:

- **Dispatcher** owns the decision to start a run (due? enabled? idle?) and the
  single write-point for run state.
- **Facade** owns the one admin-facing HTTP surface for every pipeline action.
- **Pipelines** keep owning their own data, checkpoints, and status shape.
- **Scheduler config** owns per-pipeline cadence and enablement, now runtime
  configurable.

---

## Design

### Overview

```
                 ┌──────────────────────────────────────────────┐
   timer (5 min, │                Dispatcher                    │
   run_on_startup│  for each registered pipeline:               │
   =True) ──────▶│    due? enabled? idle? → start(scheduled)    │
                 └───────────────┬──────────────────────────────┘
                                 │ start(pipeline, action, trigger)
   admin HTTP                    ▼
   POST /api/data-collection/{pipeline}/{action}
                 ┌──────────────────────────────────────────────┐
                 │            DataCollectionDispatcher.start()    │
                 │  validate pipeline ∈ registry                 │
                 │  (scheduled) check enabled                    │
                 │  gate: scope idle? (point read)               │
                 │  client.start_new(<orchestrator>)             │
                 │  write RunState + open RunHistory row         │
                 └───────────────┬──────────────────────────────┘
                                 ▼
                    existing per-pipeline orchestrators
                    (resolve/plan, paging, flush) — unchanged
```

There is exactly **one** timer in the app (the dispatcher) and **one** HTTP
route family (the facade). Both call the same `start()`.

### 1. Pipeline registry

A static, code-defined registry describes every collectable pipeline. It is the
single source of truth the dispatcher and facade iterate.

```json
{
  "id": "cr_usage",
  "label": "CR usage",
  "refreshOrchestrator": "cr_usage_refresh_orchestrator",
  "flushOrchestrator": "cr_usage_flush_orchestrator",
  "scope": "cr_usage",
  "contentionGroup": "cost_management",
  "priority": 50,
  "defaultFrequencySeconds": 86400,
  "defaultAnchorSeconds": 23400,
  "defaultEnabled": true,
  "supportsFlush": true,
  "canDisable": true
}
```

- `id` matches the facade path segment and normalizes to `^[a-z][a-z0-9_]{1,31}$`.
- `scope` identifies the mutual-exclusion domain (usually equal to `id`).
- `contentionGroup` groups pipelines that must not run concurrently against a
  shared external limit. Cost Management pipelines (`vm_usage`, `cr_usage`) share
  one group; catalogues are independent.
- `priority` orders dispatch within a tick (lower runs first). Subscription
  Reference has the lowest number so its snapshot warms before scope-dependent
  pipelines.
- `defaultFrequencySeconds` / `defaultAnchorSeconds` define the schedule as a
  fixed **grid**, not an interval from the last run: slots fall at
  `anchor + k*frequency` (`anchor` is seconds-into-period from the Unix epoch, so
  a daily job with anchor `23400` fires at 06:30 UTC every day). This preserves
  fixed time-of-day and prevents clock drift (see §5).
- `canDisable` (default `true`) governs whether an admin may disable the
  schedule. **Subscription Reference** sets it `false` — it is infrastructural
  (App Scope warm) and must always run; the list surfaces the flag so the UI
  greys out its enable/disable control, and `PATCH config` refuses to disable it.

Registering a pipeline in this list is the only step needed to schedule it;
there is no per-pipeline timer to add.

### 2. Facade HTTP surface

A single route family replaces all per-pipeline routes:

| Method & route | Purpose | Cost |
|----------------|---------|------|
| `GET  /api/data-collection/` | **List/summary** of every pipeline in one call (§2.1) | Cheap: app-owned stores only |
| `GET  /api/data-collection/{pipeline}/status` | **Detailed** single-pipeline status (§2.2) | Heavier: calls `pipeline.status()` + live custom status |
| `GET  /api/data-collection/{pipeline}/run-history` | Run history for a pipeline (§2.3) | Cheap: RunHistory range read + live enrich of running rows |
| `POST /api/data-collection/{pipeline}/refresh` | Manual refresh | — |
| `POST /api/data-collection/{pipeline}/flush` | Manual flush | — |
| `PATCH /api/data-collection/{pipeline}/config` | Edit cadence/enablement (§8) | — |

General rules:

- `require_admin` is applied once, in the facade, for every action.
- `{pipeline}` is validated against the registry; unknown ⇒ HTTP 404.
- `{action}` ∈ `{refresh, flush}` for `POST`; `flush` on a pipeline with
  `supportsFlush=false` ⇒ HTTP 405. `flush` empties the pipeline's data and
  leaves the schedule untouched (see §5).
- All routes are `api/`-prefixed, so the SPA catch-all (`zzz_spa_fallback`,
  sorts last, 404s non-`api/`) does not shadow them. Path params stay under the
  `api/data-collection/` prefix so they do not greedily match other `api/`
  routes.
- The legacy `api/<pipeline>/{refresh,flush,status}` routes and the current
  `api/notifications*` scans are **replaced**. `http_test/*.http` and tests are
  updated to the facade in the same change; no compatibility shims are kept.

#### 2.1 List endpoint — `GET /api/data-collection/`

One fast call that feeds a future table UI. It reads only app-owned stores
(registry + `SchedulerConfig` + `SchedulerRunState`) plus a **bounded** point
`get_status` for the few pipelines currently running (for live progress). It does
**not** call each pipeline's `status()`, so it avoids all the per-pipeline table
reads that make the detailed status heavy.

```json
{
  "pipelines": [
    {
      "id": "cr_usage",
      "name": "CR usage",
      "enabled": true,
      "canDisable": true,
      "frequencySeconds": 86400,
      "lastDispatchedAt": "2026-09-02T06:30:00Z",
      "lastTrigger": "scheduled",
      "lastOutcome": "completed",
      "lastCompletedAt": "2026-09-02T06:41:00Z",
      "running": false,
      "nextDueAt": "2026-09-03T06:30:00Z",
      "progress": null
    }
  ]
}
```

- `nextDueAt = lastDispatchedAt + frequency` when `enabled`; `null` when disabled.
- `progress` is the common progress object (§2.4) for running pipelines, else
  `null`.

#### 2.2 Detailed status — `GET /api/data-collection/{pipeline}/status`

Returns a common envelope wrapping the pipeline's own `status()` body plus the
normalized progress. Used for a per-pipeline detail/drawer view, not the table.

```json
{
  "id": "activity_log",
  "name": "Activity Log",
  "enabled": true,
  "frequencySeconds": 86400,
  "lastDispatchedAt": "...", "lastTrigger": "scheduled",
  "lastOutcome": "completed", "lastCompletedAt": "...",
  "running": true,
  "nextDueAt": "...",
  "progress": { "complete": 0.84, "label": "8/10 subscriptions completed" },
  "detail": { "...pipeline.status() body, unchanged...": null }
}
```

- When the pipeline is running and its orchestration exposes a Durable **custom
  status**, the pipeline's `status()` normalizes it into `progress` (§2.4).
- `detail` is the existing per-pipeline `status()` payload (coverage window,
  `regionsKnown`, `publishedAt`, etc.), passed through untouched.

#### 2.3 Run history — `GET /api/data-collection/{pipeline}/run-history`

Root-only, pre-labeled rows from `SchedulerRunHistory`, newest first, for a
drawer view. Terminal rows use the stored summary; **running** rows are enriched
live with the Durable orchestration's current runtime status and normalized
custom status.

```json
{
  "pipeline": "activity_log",
  "runs": [
    {
      "startedAt": "2026-09-02T07:00:00Z",
      "action": "refresh",
      "trigger": "scheduled",
      "status": "running",
      "instanceId": "...",
      "runtimeStatus": "Running",
      "progress": { "complete": 0.84, "label": "8/10 subscriptions completed" }
    },
    {
      "startedAt": "2026-09-01T07:00:00Z",
      "action": "refresh",
      "trigger": "scheduled",
      "status": "completed",
      "completedAt": "2026-09-01T07:12:00Z",
      "instanceId": "...",
      "summary": { "upserted": 1533, "failed": 0 }
    }
  ]
}
```

Supports a `limit` query param (default e.g. 50) and paging by `RowKey`
continuation.

#### 2.4 Common progress contract

Every pipeline that reports granular progress normalizes it **inside its own
`status()`** into a single shape (a small progress-normalizer function the
pipeline also reuses for the list and run-history enrichment — there is no
separate registry-registered adapter):

```json
{ "complete": 0.84, "label": "8/10 subscriptions completed" }
```

- `complete`: number in `[0, 1]`, or `null` when progress is indeterminate.
- `label`: short human string, or `null`.
- A running pipeline that sets no custom status yields `progress = null`
  (running-but-indeterminate); the `running` flag still distinguishes it from
  idle.

Each pipeline owns the mapping from its raw Durable custom status (e.g. Activity
Log's `{completedSubscriptions,totalSubscriptions,percentCompleted}`) to this
shape, so the frontend never learns pipeline-specific progress formats and no
separate adapter layer is needed.

**UI rendering (no retrofit needed).** Pipelines that expose no custom status
stay `progress = null` and simply render **“In Progress”**. Pipelines that expose
one render **“In Progress (84%)”** from `complete`, with the `label` shown on
hover. There is no need to add custom status to pipelines that lack it.

### 3. Dispatcher

The dispatcher is a **plain timer function**, not an orchestrator, so it has no
determinism constraints. Timer schedule and startup:

- `schedule = %DATA_COLLECTION_DISPATCH_SCHEDULE%` (default `0 */5 * * * *`).
- `run_on_startup = True`. This is the **only** trigger that needs an env var
  and a restart to reconfigure, and it subsumes the current Subscription
  Reference startup warm.

**Cold start.** The dispatcher fires on `run_on_startup` (below) and immediately
runs a normal tick. On a fresh deploy every `SchedulerRunState` is empty, so every
pipeline is **due** and dispatched on that first tick (Subscription Reference
first, by `priority`). Scope-dependent pipelines do **not** wait for the
Subscription snapshot to publish: dispatch starts them in the same tick, and when
the snapshot is still absent they resolve their scope from a live inventory query
(§11). Priority ordering is therefore an optimization, not a correctness
requirement.

**Per-tick responsibilities**, in order:

1. **Reconcile** orphaned run state (§7): for each `SchedulerRunState` with an
   `ActiveInstanceId`, point `get_status`; if terminal, close the RunHistory row
   from the Durable status and clear `ActiveInstanceId`. This is the backstop for
   runs that crashed before self-reporting.
2. **Due-based dispatch**, ordered by `priority` then `contentionGroup`:
   1. Load `SchedulerConfig` (cadence + enabled) and run state per pipeline.
   2. Skip if `enabled = false`.
   3. Skip if not **due** (§5).
   4. Skip if the scope is **busy** (§6).
   5. Skip if the pipeline's `contentionGroup` already started this tick or has
      an active run (serialize contended pipelines; one now, the rest next tick).
   6. Otherwise `start(pipeline, "refresh", trigger="scheduled")`.
3. **Retention prune** (§7), throttled to run at most once per day via a
   `LastPrunedAt` marker, so no separate timer is needed.

**Per-pipeline fault isolation.** The dispatcher is the sole trigger for all
collection, so a bug that aborts a tick would silently stop **every** pipeline.
Each pipeline's reconcile and dispatch step is therefore wrapped so an exception
against one pipeline is logged and swallowed, never aborting the loop or the
remaining steps. Each tick also writes a `LastTickAt` heartbeat marker so a
stalled dispatcher is externally detectable. This path carries dedicated,
exhaustive unit tests (see Required Verification).

The dispatcher never starts `flush`. Flush is admin-only through the facade.

### 4. `DataCollectionDispatcher.start()` — the single start point

Both the timer and the facade call this pure service method:

```
start(pipeline_id, action, trigger_source) -> StartResult
  1. resolve registry entry (raise if unknown)
  2. if action == refresh and trigger == scheduled: assert enabled
  3. gate: if scope busy -> return Busy (facade maps to 409)
  4. instance_id = client.start_new(orchestrator_for(action))
  5. write RunState: { scope, activeInstanceId, activeAction, startedAt, trigger }
  6. open RunHistory row: { pipeline, action, trigger, startedAt, status: running,
                            instanceId }
  7. return Started(instance_id)
```

Because this is the **only** place a run starts, the following become structural
guarantees rather than per-handler discipline:

- Every start updates run state (so cadence and the gate stay correct).
- Manual and scheduled starts are gated and recorded identically.
- `lastDispatchedAt` is written for manual runs too (see cadence).

A **manual** refresh is allowed even when `enabled = false`; enablement only
suppresses **scheduled** dispatch. A manual run on a disabled pipeline is an
explicit admin override.

### 5. Run state and cadence

Two app-owned Table stores decouple scheduling and history from the Durable hub.

**SchedulerRunState** — one row per scope, the authoritative "is it running / when
did it last start".

```
PartitionKey = "state"
RowKey       = <pipeline id>
Fields: Scope, ActiveInstanceId (nullable), ActiveAction (nullable),
        LastDispatchedAt (UTC), LastTrigger (manual|scheduled),
        LastOutcome (completed|partial|failed|null), LastCompletedAt (nullable)
```

**Cadence rule (fixed grid, no drift):** the schedule is a grid of slots at
`anchor + k*frequency` (both from `SchedulerConfig`/registry; `anchor` is
seconds-into-period from the Unix epoch). The current slot is
`slot = floor((now - anchor) / frequency) * frequency + anchor`. A pipeline is
**due** when `LastDispatchedAt < slot` (or `LastDispatchedAt` is absent).

- **Slots are anchored to wall-clock, not to the previous run**, so a daily job
  with `anchor = 23400` fires at ~06:30 UTC every day and never drifts across the
  clock. The ±one-tick granularity (a slot fires at the first ≤5-min tick after
  it opens) is the only imprecision.
- A **failed or delayed** run does not create a retry storm: `LastDispatchedAt`
  advances to the dispatch time, which is ≥ the slot, so the pipeline is not due
  again until the **next** slot. `LastOutcome`/`LastCompletedAt` are recorded for
  observability, not for the due calculation.
- **Manual runs update `LastDispatchedAt`.** A manual refresh satisfies the
  current slot, so the next scheduled run is the next grid slot — no redundant
  scheduled run moments after an admin refresh.
- **Flush** empties the pipeline's data and leaves the schedule untouched; the
  next due slot repopulates naturally. It is allowed whenever the scope is idle
  (the G1 claim + scope gate prevent a flush racing a refresh; a busy scope
  returns 409). To keep data empty (teardown/maintenance) an admin disables the
  pipeline separately via `PATCH config`, then flushes. Manual **refresh** stays
  allowed while disabled as an admin override, since refresh is idempotent.

### 6. Concurrency gate (simplified)

The gate no longer scans the task hub. Because `start()` records
`ActiveInstanceId`, the check is a point read plus at most one targeted status
lookup:

```
scope_busy(pipeline):
  st = SchedulerRunState.get(pipeline)
  if st is None or st.ActiveInstanceId is None: return False
  status = client.get_status(st.ActiveInstanceId)         # O(1) point read
  if status is None or status.runtime_status in TERMINAL:
      clear ActiveInstanceId; return False                 # reconcile stale row
  return True
```

Properties:

- O(1) and **immune to sub-orchestrator count** — no first-page crowding.
- Eliminates the paging-correctness bug class entirely.
- Stale "running" rows (worker crash before the terminal write) self-heal on the
  next check via the point `get_status`.
- One active op per scope remains the invariant, so a single `ActiveInstanceId`
  suffices for both refresh and flush.

### 7. Run history and notifications (simplified)

**SchedulerRunHistory** — one compact, pre-labeled row per dispatched **root**
(no sub-orchestrator noise).

```
PartitionKey = <pipeline id>
RowKey       = <inverted-ticks startedAt>   (newest first)
Fields: Pipeline, Action, Trigger, StartedAt, Status (running|completed|
        partial|failed), CompletedAt (nullable), InstanceId, Summary (JSON:
        counts like upserted/failed/deletedRows)
```

**Closing a row — shared completion activity + reconcile backstop.** A single
`record_run_outcome` activity, called by **every** orchestrator as its final
step, closes the row with the terminal `Status`, `CompletedAt`, and `Summary`,
and clears `SchedulerRunState.ActiveInstanceId`. Orchestrators wrap their body so
the activity runs on **both** the success and the caught-exception path.

That covers graceful success and handled failure, but a run can still die
*before* reaching the activity — an unhandled/replay error, a `Terminate`, or a
worker crash. Durable is the backstop source of truth for those:

- The **dispatcher tick reconciler** (§3, step 1) and the **gate** (§6) both
  point-read `get_status(ActiveInstanceId)`; a terminal runtime status with no
  closed row means the orchestrator never self-reported. The reconciler then
  closes the RunHistory row from the Durable status (`runtimeStatus` →
  `failed`/`completed`, `output` → `Summary` when present) and clears
  `ActiveInstanceId`.
- So a crashed run is closed within one dispatch tick even though the completion
  activity never ran. The completion activity is the fast/rich path; the
  reconciler guarantees eventual closure.

**Notifications** reads directly from this table:

- *Running portion* comes from rows with `Status = running`, reconciled by a
  point `get_status(InstanceId)` (bounded to the few active rows).
- *History portion (7-day)* is a tiny range read of root-only, already-labeled
  rows — no management-endpoint round trips, no continuation-token paging, no
  sub-orchestrator filtering.
- **Purge** (`DELETE /api/notifications`, `DELETE /api/notifications/{id}`)
  deletes app-owned rows. Durable terminal history may still be purged separately
  for storage hygiene, but it is no longer the notifications source.

**Retention.** Rows older than `DATA_COLLECTION_HISTORY_RETENTION_DAYS` (default
30) are pruned by the dispatcher tick (§3, step 4), throttled to at most once per
day — no dedicated retention timer. Running rows are never pruned. Durable
terminal history is not required to be purged in lockstep, but may be on the same
cadence for storage hygiene.

**Consequence:** once both consumers move to run state + point reads, the paged
`query_instances` helper (and its direct `rpc_base_url` / continuation-token
handling) can be **deleted**.

### 8. Scheduler configuration (runtime-editable)

Per-pipeline cadence and enablement move out of function bindings into a runtime
config store so they change **without a restart**.

**SchedulerConfig** — one row per pipeline.

```
PartitionKey = "config"
RowKey       = <pipeline id>
Fields: Enabled (bool), FrequencySeconds (int), AnchorSeconds (int),
        Revision (int), UpdatedAt (UTC), UpdatedBy (JSON identity)
```

- Read on every dispatch tick; missing row falls back to the registry defaults
  (`defaultEnabled`, `defaultFrequencySeconds`, `defaultAnchorSeconds`).
- `PATCH /api/data-collection/{pipeline}/config` validates
  `FrequencySeconds` within `[DATA_COLLECTION_MIN_FREQUENCY_SECONDS` (default
  300, the dispatch-tick floor since finer granularity is unachievable)`,
  `DATA_COLLECTION_MAX_FREQUENCY_SECONDS` (default 2592000)`] bounds, requires
  the current `Revision`, and returns HTTP 409 on a stale update — matching the
  Business Context optimistic-concurrency pattern. Disabling a pipeline whose
  registry `canDisable = false` (Subscription Reference) is refused with HTTP
  409.
- A read-only "Data Collection" Settings card lists each pipeline's enabled
  state, frequency, `lastDispatchedAt`, `nextDueAt`, `lastOutcome`, and running
  status; admins edit cadence/enablement inline.

> **Decision (accepted).** Scheduling cadence and enablement are runtime
> configurable through `SchedulerConfig`; this intentionally scopes the App Scope
> environment-only stance to *scope and data semantics only*, not to scheduling.
> The reversal covers cadence and enablement, nothing else.

### 9. Startup, ordering, and the App Scope warm

- The dispatcher's `run_on_startup = True` fires a normal tick on cold start; no
  dedicated startup path exists. On a fresh deploy `SchedulerRunState` is empty,
  so every pipeline is due and dispatched on that first tick.
- **App Scope warm falls out of the ordinary schedule.** Subscription Reference
  has the lowest `priority`, so the first tick (and every tick where it is due)
  dispatches it before scope-dependent pipelines. Its 15-min cadence plus the
  cold-start due-now behavior keep the snapshot warm without a `run_on_startup`
  flag of its own. Scope-dependent **collection** pipelines do not block on the
  snapshot: they opt into the live scope fallback (§11), so ordering only reduces
  redundant live inventory queries during the cold-start window — it is not a
  correctness requirement.
- **Per-worker startup is harmless.** In a multi-worker host `run_on_startup`
  fires on each worker, but the G1 claim guard ensures only one worker actually
  dispatches any given due pipeline (the others get Busy), and the reconciler and
  prune use conditional writes, so concurrent workers are idempotent. No
  cross-worker lock is needed.
- **Thundering-herd control:** the same `priority` + `contentionGroup`
  serialization used per tick applies to the cold-start burst. Cost Management
  pipelines start one per tick; catalogues proceed in priority order. A per-tick
  dispatch cap (`DATA_COLLECTION_MAX_DISPATCH_PER_TICK`, `0` = unlimited) bounds
  the burst further; the optional random jitter proved unnecessary and is not
  implemented.

### 10. Failure semantics

| Condition | Behavior |
|-----------|----------|
| Unknown pipeline id (facade) | HTTP 404 |
| `flush` on non-flushable pipeline | HTTP 405 |
| Scope busy at `start()` | Facade HTTP 409 `operation_in_progress` with instance id; scheduled tick skips silently |
| Scheduled start on disabled pipeline | Skipped (no run) |
| Manual refresh on disabled pipeline | Allowed (admin override) |
| `PATCH config` disabling a `canDisable=false` pipeline | HTTP 409 |
| Run fails/partial (handled) | Completion activity records `LastOutcome`; `LastDispatchedAt` advanced past the slot, so the next scheduled attempt is the next grid slot |
| Run crashes/terminated before completion activity | RunHistory row stays `running` until the dispatcher reconciler (§3) or gate closes it from the Durable terminal status within one tick |
| Stale config `PATCH` (wrong revision) | HTTP 409 |
| Config row absent | Registry defaults used |
| Scope-dependent refresh, snapshot absent | Resolves scope from a live inventory query (§11); no 503, no park |

---

### 11. App Scope resolution on cold start (implemented deviation)

*Not in the original spec — added during implementation.*

The spec assumed scope-dependent pipelines tolerate an absent Subscription
snapshot through the retryable `app_scope_initializing` (HTTP 503) read contract,
and relied on Subscription Reference's low `priority` to warm the snapshot first.
Implementation exposed a real race: on a clean deploy the first tick dispatches
Subscription Reference **and** the scope-dependent pipelines in the *same* tick
(dispatch does not wait for the snapshot to publish). Their `resolve_plan` ran
before the snapshot existed and raised `AppScopeInitializingError`; because
dispatch stamps `LastDispatchedAt` before the outcome is known, the failed
**daily** pipelines then parked for a full frequency interval (~24h).

**Resolution — opt-in live scope fallback.** `resolve_app_scope` takes a
keyword-only `allow_live_fallback: bool = False`. When the published snapshot is
absent *and* the caller opts in *and* a backend identity is available, it
resolves the boundary from a **live** `get_subscription_inventory` query
(normalized exactly like the snapshot, including `managementGroupAncestors`
filtering in `MODE_MANAGEMENT_GROUPS`) instead of raising. The live path is a
pure read that never publishes a generation, so it cannot mutate the enforced
boundary. A shared `_live_subscription_values` helper backs both this path and
the diagnostic path; it uses a function-local import to avoid the
`reference_data` ↔ `app_scope` import cycle.

- The backend collection resolvers opt in: `activity_log`, `compute_skus`,
  `usage_refresh` (CR + VM), `zone_mapping`, and `LocationReferencePipeline`.
- Interactive/diagnostic callers (`diagnose_app_scope`) keep the strict,
  snapshot-only behavior — the live fallback is collection-only.
- When the snapshot **is** present it always wins; the live query is never issued
  on the warm path.
- Consequence: collection pipelines are **independent** of the Subscription
  Reference publish order. No cross-pipeline dependency gate is introduced;
  `priority` ordering only reduces redundant live inventory queries during the
  cold-start window. The snapshot itself is unchanged and still powers UI
  dropdowns, row enrichment, and diagnostics.

Validated live on a clean-slate deploy: on the single cold-start tick,
Subscription Reference, Location Reference, Compute SKU, and CR usage all
resolved scope and completed with **zero** `AppScopeInitializingError`; Activity
Log resolved live and collected across all in-scope subscriptions.

---

## Impact on existing code

- **Removed:** per-pipeline timer triggers; per-pipeline `refresh`/`flush`/
  `status` HTTP routes; `query_instances` and the running-scan path in
  `find_running_scope_instance`; notifications' twin hub scans.
- **Added:** registry; `DataCollectionDispatcher` service (with reconciler and
  retention prune); facade blueprint (list, detailed status, run-history,
  refresh, flush, config); `SchedulerRunState`, `SchedulerRunHistory`,
  `SchedulerConfig` stores; dispatch timer; a per-pipeline progress normalizer
  inside each `status()`; a shared `record_run_outcome` completion activity
  called by every orchestrator; the keyword-only `allow_live_fallback` parameter
  on `resolve_app_scope` plus its opt-in at the five backend collection
  resolvers and the shared `_live_subscription_values` helper (§11).
- **Unchanged:** every pipeline's resolve/plan, paging, throttle handling, flush,
  checkpoint state, and existing `status()` computation (wrapped by an adapter).
- **Frontend:** `web/app.js` swaps its per-pipeline URLs for
  `/api/data-collection/${kind}/${action}` through the existing
  `collectionSections`/`statuses` registry — a contained change that also
  simplifies the SPA. New Settings card for cadence/enablement.

---

## Phasing

Because the app is not yet running anywhere, cutover is a **clean slate**: before
phase 1, flush all app state and purge the Durable task hub / history. There are
no in-flight orchestrations to reconcile and no legacy rows to migrate, so the
dispatcher starts against empty stores (every pipeline due on the first tick) and
no backfill or double-run window exists.

1. **Registry + dispatcher + facade + run state**, cadence still from registry
   defaults (behavior-preserving): collapses timers into one, adds point-read
   gating. Remove per-pipeline timers/routes **and repoint `web/app.js` to the
   facade in the same change** so the SPA is never left calling deleted routes;
   update `http_test` and tests.
2. **Gate + notifications on run state / RunHistory**, delete `query_instances`.
3. **Contention-group serialization + per-tick dispatch cap** hardening for the
   cold-start burst (random jitter was dropped as unnecessary).
4. **Editable SchedulerConfig + Settings card**, and a `GET /api/data-collection/`
   -backed table UI replacing the per-section status calls.

---

## Open Decisions

All four original decisions are resolved; recorded here for traceability.

1. **Editable cadence store (§8).** RESOLVED — accepted. Cadence and enablement
   are runtime configurable via `SchedulerConfig`; the App Scope environment-only
   stance is scoped to scope/data semantics only.
2. **Flush vs. cadence (§5).** RESOLVED — flush is a single action that empties
   data whenever the scope is idle and leaves the schedule running; the next due
   slot repopulates. To keep data empty, an admin disables the pipeline via
   `PATCH config` first. No `disableAfter` flag and no forced disable dance.
3. **Terminal-write mechanism (§7).** RESOLVED — a shared `record_run_outcome`
   completion activity called by every orchestrator, backstopped by the
   dispatcher reconciler that closes crashed/terminated runs from the Durable
   status.
4. **RunHistory retention (§7).** RESOLVED — time-based, default 30 days
   (`DATA_COLLECTION_HISTORY_RETENTION_DAYS`), pruned by the dispatcher tick
   (throttled to once/day); no separate retention timer.

---

## Required Verification

1. Exactly one timer trigger exists in the app (the dispatcher); no
   per-pipeline timers remain.
2. Exactly one HTTP route family (`api/data-collection/...`) serves refresh,
   flush, status, and config; legacy `api/<pipeline>/*` routes are gone.
3. A manual refresh and a scheduled refresh produce identical gating, run-state,
   and RunHistory writes (same `start()` path).
4. A manual refresh updates `LastDispatchedAt`; the next scheduled run is delayed
   by one frequency interval.
5. A failed run does not advance last-success but still anchors cadence on start
   (no retry storm); `LastOutcome = failed` is recorded.
6. A busy scope returns HTTP 409 on the facade and is skipped by the scheduled
   tick, using a point read (no task-hub scan).
7. During an Activity Log refresh (hundreds of sub-orchestrators), the gate and
   notifications return correct results with no continuation-token paging.
8. A disabled pipeline is skipped by the scheduler but still runs on a manual
   refresh.
9. On a fresh (empty-state) start, the first tick dispatches Subscription
   Reference first and serializes Cost Management pipelines (VM and CR not
   concurrent).
10. `query_instances` is removed; no code reads the Durable management endpoint
    for the gate or notifications except point `get_status(instanceId)`.
11. A stale `PATCH .../config` returns HTTP 409; an absent config row falls back
    to registry defaults.
12. A worker crash mid-run leaves a `running` row that the dispatcher reconciler
    closes from the Durable terminal status within one tick.
13. `GET /api/data-collection/` returns every pipeline in one response, reads
    only app-owned stores plus bounded point reads for running pipelines, and
    does not call any pipeline's `status()`.
14. `GET /api/data-collection/{pipeline}/run-history` returns root-only rows
    newest-first; running rows are enriched with live `runtimeStatus` and
    normalized `progress`.
15. A pipeline's Durable custom status is normalized to `{complete, label}` by
    its adapter and surfaced in detailed status, the list, and run-history;
    running-but-indeterminate yields `progress = null`.
16. `flush` is allowed when the scope is idle, empties data, and leaves the
    schedule running (no `disableAfter`); keeping data empty requires a separate
    `PATCH config` disable. Disabling a `canDisable=false` pipeline via
    `PATCH config` returns HTTP 409.
17. On cold start the dispatcher runs an ordinary tick; with empty state every
    pipeline is due and dispatched (Subscription Reference first); there is no
    separate startup path or `startOnEveryStartup` flag.
18. RunHistory rows older than the retention window are pruned by the dispatcher
    tick (throttled to once/day) with no separate timer; running rows survive.
19. Daily schedules fire at a **fixed time of day** (grid slot `anchor + k*freq`)
    across many days with no cumulative drift; a delayed or manual run advances to
    the next slot, not `run + frequency`.
20. Dispatcher fault isolation: an exception raised while reconciling or
    dispatching one pipeline is logged and does not abort the tick or block any
    other pipeline; every tick writes the `LastTickAt` heartbeat. This path has
    exhaustive unit-test coverage.
21. On a clean cold start, scope-dependent collection pipelines dispatched in the
    same tick as Subscription Reference resolve their scope from the live
    inventory fallback (`allow_live_fallback=True`) and complete with no
    `AppScopeInitializingError`; when the snapshot is already published the live
    query is not issued; `diagnose_app_scope` stays strict (snapshot-only). (§11)

---

## Second-pass review — resolutions

The first-pass gaps are resolved below (decisions of 2026-09-02). One remains an
implementation task rather than an open question.

### G1. `start()` is not atomic — RESOLVED (design task)

The claim sequence is *gate check → `start_new` → write `ActiveInstanceId`*. Two
callers (the dispatcher tick and an admin manual, or two workers) can both pass
the idle check before either writes, then both `start_new` the same scope — a
double run the gate was meant to prevent.

**Resolution:** claim first, start second. Conditionally write `ActiveInstanceId`
to `SchedulerRunState` using an **ETag / insert-if-null** guard *before*
`start_new`; only the winner proceeds to `start_new` and then fills the real
instance id. A loser returns Busy (409) or skips. On `start_new` failure the
winner releases the claim (a transient orphaned claim is self-healed by the
reconciler). This guarantees single-start. Carried into planning as the first
implementation task.

### G2. Multi-worker `run_on_startup` — RESOLVED (accept per-worker)

`run_on_startup` fires on each worker at cold start, so each worker runs a tick.
We **accept** that rather than adding a cross-worker lock: the G1 claim guard
ensures only one worker actually dispatches any given due pipeline (the others get
Busy), and the reconciler and prune use conditional writes, so concurrent workers
are idempotent without extra locking. No added complexity. (The
`startOnEveryStartup` flag and per-process startup detection were dropped
entirely — cold-start warm now falls out of empty-state due-now dispatch.)

### G3. First-deploy / empty-RunState — RESOLVED (in-code defaults + serialization)

An absent `LastDispatchedAt` is treated as due, but the burst is harmless: the
contention-group serialization already lets only one Cost Management pipeline
start per tick, and catalogues hit independent, throttle-tolerant APIs. No
migration-time seeding from dataset state is needed. If extra smoothing is ever
wanted, the registry can carry an in-code `initialStaggerSeconds` default per
pipeline; not required for correctness.

### G4. Infrastructural pipeline cannot be disabled — RESOLVED

Subscription Reference is marked `canDisable = false` (§1). The list response
returns the flag so the UI greys out its enable/disable control, and
`PATCH config` refuses to disable it (HTTP 409). It always runs; no
`startOnEveryStartup`-vs-`enabled` conflict remains.

### G5. Progress coverage is uneven — RESOLVED (no retrofit)

Accepted as-is. A pipeline without a custom status renders **“In Progress”**; one
with a custom status renders **“In Progress (xx%)”** with the label on hover
(§2.4). Usage/catalogue pipelines are **not** retrofitted with a custom status.

### G6. Reconciler double-close — RESOLVED (idempotent, activity wins)

Two things can close a run's RunHistory row: the orchestrator's
`record_run_outcome` activity (the normal path, which knows the rich outcome such
as `partial`) and the dispatcher reconciler (the crash backstop, which only sees
Durable's coarse `runtimeStatus`). The rule that avoids conflict: the reconciler
**only acts when the row is still `running`** and **never overwrites an
already-terminal row** — so the rich activity outcome always wins when it ran.
When the reconciler does close a crashed run, it records `failed` (or reads the
orchestration `output` if present). That is all this gap meant: prefer the
activity's outcome, make the backstop a no-op once the row is closed.

### G7. Flush UX — RESOLVED (single action)

Flush is one action: it empties data whenever the scope is idle and leaves the
schedule running, so the next due slot repopulates. The old disable→flush→re-enable
dance is gone. To keep data empty for teardown, an admin disables the pipeline via
`PATCH config` first — no `disableAfter` variant is kept, for simplicity.

### G8. Cadence granularity shift — RESOLVED (acceptable)

Daily jobs firing within 5 minutes of due (instead of an exact minute) is
acceptable; staggering moves from fixed clock offsets to contention-group
serialization. Nothing downstream depends on the exact wall-clock minute.

### G9. Notifications label source — CONFIRMED non-issue

Double-checked: `web_bp._TASK_LABELS` is used **only** by the notifications
endpoints — `_build_item` (label), the instance filter (`status.name in
_TASK_LABELS`), and the purge guard — all of which this design replaces with
RunHistory reads. `_build_item` already surfaces `custom_status` as `progress`.
No other consumer references it, and sub-orchestrator rows are already excluded
today. So it is a mechanical delete when notifications is repointed at RunHistory
(labels come from the registry `label` + action), not a design decision.
