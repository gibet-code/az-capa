from __future__ import annotations

from datetime import datetime, timezone

from functions.activity_log_bp import (
    _activity_log_refresh,
    _activity_log_subscription,
)


class _SubOrchestrationTask:
    def __init__(self, name, payload):
        self.name = name
        self.payload = payload
        self._result = None
        self.is_completed = False

    @property
    def result(self):
        return self._result

    @result.setter
    def result(self, value):
        self._result = value
        self.is_completed = True


class _TimerTask(_SubOrchestrationTask):
    def __init__(self, due):
        super().__init__("timer", due)


class _Context:
    current_utc_datetime = datetime(2026, 9, 7, tzinfo=timezone.utc)

    def __init__(self, input_value=None):
        self._input = input_value
        self.custom_statuses = []

    def get_input(self):
        return self._input

    def call_activity(self, name, payload):
        return "activity", name, payload

    def call_sub_orchestrator(self, name, payload):
        return _SubOrchestrationTask(name, payload)

    def task_all(self, tasks):
        return "all", tasks

    def task_any(self, tasks):
        return "any", tasks

    def set_custom_status(self, status):
        self.custom_statuses.append(status)

    def create_timer(self, due):
        return _TimerTask(due)


def test_up_to_date_plan_completes_without_subscription_work():
    generator = _activity_log_refresh(_Context())

    assert next(generator) == ("activity", "activity_log_resolve_plan", {})
    try:
        generator.send({
            "action": "noop",
            "reason": "up-to-date",
            "targetStart": "2026-08-08T00:00:00Z",
            "targetEnd": "2026-09-07T00:00:00Z",
        })
    except StopIteration as completed:
        result = completed.value
    else:
        raise AssertionError("Activity Log refresh did not complete")

    assert result == {
        "status": "completed",
        "action": "up-to-date",
        "targetStart": "2026-08-08T00:00:00Z",
        "targetEnd": "2026-09-07T00:00:00Z",
        "subscriptions": 0,
    }


def test_subscription_commits_only_after_all_windows_succeed():
    request = {
        "subscriptionId": "sub-1",
        "windows": [
            {"start": "2026-08-08T00:00:00Z", "end": "2026-08-20T00:00:00Z"},
            {"start": "2026-09-04T00:00:00Z", "end": "2026-09-07T00:00:00Z"},
        ],
        "coverage": {"subscriptionId": "sub-1"},
        "operations": ["op"],
    }
    generator = _activity_log_subscription(_Context(request))

    first = next(generator)
    assert first[1] == "activity_log_fetch_page"
    assert first[2]["start"] == "2026-08-08T00:00:00Z"
    second = generator.send({"status": "ok", "received": 2, "upserted": 1, "nextLink": None})
    assert second[2]["start"] == "2026-09-04T00:00:00Z"
    commit = generator.send({"status": "ok", "received": 3, "upserted": 2, "nextLink": None})
    assert commit == ("activity", "activity_log_write_state", [{"subscriptionId": "sub-1"}])

    try:
        generator.send({"ok": True})
    except StopIteration as completed:
        result = completed.value
    else:
        raise AssertionError("Subscription orchestration did not complete")

    assert result["success"] is True
    assert result["committed"] is True
    assert result["pages"] == 2
    assert result["received"] == 5
    assert result["upserted"] == 3


def test_subscription_failure_in_later_window_does_not_advance_coverage():
    request = {
        "subscriptionId": "sub-1",
        "windows": [
            {"start": "2026-08-08T00:00:00Z", "end": "2026-08-20T00:00:00Z"},
            {"start": "2026-09-04T00:00:00Z", "end": "2026-09-07T00:00:00Z"},
        ],
        "coverage": {"subscriptionId": "sub-1"},
        "operations": ["op"],
    }
    generator = _activity_log_subscription(_Context(request))

    next(generator)
    generator.send({"status": "ok", "received": 2, "upserted": 1, "nextLink": None})
    try:
        generator.send({"status": "error", "error": "expired credential"})
    except StopIteration as completed:
        result = completed.value
    else:
        raise AssertionError("Subscription orchestration did not stop")

    assert result["success"] is False
    assert result["error"] == "expired credential"
    assert "committed" not in result


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
    assert request[0] == "any"
    first, second = request[1]
    first.result = {
        "subscriptionId": "sub-1", "success": True, "committed": True,
        "received": 3, "upserted": 2,
    }
    request = generator.send(first)
    assert request[0] == "any"
    second.result = {
        "subscriptionId": "sub-2", "success": True, "committed": True,
        "received": 4, "upserted": 3,
    }
    request = generator.send(second)
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


