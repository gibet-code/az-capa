"""Azure Resource Graph helpers (raw REST, no SDK dependency).

Provides just enough to resolve management groups to the subscriptions beneath
them, mirroring the logic of ``core.management_groups`` in the az-toolbox
project. Uses ``urllib`` to stay dependency-light, consistent with the Cost
Management client.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterable

from ._http import retry_after_seconds

_RG_URL = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
# KQL: every management group's immutable ID (name) in the tenant.
_MANAGEMENT_GROUPS_KQL = (
    "resourcecontainers "
    "| where type =~ 'microsoft.management/managementgroups' "
    "| project name"
)

# Every subscription visible to the calling identity, including the complete
# management-group ancestor chain used by the global scope picker.
_SUBSCRIPTIONS_KQL = (
    "resourcecontainers "
    "| where type =~ 'microsoft.resources/subscriptions' "
    "| project id = subscriptionId, name, state = tostring(properties.state), "
    "managementGroupAncestors = properties.managementGroupAncestorsChain, tags "
    "| order by id asc"
)

# KQL: subscriptions whose ancestor chain contains any requested management group.
_SUBSCRIPTIONS_UNDER_MG_KQL = (
    "resourcecontainers "
    "| where type =~ 'microsoft.resources/subscriptions' "
    "| mv-expand chain = properties.managementGroupAncestorsChain "
    "| extend mgId = tolower(tostring(chain.name)) "
    "| where mgId in ({mg_ids}) "
    "| distinct subscriptionId"
)

def _post(url: str, token: str, body: dict, max_retries: int = 6) -> dict:
    retry_statuses = {429, 500, 502, 503, 504}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8")
    delay = 5.0
    for attempt in range(max_retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in retry_statuses or attempt == max_retries:
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError(f"Resource Graph query failed ({exc.code}): {detail}") from exc
            wait = retry_after_seconds(exc.headers, fallback=int(delay))
            logging.warning("Resource Graph HTTP %s — retrying in %.0fs (attempt %d/%d)", exc.code, wait, attempt + 1, max_retries)
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
        except urllib.error.URLError:
            if attempt == max_retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    raise RuntimeError("Resource Graph query exhausted retries")


def run_graph_query(
    kql: str,
    token: str,
    *,
    subscription_ids: Iterable[str] | None = None,
) -> list[dict]:
    """Run a Resource Graph query, optionally scoped to subscriptions."""
    subscriptions = None
    if subscription_ids is not None:
        subscriptions = list(dict.fromkeys(
            str(value).strip().lower()
            for value in subscription_ids
            if str(value).strip()
        ))
        if not subscriptions:
            return []

    rows: list[dict] = []
    skip_token = None
    while True:
        options = {"resultFormat": "objectArray", "$top": 1000}
        if skip_token:
            options["$skipToken"] = skip_token
        request = {"query": kql, "options": options}
        if subscriptions is not None:
            request["subscriptions"] = subscriptions
        payload = _post(_RG_URL, token, request)
        rows.extend(payload.get("data", []) or [])
        skip_token = payload.get("$skipToken")
        if not skip_token:
            break
    return rows


def _normalize_management_group_id(value: str) -> str:
    """Return the bare management-group ID from a bare ID or full ARM resource ID."""
    text = (value or "").strip().rstrip("/")
    marker = "/managementgroups/"
    idx = text.lower().rfind(marker)
    if idx != -1:
        text = text[idx + len(marker):]
    return text


def get_accessible_subscriptions(token: str, subscription_ids=None) -> list[str]:
    """Return subscription IDs visible to the calling identity."""
    requested = sorted({str(value).strip().lower() for value in (subscription_ids or []) if str(value).strip()})
    if subscription_ids is not None and not requested:
        return []
    query = _SUBSCRIPTIONS_KQL
    if requested:
        values = ", ".join("'" + value.replace("'", "''") + "'" for value in requested)
        query = query.replace(
            "| project id = subscriptionId",
            f"| where subscriptionId in~ ({values})\n| project id = subscriptionId",
            1,
        )
    rows = run_graph_query(query, token)
    subscription_ids = sorted({row["id"] for row in rows if row.get("id")})
    logging.info("Resolved %d subscription(s) visible to the calling identity.", len(subscription_ids))
    return subscription_ids


def get_subscription_inventory(token: str) -> list[dict]:
    """Return visible subscriptions and their normalized management-group chains."""
    rows = run_graph_query(_SUBSCRIPTIONS_KQL, token)
    inventory: list[dict] = []
    for row in rows:
        subscription_id = str(row.get("id") or "").strip()
        if not subscription_id:
            continue
        ancestors = []
        for ancestor in row.get("managementGroupAncestors") or []:
            management_group_id = str(ancestor.get("name") or "").strip()
            if not management_group_id:
                continue
            display_name = str(ancestor.get("displayName") or management_group_id).strip()
            ancestors.append({"id": management_group_id, "name": display_name})
        inventory.append({
            "id": subscription_id,
            "name": str(row.get("name") or subscription_id).strip(),
            "state": str(row.get("state") or "").strip(),
            "managementGroupAncestors": ancestors,
            "tags": {
                str(key): "" if value is None else str(value)
                for key, value in (row.get("tags") or {}).items()
            } if isinstance(row.get("tags"), dict) else {},
        })
    inventory.sort(key=lambda item: (item["name"].lower(), item["id"].lower()))
    return inventory


def get_subscriptions_under_management_groups(token: str, management_group_ids) -> list[str]:
    """Return distinct subscription IDs under the given management groups.

    Management groups are matched on their immutable ID (case-insensitive).
    Unknown IDs raise :class:`ValueError`.
    """
    requested = [_normalize_management_group_id(mg) for mg in (management_group_ids or [])]
    requested = [mg for mg in requested if mg]
    if not requested:
        raise ValueError("At least one management group ID must be provided.")

    known_rows = run_graph_query(_MANAGEMENT_GROUPS_KQL, token)
    known_by_id = {row["name"].lower(): row["name"] for row in known_rows if row.get("name")}

    seen: set[str] = set()
    matched: list[str] = []
    unmatched: list[str] = []
    for mg in requested:
        key = mg.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in known_by_id:
            matched.append(known_by_id[key])
        else:
            unmatched.append(mg)

    if unmatched:
        raise ValueError(
            "Unknown management group ID(s): " + ", ".join(sorted(unmatched))
            + ". Matching is on the management group ID (case-insensitive), not the display name."
        )

    mg_ids_kql = ", ".join(f"'{mg.lower()}'" for mg in matched)
    rows = run_graph_query(_SUBSCRIPTIONS_UNDER_MG_KQL.format(mg_ids=mg_ids_kql), token)
    subscription_ids = sorted({row["subscriptionId"] for row in rows if row.get("subscriptionId")})
    logging.info("Resolved %d subscription(s) under %d management group(s).", len(subscription_ids), len(matched))
    return subscription_ids
