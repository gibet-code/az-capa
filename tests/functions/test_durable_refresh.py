from __future__ import annotations

from datetime import datetime, timezone

import pytest

from functions._durable import run_refresh


class _Context:
    instance_id = "run"
    current_utc_datetime = datetime(2026, 11, 12, tzinfo=timezone.utc)

    def call_activity(self, name, payload):
        return name, payload

    def create_timer(self, due):
        return "timer", due


_NAMES = {
    "resolve": "resolve",
    "delete_subscriptions": "delete",
    "fetch_page": "fetch",
    "write_state": "write",
}


def _run(outcomes):
    context = _Context()
    plan = {
        "action": "load",
        "reason": "initial-full",
        "deletions": [],
        "workUnits": [
            {"scope": f"/subscriptions/sub-{index}", "start": "2026-08-03", "end": "2026-09-01", "filter": None}
            for index in range(len(outcomes))
        ],
        "streams": [{"exportType": "ActualCost", "aggs": ["UsageQuantity"], "kind": "hours"}],
        "newState": {},
    }
    generator = run_refresh(context, _NAMES)
    response = None
    pending_outcomes = iter(outcomes)
    while True:
        try:
            request = next(generator) if response is None else generator.send(response)
        except StopIteration as completed:
            return completed.value
        name, _ = request
        if name == "resolve":
            response = plan
        elif name == "fetch":
            response = next(pending_outcomes)
        elif name == "write":
            response = {"ok": True}


def test_all_failed_work_units_report_failed():
    result = _run([
        {"status": "error", "error": "denied"},
        {"status": "error", "error": "denied"},
    ])

    assert result["status"] == "failed"
    assert result["failed"] == 2
    assert result["stateWritten"] is False


def test_mixed_work_units_report_partial():
    result = _run([
        {"status": "ok", "upserted": 4, "nextLink": None},
        {"status": "error", "error": "denied"},
    ])

    assert result["status"] == "partial"
    assert result["failed"] == 1
    assert result["upserted"] == 4


def test_successful_work_units_report_completed_and_write_state():
    result = _run([
        {"status": "ok", "upserted": 4, "nextLink": None},
        {"status": "ok", "upserted": 3, "nextLink": None},
    ])

    assert result["status"] == "completed"
    assert result["failed"] == 0
    assert result["upserted"] == 7
    assert result["stateWritten"] is True


def _cr_run(fetch_results, reconciliation_error=False):
    plan = {
        "action": "load", "reason": "refresh", "deletions": [], "reconcile": True,
        "workUnits": [{
            "scope": "/subscriptions/sub-a", "start": "2026-10-02", "end": "2026-11-10",
            "filter": None, "subscriptionIds": ["sub-a"],
        }],
        "streams": [
            {"exportType": "ActualCost", "aggs": ["UsageQuantity"], "kind": "hours"},
            {"exportType": "AmortizedCost", "aggs": ["Cost"], "kind": "cost"},
        ],
        "newState": {},
    }
    generator = run_refresh(_Context(), {**_NAMES, "reconcile": "reconcile"})
    requests = []
    responses = iter(fetch_results)
    request = next(generator)
    while True:
        name, payload = request
        requests.append(request)
        if name == "resolve":
            response = plan
        elif name == "fetch":
            assert payload["refreshId"] == "run:0"
            response = next(responses)
        elif name == "reconcile":
            if reconciliation_error:
                generator.throw(RuntimeError("storage unavailable"))
            response = {"deleted": 2, "corrected": 1}
        else:
            response = {"ok": True}
        try:
            request = generator.send(response)
        except StopIteration as finished:
            return finished.value, requests


def test_cr_reconciles_only_after_all_pages_and_both_streams_succeed():
    result, requests = _cr_run([
        {"status": "throttled", "retryAfter": 2},
        {"status": "ok", "upserted": 2, "nextLink": "next"},
        {"status": "ok", "upserted": 1, "nextLink": None},
        {"status": "ok", "upserted": 0, "nextLink": None},
    ])
    assert [name for name, _ in requests] == [
        "resolve", "fetch", "timer", "fetch", "fetch", "fetch", "reconcile", "write",
    ]
    assert result["deletedRows"] == 2
    assert result["upserted"] == 3
    assert result["stateWritten"] is True


@pytest.mark.parametrize("responses", [
    [{"status": "error", "error": "denied"}],
    [{"status": "ok", "upserted": 1, "nextLink": "next"}, {"status": "error", "error": "failed page"}],
    [{"status": "ok", "upserted": 1, "nextLink": None}, {"status": "error", "error": "failed cost"}],
])
def test_cr_failed_page_or_stream_never_cleans_up_or_advances_state(responses):
    result, requests = _cr_run(responses)
    assert result["status"] == "failed"
    assert result["stateWritten"] is False
    assert not any(name in ("reconcile", "write") for name, _ in requests)


def test_cr_cleanup_failure_prevents_state_write():
    with pytest.raises(RuntimeError, match="storage unavailable"):
        _cr_run([
            {"status": "ok", "upserted": 0, "nextLink": None},
            {"status": "ok", "upserted": 0, "nextLink": None},
        ], reconciliation_error=True)


def test_cr_successful_empty_streams_still_reconcile_the_window():
    result, requests = _cr_run([
        {"status": "ok", "upserted": 0, "nextLink": None},
        {"status": "ok", "upserted": 0, "nextLink": None},
    ])
    assert result["status"] == "completed"
    assert result["deletedRows"] == 2
    assert [name for name, _ in requests][-2:] == ["reconcile", "write"]