def test_all_subscription_failures_report_failed_result():
    context = _Context()
    plan = {
        "action": "load",
        "reason": "incremental",
        "targetStart": "2026-08-08T00:00:00Z",
        "targetEnd": "2026-09-07T00:00:00Z",
        "deletions": [],
        "workUnits": [{"subscriptionId": "sub-1", "windows": []}],
        "operations": [],
        "maxConcurrency": 1,
        "replayMaxUnits": 200,
    }
    generator = _activity_log_refresh(context)

    assert next(generator) == ("activity", "activity_log_resolve_plan", {})
    request = generator.send(plan)
    assert request[0] == "any"
    completed_task = request[1][0]
    completed_task.result = {
        "subscriptionId": "sub-1",
        "success": False,
        "error": "credential unavailable",
        "received": 0,
        "upserted": 0,
    }
    request = generator.send(completed_task)
    assert request[0] == "all"

    try:
        generator.send([{"subscriptionId": "sub-1", "status": "ok", "reconciled": 0}])
    except StopIteration as completed:
        result = completed.value
    else:
        raise AssertionError("Activity Log refresh did not complete")

    assert result["status"] == "failed"
    assert result["committedSubscriptions"] == 0
    assert result["failed"] == 1


def test_subscription_pool_refills_one_slot_after_each_completion():
    context = _Context()
    plan = {
        "action": "load",
        "reason": "incremental",
        "targetStart": "2026-08-08T00:00:00Z",
        "targetEnd": "2026-09-07T00:00:00Z",
        "deletions": [],
        "workUnits": [
            {"subscriptionId": "sub-1", "windows": []},
            {"subscriptionId": "sub-2", "windows": []},
            {"subscriptionId": "sub-3", "windows": []},
        ],
        "operations": [],
        "maxConcurrency": 2,
        "replayMaxUnits": 200,
    }
    generator = _activity_log_refresh(context)

    assert next(generator) == ("activity", "activity_log_resolve_plan", {})
    request = generator.send(plan)
    first, second = request[1]
    assert [task.payload["subscriptionId"] for task in request[1]] == ["sub-1", "sub-2"]

    first.result = {
        "subscriptionId": "sub-1", "success": True, "committed": True,
        "received": 1, "upserted": 1,
    }
    request = generator.send(first)
    assert request[0] == "any"
    dispatch_timer, third = request[1]
    assert dispatch_timer.name == "timer"
    assert third.payload["subscriptionId"] == "sub-3"
    dispatch_timer.result = None
    request = generator.send(dispatch_timer)
    assert [task.payload["subscriptionId"] for task in request[1]] == ["sub-2", "sub-3"]
    assert context.custom_statuses[-1] == {
        "completedSubscriptions": 1,
        "totalSubscriptions": 3,
        "percentCompleted": 33,
    }

    second.result = {
        "subscriptionId": "sub-2", "success": True, "committed": True,
        "received": 1, "upserted": 1,
    }
    request = generator.send(second)
    third = request[1][0]
    assert third.payload["subscriptionId"] == "sub-3"

    third.result = {
        "subscriptionId": "sub-3", "success": True, "committed": True,
        "received": 1, "upserted": 1,
    }
    request = generator.send(third)
    assert request[0] == "all"
    assert context.custom_statuses[-1] == {
        "completedSubscriptions": 3,
        "totalSubscriptions": 3,
        "percentCompleted": 100,
    }


def test_subscription_pool_drains_completion_burst_before_refilling():
    context = _Context()
    plan = {
        "action": "load",
        "reason": "incremental",
        "targetStart": "2026-08-08T00:00:00Z",
        "targetEnd": "2026-09-07T00:00:00Z",
        "deletions": [],
        "workUnits": [
            {"subscriptionId": f"sub-{index}", "windows": []}
            for index in range(1, 6)
        ],
        "operations": [],
        "maxConcurrency": 3,
        "replayMaxUnits": 200,
    }
    generator = _activity_log_refresh(context)

    next(generator)
    request = generator.send(plan)
    first, second, third = request[1]
    first.result = {"subscriptionId": "sub-1", "success": True, "committed": True}
    second.result = {"subscriptionId": "sub-2", "success": True, "committed": True}

    request = generator.send(first)

    assert request[0] == "any"
    dispatch_timer, fourth, fifth = request[1]
    assert dispatch_timer.name == "timer"
    assert [fourth.payload["subscriptionId"], fifth.payload["subscriptionId"]] == [
        "sub-4", "sub-5",
    ]
    dispatch_timer.result = None
    request = generator.send(dispatch_timer)
    assert [task.payload["subscriptionId"] for task in request[1]] == [
        "sub-3", "sub-4", "sub-5",
    ]
    assert third is request[1][0]
    assert context.custom_statuses[-1] == {
        "completedSubscriptions": 2,
        "totalSubscriptions": 5,
        "percentCompleted": 40,
    }