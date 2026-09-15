from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.settings import METHOD_PER_BILLING, METHOD_PER_SUB
from services.usage_refresh import (
    REFUSE_BOUNDARY,
    REFUSE_STRUCTURAL,
    RefreshPipeline,
    map_page_records,
    refresh_streams,
)

_DEFAULT_SUBS = object()


def _scope(method=METHOD_PER_SUB, subs=_DEFAULT_SUBS, account=None, profile=None):
    return {
        "method": method,
        "billingAccount": account,
        "billingProfile": profile,
        "subs": ["sub-a", "sub-b"] if subs is _DEFAULT_SUBS else subs,
        "configSelectors": {"subscriptions": [], "managementGroups": []},
    }


def _state(scope, start="2026-08-01", end="2026-08-29", **extra):
    return {**scope, "coverageStart": start, "coverageEnd": end, **extra}


def _pipeline(state=None, projection_current=True):
    store = MagicMock()
    store.get_state.return_value = state
    store.projection_is_current.return_value = projection_current
    build_filter = MagicMock(side_effect=lambda **kwargs: {"subscriptions": kwargs.get("subscription_ids")})
    return RefreshPipeline(build_filter, store), store, build_filter


def _resolve(pipeline, scope):
    with (
        patch("services.usage_refresh._resolve_scope", return_value=scope),
        patch("services.usage_refresh._end_date", return_value=date(2026, 8, 31)),
        patch("services.usage_refresh.settings.cost_management_agreement_type", return_value="MCA"),
    ):
        return pipeline.resolve_plan({})


def test_refresh_streams_uses_one_mca_stream_and_split_ea_streams():
    with patch("services.usage_refresh.settings.AGREEMENT_MCA", "MCA"):
        mca_streams = refresh_streams("MCA")
    assert mca_streams == [
        {"exportType": "AmortizedCost", "aggs": ["UsageQuantity", "Cost"], "kind": "both"}
    ]
    assert [stream["kind"] for stream in refresh_streams("EA")] == ["hours", "cost"]


def test_map_page_records_keeps_stream_metrics_separate_and_skips_invalid_rows():
    rows = [
        {
            "ResourceId": "/subscriptions/SUB-A/resourceGroups/rg/providers/type/name",
            "UsageDate": 20260830,
            "UsageQuantity": None,
            "Cost": 4.5,
            "Currency": "EUR",
        },
        {"ResourceId": None, "UsageDate": 20260830},
        {"ResourceId": "resource", "UsageDate": None},
    ]

    assert map_page_records(rows, "hours") == [{
        "resourceId": rows[0]["ResourceId"],
        "subscriptionId": "sub-a",
        "date": "2026-08-30",
        "currency": "EUR",
        "uptimeHours": 0.0,
    }]
    assert map_page_records(rows[:1], "cost")[0]["cost"] == 4.5
    assert "uptimeHours" not in map_page_records(rows[:1], "cost")[0]


def test_initial_plan_loads_full_window_for_every_subscription():
    pipeline, _store, _filter = _pipeline()

    plan = _resolve(pipeline, _scope(subs=["sub-b", "sub-a"]))

    assert plan["reason"] == "initial-full"
    assert [unit["scope"] for unit in plan["workUnits"]] == [
        "/subscriptions/sub-b",
        "/subscriptions/sub-a",
    ]
    assert {unit["start"] for unit in plan["workUnits"]} == {"2026-08-02"}
    assert plan["newState"]["coverageEnd"] == "2026-08-31"


def test_plan_refuses_structural_and_all_subset_boundary_changes():
    old_scope = _scope(subs=["sub-a"])
    pipeline, _store, _filter = _pipeline(_state(old_scope))

    structural = _resolve(
        pipeline,
        _scope(METHOD_PER_BILLING, ["sub-a"], account="account", profile="profile"),
    )
    assert structural["action"] == "fail"
    assert structural["reason"] == REFUSE_STRUCTURAL
    assert "flush then refresh" in structural["message"]

    all_scope = _scope(METHOD_PER_BILLING, None, account="account", profile="profile")
    subset_scope = _scope(METHOD_PER_BILLING, ["sub-a"], account="account", profile="profile")
    pipeline, _store, _filter = _pipeline(_state(all_scope))

    boundary = _resolve(pipeline, subset_scope)
    assert boundary["action"] == "fail"
    assert boundary["reason"] == REFUSE_BOUNDARY


def test_projection_rebuild_reloads_only_bounded_window_without_deletions():
    scope = _scope(subs=["sub-a"])
    pipeline, _store, _filter = _pipeline(
        _state(scope, start="2026-07-01", end="2026-08-29"),
        projection_current=False,
    )

    plan = _resolve(pipeline, scope)

    assert plan["reason"] == "projection-rebuild"
    assert plan["deletions"] == []
    assert plan["workUnits"][0]["start"] == "2026-08-02"
    assert plan["newState"]["coverageStart"] == "2026-07-01"


