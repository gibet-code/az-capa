"""User-scoped ODCR coverage report endpoints (Easy Auth forwarded identity).

Unlike the collector pipelines (which run as the app's Managed Identity), these
routes act on behalf of the connected user: Resource Graph is queried with the
user's ARM token, so results are RBAC-trimmed to exactly what the user can see in
the portal. Locally the user identity resolves to the Azure CLI account.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
import urllib.parse

import azure.durable_functions as df
import azure.functions as func

from core import identity
from services.app_scope import resolve_app_scope
from . import _authorization as authorization
from ._app_scope_http import app_scope_error_response
from services import compute_skus, odcr_coverage_decisions, request_dimensions, zone_mapping
from services.subscription_context import enrich_rows_with_business_context
from services.odcr_coverage_decisions import build_visibility_query, parse_decision_change, subscription_id_from_resource_id
from clients.resource_graph import get_subscription_inventory, run_graph_query
from services.odcr_coverage import (
    apply_physical_zones,
    apply_odcr_eligibility,
    build_capacity_reservation_graph_query,
    build_vm_graph_query,
    enrich_vm_reservation_lifecycle,
    filter_vm_reservation_statuses,
    project_vm_list_row,
    reconcile_vm_reservations,
    vm_list_metadata,
    zone_mapping_pairs,
)

bp = df.Blueprint()
# Keep Resource Graph ID filters small enough for predictable query payloads;
# this is independent from Azure Table's 100-operation transaction limit.
_VISIBILITY_QUERY_CHUNK = 100


def _query_values(req: func.HttpRequest, name: str) -> list[str] | None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.url).query, keep_blank_values=True)
    return query.get(name)


def _vm_request_filters(req: func.HttpRequest) -> dict[str, list[str] | None]:
    if str(getattr(req, "method", "GET")).upper() == "POST":
        body = req.get_json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        subscriptions = body.get("subscriptionIds")
        if subscriptions is not None and (
            not isinstance(subscriptions, list)
            or any(not isinstance(value, str) for value in subscriptions)
        ):
            raise ValueError("subscriptionIds must be an array of strings.")
        locations = body.get("locations")
        if locations is not None and (
            not isinstance(locations, list)
            or any(not isinstance(value, str) or not value.strip() for value in locations)
        ):
            raise ValueError("locations must be an array of non-empty strings.")
        return {
            "subscriptionId": subscriptions,
            "location": locations,
            "contextFilters": request_dimensions.parse_context_filters(body.get("contextFilters")),
            "resourceGroup": None,
            "deploymentType": None,
            "vmSize": None,
            "vmssFlexible": None,
            "reservationStatus": None,
        }
    filters = {
        name: _query_values(req, name)
        for name in (
            "subscriptionId", "location", "resourceGroup", "deploymentType",
            "vmSize", "vmssFlexible", "reservationStatus",
        )
    }
    filters["contextFilters"] = []
    return filters


@bp.route(route="api/scope/subscriptions", methods=["GET"])
def subscription_inventory(req: func.HttpRequest) -> func.HttpResponse:
    """List user-visible subscriptions within the configured application boundary."""
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    try:
        who = identity.for_user_action(req)
    except identity.UserNotAuthenticated:
        return func.HttpResponse(
            json.dumps({"error": "not_authenticated", "message": "Sign in to list subscriptions."}),
            status_code=401,
            mimetype="application/json",
        )
    try:
        app_scope = resolve_app_scope(identity.for_backend_operation())
        allowed = set(app_scope.subscription_ids)
        inventory = [
            item for item in get_subscription_inventory(who.get_token())
            if item["id"].strip().lower() in allowed
        ]
    except Exception as exc:  # noqa: BLE001 - discovery failures must fail closed
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Subscription inventory query failed")
        return func.HttpResponse(
            json.dumps({"error": "scope_inventory_failed", "message": str(exc)}),
            status_code=502,
            mimetype="application/json",
        )
    return func.HttpResponse(
        json.dumps({"count": len(inventory), "value": inventory}),
        mimetype="application/json",
    )


@bp.route(route="api/global-filters/catalogue", methods=["GET"])
def global_filters_catalogue(req: func.HttpRequest) -> func.HttpResponse:
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    try:
        who = identity.for_user_action(req)
    except identity.UserNotAuthenticated:
        return func.HttpResponse(
            json.dumps({"error": "not_authenticated", "message": "Sign in to list Global filters."}),
            status_code=401,
            mimetype="application/json",
        )
    try:
        app_scope = resolve_app_scope(identity.for_backend_operation())
        allowed = set(app_scope.subscription_ids)
        visible_subscription_ids = [
            item["id"] for item in get_subscription_inventory(who.get_token())
            if str(item.get("id") or "").strip().lower() in allowed
        ]
        dimensions = request_dimensions.open_current_view(
            {"subscriptions", "locations", "business_context"}
        )
        body = dimensions.catalogue(
            visible_subscription_ids,
            app_scope.configured.locations or None,
        )
    except Exception as exc:  # noqa: BLE001 - catalogue failures must fail closed
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Global filters catalogue query failed")
        return func.HttpResponse(
            json.dumps({"error": "global_filters_catalogue_failed", "message": str(exc)}),
            status_code=502,
            mimetype="application/json",
        )
    return func.HttpResponse(json.dumps(body), mimetype="application/json")


@bp.route(route="api/odcr/coverage", methods=["GET", "POST"])
def vm_list(req: func.HttpRequest) -> func.HttpResponse:
    """List VMs the signed-in user can read (portal parity, via Resource Graph)."""
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    started_at = time.perf_counter()
    timings: dict[str, float] = {}

    def time_stage(name: str, action):
        stage_started_at = time.perf_counter()
        result = action()
        timings[name] = time.perf_counter() - stage_started_at
        return result

    try:
        who = identity.for_user_action(req)
    except identity.UserNotAuthenticated:
        return func.HttpResponse(
            json.dumps({"error": "not_authenticated", "message": "Sign in to list VMs."}),
            status_code=401,
            mimetype="application/json",
        )

    try:
        filters = _vm_request_filters(req)
        reservation_statuses = filters["reservationStatus"]
        requested_subscriptions = filters["subscriptionId"]
        requested_locations = filters["location"]
        requested_context = filters["contextFilters"]
        resource_groups = filters["resourceGroup"]
        deployment_types = filters["deploymentType"]
        vm_sizes = filters["vmSize"]
        vmss_flexible = filters["vmssFlexible"]
        build_vm_graph_query(
            resource_groups=resource_groups,
            locations=requested_locations,
            deployment_types=deployment_types,
            vm_sizes=vm_sizes,
            vmss_flexible=vmss_flexible,
            reservation_statuses=reservation_statuses,
        )
    except ValueError as exc:
        return func.HttpResponse(
            json.dumps({"error": "invalid_filter", "message": str(exc)}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        app_scope = time_stage(
            "app_scope",
            lambda: resolve_app_scope(identity.for_backend_operation()),
        )
        dimensions = request_dimensions.open_current_view(
            {"subscriptions", "locations", "business_context"}
        )
        effective_subscriptions = dimensions.filter_subscriptions(
            app_scope.subscription_ids,
            requested_subscriptions,
            requested_context,
        )
        effective_locations = dimensions.filter_locations(
            app_scope.configured.locations,
            requested_locations,
        )
    except Exception as exc:  # noqa: BLE001 - scope failures must fail closed
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Global app-scope resolution failed")
        return func.HttpResponse(
            json.dumps({"error": "scope_resolution_failed", "message": str(exc)}),
            status_code=502,
            mimetype="application/json",
        )

    if requested_subscriptions == [] or requested_locations == [] or not effective_subscriptions:
        metadata = vm_list_metadata()
        dimensions.enrich_rows([])
        metadata.update(dimensions.metadata())
        return func.HttpResponse(
            json.dumps({
                "count": 0,
                "metadata": metadata,
                "value": [],
            }),
            mimetype="application/json",
        )

    query = build_vm_graph_query(
        resource_groups=resource_groups,
        locations=effective_locations,
        deployment_types=deployment_types,
        vm_sizes=vm_sizes,
        vmss_flexible=vmss_flexible,
        reservation_statuses=reservation_statuses,
    )
    reservation_query = build_capacity_reservation_graph_query()

    try:
        token = time_stage("user_token", who.get_token)
        resource_graph_started_at = time.perf_counter()

        def run_timed_graph_query(
            graph_query: str,
            subscription_ids: tuple[str, ...] | None = None,
        ) -> tuple[list[dict], float]:
            query_started_at = time.perf_counter()
            result = run_graph_query(
                graph_query,
                token,
                subscription_ids=subscription_ids,
            )
            return result, time.perf_counter() - query_started_at

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            vm_future = executor.submit(run_timed_graph_query, query, effective_subscriptions)
            reservation_future = executor.submit(run_timed_graph_query, reservation_query)
            rows, timings["vm_resource_graph"] = vm_future.result()
            reservation_rows, timings["capacity_reservation_resource_graph"] = reservation_future.result()
        timings["resource_graph"] = time.perf_counter() - resource_graph_started_at
        pairs = zone_mapping_pairs(rows, reservation_rows)
        mappings = time_stage(
            "zone_mapping_lookup",
            lambda: zone_mapping.pipeline.get_mappings(pairs),
        )
        time_stage(
            "physical_zone_enrichment",
            lambda: apply_physical_zones(rows, reservation_rows, mappings),
        )
        rows = time_stage(
            "resource_graph_reconcile",
            lambda: reconcile_vm_reservations(rows, reservation_rows),
        )
        vm_count = len(rows)
        reservation_count = len({
            reservation_id
            for row in rows
            for reservation_id in (row.get("matchingCapacityReservationIds") or [])
            if reservation_id
        })
        rows = time_stage(
            "reservation_instance_views",
            lambda: enrich_vm_reservation_lifecycle(rows, token),
        )
        rows = time_stage("compute_sku_lookup", lambda: compute_skus.pipeline.enrich_vm_rows(rows))
        rows = time_stage("odcr_eligibility", lambda: apply_odcr_eligibility(rows))
        rows = time_stage(
            "reservation_status_filter",
            lambda: filter_vm_reservation_statuses(rows, reservation_statuses),
        )
        subscriptions = {row.get("subscriptionId") for row in rows if row.get("subscriptionId")}
        coverage_by_id = time_stage(
            "coverage_table_lookup",
            lambda: odcr_coverage_decisions.get_for_subscriptions(subscriptions),
        )

        def attach_coverage() -> None:
            for row in rows:
                row["odcrCoverage"] = coverage_by_id.get(str(row.get("id") or "").lower())

        time_stage("coverage_join", attach_coverage)
        time_stage(
            "request_dimensions_enrichment",
            lambda: dimensions.enrich_rows(rows),
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI as an error
        logging.exception("VM list query failed (identity source=%s)", who.source)
        return func.HttpResponse(
            json.dumps({"error": "query_failed", "message": str(exc)}),
            status_code=502,
            mimetype="application/json",
        )

    response_rows = [project_vm_list_row(row) for row in rows]
    response_metadata = vm_list_metadata()
    response_metadata.update(dimensions.metadata())
    body = time_stage(
        "json_serialization",
        lambda: json.dumps({
            "count": len(response_rows),
            "metadata": response_metadata,
            "value": response_rows,
        }),
    )
    timings["total"] = time.perf_counter() - started_at
    logging.info(
        "VM list performance: vms=%d reservations=%d output_bytes=%d timings_ms=%s",
        vm_count,
        reservation_count,
        len(body.encode("utf-8")),
        {name: round(seconds * 1000, 1) for name, seconds in timings.items()},
    )
    server_timing = ", ".join(
        f"{name};dur={seconds * 1000:.1f}" for name, seconds in timings.items()
    )
    return func.HttpResponse(
        body,
        mimetype="application/json",
        headers={
            "Server-Timing": server_timing,
            "X-VM-List-Counts": f"vms={vm_count}; reservations={reservation_count}",
        },
    )


def _json_response(body: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status_code, mimetype="application/json")


@bp.route(route="api/odcr/coverage/decisions", methods=["PUT"])
def update_odcr_coverage_decisions(req: func.HttpRequest) -> func.HttpResponse:
    """Apply one shared ODCR coverage decision to one or more visible VMs."""
    try:
        authorization.require_user(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    try:
        who = identity.for_user_action(req)
        token = who.get_token()
        audit = identity.user_audit_metadata(who, token)
    except identity.UserNotAuthenticated:
        return _json_response({"error": "not_authenticated", "message": "Sign in to update ODCR coverage."}, 401)
    except ValueError as exc:
        return _json_response({"error": "invalid_identity", "message": str(exc)}, 401)

    try:
        change = parse_decision_change(req.get_json())
    except (ValueError, TypeError) as exc:
        return _json_response({"error": "invalid_request", "message": str(exc)}, 400)

    requested_subscriptions = {
        subscription_id_from_resource_id(resource_id) for resource_id in change.resource_ids
    }
    try:
        app_scope = resolve_app_scope(identity.for_backend_operation())
    except Exception as exc:  # noqa: BLE001 - scope failures must fail closed
        mapped = app_scope_error_response(exc)
        if mapped is not None:
            return mapped
        logging.exception("Global app-scope resolution failed for ODCR coverage update")
        return _json_response({"error": "scope_resolution_failed", "message": str(exc)}, 502)
    if not requested_subscriptions.issubset(set(app_scope.subscription_ids)):
        return _json_response({
            "error": "vm_not_accessible",
            "message": "Every VM must be within the configured application scope and visible to the signed-in user.",
        }, 403)

    try:
        visible_ids: set[str] = set()
        for start in range(0, len(change.resource_ids), _VISIBILITY_QUERY_CHUNK):
            chunk = change.resource_ids[start:start + _VISIBILITY_QUERY_CHUNK]
            visible_ids.update(
                str(row.get("id") or "").lower()
                for row in run_graph_query(build_visibility_query(chunk), token)
                if row.get("id")
            )
    except Exception as exc:  # noqa: BLE001 - surfaced as a stable API error
        logging.exception("VM visibility validation failed for ODCR coverage update")
        return _json_response({"error": "visibility_check_failed", "message": str(exc)}, 502)
    missing_ids = [resource_id for resource_id in change.resource_ids if resource_id not in visible_ids]
    if missing_ids:
        return _json_response({
            "error": "vm_not_accessible",
            "message": "Every VM must be within the configured application scope and visible to the signed-in user.",
            "resourceIds": missing_ids,
        }, 403)

    try:
        updated = odcr_coverage_decisions.upsert_change(change, audit)
    except Exception as exc:  # noqa: BLE001 - surfaced as a stable API error
        logging.exception("ODCR coverage persistence failed")
        return _json_response({"error": "storage_failed", "message": str(exc)}, 502)
    return _json_response({"count": len(updated), "value": updated})
