from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from storage.usage_store import UsageStore, resource_partition_key
from tests.storage._fake_table import FakeTable


class _Table(FakeTable):
    def query_entities(self, query, select=None, parameters=None):
        for name, value in (parameters or {}).items():
            query = query.replace(f"@{name}", f"'{value}'")
        return super().query_entities(query, select)

    def submit_transaction(self, operations):
        assert len(operations) <= 100
        assert len({operation[1]["PartitionKey"] for operation in operations}) == 1
        for operation, entity, *options in operations:
            if operation == "delete":
                self.delete_entity(entity["PartitionKey"], entity["RowKey"])
            else:
                self.upsert_entity(entity, **options[0])


def _store(monkeypatch):
    monkeypatch.setenv("COST_FINALIZATION_LAG_DAYS", "2")
    store = UsageStore(lambda: "daily", lambda: "state", lambda: "summary")
    daily, summary = _Table(), _Table()
    store._data_table = MagicMock(return_value=daily)
    store._summary_table = MagicMock(return_value=summary)
    return store, daily, summary


def _record(day, name="a", sub="sub-a", **metrics):
    return {
        "resourceId": f"/subscriptions/{sub}/resourceGroups/rg/providers/type/{name}",
        "subscriptionId": sub, "date": day, "currency": "EUR", **metrics,
    }


def _key(record):
    return resource_partition_key(record["resourceId"]), record["date"].replace("-", "")


def test_reconciliation_deletes_absent_rows_and_clears_summary_slots(monkeypatch):
    store, daily, summary = _store(monkeypatch)
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    start = end - timedelta(days=39)
    stale = _record(end.isoformat(), uptimeHours=10.0, cost=20.0)
    older = _record((start - timedelta(days=1)).isoformat(), uptimeHours=4.0, cost=5.0)
    unrelated = _record(end.isoformat(), sub="sub-b", uptimeHours=6.0, cost=7.0)
    store.upsert_daily_records([stale, older, unrelated])

    result = store.reconcile_window(start.isoformat(), end.isoformat(), "run", ["sub-a"])

    assert result == {"deleted": 1, "corrected": 0}
    assert _key(stale) not in daily.rows
    assert _key(older) in daily.rows
    assert _key(unrelated) in daily.rows
    assert ("sub-a", resource_partition_key(stale["resourceId"])) not in summary.rows
    assert store.reconcile_window(start.isoformat(), end.isoformat(), "run", ["sub-a"]) == {
        "deleted": 0, "corrected": 0,
    }


def test_split_streams_zero_only_missing_metric_and_replace_revised_values(monkeypatch):
    store, daily, summary = _store(monkeypatch)
    day = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    hours_only = _record(day, "hours", uptimeHours=10.0, cost=20.0)
    cost_only = _record(day, "cost", uptimeHours=10.0, cost=20.0)
    revised = _record(day, "both", uptimeHours=10.0, cost=20.0)
    store.upsert_daily_records([hours_only, cost_only, revised])
    store.upsert_daily_records([
        _record(day, "hours", uptimeHours=3.0, refreshId="run"),
        _record(day, "cost", cost=4.0, refreshId="run"),
        _record(day, "both", uptimeHours=5.0, cost=6.0, refreshId="run"),
    ], merge=True)
    assert store.reconcile_window(day, day, "run", None) == {"deleted": 0, "corrected": 2}
    assert daily.rows[_key(hours_only)]["UptimeHours"] == 3.0
    assert daily.rows[_key(hours_only)]["Cost"] == 0.0
    assert daily.rows[_key(cost_only)]["UptimeHours"] == 0.0
    assert daily.rows[_key(cost_only)]["Cost"] == 4.0
    assert daily.rows[_key(revised)]["UptimeHours"] == 5.0
    assert daily.rows[_key(revised)]["Cost"] == 6.0
    suffix = f"{datetime.strptime(day, '%Y-%m-%d').date().toordinal() % 30:02d}"
    projection = summary.rows[("sub-a", resource_partition_key(cost_only["resourceId"]))]
    assert f"Hours{suffix}" not in projection
    assert f"HoursDate{suffix}" not in projection
    assert projection[f"Cost{suffix}"] == 4.0


def test_new_late_arriving_rows_survive_and_empty_subscription_scope_is_safe(monkeypatch):
    store, daily, _ = _store(monkeypatch)
    record = _record("2026-10-15", uptimeHours=2.0, cost=4.0, refreshId="run")
    store.upsert_daily_records([record], merge=True)
    assert store.reconcile_window("2026-10-01", "2026-10-31", "run", None)["deleted"] == 0
    assert store.reconcile_window("2026-10-01", "2026-10-31", "different-run", [])["deleted"] == 0
    assert daily.rows[_key(record)]["Cost"] == 4.0


def test_projection_failure_does_not_delete_canonical_rows_and_cleanup_is_retryable(monkeypatch):
    store, daily, _ = _store(monkeypatch)
    record = _record("2026-10-15", uptimeHours=2.0, cost=4.0)
    store.upsert_daily_records([record])
    original = store._clear_summary_metrics
    store._clear_summary_metrics = MagicMock(side_effect=RuntimeError("summary failed"))
    with pytest.raises(RuntimeError, match="summary failed"):
        store.reconcile_window("2026-10-01", "2026-10-31", "run", None)
    assert _key(record) in daily.rows
    store._clear_summary_metrics = original
    assert store.reconcile_window("2026-10-01", "2026-10-31", "run", None)["deleted"] == 1


def test_reconciliation_batches_more_than_100_dates_per_partition(monkeypatch):
    store, daily, _ = _store(monkeypatch)
    start = datetime(2026, 1, 1).date()
    records = [
        _record((start + timedelta(days=offset)).isoformat(), uptimeHours=1.0, cost=1.0)
        for offset in range(120)
    ]
    store.upsert_daily_records(records)
    assert store.reconcile_window("2026-01-01", "2026-04-30", "run", None)["deleted"] == 120
    assert not daily.rows
