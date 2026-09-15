"""Application-role authorization for Azure Functions HTTP adapters.

The Easy Auth ``X-MS-CLIENT-PRINCIPAL`` header is the only Azure source of
application roles. The separately forwarded access token remains exclusively an
ARM credential. Local runs use an HttpOnly cookie to simulate app-role contexts.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Iterable

import azure.functions as func

from core import credentials

USER_ROLE = "User"
ADMIN_ROLE = "Admin"
LOCAL_ROLE_COOKIE = "az_capacity_local_role"
LOCAL_ROLES = (ADMIN_ROLE, USER_ROLE, "None")


@dataclass(frozen=True)
class ClientPrincipal:
    name: str
    identity: str
    roles: frozenset[str]
    local: bool = False
    tenant_id: str | None = None
    object_id: str | None = None


class AuthorizationError(Exception):
    def __init__(self, status_code: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message


def error_response(exc: AuthorizationError) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": exc.error, "message": exc.message}),
        status_code=exc.status_code,
        mimetype="application/json",
    )


def _header(req: func.HttpRequest, name: str) -> str | None:
    headers = getattr(req, "headers", None) or {}
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), None)


def _local_role(req: func.HttpRequest) -> str:
    raw_cookie = _header(req, "Cookie") or ""
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:
        return ADMIN_ROLE
    morsel = cookie.get(LOCAL_ROLE_COOKIE)
    role = morsel.value if morsel is not None else ADMIN_ROLE
    return role if role in LOCAL_ROLES else ADMIN_ROLE


def _claim_values(claims: Iterable[dict], claim_type: str) -> list[str]:
    return [
        str(claim.get("val"))
        for claim in claims
        if claim.get("typ") == claim_type and claim.get("val") is not None
    ]


def _azure_principal(req: func.HttpRequest) -> ClientPrincipal:
    encoded = _header(req, "X-MS-CLIENT-PRINCIPAL")
    if not encoded:
        raise AuthorizationError(401, "not_authenticated", "Sign in to use this application.")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.b64decode(padded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise AuthorizationError(401, "invalid_principal", "The authenticated principal is malformed.") from exc

    claims = payload.get("claims")
    auth_type = payload.get("auth_typ")
    if not auth_type or not isinstance(claims, list) or any(not isinstance(item, dict) for item in claims):
        raise AuthorizationError(401, "invalid_principal", "The authenticated principal is malformed.")

    role_type = payload.get("role_typ")
    role_claim_types = {
        "roles",
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
    }
    if isinstance(role_type, str) and role_type:
        role_claim_types.add(role_type)
    roles = frozenset(
        value
        for claim_type in role_claim_types
        for value in _claim_values(claims, claim_type)
    )
    name_type = payload.get("name_typ")
    names = _claim_values(claims, name_type) if isinstance(name_type, str) and name_type else []
    identity_values = (
        _claim_values(claims, "preferred_username")
        or _claim_values(claims, "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
        or names
    )
    identity_value = identity_values[0] if identity_values else ""
    name = names[0] if names else identity_value or "Signed in"
    tenant_values = _claim_values(claims, "tid") or _claim_values(
        claims, "http://schemas.microsoft.com/identity/claims/tenantid"
    )
    object_values = _claim_values(claims, "oid") or _claim_values(
        claims, "http://schemas.microsoft.com/identity/claims/objectidentifier"
    )
    return ClientPrincipal(
        name=name,
        identity=identity_value,
        roles=roles,
        tenant_id=tenant_values[0] if tenant_values else None,
        object_id=object_values[0] if object_values else None,
    )


def principal(req: func.HttpRequest) -> ClientPrincipal:
    if credentials.is_running_locally():
        role = _local_role(req)
        roles = frozenset() if role == "None" else frozenset({role})
        return ClientPrincipal(
            name="Local dev",
            identity="Azure CLI identity",
            roles=roles,
            local=True,
        )
    return _azure_principal(req)


def require_user(req: func.HttpRequest) -> ClientPrincipal:
    current = principal(req)
    if not current.roles.intersection({USER_ROLE, ADMIN_ROLE}):
        raise AuthorizationError(403, "forbidden", "The User or Admin app role is required.")
    return current


def require_admin(req: func.HttpRequest) -> ClientPrincipal:
    current = principal(req)
    if ADMIN_ROLE not in current.roles:
        raise AuthorizationError(403, "forbidden", "The Admin app role is required.")
    return current


def local_role_cookie(role: str) -> str:
    if role not in LOCAL_ROLES:
        raise ValueError("role must be Admin, User, or None")
    return f"{LOCAL_ROLE_COOKIE}={role}; Path=/; HttpOnly; SameSite=Lax"