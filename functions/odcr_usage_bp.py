"""User-scoped ODCR usage endpoints."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time

import azure.durable_functions as df
import azure.functions as func

from core import identity
from services.app_scope import resolve_app_scope
from . import _authorization as authorization
from ._app_scope_http import app_scope_error_response
from services import cr_usage, request_dimensions, zone_mapping
from services.subscription_context import enrich_rows_with_business_context
from services.odcr_usage import (
    apply_physical_zones,
    build_capacity_reservation_graph_query,
    build_capacity_reservation_group_graph_query,
    zone_mapping_pairs,
)
from clients.resource_graph import get_accessible_subscriptions, run_graph_query

bp = df.Blueprint()


def _request_filters(req: func.HttpRequest) -> dict[str, list[str] | None]:
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
    }


def _json_response(body: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status_code, mimetype="application/json")


@bp.route(route="api/odcr_usage/list", methods=["POST"])
def odcr_usage_list(req: func.HttpRequest) -> func.HttpResponse:
    """List capacity reservations visible to the signed-in user."""
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
        return _json_response({
            "error": "not_authenticated",
            "message": "Sign in to list capacity reservations.",
        }, 401)

    try:
        filters = _request_filters(req)
    except (ValueError, TypeError) as exc:
        return _json_response({"error": "invalid_filter", "message": str(exc)}, 400)

    requested_subscriptions = filters["subscriptionId"]
    requested_locations = filters["location"]
    requested_context = filters["contextFilters"]
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
        logging.exception("Global app-scope resolution failed for ODCR usage")
        return _json_response({"error": "scope_resolution_failed", "message": str(exc)}, 502)

    if requested_subscriptions == [] or requested_locations == [] or not effective_subscriptions:
        metadata, _ = cr_usage.usage_view([])
        dimensions.enrich_rows([])
        metadata.update(dimensions.metadata())
        return _json_response({
            "schemaVersion": 2,
            "count": 0,
            "counts": {"capacityReservationGroups": 0, "capacityReservations": 0},
            "metadata": metadata,
            "capacityReservationGroups": [],
            "capacityReservations": [],
            "value": [],
        })

    group_query = build_capacity_reservation_group_graph_query(
        locations=effective_locations,
    )
    reservation_query = build_capacity_reservation_graph_query()
    try:
        token = time_stage("user_token", who.get_token)
        resource_graph_started_at = time.perf_counter()

        def run_timed_graph_query(query: str) -> tuple[list[dict], float]:
            query_started_at = time.perf_counter()
            result = run_graph_query(
                query,
                token,
                subscription_ids=effective_subscriptions,
            )
            return result, time.perf_counter() - query_started_at

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            group_future = executor.submit(run_timed_graph_query, group_query)
            reservation_future = executor.submit(run_timed_graph_query, reservation_query)
            groups, timings["capacity_reservation_group_resource_graph"] = group_future.result()
            rows, timings["capacity_reservation_resource_graph"] = reservation_future.result()
        timings["resource_graph"] = time.perf_counter() - resource_graph_started_at

        groups_by_id = {
            str(group.get("id") or "").strip().lower(): group
            for group in groups
            if group.get("id")
        }
        rows = [
            row for row in rows
            if str(row.get("capacityReservationGroupId") or "").strip().lower() in groups_by_id
        ]
        for row in rows:
            group = groups_by_id[str(row["capacityReservationGroupId"]).strip().lower()]
            row["subscriptionId"] = group.get("subscriptionId")
            row["location"] = group.get("location")
        mappings = time_stage(
            "zone_mapping_lookup",
            lambda: zone_mapping.pipeline.get_mappings(zone_mapping_pairs(rows)),
        )
        time_stage("physical_zone_enrichment", lambda: apply_physical_zones(rows, mappings))
        metadata, usage_by_id = time_stage(
            "historical_usage_lookup",
            lambda: cr_usage.usage_view(effective_subscriptions),
        )
        usage_by_id = {
            str(resource_id or "").strip().lower(): row
            for resource_id, row in usage_by_id.items()
            if resource_id
        }
        graph_subscriptions = {
            str(group.get("subscriptionId") or "").strip().lower()
            for group in groups
            if group.get("subscriptionId")
        }
        historical_subscriptions = {
            str(row.get("subscriptionId") or "").strip().lower()
            for row in usage_by_id.values()
            if row.get("subscriptionId")
        }
        unknown_subscriptions = sorted(historical_subscriptions - graph_subscriptions)
        accessible_unknown_subscriptions = {
            value.lower()
            for value in time_stage(
                "accessible_subscriptions",
                lambda: get_accessible_subscriptions(token, unknown_subscriptions)
                if unknown_subscriptions else [],
            )
        }
        allowed_historical_subscriptions = graph_subscriptions | accessible_unknown_subscriptions
        usage_by_id = {
            resource_id: row
            for resource_id, row in usage_by_id.items()
            if str(row.get("subscriptionId") or "").strip().lower() in allowed_historical_subscriptions
        }
        if effective_locations:
            usage_by_id = {
                resource_id: row
                for resource_id, row in usage_by_id.items()
                if str(row.get("capacityReservationGroupId") or "").strip().lower() in groups_by_id
            }

        def join_usage() -> None:
            for row in rows:
                resource_id = str(row.get("id") or "").strip().lower()
                summary = usage_by_id.pop(resource_id, None)
                row.update(
                    cr_usage.usage_metrics(summary)
                    if summary is not None
                    else cr_usage.empty_usage(metadata)
                )
                row["resourceExists"] = True
            for historical_row in usage_by_id.values():
                historical_row["resourceExists"] = False
                rows.append(historical_row)

        time_stage("usage_join", join_usage)
        for row in rows:
            group_id = str(row.get("capacityReservationGroupId") or "").strip().lower()
            if not group_id or group_id in groups_by_id:
                continue
            group = {
                "id": group_id,
                "resourceType": "microsoft.compute/capacityreservationgroups",
                "name": row.get("capacityReservationGroupName"),
                "subscriptionId": row.get("subscriptionId"),
                "subscriptionName": row.get("subscriptionName"),
                "resourceGroup": row.get("resourceGroup"),
                "location": None,
                "deploymentType": None,
                "resourceExists": False,
            }
            groups_by_id[group_id] = group
            groups.append(group)
        time_stage(
            "request_dimensions_enrichment",
            lambda: dimensions.enrich_rows([*groups, *rows]),
        )
        metadata.update(dimensions.metadata())
        for row in rows:
            for field in (
                "subscriptionId", "subscriptionName", "resourceGroup", "location",
                "capacityReservationGroupName",
            ):
                row.pop(field, None)
        time_stage("sort", lambda: rows.sort(key=lambda row: str(row.get("id") or "")))
        groups.sort(key=lambda group: str(group.get("id") or ""))
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a stable error
        logging.exception("ODCR usage list query failed (identity source=%s)", who.source)
        return _json_response({"error": "query_failed", "message": str(exc)}, 502)

    body = time_stage(
        "json_serialization",
        lambda: json.dumps({
            "schemaVersion": 2,
            "count": len(rows),
            "counts": {
                "capacityReservationGroups": len(groups),
                "capacityReservations": len(rows),
            },
            "metadata": metadata,
            "capacityReservationGroups": groups,
            "capacityReservations": rows,
            "value": rows,
        }),
    )
    timings["total"] = time.perf_counter() - started_at
    logging.info(
        "ODCR usage list performance: groups=%d reservations=%d output_bytes=%d timings_ms=%s",
        len(groups),
        len(rows),
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
            "X-ODCR-Usage-Counts": f"groups={len(groups)},reservations={len(rows)}",
        },
    )