def test_subset_reconciliation_deletes_removed_and_loads_added_and_kept_gaps():
    previous = _scope(subs=["sub-a", "sub-b"])
    current = _scope(subs=["sub-b", "sub-c"])
    pipeline, _store, _filter = _pipeline(_state(previous))

    plan = _resolve(pipeline, current)

    assert plan["reason"] == "reconcile"
    assert plan["deletions"] == ["sub-a"]
    assert [(unit["scope"], unit["start"], unit["end"]) for unit in plan["workUnits"]] == [
        ("/subscriptions/sub-c", "2026-08-01", "2026-08-31"),
        ("/subscriptions/sub-b", "2026-08-30", "2026-08-31"),
    ]


def test_all_scope_refreshes_only_forward_gap():
    scope = _scope(METHOD_PER_BILLING, None, account="account", profile="profile")
    pipeline, _store, build_filter = _pipeline(_state(scope))

    plan = _resolve(pipeline, scope)

    assert plan["reason"] == "refresh-all"
    assert len(plan["workUnits"]) == 1
    assert plan["workUnits"][0]["start"] == "2026-08-30"
    build_filter.assert_called_once_with(subscription_ids=None)


def test_status_reports_missing_data_and_structural_conflict():
    current = _scope(subs=["sub-a"])
    pipeline, store, _filter = _pipeline()
    with (
        patch("services.usage_refresh._resolve_scope", return_value=current),
        patch("services.usage_refresh._end_date", return_value=date(2026, 8, 31)),
    ):
        assert pipeline.status({})["hasData"] is False

    store.get_state.return_value = _state(_scope(METHOD_PER_BILLING, ["sub-a"], "account", "profile"))
    with (
        patch("services.usage_refresh._resolve_scope", return_value=current),
        patch("services.usage_refresh._end_date", return_value=date(2026, 8, 31)),
    ):
        status = pipeline.status({})

    assert status["hasData"] is True
    assert status["isRefreshEnabled"] is False
    assert status["warning"] is not None
    assert status["stalenessDays"] == 2


def test_fetch_and_store_page_persists_ok_page_and_passes_throttle_through():
    pipeline, store, _filter = _pipeline()
    page = {
        "scope": "/subscriptions/sub-a",
        "start": "2026-08-30",
        "end": "2026-08-31",
        "filter": None,
        "exportType": "ActualCost",
        "aggs": ["UsageQuantity"],
        "kind": "hours",
    }
    store.upsert_daily_records.return_value = 1
    payload = {
        "properties": {
            "columns": [{"name": "ResourceId"}, {"name": "UsageDate"}, {"name": "UsageQuantity"}],
            "rows": [["/subscriptions/sub-a/resource", 20260830, 1.5]],
            "nextLink": "https://next",
        }
    }
    identity = SimpleNamespace(get_token=MagicMock(return_value="token"))

    with (
        patch("services.usage_refresh.for_backend_operation", return_value=identity),
        patch("services.usage_refresh.post_query_once", return_value={"status": "ok", "payload": payload}),
    ):
        result = pipeline.fetch_and_store_page(page)

    assert result == {"status": "ok", "upserted": 1, "nextLink": "https://next"}
    store.upsert_daily_records.assert_called_once_with([
        {
            "resourceId": "/subscriptions/sub-a/resource",
            "subscriptionId": "sub-a",
            "date": "2026-08-30",
            "currency": None,
            "uptimeHours": 1.5,
        }
    ], merge=True)

    with (
        patch("services.usage_refresh.for_backend_operation", return_value=identity),
        patch("services.usage_refresh.post_query_once", return_value={"status": "throttled", "retryAfter": 20}),
    ):
        assert pipeline.fetch_and_store_page(page) == {"status": "throttled", "retryAfter": 20}


def test_activity_delegates_return_store_results_and_fetch_errors_are_stable():
    pipeline, store, _filter = _pipeline()
    store.delete_subscriptions_data.return_value = 3
    store.delete_usage_tables.return_value = 2
    store.ensure_usage_tables.return_value = False

    assert pipeline.delete_subscriptions(["sub-a"]) == {"deleted": 3}
    assert pipeline.write_state({"state": True}) == {"ok": True}
    store.set_state.assert_called_once_with({"state": True})
    assert pipeline.flush_delete_tables({}) == {"deleted": 2}
    assert pipeline.flush_ensure_tables({}) == {"ready": False}

    with patch("services.usage_refresh.for_backend_operation", side_effect=RuntimeError("token failed")):
        result = pipeline.fetch_and_store_page({
            "scope": "scope",
            "start": "2026-08-01",
            "end": "2026-08-02",
            "filter": None,
            "exportType": "ActualCost",
            "aggs": ["Cost"],
            "kind": "cost",
        })
    assert result == {"status": "error", "error": "token failed"}
