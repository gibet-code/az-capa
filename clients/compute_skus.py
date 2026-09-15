"""Azure Compute SKU REST client."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from ._http import TRANSIENT_STATUS_CODES, azure_error_detail, retry_after_seconds

_API_VERSION = "2021-07-01"


def build_url(subscription_id: str) -> str:
    subscription = urllib.parse.quote(subscription_id, safe="")
    return (
        f"https://management.azure.com/subscriptions/{subscription}/"
        f"providers/Microsoft.Compute/skus?api-version={_API_VERSION}"
    )


def fetch_catalog_pages(subscription_id: str, token: str) -> dict:
    """Retrieve every unfiltered SKU page without applying domain normalization."""
    url = build_url(subscription_id)
    items: list[dict] = []
    pages = 0
    while url:
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_STATUS_CODES:
                return {
                    "status": "throttled" if exc.code == 429 else "transient",
                    "statusCode": exc.code,
                    "retryAfter": retry_after_seconds(exc.headers),
                }
            return {"status": "error", "statusCode": exc.code, "error": azure_error_detail(exc.read())}
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
            return {"status": "transient", "retryAfter": 5, "error": str(exc)}
        items.extend(payload.get("value") or [])
        pages += 1
        url = payload.get("nextLink")
    return {"status": "ok", "items": items, "records": len(items), "pages": pages}