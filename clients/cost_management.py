"""Scope-agnostic Cost Management query client (raw REST, no SDK dependency).

This is the reusable layer shared by any Cost Management consumer. Callers supply
their own ``query_filter`` (the *filter seam*), so the VM usage pipeline and a
future unused-capacity-reservations pipeline differ only by the filter they pass.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timezone

from core.settings import METHOD_PER_BILLING, METHOD_PER_SUB

_API_VERSION = "2023-11-01"


def build_scope(method: str, subscription_id=None, billing_account_id=None, billing_profile_id=None) -> str:
    """Return the ARM scope path for a Cost Management query."""
    if method == METHOD_PER_SUB:
        if not subscription_id:
            raise ValueError("per-sub scope requires a subscription_id.")
        return f"/subscriptions/{urllib.parse.quote(subscription_id, safe='')}"
    if method == METHOD_PER_BILLING:
        if not billing_account_id:
            raise ValueError("per-billing scope requires a billing_account_id.")
        scope = f"/providers/Microsoft.Billing/billingAccounts/{urllib.parse.quote(billing_account_id, safe='')}"
        if billing_profile_id:
            scope += f"/billingProfiles/{urllib.parse.quote(billing_profile_id, safe='')}"
        return scope
    raise ValueError(f"Unknown Cost Management method: {method!r}")


def _query_url(scope: str) -> str:
    return f"https://management.azure.com{scope}/providers/Microsoft.CostManagement/query?api-version={_API_VERSION}"


def dimension_filter(name: str, values) -> dict:
    """Build a single ``In`` dimension filter."""
    return {"dimensions": {"name": name, "operator": "In", "values": list(values)}}


def combine_filters(filters: list[dict]) -> dict | None:
    """Combine filters with a logical ``and`` (or return the single/None filter)."""
    filters = [f for f in filters if f]
    if not filters:
        return None
    return filters[0] if len(filters) == 1 else {"and": filters}


# Cost Management surfaces its per-window request-count reset only on 429s, via
# these headers. The binding limit is request COUNT (not QPU/response size), so
# honoring the reset value paces us exactly instead of guessing with back-off.
_RETRY_AFTER_HEADERS = (
    "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "Retry-After",
)


def _retry_after_seconds(headers) -> float | None:
    """Return the largest advertised retry-after (seconds), or None if absent."""
    if not headers:
        return None
    waits = []
    for name in _RETRY_AFTER_HEADERS:
        value = headers.get(name)
        if value:
            try:
                waits.append(float(value))
            except ValueError:
                pass
    return max(waits) if waits else None


def _post_query(url: str, token: str, body: dict, max_retries: int = 8) -> dict:
    """POST a Cost Management query, retrying transient failures with back-off."""
    retry_statuses = {429, 500, 502, 503, 504}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8")
    delay = 10.0
    for attempt in range(max_retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in retry_statuses or attempt == max_retries:
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"Cost Management query failed ({exc.code}): {detail}") from exc
            advertised = _retry_after_seconds(exc.headers)
            # Advertised reset is exact; add 1s slack. Fall back to back-off only
            # when the service gives us nothing to go on.
            wait = advertised + 1.0 if advertised is not None else delay
            logging.warning(
                "Cost query HTTP %s — retrying in %.0fs (attempt %d/%d)",
                exc.code, wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            delay = min(delay * 2, 120.0)
        except urllib.error.URLError:
            if attempt == max_retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 120.0)
    raise RuntimeError("Cost Management query exhausted retries")


def _day_bounds(start_date: str, end_date: str) -> tuple[str, str]:
    """Return ISO timestamps spanning the inclusive [start_date, end_date] days (UTC)."""
    start = datetime.combine(datetime.strptime(start_date, "%Y-%m-%d").date(), dtime.min, tzinfo=timezone.utc)
    end = datetime.combine(datetime.strptime(end_date, "%Y-%m-%d").date(), dtime.max.replace(microsecond=0), tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


def query_url(scope: str) -> str:
    """Public: the Cost Management query URL for an ARM scope (initial page POST)."""
    return _query_url(scope)


def build_query_body(
    start_date: str,
    end_date: str,
    query_filter: dict | None,
    export_type: str,
    agg_name,
    grouping: list[str] | None = None,
    granularity: str = "Daily",
) -> dict:
    """Build the Cost Management query request body for an inclusive day range.

    ``agg_name`` may be a single metric name or a list of names (e.g.
    ``["UsageQuantity", "Cost"]`` to fetch both in one call on MCA). Pagination
    re-POSTs this same body to the ``nextLink`` URL.
    """
    grouping = grouping or ["ResourceId"]
    agg_names = [agg_name] if isinstance(agg_name, str) else list(agg_name)
    from_iso, to_iso = _day_bounds(start_date, end_date)
    return {
        "type": export_type,
        "timeframe": "Custom",
        "timePeriod": {"from": from_iso, "to": to_iso},
        "dataset": {
            "granularity": granularity,
            "aggregation": {name: {"name": name, "function": "Sum"} for name in agg_names},
            "grouping": [{"type": "Dimension", "name": name} for name in grouping],
            "filter": query_filter,
        },
    }


def rows_from_payload(payload: dict) -> list[dict]:
    """Turn a query response page into row dicts keyed by column name."""
    props = payload.get("properties", {})
    columns = [col.get("name") for col in props.get("columns", [])]
    if not columns:
        return []
    return [dict(zip(columns, row)) for row in (props.get("rows", []) or [])]


def page_next_link(payload: dict) -> str | None:
    """Return the ``nextLink`` for the next page, or None when exhausted."""
    return payload.get("properties", {}).get("nextLink")


def post_query_once(url: str, token: str, body: dict) -> dict:
    """POST a single Cost Management query page — one attempt, no sleeping.

    Returns one of:
      * ``{"status": "ok", "payload": {...}}``
      * ``{"status": "throttled", "retryAfter": <seconds>}`` — 429/5xx/network
      * ``{"status": "error", "error": "<detail>"}`` — other 4xx

    The single-attempt shape lets a Durable orchestrator own the wait (via a
    durable timer) and refresh its token per page, instead of blocking an
    activity for the whole grind.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return {"status": "ok", "payload": json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code in {500, 502, 503, 504}:
            advertised = _retry_after_seconds(exc.headers)
            wait = advertised + 1.0 if advertised is not None else 20.0
            return {"status": "throttled", "retryAfter": wait}
        detail = exc.read().decode("utf-8", "replace")
        return {"status": "error", "error": f"Cost Management query failed ({exc.code}): {detail}"}
    except urllib.error.URLError as exc:
        return {"status": "throttled", "retryAfter": 20.0}


def query_metric(
    scope: str,
    token: str,
    start_date: str,
    end_date: str,
    query_filter: dict | None,
    export_type: str,
    agg_name,
    grouping: list[str] | None = None,
    granularity: str = "Daily",
) -> list[dict]:
    """Run one Cost Management query over an inclusive day range, following pagination.

    Batch helper for the local/sync path: accumulates all pages in memory. The
    Durable pipeline uses the per-page activity instead. Returns a list of row
    dicts keyed by the response column names.
    """
    body = build_query_body(start_date, end_date, query_filter, export_type, agg_name, grouping, granularity)
    rows: list[dict] = []
    next_url = _query_url(scope)
    while next_url:
        payload = _post_query(next_url, token, body)
        rows.extend(rows_from_payload(payload))
        next_url = page_next_link(payload)
    return rows
