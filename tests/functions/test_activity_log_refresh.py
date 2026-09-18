from __future__ import annotations

from functions.activity_log_bp import _activity_log_refresh


class _Context:
    def call_activity(self, name, payload):
        return "activity", name, payload

    def call_sub_orchestrator(self, name, payload):
        return "sub", name, payload

    def task_all(self, tasks):
        return "all", tasks

    def set_custom_status(self, _status):
        pass


def test_reconcile_failure_reports_partial_result():
    context = _Context()
    plan = {
        "action": "load",
        "reason": "refresh",
        "deletions": [],
        "workUnits": [
            {"subscriptionId": "sub-1", "operations": []},
            {"subscriptionId": "sub-2", "operations": []},
        ],
        "operations": [],
        "maxConcurrency": 2,
        "replayMaxUnits": 200,
    }
    generator = _activity_log_refresh(context)

    assert next(generator) == ("activity", "activity_log_resolve_plan", {})
    request = generator.send(plan)
    assert request[0] == "all"
    request = generator.send([
        {"subscriptionId": "sub-1", "success": True, "committed": True, "received": 3, "upserted": 2},
        {"subscriptionId": "sub-2", "success": True, "committed": True, "received": 4, "upserted": 3},
    ])
    assert request[0] == "all"

    try:
        generator.send([
            {"subscriptionId": "sub-1", "status": "ok", "reconciled": 2},
            {"subscriptionId": "sub-2", "status": "error", "error": "storage reset", "reconciled": 0},
        ])
    except StopIteration as completed:
        result = completed.value
    else:
        raise AssertionError("Activity Log refresh did not complete")

    assert result["status"] == "partial"
    assert result["subscriptions"] == 2
    assert result["committedSubscriptions"] == 2
    assert result["failed"] == 0
    assert result["reconciled"] == 2
    assert result["reconcileFailed"] == 1