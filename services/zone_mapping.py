"""Zone-mapping refresh planning, direct REST retrieval, and persistence.

Collects the logical->physical availability-zone mapping for every region a
subscription can see (``GET /subscriptions/{id}/locations``) and publishes a
compressed Blob snapshot. There is no time dimension — mappings are static
until Azure changes them. Scope (subscriptions or management groups) is honored;
``SCOPE_LOCATIONS`` is intentionally ignored (all locations are collected).

Refresh is incremental: only added subscriptions are queried and removed ones are
deleted. A region-drift pre-check lists locations on a single subscription first;
if Azure exposes a region not yet stored, a full refresh of every subscription is
triggered so the new region's mapping is captured everywhere.
"""
from __future__ import annotations

import logging

from clients.azure_locations import fetch_zone_mapping_locations as _fetch_locations
from core import settings
from core.app_scope import MODE_MANAGEMENT_GROUPS, MODE_SUBSCRIPTIONS, configured_scope
from core.identity import for_backend_operation
from services.app_scope import resolve_app_scope
from storage.zone_mapping_store import ZoneMappingBlobStore

class ZoneMappingPipeline:
    def __init__(self, store: ZoneMappingBlobStore) -> None:
        self._store = store

    def _scope(self, identity) -> dict:
        configured = configured_scope()
        subscriptions = resolve_app_scope(
            identity, configured, allow_live_fallback=True
        ).subscription_ids
        return {
            "subs": list(subscriptions),
            "configSelectors": {
                "subscriptions": list(configured.selectors) if configured.mode == MODE_SUBSCRIPTIONS else [],
                "managementGroups": list(configured.selectors) if configured.mode == MODE_MANAGEMENT_GROUPS else [],
            },
        }

    @staticmethod
    def _describe(scope: dict) -> str:
        selectors = scope.get("configSelectors") or {}
        if selectors.get("managementGroups"):
            source = f"management group(s) {selectors['managementGroups']}"
        elif selectors.get("subscriptions"):
            source = f"{len(scope['subs'])} configured subscription(s)"
        else:
            source = "subscriptions visible to the calling identity"
        return f"scope={source} ({len(scope['subs'])} subscriptions), locations=all (SCOPE_LOCATIONS ignored)"

    def resolve_plan(self, _request: dict) -> dict:
        identity = for_backend_operation()
        scope = self._scope(identity)
        token = identity.get_token()
        state = self._store.get_state()
        current_subs = set(scope["subs"])
        previous_subs = set(state["subs"]) if state else set()
        known_regions = set(state["knownRegions"]) if state else set()

        # Region-drift pre-check: list locations on one subscription and compare the
        # full region set against what we have stored. A newly exposed region forces
        # a full refresh so every subscription captures the new mapping.
        probe_sub = sorted(current_subs)[0]
        probe = _fetch_locations(probe_sub, token)
        if probe["status"] != "ok":
            return {
                "action": "fail",
                "reason": "region drift check failed",
                "message": f"Could not list locations on {probe_sub} ({probe.get('status')}): {probe.get('error') or probe.get('statusCode')}.",
            }
        probed_regions = {region["region"] for region in probe["regions"]}
        new_regions = probed_regions - known_regions if state else set()

        added = current_subs - previous_subs
        removed = sorted(previous_subs - current_subs)
        full_refresh = state is None or bool(new_regions)
        targets = sorted(current_subs) if full_refresh else sorted(added)

        if state is None:
            reason = "initial-full"
        elif new_regions:
            reason = "region-drift-full"
        elif added or removed:
            reason = "reconcile"
        else:
            reason = "noop"

        new_state = {
            **scope,
            "knownRegions": sorted(known_regions | probed_regions),
        }
        return {
            "action": "load",
            "reason": reason,
            "fullRefresh": full_refresh,
            "newRegions": sorted(new_regions),
            "deletions": removed,
            "workUnits": [{"subscriptionId": sub} for sub in targets],
            "maxConcurrency": settings.zone_mapping_max_concurrency(),
            "newState": new_state,
        }

    def fetch_and_store(self, request: dict) -> dict:
        subscription_id = request["subscriptionId"]
        token = for_backend_operation().get_token()
        result = _fetch_locations(subscription_id, token)
        if result["status"] in ("throttled", "transient"):
            return {
                "subscriptionId": subscription_id,
                "status": result["status"],
                "retryAfter": result.get("retryAfter", 5),
            }
        if result["status"] == "error":
            return {"subscriptionId": subscription_id, "status": "error", "error": result.get("error")}
        try:
            written = self._store.replace_subscription_mappings(subscription_id, result["regions"])
        except Exception as exc:  # noqa: BLE001 — returned to Durable as a failed subscription
            logging.exception("Zone mapping persistence failed for subscription %s", subscription_id)
            return {"subscriptionId": subscription_id, "status": "error", "error": str(exc)}
        return {
            "subscriptionId": subscription_id,
            "status": "ok",
            "regions": len(result["regions"]),
            "upserted": written,
        }

    def delete_subscriptions(self, subscription_ids: list) -> dict:
        return {"deleted": self._store.delete_subscriptions_data(subscription_ids)}

    def write_state(self, state: dict) -> dict:
        return {"ok": True, **self._store.publish_snapshot(state)}

    def status(self, _request: dict) -> dict:
        scope = self._scope(for_backend_operation())
        state = self._store.get_state()
        updated_at = state.get("updatedAt") if state else None
        return {
            "hasData": state is not None,
            "isRefreshEnabled": True,
            "warning": None,
            "subscriptionsStored": len(state["subs"]) if state else 0,
            "regionsKnown": len(state["knownRegions"]) if state else 0,
            "lastUpdated": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
            "currentScope": self._describe(scope),
            "lastLoadedScope": self._describe(state) if state else None,
        }

    def get_mappings(self, pairs) -> dict[tuple[str, str], dict[str, str]]:
        return self._store.get_mappings(pairs)

    def flush_delete_data(self, _input: dict) -> dict:
        return {"deleted": self._store.delete_snapshot_data()}

    def flush_ensure_storage(self, _input: dict) -> dict:
        return {"ready": self._store.ensure_container()}


_store = ZoneMappingBlobStore(settings.zone_mapping_blob_container_name)
pipeline = ZoneMappingPipeline(_store)
