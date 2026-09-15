"""Admin SPA: static file serving + UI-facing JSON APIs.

Served directly by the Function app (no separate Static Web App). The SPA lives
at ``/`` and its data APIs under ``/api/*`` (host.json sets ``routePrefix=""`` and
these routes carry the ``api/`` prefix). All routes are ANONYMOUS; Easy Auth
(App Service Authentication) gates the app in Azure, and local func-tools runs open.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import azure.durable_functions as df
import azure.functions as func
from azure.durable_functions.models.OrchestrationRuntimeStatus import (
    OrchestrationRuntimeStatus,
)

from core import credentials, identity, settings

from . import _authorization as authorization

from services.app_scope import diagnose_app_scope
from ._durable import DurableClientAdapter
from .data_collection_bp import get_dispatcher

bp = df.Blueprint()

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_CONTENT_TYPES = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

_TERMINAL_STATUSES = [
    OrchestrationRuntimeStatus.Completed,
    OrchestrationRuntimeStatus.Failed,
    OrchestrationRuntimeStatus.Canceled,
    OrchestrationRuntimeStatus.Terminated,
]

# Age bound for the "recent/completed" query; running tasks are fetched unbounded.
_NOTIFICATIONS_WINDOW_DAYS = 7


# ── Static assets ─────────────────────────────────────────────────────────────
def _serve(filename: str) -> func.HttpResponse:
    path = WEB_DIR / filename
    if not path.is_file():
        return func.HttpResponse(f"{filename} not found", status_code=404)
    content_type = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
    return func.HttpResponse(path.read_bytes(), mimetype=content_type)


# Catch-all under the (empty) route prefix: serves known SPA assets by name and
# falls back to index.html for everything else (client-side hash routing). The
# function is named to sort LAST — this host matches overlapping routes in
# function-name order, so a greedy `{*path}` must come after every `api/*` route.
_ASSETS = {
    "app.js",
    "alpine.min.js",
    "echarts.min.js",
    "styles/base.css",
    "styles/controls.css",
    "styles/vm-coverage.css",
    "styles/drawers.css",
    "styles/responsive.css",
    "js/features/administration.js",
    "js/features/data-collection.js",
    "js/features/coverage-editor.js",
    "js/features/coverage-report-model.js",
    "js/features/notifications.js",
    "js/features/odcr-usage-model.js",
    "js/features/odcr-usage.js",
    "js/features/scope.js",
    "js/features/vm-coverage.js",
    "js/features/vm-coverage-charts.js",
    "js/shared/resources.js",
    "js/shared/http.js",
    "js/shared/tables.js",
    "js/shared/csv.js",
    "js/shared/business-context.js",
    "js/shared/report-model.js",
    "js/shared/echarts-adapter.js",
    "js/shared/preference-store.js",
    "icons/capacity-reservation-groups.svg",
    "icons/generic-resource.svg",
    "icons/reserved-capacity.svg",
    "icons/virtual-machine.svg",
}


@bp.route(route="{*path}", methods=["GET"])
def zzz_spa_fallback(req: func.HttpRequest) -> func.HttpResponse:
    path = (req.route_params.get("path") or "").strip("/")
    # Never masquerade an API path as HTML if an api route ever sorts after this.
    if path.startswith("api/"):
        return func.HttpResponse("Not found", status_code=404)
    if path in _ASSETS:
        return _serve(path)
    return _serve("index.html")


# ── Settings API ──────────────────────────────────────────────────────────────
@bp.route(route="api/settings", methods=["GET"])
def api_settings(req: func.HttpRequest) -> func.HttpResponse:
    """Loaded app settings (no redaction — internal admin surface behind Easy Auth)."""
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    if credentials.is_running_locally():
        table_storage = "UseDevelopmentStorage=true (local)"
    else:
        try:
            table_storage = f"{settings.table_storage_endpoint()} (managed identity)"
        except ValueError as exc:
            table_storage = f"<error: {exc}>"

    view = {
        "scope": {
            "subscriptionIds": settings.scope_subscription_ids(),
            "managementGroupIds": settings.scope_management_group_ids(),
            "locations": settings.scope_locations(),
        },
        "costManagement": {
            "method": settings.cost_management_method(),
            "agreementType": settings.cost_management_agreement_type(),
            "billingAccountId": settings.cost_management_billing_account_id(),
            "billingProfileId": settings.cost_management_billing_profile_id(),
            "maxConcurrency": settings.cost_management_max_concurrency(),
            "timeChunkDays": settings.cost_management_time_chunk_days(),
            "finalizationLagDays": settings.cost_finalization_lag_days(),
        },
        "vmUsage": {
            "historicalDays": settings.usage_historical_days(),
            "tableName": settings.vm_usage_table_name(),
            "checkpointTableName": settings.vm_usage_checkpoint_table_name(),
        },
        "crUsage": {
            "tableName": settings.cr_usage_table_name(),
            "checkpointTableName": settings.cr_usage_checkpoint_table_name(),
        },
        "activityLog": {
            "historicalDays": settings.activity_log_historical_days(),
            "refreshOverlapHours": settings.activity_log_refresh_overlap_hours(),
            "ingestionLagMinutes": settings.activity_log_ingestion_lag_minutes(),
            "maxConcurrency": settings.activity_log_max_concurrency(),
            "replayMaxUnitsPerRun": settings.activity_log_replay_max_units_per_run(),
            "pendingUnknownHours": settings.activity_log_pending_unknown_hours(),
            "operationsTableName": settings.activity_log_operations_table_name(),
            "collectionStateTableName": settings.activity_log_collection_state_table_name(),
            "spillContainerName": settings.activity_log_spill_container_name(),
            "locations": "All (SCOPE_LOCATIONS ignored)",
        },
        "zoneMapping": {
            "maxConcurrency": settings.zone_mapping_max_concurrency(),
            "containerName": settings.zone_mapping_blob_container_name(),
            "snapshotBlob": "current.json.gz",
            "locations": "All (SCOPE_LOCATIONS ignored)",
        },
        "computeSkus": {
            "containerName": settings.compute_sku_blob_container_name(),
            "manifestBlob": "current.json",
            "schedule": "Daily at 05:30 UTC",
            "locations": "All (unfiltered global union)",
        },
        "businessContext": {
            "maxFields": settings.subscription_context_max_fields(),
            "maxUploadBytes": settings.subscription_context_max_upload_bytes(),
            "refreshLeaseSeconds": settings.subscription_context_refresh_lease_seconds(),
            "refreshSchedule": "Every 15 minutes",
            "maxStaleMinutes": settings.subscription_context_max_stale_minutes(),
            "tableName": settings.subscription_context_table_name(),
            "containerName": settings.subscription_context_blob_container_name(),
        },
        "runtime": {
            "runningLocally": credentials.is_running_locally(),
            "tableStorage": table_storage,
        },
    }
    return func.HttpResponse(json.dumps(view), mimetype="application/json")


# ── App Scope diagnostics API ──────────────────────────────────────
@bp.route(route="api/app_scope", methods=["GET"])
def api_app_scope(req: func.HttpRequest) -> func.HttpResponse:
    """Live App Scope diagnostic for the App Scope Settings tab (self-contained).

    Runs a live inventory read, so it is kept off the general ``/api/settings``
    surface to avoid paying for the Azure query on every settings load.
    """
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    try:
        payload = diagnose_app_scope(identity.for_backend_operation(), live=True)
    except Exception as exc:  # noqa: BLE001 - infra failures shown as a sanitized error
        logging.exception("App Scope diagnostics failed")
        payload = {"error": str(exc)}
    return func.HttpResponse(json.dumps(payload), mimetype="application/json")


# ── Identity API ──────────────────────────────────────────────────────────────
@bp.route(route="api/me", methods=["GET"])
def api_me(req: func.HttpRequest) -> func.HttpResponse:
    """Return one normalized principal and capability contract in every environment."""
    try:
        current = authorization.principal(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    name = current.name
    detail = current.identity
    if current.local:
        try:
            # Display metadata only: local app roles still come exclusively from
            # the simulator cookie, never from this ARM-audience CLI token.
            claims = identity.decode_jwt_claims(identity.for_user_action(req).get_token())
            upn = str(
                claims.get("upn")
                or claims.get("preferred_username")
                or claims.get("unique_name")
                or ""
            ).strip()
            name = str(claims.get("name") or upn or current.name).strip()
            detail = upn or current.identity
        except Exception as exc:  # Azure CLI unavailable/offline: retain local fallback
            logging.info("Could not resolve local Azure CLI display identity: %s", exc)
    body = {
        "local": current.local,
        "name": name,
        "detail": detail,
        "authenticated": not current.local,
        "roles": sorted(current.roles),
        "tenantId": current.tenant_id,
        "objectId": current.object_id,
        "capabilities": {
            "useOdcr": bool(current.roles.intersection({authorization.USER_ROLE, authorization.ADMIN_ROLE})),
            "administer": authorization.ADMIN_ROLE in current.roles,
        },
    }
    return func.HttpResponse(json.dumps(body), mimetype="application/json")


@bp.route(route="api/me/role", methods=["POST"])
def api_me_role(req: func.HttpRequest) -> func.HttpResponse:
    """Switch the local role simulator; this endpoint does not exist in Azure."""
    if not credentials.is_running_locally():
        return func.HttpResponse("Not found", status_code=404)
    try:
        body = req.get_json()
        role = body.get("role") if isinstance(body, dict) else None
        cookie = authorization.local_role_cookie(role)
    except (ValueError, TypeError):
        return func.HttpResponse(
            json.dumps({"error": "invalid_role", "message": "role must be Admin, User, or None."}),
            status_code=400,
            mimetype="application/json",
        )
    return func.HttpResponse(
        json.dumps({"role": role}),
        mimetype="application/json",
        headers={"Set-Cookie": cookie},
    )


# ── Notifications API (RunHistory-backed) ─────────────────────────────────────
@bp.route(route="api/notifications", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def api_notifications(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Recent pipeline runs (7d) plus any still-running run regardless of age."""
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    since = datetime.now(timezone.utc) - timedelta(days=_NOTIFICATIONS_WINDOW_DAYS)
    items = await get_dispatcher().notifications(DurableClientAdapter(client), since)
    return func.HttpResponse(json.dumps(items), mimetype="application/json")


