from __future__ import annotations

import pytest

from storage.feature_activation_store import (
    FeatureActivationConflictError,
    FeatureActivationStore,
)
from tests.storage._fake_table import FakeTable


def _store() -> tuple[FeatureActivationStore, FakeTable]:
    store = FeatureActivationStore(lambda: "FeatureActivation")
    table = FakeTable()
    store._table = lambda: table
    return store, table


def test_missing_state_is_read_only():
    store, table = _store()

    assert store.get() is None
    assert table.rows == {}


def test_create_and_replace_state_with_etag():
    store, _ = _store()
    store.replace(("odcr",), expected_etag=None)
    first = store.get()

    store.replace(("allocation_events",), expected_etag=first.etag)

    assert store.get().enabled_feature_ids == ("allocation_events",)


def test_stale_replace_conflicts():
    store, _ = _store()
    store.replace(("odcr",), expected_etag=None)
    stale = store.get()
    store.replace(("odcr", "allocation_events"), expected_etag=stale.etag)

    with pytest.raises(FeatureActivationConflictError):
        store.replace(("allocation_events",), expected_etag=stale.etag)
