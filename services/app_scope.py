"""Resolve the configured application boundary from the published snapshot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clients.resource_graph import get_subscription_inventory
from core import settings
from core.app_scope import (
    MODE_ALL,
    MODE_MANAGEMENT_GROUPS,
    MODE_SUBSCRIPTIONS,
    AppScopeEmptyError,
    AppScopeInitializingError,
    ConfiguredAppScope,
    ResolvedAppScope,
    canonical_values,
    configured_scope,
)
from core.identity import AzureIdentity
from storage.reference_data_store import SUBSCRIPTIONS, ReferenceDataStore, store as reference_store

__all__ = [
    "AppScopeEmptyError",
    "AppScopeInitializingError",
    "diagnose_app_scope",
    "mode_label",
    "open_subscription_snapshot",
    "resolve_app_scope",
]

_MODE_LABELS = {
    MODE_ALL: "All accessible subscriptions",
    MODE_SUBSCRIPTIONS: "Selected subscriptions",
    MODE_MANAGEMENT_GROUPS: "Selected management groups",
}


def mode_label(mode: str) -> str:
    """Return the administrator-facing label for a normalized scope mode."""
    return _MODE_LABELS.get(mode, mode)


def open_subscription_snapshot(store: ReferenceDataStore = reference_store) -> dict | None:
    """Return the published Subscription snapshot, or ``None`` if none is published.

    App Scope never initializes the snapshot inline on a read. Storage failures
    and invalid persisted data propagate as errors from the store rather than an
    absent result.
    """
    return store.open_snapshot(SUBSCRIPTIONS)


def _live_subscription_values(identity: AzureIdentity) -> dict:
    """Fetch and normalize the live subscription inventory, bypassing the cache."""
    from services.reference_data import normalize_subscriptions  # lazy: avoids import cycle

    return normalize_subscriptions(get_subscription_inventory(identity.get_token()))


def _match_management_groups(
    values: dict, selectors: tuple[str, ...]
) -> tuple[str, ...]:
    wanted = set(selectors)
    matched = [
        subscription_id
        for subscription_id, entry in (values or {}).items()
        if any(
            str(ancestor.get("id") or "").strip().lower() in wanted
            for ancestor in (entry or {}).get("managementGroupAncestors") or []
        )
    ]
    return canonical_values(matched)


def resolve_app_scope(
    identity: AzureIdentity | None = None,
    scope: ConfiguredAppScope | None = None,
    store: ReferenceDataStore = reference_store,
    *,
    allow_live_fallback: bool = False,
) -> ResolvedAppScope:
    """Resolve the enforced boundary from the published Subscription snapshot.

    Selected-subscription mode uses the configured IDs directly and reads no
    snapshot. All and Management Group modes read the published snapshot: an
    absent snapshot raises :class:`AppScopeInitializingError`, and an empty
    effective scope raises :class:`AppScopeEmptyError`.

    ``allow_live_fallback`` lets backend, non-interactive pipelines resolve the
    boundary from a live inventory query when the snapshot is absent, so they do
    not depend on the Subscription reference pipeline having published first. It
    is read-only and never republishes; interactive callers leave it off and
    keep the strict, snapshot-only behavior.
    """
    configured = scope or configured_scope()
    if configured.mode == MODE_SUBSCRIPTIONS:
        return ResolvedAppScope(configured, configured.selectors)

    snapshot = open_subscription_snapshot(store)
    if snapshot is not None:
        values = snapshot.get("values") or {}
    elif allow_live_fallback and identity is not None:
        values = _live_subscription_values(identity)
    else:
        raise AppScopeInitializingError(
            "The Subscription reference-data snapshot is not yet published."
        )
    if configured.mode == MODE_MANAGEMENT_GROUPS:
        subscription_ids = _match_management_groups(values, configured.selectors)
    else:
        subscription_ids = canonical_values(values.keys())
    if not subscription_ids:
        raise AppScopeEmptyError("The application scope resolved to no subscriptions.")
    return ResolvedAppScope(configured, subscription_ids)


def _enforced_metadata(store: ReferenceDataStore) -> dict:
    """Published Subscription generation/time/staleness behind the enforced boundary."""
    snapshot = store.open_snapshot(SUBSCRIPTIONS)
    published_at = snapshot.get("publishedAt") if snapshot else None
    stale = False
    if published_at:
        resolved = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        maximum = timedelta(minutes=settings.subscription_reference_max_stale_minutes())
        stale = datetime.now(timezone.utc) - resolved > maximum
    return {
        "generation": snapshot.get("generation") if snapshot else None,
        "publishedAt": published_at,
        "stale": stale,
    }


def _diagnostic_values(
    identity: AzureIdentity | None, live: bool, store: ReferenceDataStore
) -> tuple[dict, bool]:
    """Return the inventory values to diagnose against and whether they are live.

    ``live`` queries Azure Resource Graph directly through the backend identity;
    it is a pure read that never republishes a generation, so it cannot mutate
    the enforced boundary. The cached path reads the published snapshot.
    """
    if live and identity is not None:
        return _live_subscription_values(identity), True
    snapshot = open_subscription_snapshot(store)
    return ((snapshot.get("values") or {}) if snapshot else {}), False


def diagnose_app_scope(
    identity: AzureIdentity | None = None,
    scope: ConfiguredAppScope | None = None,
    live: bool = False,
    store: ReferenceDataStore = reference_store,
) -> dict:
    """Resolve the configured scope for display without failing on an empty result.

    Unlike :func:`resolve_app_scope`, this never raises for an empty effective
    scope; it reports an ``isEmpty`` flag and a per-selector breakdown instead.
    Genuine infrastructure failures (storage read, invalid snapshot, live Azure
    query) still propagate. Only the App Scope Settings tab passes ``live=True``.
    """
    configured = scope or configured_scope()
    values, is_live = _diagnostic_values(identity, live, store)
    result: dict = {
        "mode": configured.mode,
        "modeLabel": mode_label(configured.mode),
        "subscriptionIds": list(configured.selectors) if configured.mode == MODE_SUBSCRIPTIONS else [],
        "managementGroupIds": list(configured.selectors) if configured.mode == MODE_MANAGEMENT_GROUPS else [],
        "locations": list(configured.locations),
        "enforced": _enforced_metadata(store),
        "live": is_live,
        "subscriptions": [],
        "managementGroups": [],
    }

    if configured.mode == MODE_SUBSCRIPTIONS:
        breakdown = [
            {
                "id": subscription_id,
                "resolved": values.get(subscription_id) is not None,
                "displayName": (values.get(subscription_id) or {}).get("displayName"),
            }
            for subscription_id in configured.selectors
        ]
        resolved = [item for item in breakdown if item["resolved"]]
        result["subscriptions"] = breakdown
        result["resolvedCount"] = len(resolved)
        result["hasUnresolved"] = len(resolved) < len(configured.selectors)
        result["isEmpty"] = len(resolved) == 0
    elif configured.mode == MODE_MANAGEMENT_GROUPS:
        per_group = []
        matched: set[str] = set()
        for management_group_id in configured.selectors:
            ids = _match_management_groups(values, (management_group_id,))
            matched.update(ids)
            per_group.append({"id": management_group_id, "count": len(ids)})
        result["managementGroups"] = per_group
        result["resolvedCount"] = len(matched)
        result["hasUnresolved"] = any(item["count"] == 0 for item in per_group)
        result["isEmpty"] = len(matched) == 0
    else:
        all_ids = canonical_values(values.keys())
        result["resolvedCount"] = len(all_ids)
        result["hasUnresolved"] = False
        result["isEmpty"] = len(all_ids) == 0

    return result