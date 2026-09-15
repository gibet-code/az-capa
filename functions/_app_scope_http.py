"""Shared HTTP mapping for App Scope resolution failures.

Read endpoints resolve the effective application scope from the published
Subscription snapshot. When that snapshot has not yet been warmed (cold start),
resolution raises ``AppScopeInitializingError`` and the caller should return a
retryable ``app_scope_initializing`` (HTTP 503). When the configured scope
resolves to zero subscriptions, resolution raises ``AppScopeEmptyError`` and the
caller should return the admin-facing ``app_scope_empty`` (HTTP 409).
"""
from __future__ import annotations

import json

import azure.functions as func

from core.app_scope import AppScopeEmptyError, AppScopeInitializingError


def app_scope_error_response(exc: Exception) -> func.HttpResponse | None:
    """Map an App Scope resolution failure to its HTTP response, else ``None``."""
    if isinstance(exc, AppScopeInitializingError):
        payload = {"error": "app_scope_initializing", "message": str(exc)}
        status_code = 503
    elif isinstance(exc, AppScopeEmptyError):
        payload = {"error": "app_scope_empty", "message": str(exc)}
        status_code = 409
    else:
        return None
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )