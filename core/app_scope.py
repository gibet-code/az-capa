"""Canonical global app-scope resolution and request-filter intersection."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from . import settings

MODE_ALL = "all"
MODE_SUBSCRIPTIONS = "subscriptions"
MODE_MANAGEMENT_GROUPS = "management_groups"


class AppScopeEmptyError(Exception):
    """The effective application scope resolved to zero subscriptions."""


class AppScopeInitializingError(Exception):
    """The Subscription snapshot is not yet published; retry after warm-up."""


def canonical_values(values: Iterable[str] | None) -> tuple[str, ...]:
    """Trim, lowercase, deduplicate, and sort values for stable comparisons."""
    return tuple(sorted({value.strip().lower() for value in (values or []) if value.strip()}))


@dataclass(frozen=True)
class ConfiguredAppScope:
    mode: str
    selectors: tuple[str, ...]
    locations: tuple[str, ...]

    @property
    def selector_fingerprint(self) -> str:
        payload = json.dumps([self.mode, list(self.selectors)], separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedAppScope:
    configured: ConfiguredAppScope
    subscription_ids: tuple[str, ...]


@dataclass(frozen=True)
class EffectiveFilters:
    subscription_ids: tuple[str, ...]
    locations: tuple[str, ...] | None
    is_empty: bool


def configured_scope() -> ConfiguredAppScope:
    subscription_ids = canonical_values(settings.scope_subscription_ids())
    management_group_ids = canonical_values(settings.scope_management_group_ids())
    settings.validate_scope_exclusivity(subscription_ids, management_group_ids)
    if subscription_ids:
        mode, selectors = MODE_SUBSCRIPTIONS, subscription_ids
    elif management_group_ids:
        mode, selectors = MODE_MANAGEMENT_GROUPS, management_group_ids
    else:
        mode, selectors = MODE_ALL, ()
    return ConfiguredAppScope(mode, selectors, canonical_values(settings.scope_locations()))


def intersect_filters(
    scope: ResolvedAppScope,
    requested_subscription_ids: Iterable[str] | None = None,
    requested_locations: Iterable[str] | None = None,
) -> EffectiveFilters:
    """Intersect request filters with the app boundary without an empty-list fail-open."""
    raw_requested_subs = tuple(requested_subscription_ids) if requested_subscription_ids is not None else None
    requested_subs = canonical_values(raw_requested_subs)
    if raw_requested_subs == ():
        effective_subs = ()
    else:
        effective_subs = tuple(
            value for value in scope.subscription_ids if not requested_subs or value in requested_subs
        )
    raw_requested_locs = tuple(requested_locations) if requested_locations is not None else None
    requested_locs = canonical_values(raw_requested_locs)
    if raw_requested_locs == ():
        effective_locs = ()
    else:
        allowed_locs = scope.configured.locations
        if allowed_locs:
            effective_locs = tuple(value for value in allowed_locs if not requested_locs or value in requested_locs)
        else:
            effective_locs = requested_locs or None
    return EffectiveFilters(
        effective_subs,
        effective_locs,
        not effective_subs or effective_locs == (),
    )