@bp.route(route="api/notifications/{instanceId}", methods=["DELETE"])
@bp.durable_client_input(client_name="client")
async def api_notification_delete(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Discard one terminal run's history row (and purge its Durable instance)."""
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    instance_id = req.route_params.get("instanceId")
    outcome = await get_dispatcher().delete_run(DurableClientAdapter(client), instance_id)
    if outcome == "not_found":
        return func.HttpResponse(
            json.dumps({"error": "notification_not_found", "message": "Notification not found."}),
            status_code=404,
            mimetype="application/json",
        )
    if outcome == "running":
        return func.HttpResponse(
            json.dumps({
                "error": "notification_not_terminated",
                "message": "Cannot discard notification while the run is still in progress.",
                "instanceId": instance_id,
            }),
            status_code=409,
            mimetype="application/json",
        )
    try:
        await client.purge_instance_history(instance_id)
    except Exception:  # Durable cleanup is best-effort; the app row is already gone.
        logging.warning("purge_instance_history failed for %s", instance_id, exc_info=True)
    return func.HttpResponse(
        json.dumps({"deleted": 1, "instanceId": instance_id}),
        mimetype="application/json",
    )


@bp.route(route="api/notifications", methods=["DELETE"])
@bp.durable_client_input(client_name="client")
async def api_notifications_delete(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Discard all terminal run history while preserving running runs."""
    try:
        authorization.require_admin(req)
    except authorization.AuthorizationError as exc:
        return authorization.error_response(exc)
    deleted = get_dispatcher().clear_terminal_runs()
    try:
        await client.purge_instance_history_by(
            created_time_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
            created_time_to=datetime.now(timezone.utc),
            runtime_status=_TERMINAL_STATUSES,
        )
    except Exception:  # Durable cleanup is best-effort; the app rows are already gone.
        logging.warning("purge_instance_history_by failed", exc_info=True)
    return func.HttpResponse(
        json.dumps({"deleted": deleted}),
        mimetype="application/json",
    )
