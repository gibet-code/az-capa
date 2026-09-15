"""Azure Activity Log REST client and source-event normalization.

The client is generic transport: it requests the management events endpoint,
follows opaque ``nextLink`` pages, and normalizes each event into the canonical
source envelope (Section 2) preserving the full ``properties`` bag. It performs
no recipe matching, projection, or status filtering — those are service concerns
(Section 3, step 5).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from ._http import TRANSIENT_STATUS_CODES, azure_error_detail, retry_after_seconds

API_VERSION = "2017-03-01-preview"
SCHEMA_VERSION = 1

_EVENT_CHANNELS = "Admin, Operation"
_LEVELS = "Critical,Error,Warning,Informational"


def _localizable_value(value) -> str | None:
    if isinstance(value, dict):
        return value.get("value") or value.get("localizedValue")
    return value


def _resource_type_from_id(resource_id: str | None) -> str | None:
    """Derive ``namespace/type[/subtype]`` from an ARM resource id."""
    if not resource_id:
        return None
    segments = [segment for segment in resource_id.split("/") if segment]
    lowered = [segment.lower() for segment in segments]
    if "providers" not in lowered:
        return None
    start = lowered.index("providers")
    tail = segments[start + 1:]
    if not tail:
        return None
    parts = [tail[0]]  # namespace
    parts.extend(tail[index] for index in range(1, len(tail), 2))  # every type segment
    return "/".join(parts)


def build_filter(start: str, end: str, operations=None, resource_provider: str | None = None) -> str:
    """Build the ``$filter`` for one work unit.

    ``operations`` drives the efficient case-insensitive ``operations eq`` push
    down. When no operation filter is available a coarser ``resourceProvider eq``
    is pushed instead (Section 3). Both are optional; an absent selector imposes
    no filter on that dimension.
    """
    clauses = [
        f"eventTimestamp ge '{start}'",
        f"eventTimestamp le '{end}'",
        f"eventChannels eq '{_EVENT_CHANNELS}'",
    ]
    if operations:
        operation_text = ",".join(value.replace("'", "''") for value in operations)
        clauses.append(f"operations eq '{operation_text}'")
    elif resource_provider:
        clauses.append(f"resourceProvider eq '{resource_provider.replace(chr(39), chr(39) * 2)}'")
    clauses.append(f"levels eq '{_LEVELS}'")
    return " and ".join(clauses)


def build_url(subscription_id: str, filter_value: str) -> str:
    base = (
        "https://management.azure.com/subscriptions/"
        f"{urllib.parse.quote(subscription_id, safe='')}/providers/"
        "microsoft.insights/eventtypes/management/values"
    )
    query = urllib.parse.urlencode({
        "api-version": API_VERSION,
        "$filter": filter_value,
    })
    return f"{base}?{query}"


def normalize_event(event: dict) -> dict:
    """Normalize one raw Activity Log event into the canonical envelope."""
    resource_id = event.get("resourceId")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventDataId": event.get("eventDataId"),
        "eventName": _localizable_value(event.get("eventName")),
        "eventTimestamp": event.get("eventTimestamp"),
        "submissionTimestamp": event.get("submissionTimestamp"),
        "subscriptionId": event.get("subscriptionId"),
        "resourceId": resource_id,
        "resourceType": _resource_type_from_id(resource_id),
        "resourceGroupName": event.get("resourceGroupName"),
        "resourceProviderName": _localizable_value(event.get("resourceProviderName")),
        "operationName": _localizable_value(event.get("operationName")),
        "operationId": event.get("operationId"),
        "correlationId": event.get("correlationId"),
        "status": _localizable_value(event.get("status")),
        "subStatus": _localizable_value(event.get("subStatus")),
        "caller": event.get("caller"),
        "properties": event.get("properties") or {},
    }


def normalize_events(payload: dict) -> list[dict]:
    return [normalize_event(event) for event in (payload.get("value") or [])]


def fetch_page(request: dict, token: str) -> dict:
    """Fetch and normalize one Activity Log page without persisting it."""
    attempted_url = request.get("nextLink") or build_url(
        request["subscriptionId"],
        build_filter(
            request["start"],
            request["end"],
            operations=request.get("operations"),
            resource_provider=request.get("resourceProvider"),
        ),
    )
    http_request = urllib.request.Request(attempted_url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Prefer": "wait=75",
    })
    try:
        with urllib.request.urlopen(http_request, timeout=80) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in TRANSIENT_STATUS_CODES:
            return {
                "status": "throttled" if exc.code == 429 else "transient",
                "statusCode": exc.code,
                "retryAfter": retry_after_seconds(exc.headers),
            }
        return {"status": "error", "statusCode": exc.code, "error": azure_error_detail(exc.read())}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"status": "transient", "retryAfter": 5, "error": str(exc)}
    return {
        "status": "ok",
        "received": len(payload.get("value", []) or []),
        "events": normalize_events(payload),
        "nextLink": payload.get("nextLink"),
    }