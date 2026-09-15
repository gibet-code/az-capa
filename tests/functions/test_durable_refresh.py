from __future__ import annotations

from functions._durable import run_refresh


class _Context:
    def call_activity(self, name, payload):
        return name, payload


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