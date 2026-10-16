"""Static feature registry and cached application-wide activation state."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from azure.core.exceptions import AzureError

from core import settings
from storage.feature_activation_store import (
    FeatureActivationConflictError,
    FeatureActivationStore,
)

logger = logging.getLogger(__name__)

ODCR = "odcr"
ALLOCATION_EVENTS = "allocation_events"


@dataclass(frozen=True)
class FeatureDescriptor:
    id: str
    name: str
    maturity: str
    default_enabled: bool


_DESCRIPTORS = (
    FeatureDescriptor(ODCR, "On-Demand Capacity Reservations", "general_availability", True),
    FeatureDescriptor(ALLOCATION_EVENTS, "Allocation Events", "preview", False),
)
_BY_ID = {descriptor.id: descriptor for descriptor in _DESCRIPTORS}
_DEFAULT_ENABLED = frozenset(
    descriptor.id for descriptor in _DESCRIPTORS if descriptor.default_enabled
)


class FeatureActivationError(Exception):
    pass


class UnknownFeatureError(FeatureActivationError):
    pass


class FeatureStateUnavailableError(FeatureActivationError):
    pass


class AtLeastOneFeatureRequiredError(FeatureActivationError):
    pass


class FeatureUpdateConflictError(FeatureActivationError):
    pass


def all_descriptors() -> tuple[FeatureDescriptor, ...]:
    return _DESCRIPTORS


def ids() -> tuple[str, ...]:
    return tuple(descriptor.id for descriptor in _DESCRIPTORS)


def get_descriptor(feature_id: str) -> FeatureDescriptor:
    try:
        return _BY_ID[feature_id]
    except KeyError:
        raise UnknownFeatureError(f"Unknown feature {feature_id!r}.") from None


class FeatureActivationService:
    def __init__(
        self,
        *,
        store: FeatureActivationStore | None = None,
        ttl_seconds: Callable[[], int] = settings.feature_activation_l1_ttl_seconds,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store or FeatureActivationStore()
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached_enabled: frozenset[str] | None = None
        self._cache_expires_at = 0.0

    @staticmethod
    def _validate_enabled(feature_ids: tuple[str, ...]) -> frozenset[str]:
        enabled = frozenset(feature_ids)
        unknown = enabled.difference(_BY_ID)
        if unknown:
            raise FeatureStateUnavailableError(
                f"Feature activation contains unknown feature IDs: {sorted(unknown)!r}."
            )
        if len(enabled) != len(feature_ids):
            raise FeatureStateUnavailableError("Feature activation contains duplicate feature IDs.")
        if not enabled:
            raise FeatureStateUnavailableError(
                "Feature activation must contain at least one enabled feature."
            )
        return enabled

    def _read_enabled(self) -> tuple[frozenset[str], str | None]:
        try:
            stored = self._store.get()
        except (AzureError, KeyError, TypeError, ValueError) as exc:
            raise FeatureStateUnavailableError(
                "Feature activation state is unavailable."
            ) from exc
        if stored is None:
            return _DEFAULT_ENABLED, None
        return self._validate_enabled(stored.enabled_feature_ids), stored.etag

    def enabled_ids(self) -> frozenset[str]:
        now = self._monotonic()
        with self._lock:
            if self._cached_enabled is not None and self._cache_expires_at > now:
                return self._cached_enabled
            enabled, _ = self._read_enabled()
            self._cached_enabled = enabled
            self._cache_expires_at = self._monotonic() + self._ttl_seconds()
            return enabled

    def is_enabled(self, feature_id: str) -> bool:
        get_descriptor(feature_id)
        return feature_id in self.enabled_ids()

    def list_features(self) -> list[dict]:
        enabled = self.enabled_ids()
        return [
            {
                "id": descriptor.id,
                "name": descriptor.name,
                "enabled": descriptor.id in enabled,
                "maturity": descriptor.maturity,
            }
            for descriptor in _DESCRIPTORS
        ]

    def update(self, feature_id: str, enabled: bool) -> dict:
        descriptor = get_descriptor(feature_id)
        with self._lock:
            current, etag = self._read_enabled()
            changed = set(current)
            if enabled:
                changed.add(feature_id)
            else:
                changed.discard(feature_id)
            if not changed:
                raise AtLeastOneFeatureRequiredError(
                    "At least one feature must remain enabled."
                )
            ordered = tuple(item.id for item in _DESCRIPTORS if item.id in changed)
            if frozenset(changed) != current:
                try:
                    self._store.replace(ordered, expected_etag=etag)
                except FeatureActivationConflictError as exc:
                    raise FeatureUpdateConflictError(str(exc)) from exc
                except AzureError as exc:
                    raise FeatureStateUnavailableError(
                        "Feature activation state could not be saved."
                    ) from exc
                logger.info(
                    "Feature activation changed: feature=%s old=%s new=%s",
                    feature_id,
                    feature_id in current,
                    enabled,
                )
            effective = frozenset(changed)
            self._cached_enabled = effective
            self._cache_expires_at = self._monotonic() + self._ttl_seconds()
        return {
            "id": descriptor.id,
            "name": descriptor.name,
            "enabled": descriptor.id in effective,
            "maturity": descriptor.maturity,
        }


service = FeatureActivationService()
