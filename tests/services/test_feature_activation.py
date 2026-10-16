from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services import feature_activation
from storage.feature_activation_store import StoredFeatureActivation


def _service(store=None, now=None):
    clock = now or [0.0]
    return feature_activation.FeatureActivationService(
        store=store or MagicMock(),
        ttl_seconds=lambda: 60,
        monotonic=lambda: clock[0],
    )


def test_missing_entity_uses_defaults_without_write():
    store = MagicMock()
    store.get.return_value = None
    service = _service(store)

    assert service.enabled_ids() == frozenset({feature_activation.ODCR})
    store.replace.assert_not_called()


def test_list_returns_registry_order_and_maturity():
    store = MagicMock()
    store.get.return_value = None

    features = _service(store).list_features()

    assert [item["id"] for item in features] == ["odcr", "allocation_events"]
    assert features[0]["enabled"] is True
    assert features[1] == {
        "id": "allocation_events",
        "name": "Allocation Events",
        "enabled": False,
        "maturity": "preview",
    }


def test_update_creates_singleton_and_writes_through_cache():
    store = MagicMock()
    store.get.return_value = None
    service = _service(store)

    updated = service.update(feature_activation.ALLOCATION_EVENTS, True)

    assert updated["enabled"] is True
    store.replace.assert_called_once_with(
        ("odcr", "allocation_events"),
        expected_etag=None,
    )
    assert service.enabled_ids() == frozenset({"odcr", "allocation_events"})
    store.get.assert_called_once()


def test_cannot_disable_last_enabled_feature():
    store = MagicMock()
    store.get.return_value = None

    with pytest.raises(feature_activation.AtLeastOneFeatureRequiredError):
        _service(store).update(feature_activation.ODCR, False)

    store.replace.assert_not_called()


def test_can_disable_odcr_when_allocation_events_is_enabled():
    store = MagicMock()
    store.get.return_value = StoredFeatureActivation(
        ("odcr", "allocation_events"), "etag-1"
    )

    updated = _service(store).update(feature_activation.ODCR, False)

    assert updated["enabled"] is False
    store.replace.assert_called_once_with(("allocation_events",), expected_etag="etag-1")


@pytest.mark.parametrize(
    "enabled",
    [(), ("unknown",), ("odcr", "odcr")],
)
def test_invalid_persisted_state_fails_closed(enabled):
    store = MagicMock()
    store.get.return_value = StoredFeatureActivation(enabled, "etag")

    with pytest.raises(feature_activation.FeatureStateUnavailableError):
        _service(store).enabled_ids()


def test_l1_cache_expires_and_reloads():
    now = [0.0]
    store = MagicMock()
    store.get.return_value = None
    service = _service(store, now)

    service.enabled_ids()
    now[0] = 59.0
    service.enabled_ids()
    now[0] = 61.0
    service.enabled_ids()

    assert store.get.call_count == 2
