"""Generic daily-usage refresh pipeline (scope resolution, planning, paging).

The VM usage and capacity-reservation pipelines are identical except for the
Cost Management *filter* they query with and the *tables* they persist to. This
module captures everything they share; a concrete pipeline is just a
:class:`RefreshPipeline` constructed with a filter-builder callable and a
:class:`~storage.usage_store.UsageStore`.

The durable triggers stay thin: they drive the orchestrator, which calls the
pipeline's activity bodies (:meth:`RefreshPipeline.resolve_plan`,
:meth:`~RefreshPipeline.fetch_and_store_page`,
:meth:`~RefreshPipeline.delete_subscriptions`, :meth:`~RefreshPipeline.write_state`).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from core import settings
from core.app_scope import (
    MODE_ALL,
    MODE_MANAGEMENT_GROUPS,
    MODE_SUBSCRIPTIONS,
    configured_scope,
)
from clients.cost_management import (
    build_query_body,
    build_scope,
    page_next_link,
    post_query_once,
    query_url,
    rows_from_payload,
)
from core.identity import for_backend_operation
from core.settings import METHOD_PER_BILLING, METHOD_PER_SUB
from services.app_scope import resolve_app_scope
from storage.usage_store import UsageStore

FilterBuilder = Callable[..., "dict | None"]


# ── Query streams (one page loop per stream) ──────────────────────────────────
# MCA: a single AmortizedCost query yields correct hours AND cost. EA:
# AmortizedCost.UsageQuantity double-counts savings-plan hours, so hours must
# come from a separate ActualCost query — two streams, MERGE-upserted per page so
# neither clobbers the other's field.
def refresh_streams(agreement: str) -> list[dict]:
    if agreement == settings.AGREEMENT_MCA:
        return [{"exportType": "AmortizedCost", "aggs": ["UsageQuantity", "Cost"], "kind": "both"}]
    return [
        {"exportType": "ActualCost", "aggs": ["UsageQuantity"], "kind": "hours"},
        {"exportType": "AmortizedCost", "aggs": ["Cost"], "kind": "cost"},
    ]


# ── Date helpers ──────────────────────────────────────────────────────────────
def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _end_date() -> date:
    """Most recent finalized day = today − finalization lag."""
    return _today_utc() - timedelta(days=settings.cost_finalization_lag_days())


def _historical_start(end: date) -> date:
    return end - timedelta(days=settings.usage_historical_days() - 1)


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _fmt(d: date) -> str:
    return d.isoformat()


# ── Record mapping ────────────────────────────────────────────────────────────
def _fmt_usage_date(value) -> str | None:
    if value is None:
        return None
    text = str(int(value)) if isinstance(value, (int, float)) else str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _subscription_from_resource_id(resource_id: str) -> str | None:
    parts = resource_id.lower().split("/")
    if "subscriptions" in parts:
        idx = parts.index("subscriptions")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def map_page_records(rows: list[dict], kind: str) -> list[dict]:
    """Map one query page into *partial* records carrying only the stream's metric.

    ``kind`` is ``"hours"``, ``"cost"`` or ``"both"``. Partial records (no default
    0.0 for the absent metric) are essential for the EA two-stream MERGE upsert:
    the cost stream must not zero the hours the first stream wrote, and vice
    versa. Grouping is per (ResourceId, day), so a page has no intra-page dups.
    """
    records: list[dict] = []
    for row in rows:
        resource_id = row.get("ResourceId")
        usage_date = _fmt_usage_date(row.get("UsageDate"))
        if not resource_id or not usage_date:
            continue
        rec = {
            "resourceId": resource_id,
            "subscriptionId": _subscription_from_resource_id(resource_id),
            "date": usage_date,
            "currency": row.get("Currency"),
        }
        if kind in ("both", "hours"):
            rec["uptimeHours"] = float(row.get("UsageQuantity") or 0.0)
        if kind in ("both", "cost"):
            rec["cost"] = float(row.get("Cost") or 0.0)
        records.append(rec)
    return records


# ── Scope resolution ──────────────────────────────────────────────────────────
def _resolve_scope(identity) -> dict:
    """Resolve the configured scope into a canonical descriptor.

    ``subs`` is the sorted, lowercased resolved subscription set, or ``None`` for
    the per-billing *unfiltered whole profile* (the ALL scope we never enumerate).
    ``configSelectors`` keeps the raw ``SCOPE_*`` inputs for the revert hint in
    fail messages.
    """
    method = settings.cost_management_method()
    configured = configured_scope()
    sub_ids = configured.selectors if configured.mode == MODE_SUBSCRIPTIONS else ()
    mg_ids = configured.selectors if configured.mode == MODE_MANAGEMENT_GROUPS else ()
    config = {
        "subscriptions": list(sub_ids),
        "managementGroups": list(mg_ids),
    }

    billing_account = None
    billing_profile = None
    if method == METHOD_PER_BILLING:
        billing_account = settings.cost_management_billing_account_id()
        billing_profile = settings.cost_management_billing_profile_id()
        if not billing_account:
            raise ValueError("per-billing requires COST_MANAGEMENT_BILLING_ACCOUNT_ID.")
    if method == METHOD_PER_BILLING and configured.mode == MODE_ALL:
        subs = None  # ALL — whole unfiltered profile
    else:
        subs = list(
            resolve_app_scope(identity, configured, allow_live_fallback=True).subscription_ids
        )

    subs_norm = None if subs is None else sorted({s.lower() for s in subs})
    if method == METHOD_PER_SUB and not subs_norm:
        raise ValueError("per-sub scope resolved to no accessible subscriptions.")
    return {
        "method": method,
        "billingAccount": billing_account,
        "billingProfile": billing_profile,
        "subs": subs_norm,
        "configSelectors": config,
    }


def _structural(scope: dict) -> tuple:
    """Fields whose change forces a full rebuild (an explicit flush)."""
    return (scope["method"], scope.get("billingAccount"), scope.get("billingProfile"))


def _describe_scope(method, billing_profile, subs, config) -> str:
    base = f"method=per-billing, billingProfile={billing_profile}" if method == METHOD_PER_BILLING else "method=per-sub"
    if subs is None:
        scope_desc = "whole profile (unfiltered)"
    else:
        cfg = config or {}
        if cfg.get("managementGroups"):
            scope_desc = f"management group(s) {cfg['managementGroups']} ({len(subs)} subscriptions)"
        elif cfg.get("subscriptions"):
            scope_desc = f"{len(subs)} configured subscription(s)"
        else:
            scope_desc = f"{len(subs)} subscription(s)"
    return f"{base}, scope={scope_desc}"


# Reasons a refresh must be refused (needs a manual flush + rebuild).
REFUSE_STRUCTURAL = "structural configuration change (method, or billing account/profile)"
REFUSE_BOUNDARY = "switching between the whole unfiltered profile and a subscription subset"


def _scope_conflict(state: dict, scope: dict) -> str | None:
    """The reason a refresh would be refused (requires a flush), or None if compatible.

    Mirrors the two refusal branches in :meth:`RefreshPipeline.resolve_plan`: a
    structural change, or crossing the ALL/subset boundary.
    """
    if _structural(state) != _structural(scope):
        return REFUSE_STRUCTURAL
    if (state["subs"] is None) != (scope["subs"] is None):
        return REFUSE_BOUNDARY
    return None


def _scope_change_message(state: dict, scope: dict, reason: str) -> str:
    prev = _describe_scope(state["method"], state.get("billingProfile"), state["subs"],
                           state.get("configSelectors"))
    now = _describe_scope(scope["method"], scope.get("billingProfile"), scope["subs"],
                          scope.get("configSelectors"))
    return (
        f"this scope change requires a full rebuild (reason: {reason}). "
        f"Previously loaded: {prev}, covered {state['coverageStart']} -> {state['coverageEnd']}. "
        f"Now configured: {now}. "
        "If unintended, revert your SCOPE_* settings. Otherwise flush then refresh."
    )


def _new_state(scope: dict, coverage_start: date, coverage_end: date) -> dict:
    return {
        "method": scope["method"],
        "billingAccount": scope["billingAccount"],
        "billingProfile": scope["billingProfile"],
        "subs": scope["subs"],
        "configSelectors": scope["configSelectors"],
        "coverageStart": _fmt(coverage_start),
        "coverageEnd": _fmt(coverage_end),
    }


class RefreshPipeline:
    """One daily-usage pipeline: a Cost Management filter + a backing store.

    The only per-pipeline differences are ``build_filter`` (the meter/resource
    filter) and ``store`` (the tables). Everything else — scope resolution,
    planning, reconcile, paging and flush — is shared.
    """

    def __init__(self, build_filter: FilterBuilder, store: UsageStore) -> None:
        self._build_filter = build_filter
        self._store = store

    # ── Work-unit + plan builders ─────────────────────────────────────────────
    def _work_units(self, scope: dict, subs, start: date, end: date) -> list[dict]:
        """Expand a (subscriptions, date-range) load into page-loop work units.

        ``subs`` is a list of subscription IDs, or ``None`` for the per-billing ALL
        scope. per-sub yields one unit per subscription (each its own scope); per-
        billing yields one unit with a ``SubscriptionId`` filter (or none for ALL).
        """
        if start > end:
            return []
        s, e = _fmt(start), _fmt(end)
        if scope["method"] == METHOD_PER_SUB:
            return [
                {"scope": build_scope(METHOD_PER_SUB, subscription_id=sub),
                 "filter": self._build_filter(),
                 "start": s, "end": e}
                for sub in subs
            ]
        billing_scope = build_scope(METHOD_PER_BILLING, billing_account_id=scope["billingAccount"],
                                    billing_profile_id=scope["billingProfile"])
        sub_filter = None if subs is None else list(subs)
        return [{
            "scope": billing_scope,
            "filter": self._build_filter(subscription_ids=sub_filter),
            "start": s, "end": e,
        }]

    def _plan(self, scope, streams, agreement, loads, deletions, new_state, reason) -> dict:
        work_units: list[dict] = []
        for subs, start, end in loads:
            work_units.extend(self._work_units(scope, subs, start, end))
        return {
            "action": "load",
            "reason": reason,
            "deletions": list(deletions),
            "workUnits": work_units,
            "streams": streams,
            "agreement": agreement,
            "newState": new_state,
        }

    def _fail(self, state: dict, scope: dict, reason: str) -> dict:
        message = "Refresh aborted — " + _scope_change_message(state, scope, reason)
        logging.warning(message)
        return {"action": "fail", "reason": reason, "message": message}

    # ── Activity: resolve the current scope + prior state into a plan ─────────
    def resolve_plan(self, _request: dict) -> dict:
        """Compare the configured scope against the stored state and produce a plan.

        Returns either ``{"action": "load", ...}`` (deletions + work units + the
        new state to persist on success) or ``{"action": "fail", ...}`` when the
        change needs a manual flush (structural change, or crossing the ALL/subset
        boundary).
        """
        scope = _resolve_scope(for_backend_operation())
        agreement = settings.cost_management_agreement_type()
        streams = refresh_streams(agreement)
        W = _end_date()
        state = self._store.get_state()

        # 1. No prior dataset -> full-window load (the implicit "Full").
        if state is None:
            start = _historical_start(W)
            return self._plan(scope, streams, agreement, [(scope["subs"], start, W)], [],
                              _new_state(scope, start, W), "initial-full")

        p_start = _parse_date(state["coverageStart"])
        p_end = _parse_date(state["coverageEnd"])
        P, N = state["subs"], scope["subs"]

        # 2. Scope change that requires a full rebuild -> refuse, ask for a flush.
        conflict = _scope_conflict(state, scope)
        if conflict:
            return self._fail(state, scope, conflict)

        # A newly introduced materialized read projection has no data even when
        # the canonical daily dataset is current. Re-read only its bounded
        # 30-day window once, then publish the projection version in state.
        if not self._store.projection_is_current(state):
            projection_start = max(p_start, W - timedelta(days=29))
            return self._plan(
                scope,
                streams,
                agreement,
                [(scope["subs"], projection_start, W)],
                [],
                _new_state(scope, p_start, max(p_end, W)),
                "projection-rebuild",
            )

        # 3. ALL -> ALL: forward-gap the whole profile.
        if P is None and N is None:
            loads = [] if p_end >= W else [(None, p_end + timedelta(days=1), W)]
            return self._plan(scope, streams, agreement, loads, [],
                              _new_state(scope, p_start, max(p_end, W)), "refresh-all")

        # 4. Both subsets -> reconcile add/remove and forward-gap the survivors.
        added = sorted(set(N) - set(P))
        removed = sorted(set(P) - set(N))
        kept = sorted(set(N) & set(P))
        loads = []
        if added:
            loads.append((added, p_start, W))                   # align new subs to the dataset window
        if kept and p_end < W:
            loads.append((kept, p_end + timedelta(days=1), W))  # forward-gap the survivors
        reason = "reconcile" if (added or removed) else ("refresh" if loads else "up-to-date")
        return self._plan(scope, streams, agreement, loads, removed,
                          _new_state(scope, p_start, max(p_end, W)), reason)

    # ── Read: dataset freshness + scope-vs-state compatibility ────────────────
    def status(self, _request: dict) -> dict:
        """Report dataset freshness and whether a refresh can run against the scope.

        Reuses the same scope/state comparison as :meth:`resolve_plan`: ``warning``
        is populated (and ``isRefreshEnabled`` is False) exactly when a refresh
        would be refused and a flush is required. ``finalizedThrough`` is the most
        recent finalized day (today − lag); ``stalenessDays`` is how far
        ``coverageEnd`` lags it.
        """
        scope = _resolve_scope(for_backend_operation())
        W = _end_date()
        state = self._store.get_state()
        current_scope = _describe_scope(scope["method"], scope.get("billingProfile"), scope["subs"],
                                        scope.get("configSelectors"))

        if state is None:
            return {
                "hasData": False,
                "isRefreshEnabled": True,
                "warning": None,
                "finalizedThrough": _fmt(W),
                "coverageStart": None,
                "coverageEnd": None,
                "stalenessDays": None,
                "isStale": None,
                "currentScope": current_scope,
                "lastLoadedScope": None,
            }

        conflict = _scope_conflict(state, scope)
        staleness = (W - _parse_date(state["coverageEnd"])).days
        return {
            "hasData": True,
            "isRefreshEnabled": conflict is None,
            "warning": _scope_change_message(state, scope, conflict) if conflict else None,
            "finalizedThrough": _fmt(W),
            "coverageStart": state["coverageStart"],
            "coverageEnd": state["coverageEnd"],
            "stalenessDays": staleness,
            "isStale": staleness > 0,
            "currentScope": current_scope,
            "lastLoadedScope": _describe_scope(state["method"], state.get("billingProfile"), state["subs"],
                                               state.get("configSelectors")),
        }

    # ── Activity: fetch + persist ONE query page ──────────────────────────────
    def fetch_and_store_page(self, page_input: dict) -> dict:
        """Fetch a single Cost Management page and MERGE-upsert its records.

        One page = one activity: a fresh ARM token per call (no mid-grind expiry),
        incremental persistence (no all-or-nothing loss), and no ~5-min activity
        timeout. Returns:
          * ``{"status": "ok", "upserted": n, "nextLink": url|None}``
          * ``{"status": "throttled", "retryAfter": seconds}`` — orchestrator waits
          * ``{"status": "error", "error": detail}``
        """
        scope = page_input["scope"]
        export_type = page_input["exportType"]
        aggs = page_input["aggs"]
        kind = page_input["kind"]
        next_link = page_input.get("nextLink")
        try:
            token = for_backend_operation().get_token()
            body = build_query_body(page_input["start"], page_input["end"], page_input["filter"], export_type, aggs)
            url = next_link or query_url(scope)
            res = post_query_once(url, token, body)
            if res["status"] != "ok":
                return res  # throttled / error — passed straight to the orchestrator
            payload = res["payload"]
            records = map_page_records(rows_from_payload(payload), kind)
            upserted = self._store.upsert_daily_records(records, merge=True)
            return {"status": "ok", "upserted": upserted, "nextLink": page_next_link(payload)}
        except Exception as exc:  # noqa: BLE001 — reported back, not swallowed
            logging.error("fetch_and_store_page failed for scope %s [%s..%s]: %s",
                          scope, page_input.get("start"), page_input.get("end"), exc)
            return {"status": "error", "error": str(exc)}

    # ── Activity: delete a set of subscriptions' rows (scope shrink) ──────────
    def delete_subscriptions(self, sub_ids: list) -> dict:
        """Delete every daily-usage row for subscriptions removed from the scope."""
        return {"deleted": self._store.delete_subscriptions_data(sub_ids)}

    # ── Activity: persist the dataset state after a fully-successful load ──────
    def write_state(self, new_state: dict) -> dict:
        self._store.set_state(new_state)
        return {"ok": True}

    # ── Activities: flush the usage tables ────────────────────────────────────
    def flush_delete_tables(self, _input: dict) -> dict:
        """Delete the usage data + checkpoint tables (single O(1) call each)."""
        return {"deleted": self._store.delete_usage_tables()}

    def flush_ensure_tables(self, _input: dict) -> dict:
        """Recreate the usage tables; ``ready`` is False while still being deleted."""
        return {"ready": self._store.ensure_usage_tables()}
